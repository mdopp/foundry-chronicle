import sqlite3

import pytest

from chronicle import db, notes, protocol, register, settings
from chronicle import runde as runden
from chronicle import sprache as sprachen

# Die Werte der Instanz kommen seit #230 aus der Umgebung. Die Wanderung räumt sie nur
# fort, wenn ihr Ersatz wirklich dasteht — die Tests sagen deshalb ausdrücklich, was in
# der Umgebung steht, statt sich auf die des Entwicklers zu verlassen.
ERSATZ = {
    "DISCORD_BOT_TOKEN": "token-nur-in-diesem-test",
    "OLLAMA_URL": "http://neu.example:11434",
    "OLLAMA_MODEL": "neues-modell",
}


def _meta(pfad):
    connection = db.connect(pfad)
    try:
        return {z["key"]: z["value"] for z in connection.execute("SELECT key, value FROM meta")}
    finally:
        connection.close()


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
        INSERT INTO meta (key, value) VALUES ('foundry_last_error', 'Foundry war aus.');
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
    db.init(alte_datenbank, umgebung={})
    runde = runden.erste(alte_datenbank)
    # Der Runde gehört die Foundry-Adresse, der Instanz die Rolle — und die drei Werte,
    # die seit #230 aus der Umgebung kommen, liegen hier noch, weil kein Ersatz dasteht.
    assert settings.stored(runde)["foundry_url"] == "https://foundry.example"
    assert _meta(alte_datenbank) | {} == {
        "schema_version": str(db.SCHEMA_VERSION),
        "discord_bot_token": "platzhalter",
        "ollama_url": "http://alt.example:11434",
        "ollama_model": "gemma4:12b",
    }
    connection = db.connect(alte_datenbank)
    try:
        gepflegt = {z["key"] for z in connection.execute("SELECT key FROM settings")}
        merk = {
            z["key"]: z["value"] for z in connection.execute("SELECT key, value FROM runde_meta")
        }
    finally:
        connection.close()
    # Seit #268 steht die Inhaltssprache daneben, und die Wanderung hat sie **hingeschrieben**:
    # was es vor #268 gab, wurde deutsch bedient. Ohne diese Zeile hieße der leere Eintrag ab
    # sofort Englisch, und der nächsten Sitzung liefe eine englische Einwilligungs-Ansage.
    assert gepflegt == {"foundry_url", db.SPRACHE_KEY}
    assert settings.sprache(runde) == sprachen.DEUTSCH
    # Der Zeiger des Briefkastens wandert mit — und fällt gleich danach weg, weil er auf
    # eine fremde Gilde gezeigt haben kann (#192). Ein eigener Test hält das fest.
    assert merk == {"foundry_last_error": "Foundry war aus."}


def test_wanderung_ist_idempotent(alte_datenbank):
    db.init(alte_datenbank, umgebung=ERSATZ)
    db.init(alte_datenbank, umgebung=ERSATZ)
    db.init(alte_datenbank, umgebung=ERSATZ)
    runde = runden.erste(alte_datenbank)
    assert len(runden.alle(alte_datenbank)) == 1
    assert len(notes.sessions(runde)) == 1
    assert db.current_schema_version(alte_datenbank) == db.SCHEMA_VERSION


def _zeiger_setzen(runde, wert):
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "INSERT INTO runde_meta (runde_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (runde_id, key) DO UPDATE SET value = excluded.value",
                (scope.runde_id, db.BRIEFKASTEN_ZEIGER, wert),
            )
    finally:
        scope.close()


def _zeiger(pfad):
    connection = db.connect(pfad)
    try:
        return {
            zeile["runde_id"]: zeile["value"]
            for zeile in connection.execute(
                "SELECT runde_id, value FROM runde_meta WHERE key = ?", (db.BRIEFKASTEN_ZEIGER,)
            )
        }
    finally:
        connection.close()


def _stand_setzen(pfad, stand):
    """Der Schema-Stand einer laufenden Instanz von vorher — die Wanderung liest ihn."""
    connection = db.connect(pfad)
    try:
        with connection:
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(stand),)
            )
    finally:
        connection.close()


def test_der_briefkastenzeiger_faellt_einmal_weg(tmp_path):
    """Ein Zeiger aus der Zeit vor #192 kann in eine fremde Gilde zeigen.

    Discord-Kennungen tragen ihre Zeit in sich: ein junger Zeiger aus einem fremden
    Briefkasten hielte nach der Berichtigung alle eigenen Einwürfe für schon gelesen, und
    die Runde bekäme ihren eigenen Kanal nie zu sehen. Er fällt deshalb einmal weg.

    Ohne den Aufruf in ``init`` bliebe die Suite grün — jeder Test beginnt bei einer
    frischen Datenbank, und die hat nie einen Zeiger von vorher (#195).
    """
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    eine = runden.erste(pfad)
    andere = runden.anlegen(pfad, "Die Andere", guild_id="4242")
    _zeiger_setzen(eine, "4711")
    _zeiger_setzen(andere, "4712")
    _stand_setzen(pfad, db.ZEIGER_VERWORFEN_AB - 1)

    db.init(pfad)

    assert _zeiger(pfad) == {}
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_ein_zeiger_von_danach_bleibt_stehen(tmp_path):
    """Einmal heißt einmal: ein zweiter Start nimmt einer Runde nicht ihren Stand fort."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    eine = runden.erste(pfad)
    _zeiger_setzen(eine, "4711")

    db.init(pfad)

    assert _zeiger(pfad) == {eine.id: "4711"}


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
    über ``/chronicle sitzung-loeschen`` gar nicht zu löschen. Je Sitzung eine eigene, sonst
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


# --- #230: die Instanz-Werte verlassen die Datei -------------------------------------
#
# Der gefährlichste Handgriff dieses Umbaus. Der Bot-Token in ``meta`` ist auf einer
# laufenden Instanz die **einzige** Kopie: er steht nicht im Repo, nicht in der Vorlage
# und nirgends sonst. Gelöscht, ohne dass der Ersatz steht, ist er unwiederbringlich —
# der Betreiber müsste im Discord-Portal einen neuen erzeugen, und bis dahin schweigt der
# Bot in einer echten Gilde. Deshalb: erst der Ersatz, dann das Löschen.


def _mit_werten(tmp_path, **werte):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad, umgebung={})
    connection = db.connect(pfad)
    try:
        with connection:
            for schluessel, wert in werte.items():
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (schluessel, wert),
                )
    finally:
        connection.close()
    return pfad


def test_ohne_ersatz_bleibt_der_token_liegen(tmp_path):
    """Die Reihenfolge, an der alles hängt: kein Ersatz, kein Löschen."""
    pfad = _mit_werten(tmp_path, discord_bot_token="token-nur-in-diesem-test")
    db.init(pfad, umgebung={})
    assert _meta(pfad)["discord_bot_token"] == "token-nur-in-diesem-test"


def test_der_ersatz_raeumt_den_wert_aus_der_datei(tmp_path):
    pfad = _mit_werten(
        tmp_path,
        discord_bot_token="token-nur-in-diesem-test",
        ollama_url="http://alt.example:11434",
        ollama_model="altes-modell",
    )
    db.init(pfad, umgebung=ERSATZ)
    liegt = _meta(pfad)
    for schluessel, _ in db.ABGELOESTE_SCHLUESSEL:
        assert schluessel not in liegt, schluessel
    roh = b""
    for datei in (pfad, pfad.with_suffix(".sqlite3-wal")):
        if datei.exists():
            roh += datei.read_bytes()
    assert "token-nur-in-diesem-test" not in roh.decode("utf-8", errors="ignore")


def test_geraeumt_wird_je_wert_und_nicht_im_ganzen(tmp_path):
    """Ein gesetztes OLLAMA_MODEL nimmt nicht den Token mit, für den nichts dasteht."""
    pfad = _mit_werten(
        tmp_path, discord_bot_token="token-nur-in-diesem-test", ollama_model="altes-modell"
    )
    db.init(pfad, umgebung={"OLLAMA_MODEL": "neues-modell"})
    liegt = _meta(pfad)
    assert "ollama_model" not in liegt
    assert liegt["discord_bot_token"] == "token-nur-in-diesem-test"


def test_die_warnung_sagt_was_zu_tun_ist_und_nennt_den_wert_nicht(tmp_path, caplog):
    """Eine Logzeile ist der wahrscheinlichste Weg, auf dem ein Token nach draußen kommt."""
    geheim = "token-nur-in-diesem-test"
    pfad = _mit_werten(tmp_path, discord_bot_token=geheim)
    with caplog.at_level("WARNING", logger="chronicle.db"):
        db.init(pfad, umgebung={})
    gesagt = "\n".join(eintrag.getMessage() for eintrag in caplog.records)
    assert "DISCORD_BOT_TOKEN" in gesagt
    assert geheim not in gesagt
    assert geheim not in caplog.text


def test_ohne_bestand_sagt_die_wanderung_nichts(tmp_path, caplog):
    """Der Normalfall nach dem Umbau — eine Warnung bei jedem Start wäre Lärm."""
    pfad = tmp_path / "chronicle.sqlite3"
    with caplog.at_level("WARNING", logger="chronicle.db"):
        db.init(pfad, umgebung={})
        db.init(pfad, umgebung=ERSATZ)
    assert caplog.records == []


def test_eine_frische_datenbank_bekommt_die_neue_vorgabe(tmp_path):
    """Der Stempel gilt dem Bestand, nicht jedem Start — sonst wäre die Vorgabe wirkungslos."""
    pfad = tmp_path / "frisch.sqlite3"
    db.init(pfad, umgebung={})
    assert settings.sprache(runden.erste(pfad)) == sprachen.DEFAULT == sprachen.ENGLISCH


def test_eine_gewaehlte_sprache_ueberlebt_den_naechsten_start(alte_datenbank):
    """Der zweite Lauf schreibt keine Entscheidung um — ``DO NOTHING`` ist die Zusage."""
    db.init(alte_datenbank, umgebung={})
    runde = runden.erste(alte_datenbank)
    assert settings.save_sprache(runde, sprachen.ENGLISCH)
    db.init(alte_datenbank, umgebung={})
    assert settings.sprache(runden.erste(alte_datenbank)) == sprachen.ENGLISCH
