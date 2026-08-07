-- Alles hier muss ein zweites Mal laufen können, ohne etwas zu ändern.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Die Runde ist der Mandant: eine Instanz trägt mehrere. Der Schlüssel nach außen ist die
-- Discord-Gilde, der Schlüssel nach innen bleibt die ``id`` — eine Gilde kann später
-- wegfallen oder wechseln, die Chronik dahinter nicht. ``guild_id`` bleibt leer, bis eine
-- Gilde die Runde für sich beansprucht; die Bestände der Entwicklungs-Instanz wandern in
-- eine solche noch unbeanspruchte Runde.
--
-- ``locked_at`` und ``delete_after`` tragen den Abschied: wird der Bot aus der Gilde
-- geworfen, ist die Runde sofort still und verschwindet nach der Frist. Zwei Spalten und
-- nicht eine gerechnete, weil die Frist eine **Zusage** ist — sie steht als Datum da und
-- lässt sich nicht dadurch verschieben, dass jemand später an der Konstante dreht.
CREATE TABLE IF NOT EXISTS runde (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    guild_id     TEXT UNIQUE,
    created_at   TEXT NOT NULL,
    locked_at    TEXT,
    delete_after TEXT
);

-- Merkzettel je Runde: der Zeiger auf die zuletzt abgeholte Diktat-Nachricht, der Grund
-- des letzten gescheiterten Foundry-Abgleichs. Kein Einstellungswert — nichts davon wird
-- gepflegt, alles davon wird fortgeschrieben. ``meta`` steht daneben und gehört der
-- Instanz.
CREATE TABLE IF NOT EXISTS runde_meta (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (runde_id, key)
);

-- Der Kopf des Foundry-Abgleichs: wann zuletzt geholt wurde und welches Regelwerk läuft.
-- Der Rohdump wird nie abgelegt, alles hier ist bereits gefiltert und auf die benötigten
-- Felder zusammengestrichen. Ein Kopf je Runde — die ``runde_id`` ist hier zugleich der
-- Primärschlüssel.
CREATE TABLE IF NOT EXISTS foundry_snapshot (
    runde_id   INTEGER PRIMARY KEY REFERENCES runde (id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    system     TEXT NOT NULL
);

-- Die Foundry-Ids stammen aus fremden Welten und sind nur dort eindeutig. Der
-- Primärschlüssel führt deshalb die Runde mit: zwei Runden dürfen dieselbe Id tragen.
--
-- Konten und Figuren sind **Spiegel**: der aktuelle Stand steht in Foundry, ein Abgleich
-- ersetzt sie am Stück. Die Chat-Nachrichten weiter unten sind es nicht.
CREATE TABLE IF NOT EXISTS foundry_player (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    id       TEXT NOT NULL,
    name     TEXT NOT NULL,
    role     INTEGER NOT NULL,
    is_gm    INTEGER NOT NULL,
    PRIMARY KEY (runde_id, id)
);

CREATE TABLE IF NOT EXISTS foundry_character (
    runde_id  INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    id        TEXT NOT NULL,
    name      TEXT NOT NULL,
    type      TEXT,
    owner_ids TEXT NOT NULL,
    limited   INTEGER NOT NULL,
    PRIMARY KEY (runde_id, id)
);

-- Chat-Nachrichten sind **Ereignisse, keine Zustände**: ein Abgleich fügt hinzu und behält,
-- er ersetzt nicht. Das Chat-Log in Foundry zu leeren ist übliche Praxis der Spielleitung —
-- ein Spiegel verlöre damit genau die Würfe, die eine Szene belegen und aus denen später
-- neu komponiert wird.
--
-- ``vanished_at`` ist der Herkunftsvermerk: der Zeitpunkt des ersten Abgleichs, der diese
-- Nachricht in Foundry nicht mehr fand. Leer heißt vorhanden. Er wird gezeigt, wo die
-- Nachricht benutzt wird — eine stille Karteileiche wäre die falsche Sorte Gedächtnis.
CREATE TABLE IF NOT EXISTS foundry_message (
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
    vanished_at         TEXT,
    PRIMARY KEY (runde_id, id)
);

CREATE INDEX IF NOT EXISTS foundry_message_zeit ON foundry_message (runde_id, timestamp);

-- ``UNIQUE (id, runde_id)`` sieht überflüssig aus, ist aber der Anker: die Kindtabellen
-- verweisen auf **beides**, und damit kann keine Zeile an einer Sitzung einer fremden
-- Runde hängen. Ein falsch zusammengesetztes INSERT scheitert an der Datenbank statt an
-- der Aufmerksamkeit dessen, der es schrieb.
-- ``thread_id`` ist der Discord-Thread, in dem diese Sitzung geschrieben wird: er hat
-- einen Anfang, ein Ende, eine Teilnehmerliste und eine Zeitachse, und die Runde tippt
-- ohnehin dort. Er bleibt leer, wo eine Sitzung anders entstanden ist.
CREATE TABLE IF NOT EXISTS session (
    id         INTEGER PRIMARY KEY,
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    played_on  TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL,
    thread_id  TEXT,
    UNIQUE (id, runde_id)
);

CREATE INDEX IF NOT EXISTS session_runde ON session (runde_id, played_on, id);

-- Die Reihenfolge der Szenen ist im Präsenzfall die einzige Zeitachse: Foundry wird
-- erst am Ende befüllt, dessen Zeitstempel clustern also alle auf die letzte halbe
-- Stunde.
--
-- ``runde_id`` steht hier, obwohl sie sich über ``session_id`` herleiten ließe. Genau das
-- ist der Punkt: eine Abfrage, die eine Szene anfasst, soll die Runde nennen müssen statt
-- über einen Verbund darauf zu hoffen.
CREATE TABLE IF NOT EXISTS scene (
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

-- ``discord_message_id`` bindet die Notiz an die Nachricht, aus der sie entstand. Discord
-- meldet Änderung und Löschung nur mit dieser Kennung — ohne sie bliebe eine gelöschte
-- Notiz bei uns stehen, und das wäre die falsche Sorte Gedächtnis.
CREATE TABLE IF NOT EXISTS note (
    id                 INTEGER PRIMARY KEY,
    runde_id           INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    scene_id           INTEGER NOT NULL,
    text               TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    discord_message_id TEXT,
    FOREIGN KEY (scene_id, runde_id) REFERENCES scene (id, runde_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS note_szene ON note (scene_id);

-- Der Foundry-Fakt selbst liegt in foundry_message; hier steht nur, zu welcher Szene er
-- gehört. Bewusst ohne Fremdschlüssel: die Zuordnung ist die Entscheidung eines Menschen
-- und überlebt jeden Abgleich — auch den, der die Nachricht in Foundry nicht mehr findet.
CREATE TABLE IF NOT EXISTS scene_foundry_message (
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    scene_id   INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (scene_id, message_id),
    FOREIGN KEY (scene_id, runde_id) REFERENCES scene (id, runde_id) ON DELETE CASCADE
);

-- ``delivered_at`` sagt, wann dieses Protokoll nach Discord hinausging. Es steht hier und
-- nicht in einer eigenen Tabelle, weil der Wert die Zustellung genau eines Protokolls
-- beschreibt — und weil der zweite Lauf von ``chronicle.compose`` den Text per UPSERT
-- ersetzt, ohne diese Spalte anzufassen. Gelesen wird er je Art verschieden, und das ist
-- Absicht:
--
-- * **Rückblick — eine Sitzung, eine Zustellung.** Steht ein Zeitpunkt da, passiert nichts
--   mehr. Eine neu komponierte Fassung wird nicht noch einmal in den Gruppenkanal
--   gestellt; der Kanal ist die Zeitachse der Gruppe, ein zweiter Rückblick darin läse
--   sich wie eine zweite Sitzung.
-- * **Chronik — eine Fassung, eine Datei.** Der Zeitpunkt ist der des letzten Anhängens im
--   Sitzungs-Thread. Ist ``created_at`` jünger, wurde seither neu geschrieben, und die
--   neue Fassung wird als neue Datei angehängt; die alte bleibt stehen. Der Thread ist ein
--   Protokoll, keine Tafel.
CREATE TABLE IF NOT EXISTS protocol (
    id           INTEGER PRIMARY KEY,
    runde_id     INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    session_id   INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('chronik', 'rueckblick')),
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (session_id, kind),
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);

-- Eine Spur, ein Transkript: ``source`` sagt, welche Spur es war — der Dateiname der
-- Aufnahme, beim Recorder-Bot später der Sprecher. Mehr Sprecher-Felder braucht eine
-- einzelne Spur nicht; die Zuordnung zu Personen kommt mit dem Bot.
CREATE TABLE IF NOT EXISTS transcript (
    id         INTEGER PRIMARY KEY,
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, source),
    UNIQUE (id, runde_id),
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
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
--
-- ``filename`` ist über alle Runden hinweg eindeutig: das Aufnahmeverzeichnis ist eines,
-- zwei gleichnamige Spuren wären dieselbe Datei.
CREATE TABLE IF NOT EXISTS recording (
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
    deleted_at  TEXT,
    discord_user_id TEXT,
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS recording_sitzung ON recording (session_id);

-- Was ein Knopf in der Oberfläche oder der nächtliche Zeitplan angestoßen hat. Die Zeile
-- *ist* der Lauf: sie überlebt das Neuladen der Seite, und ein Blick darauf sagt, ob noch
-- etwas passiert. ``result`` und ``error`` stehen in der Sprache der Anzeige — sie werden
-- dem Leser gezeigt. Beim ``nachtlauf`` steht in ``result`` das Ergebnis je Schritt, damit
-- jede Karte der Einstellungen ihr eigenes zeigen kann.
--
-- Ein Neustart mitten im Lauf lässt ``laeuft`` stehen; ``chronicle.jobs`` schreibt die
-- Zeile beim nächsten Blick auf ``gescheitert`` um, damit nichts für immer zu laufen
-- scheint.
--
-- Der Lauf gehört einer Runde, die Maschine allen: ``chronicle.jobs`` lässt über alle
-- Runden hinweg nur einen gleichzeitig laufen — eine CPU, ein Ollama.
CREATE TABLE IF NOT EXISTS job (
    id          INTEGER PRIMARY KEY,
    runde_id    INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('abgleich', 'chronik', 'nachtlauf')),
    session_id  INTEGER,
    state       TEXT NOT NULL CHECK (state IN ('laeuft', 'fertig', 'gescheitert')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    result      TEXT,
    error       TEXT,
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS job_art ON job (runde_id, kind, session_id, id);

-- Zeitstempel in Millisekunden ab Spurbeginn: die Zusammenführung legt später mehrere
-- Spuren auf eine Zeitachse, und Fließkomma-Sekunden wären dabei die falsche Einheit.
CREATE TABLE IF NOT EXISTS transcript_segment (
    id            INTEGER PRIMARY KEY,
    runde_id      INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    transcript_id INTEGER NOT NULL,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    text          TEXT NOT NULL,
    FOREIGN KEY (transcript_id, runde_id)
        REFERENCES transcript (id, runde_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS transcript_segment_zeit
    ON transcript_segment (transcript_id, start_ms);

-- Die Werte, die eine Runde pflegt. Was hier steht, schlägt die Umgebung; die bleibt die
-- Vorgabe beim ersten Start. Das Foundry-Passwort liegt damit im Klartext in dieser Datei
-- und geht mit ins Backup — bewusste Abwägung, siehe CLAUDE.md.
--
-- Was der **Instanz** gehört und keiner Runde — voran der Discord-Bot-Token, denn das ist
-- unser Token und nicht das einer Gruppe — steht in ``meta``, siehe ``chronicle.instanz``.
CREATE TABLE IF NOT EXISTS settings (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (runde_id, key)
);

-- Was aus dem Diktat-Kanal schon abgeholt wurde. Der Zeiger auf die zuletzt geholte
-- Nachricht steht in ``runde_meta``; er spart das erneute Holen. Diese Tabelle ist die
-- Garantie: geht der Zeiger verloren, wird trotzdem nichts ein zweites Mal abgelegt.
CREATE TABLE IF NOT EXISTS discord_intake (
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    status     TEXT NOT NULL
               CHECK (status IN ('abgelegt', 'wartet', 'uebersprungen')),
    handled_at TEXT NOT NULL,
    PRIMARY KEY (runde_id, message_id)
);

-- Das Einwilligungsprotokoll des Aufnahme-Bots. Das Aufzeichnen des nichtöffentlich
-- gesprochenen Wortes ohne Einwilligung ist strafbar (§201 StGB); der Bot sagt hörbar an,
-- und **was** er angesagt hat, steht hier im Wortlaut — nicht als Verweis auf eine
-- Konstante im Code, die sich später ändern kann. Ein Nachweis, der sich rückwirkend
-- umschreibt, ist keiner. Die Zeile überlebt deshalb auch das Löschen ihrer Sitzung.
CREATE TABLE IF NOT EXISTS consent_event (
    id           INTEGER PRIMARY KEY,
    runde_id     INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    -- Einspaltig und nicht zusammengesetzt wie sonst: ``SET NULL`` löschte sonst auch die
    -- ``runde_id``, und die darf hier nie leer sein. Der Nachweis überlebt seine Sitzung.
    session_id   INTEGER REFERENCES session (id) ON DELETE SET NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('ansage', 'nachzuegler')),
    announced_at TEXT NOT NULL,
    guild_id     TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    text         TEXT NOT NULL,
    UNIQUE (id, runde_id)
);

CREATE INDEX IF NOT EXISTS consent_event_sitzung ON consent_event (session_id);

-- Wer im Sprachkanal war, als die Ansage zu Ende gespielt hatte. Der Anzeigename steht
-- dabei: eine Discord-Id allein ist Wochen später niemand mehr.
CREATE TABLE IF NOT EXISTS consent_member (
    runde_id INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL,
    user_id  TEXT NOT NULL,
    name     TEXT NOT NULL,
    PRIMARY KEY (event_id, user_id),
    FOREIGN KEY (event_id, runde_id) REFERENCES consent_event (id, runde_id) ON DELETE CASCADE
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
--
-- Derselbe Mensch kann in zwei Runden mitspielen: der Schlüssel führt die Runde mit.
CREATE TABLE IF NOT EXISTS person_mapping (
    runde_id        INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    discord_user_id TEXT NOT NULL,
    foundry_user_id TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL,
    PRIMARY KEY (runde_id, discord_user_id)
);

-- Das kampagnenweite Register: Figuren, Orte, Handlungsfäden. Ein **Index**, kein Wiki —
-- Name, ein Satz, Verweise. Die Wahrheit über eine Figur wohnt in Foundry; ``foundry_actor_id``
-- zeigt nur darauf und pflegt nichts doppelt. Kein Fremdschlüssel dorthin: ein Abgleich
-- ersetzt den Zwischenspeicher am Stück, die Bestätigung eines Menschen darf das überleben.
--
-- ``state`` ist der ganze Sinn der Tabelle. Ein Vorschlag ist eine Deutung des Sprachmodells
-- und wird nie von allein bestätigt; erst was ein Mensch bestätigt hat, steht im Register und
-- im Suchindex. Ein unbestätigtes Register verfälschte das Nacherzählen.
CREATE TABLE IF NOT EXISTS register_entry (
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

-- Wo ein Eintrag vorkommt. ``scene_id`` bleibt leer, wenn der Name in keiner Szene wörtlich
-- steht — bei einem Handlungsfaden ist das der Normalfall, denn der ist gedeutet und nicht
-- zitiert. Ein Eintrag je Sitzung und Szene wird beim Lauf ersetzt, nicht ergänzt.
CREATE TABLE IF NOT EXISTS register_mention (
    runde_id   INTEGER NOT NULL REFERENCES runde (id) ON DELETE CASCADE,
    entry_id   INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    scene_id   INTEGER,
    FOREIGN KEY (entry_id, runde_id) REFERENCES register_entry (id, runde_id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, runde_id) REFERENCES session (id, runde_id) ON DELETE CASCADE,
    FOREIGN KEY (scene_id, runde_id) REFERENCES scene (id, runde_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS register_mention_eintrag ON register_mention (entry_id, session_id);
