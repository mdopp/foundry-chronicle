"""python -m chronicle.foundry — ein Abgleich, im Stapel.

Das Passwort wird gefragt, nicht gelesen: es steht in keiner Datei und in keiner
Umgebungsvariable, und als Aufrufargument stünde es in der Shell-History.

``--dump`` macht denselben Handschlag, schreibt aber die rohe Antwort weg, statt sie zu
filtern und zu speichern. Das ist der Rohstoff für ``scripts/anonymisiere_welt.py`` — und
**personenbezogen**: siehe die Warnung, die der Lauf ausgibt. Das Ziel ist deshalb fest
verdrahtet (``dumps/welt-dump.json``, gitignoriert) und nicht wählbar: ein frei
angegebener Pfad legte den Abzug bei einem Tippfehler neben den Quelltext, wo ihn keine
Ignore-Regel mehr fängt.

Die Runde wird genannt, nicht geraten: eine Instanz trägt mehrere, und die erste still zu
nehmen hieße, den ungefilterten Abzug einer fremden Gruppe zu ziehen, ohne dass es jemand
sieht. Bei genau einer Runde bleibt es beim Aufruf ohne ``--runde``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from getpass import getpass

from chronicle import db, settings
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.foundry.client import FoundryError
from chronicle.foundry.service import ABZUG_ZIEL, abzug, sync

FRAGE = "Foundry-Passwort: "


class Wahl(RuntimeError):
    """Die Runde steht nicht fest — es wird nichts getan."""


def _gewaehlte_runde(config: Config, kennung: int | None) -> runden.Runde:
    # ``erste`` tut zweierlei: Schema anlegen und, bei leerer Tabelle, eine Runde
    # einfügen. Gebraucht wird hier nur das Schema — wer eine Kennung nennt, die es
    # nicht gibt, soll nichts hinterlassen.
    db.init(config.database_path)
    if kennung is not None:
        gefunden = runden.get(config.database_path, kennung)
        if gefunden is None:
            raise Wahl(f"Keine Runde mit der Kennung {kennung}.")
        return gefunden
    vorhanden = runden.alle(config.database_path)
    if len(vorhanden) > 1:
        aufzaehlung = ", ".join(f"{runde.id} = {runde.name}" for runde in vorhanden)
        raise Wahl(f"Diese Instanz trägt mehrere Runden — bitte --runde angeben: {aufzaehlung}")
    return runden.erste(config.database_path)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ein Abgleich mit Foundry.")
    parser.add_argument(
        "--dump",
        action="store_true",
        help=(
            f"statt abzugleichen die rohe Weltantwort nach {ABZUG_ZIEL} schreiben "
            "— enthält Klarnamen"
        ),
    )
    parser.add_argument(
        "--runde",
        type=int,
        metavar="KENNUNG",
        help="welche Runde — nötig, sobald die Instanz mehr als eine trägt",
    )
    args = parser.parse_args(argv)
    config = Config.from_env()
    try:
        runde = _gewaehlte_runde(config, args.runde)
    except Wahl as fehler:
        print(fehler, file=sys.stderr)
        return 1
    print(f"Runde {runde.id}: {runde.name}")
    if args.dump:
        try:
            print(abzug(config, runde, ABZUG_ZIEL, passwort=getpass(FRAGE)))
        except FoundryError as fehler:
            print(f"Kein Abzug: {fehler}", file=sys.stderr)
            return 1
        return 0
    # Die Testwelt liegt im Paket — nach einem Passwort zu fragen, das niemand braucht,
    # wäre nur eine Hürde vor einer Datei.
    testwelt = settings.foundry_quelle(runde) == settings.TESTWELT
    zustand = sync(config, runde, passwort=None if testwelt else getpass(FRAGE))
    print(zustand.message)
    return 1 if zustand.stale else 0


if __name__ == "__main__":
    sys.exit(main())
