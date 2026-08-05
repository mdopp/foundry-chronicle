import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_diff_coverage",
    Path(__file__).resolve().parent.parent / "scripts" / "check_diff_coverage.py",
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

DIFF = """diff --git a/src/chronicle/app.py b/src/chronicle/app.py
index 1111111..2222222 100644
--- a/src/chronicle/app.py
+++ b/src/chronicle/app.py
@@ -10,0 +11,2 @@ def create_app():
+    neu = 1
+    return neu
@@ -20 +21 @@ def status():
-    alt = 2
+    alt = 3
diff --git a/README.md b/README.md
index 3333333..4444444 100644
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-alt
+neu
"""

XML = """<?xml version="1.0" ?>
<coverage>
  <sources><source>src</source></sources>
  <packages><package><classes>
    <class filename="chronicle/app.py"><lines>
      <line number="11" hits="1"/>
      <line number="12" hits="0"/>
      <line number="21" hits="1"/>
    </lines></class>
  </classes></package></packages>
</coverage>
"""


def test_parse_diff_sammelt_hinzugefuegte_zeilennummern():
    assert check.parse_diff(DIFF) == {
        "src/chronicle/app.py": {11, 12, 21},
        "README.md": {1},
    }


def test_parse_diff_ignoriert_geloeschte_dateien():
    diff = "--- a/weg.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-weg = 1\n"
    assert check.parse_diff(diff) == {}


def test_parse_coverage_liest_zeilen_und_treffer():
    zeilen = {11: 1, 12: 0, 21: 1}
    assert check.parse_coverage(XML) == {
        "chronicle/app.py": zeilen,
        "src/chronicle/app.py": zeilen,
    }


def test_pfad_aus_dem_src_layout_wird_ueber_die_quellwurzel_zugeordnet():
    measured = check.parse_coverage(XML)
    assert check.measured_for("src/chronicle/app.py", measured) == {11: 1, 12: 0, 21: 1}
    assert check.measured_for("tests/test_app.py", measured) is None


def test_absolute_quellwurzel_wird_ueber_den_suffix_zugeordnet():
    measured = check.parse_coverage(
        XML.replace("<source>src</source>", "<source>/repo/src</source>")
    )
    assert check.measured_for("src/chronicle/app.py", measured) == {11: 1, 12: 0, 21: 1}


def test_zaehlt_nur_gemessene_python_zeilen():
    total, covered, misses = check.diff_coverage(check.parse_diff(DIFF), check.parse_coverage(XML))
    assert (total, covered) == (3, 2)
    assert misses == ["src/chronicle/app.py:12"]


def test_ungemessene_datei_bleibt_ausserhalb_des_nenners():
    changed = {"scripts/ohne_messung.py": {1, 2, 3}}
    assert check.diff_coverage(changed, check.parse_coverage(XML)) == (0, 0, [])


def test_volle_deckung_hat_keine_luecken():
    xml = XML.replace('number="12" hits="0"', 'number="12" hits="3"')
    total, covered, misses = check.diff_coverage(check.parse_diff(DIFF), check.parse_coverage(xml))
    assert (total, covered, misses) == (3, 3, [])


def test_main_ist_gruen_ohne_python_aenderung(monkeypatch):
    monkeypatch.setattr(check, "changed_lines", lambda base: {"README.md": {1}})
    assert check.main(["check_diff_coverage.py", "--base", "origin/main"]) == 0


def test_main_meldet_fehlende_coverage_datei(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "changed_lines", lambda base: {"src/chronicle/app.py": {11}})
    assert check.main(["x", "--xml", str(tmp_path / "fehlt.xml")]) == 2


def test_main_faellt_unter_der_untergrenze_durch(monkeypatch, tmp_path):
    xml_pfad = tmp_path / "coverage.xml"
    xml_pfad.write_text(XML, encoding="utf-8")
    monkeypatch.setattr(check, "changed_lines", lambda base: check.parse_diff(DIFF))
    assert check.main(["x", "--xml", str(xml_pfad)]) == 1
    assert check.main(["x", "--xml", str(xml_pfad), "--min", "60"]) == 0


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
