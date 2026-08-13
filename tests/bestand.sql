-- Eine gewachsene Datenbank, wie sie am 2026-08-13 auf der laufenden Instanz stand:
-- Schema 26 ohne die Spalten, die ``db.NACHGETRAGEN`` nachträgt. Sie ist eine
-- Momentaufnahme der Vergangenheit und **darf sich nie wieder ändern** — wer sie an das
-- heutige ``schema.sql`` angleicht, macht ihren Test still gegenstandslos, denn dann
-- fehlt keine Spalte mehr und es bleibt nichts nachzutragen. Sie wächst auch nicht mit:
-- je älter sie ist, desto mehr deckt sie. Entstanden aus ``schema.sql`` mit einem
-- ``ALTER TABLE … DROP COLUMN`` je Eintrag aus ``NACHGETRAGEN``; die Kommentare des
-- Schemas sind fort, weil sie die Absicht von heute beschreiben und nicht die von damals.

CREATE TABLE consent_event (
    id           INTEGER PRIMARY KEY,
    runde_id     INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    session_id   INTEGER REFERENCES session (id) ON DELETE SET NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('ansage', 'nachzuegler')),
    announced_at TEXT NOT NULL,
    guild_id     TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    text         TEXT NOT NULL,
    UNIQUE (id, runde_id)
);
CREATE TABLE consent_member (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL,
    user_id  TEXT NOT NULL,
    name     TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id),
    FOREIGN KEY (event_id, runde_id) REFERENCES consent_event (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE discord_intake (
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    status     TEXT NOT NULL
               CHECK (status IN ('abgelegt', 'wartet', 'uebersprungen')),
    handled_at TEXT NOT NULL,
    PRIMARY KEY (runde_id, message_id)
);
CREATE TABLE foundry_character (
    runde_id  INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    id        TEXT NOT NULL,
    name      TEXT NOT NULL,
    type      TEXT,
    owner_ids TEXT NOT NULL,
    limited   INTEGER NOT NULL,
    PRIMARY KEY (runde_id, id)
);
CREATE TABLE foundry_message (
    runde_id            INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    id                  TEXT NOT NULL,
    timestamp           INTEGER NOT NULL,
    speaker_actor       TEXT,
    speaker_alias       TEXT,
    content             TEXT NOT NULL,
    roll_title          TEXT,
    roll_total          INTEGER,
    roll_formula        TEXT,
    roll_kind           TEXT,
    roll_critical       INTEGER,
    roll_modifier_total INTEGER,
    roll_dice           TEXT,
    PRIMARY KEY (runde_id, id)
);
CREATE TABLE foundry_player (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    id       TEXT NOT NULL,
    name     TEXT NOT NULL,
    role     INTEGER NOT NULL,
    is_gm    INTEGER NOT NULL,
    PRIMARY KEY (runde_id, id)
);
CREATE TABLE foundry_scene (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    id       TEXT NOT NULL,
    name     TEXT NOT NULL,
    active   INTEGER NOT NULL,
    PRIMARY KEY (runde_id, id)
);
CREATE TABLE foundry_snapshot (
    runde_id   INTEGER PRIMARY KEY REFERENCES runde (id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    system     TEXT NOT NULL
);
CREATE TABLE job (
    id          INTEGER PRIMARY KEY,
    runde_id    INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('abgleich', 'chronik', 'nachtlauf',
                                              'nacherzaehlung')),
    session_id  INTEGER,
    state       TEXT NOT NULL CHECK (state IN ('laeuft', 'fertig', 'gescheitert')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    result      TEXT,
    error       TEXT,
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE note (
    id                 INTEGER PRIMARY KEY,
    runde_id           INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    scene_id           INTEGER NOT NULL,
    text               TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    FOREIGN KEY (scene_id, runde_id) REFERENCES scene (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE person_mapping (
    runde_id        INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    discord_user_id TEXT NOT NULL,
    foundry_user_id TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL,
    PRIMARY KEY (runde_id, discord_user_id)
);
CREATE TABLE protocol (
    id           INTEGER PRIMARY KEY,
    runde_id     INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    session_id   INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('chronik', 'rueckblick')),
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (session_id, kind),
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE recording (
    id          INTEGER PRIMARY KEY,
    runde_id    INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    session_id  INTEGER NOT NULL,
    filename    TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('wartet', 'laeuft', 'fertig', 'gescheitert')),
    detail      TEXT,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE register_entry (
    id               INTEGER PRIMARY KEY,
    runde_id         INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('figur', 'ort', 'faden')),
    name             TEXT NOT NULL,
    description      TEXT NOT NULL,
    foundry_actor_id TEXT,
    state            TEXT NOT NULL CHECK (state IN ('vorschlag', 'bestaetigt')),
    suggested_at     TEXT NOT NULL,
    confirmed_at     TEXT,
    UNIQUE (runde_id, kind, name),
    UNIQUE (id, runde_id)
);
CREATE TABLE register_mention (
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    entry_id   INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    scene_id   INTEGER,
    FOREIGN KEY (entry_id, runde_id) REFERENCES register_entry (id, runde_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE,
    FOREIGN KEY (scene_id, runde_id) REFERENCES scene (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE runde (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    guild_id     TEXT UNIQUE,
    created_at   TEXT NOT NULL);
CREATE TABLE runde_meta (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (runde_id, key)
);
CREATE TABLE scene (
    id         INTEGER PRIMARY KEY,
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, position),
    UNIQUE (id, runde_id),
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE scene_foundry_message (
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    scene_id   INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (scene_id, message_id),
    FOREIGN KEY (scene_id, runde_id) REFERENCES scene (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE session (
    id         INTEGER PRIMARY KEY,
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    played_on  TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (id, runde_id)
);
CREATE TABLE settings (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (runde_id, key)
);
CREATE TABLE transcript (
    id         INTEGER PRIMARY KEY,
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, source),
    UNIQUE (id, runde_id),
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);
CREATE TABLE transcript_segment (
    id            INTEGER PRIMARY KEY,
    runde_id      INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    transcript_id INTEGER NOT NULL,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    text          TEXT NOT NULL,
    FOREIGN KEY (transcript_id, runde_id)
        REFERENCES transcript (id, runde_id) ON DELETE CASCADE
);
CREATE INDEX consent_event_sitzung ON consent_event (session_id);
CREATE INDEX foundry_message_zeit ON foundry_message (runde_id, timestamp);
CREATE INDEX job_art ON job (runde_id, kind, session_id, id);
CREATE INDEX note_szene ON note (scene_id);
CREATE INDEX recording_sitzung ON recording (session_id);
CREATE INDEX register_mention_eintrag ON register_mention (entry_id, session_id);
CREATE INDEX session_runde ON session (runde_id, played_on, id);
CREATE INDEX transcript_segment_zeit
    ON transcript_segment (transcript_id, start_ms);
