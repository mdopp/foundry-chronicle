"""Lokaler Speicher: SQLite aus der Stdlib, kein ORM.

Das Schema wird bei jedem Start angelegt; ``schema.sql`` ist so geschrieben, dass ein
zweiter Lauf nichts ändert.

**Eine Instanz trägt mehrere Runden**, und die Trennung zwischen ihnen ist die wichtigste
Sicherheitseigenschaft dieses Systems: ein vergessenes ``WHERE runde_id = ?`` ist kein
Fehler, sondern ein Datenleck zwischen fremden Kampagnen. Deshalb gibt es keinen rohen
Zugriff mehr auf die runden-eigenen Tabellen — er läuft über ``scoped(runde)``, und
dieser ``Scope`` weist eine Abfrage zurück, die eine solche Tabelle anfasst, ohne die
Runde zu nennen. Das ist eine Reißleine und kein Beweis: was sie fängt, fällt sofort auf,
statt Wochen später in einer fremden Chronik zu stehen.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - nur für die Typprüfung
    from chronicle.runde import Runde

SCHEMA_VERSION = 17

# Der Name der ersten Runde: die Bestände der Entwicklungs-Instanz wandern hier hinein,
# und eine frische Datenbank bekommt sie ebenfalls — die Oberfläche braucht bis zu ihrer
# Abschaltung (#69) eine Runde, ohne nach ihr zu fragen.
ERSTE_RUNDE = "Daggerheart"

# Alles, was einer Runde gehört. Wer hier eine Tabelle vergisst, verliert die Schranke
# für sie — deshalb steht die Liste an einer Stelle und wird vom Isolationstest gegen das
# Schema geprüft.
GESCOPTE_TABELLEN = frozenset(
    {
        "runde_meta",
        "foundry_snapshot",
        "foundry_player",
        "foundry_character",
        "foundry_message",
        "session",
        "scene",
        "note",
        "scene_foundry_message",
        "protocol",
        "transcript",
        "transcript_segment",
        "recording",
        "job",
        "settings",
        "discord_intake",
        "consent_event",
        "consent_member",
        "person_mapping",
        "register_entry",
        "register_mention",
        "search_index",
    }
)

# Diese Werte lagen vor dem Runden-Modell in ``settings`` und gehören der Instanz, nicht
# einer Gruppe — voran der Bot-Token: das ist **unser** Token. Sie wandern nach ``meta``.
INSTANZ_SCHLUESSEL = ("discord_bot_token", "admin_group", "onboarding_done")

# Umgekehrt: diese lagen in ``meta`` und gehören einer Runde. Sie wandern nach
# ``runde_meta``.
RUNDEN_SCHLUESSEL = ("discord_cursor", "foundry_last_error", "foundry_last_error_at")

# Diese Werte wurden gepflegt und werden es nicht mehr. Sie wandern nirgendwohin, sie
# **verschwinden** — das Foundry-Passwort seit #64. Ein Bestand aus der Zeit davor liegt
# im Klartext in der Datei und damit im Backup; ein Wert, den niemand mehr liest, soll
# dort nicht liegen bleiben. Der Löschlauf ist idempotent und ohne Ersatz: das Passwort
# wird ab jetzt beim Abgleich erfragt.
VERWORFENE_SCHLUESSEL = ("foundry_password",)

# ``CREATE TABLE IF NOT EXISTS`` erreicht eine bestehende Tabelle nicht mehr; eine neue
# Spalte muss deshalb einmal nachgetragen werden. Nur additiv — mehr kann und soll dieser
# Weg nicht.
NACHGETRAGEN = (
    ("recording", "deleted_at", "TEXT"),
    ("recording", "discord_user_id", "TEXT"),
    ("protocol", "delivered_at", "TEXT"),
    ("session", "thread_id", "TEXT"),
    ("note", "discord_message_id", "TEXT"),
)

_TABELLENWORT = re.compile(r"\b(?:from|join|into|update)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)

UNGESCOPT = (
    "Abfrage ohne Runde auf {tabellen}: »{sql}«. Runden-eigene Tabellen werden nur über "
    "einen Scope gelesen und geschrieben, und jede Bedingung nennt die runde_id."
)


class UngescopteAbfrage(RuntimeError):
    """Eine Abfrage, die eine runden-eigene Tabelle ohne die Runde anfasst."""


def _pruefen(sql: str) -> None:
    beruehrt = {name.lower() for name in _TABELLENWORT.findall(sql)} & GESCOPTE_TABELLEN
    if beruehrt and "runde_id" not in sql.lower():
        raise UngescopteAbfrage(
            UNGESCOPT.format(tabellen=", ".join(sorted(beruehrt)), sql=" ".join(sql.split()))
        )


class Scope:
    """Der Zugang zu genau einer Runde — die einzige Art, ihre Zeilen anzufassen.

    Er ist absichtlich dünn: dieselbe Handhabung wie eine ``sqlite3.Connection``, damit
    die Datenschicht ihre Abfragen weiter selbst schreibt. Was er hinzufügt, ist die
    Schranke davor.
    """

    def __init__(self, connection: sqlite3.Connection, runde: Runde) -> None:
        self._connection = connection
        self.runde = runde

    @property
    def runde_id(self) -> int:
        return self.runde.id

    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        _pruefen(sql)
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, parameters) -> sqlite3.Cursor:
        _pruefen(sql)
        return self._connection.executemany(sql, parameters)

    def __enter__(self) -> Scope:
        self._connection.__enter__()
        return self

    def __exit__(self, *fehler) -> bool:
        return self._connection.__exit__(*fehler)

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


def schema_sql() -> str:
    return resources.files("chronicle").joinpath("schema.sql").read_text(encoding="utf-8")


def suchindex_sql() -> str:
    return resources.files("chronicle").joinpath("suchindex.sql").read_text(encoding="utf-8")


def connect(database_path: Path) -> sqlite3.Connection:
    """Die rohe Verbindung. Sie gehört dem Schema und der Wanderung — sonst ``scoped``."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL vermeidet »database is locked« bei gleichzeitigen Lese- und Schreibzugriffen —
    # eine wiederholt bezahlte Lektion der Zielplattform (ServiceBay-Standard).
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def scoped(runde: Runde) -> Scope:
    return Scope(connect(runde.database_path), runde)


def _spalten(connection: sqlite3.Connection, tabelle: str) -> list[str]:
    return [zeile["name"] for zeile in connection.execute(f"PRAGMA table_info({tabelle})")]


def _nachtragen(connection: sqlite3.Connection) -> None:
    for tabelle, spalte, typ in NACHGETRAGEN:
        vorhanden = set(_spalten(connection, tabelle))
        if vorhanden and spalte not in vorhanden:
            connection.execute(f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {typ}")


def erste_runde_id(connection: sqlite3.Connection) -> int:
    """Die Runde mit der kleinsten Id — angelegt, falls es noch keine gibt.

    Es gibt immer mindestens eine: die Oberfläche kennt bis #69 keine Runden und bekommt
    diese hier stillschweigend, und die Wanderung braucht ein Ziel für die Bestände.
    """
    zeile = connection.execute("SELECT MIN(id) AS id FROM runde").fetchone()
    if zeile is not None and zeile["id"] is not None:
        return int(zeile["id"])
    zeiger = connection.execute(
        "INSERT INTO runde (name, guild_id, created_at) VALUES (?, NULL, ?)",
        (ERSTE_RUNDE, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    return int(zeiger.lastrowid)


def _umziehen(connection: sqlite3.Connection, runde_id: int) -> None:
    """Was vor dem Runden-Modell im falschen Schlüsselraum lag, findet seinen Platz."""
    for schluessel in INSTANZ_SCHLUESSEL:
        zeile = connection.execute(
            "SELECT value FROM settings WHERE key = ? AND runde_id = ?", (schluessel, runde_id)
        ).fetchone()
        if zeile is None:
            continue
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (schluessel, zeile["value"]),
        )
        connection.execute(
            "DELETE FROM settings WHERE key = ? AND runde_id = ?", (schluessel, runde_id)
        )
    for schluessel in RUNDEN_SCHLUESSEL:
        zeile = connection.execute("SELECT value FROM meta WHERE key = ?", (schluessel,)).fetchone()
        if zeile is None:
            continue
        connection.execute(
            "INSERT INTO runde_meta (runde_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (runde_id, key) DO UPDATE SET value = excluded.value",
            (runde_id, schluessel, zeile["value"]),
        )
        connection.execute("DELETE FROM meta WHERE key = ?", (schluessel,))


def _geheimnisse_verwerfen(connection: sqlite3.Connection) -> None:
    """Ein nicht mehr gepflegter Wert wird gelöscht, nicht stehen gelassen."""
    for schluessel in VERWORFENE_SCHLUESSEL:
        connection.execute("DELETE FROM settings WHERE key = ?", (schluessel,))
        connection.execute("DELETE FROM meta WHERE key = ?", (schluessel,))


def _index_verwerfen(connection: sqlite3.Connection) -> None:
    """Einen Suchindex alter Form samt Triggern verwerfen — er wird gleich neu gebaut.

    Zuerst und nicht zuletzt: ein Trigger aus dem alten Schema schriebe während der
    Wanderung in einen Index, dessen Spalten sich gerade ändern. Passen die Spalten schon,
    passiert hier nichts — eine fts5-Tabelle neu anzulegen kostet einen Wimpernschlag zu
    viel für etwas, das bei jedem Start läuft. Die Trigger-Namen werden gelesen statt
    aufgezählt; eine zweite Liste neben ``suchindex.sql`` liefe ihr davon.
    """
    spalten = _spalten(connection, "search_index")
    if not spalten or "runde_id" in spalten:
        return
    namen = [
        zeile["name"]
        for zeile in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        if "_search_" in zeile["name"]
    ]
    for name in namen:
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    connection.execute("DROP TABLE IF EXISTS search_index")


def _wandern(connection: sqlite3.Connection) -> None:
    """Bestehende Tabellen bekommen ihre ``runde_id``; die Bestände die erste Runde.

    Ein zweiter Lauf findet nichts mehr zu tun — dieselbe Zusage wie beim übrigen Schema.
    SQLite kann eine Spalte nicht nachträglich in einen Primärschlüssel aufnehmen, die
    Tabelle wird deshalb umbenannt, neu gebaut und umgefüllt. ``legacy_alter_table`` hält
    das Umbenennen davon ab, die Fremdschlüssel der Kindtabellen mitzuziehen.
    """
    veraltet = [
        tabelle
        for tabelle in sorted(GESCOPTE_TABELLEN)
        if (spalten := _spalten(connection, tabelle)) and "runde_id" not in spalten
    ]
    if not veraltet:
        return
    connection.execute("PRAGMA legacy_alter_table = ON")
    connection.execute("PRAGMA foreign_keys = OFF")
    for tabelle in veraltet:
        connection.execute(f"ALTER TABLE {tabelle} RENAME TO {tabelle}__alt")
    connection.executescript(schema_sql())
    runde_id = erste_runde_id(connection)
    for tabelle in veraltet:
        alt = set(_spalten(connection, f"{tabelle}__alt"))
        gemeinsam = [
            name for name in _spalten(connection, tabelle) if name in alt and name != "runde_id"
        ]
        liste = ", ".join(gemeinsam)
        connection.execute(
            f"INSERT INTO {tabelle} (runde_id, {liste}) SELECT ?, {liste} FROM {tabelle}__alt",
            (runde_id,),
        )
        connection.execute(f"DROP TABLE {tabelle}__alt")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA legacy_alter_table = OFF")


def init(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database_path)
    try:
        _index_verwerfen(connection)
        _wandern(connection)
        # Ein benannter Index wandert beim Umbenennen mit und fällt mit der alten Tabelle;
        # dieser Durchlauf legt ihn an der neuen wieder an — und auf einer frischen
        # Datenbank legt er überhaupt erst alles an.
        connection.executescript(schema_sql())
        _umziehen(connection, erste_runde_id(connection))
        _geheimnisse_verwerfen(connection)
        _nachtragen(connection)
        # Der Suchindex wird abgeleitet und deshalb zuletzt neu gebaut — er liest Spalten,
        # die die Wanderung erst angelegt hat.
        connection.executescript(suchindex_sql())
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
