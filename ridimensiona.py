"""
Ridimensiona le foto originali per lo schermo dell'Echo Show 15" (1280x800) e
estrae data + luogo scattato (per la didascalia nello slideshow, es. "Villaspeciosa
· Settembre 2019") prima di togliere i metadati EXIF dalla copia servita alla skill.
Genera anche una miniatura piccola ("miniature/", 24/08/2026) per la griglia di
sfoglio dell'app Galleria Alexa — troppo pesante mostrare in griglia le stesse
foto da 1600px/~350KB pensate per lo schermo dell'Echo Show.
Idempotente: salta il resize dei file già presenti in "ridimensionate/" — si
può rilanciare dopo ogni nuovo giro di rsync senza rifare le foto già fatte.
Le foto già ridimensionate PRIMA dell'introduzione delle miniature vengono
comunque riprese al primo rilancio, solo per generare la miniatura mancante
(non serve riaprire l'originale, si parte dalla ridimensionata già pronta).
L'estrazione di data/luogo invece gira SOLO sulle foto non ancora presenti in
meta.json (il luogo richiede una chiamata di rete a testa, non ha senso
rifarla ad ogni rilancio dello script per foto già processate).

Opzione "--salta-geocoding": estrae solo la data, salta la chiamata a
Nominatim (1 richiesta/secondo — su migliaia di foto sono ore). Le foto
processate così NON hanno la chiave "luogo" in meta.json, quindi un rilancio
successivo SENZA questa opzione le riprende automaticamente per completare
il geocoding — nessun doppio lavoro, nessuna foto persa.
"""
import json
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

ORIGINALI = Path("/opt/foto_slideshow/originali")
RIDIMENSIONATE = Path("/opt/foto_slideshow/ridimensionate")
MINIATURE = Path("/opt/foto_slideshow/miniature")
META_PATH = Path("/opt/foto_slideshow/meta.json")

# Poco oltre la risoluzione reale dello schermo (1280x800): lascia un po' di
# margine per zoom/pan senza appesantire troppo rispetto al bisogno reale.
MAX_LATO = 1600
QUALITA_JPEG = 85

# Miniatura per la griglia dell'app — solo per far vedere in colpo d'occhio
# di che foto si tratta, non serve nitidezza: dimensione e qualità basse
# tenute deliberatamente piccole (poche decine di KB contro i ~350KB medi
# della ridimensionata).
MAX_LATO_MINIATURA = 220
QUALITA_JPEG_MINIATURA = 75

ESTENSIONI_VALIDE = {".jpg", ".jpeg", ".png", ".heic", ".webp"}

_MESI_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
}
_TAG_DATETIME_ORIGINAL = next(k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal")
_TAG_DATETIME = next(k for k, v in ExifTags.TAGS.items() if v == "DateTime")
_TAG_EXIF_IFD = 0x8769  # puntatore al sotto-IFD "Exif" dove vive DateTimeOriginal
_TAG_GPS_IFD = 0x8825   # puntatore al sotto-IFD "GPS"

# Nominatim (OpenStreetMap) — geocoding inverso gratuito, nessuna chiave API,
# ma richiede un User-Agent descrittivo e max 1 richiesta/secondo (politica
# d'uso pubblica). Va bene qui: è un giro UNA TANTUM per foto (mai ripetuto,
# vedi cache in meta.json), non una chiamata a ogni apertura della skill.
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_USER_AGENT = "foto-slideshow-alexa/1.0 (personal use, replace with your own contact info)"
_NOMINATIM_RATE_LIMIT_SEC = 1.1


def _nome_output(nome_originale: str) -> str:
    # Sempre .jpg in output, indipendentemente dal formato sorgente — un solo
    # formato da servire semplifica sia il resize sia l'app/skill lato client.
    return Path(nome_originale).stem + ".jpg"


def _dms_a_decimale(dms, ref):
    gradi, minuti, secondi = dms
    decimale = float(gradi) + float(minuti) / 60 + float(secondi) / 3600
    if ref in ("S", "W"):
        decimale = -decimale
    return decimale


def _coordinate_da_exif(path_originale: Path):
    """(lat, lon) in gradi decimali, o None se la foto non ha dati GPS."""
    try:
        with Image.open(path_originale) as img:
            exif = img.getexif()
            gps = exif.get_ifd(_TAG_GPS_IFD)
            if not gps:
                return None
            lat = gps.get(2)  # GPSLatitude
            lat_ref = gps.get(1)  # GPSLatitudeRef
            lon = gps.get(4)  # GPSLongitude
            lon_ref = gps.get(3)  # GPSLongitudeRef
            if not (lat and lat_ref and lon and lon_ref):
                return None
            return _dms_a_decimale(lat, lat_ref), _dms_a_decimale(lon, lon_ref)
    except Exception:
        return None


def _luogo_da_coordinate(lat, lon):
    """Nome di città/paese da coordinate GPS via Nominatim — stringa vuota se
    fallisce (rete assente, coordinate in mezzo al nulla, ecc.), mai
    un'eccezione che blocchi l'intero giro di elaborazione."""
    try:
        qs = urllib.parse.urlencode({
            "lat": lat, "lon": lon, "format": "jsonv2",
            "zoom": 10,  # livello città/paese, non indirizzo esatto
            "accept-language": "it",
        })
        req = urllib.request.Request(
            f"{_NOMINATIM_URL}?{qs}",
            headers={"User-Agent": _NOMINATIM_USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            dati = json.loads(resp.read().decode("utf-8"))
        indirizzo = dati.get("address", {})
        return (
            indirizzo.get("city") or indirizzo.get("town") or indirizzo.get("village")
            or indirizzo.get("municipality") or indirizzo.get("county")
            or indirizzo.get("country") or ""
        )
    except Exception as e:
        print(f"  [GEOCODING FALLITO] {lat},{lon}: {e}", file=sys.stderr)
        return ""


def _giorno_mese_da_exif(path_originale: Path) -> str:
    """"MM-DD" dalla data EXIF originale, indipendente dall'anno — serve per
    "Accadde oggi" (24/08/2026): confrontare solo giorno+mese con la data di
    oggi, foto di qualsiasi anno passato. Stessa fonte EXIF di _data_da_exif,
    stringa vuota se manca."""
    try:
        with Image.open(path_originale) as img:
            exif = img.getexif()
            sub = exif.get_ifd(_TAG_EXIF_IFD)
            valore = sub.get(_TAG_DATETIME_ORIGINAL) or exif.get(_TAG_DATETIME)
            if not valore:
                return ""
            _, mese, giorno = valore.split(" ")[0].split(":")
            return f"{mese}-{giorno}"
    except Exception:
        return ""


def _data_iso_da_exif(path_originale: Path) -> str:
    """"2019-09-17" (giorno esatto, formato ISO — ordina già correttamente
    come stringa) dalla data EXIF originale — per l'app Galleria Alexa
    (24/08/2026), che raggruppa le miniature per giorno invece che mostrarle
    in ordine alfabetico per nome file. Stessa fonte EXIF di _data_da_exif/
    _giorno_mese_da_exif, stringa vuota se manca."""
    try:
        with Image.open(path_originale) as img:
            exif = img.getexif()
            sub = exif.get_ifd(_TAG_EXIF_IFD)
            valore = sub.get(_TAG_DATETIME_ORIGINAL) or exif.get(_TAG_DATETIME)
            if not valore:
                return ""
            anno, mese, giorno = valore.split(" ")[0].split(":")
            return f"{anno}-{mese}-{giorno}"
    except Exception:
        return ""


def _data_da_exif(path_originale: Path) -> str:
    """"Giugno 2020" dalla data EXIF originale — solo mese e anno. Stringa
    vuota se manca l'EXIF. DateTimeOriginal vive nel sotto-IFD "Exif"
    (0x8769), non nell'IFD0 principale — get_ifd() serve a raggiungerlo,
    exif.get() da solo non lo trova mai (verificato: dava 0/103 prima di
    questo fix)."""
    try:
        with Image.open(path_originale) as img:
            exif = img.getexif()
            sub = exif.get_ifd(_TAG_EXIF_IFD)
            valore = sub.get(_TAG_DATETIME_ORIGINAL) or exif.get(_TAG_DATETIME)
            if not valore:
                return ""
            # Formato EXIF: "2020:06:16 18:50:35"
            anno, mese = valore.split(" ")[0].split(":")[:2]
            return f"{_MESI_IT[int(mese)]} {anno}"
    except Exception:
        return ""


def _didascalia_e_meta(path_originale: Path, salta_geocoding: bool = False) -> dict:
    data = _data_da_exif(path_originale)
    # "giorno_mese" ("MM-DD"): a differenza di "luogo" può essere calcolato
    # una volta sola e per sempre (è un semplice campo EXIF locale, non una
    # chiamata di rete che può fallire transitoriamente) — va bene scriverlo
    # anche come stringa vuota quando manca, non serve mai riprovarlo.
    giorno_mese = _giorno_mese_da_exif(path_originale)
    data_iso = _data_iso_da_exif(path_originale)

    if salta_geocoding:
        # NIENTE chiave "luogo" qui (nemmeno vuota): il controllo di idempotenza
        # in main() è "'luogo' not in meta[nome]" — se scrivessimo "luogo": ""
        # un rilancio futuro SENZA --salta-geocoding penserebbe che questa foto
        # è già stata processata (magari senza GPS) e non la riprenderebbe mai
        # più. Ometterla la lascia "in sospeso" fino al prossimo giro completo.
        return {"didascalia": data, "data": data, "giorno_mese": giorno_mese, "data_iso": data_iso}

    coordinate = _coordinate_da_exif(path_originale)
    luogo = ""
    if coordinate:
        luogo = _luogo_da_coordinate(*coordinate)
        time.sleep(_NOMINATIM_RATE_LIMIT_SEC)  # politica d'uso Nominatim: max 1 req/sec

    if luogo and data:
        didascalia = f"{luogo} · {data}"
    else:
        didascalia = luogo or data
    return {"didascalia": didascalia, "luogo": luogo, "data": data, "giorno_mese": giorno_mese, "data_iso": data_iso}


def ridimensiona_una(path_originale: Path) -> bool:
    """Ritorna True se ha effettivamente processato qualcosa (ridimensionata
    e/o miniatura), False se entrambe già presenti o file non valido."""
    nome_out = _nome_output(path_originale.name)
    path_out = RIDIMENSIONATE / nome_out
    path_miniatura = MINIATURE / nome_out
    if path_out.exists() and path_miniatura.exists():
        return False

    try:
        if path_out.exists():
            # Ridimensionata già presente (foto processate prima
            # dell'introduzione delle miniature) — genera solo la miniatura
            # partendo da lei (già orientata, senza EXIF), invece di riaprire
            # l'originale: molto più veloce per il backfill una tantum delle
            # foto già esistenti.
            with Image.open(path_out) as img:
                img.thumbnail((MAX_LATO_MINIATURA, MAX_LATO_MINIATURA), Image.LANCZOS)
                img.save(path_miniatura, "JPEG", quality=QUALITA_JPEG_MINIATURA, optimize=True)
            return True

        with Image.open(path_originale) as img:
            # exif_transpose corregge la rotazione (molte foto da smartphone
            # hanno un tag EXIF di orientamento invece di essere già ruotate
            # nei pixel) — senza questo passaggio molte foto arriverebbero
            # storte sullo schermo.
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((MAX_LATO, MAX_LATO), Image.LANCZOS)
            # exif=b"" rimuove i metadati (GPS/dati dispositivo) dalla copia
            # servita alla skill — le foto originali su /originali/ restano intatte.
            # Data/luogo vanno estratti PRIMA (vedi main()), qui sono già persi.
            img.save(path_out, "JPEG", quality=QUALITA_JPEG, optimize=True, exif=b"")
            # Miniatura dalla stessa immagine già ridimensionata/orientata in
            # memoria — un secondo giro di thumbnail() più piccolo, zero
            # riletture da disco. thumbnail() muta l'immagine in place.
            img.thumbnail((MAX_LATO_MINIATURA, MAX_LATO_MINIATURA), Image.LANCZOS)
            img.save(path_miniatura, "JPEG", quality=QUALITA_JPEG_MINIATURA, optimize=True)
        return True
    except Exception as e:
        print(f"  [ERRORE] {path_originale.name}: {e}", file=sys.stderr)
        return False


def main():
    salta_geocoding = "--salta-geocoding" in sys.argv

    RIDIMENSIONATE.mkdir(parents=True, exist_ok=True)
    MINIATURE.mkdir(parents=True, exist_ok=True)
    originali = sorted(
        p for p in ORIGINALI.iterdir()
        if p.is_file() and p.suffix.lower() in ESTENSIONI_VALIDE
    )
    print(f"Trovate {len(originali)} foto in {ORIGINALI}")
    if salta_geocoding:
        print("--salta-geocoding attivo: solo data, nessuna chiamata a Nominatim "
              "(rilancia SENZA questa opzione più tardi per completare i luoghi)")

    meta = {}
    if META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text())
        except json.JSONDecodeError:
            pass

    processate = 0
    saltate = 0
    geocodificate = 0
    for p in originali:
        nome_out = _nome_output(p.name)
        if ridimensiona_una(p):
            processate += 1
        else:
            saltate += 1
        # Rielabora SOLO le foto non ancora in meta.json (o quelle salvate
        # con lo schema vecchio, senza "luogo") — il geocoding costa una
        # chiamata di rete + 1.1s a foto, non va ripetuto inutilmente.
        if nome_out not in meta or "luogo" not in meta[nome_out]:
            meta[nome_out] = _didascalia_e_meta(p, salta_geocoding=salta_geocoding)
            if meta[nome_out].get("luogo"):
                geocodificate += 1
            META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))  # salva incrementale

    dim_orig = sum(p.stat().st_size for p in originali) / 1024 / 1024
    dim_ridim = sum(p.stat().st_size for p in RIDIMENSIONATE.iterdir() if p.is_file()) / 1024 / 1024
    dim_mini = sum(p.stat().st_size for p in MINIATURE.iterdir() if p.is_file()) / 1024 / 1024
    con_data = sum(1 for v in meta.values() if v.get("data"))
    con_luogo = sum(1 for v in meta.values() if v.get("luogo"))
    print(f"Elaborate: {processate}, saltate (già fatte): {saltate}")
    print(f"Geocodificate in questo giro: {geocodificate}")
    print(f"Con data: {con_data}/{len(meta)} — con luogo: {con_luogo}/{len(meta)}")
    print(f"Dimensione totale: {dim_orig:.1f} MB -> ridimensionate {dim_ridim:.1f} MB -> miniature {dim_mini:.1f} MB")


if __name__ == "__main__":
    main()
