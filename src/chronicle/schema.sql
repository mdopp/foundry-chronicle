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

-- ``delivered_at`` gilt nur dem Rückblick: wann er in den Gruppenkanal gestellt wurde. Es
-- steht hier und nicht in einer eigenen Tabelle, weil der Wert die Zustellung genau eines
-- Protokolls beschreibt — und weil der zweite Lauf von ``chronicle.compose`` den Text per
-- UPSERT ersetzt, ohne diese Spalte anzufassen. Genau das ist die Zusage: **eine Sitzung,
-- eine Zustellung.** Eine neu komponierte Fassung wird nicht noch einmal gepostet; der
-- Kanal ist die Zeitachse der Gruppe, ein zweiter Rückblick darin läse sich wie eine
-- zweite Sitzung. Die jeweils gültige Fassung steht in der Chronik-Ansicht.
CREATE TABLE IF NOT EXISTS protocol (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('chronik', 'rueckblick')),
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (session_id, kind)
);

-- Eine Spur, ein Transkript: ``source`` sagt, welche Spur es war — der Dateiname der
-- Aufnahme, beim Recorder-Bot später der Sprecher. Mehr Sprecher-Felder braucht eine
-- einzelne Spur nicht; die Zuordnung zu Personen kommt mit dem Bot.
CREATE TABLE IF NOT EXISTS transcript (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, source)
);

-- Die Warteschlange der Stufe: eine Zeile je Spur, die transkribiert werden will. Sie
-- ist der Job — ``id`` ist die Job-Id, ``status`` der einzige Fortschritt, den es hier
-- ehrlich zu melden gibt. Der Diktat-Upload reiht sich hier ein, der Recorder-Bot
-- später ebenso; einen zweiten Verarbeitungsweg gibt es nicht.
--
-- ``deleted_at`` sagt, wann die Audiodatei nach der zugesagten Frist entfernt wurde. Die
-- Zeile bleibt: dass es die Spur gab und was aus ihr wurde, ist die ehrliche Hälfte der
-- Geschichte. Ein eigenes Feld statt eines weiteren ``status``, damit der Ausgang des
-- Laufs daneben stehen bleibt. Kommentare gehören außerhalb der Klammer — SQLite liest
-- den Tabellentext bei ``ALTER TABLE DROP COLUMN`` neu ein und stolpert sonst darüber.
CREATE TABLE IF NOT EXISTS recording (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    filename    TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('wartet', 'laeuft', 'fertig', 'gescheitert')),
    detail      TEXT,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT,
    discord_user_id TEXT
);

CREATE INDEX IF NOT EXISTS recording_sitzung ON recording (session_id);

-- Zeitstempel in Millisekunden ab Spurbeginn: die Zusammenführung legt später mehrere
-- Spuren auf eine Zeitachse, und Fließkomma-Sekunden wären dabei die falsche Einheit.
CREATE TABLE IF NOT EXISTS transcript_segment (
    id            INTEGER PRIMARY KEY,
    transcript_id INTEGER NOT NULL REFERENCES transcript (id) ON DELETE CASCADE,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    text          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS transcript_segment_zeit
    ON transcript_segment (transcript_id, start_ms);

-- Die Werte aus der Oberfläche. Was hier steht, schlägt die Umgebung; die bleibt die
-- Vorgabe beim ersten Start. Foundry-Passwort und Discord-Bot-Token liegen damit im
-- Klartext in dieser Datei und gehen mit ins Backup — bewusste Abwägung, siehe CLAUDE.md.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Was aus dem Diktat-Kanal schon abgeholt wurde. Der Zeiger auf die zuletzt geholte
-- Nachricht steht in ``meta``; er spart das erneute Holen. Diese Tabelle ist die
-- Garantie: geht der Zeiger verloren, wird trotzdem nichts ein zweites Mal abgelegt.
CREATE TABLE IF NOT EXISTS discord_intake (
    message_id TEXT PRIMARY KEY,
    status     TEXT NOT NULL
               CHECK (status IN ('abgelegt', 'wartet', 'uebersprungen')),
    handled_at TEXT NOT NULL
);

-- Das Einwilligungsprotokoll des Aufnahme-Bots. Das Aufzeichnen des nichtöffentlich
-- gesprochenen Wortes ohne Einwilligung ist strafbar (§201 StGB); der Bot sagt hörbar an,
-- und **was** er angesagt hat, steht hier im Wortlaut — nicht als Verweis auf eine
-- Konstante im Code, die sich später ändern kann. Ein Nachweis, der sich rückwirkend
-- umschreibt, ist keiner. Die Zeile überlebt deshalb auch das Löschen ihrer Sitzung.
CREATE TABLE IF NOT EXISTS consent_event (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES session (id) ON DELETE SET NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('ansage', 'nachzuegler')),
    announced_at TEXT NOT NULL,
    guild_id     TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    text         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS consent_event_sitzung ON consent_event (session_id);

-- Wer im Sprachkanal war, als die Ansage zu Ende gespielt hatte. Der Anzeigename steht
-- dabei: eine Discord-Id allein ist Wochen später niemand mehr.
CREATE TABLE IF NOT EXISTS consent_member (
    event_id INTEGER NOT NULL REFERENCES consent_event (id) ON DELETE CASCADE,
    user_id  TEXT NOT NULL,
    name     TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id)
);

-- Die Personen-Zuordnung Discord ↔ Foundry. Hier steht ausschließlich **Bestätigtes**:
-- ein Vorschlag wird bei jedem Aufruf neu gerechnet, denn ein gespeicherter Vorschlag
-- sähe wenige Wochen später aus wie eine Zuordnung. Kein Fremdschlüssel auf
-- ``foundry_player``: ein Abgleich ersetzt den Zwischenspeicher am Stück, die
-- Bestätigung eines Menschen darf das überleben.
--
-- Die Anzeigenamen stehen nicht hier — sie liegen im Einwilligungsprotokoll und im
-- Foundry-Zwischenspeicher. Dieselbe personenbezogene Angabe ein drittes Mal zu führen
-- wäre keine Erleichterung.
CREATE TABLE IF NOT EXISTS person_mapping (
    discord_user_id TEXT PRIMARY KEY,
    foundry_user_id TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL
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

-- Gesucht wird im Segment, gefunden wird das Transkript: ``ref_id`` zeigt deshalb auf
-- das Transkript und nicht auf die einzelne Zeile. Segmente werden nur im Ganzen
-- ersetzt, also darf der Löschtrigger über den Text gehen — zwei gleichlautende Zeilen
-- verschwinden ohnehin zusammen.
CREATE TRIGGER IF NOT EXISTS transcript_search_insert
AFTER INSERT ON transcript_segment BEGIN
    INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
    VALUES (new.text, 'transkript', new.transcript_id,
            (SELECT session_id FROM transcript WHERE id = new.transcript_id), NULL);
END;

CREATE TRIGGER IF NOT EXISTS transcript_search_delete
AFTER DELETE ON transcript_segment BEGIN
    DELETE FROM search_index
    WHERE kind = 'transkript' AND ref_id = old.transcript_id AND text = old.text;
END;

-- Der Index ist abgeleitet: die Trigger halten ihn im Betrieb aktuell, dieser Neuaufbau
-- holt beim Start, was vor ihnen entstanden ist — eine Datenbank aus Schema 4.
DELETE FROM search_index;

INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
SELECT n.text, 'notiz', n.id, c.session_id, n.scene_id
FROM note n JOIN scene c ON c.id = n.scene_id;

INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
SELECT p.text, p.kind, p.id, p.session_id, NULL FROM protocol p;

INSERT INTO search_index (text, kind, ref_id, session_id, scene_id)
SELECT s.text, 'transkript', s.transcript_id, t.session_id, NULL
FROM transcript_segment s JOIN transcript t ON t.id = s.transcript_id;

INSERT INTO meta (key, value) VALUES ('schema_version', '12')
ON CONFLICT (key) DO UPDATE SET value = excluded.value;
