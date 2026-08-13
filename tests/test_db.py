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
        INSERT INTO settings (key, value)
        VALUES ('ollama_url', 'http://alt.example:11434');
        INSERT INTO settings (key, value) VALUES ('ollama_model', 'gemma4:12b');
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
    # Von Hand geschrieben, also ohne Herkunft — und damit nichts, was ein Lauf ersetzt.
    assert notes.drop_derived(runde, 1, "transkript") == 0
    assert notes.session(runde, 1).note_count == 1
    assert protocol.stored(runde, 1).text == "Die Chronik von damals."
    assert [e.name for g in register.overview(runde) for e in g.entries] == ["Mira"]


def test_wanderung_sortiert_die_schluesselraeume(alte_datenbank):
    db.init(alte_datenbank)
    runde = runden.erste(alte_datenbank)
    # Der Runde gehört die Foundry-Adresse, der Instanz Bot-Token, Ollama und die Rolle.
    assert settings.stored(runde)["foundry_url"] == "https://foundry.example"
    assert instanz.stored(alte_datenbank) == {
        "discord_bot_token": "platzhalter",
        "ollama_url": "http://alt.example:11434",
        "ollama_model": "gemma4:12b",
    }
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


def _kennungen_entfernen(pfad):
    """Der Stand vor der Zusicherung: die Spalte ist nullbar und keine Runde hat eine."""
    connection = sqlite3.connect(pfad)
    connection.executescript(
        """
        PRAGMA legacy_alter_table = ON;
        PRAGMA foreign_keys = OFF;
        ALTER TABLE runde RENAME TO runde__nullbar;
        CREATE TABLE runde (
            id           INTEGER PRIMARY KEY,
            name         TEXT NOT NULL,
            guild_id     TEXT UNIQUE,
            created_at   TEXT NOT NULL,
            token        TEXT,
            locked_at    TEXT,
            delete_after TEXT);
        INSERT INTO runde (id, name, guild_id, created_at, token, locked_at, delete_after)
        SELECT id, name, guild_id, created_at, NULL, locked_at, delete_after
        FROM runde__nullbar;
        DROP TABLE runde__nullbar;
        """
    )
    connection.commit()
    connection.close()


def test_die_wanderung_traegt_jeder_runde_eine_eigene_kennung_nach(tmp_path):
    """Nachgetragen wird je Runde und nicht einmal für alle — sonst gälten sie als gleich."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    andere = runden.anlegen(pfad, "Die Andere", guild_id="4242")
    _kennungen_entfernen(pfad)

    db.init(pfad)

    alle = runden.alle(pfad)
    assert len(alle) == 2
    assert all(runde.token for runde in alle)
    assert len({runde.token for runde in alle}) == 2
    assert runden.get(pfad, andere.id).name == "Die Andere"


def test_nach_der_wanderung_ist_die_kennung_pflicht(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    _kennungen_entfernen(pfad)
    db.init(pfad)

    connection = db.connect(pfad)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runde (name, guild_id, created_at) VALUES (?, ?, ?)",
                ("Ohne Kennung", "4243", "2026-08-11T12:00:00+00:00"),
            )
    finally:
        connection.close()


def test_die_wanderung_traegt_jeder_sitzung_eine_eigene_kennung_nach(tmp_path):
    """Ohne Kennung wäre eine Sitzung aus der Zeit davor nicht wiedererkennbar — und damit
    über ``/chronik sitzung-loeschen`` gar nicht zu löschen. Je Sitzung eine eigene, sonst
    gälten zwei als dieselbe."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    runde = runden.erste(pfad)
    erste = notes.create_session(runde, played_on="2026-05-01", title="Erster Abend")
    zweite = notes.create_session(runde, played_on="2026-05-08", title="Zweiter Abend")
    connection = db.connect(pfad)
    try:
        connection.execute("UPDATE session SET token = NULL")
        connection.commit()
    finally:
        connection.close()

    db.init(pfad)

    kennungen = {sitzung.token for sitzung in notes.sessions(runde)}
    assert len(kennungen) == 2
    assert all(kennungen)
    assert (
        notes.gemeinte_sitzung(runde, notes.sitzungsmarke(notes.session(runde, erste))) is not None
    )
    assert notes.session(runde, zweite).title == "Zweiter Abend"


def test_ohne_kennung_ist_eine_sitzung_nicht_die_gemeinte(tmp_path):
    """Zwei Sitzungen ohne Kennung dürfen nicht als dieselbe durchgehen — sonst fiele der
    Vergleich still auf die Nummer zurück, gegen die die Kennung gerade steht."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    runde = runden.erste(pfad)
    sitzung = notes.create_session(runde, played_on="2026-05-01", title="Erster Abend")
    connection = db.connect(pfad)
    try:
        connection.execute("UPDATE session SET token = NULL")
        connection.commit()
    finally:
        connection.close()

    assert notes.gemeinte_sitzung(runde, f"{sitzung}:") is None
    assert notes.gemeinte_sitzung(runde, str(sitzung)) is None


def test_die_leere_kennung_traegt_die_wache_allein(tmp_path):
    """``NULL`` allein nagelt die Wache nicht fest: ``NULL = ''`` ist in SQL nie wahr, der
    Vergleich fiele also auch ohne sie durch. Beim leeren String trägt sie allein."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    runde = runden.erste(pfad)
    sitzung = notes.create_session(runde, played_on="2026-05-01", title="Erster Abend")
    connection = db.connect(pfad)
    try:
        connection.execute("UPDATE session SET token = ''")
        connection.commit()
    finally:
        connection.close()

    assert notes.gemeinte_sitzung(runde, f"{sitzung}:") is None


def test_die_wanderung_holt_auch_die_leere_kennung_nach(tmp_path):
    """Sonst bliebe die Zeile für immer unlöschbar und meldete »schon fort«, obwohl sie steht."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    runde = runden.erste(pfad)
    notes.create_session(runde, played_on="2026-05-01", title="Erster Abend")
    connection = db.connect(pfad)
    try:
        connection.execute("UPDATE session SET token = ''")
        connection.commit()
    finally:
        connection.close()

    db.init(pfad)

    sitzung = notes.sessions(runde)[0]
    assert sitzung.token
    assert notes.gemeinte_sitzung(runde, notes.sitzungsmarke(sitzung)) is not None


def test_eine_gebastelte_marke_wird_abgewiesen_statt_zu_werfen(tmp_path):
    """``"²".isdigit()`` ist wahr, ``int("²")`` wirft — und den Wert setzt ein Discord-Client."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    runde = runden.erste(pfad)

    for marke in ("²:abc", "9999999999999999999999:abc", "٣:abc", "1²:abc"):
        assert notes.gemeinte_sitzung(runde, marke) is None


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
