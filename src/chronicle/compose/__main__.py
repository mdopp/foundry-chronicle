"""python -m chronicle.compose <sitzung> — Chronik, Rückblick, Zustellung, im Stapel.

Ein Lauf, zwei Ausgänge: der Rückblick liest die Chronik, die der erste Schritt gerade
abgelegt hat. Ein eigener Aufruf daneben wäre ein zweiter Stapel für dieselbe
Komposition; ein zweiter Lauf ersetzt ohnehin beides.

Die Zustellung nach Discord hängt hier und nicht am Diktat-Lauf: **hier** entsteht ein
neuer Rückblick, und der Diktat-Lauf ist die Gegenrichtung — dort abgeholt, würde der
Rückblick eine Nacht zu spät im Kanal stehen. Zugestellt wird höchstens einmal je
Sitzung; ohne Kanal oder ohne Bot-Token passiert nichts und der Lauf sagt es. Die Chronik
geht denselben Weg als Datei in den Thread der Sitzung — eine neue Fassung als neue Datei.

Rückgabewert 1 heißt: die Protokolle stehen, aber ohne Sprachmodell — geordnet statt
formuliert. Das ist ein Zustand, den ein Aufrufer sehen soll, kein Absturz.
"""

from __future__ import annotations

import logging
import sys

from chronicle import register
from chronicle import runde as runden
from chronicle.compose.service import compose_session, recap_session
from chronicle.config import Config
from chronicle.discord.ausgabe import anhaengen
from chronicle.discord.rueckblick import deliver


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        print("Aufruf: python -m chronicle.compose <sitzungs-id>")
        return 2
    sitzung = int(args[0])
    config = Config.from_env()
    # Der Stapelaufruf kennt noch keine Runde; er nimmt die erste, wie die Oberfläche.
    runde = runden.erste(config.database_path)
    chronik = compose_session(config, runde, sitzung)
    if chronik is None:
        print(f"Sitzung {sitzung} gibt es nicht.")
        return 2
    rueckblick = recap_session(config, runde, sitzung)
    print(chronik.message)
    print(rueckblick.message)
    print(deliver(config, runde, sitzung).meldung)
    hinweis = anhaengen(config, runde, sitzung)
    if hinweis:
        print(hinweis)
    print(register.suggest(config, runde, sitzung).message)
    return 1 if chronik.reason or rueckblick.reason else 0


if __name__ == "__main__":
    sys.exit(main())
