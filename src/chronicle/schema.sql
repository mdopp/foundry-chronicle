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

INSERT INTO meta (key, value) VALUES ('schema_version', '2')
ON CONFLICT (key) DO UPDATE SET value = excluded.value;
