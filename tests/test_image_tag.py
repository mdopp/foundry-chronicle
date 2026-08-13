"""Der abgeleitete Image-Tag für den Rollout.

Die Behauptung, die hier gehalten wird: der Tag zeigt auf einen Stand, den es in GHCR
wirklich gibt — also auf den jüngsten Commit, der den pfadgefilterten Bau ausgelöst hat,
und nicht blind auf ``HEAD``. Und ``--zurueck 1`` nennt den Stand davor, den Weg zurück.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "bestimme_image_tag", WURZEL / "scripts" / "bestimme_image_tag.py"
)
tagskript = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tagskript)


def _git(verzeichnis: Path, *argumente: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *argumente],
        cwd=verzeichnis,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ein Wegwerf-Repo mit gemischter Historie: gebaute und nicht gebaute Commits."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "src").mkdir()
    for datei, text in [
        ("src/eins.py", "a"),
        ("README.md", "b"),
        ("src/zwei.py", "c"),
        ("docs.md", "d"),
    ]:
        pfad = tmp_path / datei
        pfad.write_text(text, encoding="utf-8")
        _git(tmp_path, "add", datei)
        _git(tmp_path, "commit", "-q", "-m", f"chore: {datei}")
    monkeypatch.setattr(tagskript, "WURZEL", tmp_path)
    return tmp_path


def _sha(repo: Path, tiefe: int) -> str:
    ergebnis = subprocess.run(
        ["git", "log", "--format=%H", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return ergebnis.stdout.split()[tiefe]


def test_bildpfade_stehen_im_workflow() -> None:
    pfade = tagskript.bildpfade(tagskript.WORKFLOW.read_text(encoding="utf-8"))
    assert ":(glob)src/**" in pfade
    assert ":(glob)pyproject.toml" in pfade
    assert ":(glob)Dockerfile" in pfade


def test_der_tag_hat_die_form_aus_der_tag_matrix() -> None:
    # type=sha,prefix=sha-,format=short — sieben Zeichen, nicht die wachsende Kurzform
    # von git.
    assert tagskript.tag("0123456789abcdef0123456789abcdef01234567") == "sha-0123456"


def test_ein_nicht_gebauter_stand_wird_uebersprungen(repo: Path) -> None:
    # HEAD ist ein reiner Doku-Commit: der hat kein Image, also nennt das Skript den
    # jüngsten Commit darunter, der einen Bau ausgelöst hat.
    assert tagskript.main(["--ref", "main"]) == 0
    pfade = tagskript.bildpfade(tagskript.WORKFLOW.read_text(encoding="utf-8"))
    assert tagskript.gebaute_staende("main", pfade, 1) == [_sha(repo, 1)]


def test_zurueck_nennt_den_vorherigen_gebauten_stand(repo: Path, capsys) -> None:
    assert tagskript.main(["--ref", "main", "--zurueck", "1"]) == 0
    assert capsys.readouterr().out.strip() == tagskript.tag(_sha(repo, 3))


def test_voll_liefert_die_ganze_referenz(repo: Path, capsys) -> None:
    assert tagskript.main(["--ref", "main", "--voll"]) == 0
    assert capsys.readouterr().out.strip() == f"{tagskript.IMAGE}:{tagskript.tag(_sha(repo, 1))}"


def test_zu_weit_zurueck_meldet_sich_statt_zu_raten(repo: Path, capsys) -> None:
    assert tagskript.main(["--ref", "main", "--zurueck", "5"]) == 1
    assert "gebaute Stände" in capsys.readouterr().err


def test_die_registry_adresse_stimmt_mit_dem_template_ueberein() -> None:
    # Zwei Stellen, ein Image: sonst pinnt das Skript einen Tag, den das Manifest gar
    # nicht zieht.
    manifest = (WURZEL / "templates" / "daggerheart-chronik" / "template.yml").read_text(
        encoding="utf-8"
    )
    assert re.findall(r"^\s*image: (\S+):", manifest, re.MULTILINE) == [tagskript.IMAGE] * 2


def test_die_variable_verweist_auf_das_skript() -> None:
    # Der Assistent zeigt diese Beschreibung beim Ausrollen an; steht der Weg zum Tag
    # nicht dort, wird wieder 'latest' eingetragen.
    variablen = json.loads(
        (WURZEL / "templates" / "daggerheart-chronik" / "variables.json").read_text(
            encoding="utf-8"
        )
    )
    assert "bestimme_image_tag.py" in variablen["CHRONICLE_IMAGE_TAG"]["description"]
