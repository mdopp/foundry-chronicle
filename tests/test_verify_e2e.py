"""Der synthetische Durchstich, in der CI durchgespielt.

Das Skript ist der Prüfschritt der Verify-Stufe; wenn es erst auf der Box auffällt,
dass es nicht läuft, ist es als Prüfschritt wertlos. Deshalb läuft es hier bei jedem
Pull-Request einmal ganz durch — gegen eine Wegwerf-Instanz wie auf der Box.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

from chronicle import settings

WURZEL = Path(__file__).resolve().parents[1]

SKRIPT = WURZEL / "scripts" / "verify_e2e.py"

_spec = importlib.util.spec_from_file_location("verify_e2e", SKRIPT)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

# Der Pfad, unter dem stages/verify.md das Skript im Container aufruft.
IM_IMAGE = "/app/scripts/verify_e2e.py"


def test_der_durchstich_geht_ganz_durch():
    assert verify.main([]) == 0


def test_der_durchstich_laeuft_auch_als_eigenes_skript():
    ergebnis = subprocess.run(
        [sys.executable, str(SKRIPT)], capture_output=True, text=True, timeout=600
    )
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    assert verify.BESTANDEN in ergebnis.stdout


def test_ein_gescheiterter_schritt_faellt_auf_und_raeumt_trotzdem_auf(monkeypatch, capsys):
    angelegt = []
    echtes_mkdtemp = verify.tempfile.mkdtemp

    def mitschreiben(*args, **kwargs):
        pfad = echtes_mkdtemp(*args, **kwargs)
        angelegt.append(Path(pfad))
        return pfad

    def scheitern(*_args):
        raise verify.Fehlschlag("kaputt")

    monkeypatch.setattr(verify.tempfile, "mkdtemp", mitschreiben)
    monkeypatch.setattr(verify, "warten", scheitern)

    assert verify.main([]) == 1

    ausgabe = capsys.readouterr().out
    assert "kaputt" in ausgabe
    assert verify.GESCHEITERT in ausgabe
    assert angelegt and not angelegt[0].exists()


def test_argumente_gibt_es_keine(capsys):
    assert verify.main(["--schnell"]) == 2
    assert "ohne Argumente" in capsys.readouterr().out


def test_das_skript_kommt_mit_der_standardbibliothek_aus():
    """Im Image liegt kein pytest und keine Testabhängigkeit — nur Python und das Paket."""
    baum = ast.parse(SKRIPT.read_text(encoding="utf-8"))
    module = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            module.update(name.name.split(".")[0] for name in knoten.names)
        elif isinstance(knoten, ast.ImportFrom) and knoten.level == 0 and knoten.module:
            module.add(knoten.module.split(".")[0])
    assert module <= set(sys.stdlib_module_names)


def test_der_durchstich_prueft_die_vorgabezone_der_einstellungen():
    """Zwei Orte, eine Zone: das Skript darf ``chronicle`` nicht importieren."""
    assert verify.STANDARDZONE == settings.DEFAULT_NIGHTLY_ZONE


def test_das_image_bringt_das_skript_mit():
    dockerfile = (WURZEL / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (WURZEL / ".dockerignore").read_text(encoding="utf-8")
    assert IM_IMAGE in dockerfile
    assert "!scripts/verify_e2e.py" in dockerignore
