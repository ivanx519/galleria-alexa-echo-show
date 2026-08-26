"""
Migrazione una tantum (24/08/2026): aggiunge la data esatta ("data_iso",
formato "YYYY-MM-DD") alle voci di meta.json già processate — serve per la
griglia miniature dell'app Galleria Alexa, che raggruppa le foto per giorno
in ordine cronologico invece che alfabetico per nome file. ridimensiona.py
normale scrive già questo campo per le foto NUOVE (vedi _data_iso_da_exif
lì), ma non ritocca le voci già complete (idempotenza esistente: salta se
"luogo" è già presente) — questo script backfilla quelle vecchie senza
rifare geocoding o resize. Stesso identico schema di estrai_giorno.py
(usato per "giorno_mese"), solo un campo diverso.

Rilegge l'EXIF dalle foto ORIGINALI (mai modificate, sempre su disco).
Idempotente: salta le voci che hanno già "data_iso" — si può rilanciare
in sicurezza in qualsiasi momento.
"""
import json
import sys
from pathlib import Path

from PIL import ExifTags, Image

ORIGINALI = Path("/opt/foto_slideshow/originali")
META_PATH = Path("/opt/foto_slideshow/meta.json")

_TAG_DATETIME_ORIGINAL = next(k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal")
_TAG_DATETIME = next(k for k, v in ExifTags.TAGS.items() if v == "DateTime")
_TAG_EXIF_IFD = 0x8769


def _data_iso_da_exif(path_originale: Path) -> str:
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


def main():
    meta = json.loads(META_PATH.read_text())

    # Indice nome-senza-estensione -> path originale, costruito una sola
    # volta (altrimenti una scansione della cartella per ognuna delle
    # ~10900 voci sarebbe quadratico e lentissimo).
    indice_originali = {}
    for p in ORIGINALI.iterdir():
        if p.is_file():
            indice_originali.setdefault(p.stem, p)

    aggiornate = 0
    senza_exif = 0
    originale_mancante = 0
    for i, (nome, v) in enumerate(meta.items(), 1):
        if "data_iso" in v:
            continue
        originale = indice_originali.get(Path(nome).stem)
        if not originale:
            originale_mancante += 1
            continue
        data_iso = _data_iso_da_exif(originale)
        v["data_iso"] = data_iso  # stringa vuota se manca l'EXIF, mai riprovato (è un dato locale, non di rete)
        aggiornate += 1
        if not data_iso:
            senza_exif += 1
        if aggiornate % 500 == 0:
            META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
            print(f"[{i}/{len(meta)}] aggiornate={aggiornate}", file=sys.stderr)

    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
    print(f"Completato: aggiornate={aggiornate}, senza EXIF={senza_exif}, "
          f"originale non trovato={originale_mancante}, totale meta={len(meta)}")


if __name__ == "__main__":
    main()
