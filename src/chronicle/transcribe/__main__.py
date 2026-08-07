"""python -m chronicle.transcribe — Audiospuren zu Text, im Stapel.

Ohne Argumente wird die Warteschlange abgearbeitet: alles, was über die Oberfläche
hochgeladen wurde und noch wartet. Das ist der nächtliche Aufruf. Mit Sitzung und
Dateiname läuft eine von Hand abgelegte Spur — dieselbe Stufe, nur ohne Warteschlange.

Ein relativer Dateiname liegt im Aufnahmeverzeichnis (``CHRONICLE_RECORDINGS_DIR``,
Vorgabe ``recordings/``) — bewusst neben dem Datenverzeichnis und nicht darin, denn die
SQLite geht ins Backup und die Spuren nie.

Der Name der Datei wird die Quellenkennung der Spur; der Recorder-Bot legt seine Spuren
später nach Sprecher benannt ab und braucht deshalb kein weiteres Argument.

``--loeschen`` entfernt die Aufnahme nach einem erfolgreichen Lauf. Ohne die Angabe
bleibt sie liegen — nichts löscht hier von selbst.
"""

from __future__ import annotations

import logging
import sys

from chronicle import runde as runden
from chronicle.config import Config
from chronicle.transcribe.client import TranscriberError
from chronicle.transcribe.service import recording_path, run_queue, transcribe_session

AUFRUF = (
    "Aufruf: python -m chronicle.transcribe [--loeschen]                       "
    "— die Warteschlange\n"
    "        python -m chronicle.transcribe <sitzungs-id> <audiodatei> [--loeschen]"
)

LOESCHEN = "--loeschen"

LEER = "Nichts in der Warteschlange."


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    loeschen = LOESCHEN in args
    stellen = [wert for wert in args if wert != LOESCHEN]
    if not stellen:
        return _warteschlange(loeschen)
    if len(stellen) != 2 or not stellen[0].isdigit():
        print(AUFRUF)
        return 2

    config = Config.from_env()
    pfad = recording_path(config, stellen[1])
    if not pfad.is_file():
        print(f"Die Spur {pfad} gibt es nicht.")
        return 2

    try:
        transkript = transcribe_session(
            config,
            runden.erste(config.database_path),
            int(stellen[0]),
            pfad,
            delete_audio=loeschen,
        )
    except TranscriberError as fehler:
        print(str(fehler))
        return 2
    if transkript is None:
        print(f"Sitzung {stellen[0]} gibt es nicht.")
        return 2
    print(transkript.message)
    return 0


def _warteschlange(loeschen: bool) -> int:
    try:
        config = Config.from_env()
        meldungen = run_queue(config, runden.erste(config.database_path), delete_audio=loeschen)
    except TranscriberError as fehler:
        print(str(fehler))
        return 2
    for zeile in meldungen or (LEER,):
        print(zeile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
