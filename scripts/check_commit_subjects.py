#!/usr/bin/env python3
"""Prüft Commit-Subjects gegen CLAUDE.md » Commits.

scripts/check_commit_subjects.py origin/main..HEAD
scripts/check_commit_subjects.py --message-file .git/COMMIT_EDITMSG
"""

import re
import subprocess
import sys

TYPES = ("feat", "fix", "refactor", "chore", "docs", "test", "revert")

# Ein verirrtes (...) im Subject lässt release-please grün laufen und trotzdem
# kein Release schneiden — deshalb ist die Klammer eine eigene Prüfung.
SUBJECT = re.compile(
    r"^(?P<type>" + "|".join(TYPES) + r")"
    r"(?:\((?P<scope>[a-z0-9._/-]+)\))?"
    r"!?: (?P<rest>\S.*)$"
)

SKIP = ("Merge ", "Revert ", "fixup!", "squash!")


def problem(subject):
    if subject.startswith(SKIP):
        return None
    match = SUBJECT.match(subject)
    if match is None:
        return f"nicht »type(scope): subject« mit type aus {'/'.join(TYPES)}"
    if "(" in match["rest"] or ")" in match["rest"]:
        return "Klammern im Subject — Release-Werkzeuge laufen grün und schneiden kein Release"
    return None


def subjects_in_range(rev_range):
    result = subprocess.run(
        ["git", "log", "--no-merges", "--format=%s", rev_range],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main(argv):
    if len(argv) == 3 and argv[1] == "--message-file":
        with open(argv[2], encoding="utf-8") as handle:
            subjects = [handle.readline().rstrip("\n")]
    elif len(argv) == 2 and not argv[1].startswith("-"):
        subjects = subjects_in_range(argv[1])
    else:
        print(__doc__, file=sys.stderr)
        return 2

    failed = [(subject, found) for subject in subjects if (found := problem(subject))]
    for subject, found in failed:
        print(f"✗ {subject}\n  {found}", file=sys.stderr)

    if failed:
        print(
            f"\n{len(failed)} von {len(subjects)} Subjects verletzen die Hausregel.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(subjects)} Subject(s) in Ordnung.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
