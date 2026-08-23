#!/usr/bin/env python3
"""Kann der installierte Produktionssatz sein eigenes Einstiegsmodul importieren?

Das ist die teure, aber ehrliche Hälfte des Tors aus #259: nicht die erklärten
Abhängigkeiten gegen die Importzeilen halten (das tut ``tests/test_laufzeit_importe.py``
in einer Zehntelsekunde), sondern den Satz **installieren** und wirklich importieren.
Nur so fällt auf, was keine Importzeile verrät — ein Paket, dessen Modul unter anderem
Namen liegt, eine Endlosschleife beim Laden, eine fehlende Systembibliothek.

Aufgerufen wird das gegen eine Umgebung, in der ``pip install .[discord]`` gelaufen ist
— derselbe Befehl, den der Dockerfile ausführt — und **ohne** das ``dev``-Extra. Läuft es
gegen die Arbeitskopie, prüft es nichts: dann liegt ``src`` im Pfad und jedes Paket der
Entwicklungsumgebung ist da. Deshalb steht die Herkunftsprüfung ganz vorn.

Importiert wird das **ganze** Paket, nicht nur ``chronicle.bot.__main__``. Der Grund ist
der Vorfall selbst: ``__main__`` holt ``gateway`` erst in ``_gateway()``, also innerhalb
von ``main()``. Ein bloßes ``import chronicle.bot.__main__`` wäre am 06f2329 grün
gewesen und der Bot trotzdem in der Absturzschleife.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    # ``--paket`` ist nur für den Test da: ein Name, den es garantiert nur an einer Stelle
    # gibt, hängt nicht davon ab, wie chronicle gerade installiert ist.
    zerleger = argparse.ArgumentParser(description=__doc__)
    zerleger.add_argument("--paket", default="chronicle")
    argumente = zerleger.parse_args(argv)
    einstieg = f"{argumente.paket}.bot.__main__"

    try:
        paket = importlib.import_module(argumente.paket)
    except ImportError as fehler:
        print(f"{argumente.paket} ist gar nicht installiert: {fehler}", file=sys.stderr)
        return 1

    ort = Path(paket.__file__).resolve()
    if (Path.cwd() / "src") in ort.parents:
        print(
            f"Geprüft würde die Arbeitskopie ({ort}), nicht die Installation. "
            "Diesen Lauf in einer Umgebung starten, in der `pip install .[discord]` "
            "gelaufen ist und `src` nicht im Pfad liegt.",
            file=sys.stderr,
        )
        return 1

    module = [einstieg] + [
        modul.name
        for modul in pkgutil.walk_packages(paket.__path__, f"{argumente.paket}.")
        if modul.name != einstieg
    ]

    kaputt: list[tuple[str, str]] = []
    for name in module:
        try:
            importlib.import_module(name)
        except Exception as fehler:
            kaputt.append((name, f"{type(fehler).__name__}: {fehler}"))

    if kaputt:
        print(
            f"Der Produktionssatz kann {len(kaputt)} von {len(module)} Modulen nicht "
            "importieren — so gebaut startet das Abbild nicht:",
            file=sys.stderr,
        )
        for name, grund in kaputt:
            print(f"  {name}: {grund}", file=sys.stderr)
        return 1

    print(f"{len(module)} Module importiert, {einstieg} erreichbar — aus {ort.parent}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
