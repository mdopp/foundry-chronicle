#!/usr/bin/env python3
"""Prüft, ob jede Änderung, die die Box erreicht, auch eine Version schneidet.

    scripts/check_release_reach.py --base origin/main

Der Typ eines Commits beschreibt seine **Absicht**, nicht seine **Reichweite** (#343).
Ein `refactor`, der `src/**` anfasst, ist grün gemergt, schneidet aber kein Release —
das Abbild wird nie gebaut, die Änderung erreicht den Server nie, und nichts meldet es.
Geprüft wird deshalb nach Wirkung: berührt ein Commit einen Pfad, der ausgeliefert wird,
muss er unter einer Art stehen, die release-please zu einem Release-PR bewegt.

Läuft **nur** in CI gegen den PR-Base, nicht im `commit-msg`-Hook: dort gibt es nur die
Nachricht und keinen Vergleichsstand, und eine Prüfung, die dort still nichts tut, wäre
die schlechtere Hälfte einer Prüfung, die laut scheitern soll.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field

# Gemessen an release-please, nicht angenommen: die Vorgabe-VersioningStrategy bumpt zwar
# für jede Art, aber davor liegt der eigentliche Riegel — bleiben die Release-Notes leer,
# legt `strategies/base.ts` gar keinen Release-PR an. Was in die Notes kommt, entscheidet
# `src/util/filter-commits.ts`: DEFAULT_CHANGELOG_SECTIONS führt chore/docs/style/refactor/
# test/build/ci als `hidden`, sichtbar bleiben feat/fix/perf/revert.
# `release-please-config.json` setzt kein `changelog-sections` und erbt damit genau das.
# In-Repo-Beleg: v0.5.0..main enthält genau einen Commit (refactor) — und keinen Release-PR.
SCHNEIDENDE_ARTEN = ("feat", "fix", "revert")

# Ausdrücklich abgeleitet, nicht beobachtet: dass ein Commit-Bereich **allein** aus `docs`
# eine Version schneidet oder eben nicht, ist in diesem Repo nie vorgekommen — kein
# Release-Bereich von v0.2.0 bis v0.5.0 bestand nur aus docs. Die fünf
# »### Documentation«-Abschnitte im CHANGELOG belegen das Gegenteil nicht: sichtbar **in**
# einem ohnehin geschnittenen Release ist nicht dasselbe wie auslösend. Ohne Messung gilt
# hier die vorsichtige Seite — `docs` bleibt eine nicht schneidende Art.
NICHT_SCHNEIDENDE_ARTEN = ("refactor", "chore", "docs", "test")

# Bodenwahrheit ist das Dockerfile, nicht die Intuition: kopiert werden `pyproject.toml`,
# `src` und `scripts/verify_e2e.py`; `Dockerfile` und `.dockerignore` bestimmen das Abbild
# selbst. `templates/**` liegt **nicht** im Abbild, erreicht die Box aber über ServiceBay
# und ist zugleich der einzige verify-pflichtige Pfad — »geht ins Abbild« ist die Kurzform,
# gemeint ist »erreicht die Box«.
AUSLIEFERUNGS_DATEIEN = ("pyproject.toml", "Dockerfile", ".dockerignore", "scripts/verify_e2e.py")
AUSLIEFERUNGS_BAEUME = ("src", "templates")

# README.md wird zwar ins Abbild kopiert, landet dort aber nur als Paket-Metadatum in der
# dist-info und hat keine Wirkung für eine Runde. Stünde es in der Liste, müsste jeder
# docs-Commit am README als `fix` getarnt werden — genau die Alternative, die der Betreiber
# am 2026-09-06 verworfen hat, weil sie Versionen ohne Änderung für die Runde erzeugt.
NICHT_AUSGELIEFERT = ("README.md",)

# `Revert "…"` fehlt hier bewusst, anders als in scripts/check_commit_subjects.py: Gits
# eigene Rücknahme-Form trägt keine Conventional-Art, schneidet also kein Release — und
# eine Rücknahme, die den Server nicht erreicht, ist der Fall, für den es diesen Wächter
# gibt. CLAUDE.md § Commits nennt diese eine Strenge ausdrücklich.
UEBERSPRUNGEN = ("Merge ", "fixup!", "squash!")

SUBJECT = re.compile(r"^(?P<art>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<bruch>!)?: ")
BRUCH = re.compile(r"^BREAKING[ -]CHANGE:", re.M)
AUSSTIEG = re.compile(r"^Ohne-Auslieferung:(?P<grund>.*)$", re.M)


@dataclass
class Commit:
    sha: str
    subject: str
    rumpf: str
    pfade: list[str] = field(default_factory=list)


@dataclass
class Verstoss:
    commit: Commit
    pfade: list[str]
    grund: str


def ist_auslieferungspfad(pfad: str) -> bool:
    if pfad in AUSLIEFERUNGS_DATEIEN:
        return True
    return any(pfad == baum or pfad.startswith(baum + "/") for baum in AUSLIEFERUNGS_BAEUME)


def art_von(subject: str) -> str | None:
    treffer = SUBJECT.match(subject)
    return None if treffer is None else treffer["art"]


def schneidet_release(subject: str, rumpf: str = "") -> bool:
    treffer = SUBJECT.match(subject)
    if treffer is None:
        return False
    if treffer["bruch"] or BRUCH.search(rumpf):
        return True
    return treffer["art"] in SCHNEIDENDE_ARTEN


def ausstiegsgrund(rumpf: str) -> str | None:
    """Der Grund des Notausstiegs — `None` ohne Trailer, `''` bei leerem Grund."""
    treffer = AUSSTIEG.search(rumpf)
    return None if treffer is None else treffer["grund"].strip()


def pruefe(commits: list[Commit]) -> tuple[list[Verstoss], list[tuple[Commit, str]]]:
    verstoesse: list[Verstoss] = []
    ausstiege: list[tuple[Commit, str]] = []
    for commit in commits:
        if commit.subject.startswith(UEBERSPRUNGEN):
            continue
        pfade = sorted(pfad for pfad in commit.pfade if ist_auslieferungspfad(pfad))
        if not pfade or schneidet_release(commit.subject, commit.rumpf):
            continue
        grund = ausstiegsgrund(commit.rumpf)
        if grund:
            ausstiege.append((commit, grund))
        elif grund == "":
            verstoesse.append(Verstoss(commit, pfade, "Trailer »Ohne-Auslieferung« ohne Grundtext"))
        else:
            art = art_von(commit.subject)
            bezeichnung = f"Art »{art}«" if art else "Subject ohne Conventional-Art"
            verstoesse.append(Verstoss(commit, pfade, f"{bezeichnung} schneidet kein Release"))
    return verstoesse, ausstiege


def commits_im_bereich(base: str) -> list[Commit]:
    trenner, satzende = "\x1f", "\x1e"
    muster = f"--format=%H{trenner}%s{trenner}%b{satzende}"
    roh = subprocess.run(
        ["git", "log", "--no-merges", muster, f"{base}..HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    commits = []
    for satz in roh.split(satzende):
        if not satz.strip():
            continue
        sha, subject, rumpf = satz.lstrip("\n").split(trenner, 2)
        commits.append(Commit(sha=sha, subject=subject, rumpf=rumpf, pfade=pfade_von(sha)))
    return commits


def pfade_von(sha: str) -> list[str]:
    roh = subprocess.run(
        ["git", "show", "--name-only", "--format=", sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [zeile for zeile in roh.splitlines() if zeile.strip()]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Prüfen, ob ausgelieferte Änderungen eine Version schneiden."
    )
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args(argv[1:])

    commits = commits_im_bereich(args.base)
    verstoesse, ausstiege = pruefe(commits)

    # Auch im grünen Lauf gedruckt: ein Ausstieg, den niemand sieht, ist ein Loch.
    for commit, grund in ausstiege:
        print(f"↷ {commit.sha[:7]} {commit.subject}\n  Ohne-Auslieferung: {grund}")

    for verstoss in verstoesse:
        commit = verstoss.commit
        print(f"✗ {commit.sha[:7]} {commit.subject}", file=sys.stderr)
        print(f"  {verstoss.grund}, die Änderung erreicht aber die Box:", file=sys.stderr)
        for pfad in verstoss.pfade:
            print(f"    {pfad}", file=sys.stderr)
        print(
            "  Art auf feat/fix/revert ändern — oder, wenn wirklich nichts ausgeliefert",
            file=sys.stderr,
        )
        print(
            "  wird, »Ohne-Auslieferung: <Grund>« in den Commit-Rumpf setzen.",
            file=sys.stderr,
        )

    if verstoesse:
        print(
            f"\n{len(verstoesse)} von {len(commits)} Commits gehen auf die Box, "
            "ohne eine Version zu schneiden.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(commits)} Commit(s) geprüft — was ausgeliefert wird, schneidet eine Version.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
