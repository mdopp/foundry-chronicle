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
import zoneinfo
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


def test_das_skript_kommt_ohne_testabhaengigkeit_aus():
    """Im Image liegt kein pytest und keine Testabhängigkeit — nur Python und das Paket.

    Seit #158 fährt der Durchstich über die Bot-Befehle statt über HTTP-Routen, also
    **muss** er ``chronicle`` importieren. Die Zusage ist damit nicht mehr »nur
    Standardbibliothek«, sondern »Standardbibliothek und das Paket« — was im Image liegt.
    Alles darüber hinaus wäre eine Abhängigkeit, die dort fehlt, und der Lauf stürbe am
    Import statt an einem Schritt.
    """
    baum = ast.parse(SKRIPT.read_text(encoding="utf-8"))
    module = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            module.update(name.name.split(".")[0] for name in knoten.names)
        elif isinstance(knoten, ast.ImportFrom) and knoten.level == 0 and knoten.module:
            module.add(knoten.module.split(".")[0])
    assert module <= set(sys.stdlib_module_names) | {"chronicle"}


def test_der_durchstich_prueft_die_vorgabezone_der_einstellungen():
    """Eine Zone, ein Ort: seit #158 gelesen statt abgeschrieben.

    Die frühere Fassung verglich eine Kopie im Skript mit der Vorgabe — nötig, solange
    das Skript ``chronicle`` nicht importieren durfte. Jetzt darf es, also gibt es keine
    Kopie mehr, die auseinanderlaufen könnte.
    """
    assert not hasattr(verify, "STANDARDZONE")
    assert "settings.DEFAULT_NIGHTLY_ZONE" in SKRIPT.read_text(encoding="utf-8")
    zoneinfo.ZoneInfo(settings.DEFAULT_NIGHTLY_ZONE)


def test_der_durchstich_faehrt_ueber_die_bot_befehle_und_nicht_ueber_routen():
    """Die Routen, über die er lief, gibt es mit #157 nicht mehr (#158).

    Ohne diesen Nachzug prüfte ``/verify`` auf der Box nur noch ``/healthz``.
    """
    quelle = SKRIPT.read_text(encoding="utf-8")
    for fort in ("/sitzungen/", "/szenen/", "/protokolle", "/suche?", "/einrichtung"):
        assert fort not in quelle, fort
    assert "chronik.aufnehmen" in quelle
    assert "chronik.abschluss_starten" in quelle
    assert "erinnern.suche" in quelle


def test_das_image_bringt_das_skript_mit():
    dockerfile = (WURZEL / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (WURZEL / ".dockerignore").read_text(encoding="utf-8")
    assert IM_IMAGE in dockerfile
    assert "!scripts/verify_e2e.py" in dockerignore
