-- Alles hier muss ein zweites Mal laufen können, ohne etwas zu ändern.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Der Foundry-Zwischenspeicher: bereits gefiltert und auf die benötigten Felder
-- zusammengestrichen. Der Rohdump wird nie abgelegt.
CREATE TABLE IF NOT EXISTS foundry_snapshot (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at TEXT NOT NULL,
    system     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS foundry_player (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    role  INTEGER NOT NULL,
    is_gm INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS foundry_character (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    type      TEXT,
    owner_ids TEXT NOT NULL,
    limited   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS foundry_message (
    id                  TEXT PRIMARY KEY,
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
    roll_dice           TEXT
);

CREATE INDEX IF NOT EXISTS foundry_message_zeit ON foundry_message (timestamp);

CREATE TABLE IF NOT EXISTS session (
    id         INTEGER PRIMARY KEY,
    played_on  TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL
);

-- Die Reihenfolge der Szenen ist im Präsenzfall die einzige Zeitachse: Foundry wird
-- erst am Ende befüllt, dessen Zeitstempel clustern also alle auf die letzte halbe
-- Stunde.
CREATE TABLE IF NOT EXISTS scene (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, position)
);

CREATE TABLE IF NOT EXISTS note (
    id         INTEGER PRIMARY KEY,
    scene_id   INTEGER NOT NULL REFERENCES scene (id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS note_szene ON note (scene_id);

-- Der Foundry-Fakt selbst liegt in foundry_message; hier steht nur, zu welcher Szene er
-- gehört. Bewusst ohne Fremdschlüssel: ein Abgleich ersetzt den Zwischenspeicher am
-- Stück, die Foundry-Id bleibt dabei stehen — die Zuordnung darf das überleben.
CREATE TABLE IF NOT EXISTS scene_foundry_message (
    scene_id   INTEGER NOT NULL REFERENCES scene (id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    PRIMARY KEY (scene_id, message_id)
);

CREATE TABLE IF NOT EXISTS protocol (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('chronik', 'rueckblick')),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, kind)
);

INSERT INTO meta (key, value) VALUES ('schema_version', '3')
ON CONFLICT (key) DO UPDATE SET value = excluded.value;
