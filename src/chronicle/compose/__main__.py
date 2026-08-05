"""python -m chronicle.compose <sitzung> — Chronik und Rückblick, im Stapel.

Ein Lauf, zwei Ausgänge: der Rückblick liest die Chronik, die der erste Schritt gerade
abgelegt hat. Ein eigener Aufruf daneben wäre ein zweiter Stapel für dieselbe
Komposition; ein zweiter Lauf ersetzt ohnehin beides.

Rückgabewert 1 heißt: die Protokolle stehen, aber ohne Sprachmodell — geordnet statt
formuliert. Das ist ein Zustand, den ein Aufrufer sehen soll, kein Absturz.
"""

from __future__ import annotations

import logging
import sys

from chronicle.compose.service import compose_session, recap_session
from chronicle.config import Config


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        print("Aufruf: python -m chronicle.compose <sitzungs-id>")
        return 2
    sitzung = int(args[0])
    config = Config.from_env()
    chronik = compose_session(config, sitzung)
    if chronik is None:
        print(f"Sitzung {sitzung} gibt es nicht.")
        return 2
    rueckblick = recap_session(config, sitzung)
    print(chronik.message)
    print(rueckblick.message)
    return 1 if chronik.reason or rueckblick.reason else 0


if __name__ == "__main__":
    sys.exit(main())
