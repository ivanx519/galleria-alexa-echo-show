"""
Migrazione una tantum (24/08/2026): aggiunge il giorno esatto ("giorno_mese",
formato "MM-DD") alle voci di meta_video.json già processate — stesso
principio e stesso motivo di estrai_giorno.py per le foto ("Accadde oggi").
ridimensiona_video.py normale scrive già questo campo per i video NUOVI, ma
non ritocca le voci già presenti in meta.json — questo script backfilla
quelle vecchie senza ricomprimere nulla.

Rilegge creation_time dai video ORIGINALI via ffprobe (stessa fonte già
usata da ridimensiona_video.py), con lo stesso fallback sul nome file se il
tag manca. Idempotente: salta le voci che hanno già "giorno_mese".
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ORIGINALI = Path("/opt/foto_slideshow/video_originali")
META_PATH = Path("/opt/foto_slideshow/meta_video.json")

_PATTERN_DATA_NOME = re.compile(r"(20\d{2})(\d{2})(\d{2})")


def _giorno_mese_da_creation_time(path: Path) -> str:
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


def _giorno_mese_da_nome_file(nome: str) -> str:
    m = _PATTERN_DATA_NOME.search(nome)
    if not m:
        return ""
    anno, mese, giorno = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mese <= 12) or not (1 <= giorno <= 31) or not (2000 <= anno <= 2035):
        return ""
    return f"{m.group(2)}-{m.group(3)}"


def main():
    meta = json.loads(META_PATH.read_text())

    indice_originali = {}
    for p in ORIGINALI.iterdir():
        if p.is_file():
            indice_originali.setdefault(p.stem, p)

    aggiornate = 0
    senza_data = 0
    originale_mancante = 0
    for i, (nome, v) in enumerate(meta.items(), 1):
        if "giorno_mese" in v:
            continue
        originale = indice_originali.get(Path(nome).stem)
        if not originale:
            originale_mancante += 1
            continue
        giorno_mese = _giorno_mese_da_creation_time(originale) or _giorno_mese_da_nome_file(originale.name)
        v["giorno_mese"] = giorno_mese
        aggiornate += 1
        if not giorno_mese:
            senza_data += 1
        if aggiornate % 100 == 0:
            META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
            print(f"[{i}/{len(meta)}] aggiornate={aggiornate}", file=sys.stderr)

    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
    print(f"Completato: aggiornate={aggiornate}, senza data={senza_data}, "
          f"originale non trovato={originale_mancante}, totale meta={len(meta)}")


if __name__ == "__main__":
    main()
