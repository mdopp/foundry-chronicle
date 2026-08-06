import sqlite3

import pytest
from conftest import UNSER_KONTO

from chronicle import db
from chronicle.foundry import store
from chronicle.foundry.world import project

STAND = "2026-08-05T20:00:00+00:00"
SPAETER = "2026-08-12T20:00:00+00:00"


@pytest.fixture
def connection(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    verbindung = db.connect(pfad)
    yield verbindung
    verbindung.close()


def sitzung(connection, *, played_on="2026-08-05", title="Der Keller"):
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


def test_eine_sitzung_hat_szenen_und_eine_szene_hat_notizen(connection):
    sitzung_id = sitzung(connection)
    erste = szene(connection, sitzung_id, position=1, title="Aufbruch")
    zweite = szene(connection, sitzung_id, position=2, title="Der Keller")
    notiz(connection, erste, "Wir brechen bei Sonnenaufgang auf.")
    notiz(connection, zweite, "Die Wirtin warnt vor dem Keller.")

    zeilen = connection.execute(
        "SELECT scene.title AS szene, note.text AS text FROM note "
        "JOIN scene ON scene.id = note.scene_id "
        "WHERE scene.session_id = ? ORDER BY scene.position, note.id",
        (sitzung_id,),
    ).fetchall()
    assert [(z["szene"], z["text"]) for z in zeilen] == [
        ("Aufbruch", "Wir brechen bei Sonnenaufgang auf."),
        ("Der Keller", "Die Wirtin warnt vor dem Keller."),
    ]


def test_szenenreihenfolge_ist_je_sitzung_eindeutig(connection):
    sitzung_id = sitzung(connection)
    szene(connection, sitzung_id, position=1)
    with pytest.raises(sqlite3.IntegrityError):
        szene(connection, sitzung_id, position=1, title="Noch einmal die erste")


def test_dieselbe_position_in_einer_anderen_sitzung_ist_erlaubt(connection):
    erste = sitzung(connection, played_on="2026-08-05")
    zweite = sitzung(connection, played_on="2026-08-12")
    szene(connection, erste, position=1)
    szene(connection, zweite, position=1)


def test_notiz_ohne_szene_wird_abgewiesen(connection):
    with pytest.raises(sqlite3.IntegrityError):
        notiz(connection, 999, "Notiz ins Leere")


def test_loeschen_einer_sitzung_raeumt_szenen_notizen_und_protokolle_ab(connection):
    sitzung_id = sitzung(connection)
    szene_id = szene(connection, sitzung_id)
    notiz(connection, szene_id, "Wir brechen auf.")
    connection.execute(
        "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
        (sitzung_id, "chronik", "Es war einmal.", STAND),
    )
    connection.execute(
        "INSERT INTO scene_foundry_message (scene_id, message_id) VALUES (?, ?)",
        (szene_id, "m-wurf"),
    )

    connection.execute("DELETE FROM session WHERE id = ?", (sitzung_id,))

    for tabelle in ("scene", "note", "protocol", "scene_foundry_message"):
        assert connection.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0] == 0


def test_je_sitzung_gibt_es_chronik_und_rueckblick_genau_einmal(connection):
    sitzung_id = sitzung(connection)
    for art in ("chronik", "rueckblick"):
        connection.execute(
            "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
            (sitzung_id, art, "Text", STAND),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
            (sitzung_id, "chronik", "Zweite Chronik", SPAETER),
        )


def test_eine_unbekannte_protokollart_wird_abgewiesen(connection):
    sitzung_id = sitzung(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
            (sitzung_id, "zusammenfassung", "Text", STAND),
        )


def test_foundry_fakt_haengt_ueber_die_szene_an_der_notiz(connection, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(connection)
    szene_id = szene(connection, sitzung_id)
    notiz(connection, szene_id, "Brok prüft, was er über den Keller weiß.")
    connection.execute(
        "INSERT INTO scene_foundry_message (scene_id, message_id) VALUES (?, ?)",
        (szene_id, "m-wurf"),
    )

    zeile = connection.execute(
        "SELECT foundry_message.speaker_actor AS figur, foundry_message.roll_total AS summe "
        "FROM scene_foundry_message "
        "JOIN foundry_message ON foundry_message.id = scene_foundry_message.message_id "
        "WHERE scene_foundry_message.scene_id = ?",
        (szene_id,),
    ).fetchone()
    assert (zeile["figur"], zeile["summe"]) == ("a-brok", 7)


def test_zuordnung_ueberlebt_einen_neuen_foundry_abgleich(connection, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    szene_id = szene(connection, sitzung(connection))
    connection.execute(
        "INSERT INTO scene_foundry_message (scene_id, message_id) VALUES (?, ?)",
        (szene_id, "m-wurf"),
    )

    store.save(connection, project(welt, UNSER_KONTO, fetched_at=SPAETER))

    zeile = connection.execute(
        "SELECT foundry_message.roll_total AS summe FROM scene_foundry_message "
        "JOIN foundry_message ON foundry_message.id = scene_foundry_message.message_id "
        "WHERE scene_foundry_message.scene_id = ?",
        (szene_id,),
    ).fetchone()
    assert zeile["summe"] == 7


def test_ein_protokoll_aus_schema_11_bekommt_die_zustellspalte_nachgetragen(tmp_path):
    # ``CREATE TABLE IF NOT EXISTS`` erreicht eine bestehende Tabelle nicht — hier steht
    # deshalb die Fassung vor Schema 12, samt einem Rückblick, der noch nie zugestellt war.
    pfad = tmp_path / "alt.sqlite3"
    verbindung = db.connect(pfad)
    try:
        with verbindung:
            verbindung.execute(
                "CREATE TABLE protocol (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, "
                "kind TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, "
                "UNIQUE (session_id, kind))"
            )
            verbindung.execute(
                "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                (1, "rueckblick", "Die Runde tastete sich voran.", STAND),
            )
    finally:
        verbindung.close()

    db.init(pfad)

    verbindung = db.connect(pfad)
    try:
        zeile = verbindung.execute("SELECT text, delivered_at FROM protocol").fetchone()
    finally:
        verbindung.close()
    assert zeile["delivered_at"] is None
    assert zeile["text"] == "Die Runde tastete sich voran."
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_ein_zweiter_lauf_des_schemas_laesst_die_daten_stehen(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    verbindung = db.connect(pfad)
    sitzung_id = sitzung(verbindung, title="Der Krumme Ast")
    notiz(verbindung, szene(verbindung, sitzung_id), "Wir brechen auf.")
    verbindung.commit()
    verbindung.close()

    db.init(pfad)

    verbindung = db.connect(pfad)
    try:
        assert db.current_schema_version(pfad) == db.SCHEMA_VERSION
        assert verbindung.execute("SELECT title FROM session").fetchone()["title"] == (
            "Der Krumme Ast"
        )
        assert verbindung.execute("SELECT COUNT(*) FROM note").fetchone()[0] == 1
    finally:
        verbindung.close()
