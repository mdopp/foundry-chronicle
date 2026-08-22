"""Einen Abend mitschreiben und ohne Foundry noch einmal durchlaufen lassen (#242).

Warum es diese Datei gibt: #242 überlebte, weil die Fixtures von Hand geschrieben waren.
Sie trugen ``system.roll``, der echte Server nicht — und die Tests bestätigten damit
genau die Annahme, an der der Adapter scheiterte. Ein Mitschnitt dreht das um: aufgehoben
wird die **rohe** Antwort, und was hinterher geprüft wird, läuft gegen sie statt gegen
eine Nachbildung.

Die Welt hier ist trotzdem eine erfundene — ein echter Abzug ist personenbezogen und
gehört nie ins Repo. Was an ihr echt ist, ist die **Ablage**: der Wurf steht nur in
``rolls[]``, so wie ihn ein Server ohne ``system.roll`` schickt. Sobald ein
anonymisierter Mitschnitt von der Box vorliegt, tritt er hier an ihre Stelle, ohne dass
sich eine Zeile Code ändert — ``Wiedergabe`` liest ihn genauso.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import PASSWORT, UNSER_KONTO, WELT, runde

import chronicle.foundry.__main__ as batch
from chronicle import db, notes
from chronicle.compose import service as compose_service
from chronicle.foundry import mitschnitt, service

# Vor der Epoche liegt nichts; die Nachrichten der Testwelt stehen bei 1000..5000 ms.
SZENENBEGINN = "1970-01-01T00:00:00+00:00"


def in_rolls(welt: dict) -> dict:
    """Dieselbe Welt, den Wurf aber dort, wo ein echter Server ihn ablegt.

    Verschoben, nicht dazuerfunden: der aufbereitete Block wird so serialisiert, wie
    Foundry es tut — ``rolls`` ist eine Liste von JSON-**Strings**, und der Block steht
    darin noch einmal unter ``options.roll`` (#146). Danach trägt keine Nachricht mehr
    einen ``system.roll``; das ist der Zustand, den #242 auf der Box gemessen hat.
    """
    nachrichten = []
    for nachricht in welt["messages"]:
        block = (nachricht.get("system") or {}).get("roll")
        if block is None:
            nachrichten.append(nachricht)
            continue
        roh = {feld: wert for feld, wert in nachricht.items() if feld != "system"}
        roh["rolls"] = [
            json.dumps(
                {
                    "class": "DualityRoll",
                    "formula": block.get("formula"),
                    "total": block.get("total"),
                    "evaluated": True,
                    "options": {"title": block.get("title"), "roll": block},
                },
                ensure_ascii=False,
            )
        ]
        nachrichten.append(roh)
    return dict(welt, messages=nachrichten)


class Server:
    """Ein Foundry, das genau diese Welt zeigt — dieselbe Oberfläche wie der Client."""

    def __init__(self, welt):
        self._welt = welt
        self.aufrufe = 0

    def fetch_world(self):
        self.aufrufe += 1
        return UNSER_KONTO, self._welt


@pytest.fixture
def welt_in_rolls():
    return in_rolls(WELT)


@pytest.fixture
def mitschreibend(config, tmp_path, monkeypatch):
    """Eine Instanz, die mitschreibt — der Ordner liegt relativ zum Arbeitsverzeichnis."""
    monkeypatch.chdir(tmp_path)
    return replace(config, foundry_mitschnitt=True)


def _wurfzeilen(config):
    scope = db.scoped(runde(config))
    try:
        return scope.execute(
            "SELECT id, roll_title, roll_total, roll_formula FROM foundry_message "
            "WHERE runde_id = ? AND roll_total IS NOT NULL ORDER BY timestamp",
            (scope.runde_id,),
        ).fetchall()
    finally:
        scope.close()


def _sitzung_mit_szene(config):
    scope = db.scoped(runde(config))
    try:
        with scope:
            sitzung = int(
                scope.execute(
                    "INSERT INTO session (runde_id, played_on, title, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (scope.runde_id, "1970-01-01", "Der Aufbruch", SZENENBEGINN),
                ).lastrowid
            )
            scope.execute(
                "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope.runde_id, sitzung, 1, "Der Aufbruch", SZENENBEGINN),
            )
    finally:
        scope.close()
    return sitzung


# -- Der Fehler selbst ------------------------------------------------------------------


def test_ein_wurf_aus_rolls_landet_im_archiv(config, welt_in_rolls):
    """#242: 76 Nachrichten, kein einziger Wurf. Ohne den zweiten Einstieg bleibt alles NULL."""
    service.sync(config, runde(config), client=Server(welt_in_rolls), passwort=PASSWORT)
    zeilen = _wurfzeilen(config)
    assert [zeile["id"] for zeile in zeilen] == ["m-wurf"]
    assert zeilen[0]["roll_title"] == "Knowledge Roll"
    assert zeilen[0]["roll_total"] == 7
    assert zeilen[0]["roll_formula"] == "1d12 + 1d12 + 3"


def test_der_wurf_aus_rolls_wird_in_der_chronik_zur_tatsache(config, welt_in_rolls):
    """``scene_foundry_message`` war leer — und nur was dort steht, trägt die Chronik."""
    sitzung = _sitzung_mit_szene(config)
    zustand = service.sync(
        config,
        runde(config),
        client=Server(welt_in_rolls),
        passwort=PASSWORT,
        session_id=sitzung,
    )
    assert zustand.nachgetragen == 1
    scope = db.scoped(runde(config))
    try:
        stoff = compose_service.material(scope, sitzung)
    finally:
        scope.close()
    assert [fakt.id for fakt in stoff.scenes[0].facts] == ["m-wurf"]


def test_der_strom_stellt_einen_wurf_aus_rolls_ein(config, welt_in_rolls):
    from chronicle import zugang

    zugang.merken(runde(config), PASSWORT)
    ereignisse = service.beobachten(config, runde(config), client=Server(welt_in_rolls))
    assert [n.id for n in ereignisse.neu] == ["m-wurf"]


# -- Der Mitschnitt ---------------------------------------------------------------------


def test_der_lauf_schreibt_das_rohmaterial_mit(mitschreibend, welt_in_rolls):
    """Aufgehoben wird die rohe Antwort, nicht das Destillat.

    Sonst prüfte sich der Adapter an seiner eigenen Annahme — der Fehler aus #242.
    """
    service.sync(
        mitschreibend, runde(mitschreibend), client=Server(welt_in_rolls), passwort=PASSWORT
    )
    bilder = mitschnitt.lies(mitschnitt.ziel(service.ABZUG_ORDNER, runde(mitschreibend).id))
    assert len(bilder) == 1
    assert bilder[0].user_id == UNSER_KONTO
    assert bilder[0].welt["messages"] == welt_in_rolls["messages"]


def test_jeder_blick_haengt_ein_bild_an(mitschreibend, welt_in_rolls):
    """Ein Abend ist eine Folge von Blicken — der Strom sieht immer wieder nach."""
    from chronicle import zugang

    eine = runde(mitschreibend)
    zugang.merken(eine, PASSWORT)
    service.beobachten(mitschreibend, eine, client=Server(welt_in_rolls))
    service.beobachten(mitschreibend, eine, client=Server(welt_in_rolls))
    service.sync(mitschreibend, eine, client=Server(welt_in_rolls), passwort=PASSWORT)
    datei = mitschnitt.ziel(service.ABZUG_ORDNER, eine.id)
    assert len(mitschnitt.lies(datei)) == 3


def test_zwei_runden_schreiben_nicht_ineinander(mitschreibend, welt_in_rolls):
    """Die Trennung zwischen Runden gilt auch für das Rohmaterial — sonst wäre sie ein Leck."""
    from chronicle import runde as runden

    erste = runde(mitschreibend)
    zweite = runden.anlegen(mitschreibend.database_path, "Die Zweiten")
    service.sync(mitschreibend, erste, client=Server(welt_in_rolls), passwort=PASSWORT)
    service.sync(mitschreibend, zweite, client=Server(welt_in_rolls), passwort=PASSWORT)
    for eine in (erste, zweite):
        assert len(mitschnitt.lies(mitschnitt.ziel(service.ABZUG_ORDNER, eine.id))) == 1


def test_ohne_schalter_wird_nichts_mitgeschrieben(config, welt_in_rolls, tmp_path, monkeypatch):
    """Personenbezogenes fängt nicht von selbst an, sich anzusammeln."""
    monkeypatch.chdir(tmp_path)
    service.sync(config, runde(config), client=Server(welt_in_rolls), passwort=PASSWORT)
    assert not service.ABZUG_ORDNER.exists()


def test_der_mitschnitt_liegt_so_geschuetzt_wie_der_abzug(mitschreibend, welt_in_rolls):
    service.sync(
        mitschreibend, runde(mitschreibend), client=Server(welt_in_rolls), passwort=PASSWORT
    )
    datei = mitschnitt.ziel(service.ABZUG_ORDNER, runde(mitschreibend).id)
    assert datei.stat().st_mode & 0o777 == 0o600
    assert datei.parent.stat().st_mode & 0o777 == 0o700


def test_kein_inhalt_des_mitschnitts_in_einer_logzeile(mitschreibend, welt_in_rolls, caplog):
    caplog.set_level("INFO")
    service.sync(
        mitschreibend, runde(mitschreibend), client=Server(welt_in_rolls), passwort=PASSWORT
    )
    protokoll = "\n".join(caplog.messages)
    assert "Mitschnitt" in protokoll
    for spur in ("Brok Eisenfaust", "Knowledge Roll", "1d12", PASSWORT):
        assert spur not in protokoll


# -- Die Wiedergabe: derselbe Abend, kein Server ------------------------------------------


def test_ein_mitgeschriebener_abend_laeuft_ohne_foundry_noch_einmal(
    mitschreibend, welt_in_rolls, tmp_path
):
    """Die Bedingung aus #242: aus dem Rohmaterial wird ein Server, gegen den es etwas beweist."""
    service.sync(
        mitschreibend, runde(mitschreibend), client=Server(welt_in_rolls), passwort=PASSWORT
    )
    datei = mitschnitt.ziel(service.ABZUG_ORDNER, runde(mitschreibend).id)

    nachher = replace(mitschreibend, data_dir=tmp_path / "nachher", foundry_mitschnitt=False)
    zweite = runde(nachher)
    sitzung = _sitzung_mit_szene(nachher)
    zustand = service.sync(
        nachher, zweite, client=mitschnitt.Wiedergabe.aus_datei(datei), session_id=sitzung
    )

    assert not zustand.stale
    assert zustand.nachgetragen == 1
    assert [zeile["roll_total"] for zeile in _wurfzeilen(nachher)] == [7]
    assert notes.verknuepfte_foundry_ereignisse(zweite) == {"m-wurf"}


def test_die_wiedergabe_gibt_die_bilder_der_reihe_nach(tmp_path):
    datei = tmp_path / "abend.jsonl"
    mitschnitt.schreibe(datei, "u-1", {"messages": [{"_id": "eins"}]})
    mitschnitt.schreibe(datei, "u-1", {"messages": [{"_id": "zwei"}]})
    gespielt = mitschnitt.Wiedergabe.aus_datei(datei)
    assert len(gespielt) == 2
    assert [gespielt.fetch_world()[1]["messages"][0]["_id"] for _ in range(2)] == ["eins", "zwei"]


def test_nach_dem_letzten_bild_bleibt_das_letzte_stehen(tmp_path):
    """Ein Abgleich fragt öfter, als der Strom mitschrieb — das sagt nichts über die Anbindung."""
    datei = tmp_path / "abend.jsonl"
    mitschnitt.schreibe(datei, "u-1", {"messages": []})
    gespielt = mitschnitt.Wiedergabe.aus_datei(datei)
    assert gespielt.fetch_world() == gespielt.fetch_world()


def test_ein_mitschnitt_ohne_bild_ist_kein_server(tmp_path):
    datei = tmp_path / "leer.jsonl"
    datei.write_text("\n\nkein json\n", encoding="utf-8")
    with pytest.raises(mitschnitt.Leer):
        mitschnitt.Wiedergabe.aus_datei(datei)


def test_der_stapellauf_spielt_einen_mitschnitt_ab(
    mitschreibend, welt_in_rolls, monkeypatch, capsys
):
    """Ohne Netz und ohne Passwort — sonst wäre »nachspielen« nur ein zweiter Abgleich."""
    service.sync(
        mitschreibend, runde(mitschreibend), client=Server(welt_in_rolls), passwort=PASSWORT
    )
    datei = mitschnitt.ziel(service.ABZUG_ORDNER, runde(mitschreibend).id)

    ohne_mitschnitt = replace(mitschreibend, foundry_mitschnitt=False)
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: ohne_mitschnitt))
    monkeypatch.setattr(
        batch, "getpass", lambda frage: pytest.fail("eine Wiedergabe fragt nach keinem Passwort")
    )
    monkeypatch.setattr(
        service, "FoundryClient", lambda *_: pytest.fail("eine Wiedergabe geht zu keinem Server")
    )

    assert batch.main(["--nachspielen", str(datei)]) == 0
    assert "1 Bildern" in capsys.readouterr().out
    assert [zeile["roll_total"] for zeile in _wurfzeilen(mitschreibend)] == [7]


def test_ein_fehlender_mitschnitt_meldet_das(config, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))
    assert batch.main(["--nachspielen", str(tmp_path / "gibt-es-nicht.jsonl")]) == 1
    assert "Kein Mitschnitt" in capsys.readouterr().err
