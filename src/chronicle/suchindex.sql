-- Der Suchindex ist abgeleitet: er wird bei jedem Start verworfen und neu gebaut, die
-- Trigger halten ihn danach im Betrieb aktuell. Deshalb steht er in einer eigenen Datei
-- und läuft **nach** der Schema-Wanderung — sonst läse er Spalten, die es noch nicht gibt.
--
-- ``runde_id`` steht mit im Index und nicht nur in der Sitzung dahinter: die Suche ist
-- der Ort, an dem ein vergessenes ``WHERE`` fremde Notizen sichtbar machte, und ein
-- Verbund über ``session`` wäre eine Bedingung mehr, die man weglassen kann.
--
-- Verworfen wird ein Index **alter Form** vorher in ``chronicle.db``: sonst störte ein
-- Trigger aus dem alten Schema die Wanderung. Eine fts5-Tabelle neu anzulegen kostet
-- spürbar, deshalb passiert das nur, wenn die Spalten wirklich nicht mehr passen — sonst
-- wird der Inhalt bloß verworfen und neu geschrieben.

-- FTS5 steckt in SQLite, also keine neue Abhängigkeit. Eine Zeile je auffindbarem Stück;
-- ``kind`` unterscheidet sie, damit eine weitere Art keine weitere Tabelle braucht.
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5 (
    text,
    kind       UNINDEXED,
    ref_id     UNINDEXED,
    runde_id   UNINDEXED,
    session_id UNINDEXED,
    scene_id   UNINDEXED
);

CREATE TRIGGER IF NOT EXISTS note_search_insert AFTER INSERT ON note BEGIN
    INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
    VALUES (new.text, 'notiz', new.id, new.runde_id,
            (SELECT session_id FROM scene WHERE id = new.scene_id), new.scene_id);
END;

-- Eine im Thread nachträglich geänderte Nachricht ändert ihre Notiz; ohne diesen Trigger
-- fände die Suche weiter den zurückgenommenen Wortlaut.
CREATE TRIGGER IF NOT EXISTS note_search_update AFTER UPDATE ON note BEGIN
    UPDATE search_index SET text = new.text WHERE kind = 'notiz' AND ref_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS note_search_delete AFTER DELETE ON note BEGIN
    DELETE FROM search_index WHERE kind = 'notiz' AND ref_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS protocol_search_insert AFTER INSERT ON protocol BEGIN
    INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
    VALUES (new.text, new.kind, new.id, new.runde_id, new.session_id, NULL);
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
    INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
    VALUES (new.text, 'transkript', new.transcript_id, new.runde_id,
            (SELECT session_id FROM transcript WHERE id = new.transcript_id), NULL);
END;

CREATE TRIGGER IF NOT EXISTS transcript_search_delete
AFTER DELETE ON transcript_segment BEGIN
    DELETE FROM search_index
    WHERE kind = 'transkript' AND ref_id = old.transcript_id AND text = old.text;
END;

-- Nur Bestätigtes steht im Index: ein Vorschlag als Suchtreffer läse sich wie eine Tatsache.
-- Die Sitzung ist die erste, in der der Eintrag vorkommt — die Suche verlangt eine, die
-- vollständige Liste steht im Register.
CREATE TRIGGER IF NOT EXISTS register_search_insert AFTER INSERT ON register_entry
WHEN new.state = 'bestaetigt' BEGIN
    INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
    SELECT new.name || ' — ' || new.description, 'register', new.id, new.runde_id,
           (SELECT MIN(session_id) FROM register_mention WHERE entry_id = new.id), NULL;
END;

CREATE TRIGGER IF NOT EXISTS register_search_update AFTER UPDATE ON register_entry BEGIN
    DELETE FROM search_index WHERE kind = 'register' AND ref_id = old.id;
    INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
    SELECT new.name || ' — ' || new.description, 'register', new.id, new.runde_id,
           (SELECT MIN(session_id) FROM register_mention WHERE entry_id = new.id), NULL
    WHERE new.state = 'bestaetigt';
END;

CREATE TRIGGER IF NOT EXISTS register_search_delete AFTER DELETE ON register_entry BEGIN
    DELETE FROM search_index WHERE kind = 'register' AND ref_id = old.id;
END;

-- Der Neuaufbau: was vor den Triggern entstanden ist, kommt hier herein.
DELETE FROM search_index;

INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
SELECT n.text, 'notiz', n.id, n.runde_id, c.session_id, n.scene_id
FROM note n JOIN scene c ON c.id = n.scene_id;

INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
SELECT p.text, p.kind, p.id, p.runde_id, p.session_id, NULL FROM protocol p;

INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
SELECT s.text, 'transkript', s.transcript_id, s.runde_id, t.session_id, NULL
FROM transcript_segment s JOIN transcript t ON t.id = s.transcript_id;

INSERT INTO search_index (text, kind, ref_id, runde_id, session_id, scene_id)
SELECT e.name || ' — ' || e.description, 'register', e.id, e.runde_id,
       (SELECT MIN(session_id) FROM register_mention WHERE entry_id = e.id), NULL
FROM register_entry e WHERE e.state = 'bestaetigt';

INSERT INTO meta (key, value) VALUES ('schema_version', '27')
ON CONFLICT (key) DO UPDATE SET value = excluded.value;
