from chronicle import db


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
