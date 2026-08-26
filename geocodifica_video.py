"""
Estrae luogo (geocoding via Nominatim, stesso principio di ridimensiona.py
per le foto) per i video già compressi — mai fatto finora, meta_video.json
ha solo il campo "data". Legge il tag GPS "location" (formato ISO 6709,
es. "+39.2421+009.2022/") dai video ORIGINALI (non da quelli già
compressi/ricompressi, per sicurezza — l'originale è la fonte più
affidabile), poi geocodifica una volta sola e salva in meta_video.json.

Idempotente: salta i video che hanno già la chiave "luogo" in
meta_video.json (anche se vuota — a differenza di ridimensiona.py qui non
serve la distinzione "in sospeso vs niente GPS", è un giro unico non
richiamato di continuo). Rispetta il limite di 1 richiesta/secondo di
Nominatim.
"""
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

VIDEO_ORIGINALI = Path("/opt/foto_slideshow/video_originali")
META_VIDEO_PATH = Path("/opt/foto_slideshow/meta_video.json")

_MESI_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
}

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_USER_AGENT = "foto-slideshow-alexa/1.0 (personal use, replace with your own contact info)"
_NOMINATIM_RATE_LIMIT_SEC = 1.1

# ISO 6709: "+39.2421+009.2022/" (lat, lon, "/" finale opzionale, altitudine
# opzionale dopo la longitudine — presa solo la parte lat/lon).
_PATTERN_ISO6709 = re.compile(r"^([+-]\d+\.?\d*)([+-]\d+\.?\d*)")


def _coordinate_da_video(path: Path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format_tags=location",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        valore = r.stdout.strip()
        if not valore:
            return None
        m = _PATTERN_ISO6709.match(valore)
        if not m:
            return None
        return float(m.group(1)), float(m.group(2))
    except Exception:
        return None


def _luogo_da_coordinate(lat, lon):
    try:
        qs = urllib.parse.urlencode({
            "lat": lat, "lon": lon, "format": "jsonv2",
            "zoom": 10, "accept-language": "it",
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


def main():
    meta = {}
    if META_VIDEO_PATH.exists():
        try:
            meta = json.loads(META_VIDEO_PATH.read_text())
        except json.JSONDecodeError:
            pass

    originali = sorted(p for p in VIDEO_ORIGINALI.iterdir() if p.is_file())
    da_fare = [p for p in originali if "luogo" not in meta.get(p.name, {})]
    print(f"Video totali: {len(originali)}, da geocodificare: {len(da_fare)}")

    trovati = 0
    for i, path in enumerate(da_fare, 1):
        nome = path.name
        if nome not in meta:
            meta[nome] = {}
        coordinate = _coordinate_da_video(path)
        luogo = ""
        if coordinate:
            luogo = _luogo_da_coordinate(*coordinate)
            if luogo:
                trovati += 1
            time.sleep(_NOMINATIM_RATE_LIMIT_SEC)
        meta[nome]["luogo"] = luogo

        if i % 25 == 0 or i == len(da_fare):
            META_VIDEO_PATH.write_text(json.dumps(meta))  # salva progressivo
            print(f"[{i}/{len(da_fare)}] trovati={trovati}")

    META_VIDEO_PATH.write_text(json.dumps(meta))
    print(f"Completato: {trovati}/{len(da_fare)} con un luogo trovato")


if __name__ == "__main__":
    main()
