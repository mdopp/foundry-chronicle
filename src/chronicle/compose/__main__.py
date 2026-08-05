"""python -m chronicle.compose <sitzung> — eine Chronik, im Stapel.

Rückgabewert 1 heißt: das Protokoll steht, aber ohne Sprachmodell — geordnet statt
formuliert. Das ist ein Zustand, den ein Aufrufer sehen soll, kein Absturz.
"""

from __future__ import annotations

import logging
import sys

from chronicle.compose.service import compose_session
from chronicle.config import Config


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        print("Aufruf: python -m chronicle.compose <sitzungs-id>")
        return 2
    sitzung = int(args[0])
    ergebnis = compose_session(Config.from_env(), sitzung)
    if ergebnis is None:
        print(f"Sitzung {sitzung} gibt es nicht.")
        return 2
    print(ergebnis.message)
    return 1 if ergebnis.reason else 0


if __name__ == "__main__":
    sys.exit(main())
