"""Gate für den py-cord-Fork (#152).

Der Fork ``mdopp/pycord`` hält einen fremden, unveröffentlichten Stand fest. Zwei Dinge
dürfen daran nicht still verrutschen: die Zeile in ``pyproject.toml`` muss auf den Fork
**und** den Commit zeigen, und der Wächter, der den Ausstieg meldet, muss urteilen können.
Geprüft wird hier ohne Netz — die Abfrage läuft wöchentlich in der CI, nicht im Testlauf.
"""

import importlib.util
import tomllib
import urllib.error
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "pruefe_pycord_ausstieg",
    WURZEL / "scripts" / "pruefe_pycord_ausstieg.py",
)
waechter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(waechter)

COMMIT = "326b72acc8d1d952ac002fe07ca65581cf5952bc"
FORK = f"git+https://github.com/mdopp/pycord@{COMMIT}"

OFFEN = {"state": "open", "merged": False}
GEMERGT = {"state": "closed", "merged": True, "merged_at": "2026-09-01T10:00:00Z"}
ALT = {"releases": {"2.8.1": [{"upload_time_iso_8601": "2026-02-01T00:00:00Z"}]}}
NEU = {
    "releases": {
        "2.8.1": [{"upload_time_iso_8601": "2026-02-01T00:00:00Z"}],
        "2.9.0": [{"upload_time_iso_8601": "2026-09-04T12:00:00Z"}],
    }
}


def _py_cord_zeilen():
    extras = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    return [zeile for name in ("discord", "dev") for zeile in extras[name] if "py-cord" in zeile]


def test_beide_extras_ziehen_py_cord_aus_dem_eigenen_fork():
    """Der Fremd-Branch kann gelöscht werden; dann stirbt der Image-Bau (#145)."""
    zeilen = _py_cord_zeilen()

    assert len(zeilen) == 2, zeilen
    assert all(FORK in zeile for zeile in zeilen), zeilen
    assert not any("Pycord-Development" in zeile for zeile in zeilen), zeilen


def test_die_zeile_traegt_herkunft_grund_und_ausstieg():
    """Ein Fork ohne diese Spur ist in drei Monaten ein Rätsel."""
    text = (WURZEL / "pyproject.toml").read_text(encoding="utf-8")

    for spur in ("#3159", "fix/voice-rec-2", "2026-07-22", "DAVE", "Ausstieg", "#145"):
        assert spur in text, spur


def test_der_waechter_liest_den_genagelten_commit():
    assert waechter.genagelter_commit((WURZEL / "pyproject.toml").read_text(encoding="utf-8")) == (
        COMMIT
    )


def test_offener_pr_heisst_fork_bleibt():
    darf_weg, satz = waechter.urteil(OFFEN, NEU)

    assert darf_weg is False
    assert "nicht gemergt" in satz


def test_gemergt_aber_unveroeffentlicht_heisst_fork_bleibt():
    """Gemergt allein reicht nicht — installieren lässt sich nur, was veröffentlicht ist."""
    darf_weg, satz = waechter.urteil(GEMERGT, ALT)

    assert darf_weg is False
    assert "noch nicht veröffentlicht" in satz


def test_gemergt_und_veroeffentlicht_heisst_fork_darf_weg():
    darf_weg, satz = waechter.urteil(GEMERGT, NEU)

    assert darf_weg is True
    assert "2.9.0" in satz


def test_der_lauf_schlaegt_erst_fehl_wenn_der_fork_weg_darf(capsys):
    assert waechter.main(hole=lambda url: OFFEN if "pulls" in url else NEU) == 0
    assert waechter.main(hole=lambda url: GEMERGT if "pulls" in url else NEU) == 1

    assert "Der Fork darf weg" in capsys.readouterr().out


def test_ein_netzhusten_schlaegt_keinen_alarm(capsys):
    """Ein Wächter, der bei jedem Ausfall Alarm schlägt, wird abgeschaltet."""

    def kaputt(url):
        raise urllib.error.URLError("kein Netz")

    assert waechter.main(hole=kaputt) == 0
    assert "keine Aussage" in capsys.readouterr().out
