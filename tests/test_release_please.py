"""Gates für die Release-Kette.

release-please leitet Version und Changelog aus Konfiguration und Historie ab. Beides
kippt still: eine auseinandergelaufene Version bumpt nur halb, ein Merge-Commit auf
`main` landet doppelt im Changelog (#70).
"""

import json
import re
import tomllib
from pathlib import Path

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
