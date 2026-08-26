"""
Migrazione una tantum (23/08/2026): rimuxa tutti i video già compressi per
spostare il "moov atom" (indice del file) in testa invece che in fondo —
bug trovato dal vivo, vedi commento in ridimensiona_video.py sul comando
ffmpeg. Copia solo i flussi ("-c copy"), niente ricompressione: nessuna
perdita di qualità, velocissimo (verificato ~0,5s/video su un campione).

ATTENZIONE hardlink: i file di video_ridimensionati/ sono agganciati anche
in /opt/foto_slideshow_visibili/Video/<anno>/ (vedi organizza_per_anno.py).
Sostituire il file qui SPEZZA quei collegamenti (restano agganciati alla
vecchia versione, col bug) — questo script non li tocca: dopo averlo fatto
girare, rilanciare organizza_per_anno.py DOPO aver svuotato la cartella
Video/ per ricostruire tutti i collegamenti da zero.
"""
import subprocess
import sys
from pathlib import Path

VIDEO_RIDIM = Path("/opt/foto_slideshow/video_ridimensionati")


def main():
    video = sorted(p for p in VIDEO_RIDIM.iterdir() if p.suffix.lower() == ".mp4")
    print(f"Trovati {len(video)} video da correggere")

    fatti = 0
    errori = 0
    for i, path in enumerate(video, 1):
        # DEVE finire in ".mp4" (non ".mp4.tmp"): ffmpeg indovina il
        # formato del muxer dall'estensione del file di output, e ".tmp"
        # non è un formato riconosciuto (bug preso al primo tentativo,
        # 584/584 falliti tutti con "Invalid argument" — nessun file
        # originale toccato, il fallimento avveniva prima della sostituzione).
        tmp = path.with_name(path.stem + ".tmp.mp4")
        cmd = ["ffmpeg", "-y", "-i", str(path), "-c", "copy", "-movflags", "+faststart",
               "-loglevel", "error", str(tmp)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0 or not tmp.exists():
                print(f"  [ERRORE] {path.name}: {r.stderr[-300:]}", file=sys.stderr)
                tmp.unlink(missing_ok=True)
                errori += 1
                continue
            tmp.replace(path)  # rinomina atomica, stesso filesystem
            fatti += 1
        except subprocess.TimeoutExpired:
            print(f"  [TIMEOUT] {path.name}", file=sys.stderr)
            tmp.unlink(missing_ok=True)
            errori += 1

        if i % 50 == 0 or i == len(video):
            print(f"[{i}/{len(video)}] fatti={fatti} errori={errori}")

    print(f"Completato: fatti={fatti} errori={errori}")


if __name__ == "__main__":
    main()
