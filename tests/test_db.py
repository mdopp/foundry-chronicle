import sqlite3

import pytest

from chronicle import db, instanz, notes, protocol, register, settings
from chronicle import runde as runden

# Das Schema vor dem Runden-Modell, auf das Nötigste gekürzt: Sitzung, Szene, Notiz,
# Protokoll und die beiden Schlüsselräume, die getauscht wurden. Es steht hier als
# Zeichenkette und nicht als zweite Datei — es ist eine Momentaufnahme der Vergangenheit
# und darf sich nie wieder ändern.
SCHEMA_14 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE session (
    id INTEGER PRIMARY KEY, played_on TEXT NOT NULL, title TEXT, created_at TEXT NOT NULL);
CREATE TABLE scene (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    position INTEGER NOT NULL, title TEXT, created_at TEXT NOT NULL,
    UNIQUE (session_id, position));
CREATE TABLE note (
    id INTEGER PRIMARY KEY,
    scene_id INTEGER NOT NULL REFERENCES scene (id) ON DELETE CASCADE,
    text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE protocol (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('chronik', 'rueckblick')),
    text TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT,
    UNIQUE (session_id, kind));
CREATE TABLE register_entry (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('figur', 'ort', 'faden')),
    name TEXT NOT NULL, description TEXT NOT NULL, foundry_actor_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('vorschlag', 'bestaetigt')),
    suggested_at TEXT NOT NULL, confirmed_at TEXT, UNIQUE (kind, name));
CREATE TABLE register_mention (
    entry_id INTEGER NOT NULL REFERENCES register_entry (id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    scene_id INTEGER REFERENCES scene (id) ON DELETE CASCADE);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE search_index USING fts5 (
    text, kind UNINDEXED, ref_id UNINDEXED, session_id UNINDEXED, scene_id UNINDEXED);
CREATE TRIGGER note_search_insert AFTER INSERT ON note BEGIN
    INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
    VALUES (new.text, 'notiz', new.id,
            (SELECT session_id FROM scene WHERE id = new.scene_id), new.scene_id);
END;
INSERT INTO meta (key, value) VALUES ('schema_version', '14');
"""


@pytest.fixture
def alte_datenbank(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    connection = sqlite3.connect(pfad)
    connection.executescript(SCHEMA_14)
    connection.executescript(
        """
        INSERT INTO session (id, played_on, title, created_at)
        VALUES (1, '2026-01-01', 'Der Keller', '2026-01-01T18:00:00+00:00');
        INSERT INTO scene (id, session_id, position, title, created_at)
        VALUES (1, 1, 1, NULL, '2026-01-01T18:00:00+00:00');
        INSERT INTO note (id, scene_id, text, created_at, updated_at)
        VALUES (1, 1, 'Sie stiegen hinab.',
                '2026-01-01T18:05:00+00:00', '2026-01-01T18:05:00+00:00');
        INSERT INTO protocol (id, session_id, kind, text, created_at)
        VALUES (1, 1, 'chronik', 'Die Chronik von damals.', '2026-01-02T04:00:00+00:00');
        INSERT INTO register_entry
            (id, kind, name, description, state, suggested_at)
        VALUES (1, 'figur', 'Mira', 'Eine Kundschafterin.', 'bestaetigt',
                '2026-01-02T04:00:00+00:00');
        INSERT INTO register_mention (entry_id, session_id, scene_id) VALUES (1, 1, 1);
        INSERT INTO settings (key, value) VALUES ('foundry_url', 'https://foundry.example');
        INSERT INTO settings (key, value) VALUES ('discord_bot_token', 'platzhalter');
        INSERT INTO settings (key, value) VALUES ('admin_group', 'chronisten');
        INSERT INTO meta (key, value) VALUES ('discord_cursor', '4711');
        """
    )
    connection.commit()
    connection.close()
    return pfad


def test_init_legt_datei_und_schema_an(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    assert pfad.is_file()
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_init_legt_fehlende_verzeichnisse_an(tmp_path):
    pfad = tmp_path / "data" / "unterordner" / "chronicle.sqlite3"
    db.init(pfad)
    assert pfad.is_file()


def test_init_ist_idempotent(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    db.init(pfad)
    connection = db.connect(pfad)
    try:
        zeilen = connection.execute("SELECT key, value FROM meta").fetchall()
    finally:
        connection.close()
    assert [(zeile["key"], zeile["value"]) for zeile in zeilen] == [
        ("schema_version", str(db.SCHEMA_VERSION))
    ]


def test_connect_schaltet_wal_ein(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    connection = db.connect(pfad)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_connect_setzt_fremdschluessel_durch(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    connection = db.connect(pfad)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_wanderung_bringt_die_bestaende_in_die_erste_runde(alte_datenbank):
    db.init(alte_datenbank)
    runde = runden.erste(alte_datenbank)
    assert runde.name == db.ERSTE_RUNDE
    # Noch unbeansprucht: erst eine Gilde macht sie zu ihrer (#68).
    assert runde.guild_id is None
    assert [s.title for s in notes.sessions(runde)] == ["Der Keller"]
    assert notes.session(runde, 1).scenes[0].notes[0].text == "Sie stiegen hinab."
    assert protocol.stored(runde, 1).text == "Die Chronik von damals."
    assert [e.name for g in register.overview(runde) for e in g.entries] == ["Mira"]


def test_wanderung_sortiert_die_schluesselraeume(alte_datenbank):
    db.init(alte_datenbank)
    runde = runden.erste(alte_datenbank)
    # Der Runde gehört die Foundry-Adresse, der Instanz der Bot-Token und die Rolle.
    assert settings.stored(runde)["foundry_url"] == "https://foundry.example"
    assert instanz.stored(alte_datenbank) == {"discord_bot_token": "platzhalter"}
    assert instanz.admin_group(alte_datenbank) == "chronisten"
    connection = db.connect(alte_datenbank)
    try:
        gepflegt = {z["key"] for z in connection.execute("SELECT key FROM settings")}
        merk = {
            z["key"]: z["value"] for z in connection.execute("SELECT key, value FROM runde_meta")
        }
    finally:
        connection.close()
    assert gepflegt == {"foundry_url"}
    assert merk == {"discord_cursor": "4711"}


def test_wanderung_ist_idempotent(alte_datenbank):
    db.init(alte_datenbank)
    db.init(alte_datenbank)
    db.init(alte_datenbank)
    runde = runden.erste(alte_datenbank)
    assert len(runden.alle(alte_datenbank)) == 1
    assert len(notes.sessions(runde)) == 1
    assert db.current_schema_version(alte_datenbank) == db.SCHEMA_VERSION


def test_scope_laesst_eine_abfrage_mit_runde_durch(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    runde = runden.erste(pfad)
    scope = db.scoped(runde)
    try:
        assert (
            scope.execute("SELECT * FROM session WHERE runde_id = ?", (scope.runde_id,)).fetchall()
            == []
        )
        with pytest.raises(db.UngescopteAbfrage):
            scope.execute("SELECT COUNT(*) FROM note JOIN scene ON scene.id = note.scene_id")
        with pytest.raises(db.UngescopteAbfrage):
            scope.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", ())
    finally:
        scope.close()
