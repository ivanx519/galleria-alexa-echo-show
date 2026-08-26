"""
REST API per la skill Alexa Slideshow Foto.
Gira standalone sulla porta 8770, dietro nginx (/foto-slideshow-api/).

Mescolamento persistente "stile Spotify": un ordine casuale delle foto viene
generato una volta e salvato su disco assieme alla posizione corrente. Ogni
richiesta "avanti" sposta il cursore e lo salva — così la prossima volta che
Alexa viene aperta si riparte da dove si era rimasti, mai dall'inizio.
Quando si arriva in fondo all'ordine, si rimescola daccapo (nuovo ordine,
cursore a zero) invece di richiudere il ciclo sullo stesso ordine.
"""
import json
import logging
import random
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_PORT = 8770
API_TOKEN = "CHANGE-ME-your-own-secret-token"

RIDIMENSIONATE = Path("/opt/foto_slideshow/ridimensionate")
MINIATURE = Path("/opt/foto_slideshow/miniature")
STATO_PATH = Path("/opt/foto_slideshow/stato_shuffle.json")
STATO_RICERCA_PATH = Path("/opt/foto_slideshow/stato_ricerca.json")
META_PATH = Path("/opt/foto_slideshow/meta.json")
# Risultato dell'ultimo giro di rileva_duplicati.py (script offline, va
# rilanciato a mano) — vedi handle_duplicati più sotto.
DUPLICATI_PATH = Path("/opt/foto_slideshow/duplicati.json")
IMPOSTAZIONI_PATH = Path("/opt/foto_slideshow/impostazioni.json")

ESTENSIONI_VALIDE = {".jpg", ".jpeg"}

# --- Upload dall'app Galleria Alexa (24/08/2026) — salvano nelle stesse
# cartelle "originali" che ridimensiona.py/ridimensiona_video.py già
# leggevano finora (finora popolate solo manualmente via rsync/cartella
# "upload"). ORIGINALI/VIDEO_ORIGINALI qui sotto sono nuove per server.py,
# ma sono ESATTAMENTE gli stessi percorsi già usati da quegli script — le
# estensioni accettate in upload sono le stesse che loro sanno già
# processare (duplicate qui come costanti a sé, stesso principio di
# ESTENSIONI_VALIDE/ESTENSIONI_VIDEO_VALIDE sopra: script diversi, nessun
# modulo condiviso in questo progetto, coerente con lo stile esistente).
ORIGINALI = Path("/opt/foto_slideshow/originali")
VIDEO_ORIGINALI = Path("/opt/foto_slideshow/video_originali")
ESTENSIONI_UPLOAD_FOTO = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
ESTENSIONI_UPLOAD_VIDEO = {".mp4", ".mov"}
# Cartella di questo progetto sul VPS (dove vivono ridimensiona.py e
# ridimensiona_video.py, copiati lì dal deploy — vedi .github/workflows/
# deploy.yml) — server.py invece gira da "api/" sotto la stessa cartella.
_DIR_PROGETTO = Path(__file__).resolve().parent.parent

# --- Video (Fase 1: "isola" separata dalle foto, comando vocale dedicato
# "mostra i video" — non mescolati nello stesso slideshow automatico delle
# foto, vedi nota architetturale in lambda_function.py sul perché non è
# stato fatto insieme, 23/08/2026). Stesso principio di mescolamento
# persistente delle foto, ma stato/cursore completamente separati.
VIDEO_RIDIM = Path("/opt/foto_slideshow/video_ridimensionati")
MINIATURE_VIDEO = Path("/opt/foto_slideshow/miniature_video")
STATO_VIDEO_PATH = Path("/opt/foto_slideshow/stato_shuffle_video.json")
META_VIDEO_PATH = Path("/opt/foto_slideshow/meta_video.json")
ESTENSIONI_VIDEO_VALIDE = {".mp4"}
# Ricerca video/mista (24/08/2026) — stessa filosofia della ricerca foto
# (stato "usa e getta", non tocca i cursori/ordini persistenti principali),
# file di stato dedicati e separati.
STATO_RICERCA_VIDEO_PATH = Path("/opt/foto_slideshow/stato_ricerca_video.json")
STATO_RICERCA_MISTO_PATH = Path("/opt/foto_slideshow/stato_ricerca_misto.json")
# Verificato col vero budget byte (23/08/2026): il documento video è più
# leggero di quello foto, margine ampio anche con 40 — deve combaciare con
# DIMENSIONE_BLOCCO_VIDEO in lambda_function.py.
BATCH_MASSIMO_VIDEO = 40

# --- Fase 2 (23/08/2026): "mostra foto e video" — versione mescolata, non
# un terzo cursore/stato nuovo, riusa i due già esistenti (stato_shuffle.json
# per le foto, stato_shuffle_video.json per i video) e li interlaccia in un
# unico blocco. 1 video ogni RAPPORTO_VIDEO_MISTO elementi — scelta
# arbitraria di partenza (le foto sono ~10900, i video 584: un rapporto
# proporzionale alla libreria li mostrerebbe troppo raramente), da
# aggiustare in base a come si sente dal vivo.
RAPPORTO_VIDEO_MISTO = 6
BATCH_MASSIMO_MISTO = 40

DURATA_DEFAULT_SEC = 10
DURATE_VALIDE_SEC = {5, 10, 15, 30, 60, 300}

# "Accadde oggi" (24/08/2026, richiesto per l'APP dopo la rimozione dalla
# skill — l'annuncio vocale ripetuto ogni volta che si apriva/mostrava la
# galleria era diventato fastidioso, vedi Notion). Il VPS gira in UTC
# (Etc/UTC) — "oggi" va calcolato nel fuso di Ivan, altrimenti vicino alla
# mezzanotte risulterebbe il giorno sbagliato.
FUSO_ITALIA = ZoneInfo("Europe/Rome")

# Tetto alla dimensione di un batch — evita richieste "n" abnormi che
# sposterebbero il cursore troppo avanti in un colpo solo. Deve combaciare
# con DIMENSIONE_BLOCCO in lambda_function.py (vedi lì per il perché di 65).
BATCH_MASSIMO = 65

# Ricerca per mese ("mostra dicembre" — qualsiasi anno), per "mese anno"
# ("mostra dicembre 2023" — solo quell'anno, 24/08/2026) o per luogo
# ("mostra Villaspeciosa") con lo stesso comando vocale: i 12 mesi sono un
# elenco chiuso e piccolo (a differenza delle città, potenzialmente
# migliaia) quindi si possono elencare a mano invece di usare un tipo di
# slot Alexa generico.
MESI_IT = {
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
}
# Pattern "mese anno" (es. "dicembre 2023") — un solo mese seguito da un
# anno a 4 cifre plausibile. re.escape non serve, i nomi mesi sono lettere.
_PATTERN_MESE_ANNO = re.compile(
    r"^(" + "|".join(sorted(MESI_IT)) + r")\s+(\d{4})$"
)


def _check_auth(request: web.Request) -> bool:
    return request.headers.get("Authorization", "") == f"Bearer {API_TOKEN}"


def _check_auth_binario(request: web.Request) -> bool:
    """L'endpoint immagine viene caricato direttamente dal componente Image di
    APL (una richiesta HTTP semplice del dispositivo Alexa), che non può
    aggiungere un header Authorization — serve quindi anche un token in query
    string come alternativa, solo per questa rotta."""
    if _check_auth(request):
        return True
    return request.query.get("token") == API_TOKEN


def _auth_error() -> web.Response:
    return web.json_response({"error": "Unauthorized"}, status=401)


def _carica_meta() -> dict:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("meta.json corrotto, didascalie disabilitate")
    return {}


def _carica_meta_video() -> dict:
    if META_VIDEO_PATH.exists():
        try:
            return json.loads(META_VIDEO_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("meta_video.json corrotto, didascalie video disabilitate")
    return {}


def _didascalia(nome: str, meta: dict) -> str:
    return meta.get(nome, {}).get("didascalia", "")


_MESI_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
}


def _data_completa_it(nome: str, meta: dict) -> str:
    """"17 Settembre 2019" da "data_iso" (vedi ridimensiona.py) — per la
    griglia miniature dell'app Galleria Alexa (24/08/2026), che raggruppa
    le foto per questa stringa: due foto con la stessa data qui finiscono
    nello stesso gruppo. "Data sconosciuta" per le foto senza EXIF."""
    iso = meta.get(nome, {}).get("data_iso")
    if not iso:
        return "Data sconosciuta"
    anno, mese, giorno = iso.split("-")
    return f"{int(giorno)} {_MESI_IT[int(mese)]} {int(anno)}"


def _chiave_ordinamento_cronologico(nome: str, meta: dict) -> str:
    # "0000-00-00" come fallback: essendo la stringa più piccola possibile in
    # ordine ISO, con sorted(..., reverse=True) le foto senza data finiscono
    # sempre in fondo (mai sparse in mezzo alle altre), qualunque sia il loro
    # nome file.
    return meta.get(nome, {}).get("data_iso") or "0000-00-00"


def _didascalia_video(nome: str, meta: dict) -> str:
    # A differenza di meta.json (foto), qui "luogo" e "data" restano
    # separati (vedi geocodifica_video.py) — combinati qui in lettura
    # invece che una volta sola in scrittura, stessa forma "Luogo · Mese Anno".
    v = meta.get(nome, {})
    luogo, data = v.get("luogo", ""), v.get("data", "")
    if luogo and data:
        return f"{luogo} · {data}"
    return luogo or data


def _data_completa_it_video(nome: str, meta: dict) -> str:
    """Mirror di _data_completa_it (foto) — "17 Settembre 2019" da
    "data_iso" (vedi ridimensiona_video.py/estrai_data_completa_video.py),
    per la griglia miniature video dell'app Galleria Alexa (24/08/2026)."""
    iso = meta.get(nome, {}).get("data_iso")
    if not iso:
        return "Data sconosciuta"
    anno, mese, giorno = iso.split("-")
    return f"{int(giorno)} {_MESI_IT[int(mese)]} {int(anno)}"


def _chiave_ordinamento_cronologico_video(nome: str, meta: dict) -> str:
    return meta.get(nome, {}).get("data_iso") or "0000-00-00"


def _lista_foto() -> list[str]:
    return sorted(
        p.name for p in RIDIMENSIONATE.iterdir()
        if p.is_file() and p.suffix.lower() in ESTENSIONI_VALIDE
    )


def _carica_stato() -> dict:
    if STATO_PATH.exists():
        try:
            return json.loads(STATO_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Stato shuffle corrotto, ricreo da zero")
    return {"ordine": [], "posizione": 0}


def _salva_stato(stato: dict) -> None:
    STATO_PATH.write_text(json.dumps(stato))


def _rimescola(escludi_prima: str | None = None) -> list[str]:
    """Nuovo ordine casuale. Se possibile evita che la prima foto del nuovo
    giro sia identica all'ultima vista nel giro precedente (transizione meno
    ripetitiva tra un ciclo e l'altro)."""
    foto = _lista_foto()
    random.shuffle(foto)
    if escludi_prima and len(foto) > 1 and foto[0] == escludi_prima:
        foto[0], foto[1] = foto[1], foto[0]
    return foto


def _stato_valido(stato: dict) -> dict:
    """Assicura che l'ordine salvato rifletta ancora le foto presenti su
    disco (es. dopo un nuovo giro di rsync+resize) e che la posizione sia
    dentro i limiti. Rimescola se serve, non tocca nulla se è già tutto ok."""
    foto_attuali = set(_lista_foto())
    ordine = stato.get("ordine", [])
    ordine_valido = ordine and set(ordine) == foto_attuali

    if not ordine_valido:
        if not foto_attuali:
            return {"ordine": [], "posizione": 0}
        ultima = ordine[stato["posizione"]] if ordine and 0 <= stato.get("posizione", 0) < len(ordine) else None
        stato = {"ordine": _rimescola(escludi_prima=ultima), "posizione": 0}
    elif stato.get("posizione", 0) >= len(ordine):
        stato["posizione"] = 0

    return stato


def _foto_corrente(stato: dict, meta: dict) -> dict | None:
    if not stato["ordine"]:
        return None
    nome = stato["ordine"][stato["posizione"]]
    return {
        "nome": nome,
        "didascalia": _didascalia(nome, meta),
        "posizione": stato["posizione"] + 1,
        "totale": len(stato["ordine"]),
    }


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "servizio": "foto-slideshow", "n_foto": len(_lista_foto())})


async def handle_attuale(request: web.Request) -> web.Response:
    """Foto al cursore corrente, SENZA avanzare — usata quando la skill si apre."""
    if not _check_auth(request):
        return _auth_error()
    stato = _stato_valido(_carica_stato())
    _salva_stato(stato)
    foto = _foto_corrente(stato, _carica_meta())
    if foto is None:
        return web.json_response({"error": "Nessuna foto disponibile"}, status=404)
    return web.json_response(foto)


async def handle_avanti(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return _auth_error()
    stato = _stato_valido(_carica_stato())
    if not stato["ordine"]:
        return web.json_response({"error": "Nessuna foto disponibile"}, status=404)

    nuova_pos = stato["posizione"] + 1
    if nuova_pos >= len(stato["ordine"]):
        ultima = stato["ordine"][stato["posizione"]]
        stato = {"ordine": _rimescola(escludi_prima=ultima), "posizione": 0}
    else:
        stato["posizione"] = nuova_pos

    _salva_stato(stato)
    return web.json_response(_foto_corrente(stato, _carica_meta()))


async def handle_indietro(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return _auth_error()
    stato = _stato_valido(_carica_stato())
    if not stato["ordine"]:
        return web.json_response({"error": "Nessuna foto disponibile"}, status=404)

    # In fondo all'indietro (posizione 0): resta ferma sulla prima invece di
    # sfondare nel giro precedente (evita di mostrare l'ultima foto già vista
    # del ciclo scorso, che romperebbe la sensazione di "sempre nuovo").
    stato["posizione"] = max(0, stato["posizione"] - 1)

    _salva_stato(stato)
    return web.json_response(_foto_corrente(stato, _carica_meta()))


async def handle_batch(request: web.Request) -> web.Response:
    """Un blocco di N foto consecutive nell'ordine mescolato persistente,
    a partire dalla posizione corrente (la prima del blocco è la stessa
    foto che darebbe /attuale). Il cursore avanza di N — la chiamata
    successiva a /batch riparte esattamente da dove questa finisce, senza
    ripetizioni. Serve al Pager+AutoPage lato APL: precaricando un blocco di
    foto la skill può farle scorrere in automatico SENZA richiamare la
    Lambda ad ogni singola foto (che riattiverebbe il microfono ogni volta —
    vedi bug #7 sul Freezer, stesso principio qui applicato al video)."""
    if not _check_auth(request):
        return _auth_error()
    try:
        n = min(int(request.query.get("n", 15)), BATCH_MASSIMO)
    except ValueError:
        n = 15
    n = max(1, n)

    stato = _stato_valido(_carica_stato())
    if not stato["ordine"]:
        return web.json_response({"error": "Nessuna foto disponibile"}, status=404)

    meta = _carica_meta()
    risultato = []
    for _ in range(min(n, len(stato["ordine"]))):
        nome = stato["ordine"][stato["posizione"]]
        risultato.append({
            "nome": nome, "didascalia": _didascalia(nome, meta),
            "preferita": bool(meta.get(nome, {}).get("preferita", False)),
        })
        nuova_pos = stato["posizione"] + 1
        if nuova_pos >= len(stato["ordine"]):
            stato = {"ordine": _rimescola(escludi_prima=nome), "posizione": 0}
        else:
            stato["posizione"] = nuova_pos

    _salva_stato(stato)
    return web.json_response({"foto": risultato})


def _carica_stato_ricerca() -> dict:
    if STATO_RICERCA_PATH.exists():
        try:
            return json.loads(STATO_RICERCA_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Stato ricerca corrotto, ricreo da zero")
    return {"termine": "", "ordine": [], "posizione": 0}


def _salva_stato_ricerca(stato: dict) -> None:
    STATO_RICERCA_PATH.write_text(json.dumps(stato))


def _filtra_per_termine(meta: dict, termine_norm: str) -> tuple[str, set[str]]:
    """Ritorna (tipo, nomi_trovati) — condivisa tra ricerca foto e ricerca
    video/mista (24/08/2026): meta.json e meta_video.json hanno lo stesso
    schema di campi "data" ("Mese Anno") e "luogo". Tre casi, in ordine di
    priorità: "mese anno" esatto (es. "dicembre 2023", NUOVO il 24/08/2026 —
    solo quell'anno), solo mese (qualsiasi anno, comportamento originale),
    altrimenti luogo."""
    m = _PATTERN_MESE_ANNO.match(termine_norm)
    if m:
        mese, anno = m.group(1), m.group(2)
        trovati = {nome for nome, v in meta.items()
                   if (v.get("data") or "").lower() == f"{mese} {anno}"}
        return "mese_anno", trovati
    if termine_norm in MESI_IT:
        # "data" è salvata come "Dicembre 2020" — confronto sulla prima parola
        trovati = {nome for nome, v in meta.items()
                   if (v.get("data") or "").lower().split(" ")[0] == termine_norm}
        return "mese", trovati
    trovati = {nome for nome, v in meta.items()
               if (v.get("luogo") or "").lower() == termine_norm}
    return "luogo", trovati


async def handle_cerca(request: web.Request) -> web.Response:
    """Ricerca per mese (qualsiasi anno, es. "dicembre") o per luogo (es.
    "Villaspeciosa") — stesso comando vocale "mostra {termine}", questo
    endpoint decide da solo di che tipo di ricerca si tratta. Non tocca il
    cursore/ordine persistente della galleria principale (quello resta dove
    l'utente l'aveva lasciato).

    Un comando vocale nuovo ("mostra dicembre") riparte sempre da un
    mescolamento fresco. Solo "continua=1" — usato dalla Lambda quando un
    blocco esaurisce le sue foto durante la STESSA ricerca ancora in corso
    (vedi lambda_function.py, blocco_esaurito) — riprende dal cursore
    salvato, così le foto mostrate dopo le prime BATCH_MASSIMO sono sempre
    nuove invece di essere le stesse rimescolate a caso."""
    if not _check_auth(request):
        return _auth_error()
    termine = (request.query.get("q") or "").strip()
    if not termine:
        return web.json_response({"error": "Parametro 'q' mancante"}, status=400)
    continua = request.query.get("continua") == "1"

    meta = _carica_meta()
    termine_norm = termine.lower()
    tipo, trovate = _filtra_per_termine(meta, termine_norm)

    # Solo foto ancora presenti come file ridimensionato (meta.json può
    # contenere voci più vecchie di foto nel frattempo rimosse).
    disponibili = set(_lista_foto())
    trovate &= disponibili
    totale_trovate = len(trovate)

    stato = _carica_stato_ricerca()
    # Riusa il cursore solo se è davvero la continuazione della STESSA
    # ricerca sullo STESSO insieme di risultati (stessa logica di
    # "_stato_valido" applicata alla galleria principale) — altrimenti
    # (termine diverso, o foto nuove/rimosse nel frattempo) riparte pulito.
    if not (continua and stato.get("termine") == termine_norm and set(stato.get("ordine", [])) == trovate):
        ordine = list(trovate)
        random.shuffle(ordine)
        stato = {"termine": termine_norm, "ordine": ordine, "posizione": 0}

    fine = min(stato["posizione"] + BATCH_MASSIMO, len(stato["ordine"]))
    blocco_nomi = stato["ordine"][stato["posizione"]:fine]
    stato["posizione"] = fine
    _salva_stato_ricerca(stato)

    risultato = [{"nome": nome, "didascalia": _didascalia(nome, meta)} for nome in blocco_nomi]
    return web.json_response({
        "tipo": tipo,
        "termine": termine,
        "totale_trovate": totale_trovate,
        "foto": risultato,
    })


def _lista_video() -> list[str]:
    return sorted(
        p.name for p in VIDEO_RIDIM.iterdir()
        if p.is_file() and p.suffix.lower() in ESTENSIONI_VIDEO_VALIDE
    )


def _carica_stato_video() -> dict:
    if STATO_VIDEO_PATH.exists():
        try:
            return json.loads(STATO_VIDEO_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Stato shuffle video corrotto, ricreo da zero")
    return {"ordine": [], "posizione": 0}


def _salva_stato_video(stato: dict) -> None:
    STATO_VIDEO_PATH.write_text(json.dumps(stato))


def _rimescola_video(escludi_prima: str | None = None) -> list[str]:
    video = _lista_video()
    random.shuffle(video)
    if escludi_prima and len(video) > 1 and video[0] == escludi_prima:
        video[0], video[1] = video[1], video[0]
    return video


def _stato_valido_video(stato: dict) -> dict:
    video_attuali = set(_lista_video())
    ordine = stato.get("ordine", [])
    ordine_valido = ordine and set(ordine) == video_attuali

    if not ordine_valido:
        if not video_attuali:
            return {"ordine": [], "posizione": 0}
        ultimo = ordine[stato["posizione"]] if ordine and 0 <= stato.get("posizione", 0) < len(ordine) else None
        stato = {"ordine": _rimescola_video(escludi_prima=ultimo), "posizione": 0}
    elif stato.get("posizione", 0) >= len(ordine):
        stato["posizione"] = 0

    return stato


async def handle_video_batch(request: web.Request) -> web.Response:
    """Stesso principio di handle_batch (foto): blocco di N video
    nell'ordine mescolato persistente, cursore che avanza e si salva —
    la chiamata successiva riparte da dove questa finisce. Didascalia da
    meta_video.json (luogo+data, vedi geocodifica_video.py) — vuota per i
    video non ancora geocodificati o senza GPS nel file originale."""
    if not _check_auth(request):
        return _auth_error()
    try:
        n = min(int(request.query.get("n", BATCH_MASSIMO_VIDEO)), BATCH_MASSIMO_VIDEO)
    except ValueError:
        n = BATCH_MASSIMO_VIDEO
    n = max(1, n)

    stato = _stato_valido_video(_carica_stato_video())
    if not stato["ordine"]:
        return web.json_response({"error": "Nessun video disponibile"}, status=404)

    meta_video = _carica_meta_video()
    risultato = []
    for _ in range(min(n, len(stato["ordine"]))):
        nome = stato["ordine"][stato["posizione"]]
        risultato.append({
            "nome": nome, "didascalia": _didascalia_video(nome, meta_video),
            "preferita": bool(meta_video.get(nome, {}).get("preferita", False)),
        })
        nuova_pos = stato["posizione"] + 1
        if nuova_pos >= len(stato["ordine"]):
            stato = {"ordine": _rimescola_video(escludi_prima=nome), "posizione": 0}
        else:
            stato["posizione"] = nuova_pos

    _salva_stato_video(stato)
    return web.json_response({"video": risultato})


async def handle_video_binario(request: web.Request) -> web.Response:
    if not _check_auth_binario(request):
        return _auth_error()
    nome = request.match_info["nome"]
    if "/" in nome or "\\" in nome or not nome.lower().endswith(".mp4"):
        return web.json_response({"error": "Nome non valido"}, status=400)
    path = VIDEO_RIDIM / nome
    if not path.is_file():
        return web.json_response({"error": "Video non trovato"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def handle_miniatura_video(request: web.Request) -> web.Response:
    """Mirror di handle_miniatura (foto) — per la griglia di sfoglio video
    dell'app Galleria Alexa (24/08/2026). Il nome della miniatura è sempre
    "<stem>.jpg" (vedi _genera_miniatura in ridimensiona_video.py), non
    "<stem>.mp4" come il video."""
    if not _check_auth_binario(request):
        return _auth_error()
    nome = request.match_info["nome"]
    if "/" in nome or "\\" in nome or not nome.lower().endswith(".jpg"):
        return web.json_response({"error": "Nome non valido"}, status=400)
    path = MINIATURE_VIDEO / nome
    if not path.is_file():
        return web.json_response({"error": "Miniatura non trovata"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


def _carica_stato_ricerca_video() -> dict:
    if STATO_RICERCA_VIDEO_PATH.exists():
        try:
            return json.loads(STATO_RICERCA_VIDEO_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Stato ricerca video corrotto, ricreo da zero")
    return {"termine": "", "ordine": [], "posizione": 0}


def _salva_stato_ricerca_video(stato: dict) -> None:
    STATO_RICERCA_VIDEO_PATH.write_text(json.dumps(stato))


async def handle_cerca_video(request: web.Request) -> web.Response:
    """Ricerca nei video, stesso identico principio di handle_cerca (foto,
    24/08/2026) — mese/anno/luogo, stato "usa e getta" separato, non tocca
    il cursore della galleria video principale."""
    if not _check_auth(request):
        return _auth_error()
    termine = (request.query.get("q") or "").strip()
    if not termine:
        return web.json_response({"error": "Parametro 'q' mancante"}, status=400)
    continua = request.query.get("continua") == "1"

    meta_video = _carica_meta_video()
    termine_norm = termine.lower()
    tipo, trovati = _filtra_per_termine(meta_video, termine_norm)

    disponibili = set(_lista_video())
    trovati &= disponibili
    totale_trovate = len(trovati)

    stato = _carica_stato_ricerca_video()
    if not (continua and stato.get("termine") == termine_norm and set(stato.get("ordine", [])) == trovati):
        ordine = list(trovati)
        random.shuffle(ordine)
        stato = {"termine": termine_norm, "ordine": ordine, "posizione": 0}

    fine = min(stato["posizione"] + BATCH_MASSIMO_VIDEO, len(stato["ordine"]))
    blocco_nomi = stato["ordine"][stato["posizione"]:fine]
    stato["posizione"] = fine
    _salva_stato_ricerca_video(stato)

    risultato = [{"nome": nome, "didascalia": _didascalia_video(nome, meta_video)} for nome in blocco_nomi]
    return web.json_response({
        "tipo": tipo,
        "termine": termine,
        "totale_trovate": totale_trovate,
        "video": risultato,
    })


async def handle_media_batch(request: web.Request) -> web.Response:
    """"Mostra foto e video" (Fase 2) — blocco misto, un video ogni
    RAPPORTO_VIDEO_MISTO elementi, il resto foto. Avanza ENTRAMBI i cursori
    persistenti (foto e video) usando le stesse funzioni già esistenti per
    ciascuno — non serve un terzo file di stato, solo un ordine di lettura
    diverso rispetto ai batch "solo foto"/"solo video"."""
    if not _check_auth(request):
        return _auth_error()
    try:
        n = min(int(request.query.get("n", BATCH_MASSIMO_MISTO)), BATCH_MASSIMO_MISTO)
    except ValueError:
        n = BATCH_MASSIMO_MISTO
    n = max(1, n)

    stato_foto = _stato_valido(_carica_stato())
    stato_video = _stato_valido_video(_carica_stato_video())
    if not stato_foto["ordine"] and not stato_video["ordine"]:
        return web.json_response({"error": "Nessun media disponibile"}, status=404)

    meta = _carica_meta()
    meta_video = _carica_meta_video()
    risultato = []
    for i in range(n):
        # Un video ogni RAPPORTO_VIDEO_MISTO posizioni, MA solo se ce ne
        # sono ancora — altrimenti niente foto "mancanti" nel blocco, si
        # riempie comunque con foto.
        vuole_video = (i + 1) % RAPPORTO_VIDEO_MISTO == 0
        if vuole_video and stato_video["ordine"]:
            nome = stato_video["ordine"][stato_video["posizione"]]
            risultato.append({
                "tipo": "video", "nome": nome, "didascalia": _didascalia_video(nome, meta_video),
                "preferita": bool(meta_video.get(nome, {}).get("preferita", False)),
            })
            nuova_pos = stato_video["posizione"] + 1
            if nuova_pos >= len(stato_video["ordine"]):
                stato_video = {"ordine": _rimescola_video(escludi_prima=nome), "posizione": 0}
            else:
                stato_video["posizione"] = nuova_pos
        elif stato_foto["ordine"]:
            nome = stato_foto["ordine"][stato_foto["posizione"]]
            risultato.append({
                "tipo": "foto", "nome": nome, "didascalia": _didascalia(nome, meta),
                "preferita": bool(meta.get(nome, {}).get("preferita", False)),
            })
            nuova_pos = stato_foto["posizione"] + 1
            if nuova_pos >= len(stato_foto["ordine"]):
                stato_foto = {"ordine": _rimescola(escludi_prima=nome), "posizione": 0}
            else:
                stato_foto["posizione"] = nuova_pos
        elif stato_video["ordine"]:
            # Niente foto in libreria ma ci sono video: riempie comunque.
            nome = stato_video["ordine"][stato_video["posizione"]]
            risultato.append({
                "tipo": "video", "nome": nome, "didascalia": _didascalia_video(nome, meta_video),
                "preferita": bool(meta_video.get(nome, {}).get("preferita", False)),
            })
            nuova_pos = stato_video["posizione"] + 1
            if nuova_pos >= len(stato_video["ordine"]):
                stato_video = {"ordine": _rimescola_video(escludi_prima=nome), "posizione": 0}
            else:
                stato_video["posizione"] = nuova_pos

    _salva_stato(stato_foto)
    _salva_stato_video(stato_video)
    return web.json_response({"media": risultato})


def _carica_stato_ricerca_misto() -> dict:
    if STATO_RICERCA_MISTO_PATH.exists():
        try:
            return json.loads(STATO_RICERCA_MISTO_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            logger.exception("Stato ricerca misto corrotto, ricreo da zero")
    return {"termine": "", "ordine": [], "posizione": 0}


def _salva_stato_ricerca_misto(stato: dict) -> None:
    STATO_RICERCA_MISTO_PATH.write_text(json.dumps(stato))


async def handle_cerca_misto(request: web.Request) -> web.Response:
    """Ricerca su foto E video insieme (24/08/2026) — stesso principio di
    handle_cerca, ma l'"ordine" salvato è una lista di {"tipo","nome"}
    invece di soli nomi (foto e video condividono lo stesso namespace di
    stringhe, serve il tipo per non confonderli in lettura)."""
    if not _check_auth(request):
        return _auth_error()
    termine = (request.query.get("q") or "").strip()
    if not termine:
        return web.json_response({"error": "Parametro 'q' mancante"}, status=400)
    continua = request.query.get("continua") == "1"

    meta = _carica_meta()
    meta_video = _carica_meta_video()
    termine_norm = termine.lower()
    tipo, trovate_foto = _filtra_per_termine(meta, termine_norm)
    _, trovati_video = _filtra_per_termine(meta_video, termine_norm)

    trovate_foto &= set(_lista_foto())
    trovati_video &= set(_lista_video())
    totale_trovate = len(trovate_foto) + len(trovati_video)

    combinato = sorted(
        [{"tipo": "foto", "nome": n} for n in trovate_foto] +
        [{"tipo": "video", "nome": n} for n in trovati_video],
        key=lambda el: (el["tipo"], el["nome"]),
    )

    stato = _carica_stato_ricerca_misto()
    ordine_salvato = stato.get("ordine", [])
    stesso_insieme = (
        sorted(ordine_salvato, key=lambda el: (el["tipo"], el["nome"])) == combinato
        if ordine_salvato else not combinato
    )
    if not (continua and stato.get("termine") == termine_norm and stesso_insieme):
        ordine = list(combinato)
        random.shuffle(ordine)
        stato = {"termine": termine_norm, "ordine": ordine, "posizione": 0}

    fine = min(stato["posizione"] + BATCH_MASSIMO_MISTO, len(stato["ordine"]))
    blocco = stato["ordine"][stato["posizione"]:fine]
    stato["posizione"] = fine
    _salva_stato_ricerca_misto(stato)

    risultato = [
        {
            "tipo": el["tipo"],
            "nome": el["nome"],
            "didascalia": _didascalia_video(el["nome"], meta_video) if el["tipo"] == "video"
            else _didascalia(el["nome"], meta),
        }
        for el in blocco
    ]
    return web.json_response({
        "tipo": tipo,
        "termine": termine,
        "totale_trovate": totale_trovate,
        "media": risultato,
    })


async def handle_accadde_oggi(request: web.Request) -> web.Response:
    """"Accadde oggi" per l'app (24/08/2026) — foto scattate lo stesso
    giorno+mese di oggi, in anni passati. Confronto locale su "giorno_mese"
    (già presente in meta.json per ogni foto), nessuna chiamata di rete.
    Ordinate per anno decrescente (più recente prima)."""
    if not _check_auth(request):
        return _auth_error()
    oggi = datetime.now(FUSO_ITALIA).strftime("%m-%d")
    meta = _carica_meta()
    disponibili = set(_lista_foto())
    trovate = []
    for nome, v in meta.items():
        # Solo foto ANCORA presenti nella galleria — una voce in meta.json
        # può restare orfana dopo un'eliminazione (non viene ripulita lì).
        if v.get("giorno_mese") == oggi and nome in disponibili:
            iso = v.get("data_iso", "")
            trovate.append({"nome": nome, "anno": iso.split("-")[0] if iso else ""})
    trovate.sort(key=lambda t: t["anno"], reverse=True)
    return web.json_response({"totale": len(trovate), "foto": trovate})


async def handle_media_accadde_oggi(request: web.Request) -> web.Response:
    """"Accadde oggi" su foto E video insieme (25/08/2026) — usata dalla
    skill per ANTEPORRE questi elementi all'apertura dello slideshow
    (foto/video/misto) quando l'opzione è attiva in Impostazioni (app),
    così le prime cose viste sono i ricordi di oggi negli anni passati.
    Diversa da /api/foto/accadde-oggi (solo foto, usata dall'app per la
    sua schermata dedicata) — qui serve anche il tipo per poter rientrare
    nello schema "misto" e i video hanno "giorno_mese" da tempo
    (estrai_giorno_video.py), solo mai stato esposto finora."""
    if not _check_auth(request):
        return _auth_error()
    oggi = datetime.now(FUSO_ITALIA).strftime("%m-%d")

    meta_foto = _carica_meta()
    disponibili_foto = set(_lista_foto())
    trovate = []
    for nome, v in meta_foto.items():
        if v.get("giorno_mese") == oggi and nome in disponibili_foto:
            iso = v.get("data_iso", "")
            trovate.append({
                "tipo": "foto", "nome": nome,
                "anno": iso.split("-")[0] if iso else "",
                "didascalia": _didascalia(nome, meta_foto),
                "preferita": bool(v.get("preferita", False)),
            })

    meta_video = _carica_meta_video()
    disponibili_video = set(_lista_video())
    for nome, v in meta_video.items():
        if v.get("giorno_mese") == oggi and nome in disponibili_video:
            iso = v.get("data_iso", "")
            trovate.append({
                "tipo": "video", "nome": nome,
                "anno": iso.split("-")[0] if iso else "",
                "didascalia": _didascalia_video(nome, meta_video),
                "preferita": bool(v.get("preferita", False)),
            })

    trovate.sort(key=lambda t: t["anno"], reverse=True)
    return web.json_response({"totale": len(trovate), "media": trovate})


async def handle_duplicati(request: web.Request) -> web.Response:
    """Ritorna l'ultimo risultato calcolato da rileva_duplicati.py (script
    offline, va rilanciato a mano — hashare 10.900+ foto ad ogni richiesta
    sarebbe troppo lento). Se non è mai stato lanciato, liste vuote invece
    di un errore. SOLO SEGNALAZIONE: l'app decide se e cosa eliminare,
    questo endpoint non cancella nulla."""
    if not _check_auth(request):
        return _auth_error()
    if not DUPLICATI_PATH.exists():
        return web.json_response({"generato": None, "gruppi_duplicati": [], "screenshot": []})
    try:
        dati = json.loads(DUPLICATI_PATH.read_text())
    except json.JSONDecodeError:
        return web.json_response({"generato": None, "gruppi_duplicati": [], "screenshot": []})
    # Filtra le foto nel frattempo già eliminate (lo script potrebbe essere
    # stato lanciato prima di eliminazioni successive).
    disponibili = set(_lista_foto())
    gruppi = [[n for n in g if n in disponibili] for g in dati.get("gruppi_duplicati", [])]
    dati["gruppi_duplicati"] = [g for g in gruppi if len(g) > 1]
    dati["screenshot"] = [n for n in dati.get("screenshot", []) if n in disponibili]
    return web.json_response(dati)


async def handle_lista(request: web.Request) -> web.Response:
    """Senza "limit" ritorna l'elenco completo di soli nomi, in ordine
    alfabetico (comportamento invariato, usato finora solo per il conteggio
    in Home). Con "limit" applica paginazione offset/limit — per la griglia
    miniature dell'app (24/08/2026): 10.900+ foto non hanno senso tutte
    insieme in una risposta sola — e ordina CRONOLOGICAMENTE (più recenti
    prima, foto senza data in fondo), con la data formattata e lo stato
    preferito per ogni foto, così l'app può raggruppare per giorno senza
    dover leggere meta.json a parte. Con "solo_preferiti=true" filtra
    prima di paginare — usato dalla griglia "❤️ Preferiti" dell'app."""
    if not _check_auth(request):
        return _auth_error()
    foto = _lista_foto()
    totale = len(foto)
    limit = request.query.get("limit")
    if limit is None:
        return web.json_response({"totale": totale, "foto": foto})
    try:
        offset = max(0, int(request.query.get("offset", 0)))
        limit = int(limit)
    except ValueError:
        return web.json_response({"error": "offset/limit non validi"}, status=400)
    meta = _carica_meta()
    if request.query.get("solo_preferiti") == "true":
        foto = [n for n in foto if meta.get(n, {}).get("preferita")]
    totale_filtrato = len(foto)
    foto_ordinate = sorted(foto, key=lambda n: _chiave_ordinamento_cronologico(n, meta), reverse=True)
    pagina = foto_ordinate[offset:offset + limit]
    return web.json_response({
        "totale": totale_filtrato,
        "foto": [
            {"nome": n, "data": _data_completa_it(n, meta), "preferita": bool(meta.get(n, {}).get("preferita", False))}
            for n in pagina
        ],
    })


async def handle_indice_mesi(request: web.Request) -> web.Response:
    """Indice leggero mese->posizione per la barretta di scorrimento veloce
    (scrubber) della griglia dell'app (24/08/2026) — evita di dover scaricare
    tutte le 10.900+ foto solo per sapere "a che offset inizia Marzo 2015".
    Stesso ordine cronologico di handle_lista in modalità paginata (più
    recenti prima), riusa "data" già presente in meta.json (mese+anno, non
    serve il giorno esatto qui). Poche decine/centinaia di voci in tutto —
    una per ogni mese in cui c'è almeno una foto — non 10.900."""
    if not _check_auth(request):
        return _auth_error()
    foto = _lista_foto()
    meta = _carica_meta()
    foto_ordinate = sorted(foto, key=lambda n: _chiave_ordinamento_cronologico(n, meta), reverse=True)
    indice = []
    bucket_corrente = None
    for i, nome in enumerate(foto_ordinate):
        bucket = meta.get(nome, {}).get("data") or "Data sconosciuta"
        if bucket != bucket_corrente:
            indice.append({"mese_anno": bucket, "offset": i})
            bucket_corrente = bucket
    return web.json_response({"mesi": indice})


async def handle_preferita_foto(request: web.Request) -> web.Response:
    """Imposta/toglie il preferito su una foto (24/08/2026) — chiamato sia
    dal tasto ❤️ sullo schermo Echo Show sia dall'app. Corpo JSON
    {"preferita": true/false}, esplicito (non un semplice toggle): sia lo
    schermo sia l'app conoscono già lo stato attuale (arriva nel batch/
    nella lista), quindi mandano direttamente il nuovo valore voluto —
    più robusto di un toggle cieco se due richieste arrivassero quasi
    insieme."""
    if not _check_auth(request):
        return _auth_error()
    nome = request.match_info["nome"]
    if "/" in nome or "\\" in nome or not nome.lower().endswith((".jpg", ".jpeg")):
        return web.json_response({"error": "Nome non valido"}, status=400)
    try:
        body = await request.json()
        preferita = bool(body.get("preferita"))
    except (ValueError, json.JSONDecodeError):
        return web.json_response({"error": "Corpo JSON non valido"}, status=400)
    meta = _carica_meta()
    if nome not in meta:
        return web.json_response({"error": "Foto non trovata"}, status=404)
    meta[nome]["preferita"] = preferita
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
    return web.json_response({"nome": nome, "preferita": preferita})


async def handle_elimina_foto(request: web.Request) -> web.Response:
    """Cancella SEMPRE le copie servite (ridimensionata + miniatura) e la
    voce in meta.json. Con "anche_originale=true" in query, cancella ANCHE
    l'originale in /opt/foto_slideshow/originali — irreversibile. Scelta di
    Ivan il 24/08/2026: inizialmente solo la versione sicura (originale mai
    toccato), poi richiesta un'opzione di cancellazione DEFINITIVA accanto a
    quella sicura (non al posto di essa) — l'app decide quale chiamare in
    base al pulsante premuto. Lo stato dello shuffle (stato_shuffle.json)
    non va aggiornato qui: _stato_valido() lo rileva già da solo al prossimo
    giro (confronta l'ordine salvato con _lista_foto() attuale, rimescola se
    non combaciano più) — stesso meccanismo già usato per i nuovi upload."""
    if not _check_auth(request):
        return _auth_error()
    nome = request.match_info["nome"]
    if "/" in nome or "\\" in nome or not nome.lower().endswith((".jpg", ".jpeg")):
        return web.json_response({"error": "Nome non valido"}, status=400)
    path = RIDIMENSIONATE / nome
    if not path.is_file():
        return web.json_response({"error": "Foto non trovata"}, status=404)
    path.unlink()
    miniatura = MINIATURE / nome
    if miniatura.is_file():
        miniatura.unlink()
    meta = _carica_meta()
    if nome in meta:
        del meta[nome]
        META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))

    originale_eliminato = False
    if request.query.get("anche_originale") == "true":
        stem = Path(nome).stem
        for p in ORIGINALI.iterdir():
            if p.is_file() and p.stem == stem:
                p.unlink()
                originale_eliminato = True
                break

    return web.json_response({"eliminata": nome, "originale_eliminato": originale_eliminato})


async def handle_lista_video(request: web.Request) -> web.Response:
    """Mirror di handle_lista (foto) — per l'app Galleria Alexa (24/08/2026).
    Senza "limit" ritorna l'elenco completo di soli nomi (comportamento
    invariato, usato per il conteggio in Home). Con "limit" pagina e ordina
    CRONOLOGICAMENTE (più recenti prima), con la data formattata per video —
    per la griglia miniature."""
    if not _check_auth(request):
        return _auth_error()
    video = _lista_video()
    totale = len(video)
    limit = request.query.get("limit")
    if limit is None:
        return web.json_response({"totale": totale, "video": video})
    try:
        offset = max(0, int(request.query.get("offset", 0)))
        limit = int(limit)
    except ValueError:
        return web.json_response({"error": "offset/limit non validi"}, status=400)
    meta = _carica_meta_video()
    if request.query.get("solo_preferiti") == "true":
        video = [n for n in video if meta.get(n, {}).get("preferita")]
    totale_filtrato = len(video)
    video_ordinati = sorted(video, key=lambda n: _chiave_ordinamento_cronologico_video(n, meta), reverse=True)
    pagina = video_ordinati[offset:offset + limit]
    return web.json_response({
        "totale": totale_filtrato,
        "video": [
            {"nome": n, "data": _data_completa_it_video(n, meta), "preferita": bool(meta.get(n, {}).get("preferita", False))}
            for n in pagina
        ],
    })


async def handle_indice_mesi_video(request: web.Request) -> web.Response:
    """Mirror di handle_indice_mesi (foto) — indice mese->posizione per lo
    scrubber della griglia video dell'app."""
    if not _check_auth(request):
        return _auth_error()
    video = _lista_video()
    meta = _carica_meta_video()
    video_ordinati = sorted(video, key=lambda n: _chiave_ordinamento_cronologico_video(n, meta), reverse=True)
    indice = []
    bucket_corrente = None
    for i, nome in enumerate(video_ordinati):
        bucket = meta.get(nome, {}).get("data") or "Data sconosciuta"
        if bucket != bucket_corrente:
            indice.append({"mese_anno": bucket, "offset": i})
            bucket_corrente = bucket
    return web.json_response({"mesi": indice})


async def handle_preferita_video(request: web.Request) -> web.Response:
    """Mirror di handle_preferita_foto per i video."""
    if not _check_auth(request):
        return _auth_error()
    nome = request.match_info["nome"]
    if "/" in nome or "\\" in nome or not nome.lower().endswith(".mp4"):
        return web.json_response({"error": "Nome non valido"}, status=400)
    try:
        body = await request.json()
        preferita = bool(body.get("preferita"))
    except (ValueError, json.JSONDecodeError):
        return web.json_response({"error": "Corpo JSON non valido"}, status=400)
    meta = _carica_meta_video()
    if nome not in meta:
        return web.json_response({"error": "Video non trovato"}, status=404)
    meta[nome]["preferita"] = preferita
    META_VIDEO_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
    return web.json_response({"nome": nome, "preferita": preferita})


async def handle_elimina_video(request: web.Request) -> web.Response:
    """Mirror di handle_elimina_foto — cancella sempre la copia servita +
    miniatura + voce meta_video.json; con "anche_originale=true" cancella
    anche l'originale in video_originali/ (irreversibile)."""
    if not _check_auth(request):
        return _auth_error()
    nome = request.match_info["nome"]
    if "/" in nome or "\\" in nome or not nome.lower().endswith(".mp4"):
        return web.json_response({"error": "Nome non valido"}, status=400)
    path = VIDEO_RIDIM / nome
    if not path.is_file():
        return web.json_response({"error": "Video non trovato"}, status=404)
    path.unlink()
    miniatura = MINIATURE_VIDEO / (Path(nome).stem + ".jpg")
    if miniatura.is_file():
        miniatura.unlink()
    meta = _carica_meta_video()
    if nome in meta:
        del meta[nome]
        META_VIDEO_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))

    originale_eliminato = False
    if request.query.get("anche_originale") == "true":
        stem = Path(nome).stem
        for p in VIDEO_ORIGINALI.iterdir():
            if p.is_file() and p.stem == stem:
                p.unlink()
                originale_eliminato = True
                break

    return web.json_response({"eliminato": nome, "originale_eliminato": originale_eliminato})


async def handle_binario(request: web.Request) -> web.Response:
    if not _check_auth_binario(request):
        return _auth_error()
    nome = request.match_info["nome"]
    # Solo nome file semplice, niente path traversal (../, sottocartelle) —
    # unico input utente di questo endpoint, va validato prima di aprirlo.
    if "/" in nome or "\\" in nome or not nome.lower().endswith((".jpg", ".jpeg")):
        return web.json_response({"error": "Nome non valido"}, status=400)
    path = RIDIMENSIONATE / nome
    if not path.is_file():
        return web.json_response({"error": "Foto non trovata"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def handle_miniatura(request: web.Request) -> web.Response:
    """Mirror di handle_binario ma serve da "miniature/" — per la griglia di
    sfoglio dell'app Galleria Alexa (24/08/2026), stessa auth (token in query,
    l'app può mandare l'header Authorization ma teniamo lo stesso schema
    dell'endpoint binario per coerenza)."""
    if not _check_auth_binario(request):
        return _auth_error()
    nome = request.match_info["nome"]
    if "/" in nome or "\\" in nome or not nome.lower().endswith((".jpg", ".jpeg")):
        return web.json_response({"error": "Nome non valido"}, status=400)
    path = MINIATURE / nome
    if not path.is_file():
        return web.json_response({"error": "Miniatura non trovata"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def handle_get_impostazioni(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return _auth_error()
    return web.json_response({
        "durata_secondi": _durata_corrente(),
        "accadde_oggi_schermo": _accadde_oggi_schermo_attivo(),
    })


async def handle_set_impostazioni(request: web.Request) -> web.Response:
    # Ripristinato il 24/08/2026 per l'app Galleria Alexa (Fase 1) — era
    # stato tolto come "codice morto" quando il pannello impostazioni è
    # sparito dallo schermo dell'Echo Show; ora il telefono ne prende il
    # posto come unico modo per cambiare la durata.
    # 25/08/2026: aggiornato per gestire anche "accadde_oggi_schermo" —
    # entrambi i campi sono ora opzionali e si aggiornano SOLO se presenti
    # nel corpo, leggendo prima il file esistente e scrivendo l'unione:
    # prima questa funzione sovrascriveva l'intero file con il solo
    # durata_secondi, il che avrebbe cancellato in silenzio il nuovo campo
    # ad ogni cambio durata dall'app.
    if not _check_auth(request):
        return _auth_error()
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "corpo non valido"}, status=400)

    attuali = _impostazioni_correnti()

    if "durata_secondi" in body:
        try:
            durata = int(body["durata_secondi"])
        except (ValueError, TypeError):
            return web.json_response({"error": "durata_secondi non valido"}, status=400)
        if durata not in DURATE_VALIDE_SEC:
            return web.json_response({"error": f"durata_secondi deve essere uno tra {sorted(DURATE_VALIDE_SEC)}"}, status=400)
        attuali["durata_secondi"] = durata

    if "accadde_oggi_schermo" in body:
        if not isinstance(body["accadde_oggi_schermo"], bool):
            return web.json_response({"error": "accadde_oggi_schermo deve essere un booleano"}, status=400)
        attuali["accadde_oggi_schermo"] = body["accadde_oggi_schermo"]

    IMPOSTAZIONI_PATH.write_text(json.dumps(attuali))
    return web.json_response({
        "durata_secondi": attuali.get("durata_secondi", DURATA_DEFAULT_SEC),
        "accadde_oggi_schermo": attuali.get("accadde_oggi_schermo", False),
    })


def _impostazioni_correnti() -> dict:
    if IMPOSTAZIONI_PATH.exists():
        try:
            return json.loads(IMPOSTAZIONI_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _durata_corrente() -> int:
    valore = _impostazioni_correnti().get("durata_secondi")
    return valore if valore in DURATE_VALIDE_SEC else DURATA_DEFAULT_SEC


def _accadde_oggi_schermo_attivo() -> bool:
    return bool(_impostazioni_correnti().get("accadde_oggi_schermo", False))


# --- Avvio in background dei processi di elaborazione dopo un upload
# (24/08/2026, app Galleria Alexa) — "fire and forget": la risposta HTTP
# torna subito all'app, ridimensiona.py/ridimensiona_video.py girano per
# conto loro (compressione + geocoding, alcuni secondi a foto). Guardia
# contro invocazioni sovrapposte: quegli script leggono e riscrivono
# meta.json per intero ad ogni foto salvata, senza lock — due istanze in
# corso insieme rischierebbero un "lost update" (la seconda sovrascrive
# meta.json con uno stato letto PRIMA che la prima avesse salvato le sue
# foto). Se un'istanza precedente è ancora viva non se ne avvia una
# seconda — lo script è comunque idempotente (salta le foto già fatte),
# quindi un upload arrivato mentre un'elaborazione è già in corso verrà
# ripreso al prossimo avvio, non perso.
_processo_resize_foto: subprocess.Popen | None = None
_processo_resize_video: subprocess.Popen | None = None


def _avvia_resize_foto() -> None:
    global _processo_resize_foto
    if _processo_resize_foto is not None and _processo_resize_foto.poll() is None:
        return
    _processo_resize_foto = subprocess.Popen(
        [sys.executable, str(_DIR_PROGETTO / "ridimensiona.py")],
        cwd=str(_DIR_PROGETTO),
    )


def _avvia_resize_video() -> None:
    global _processo_resize_video
    if _processo_resize_video is not None and _processo_resize_video.poll() is None:
        return
    _processo_resize_video = subprocess.Popen(
        [sys.executable, str(_DIR_PROGETTO / "ridimensiona_video.py")],
        cwd=str(_DIR_PROGETTO),
    )


async def _salva_multipart(
    request: web.Request, cartella: Path, estensioni_valide: set[str]
) -> tuple[str, int] | web.Response:
    """Legge un campo multipart "file" a blocchi (MAI tutto in RAM in un
    colpo solo — importante per i video) e lo salva in "cartella" con un
    nome sanificato (prefisso timestamp+UUID, evita collisioni silenziose
    tra due upload con lo stesso nome originale). Ritorna (nome_salvato,
    byte_scritti), oppure direttamente una web.Response di errore già
    pronta da propagare al chiamante."""
    try:
        reader = await request.multipart()
        field = await reader.next()
    except Exception:
        return web.json_response(
            {"error": "Corpo della richiesta non valido (atteso multipart/form-data)"}, status=400
        )

    if field is None or field.name != "file":
        return web.json_response({"error": "Campo 'file' mancante"}, status=400)

    estensione = Path(field.filename or "").suffix.lower()
    if estensione not in estensioni_valide:
        return web.json_response(
            {"error": f"Estensione non supportata: '{estensione}' (valide: {sorted(estensioni_valide)})"},
            status=400,
        )

    cartella.mkdir(parents=True, exist_ok=True)
    nome_salvato = f"{int(time.time())}_{uuid.uuid4().hex[:8]}{estensione}"
    path_dest = cartella / nome_salvato

    dimensione = 0
    with open(path_dest, "wb") as f:
        while True:
            chunk = await field.read_chunk(1024 * 1024)
            if not chunk:
                break
            dimensione += len(chunk)
            f.write(chunk)

    if dimensione == 0:
        path_dest.unlink(missing_ok=True)
        return web.json_response({"error": "File vuoto"}, status=400)

    return nome_salvato, dimensione


async def handle_foto_upload(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return _auth_error()
    risultato = await _salva_multipart(request, ORIGINALI, ESTENSIONI_UPLOAD_FOTO)
    if isinstance(risultato, web.Response):
        return risultato
    nome_salvato, dimensione = risultato
    _avvia_resize_foto()
    return web.json_response({"nome": nome_salvato, "dimensione_byte": dimensione})


async def handle_video_upload(request: web.Request) -> web.Response:
    if not _check_auth(request):
        return _auth_error()
    risultato = await _salva_multipart(request, VIDEO_ORIGINALI, ESTENSIONI_UPLOAD_VIDEO)
    if isinstance(risultato, web.Response):
        return risultato
    nome_salvato, dimensione = risultato
    _avvia_resize_video()
    return web.json_response({"nome": nome_salvato, "dimensione_byte": dimensione})


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/foto/attuale", handle_attuale)
    app.router.add_post("/api/foto/avanti", handle_avanti)
    app.router.add_post("/api/foto/indietro", handle_indietro)
    app.router.add_get("/api/foto/batch", handle_batch)
    app.router.add_get("/api/foto/cerca", handle_cerca)
    app.router.add_get("/api/foto/lista", handle_lista)
    app.router.add_get("/api/foto/accadde-oggi", handle_accadde_oggi)
    app.router.add_get("/api/foto/duplicati", handle_duplicati)
    app.router.add_get("/api/media/accadde-oggi", handle_media_accadde_oggi)
    app.router.add_get("/api/foto/indice-mesi", handle_indice_mesi)
    app.router.add_delete("/api/foto/{nome}", handle_elimina_foto)
    app.router.add_post("/api/foto/{nome}/preferita", handle_preferita_foto)
    app.router.add_get("/api/foto/binario/{nome}", handle_binario)
    app.router.add_get("/api/foto/miniatura/{nome}", handle_miniatura)
    app.router.add_post("/api/foto/upload", handle_foto_upload)
    app.router.add_get("/api/video/batch", handle_video_batch)
    app.router.add_get("/api/video/cerca", handle_cerca_video)
    app.router.add_get("/api/video/lista", handle_lista_video)
    app.router.add_get("/api/video/indice-mesi", handle_indice_mesi_video)
    app.router.add_get("/api/video/binario/{nome}", handle_video_binario)
    app.router.add_get("/api/video/miniatura/{nome}", handle_miniatura_video)
    app.router.add_delete("/api/video/{nome}", handle_elimina_video)
    app.router.add_post("/api/video/{nome}/preferita", handle_preferita_video)
    app.router.add_post("/api/video/upload", handle_video_upload)
    app.router.add_get("/api/media/batch", handle_media_batch)
    app.router.add_get("/api/media/cerca", handle_cerca_misto)
    app.router.add_get("/api/impostazioni", handle_get_impostazioni)
    app.router.add_post("/api/impostazioni", handle_set_impostazioni)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=API_PORT)
