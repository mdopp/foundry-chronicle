"""Vom Speicher bis zum abgelegten Protokoll — der Stapellauf am Stück."""

import pytest
from conftest import UNSER_KONTO

import chronicle.compose.__main__ as entry
from chronicle import db
from chronicle.compose.composer import VERBINDUNG_TITEL
from chronicle.compose.service import KIND, RUECKBLICK, compose_session, recap_session
from chronicle.foundry import store
from chronicle.foundry.world import project

STAND = "2026-08-05T20:00:00+00:00"


class Modell:
    def __init__(
        self,
        antwort="Die Runde tastet sich voran.",
        faeden="- Die Wirtin wartet auf Antwort.",
        name="chronist-test",
    ):
        self.name = name
        self.antwort = antwort
        self.faeden = faeden
        self.prompts = []

    def write(self, *, system, prompt):
        self.prompts.append(prompt)
        return self.faeden if "Fäden" in system else self.antwort


@pytest.fixture
def connection(config):
    db.init(config.database_path)
    verbindung = db.connect(config.database_path)
    yield verbindung
    verbindung.close()


def sitzung(connection, *, title="Der Keller", played_on="2026-08-05"):
    zeiger = connection.execute(
        "INSERT INTO session (played_on, title, created_at) VALUES (?, ?, ?)",
        (played_on, title, STAND),
    )
    return zeiger.lastrowid


def szene(connection, sitzung_id, *, position=1, title="Aufbruch"):
    zeiger = connection.execute(
        "INSERT INTO scene (session_id, position, title, created_at) VALUES (?, ?, ?, ?)",
        (sitzung_id, position, title, STAND),
    )
    return zeiger.lastrowid


def notiz(connection, szene_id, text):
    connection.execute(
        "INSERT INTO note (scene_id, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (szene_id, text, STAND, STAND),
    )


def fakt(connection, szene_id, message_id="m-wurf"):
    connection.execute(
        "INSERT INTO scene_foundry_message (scene_id, message_id) VALUES (?, ?)",
        (szene_id, message_id),
    )


def protokolle(connection, sitzung_id):
    return connection.execute(
        "SELECT kind, text, created_at FROM protocol WHERE session_id = ?", (sitzung_id,)
    ).fetchall()


def eine_runde(connection, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(connection)
    erste = szene(connection, sitzung_id, position=1, title="Aufbruch")
    zweite = szene(connection, sitzung_id, position=2, title="Der Keller")
    notiz(connection, erste, "Wir brechen bei Sonnenaufgang auf.")
    notiz(connection, zweite, "Brok prüft, was er über den Keller weiß.")
    fakt(connection, zweite)
    connection.commit()
    return sitzung_id


def test_die_foundry_zahl_landet_unveraendert_im_protokoll(config, connection, welt):
    sitzung_id = eine_runde(connection, welt)

    ergebnis = compose_session(config, sitzung_id, model=Modell())

    assert ergebnis.fact_count == 1
    zeilen = protokolle(connection, sitzung_id)
    assert [z["kind"] for z in zeilen] == [KIND]
    text = zeilen[0]["text"]
    assert text == ergebnis.text
    assert "Knowledge Roll: Summe 7" in text
    assert "hope d12 = 3 · fear d12 = 1" in text
    assert "Wir brechen bei Sonnenaufgang auf." in text
    assert VERBINDUNG_TITEL in text


def test_ein_zweiter_lauf_ersetzt_die_chronik(config, connection, welt):
    sitzung_id = eine_runde(connection, welt)
    compose_session(config, sitzung_id, model=Modell("Erster Anlauf."))

    compose_session(config, sitzung_id, model=Modell("Zweiter Anlauf."))

    zeilen = protokolle(connection, sitzung_id)
    assert len(zeilen) == 1
    assert "Zweiter Anlauf." in zeilen[0]["text"]
    assert "Erster Anlauf." not in zeilen[0]["text"]


def test_ohne_sprachmodell_entsteht_die_chronik_trotzdem(config, connection, welt):
    sitzung_id = eine_runde(connection, welt)

    ergebnis = compose_session(config, sitzung_id)

    assert not config.ollama_configured
    assert ergebnis.reason is not None
    assert VERBINDUNG_TITEL not in ergebnis.text
    assert "Knowledge Roll: Summe 7" in ergebnis.text
    assert protokolle(connection, sitzung_id)[0]["text"] == ergebnis.text


def test_ohne_zuordnung_traegt_die_chronik_allein_die_notizen(config, connection, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(connection)
    notiz(connection, szene(connection, sitzung_id), "Wir reden mit der Wirtin.")
    connection.commit()

    ergebnis = compose_session(config, sitzung_id, model=Modell())

    assert ergebnis.fact_count == 0
    assert "Wir reden mit der Wirtin." in ergebnis.text
    assert "Belegt aus Foundry" not in ergebnis.text


def test_eine_unbekannte_sitzung_gibt_es_nicht(config, connection):
    assert compose_session(config, 999) is None


def test_jede_szene_bekommt_ihren_eigenen_aufruf(config, connection, welt):
    sitzung_id = eine_runde(connection, welt)
    modell = Modell()

    compose_session(config, sitzung_id, model=modell)

    assert len(modell.prompts) == 2
    assert "Belegte Fakten aus Foundry" in modell.prompts[1]
    assert "Belegte Fakten aus Foundry" not in modell.prompts[0]


def test_der_rueckblick_liegt_neben_der_chronik(config, connection, welt):
    sitzung_id = eine_runde(connection, welt)
    compose_session(config, sitzung_id, model=Modell())

    ergebnis = recap_session(config, sitzung_id, model=Modell())

    zeilen = protokolle(connection, sitzung_id)
    assert {z["kind"] for z in zeilen} == {KIND, RUECKBLICK}
    abgelegt = next(z["text"] for z in zeilen if z["kind"] == RUECKBLICK)
    assert abgelegt == ergebnis.text
    assert ergebnis.scene_count == 2
    # Die Zahl steht unverändert in der Chronik und wird von dort übernommen.
    assert "Knowledge Roll: Summe 7" in abgelegt


def test_ohne_chronik_gibt_es_keinen_rueckblick(config, connection, welt):
    sitzung_id = eine_runde(connection, welt)

    assert recap_session(config, sitzung_id, model=Modell()) is None
    assert protokolle(connection, sitzung_id) == []


def test_ein_zweiter_lauf_ersetzt_den_rueckblick(config, connection, welt):
    sitzung_id = eine_runde(connection, welt)
    compose_session(config, sitzung_id, model=Modell())
    recap_session(config, sitzung_id, model=Modell("Erster Anlauf."))

    recap_session(config, sitzung_id, model=Modell("Zweiter Anlauf."))

    zeilen = [z for z in protokolle(connection, sitzung_id) if z["kind"] == RUECKBLICK]
    assert len(zeilen) == 1
    assert "Zweiter Anlauf." in zeilen[0]["text"]
    assert "Erster Anlauf." not in zeilen[0]["text"]


def test_der_rueckblick_bekommt_die_vorigen_rueckblicke_mit(config, connection, welt):
    alt = eine_runde(connection, welt)
    compose_session(config, alt, model=Modell())
    recap_session(config, alt, model=Modell("Der Hafen lag hinter uns."))
    neu = sitzung(connection, title="Der Turm", played_on="2026-08-12")
    notiz(connection, szene(connection, neu), "Wir steigen zum Turm.")
    connection.commit()
    compose_session(config, neu, model=Modell())
    modell = Modell()

    recap_session(config, neu, model=modell)

    assert "Der Hafen lag hinter uns." in modell.prompts[0]
    assert "Wir steigen zum Turm." in modell.prompts[0]


def test_der_stapelaufruf_meldet_die_betriebsart(config, connection, welt, monkeypatch, capsys):
    sitzung_id = eine_runde(connection, welt)
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    assert entry.main([str(sitzung_id)]) == 1
    assert "geordnet, nicht formuliert" in capsys.readouterr().out


def test_der_stapelaufruf_schreibt_beides(config, connection, welt, monkeypatch, capsys):
    sitzung_id = eine_runde(connection, welt)
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    entry.main([str(sitzung_id)])

    ausgabe = capsys.readouterr().out
    assert "Chronik aus" in ausgabe
    assert "Rückblick aus" in ausgabe
    assert {z["kind"] for z in protokolle(connection, sitzung_id)} == {KIND, RUECKBLICK}


def test_der_stapelaufruf_weist_falsche_argumente_ab(config, monkeypatch, capsys):
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    assert entry.main([]) == 2
    assert entry.main(["keine-zahl"]) == 2
    assert entry.main(["999"]) == 2
    assert "gibt es nicht" in capsys.readouterr().out
