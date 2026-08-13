"""python -m chronicle.discord — den Diktat-Kanal leeren, im Stapel.

Der Aufruf gehört neben ``python -m chronicle.transcribe`` in den nächtlichen Lauf und
davor: erst wird der Briefkasten geleert, dann läuft die Transkription über alles, was
darin lag. Er darf beliebig oft laufen — abgeholt wird nur, was noch nicht abgelegt ist.

Ohne Bot-Token passiert nichts und das sagt er auch; ein Fehlschlag ist das nicht.

**Jede Runde, nicht die erste.** Eine Instanz trägt seit #62 mehrere; der nächtliche Lauf
geht sie einzeln durch, und dieses Werkzeug daneben tat es nicht — es leerte den
Briefkasten der ersten und meldete Erfolg. Wer es bei mehreren Runden von Hand aufruft,
zum Nachholen oder zum Prüfen, hielte alle Briefkästen für geleert. Jede Zeile trägt
deshalb den Namen ihrer Runde, und dass es gar keine gibt, wird gesagt statt verschwiegen.

Eine Runde, die nicht durchkommt, hält die übrigen nicht auf: ihr Fehlschlag steht in
ihrer Zeile, die anderen laufen weiter, und der Rückgabewert bleibt trotzdem 2.

Eine Datenbank legt dieses Werkzeug nicht an. Es leert einen Briefkasten; eine Instanz
einzurichten ist die Sache des Dienstes. Ohne Datenbank gibt es keine Runde, und genau das
ist der Fall, in dem sonst still nichts geschähe — deshalb steht er als Satz da. Steht sie,
so trägt sie mindestens eine Runde: ``db.init`` legt die erste an, wenn keine da ist.
"""

from __future__ import annotations

import logging
import sys

from chronicle import db
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.discord.client import DiscordError
from chronicle.discord.service import LEER, run

KEINE_RUNDE = "Keine Runde auf dieser Instanz — es gibt keinen Briefkasten zu leeren."


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    if not config.database_path.exists():
        print(KEINE_RUNDE)
        return 0
    db.init(config.database_path)
    gescheitert = False
    for eine in runden.alle(config.database_path):
        try:
            meldungen = run(config, eine)
        except DiscordError as fehler:
            gescheitert = True
            meldungen = (str(fehler),)
        for zeile in meldungen or (LEER,):
            print(f"{eine.name}: {zeile}")
    return 2 if gescheitert else 0


if __name__ == "__main__":
    sys.exit(main())
