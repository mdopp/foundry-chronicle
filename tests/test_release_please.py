"""Gates für die Release-Kette.

release-please leitet Version und Changelog aus Konfiguration und Historie ab. Beides
kippt still: eine auseinandergelaufene Version bumpt nur halb, ein Merge-Commit auf
`main` landet doppelt im Changelog (#70).
"""

import json
import re
import tomllib
from pathlib import Path

import yaml

WURZEL = Path(__file__).resolve().parent.parent


def _json(pfad):
    return json.loads((WURZEL / pfad).read_text(encoding="utf-8"))


def test_konfiguration_und_manifest_zeigen_auf_dasselbe_paket():
    konfiguration = _json("release-please-config.json")
    assert set(konfiguration["packages"]) == set(_json(".release-please-manifest.json"))


def test_version_steht_in_manifest_pyproject_und_paket_gleich():
    version = _json(".release-please-manifest.json")["."]
    pyproject = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == version

    paket = (WURZEL / "src" / "chronicle" / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{version}"' in paket
    assert "x-release-please-version" in paket


def test_extra_files_zeigen_auf_vorhandene_dateien():
    for datei in _json("release-please-config.json")["packages"]["."]["extra-files"]:
        assert (WURZEL / datei).is_file(), datei


def test_seal_schritt_haelt_main_linear():
    playbook = (
        WURZEL / ".claude" / "skills" / "autoloop-issues" / "stages" / "builder.md"
    ).read_text(encoding="utf-8")
    befehle = re.findall(r"gh pr merge [^\n`]*", playbook)
    assert befehle
    for befehl in befehle:
        assert "--rebase" in befehl, befehl


def _workflow(name):
    """PyYAML liest ``on:`` nach YAML 1.1 als Wahrheitswert — beide Schreibweisen holen."""
    daten = yaml.safe_load((WURZEL / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    daten["ausloeser"] = daten.get("on", daten.get(True))
    return daten


def test_release_bau_haengt_nicht_an_einem_tag_push():
    """#309: der ``paths``-Filter gilt für den ganzen ``push``-Block, also auch für Tags,
    und ein mit ``GITHUB_TOKEN`` geschnittener Tag löst ohnehin keinen Lauf aus. Ein
    ``tags:``-Eintrag hier wäre eine Zusage, die nie eingelöst wird."""
    ausloeser = _workflow("build-images.yml")["ausloeser"]
    assert "tags" not in ausloeser["push"]
    assert ausloeser["push"]["paths"] == ausloeser["pull_request"]["paths"]
    assert "release_tag" in ausloeser["workflow_call"]["inputs"]


def test_release_please_ruft_den_bau_mit_dem_geschnittenen_tag():
    jobs = _workflow("release-please.yml")["jobs"]
    bau = jobs["image"]
    assert bau["uses"] == "./.github/workflows/build-images.yml"
    assert bau["with"]["release_tag"] == "${{ needs.release-please.outputs.tag_name }}"
    assert "release_created" in bau["if"]
    assert bau["permissions"]["packages"] == "write"


def test_publish_haengt_weiter_an_gruenen_tests():
    assert _workflow("build-images.yml")["jobs"]["image"]["needs"] == "test"


def test_dokumentierte_tag_form_ist_die_erzeugte():
    """Erzeugt wird ``0.3.1``, nicht ``v0.3.1`` — und genau das muss dort stehen, wo
    jemand ``CHRONICLE_IMAGE_TAG`` einträgt."""
    schritte = _workflow("build-images.yml")["jobs"]["image"]["steps"]
    meta = next(s for s in schritte if s.get("id") == "meta")
    tags = meta["with"]["tags"]
    assert "type=semver,pattern={{version}},value=" in tags
    assert "pattern=v{{version}}" not in tags

    beschreibung = _json("templates/daggerheart-chronik/variables.json")["CHRONICLE_IMAGE_TAG"][
        "description"
    ]
    assert "ohne führendes 'v'" in beschreibung
    assert "'0.3.1'" in beschreibung
