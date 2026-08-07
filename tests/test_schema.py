import sqlite3

import pytest
from conftest import UNSER_KONTO, runde

from chronicle import db
from chronicle.foundry import store
from chronicle.foundry.world import project

STAND = "2026-08-05T20:00:00+00:00"
SPAETER = "2026-08-12T20:00:00+00:00"


@pytest.fixture
def scope(tmp_path):
    zugang = db.scoped(runde(tmp_path / "chronicle.sqlite3"))
    yield zugang
    zugang.close()


def sitzung(scope, *, played_on="2026-08-05", title="Der Keller"):
    zeiger = scope.execute(
        "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
        (scope.runde_id, played_on, title, STAND),
    )
    return zeiger.lastrowid


def szene(scope, sitzung_id, *, position=1, title="Aufbruch"):
    zeiger = scope.execute(
        "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, sitzung_id, position, title, STAND),
    )
    return zeiger.lastrowid


def notiz(scope, szene_id, text):
    scope.execute(
        "INSERT INTO note (runde_id, scene_id, text, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, szene_id, text, STAND, STAND),
    )


def test_eine_sitzung_hat_szenen_und_eine_szene_hat_notizen(scope):
    sitzung_id = sitzung(scope)
    erste = szene(scope, sitzung_id, position=1, title="Aufbruch")
    zweite = szene(scope, sitzung_id, position=2, title="Der Keller")
    notiz(scope, erste, "Wir brechen bei Sonnenaufgang auf.")
    notiz(scope, zweite, "Die Wirtin warnt vor dem Keller.")

    zeilen = scope.execute(
        "SELECT scene.title AS szene, note.text AS text FROM note "
        "JOIN scene ON scene.id = note.scene_id "
        "WHERE scene.runde_id = ? AND scene.session_id = ? ORDER BY scene.position, note.id",
        (scope.runde_id, sitzung_id),
    ).fetchall()
    assert [(z["szene"], z["text"]) for z in zeilen] == [
        ("Aufbruch", "Wir brechen bei Sonnenaufgang auf."),
        ("Der Keller", "Die Wirtin warnt vor dem Keller."),
    ]


def test_szenenreihenfolge_ist_je_sitzung_eindeutig(scope):
    sitzung_id = sitzung(scope)
    szene(scope, sitzung_id, position=1)
    with pytest.raises(sqlite3.IntegrityError):
        szene(scope, sitzung_id, position=1, title="Noch einmal die erste")


def test_dieselbe_position_in_einer_anderen_sitzung_ist_erlaubt(scope):
    erste = sitzung(scope, played_on="2026-08-05")
    zweite = sitzung(scope, played_on="2026-08-12")
    szene(scope, erste, position=1)
    szene(scope, zweite, position=1)


def test_notiz_ohne_szene_wird_abgewiesen(scope):
    with pytest.raises(sqlite3.IntegrityError):
        notiz(scope, 999, "Notiz ins Leere")


def test_loeschen_einer_sitzung_raeumt_szenen_notizen_und_protokolle_ab(scope):
    sitzung_id = sitzung(scope)
    szene_id = szene(scope, sitzung_id)
    notiz(scope, szene_id, "Wir brechen auf.")
    scope.execute(
        "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, sitzung_id, "chronik", "Es war einmal.", STAND),
    )
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) VALUES (?, ?, ?)",
        (scope.runde_id, szene_id, "m-wurf"),
    )

    scope.execute("DELETE FROM session WHERE runde_id = ? AND id = ?", (scope.runde_id, sitzung_id))

    for tabelle in ("scene", "note", "protocol", "scene_foundry_message"):
        anzahl = scope.execute(
            f"SELECT COUNT(*) FROM {tabelle} WHERE runde_id = ?", (scope.runde_id,)
        ).fetchone()[0]
        assert anzahl == 0


def test_je_sitzung_gibt_es_chronik_und_rueckblick_genau_einmal(scope):
    sitzung_id = sitzung(scope)
    for art in ("chronik", "rueckblick"):
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.runde_id, sitzung_id, art, "Text", STAND),
        )
    with pytest.raises(sqlite3.IntegrityError):
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.runde_id, sitzung_id, "chronik", "Zweite Chronik", SPAETER),
        )


def test_eine_unbekannte_protokollart_wird_abgewiesen(scope):
    sitzung_id = sitzung(scope)
    with pytest.raises(sqlite3.IntegrityError):
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.runde_id, sitzung_id, "zusammenfassung", "Text", STAND),
        )


def test_foundry_fakt_haengt_ueber_die_szene_an_der_notiz(scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(scope)
    szene_id = szene(scope, sitzung_id)
    notiz(scope, szene_id, "Brok prüft, was er über den Keller weiß.")
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) VALUES (?, ?, ?)",
        (scope.runde_id, szene_id, "m-wurf"),
    )

    zeile = scope.execute(
        "SELECT foundry_message.speaker_actor AS figur, foundry_message.roll_total AS summe "
        "FROM scene_foundry_message "
        "JOIN foundry_message ON foundry_message.id = scene_foundry_message.message_id "
        "AND foundry_message.runde_id = scene_foundry_message.runde_id "
        "WHERE scene_foundry_message.runde_id = ? AND scene_foundry_message.scene_id = ?",
        (scope.runde_id, szene_id),
    ).fetchone()
    assert (zeile["figur"], zeile["summe"]) == ("a-brok", 7)


def test_zuordnung_ueberlebt_einen_neuen_foundry_abgleich(scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    szene_id = szene(scope, sitzung(scope))
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) VALUES (?, ?, ?)",
        (scope.runde_id, szene_id, "m-wurf"),
    )

    store.save(scope, project(welt, UNSER_KONTO, fetched_at=SPAETER))

    zeile = scope.execute(
        "SELECT foundry_message.roll_total AS summe FROM scene_foundry_message "
        "JOIN foundry_message ON foundry_message.id = scene_foundry_message.message_id "
        "AND foundry_message.runde_id = scene_foundry_message.runde_id "
        "WHERE scene_foundry_message.runde_id = ? AND scene_foundry_message.scene_id = ?",
        (scope.runde_id, szene_id),
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
    zugang = db.scoped(runde(pfad))
    with zugang:
        sitzung_id = sitzung(zugang, title="Der Krumme Ast")
        notiz(zugang, szene(zugang, sitzung_id), "Wir brechen auf.")
    zugang.close()

    db.init(pfad)

    zugang = db.scoped(runde(pfad))
    try:
        assert db.current_schema_version(pfad) == db.SCHEMA_VERSION
        titel = zugang.execute(
            "SELECT title FROM session WHERE runde_id = ?", (zugang.runde_id,)
        ).fetchone()["title"]
        assert titel == "Der Krumme Ast"
        anzahl = zugang.execute(
            "SELECT COUNT(*) FROM note WHERE runde_id = ?", (zugang.runde_id,)
        ).fetchone()[0]
        assert anzahl == 1
    finally:
        zugang.close()
