#!/usr/bin/env python3
"""Eine Weile zuhören, was Foundry von sich aus schickt — und alles mitschreiben.

    PYTHONPATH=src python3 scripts/lausche_foundry.py               # 60 Sekunden
    PYTHONPATH=src python3 scripts/lausche_foundry.py --dauer 180   # drei Minuten

Der Handschlag baut eine socket.io-Leitung auf, benutzt sie bislang aber für **eine**
Frage (``world``) und legt dann auf. Welche Ereignisse der Server von sich aus über
dieselbe Leitung schickt, steht in keiner Dokumentation: der Handschlag ist aus dem
Client nachgebaut, kein API (#146, ``docs/foundry-zugriff.md``). Also wird nicht geraten,
sondern nachgesehen — verbinden, eine einstellbare Zeit lang jedes eingehende Ereignis
samt Nutzlast wegschreiben, auflegen. Währenddessen in Foundry einmal würfeln, einmal
etwas in den Chat schreiben, eine Nachricht ändern und eine löschen; danach steht in der
Datei, wie die Ereignisse heißen.

Der Handschlag wird **nicht** nachgebaut: das Skript nimmt ``FoundryClient.verbindung``.

**Das Passwort wird nirgends gespeichert** (#64). Es wird gefragt, lebt im
Arbeitsspeicher und ist mit dem Lauf weg — keine Datei, keine Umgebungsvariable, keine
Zeile in der SQLite und kein Aufrufargument, in dem es in der Shell-History landete.

**Die Mitschrift ist personenbezogen.** Der Server filtert nicht: was über die Leitung
kommt, trägt Klarnamen, Geflüster und GM-Inhalte. Sie geht deshalb in denselben
gitignorierten Ordner wie der Weltabzug und mit denselben Rechten — 0600 in einem Ordner
mit 0700 — und der Lauf sagt in seiner ersten Zeile, was er da anlegt.

**Gespeichert wird sonst nichts.** Keine Runde, keine Nachricht, keine Zeile in der
Datenbank; die Datenbank wird nicht einmal geöffnet. Adresse und Benutzer kommen deshalb
aus der Umgebung oder vom Aufruf, nicht aus den Einstellungen einer Runde.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path

from chronicle.config import Config
from chronicle.foundry.client import FoundryClient, FoundryError
from chronicle.foundry.service import ABZUG_ORDNER, geschuetzt

FRAGE = "Foundry-Passwort: "

OHNE_PASSWORT = (
    "Ohne Foundry-Passwort gibt es keine Leitung. Es wird nirgends gespeichert — es gilt "
    "für diesen Lauf und ist danach vergessen."
)

ANSAGE = (
    "Dieser Lauf schreibt {dauer} Sekunden lang jedes eingehende Ereignis samt Nutzlast "
    "nach {ziel}.\n"
    "Foundry filtert nicht: darin stehen Klarnamen, Geflüster und GM-Inhalte. Die Datei "
    "ist personenbezogen und gehört nie ins Repo, nie in ein Issue und nie in einen "
    "Anhang.\n"
    "Sonst wird nichts gespeichert — keine Runde, keine Nachricht, keine Zeile in der "
    "Datenbank."
)

MITGESCHRIEBEN = "{anzahl} Ereignisse in {ziel} — {namen}."

NICHTS_GEHOERT = (
    "Kein einziges Ereignis in {ziel}. Entweder war in Foundry nichts los, oder der "
    "Server schickt von sich aus nichts — in dem Fall wäre ein Zuhörer der falsche Bau."
)

KEINE_LEITUNG = "Keine Leitung zu Foundry: {grund}"


def _zeitstempel() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ziel(ordner: Path = ABZUG_ORDNER) -> Path:
    return ordner / f"lauschen-{_zeitstempel().replace(':', '-')}.jsonl"


def lausche(client, datei: Path, dauer: float) -> list[str]:
    """Zuhören, wegschreiben, auflegen. Zurück kommen die Namen, nicht die Nutzlast."""
    gehoert: list[str] = []
    with geschuetzt(datei).open("a", encoding="utf-8") as mitschrift:

        def notiere(ereignis: str, *nutzlast) -> None:
            gehoert.append(ereignis)
            # ``default=str`` statt eines Abbruchs: der Handler läuft im Faden der
            # Bibliothek, und ein Fehler darin verschluckt genau das Ereignis, dessen
            # Form wir suchen.
            mitschrift.write(
                json.dumps(
                    {"zeit": _zeitstempel(), "ereignis": ereignis, "nutzlast": nutzlast},
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            mitschrift.flush()

        with client.verbindung(mithoeren=notiere):
            time.sleep(dauer)
    return gehoert


def main(
    argv: list[str] | None = None,
    *,
    client=None,
    frage=getpass,
    ordner: Path = ABZUG_ORDNER,
) -> int:
    parser = argparse.ArgumentParser(
        description="Mitschreiben, was Foundry von sich aus über die Leitung schickt."
    )
    parser.add_argument(
        "--dauer", type=float, default=60.0, metavar="SEKUNDEN", help="wie lange zugehört wird"
    )
    parser.add_argument("--url", help="Foundry-Adresse, sonst aus der Umgebung")
    parser.add_argument("--benutzer", help="Foundry-Konto, sonst aus der Umgebung")
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.url or args.benutzer:
        config = replace(
            config,
            foundry_url=args.url or config.foundry_url,
            foundry_user=args.benutzer or config.foundry_user,
        )

    passwort = frage(FRAGE)
    if not passwort:
        print(OHNE_PASSWORT, file=sys.stderr)
        return 2

    datei = ziel(ordner)
    print(ANSAGE.format(dauer=args.dauer, ziel=datei))
    try:
        gehoert = lausche(client or FoundryClient(config, passwort), datei, args.dauer)
    except FoundryError as fehler:
        print(KEINE_LEITUNG.format(grund=fehler), file=sys.stderr)
        return 1
    if not gehoert:
        print(NICHTS_GEHOERT.format(ziel=datei))
        return 0
    print(
        MITGESCHRIEBEN.format(
            anzahl=len(gehoert), ziel=datei, namen=", ".join(sorted(set(gehoert)))
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
