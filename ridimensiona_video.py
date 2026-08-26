"""
Comprime i video originali per uno storage più leggero — stesso principio di
ridimensiona.py per le foto, ma per i video: risoluzione 720p, H.264 CRF 23,
audio AAC 128k. Estrae la data (da "creation_time" nel contenitore, con
fallback al nome file) e la salva in meta_video.json, stesso schema di
meta.json per le foto.

Idempotente: salta i video già presenti in "video_ridimensionati/" — si può
rilanciare in qualsiasi momento senza rifare quelli già fatti. Salta anche i
file originali vuoti/corrotti (0 byte), loggando quanti sono senza bloccarsi.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ORIGINALI = Path("/opt/foto_slideshow/video_originali")
RIDIMENSIONATI = Path("/opt/foto_slideshow/video_ridimensionati")
MINIATURE = Path("/opt/foto_slideshow/miniature_video")
META_PATH = Path("/opt/foto_slideshow/meta_video.json")

ESTENSIONI_VALIDE = {".mp4", ".mov"}

# Miniatura per la griglia dell'app Galleria Alexa (24/08/2026) — un
# fotogramma estratto a mezzo secondo (evita il primo frame spesso nero/nei
# titoli), scalato come le miniature foto per coerenza visiva in griglia.
MINIATURA_LARGHEZZA = 220
MINIATURA_SECONDO = "00:00:00.5"

# 720p, CRF 23 (qualità buona, non massima — preset "fast" per tempi di
# elaborazione più ragionevoli su ~4.5 ore di contenuto totale, scelto senza
# attendere ulteriore conferma di Ivan su "veloce vs qualità migliore").
SCALA_ALTEZZA = 720
CRF = 23
PRESET = "fast"

_MESI_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
}

# Pattern comuni nei nomi file delle fotocamere/telefoni per il fallback data
# quando il contenitore video non ha "creation_time" (es. alcuni DJI/GoPro).
_PATTERN_DATA_NOME = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def _nome_output(nome_originale: str) -> str:
    return Path(nome_originale).stem + ".mp4"


def _data_da_creation_time(path: Path) -> str:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags=creation_time",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        valore = r.stdout.strip()
        if valore:
            anno, mese = int(valore[:4]), int(valore[5:7])
            # Alcune app che ri-salvano/ricomprimono un video scrivono un
            # creation_time placeholder ("1970-01-01") invece di lasciarlo
            # vuoto — un valore comunque "presente" quindi il fallback sul
            # nome file non scattava mai (bug scoperto il 23/08/2026: 3 video
            # con nome "VID_20230801_..." salvati con data "Gennaio 1970").
            # Stesso range di anni plausibili già usato in _data_da_nome_file.
            if 1 <= mese <= 12 and 2000 <= anno <= 2035:
                return f"{_MESI_IT[mese]} {anno}"
            return ""
    except Exception:
        pass
    return ""


def _giorno_mese_da_creation_time(path: Path) -> str:
    """"MM-DD" da creation_time, indipendente dall'anno — per "Accadde oggi"
    (24/08/2026), stessa fonte/stesso placeholder-guard di _data_da_creation_time."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags=creation_time",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        valore = r.stdout.strip()
        if valore and len(valore) >= 10:
            anno, mese, giorno = int(valore[:4]), int(valore[5:7]), int(valore[8:10])
            if 1 <= mese <= 12 and 1 <= giorno <= 31 and 2000 <= anno <= 2035:
                return f"{valore[5:7]}-{valore[8:10]}"
    except Exception:
        pass
    return ""


def _data_iso_da_creation_time(path: Path) -> str:
    """"2019-09-17" (giorno esatto) da creation_time — per la griglia
    miniature dell'app Galleria Alexa (24/08/2026), stesso principio di
    _data_iso_da_exif per le foto. Stesso placeholder-guard delle altre
    funzioni data qui sopra."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags=creation_time",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        valore = r.stdout.strip()
        if valore and len(valore) >= 10:
            anno, mese, giorno = int(valore[:4]), int(valore[5:7]), int(valore[8:10])
            if 1 <= mese <= 12 and 1 <= giorno <= 31 and 2000 <= anno <= 2035:
                return f"{valore[:4]}-{valore[5:7]}-{valore[8:10]}"
    except Exception:
        pass
    return ""


def _data_iso_da_nome_file(nome: str) -> str:
    m = _PATTERN_DATA_NOME.search(nome)
    if not m:
        return ""
    anno, mese, giorno = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mese <= 12) or not (1 <= giorno <= 31) or not (2000 <= anno <= 2035):
        return ""
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _data_iso_video(path: Path) -> str:
    return _data_iso_da_creation_time(path) or _data_iso_da_nome_file(path.name)


def _genera_miniatura(path_video: Path, path_miniatura: Path) -> bool:
    """Estrae un fotogramma a mezzo secondo (evita frame neri/titoli
    all'inizio) e lo scala a MINIATURA_LARGHEZZA px — stessa idea delle
    miniature foto, ma via ffmpeg invece di Pillow (i video ridimensionati
    non sono immagini apribili con PIL)."""
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(path_video),
                "-ss", MINIATURA_SECONDO, "-vframes", "1",
                "-vf", f"scale={MINIATURA_LARGHEZZA}:-2",
                str(path_miniatura),
            ],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0 and path_miniatura.exists()
    except Exception as e:
        print(f"  [ERRORE MINIATURA] {path_video.name}: {e}", file=sys.stderr)
        return False


def _data_da_nome_file(nome: str) -> str:
    m = _PATTERN_DATA_NOME.search(nome)
    if not m:
        return ""
    anno, mese = int(m.group(1)), int(m.group(2))
    if not (1 <= mese <= 12) or not (2000 <= anno <= 2035):
        return ""
    return f"{_MESI_IT[mese]} {anno}"


def _giorno_mese_da_nome_file(nome: str) -> str:
    m = _PATTERN_DATA_NOME.search(nome)
    if not m:
        return ""
    anno, mese, giorno = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mese <= 12) or not (1 <= giorno <= 31) or not (2000 <= anno <= 2035):
        return ""
    return f"{m.group(2)}-{m.group(3)}"


def _data_video(path: Path) -> str:
    return _data_da_creation_time(path) or _data_da_nome_file(path.name)


def _giorno_mese_video(path: Path) -> str:
    return _giorno_mese_da_creation_time(path) or _giorno_mese_da_nome_file(path.name)


def comprimi_uno(path_originale: Path) -> str:
    """Ritorna "fatto", "saltato" (già esiste, miniatura già presente),
    "solo_miniatura" (già compresso, generata solo la miniatura mancante —
    backfill dei video compressi prima dell'introduzione delle miniature) o
    "errore"."""
    nome_out = _nome_output(path_originale.name)
    path_out = RIDIMENSIONATI / nome_out
    path_miniatura = MINIATURE / (Path(nome_out).stem + ".jpg")

    if path_out.exists():
        if path_miniatura.exists():
            return "saltato"
        _genera_miniatura(path_out, path_miniatura)
        return "solo_miniatura"

    if path_originale.stat().st_size == 0:
        return "vuoto"

    cmd = [
        "ffmpeg", "-y", "-i", str(path_originale),
        "-vf", f"scale=-2:{SCALA_ALTEZZA}",
        "-c:v", "libx264", "-crf", str(CRF), "-preset", PRESET,
        "-c:a", "aac", "-b:a", "128k",
        # Senza questo, ffmpeg scrive l'indice del file (moov atom) IN FONDO
        # al video invece che in testa — un player che scarica/streamma
        # progressivamente (come il componente Video di APL su Echo Show)
        # riesce a leggere solo i primissimi byte e si blocca in attesa
        # dell'indice, che arriva solo a fine download: il video "si
        # frizza" dopo una frazione di secondo mentre l'audio continua
        # (bug segnalato dal vivo il 23/08/2026, confermato ispezionando
        # gli atomi del file: subito dopo "ftyp" si andava dritti a "mdat").
        "-movflags", "+faststart",
        str(path_out),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not path_out.exists():
            print(f"  [ERRORE] {path_originale.name}: {r.stderr[-300:]}", file=sys.stderr)
            path_out.unlink(missing_ok=True)
            return "errore"
        _genera_miniatura(path_out, path_miniatura)
        return "fatto"
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] {path_originale.name}", file=sys.stderr)
        path_out.unlink(missing_ok=True)
        return "errore"
    except Exception as e:
        print(f"  [ERRORE] {path_originale.name}: {e}", file=sys.stderr)
        return "errore"


def main():
    RIDIMENSIONATI.mkdir(parents=True, exist_ok=True)
    MINIATURE.mkdir(parents=True, exist_ok=True)
    originali = sorted(
        p for p in ORIGINALI.iterdir()
        if p.is_file() and p.suffix.lower() in ESTENSIONI_VALIDE
    )
    print(f"Trovati {len(originali)} video in {ORIGINALI}")

    meta = {}
    if META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text())
        except json.JSONDecodeError:
            pass

    contatori = {"fatto": 0, "solo_miniatura": 0, "saltato": 0, "errore": 0, "vuoto": 0}
    for i, p in enumerate(originali, 1):
        esito = comprimi_uno(p)
        contatori[esito] += 1
        nome_out = _nome_output(p.name)
        if esito == "fatto" and nome_out not in meta:
            meta[nome_out] = {
                "data": _data_video(p), "giorno_mese": _giorno_mese_video(p),
                "data_iso": _data_iso_video(p),
            }
            META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
        if i % 20 == 0:
            print(f"[{i}/{len(originali)}] fatto={contatori['fatto']} solo_miniatura={contatori['solo_miniatura']} "
                  f"saltato={contatori['saltato']} errore={contatori['errore']} vuoto={contatori['vuoto']}")

    dim_orig = sum(p.stat().st_size for p in originali) / 1024 / 1024 / 1024
    dim_ridim = sum(p.stat().st_size for p in RIDIMENSIONATI.iterdir() if p.is_file()) / 1024 / 1024 / 1024
    print(f"\nCompletato: {contatori}")
    print(f"Dimensione: {dim_orig:.2f} GB -> {dim_ridim:.2f} GB")


if __name__ == "__main__":
    main()
