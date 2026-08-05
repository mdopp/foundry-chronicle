-- Nur die Meta-Tabelle: die Fachtabellen kommen mit den folgenden Ausbau-Issues.
-- Alles hier muss ein zweites Mal laufen können, ohne etwas zu ändern.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO meta (key, value) VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;
