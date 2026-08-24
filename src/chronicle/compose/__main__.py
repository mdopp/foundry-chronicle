"""python -m chronicle.compose <sitzung> [<runde>] — Chronik, Rückblick, Zustellung, im Stapel.

Gegangen wird ``kette.schreiben``, dieselbe Reihenfolge wie hinter ``/session done`` und
im Nachtlauf; hier steht nur, was gedruckt wird. Bis #221 stand sie hier ein zweites Mal
und ohne die Übernahme der Transkripte — dieser Stapel schrieb damit eine Chronik ohne
gesprochenes Wort.

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

**In welcher Runde gesucht wird, sagt der Aufrufer.** Bis #245 nahm der Stapel immer die
erste — »wie die Oberfläche«, aus der Zeit, als eine Instanz genau eine Runde trug (die
Oberfläche selbst ist mit #157 fort). Alles außerhalb von Runde 1 war damit von hier aus
unerreichbar. Aufgelöst wird die Sitzungs-Id trotzdem **nicht** über die Runden hinweg,
obwohl sie instanzweit eindeutig ist: aus einer Nummer die Runde zu erschließen hieße, in
eine Runde zu greifen, die niemand genannt hat, und genau davor steht ``db.scoped``. Wer
mehrere Runden trägt, nennt eine; solange es nur eine gibt, bleibt es beim Aufruf von
vorher.
"""

from __future__ import annotations

import logging
import sys

from chronicle import db, kette
from chronicle import runde as runden
from chronicle.config import Config

AUFRUF = "Aufruf: python -m chronicle.compose <sitzungs-id> [<runden-id>]"
UNBEKANNTE_RUNDE = "Runde {gewaehlt} gibt es hier nicht."
WELCHE_RUNDE = (
    "Diese Instanz trägt mehrere Runden. Gesucht wird die Sitzung in der genannten Runde "
    "und in keiner anderen — welche ist gemeint?"
)
RUNDENZEILE = "  {id}  {name}"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if not 1 <= len(args) <= 2 or not all(teil.isdigit() for teil in args):
        print(AUFRUF)
        return 2
    sitzung = int(args[0])
    gewaehlt = int(args[1]) if len(args) == 2 else None
    config = Config.from_env()
    db.init(config.database_path)
    if gewaehlt is not None:
        runde = runden.get(config.database_path, gewaehlt)
        if runde is None:
            print(UNBEKANNTE_RUNDE.format(gewaehlt=gewaehlt))
            return 2
    else:
        vorhandene = runden.alle(config.database_path)
        if len(vorhandene) > 1:
            print(WELCHE_RUNDE)
            for eine in vorhandene:
                print(RUNDENZEILE.format(id=eine.id, name=eine.name))
            return 2
        # Eine einzige Runde lässt keine Wahl offen; auf einer frischen Datenbank legt
        # ``erste`` sie an, wie es dieser Aufruf immer getan hat.
        runde = runden.erste(config.database_path)
    lauf = kette.schreiben(config, runde, sitzung)
    if lauf is None:
        print(kette.warum_nicht(runde))
        return 2
    print(lauf.chronik.message)
    if lauf.rueckblick is not None:
        print(lauf.rueckblick.message)
    print(lauf.zustellung.meldung)
    if lauf.ausgabe:
        print(lauf.ausgabe)
    print(lauf.vorschlaege.message)
    ohne_modell = lauf.chronik.reason or (lauf.rueckblick is not None and lauf.rueckblick.reason)
    return 1 if ohne_modell else 0


if __name__ == "__main__":
    sys.exit(main())
