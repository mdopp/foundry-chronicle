"""python -m chronicle.transcribe <sitzung> <spur> — eine Audiospur, im Stapel.

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

from chronicle.config import Config
from chronicle.transcribe.client import TranscriberError
from chronicle.transcribe.service import recording_path, transcribe_session

AUFRUF = "Aufruf: python -m chronicle.transcribe <sitzungs-id> <audiodatei> [--loeschen]"

LOESCHEN = "--loeschen"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    loeschen = LOESCHEN in args
    stellen = [wert for wert in args if wert != LOESCHEN]
    if len(stellen) != 2 or not stellen[0].isdigit():
        print(AUFRUF)
        return 2

    config = Config.from_env()
    pfad = recording_path(config, stellen[1])
    if not pfad.is_file():
        print(f"Die Spur {pfad} gibt es nicht.")
        return 2

    try:
        transkript = transcribe_session(config, int(stellen[0]), pfad, delete_audio=loeschen)
    except TranscriberError as fehler:
        print(str(fehler))
        return 2
    if transkript is None:
        print(f"Sitzung {stellen[0]} gibt es nicht.")
        return 2
    print(transkript.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
