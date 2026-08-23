"""Das Tor, das gefehlt hat: importiert der Dienst nur, was das Abbild auch installiert?

Am 2026-08-23 lief die Box in eine Absturzschleife (#259). ``chronicle.recordings``
importierte ``werkzeug``, und werkzeug war nie eine erklärte Abhängigkeit — es kam als
Anhängsel von Flask mit. Als Flask mit #231 ins ``dev``-Extra zog, ging werkzeug mit,
und die Importkette ``bot/__main__`` → ``gateway`` → ``nightly`` → ``jobs`` → ``kette``
→ ``recordings`` war tot. Der Bot erreichte Discord nie.

**Durch alle Tore kam es, weil jedes Tor im falschen Raum stand.** CI installiert
``.[dev]``, dort steht Flask, also stand auch werkzeug da; 1469 Tests liefen grün gegen
einen Abhängigkeitssatz, den es im Abbild nicht gibt. Der fehlende Import war das
Symptom, die fehlende Prüfung der Defekt.

Hier steht die billige Hälfte davon: rein statisch, ohne Netz, ohne Installation, läuft
in jedem Schnelldurchlauf mit. Sie liest, was ``src/chronicle`` tatsächlich importiert,
und hält es gegen das, was der Dockerfile installiert — ``.[discord]``, also
``[project].dependencies`` plus das Extra ``discord``. Die teure Hälfte steht in
``.github/workflows/ci.yml`` (Job »Laufzeit-Import«): dort wird der Satz wirklich
installiert und wirklich importiert. Diese hier sagt es in einer Zehntelsekunde und
nennt den Dateinamen; jene beweist es.

**Auch ein durchgereichtes Paket zählt als undeklariert.** Dass ``requests`` heute
``urllib3`` mitbringt, ist keine Zusage an uns — genau diese Annahme war der Fehler.
Wer etwas importiert, trägt es in ``[project].dependencies`` ein.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import sys
import tomllib
from importlib.metadata import packages_distributions

WURZEL = pathlib.Path(__file__).resolve().parent.parent
PAKET = WURZEL / "src" / "chronicle"
SKRIPT = WURZEL / "scripts" / "pruefe_laufzeit_importe.py"

# Was sonst noch ins Abbild kopiert wird und dort laufen muss (siehe Dockerfile).
MITGELIEFERT = (WURZEL / "scripts" / "verify_e2e.py",)


def _normal(name: str) -> str:
    """PEP 503: ``MarkupSafe``, ``markup_safe`` und ``markupsafe`` sind dasselbe Paket."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _laufzeit_pakete() -> set[str]:
    """Die Verteilungen, die ``pip install .[discord]`` erklärtermaßen holt."""
    daten = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    zeilen = list(daten["project"]["dependencies"])
    zeilen += daten["project"]["optional-dependencies"]["discord"]
    # Aus ``py-cord[voice] @ git+https://…`` und ``python-socketio[client]>=5.11`` je den
    # nackten Namen; alles ab dem ersten Trennzeichen gehört nicht mehr dazu.
    return {_normal(re.split(r"[\s\[<>=!~;@]", zeile, maxsplit=1)[0]) for zeile in zeilen}


def _importierte_wurzeln() -> dict[str, set[str]]:
    """Jedes fremde Wurzelmodul, das der Laufzeit-Code importiert, mit seinen Fundorten."""
    wurzeln: dict[str, set[str]] = {}
    for datei in sorted([*PAKET.rglob("*.py"), *MITGELIEFERT]):
        baum = ast.parse(datei.read_text(encoding="utf-8"), filename=str(datei))
        for knoten in ast.walk(baum):
            if isinstance(knoten, ast.Import):
                namen = [alias.name for alias in knoten.names]
            elif isinstance(knoten, ast.ImportFrom) and knoten.level == 0 and knoten.module:
                namen = [knoten.module]
            else:
                continue
            for name in namen:
                wurzel = name.split(".")[0]
                if wurzel in sys.stdlib_module_names or wurzel == "chronicle":
                    continue
                wurzeln.setdefault(wurzel, set()).add(str(datei.relative_to(WURZEL)))
    return wurzeln


def test_jeder_fremde_import_steht_in_den_laufzeit_abhaengigkeiten() -> None:
    erlaubt = _laufzeit_pakete()
    verteilungen = packages_distributions()

    fehler = []
    for wurzel, orte in sorted(_importierte_wurzeln().items()):
        traeger = {_normal(dist) for dist in verteilungen.get(wurzel, [])}
        if not traeger:
            fehler.append(f"{wurzel} (nicht installiert) — importiert in {sorted(orte)}")
        elif not traeger & erlaubt:
            fehler.append(
                f"{wurzel} kommt aus {sorted(traeger)}, und das steht nicht in "
                f"[project].dependencies oder im Extra discord — importiert in {sorted(orte)}"
            )

    assert not fehler, (
        "Laufzeit-Code importiert etwas, das das Abbild nicht installiert. "
        "Das Abbild baut mit `pip install .[discord]`; ein Paket, das nur im "
        "dev-Extra steht oder bloß durchgereicht wird, ist dort nicht da:\n  " + "\n  ".join(fehler)
    )


def test_das_tor_erkennt_ein_paket_aus_dem_dev_extra() -> None:
    """Die Probe aufs Exempel: das Tor darf nicht bloß deshalb grün sein, weil es nie
    etwas findet. ``flask`` steht im ``dev``-Extra und ist hier installiert — würde es
    ein Laufzeit-Modul importieren, müsste die Prüfung darauf anschlagen."""
    erlaubt = _laufzeit_pakete()
    assert "flask" not in erlaubt
    traeger = {_normal(dist) for dist in packages_distributions().get("flask", ["flask"])}
    assert not traeger & erlaubt


def test_die_erklaerten_laufzeit_pakete_sind_die_erwarteten() -> None:
    """Ein Wächter über dem Wächter: wer hier etwas hinzufügt, soll es bemerken."""
    assert _laufzeit_pakete() == {"markupsafe", "requests", "python-socketio", "py-cord", "davey"}


_spec = importlib.util.spec_from_file_location("pruefe_laufzeit_importe", SKRIPT)
pruefskript = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pruefskript)


def test_skript_importiert_das_ganze_paket(tmp_path, monkeypatch, capsys) -> None:
    """Die andere Hälfte des Tors — hier gegen die Quellen statt gegen eine Installation.

    In CI läuft dasselbe Skript gegen eine Umgebung mit ``pip install .[discord]``; dass
    es überhaupt jedes Modul lädt und den Einstieg findet, hält dieser Lauf fest.
    """
    monkeypatch.chdir(tmp_path)
    assert pruefskript.main([]) == 0
    assert "chronicle.bot.__main__ erreichbar" in capsys.readouterr().out


def test_skript_verweigert_die_arbeitskopie(monkeypatch, capsys) -> None:
    """Gegen ``src`` im eigenen Arbeitsverzeichnis prüft es nichts — und sagt das."""
    monkeypatch.chdir(WURZEL)
    assert pruefskript.main([]) == 1
    assert "Arbeitskopie" in capsys.readouterr().err


def test_skript_meldet_ein_fehlendes_paket(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert pruefskript.main(["--paket", "gibt_es_hier_nicht"]) == 1
    assert "gar nicht installiert" in capsys.readouterr().err


def test_skript_meldet_ein_modul_das_nicht_laedt(tmp_path, monkeypatch, capsys) -> None:
    """Der Vorfall in klein: ein Modul der Kette importiert etwas, das nicht da ist."""
    paket = tmp_path / "pfad" / "attrappe"
    (paket / "bot").mkdir(parents=True)
    (paket / "__init__.py").touch()
    (paket / "bot" / "__init__.py").touch()
    (paket / "bot" / "__main__.py").touch()
    (paket / "kette.py").write_text("import gibt_es_hier_nicht\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path / "pfad"))
    try:
        assert pruefskript.main(["--paket", "attrappe"]) == 1
        fehler = capsys.readouterr().err
        assert "attrappe.kette" in fehler
        assert "ModuleNotFoundError" in fehler
    finally:
        # Die Attrappe liegt in tmp_path und wäre nach diesem Test eine Leiche in
        # ``sys.modules``, die auf ein gelöschtes Verzeichnis zeigt.
        for name in [n for n in sys.modules if n.split(".")[0] == "attrappe"]:
            del sys.modules[name]
