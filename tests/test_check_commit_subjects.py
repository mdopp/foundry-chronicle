import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_commit_subjects",
    Path(__file__).resolve().parent.parent / "scripts" / "check_commit_subjects.py",
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_akzeptiert_die_erlaubten_typen():
    for typ in ("feat", "fix", "refactor", "chore", "docs", "test"):
        assert check.problem(f"{typ}: etwas geändert") is None


def test_akzeptiert_scope_und_breaking_marker():
    assert check.problem("feat(ui): Notizfeld je Szene") is None
    assert check.problem("fix(foundry): Chat-Log-Abruf bei Timeout") is None
    assert check.problem("feat(discord)!: Einwilligungs-Ansage erzwingen") is None


def test_lehnt_unbekannten_typ_ab():
    assert check.problem("wip: halbfertig") is not None
    assert check.problem("Notizfeld ergänzt") is not None


def test_lehnt_klammern_im_subject_ab():
    found = check.problem("fix(ui): Notizfeld (endlich) repariert")
    assert found is not None
    assert "Klammern" in found


def test_lehnt_leeres_subject_ab():
    assert check.problem("docs: ") is not None


def test_ueberspringt_merge_und_fixup():
    assert check.problem('Revert "feat(ui): Notizfeld"') is None
    assert check.problem("Merge branch 'main' into batch/2026-08-04a") is None
    assert check.problem("fixup! feat(ui): Notizfeld") is None


def test_scope_darf_keine_grossbuchstaben_haben():
    assert check.problem("feat(UI): Notizfeld") is not None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} Tests grün.")
    sys.exit(0)
