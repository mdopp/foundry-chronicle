#!/usr/bin/env python3
"""Sagt, welcher Image-Tag beim Rollout in ``CHRONICLE_IMAGE_TAG`` gehört.

    scripts/bestimme_image_tag.py                 # sha-1234567 — der aktuelle Stand
    scripts/bestimme_image_tag.py --zurueck 1     # der Stand davor — der Weg zurück
    scripts/bestimme_image_tag.py --voll          # ghcr.io/…/foundry-chronicle:sha-1234567
    scripts/bestimme_image_tag.py --ref v0.3.0    # ein anderer Ausgangspunkt

Warum abgeleitet und nicht abgetippt: der Tag ist eine Ableitung aus dem Repo, und
Ableitungen gehören in ein Skript (CLAUDE.md » Skripte statt Prosa). Abgetippt geht es
auch — nur eben nicht wiederholbar, denn **nicht jeder Commit auf ``main`` hat ein
Image.** ``build-images.yml`` ist pfadgefiltert; ein Push, der keine Bau-Pfade anfasst,
veröffentlicht keines.

Wonach das Skript fragt: **welche Tags in GHCR wirklich liegen.** Es rät sie nicht aus
der Historie. Der frühere Weg — rückwärts bis zum jüngsten Commit laufen, der Bau-Pfade
ändert — beantwortete eine Frage, die die Registry gar nicht indiziert: getaggt wird mit
``GITHUB_SHA`` des **Pushes**, also mit dessen letztem Commit, und der Pfadfilter greift
über den ganzen Push. Landet ein Batch fünf Commits in einem Rebase-Merge (CLAUDE.md
» main bleibt linear), dann trägt der Batch-Kopf den Tag, auch wenn er selbst nur die
README anfasst — und der Bau-Pfad-Commit mittendrin hat gar keinen. Genau so entstand
#176: abgeleitet ``sha-e4bc066``, vorhanden ``sha-7e758a1``, 404 auf der Box.

Git liefert deshalb nur noch die **Reihenfolge**, die Registry die **Antwort**: ab
``--ref`` rückwärts, der erste Commit, dessen Tag sich in GHCR abrufen lässt. ``--zurueck
1`` nennt den veröffentlichten Stand davor — den Weg zurück, den es mit ``latest`` nicht
gab.

Trägt ``--ref`` selbst kein Image, wird das **gesagt, nicht verschwiegen**: wurde seither
kein Bau-Pfad angefasst, ist der ältere Stand inhaltlich derselbe und wird mit einem
Hinweis genannt; wurde einer angefasst, fehlt ein Image, das es geben müsste — dann nennt
das Skript keinen Tag, sondern den Grund.

Nur Standardbibliothek plus PyYAML (Dev-Abhängigkeit); das Skript läuft am Entwicklungs-
oder Verify-Rechner, nicht im Image. Es braucht dabei Netz zu GHCR — anonym, das Paket
ist öffentlich.
"""

from __future__ import annotations

import argparse
import functools
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

WURZEL = Path(__file__).resolve().parents[1]
WORKFLOW = WURZEL / ".github" / "workflows" / "build-images.yml"

# Dieselbe Registry-Adresse wie in templates/daggerheart-chronik/template.yml; der Test
# hält beide zusammen.
IMAGE = "ghcr.io/mdopp/foundry-chronicle"
REGISTRY, PAKET = IMAGE.split("/", 1)

# So weit wird rückwärts gefragt. Ein Batch ist ein Push; zwanzig Pushes ohne einen
# einzigen Bau gibt es nicht — wer hier anschlägt, hat ein anderes Problem als den Tag.
GRENZE = 50

# Ohne passendes Accept antwortet eine Registry auf einen Multi-Arch-Index mit 404.
MANIFEST_TYPEN = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


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


def tag(sha: str) -> str:
    """``sha-`` plus die ersten sieben Zeichen — die Form von ``type=sha,format=short``.

    Bewusst fest abgeschnitten und nicht ``git rev-parse --short``: dessen Länge wächst
    mit dem Repo, die des Tags in GHCR nicht.
    """
    return f"sha-{sha[:7]}"


@functools.lru_cache(maxsize=1)
def _pull_token() -> str:
    """Ein anonymes Pull-Token; für ein öffentliches Paket gibt GHCR es ohne Anmeldung."""
    adresse = f"https://{REGISTRY}/token?scope=repository:{PAKET}:pull&service={REGISTRY}"
    with urllib.request.urlopen(adresse, timeout=15) as antwort:
        return json.load(antwort)["token"]


def existiert_in_der_registry(kennung: str) -> bool:
    """Liegt dieser Tag wirklich in GHCR? Dieselbe Abfrage, an der die Box scheitert."""
    anfrage = urllib.request.Request(
        f"https://{REGISTRY}/v2/{PAKET}/manifests/{kennung}",
        method="HEAD",
        headers={"Authorization": f"Bearer {_pull_token()}", "Accept": MANIFEST_TYPEN},
    )
    try:
        with urllib.request.urlopen(anfrage, timeout=15):
            return True
    except urllib.error.HTTPError as fehler:
        if fehler.code in (401, 404):
            return False
        raise


def historie(ref: str, anzahl: int = GRENZE) -> list[str]:
    """Die Commits ab ``ref`` rückwärts — ungefiltert, Git liefert hier nur die Reihenfolge."""
    ergebnis = subprocess.run(
        ["git", "log", f"--max-count={anzahl}", "--format=%H", ref],
        cwd=WURZEL,
        capture_output=True,
        text=True,
        check=True,
    )
    return ergebnis.stdout.split()


def veroeffentlichte_staende(ref: str, anzahl: int) -> list[str]:
    """Die jüngsten Commits ab ``ref``, für die in GHCR ein Image liegt."""
    treffer: list[str] = []
    for sha in historie(ref):
        if existiert_in_der_registry(tag(sha)):
            treffer.append(sha)
            if len(treffer) == anzahl:
                break
    return treffer


def baupfade_geaendert(von: str, bis: str, pfade: list[str]) -> bool:
    """Unterscheidet die beiden Gründe, aus denen ``bis`` kein eigenes Image hat."""
    ergebnis = subprocess.run(
        ["git", "diff", "--name-only", f"{von}..{bis}", "--", *pfade],
        cwd=WURZEL,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(ergebnis.stdout.strip())


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
        help="N veröffentlichte Stände zurück — 1 ist der Weg zurück nach einem schlechten Rollout",
    )
    zerleger.add_argument(
        "--voll", action="store_true", help="vollständige Image-Referenz statt nur des Tags"
    )
    argumente = zerleger.parse_args(argv)

    if argumente.zurueck < 0:
        print("--zurueck kann nicht negativ sein", file=sys.stderr)
        return 2

    pfade = bildpfade(WORKFLOW.read_text(encoding="utf-8"))
    try:
        staende = veroeffentlichte_staende(argumente.ref, argumente.zurueck + 1)
    except OSError as fehler:
        print(
            f"GHCR nicht erreichbar ({fehler}) — ohne Antwort der Registry wird kein Tag geraten",
            file=sys.stderr,
        )
        return 2

    if len(staende) <= argumente.zurueck:
        print(
            f"{argumente.ref} hat in den letzten {GRENZE} Commits nur {len(staende)} "
            f"veröffentlichte Stände — --zurueck {argumente.zurueck} gibt es dort nicht",
            file=sys.stderr,
        )
        return 1

    gewaehlt = staende[argumente.zurueck]
    kopf = historie(argumente.ref, 1)[0]
    if argumente.zurueck == 0 and gewaehlt != kopf:
        if baupfade_geaendert(gewaehlt, kopf, pfade):
            print(
                f"{argumente.ref} ändert Bau-Pfade gegenüber {tag(gewaehlt)}, hat aber selbst "
                f"kein Image in GHCR — der Bau fehlt oder ist rot. Kein Tag genannt, denn "
                f"{tag(gewaehlt)} wäre ein anderer Stand als {argumente.ref}",
                file=sys.stderr,
            )
            return 1
        print(
            f"Hinweis: {argumente.ref} hat kein eigenes Image; seit {tag(gewaehlt)} wurde "
            f"kein Bau-Pfad angefasst, der Inhalt ist derselbe",
            file=sys.stderr,
        )

    kennung = tag(gewaehlt)
    print(f"{IMAGE}:{kennung}" if argumente.voll else kennung)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
