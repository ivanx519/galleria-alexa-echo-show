"""
Rileva possibili foto duplicate (raffiche/scatti ripetuti) e screenshot
nella libreria (24/08/2026) — batch OFFLINE, non in tempo reale (hashare
10.900+ foto ad ogni richiesta sarebbe troppo lento). Va rilanciato a mano
quando si vuole un giro di pulizia aggiornato; l'app mostra solo l'ultimo
risultato salvato in duplicati.json.

SOLO SEGNALAZIONE — questo script non cancella nulla. L'eliminazione resta
sempre una scelta manuale di Ivan nell'app (stessa regola di ogni altra
eliminazione in questo progetto): l'app mostra i gruppi trovati e riusa la
selezione multipla + i due pulsanti elimina già esistenti.

Duplicati: hash percettivo (dHash, 64 bit) di ogni foto, calcolato dalla
versione già ridimensionata (piccola, veloce) e messo in cache dentro
meta.json (campo "hash_percettivo", mai ricalcolato una volta presente —
stesso principio idempotente di data_iso/giorno_mese). Confronto SOLO tra
foto dello STESSO giorno (data_iso): le raffiche sono sempre ravvicinate
nel tempo, e questo limita drasticamente il numero di confronti necessari
(altrimenti 10.900 foto tutte-con-tutte sarebbe ~60 milioni di confronti).

Screenshot: euristica sul nome file (contiene "screenshot", case-insensitive)
— nessuna analisi immagine necessaria, i telefoni li nominano in modo
riconoscibile.
"""
import json
import re
import sys
import time
from pathlib import Path

from PIL import Image

RIDIMENSIONATE = Path("/opt/foto_slideshow/ridimensionate")
META_PATH = Path("/opt/foto_slideshow/meta.json")
DUPLICATI_PATH = Path("/opt/foto_slideshow/duplicati.json")

# Hamming distance massima tra due hash per considerarle "duplicate" — 64
# bit totali, una manciata di bit diversi indica foto quasi identiche
# (stessa inquadratura, minime differenze di raffica/micro-movimento).
# Valore di partenza prudente, da aggiustare se in pratica risulta troppo
# permissivo/restrittivo una volta visto sul vivo.
SOGLIA_DISTANZA = 8

_PATTERN_SCREENSHOT = re.compile(r"screenshot", re.IGNORECASE)


def _hash_percettivo(path: Path) -> str:
    """dHash 8x8 (64 bit): scala di grigi 9x8, confronta ogni pixel col
    successivo sulla stessa riga (8 confronti per riga x 8 righe) — robusto
    a piccole differenze di compressione/luminosità, a differenza di un
    hash esatto del file (che cambierebbe per qualunque ricompressione)."""
    with Image.open(path) as img:
        img = img.convert("L").resize((9, 8), Image.LANCZOS)
        pixel = list(img.getdata())
    valore = 0
    for riga in range(8):
        for col in range(8):
            valore <<= 1
            if pixel[riga * 9 + col] > pixel[riga * 9 + col + 1]:
                valore |= 1
    return format(valore, "016x")


def _distanza_hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _calcola_hash_mancanti(meta: dict) -> int:
    aggiornate = 0
    for i, (nome, v) in enumerate(meta.items(), 1):
        if "hash_percettivo" in v:
            continue
        path = RIDIMENSIONATE / nome
        if not path.is_file():
            continue
        try:
            v["hash_percettivo"] = _hash_percettivo(path)
            aggiornate += 1
        except Exception as e:
            print(f"  [ERRORE HASH] {nome}: {e}", file=sys.stderr)
        if aggiornate % 500 == 0 and aggiornate:
            META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
            print(f"[{i}/{len(meta)}] hash calcolati finora={aggiornate}", file=sys.stderr)
    if aggiornate:
        META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=None))
    return aggiornate


def _trova_gruppi_duplicati(meta: dict) -> list[list[str]]:
    # Raggruppa per giorno — confronto solo entro lo stesso data_iso.
    per_giorno: dict[str, list[tuple[str, str]]] = {}
    for nome, v in meta.items():
        h, iso = v.get("hash_percettivo"), v.get("data_iso")
        if h and iso:
            per_giorno.setdefault(iso, []).append((nome, h))

    # Union-Find per raggruppare in cluster TRANSITIVI: se A~B e B~C, A e C
    # finiscono nello stesso gruppo anche se non sono direttamente entro
    # soglia tra loro (tipico di una raffica lunga con piccola deriva).
    genitore: dict[str, str] = {}

    def trova(x: str) -> str:
        while genitore.get(x, x) != x:
            x = genitore[x]
        return x

    def unisci(x: str, y: str) -> None:
        rx, ry = trova(x), trova(y)
        if rx != ry:
            genitore[rx] = ry

    for foto_giorno in per_giorno.values():
        for nome, _ in foto_giorno:
            genitore.setdefault(nome, nome)
        for i in range(len(foto_giorno)):
            nome_i, hash_i = foto_giorno[i]
            for j in range(i + 1, len(foto_giorno)):
                nome_j, hash_j = foto_giorno[j]
                if _distanza_hamming(hash_i, hash_j) <= SOGLIA_DISTANZA:
                    unisci(nome_i, nome_j)

    gruppi: dict[str, list[str]] = {}
    for nome in genitore:
        gruppi.setdefault(trova(nome), []).append(nome)
    return [g for g in gruppi.values() if len(g) > 1]


def main():
    meta = json.loads(META_PATH.read_text())
    print(f"Voci in meta.json: {len(meta)}")

    aggiornate = _calcola_hash_mancanti(meta)
    print(f"Hash calcolati in questo giro: {aggiornate}")

    gruppi_duplicati = _trova_gruppi_duplicati(meta)

    disponibili = {p.name for p in RIDIMENSIONATE.iterdir() if p.is_file()}
    gruppi_duplicati = [[n for n in g if n in disponibili] for g in gruppi_duplicati]
    gruppi_duplicati = [g for g in gruppi_duplicati if len(g) > 1]

    screenshot = sorted(n for n in disponibili if _PATTERN_SCREENSHOT.search(n))

    DUPLICATI_PATH.write_text(json.dumps({
        "generato": int(time.time()),
        "gruppi_duplicati": gruppi_duplicati,
        "screenshot": screenshot,
    }, ensure_ascii=False))

    print(f"Gruppi di possibili duplicati: {len(gruppi_duplicati)} "
          f"({sum(len(g) for g in gruppi_duplicati)} foto coinvolte)")
    print(f"Screenshot trovati: {len(screenshot)}")


if __name__ == "__main__":
    main()
