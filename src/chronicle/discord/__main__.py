"""python -m chronicle.discord — den Diktat-Kanal leeren, im Stapel.

Der Aufruf gehört neben ``python -m chronicle.transcribe`` in den nächtlichen Lauf und
davor: erst wird der Briefkasten geleert, dann läuft die Transkription über alles, was
darin lag. Er darf beliebig oft laufen — abgeholt wird nur, was noch nicht abgelegt ist.

Ohne Bot-Token passiert nichts und das sagt er auch; ein Fehlschlag ist das nicht.
"""

from __future__ import annotations

import logging
import sys

from chronicle.config import Config
from chronicle.discord.client import DiscordError
from chronicle.discord.service import LEER, run


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        meldungen = run(Config.from_env())
    except DiscordError as fehler:
        print(str(fehler))
        return 2
    for zeile in meldungen or (LEER,):
        print(zeile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
