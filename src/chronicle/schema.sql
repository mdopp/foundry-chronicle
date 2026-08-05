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

-- Die fünf Werte aus der Oberfläche. Was hier steht, schlägt die Umgebung; die bleibt
-- die Vorgabe beim ersten Start. Das Foundry-Passwort liegt damit im Klartext in dieser
-- Datei und geht mit ins Backup — bewusste Abwägung, siehe CLAUDE.md.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Der Suchindex. FTS5 steckt in SQLite, also keine neue Abhängigkeit. Eine Zeile je
-- auffindbarem Stück; ``kind`` unterscheidet sie, damit ein Transkript später eine
-- weitere Art bekommt und keine weitere Tabelle.
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5 (
    text,
    kind       UNINDEXED,
    ref_id     UNINDEXED,
    session_id UNINDEXED,
    scene_id   UNINDEXED
);

CREATE TRIGGER IF NOT EXISTS note_search_insert AFTER INSERT ON note BEGIN
    INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
    VALUES (new.text, 'notiz', new.id,
            (SELECT session_id FROM scene WHERE id = new.scene_id), new.scene_id);
END;

CREATE TRIGGER IF NOT EXISTS note_search_delete AFTER DELETE ON note BEGIN
    DELETE FROM search_index WHERE kind = 'notiz' AND ref_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS protocol_search_insert AFTER INSERT ON protocol BEGIN
    INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
    VALUES (new.text, new.kind, new.id, new.session_id, NULL);
END;

-- Ein zweiter Lauf von ``chronicle.compose`` ersetzt den Text der Zeile; ohne diesen
-- Trigger stünde die verworfene Fassung weiter im Index.
CREATE TRIGGER IF NOT EXISTS protocol_search_update AFTER UPDATE ON protocol BEGIN
    UPDATE search_index SET text = new.text WHERE kind = old.kind AND ref_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS protocol_search_delete AFTER DELETE ON protocol BEGIN
    DELETE FROM search_index WHERE kind = old.kind AND ref_id = old.id;
END;

-- Der Index ist abgeleitet: die Trigger halten ihn im Betrieb aktuell, dieser Neuaufbau
-- holt beim Start, was vor ihnen entstanden ist — eine Datenbank aus Schema 4.
DELETE FROM search_index;

INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
SELECT n.text, 'notiz', n.id, c.session_id, n.scene_id
FROM note n JOIN scene c ON c.id = n.scene_id;

INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
SELECT p.text, p.kind, p.id, p.session_id, NULL FROM protocol p;

INSERT INTO meta (key, value) VALUES ('schema_version', '5')
ON CONFLICT (key) DO UPDATE SET value = excluded.value;
