#!/usr/bin/env python3
"""Sagt, welcher Image-Tag beim Rollout in ``CHRONICLE_IMAGE_TAG`` gehört.

    scripts/bestimme_image_tag.py                 # sha-1234567 — der aktuelle Stand
    scripts/bestimme_image_tag.py --zurueck 1     # der Stand davor — der Weg zurück
    scripts/bestimme_image_tag.py --voll          # ghcr.io/…/foundry-chronicle:sha-1234567
    scripts/bestimme_image_tag.py --ref v0.3.0    # ein anderer Ausgangspunkt

Warum abgeleitet und nicht abgetippt: der Tag ist eine Ableitung aus dem Repo, und
Ableitungen gehören in ein Skript (CLAUDE.md » Skripte statt Prosa). Abgetippt geht es
auch — nur eben nicht wiederholbar, denn **nicht jeder Commit auf ``main`` hat ein
Image.** ``build-images.yml`` ist pfadgefiltert; ein reiner Doku- oder Template-Commit
veröffentlicht keines. ``sha-`` plus die Kurzform von ``HEAD`` zeigt deshalb regelmäßig
auf einen Tag, den es in GHCR gar nicht gibt — der Pull scheitert, und zwar erst auf der
Box. Dieses Skript läuft die Historie zurück bis zum jüngsten Commit, der wirklich einen
Bau ausgelöst hat, und liest die Pfadliste dafür aus dem Workflow selbst, damit sie nicht
an zwei Stellen gepflegt wird.

Und es beantwortet die zweite Hälfte: ``--zurueck 1`` nennt den **vorherigen** gebauten
Stand. Das ist der Weg zurück, den es mit ``latest`` nicht gab — ein misslungenes
Deployment war dort nur über einen Revert auf ``main`` und einen neuen Bau zu heilen.

Nur Standardbibliothek plus PyYAML (Dev-Abhängigkeit); das Skript läuft am Entwicklungs-
oder Verify-Rechner, nicht im Image.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

WURZEL = Path(__file__).resolve().parents[1]
WORKFLOW = WURZEL / ".github" / "workflows" / "build-images.yml"

# Dieselbe Registry-Adresse wie in templates/daggerheart-chronik/template.yml; der Test
# hält beide zusammen.
IMAGE = "ghcr.io/mdopp/foundry-chronicle"


def bildpfade(workflow_text: str) -> list[str]:
    """Die Pfade, deren Änderung einen Bau auslöst — als Git-Pathspecs.

    PyYAML liest den Schlüssel ``on:`` nach YAML 1.1 als Wahrheitswert; deshalb beide
    Schreibweisen abfragen. ``:(glob)`` weil ``src/**`` sonst ein wörtlicher Pfadname
    wäre und nichts träfe.
    """
    daten = yaml.safe_load(workflow_text)
    ausloeser = daten.get("on", daten.get(True))
    pfade = ausloeser["push"]["paths"]
    return [f":(glob){pfad}" for pfad in pfade]


def gebaute_staende(ref: str, pfade: list[str], anzahl: int) -> list[str]:
    """Die jüngsten Commits ab ``ref``, die einen Image-Bau ausgelöst haben."""
    ergebnis = subprocess.run(
        ["git", "log", f"--max-count={anzahl}", "--format=%H", ref, "--", *pfade],
        cwd=WURZEL,
        capture_output=True,
        text=True,
        check=True,
    )
    return ergebnis.stdout.split()


def tag(sha: str) -> str:
    """``sha-`` plus die ersten sieben Zeichen — die Form von ``type=sha,format=short``.

    Bewusst fest abgeschnitten und nicht ``git rev-parse --short``: dessen Länge wächst
    mit dem Repo, die des Tags in GHCR nicht.
    """
    return f"sha-{sha[:7]}"


def main(argv: list[str] | None = None) -> int:
    zerleger = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    zerleger.add_argument(
        "--ref", default="origin/main", help="Ausgangspunkt (Vorgabe origin/main)"
    )
    zerleger.add_argument(
        "--zurueck",
        type=int,
        default=0,
        metavar="N",
        help="N gebaute Stände zurück — 1 ist der Weg zurück nach einem schlechten Rollout",
    )
    zerleger.add_argument(
        "--voll", action="store_true", help="vollständige Image-Referenz statt nur des Tags"
    )
    argumente = zerleger.parse_args(argv)

    if argumente.zurueck < 0:
        print("--zurueck kann nicht negativ sein", file=sys.stderr)
        return 2

    pfade = bildpfade(WORKFLOW.read_text(encoding="utf-8"))
    staende = gebaute_staende(argumente.ref, pfade, argumente.zurueck + 1)
    if len(staende) <= argumente.zurueck:
        print(
            f"{argumente.ref} hat nur {len(staende)} gebaute Stände — "
            f"--zurueck {argumente.zurueck} gibt es dort nicht",
            file=sys.stderr,
        )
        return 1

    kennung = tag(staende[argumente.zurueck])
    print(f"{IMAGE}:{kennung}" if argumente.voll else kennung)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
