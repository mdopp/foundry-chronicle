import importlib.util
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_release_reach",
    WURZEL / "scripts" / "check_release_reach.py",
)
check = importlib.util.module_from_spec(_spec)
# Vor exec_module eintragen: das Skript nutzt Dataclasses mit ``from __future__ import
# annotations``, und dataclasses schlägt seine eigenen Namen über sys.modules nach.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)


def commit(subject, pfade, rumpf="", sha="a1b2c3d4e5f6"):
    return check.Commit(sha=sha, subject=subject, rumpf=rumpf, pfade=list(pfade))


def verstoesse(*commits):
    gefunden, _ = check.pruefe(list(commits))
    return gefunden


def test_refactor_an_der_quelle_faellt_durch():
    gefunden = verstoesse(commit("refactor(compose): Ollama faellt", ["src/chronicle/x.py"]))
    assert len(gefunden) == 1
    assert gefunden[0].pfade == ["src/chronicle/x.py"]
    assert "refactor" in gefunden[0].grund


def test_dieselbe_aenderung_als_fix_ist_gruen():
    assert verstoesse(commit("fix(compose): der Ollama-Weg faellt", ["src/chronicle/x.py"])) == []


def test_chore_an_der_vorlage_faellt_durch():
    gefunden = verstoesse(commit("chore(template): Variable weg", ["templates/x/variables.json"]))
    assert len(gefunden) == 1


def test_docs_ausserhalb_der_auslieferung_ist_gruen():
    assert verstoesse(commit("docs: Hausregeln", ["CLAUDE.md", "docs/architektur.md"])) == []
    assert verstoesse(commit("docs: Lies-mich", ["README.md"])) == []


def test_test_an_den_tests_ist_gruen():
    assert verstoesse(commit("test(db): Wanderung", ["tests/test_db.py"])) == []


def test_gits_eigene_ruecknahme_form_faellt_hier_durch():
    """check_commit_subjects.py überspringt sie — hier nicht: sie trägt keine
    Conventional-Art und schneidet damit kein Release."""
    gefunden = verstoesse(commit('Revert "feat(x): y"', ["src/chronicle/x.py"]))
    assert len(gefunden) == 1
    assert "ohne Conventional-Art" in gefunden[0].grund


def test_merge_und_fixup_bleiben_uebersprungen():
    assert verstoesse(commit("Merge branch 'main'", ["src/chronicle/x.py"])) == []
    assert verstoesse(commit("fixup! feat(x): y", ["src/chronicle/x.py"])) == []


def test_notausstieg_mit_grund_ist_gruen():
    gefunden, ausstiege = check.pruefe(
        [
            commit(
                "refactor(db): Hilfsfunktion umbenannt",
                ["src/chronicle/db.py"],
                rumpf="Ohne-Auslieferung: reine Umbenennung, kein Verhalten geaendert\n",
            )
        ]
    )
    assert gefunden == []
    assert [grund for _, grund in ausstiege] == [
        "reine Umbenennung, kein Verhalten geaendert",
    ]


def test_notausstieg_ohne_grundtext_faellt_durch():
    gefunden = verstoesse(
        commit("refactor(db): umbenannt", ["src/chronicle/db.py"], rumpf="Ohne-Auslieferung:  \n")
    )
    assert len(gefunden) == 1
    assert "ohne Grundtext" in gefunden[0].grund


def test_bruchmarker_schneidet_auch_bei_einer_stillen_art():
    assert check.schneidet_release("refactor(db)!: Schema getauscht")
    assert check.schneidet_release("chore(db): Schema getauscht", "BREAKING CHANGE: weg\n")


def test_die_vier_stillen_arten_schneiden_kein_release():
    for art in check.NICHT_SCHNEIDENDE_ARTEN:
        assert not check.schneidet_release(f"{art}(x): etwas")
    for art in check.SCHNEIDENDE_ARTEN:
        assert check.schneidet_release(f"{art}(x): etwas")


def test_die_pfadliste_deckt_abbild_und_vorlage():
    for pfad in (
        "src",
        "src/chronicle/bot/__init__.py",
        "pyproject.toml",
        "Dockerfile",
        ".dockerignore",
        "scripts/verify_e2e.py",
        "templates/daggerheart-chronik/service.yaml",
    ):
        assert check.ist_auslieferungspfad(pfad), pfad
    for pfad in ("README.md", "tests/test_db.py", "docs/architektur.md", "scripts/queue.py"):
        assert not check.ist_auslieferungspfad(pfad), pfad


def test_jedes_copy_ziel_des_dockerfile_steht_in_der_pfadliste():
    """Drift-Wächter: ein neues COPY ohne Listeneintrag liefe unbemerkt ungeprüft mit."""
    quellen = []
    for zeile in (WURZEL / "Dockerfile").read_text(encoding="utf-8").splitlines():
        treffer = re.match(r"^COPY\s+(?!--from=)(?P<felder>.+)$", zeile.strip())
        if treffer:
            quellen.extend(treffer["felder"].split()[:-1])
    assert quellen
    for quelle in quellen:
        assert check.ist_auslieferungspfad(quelle) or quelle in check.NICHT_AUSGELIEFERT, quelle


def test_claude_md_nennt_denselben_ausstiegs_trailer():
    text = (WURZEL / "CLAUDE.md").read_text(encoding="utf-8").split("## Commits")[1]
    abschnitt = text.split("## Releases")[0]
    assert "check_release_reach.py" in abschnitt
    assert "Ohne-Auslieferung:" in abschnitt
    for art in check.NICHT_SCHNEIDENDE_ARTEN:
        assert f"`{art}`" in abschnitt


def test_ci_haengt_den_waechter_in_den_commits_job():
    """Der Job `commits` hängt nicht am Pfadfilter — eine Abbild-Änderung darf diesen
    Riegel nie überspringen."""
    text = (WURZEL / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    commits_job = text.split("  commits:")[1].split("\n  pfade:")[0]
    assert "scripts/check_release_reach.py" in commits_job
    assert "if: github.event_name == 'pull_request'" in commits_job


def test_main_druckt_den_ausstieg_auch_im_gruenen_lauf(monkeypatch, capsys):
    monkeypatch.setattr(
        check,
        "commits_im_bereich",
        lambda base: [
            commit(
                "refactor(db): umbenannt",
                ["src/chronicle/db.py"],
                rumpf="Ohne-Auslieferung: reine Umbenennung\n",
            )
        ],
    )
    assert check.main(["x", "--base", "origin/main"]) == 0
    ausgabe = capsys.readouterr().out
    assert "Ohne-Auslieferung: reine Umbenennung" in ausgabe
    assert "a1b2c3d" in ausgabe


def test_main_meldet_den_verstoss_mit_sha_subject_art_und_pfad(monkeypatch, capsys):
    monkeypatch.setattr(
        check,
        "commits_im_bereich",
        lambda base: [commit("refactor(compose): weg", ["src/chronicle/x.py", "docs/a.md"])],
    )
    assert check.main(["x"]) == 1
    fehler = capsys.readouterr().err
    assert "a1b2c3d" in fehler
    assert "refactor(compose): weg" in fehler
    assert "Art »refactor«" in fehler
    assert "src/chronicle/x.py" in fehler
    assert "docs/a.md" not in fehler
    assert "feat/fix/revert" in fehler


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and "monkeypatch" not in value.__code__.co_varnames
    ]
    for test in tests:
        test()
    print(f"{len(tests)} Tests grün.")
    sys.exit(0)
