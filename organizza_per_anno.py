"""
Organizza le foto e i video già ridimensionati in due cartelle sfogliabili
("Foto" e "Video"), ciascuna con una sottocartella per anno — solo per
comodità di navigazione manuale (es. da FX File Explorer), non toccato dalla
skill Alexa che continua a leggere direttamente da ridimensionate/ e
video_ridimensionati/ (flat, invariato).

Usa hardlink, non copie: stesso file su disco, zero spazio aggiuntivo. Va
rilanciato di tanto in tanto (idempotente, salta i link già esistenti) man
mano che l'elaborazione di foto/video procede.
"""
import json
from pathlib import Path

FOTO_RIDIM = Path("/opt/foto_slideshow/ridimensionate")
VIDEO_RIDIM = Path("/opt/foto_slideshow/video_ridimensionati")
META_FOTO = Path("/opt/foto_slideshow/meta.json")
META_VIDEO = Path("/opt/foto_slideshow/meta_video.json")

FOTO_PER_ANNO = Path("/opt/foto_slideshow_visibili/Foto")
VIDEO_PER_ANNO = Path("/opt/foto_slideshow_visibili/Video")

SENZA_DATA = "Senza data"


def _anno_da_data(data: str) -> str:
    if not data:
        return SENZA_DATA
    parti = data.split()
    return parti[-1] if parti and parti[-1].isdigit() else SENZA_DATA


def _organizza(cartella_sorgente: Path, meta_path: Path, cartella_dest: Path, etichetta: str):
    if not cartella_sorgente.exists():
        print(f"{etichetta}: cartella sorgente non trovata, salto")
        return 0, 0

    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            pass

    creati = 0
    saltati = 0
    rimossi = 0
    for f in cartella_sorgente.iterdir():
        if not f.is_file():
            continue
        anno = _anno_da_data(meta.get(f.name, {}).get("data", ""))
        cartella_anno = cartella_dest / anno
        cartella_anno.mkdir(parents=True, exist_ok=True)
        link = cartella_anno / f.name

        # Se la data di un file cambia DOPO che era già stato collegato
        # altrove (tipico caso: un video finito in "Senza data" perché il
        # geocoding/la data non erano ancora pronti, poi corretti in un
        # secondo momento) — ripulisce il vecchio collegamento invece di
        # lasciarlo duplicato in due cartelle anno diverse (bug trovato dal
        # vivo il 23/08/2026: 2 video comparivano sia in "Senza data" sia
        # nell'anno giusto).
        for altra_cartella in cartella_dest.iterdir():
            if altra_cartella == cartella_anno or not altra_cartella.is_dir():
                continue
            altro_link = altra_cartella / f.name
            if altro_link.exists():
                altro_link.unlink()
                rimossi += 1

        if link.exists():
            saltati += 1
            continue
        try:
            link.hardlink_to(f)
            creati += 1
        except FileExistsError:
            saltati += 1
    print(f"{etichetta}: {creati} nuovi collegamenti, {saltati} già presenti, {rimossi} obsoleti rimossi")
    return creati, saltati


def main():
    _organizza(FOTO_RIDIM, META_FOTO, FOTO_PER_ANNO, "Foto")
    _organizza(VIDEO_RIDIM, META_VIDEO, VIDEO_PER_ANNO, "Video")


if __name__ == "__main__":
    main()
