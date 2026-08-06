"""Lokaler Speicher: SQLite aus der Stdlib, kein ORM.

Das Schema wird bei jedem Start angelegt; ``schema.sql`` ist so geschrieben, dass ein
zweiter Lauf nichts ändert.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 11

# ``CREATE TABLE IF NOT EXISTS`` erreicht eine bestehende Tabelle nicht mehr; eine neue
# Spalte muss deshalb einmal nachgetragen werden. Nur additiv — mehr kann und soll dieser
# Weg nicht.
NACHGETRAGEN = (
    ("recording", "deleted_at", "TEXT"),
    ("recording", "discord_user_id", "TEXT"),
)


def schema_sql() -> str:
    return resources.files("chronicle").joinpath("schema.sql").read_text(encoding="utf-8")


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL vermeidet »database is locked« bei gleichzeitigen Lese- und Schreibzugriffen —
    # eine wiederholt bezahlte Lektion der Zielplattform (ServiceBay-Standard).
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _nachtragen(connection: sqlite3.Connection) -> None:
    for tabelle, spalte, typ in NACHGETRAGEN:
        vorhanden = {zeile["name"] for zeile in connection.execute(f"PRAGMA table_info({tabelle})")}
        if vorhanden and spalte not in vorhanden:
            connection.execute(f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {typ}")


def init(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database_path)
    try:
        connection.executescript(schema_sql())
        _nachtragen(connection)
        connection.commit()
    finally:
        connection.close()


def current_schema_version(database_path: Path) -> int | None:
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    finally:
        connection.close()
    return None if row is None else int(row["value"])
