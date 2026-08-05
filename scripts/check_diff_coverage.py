#!/usr/bin/env python3
"""Prüft die Diff-Coverage gegen die Untergrenze aus CLAUDE.md » Tests.

    scripts/check_diff_coverage.py --base origin/main --xml coverage.xml --min 70

Gezählt werden nur geänderte Zeilen, die in `coverage.xml` als Anweisung vorkommen:
Leerzeilen, Kommentare und ungemessene Dateien stehen nicht im Nenner. Ändert ein
Commit keine gemessene Python-Zeile, ist der Lauf grün — es gibt nichts zu prüfen.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

FLOOR = 70.0

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


def parse_diff(diff_text: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = {}
    path: str | None = None
    number = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if line.startswith("--- ") or line.startswith("diff ") or line.startswith("index "):
            continue
        hunk = HUNK.match(line)
        if hunk:
            number = int(hunk["start"])
            continue
        if path is None:
            continue
        if line.startswith("+"):
            changed.setdefault(path, set()).add(number)
            number += 1
        elif line.startswith(" "):
            number += 1
    return changed


def parse_coverage(xml_text: str) -> dict[str, dict[int, int]]:
    """Zeilen je Datei — unter jedem Pfad, unter dem coverage.xml sie meinen kann.

    Die Dateinamen stehen dort relativ zur jeweiligen Quellwurzel: aus der Quelle
    »src/chronicle« und filename="app.py" wird der Git-Pfad src/chronicle/app.py.
    """
    root = ElementTree.fromstring(xml_text)
    sources = [
        source.text.strip().rstrip("/")
        for source in root.findall("./sources/source")
        if source.text and source.text.strip()
    ]
    measured: dict[str, dict[int, int]] = {}
    for element in root.iter("class"):
        filename = element.get("filename")
        if not filename:
            continue
        lines = {
            int(line.get("number", "0")): int(line.get("hits", "0"))
            for line in element.iter("line")
        }
        for key in [filename] + [f"{source}/{filename}" for source in sources]:
            measured.setdefault(key, {}).update(lines)
    return measured


def measured_for(path: str, measured: dict[str, dict[int, int]]) -> dict[int, int] | None:
    if path in measured:
        return measured[path]
    best: str | None = None
    for filename in measured:
        if path.endswith("/" + filename) and (best is None or len(filename) > len(best)):
            best = filename
    return None if best is None else measured[best]


def diff_coverage(
    changed: dict[str, set[int]], measured: dict[str, dict[int, int]]
) -> tuple[int, int, list[str]]:
    total = 0
    covered = 0
    misses: list[str] = []
    for path in sorted(changed):
        if not path.endswith(".py"):
            continue
        lines = measured_for(path, measured)
        if lines is None:
            continue
        for number in sorted(changed[path]):
            hits = lines.get(number)
            if hits is None:
                continue
            total += 1
            if hits > 0:
                covered += 1
            else:
                misses.append(f"{path}:{number}")
    return total, covered, misses


def changed_lines(base: str) -> dict[str, set[int]]:
    result = subprocess.run(
        ["git", "diff", "-U0", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_diff(result.stdout)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Diff-Coverage gegen die Untergrenze prüfen.")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--xml", default="coverage.xml")
    parser.add_argument("--min", type=float, default=FLOOR)
    args = parser.parse_args(argv[1:])

    changed = changed_lines(args.base)
    if not any(path.endswith(".py") for path in changed):
        print(f"Keine geänderten Python-Zeilen gegenüber {args.base} — nichts zu prüfen.")
        return 0

    xml_path = Path(args.xml)
    if not xml_path.is_file():
        print(
            f"{xml_path} fehlt — erst »pytest --cov --cov-report=xml« laufen lassen.",
            file=sys.stderr,
        )
        return 2

    total, covered, misses = diff_coverage(changed, parse_coverage(xml_path.read_text("utf-8")))
    if total == 0:
        print("Keine gemessene Python-Zeile geändert — nichts zu prüfen.")
        return 0

    percent = 100.0 * covered / total
    print(
        f"Diff-Coverage: {covered}/{total} Zeilen = {percent:.1f} % — Untergrenze {args.min:.0f} %"
    )
    if percent < args.min:
        for miss in misses:
            print(f"  ungedeckt: {miss}", file=sys.stderr)
        print("Untergrenze verfehlt — Test ergänzen, nicht die Grenze senken.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
