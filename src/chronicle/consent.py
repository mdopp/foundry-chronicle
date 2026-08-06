"""Das Einwilligungsprotokoll: wann angesagt wurde, in welchem Kanal, wer da war.

Das Aufzeichnen des nichtöffentlich gesprochenen Wortes ohne Einwilligung ist strafbar
(§201 StGB). Der Bot macht deshalb eine hörbare Ansage — und weil eine Ansage, die
niemand belegen kann, nichts wert ist, entsteht hier ihr Nachweis.

Abgelegt wird der **Wortlaut**, nicht ein Verweis auf die Konstante im Code: ändert
jemand die Ansage, darf sich das Protokoll vergangener Sitzungen nicht mit ändern.

Gelöscht wird hier nichts automatisch, auch nicht mit der Sitzung — der Nachweis
überlebt sie (``ON DELETE SET NULL``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db

ANSAGE = "ansage"
NACHZUEGLER = "nachzuegler"


@dataclass(frozen=True)
class Member:
    id: str
    name: str


@dataclass(frozen=True)
class Event:
    id: int
    session_id: int | None
    kind: str
    announced_at: str
    guild_id: str
    channel_id: str
    channel_name: str
    text: str
    members: tuple[Member, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record(
    database_path: Path,
    *,
    session_id: int | None,
    kind: str,
    guild_id: str,
    channel_id: str,
    channel_name: str,
    text: str,
    members: tuple[Member, ...],
) -> int:
    connection = db.connect(database_path)
    try:
        with connection:
            cursor = connection.execute(
                "INSERT INTO consent_event (session_id, kind, announced_at, guild_id, "
                "channel_id, channel_name, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, kind, _now(), guild_id, channel_id, channel_name, text),
            )
            kennung = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO consent_member (event_id, user_id, name) VALUES (?, ?, ?) "
                "ON CONFLICT (event_id, user_id) DO UPDATE SET name = excluded.name",
                [(kennung, mitglied.id, mitglied.name) for mitglied in members],
            )
    finally:
        connection.close()
    return kennung


def for_session(database_path: Path, session_id: int) -> tuple[Event, ...]:
    connection = db.connect(database_path)
    try:
        zeilen = connection.execute(
            "SELECT * FROM consent_event WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        return tuple(_mit_anwesenden(connection, zeile) for zeile in zeilen)
    finally:
        connection.close()


def _mit_anwesenden(connection, zeile) -> Event:
    anwesend = connection.execute(
        "SELECT user_id, name FROM consent_member WHERE event_id = ? ORDER BY user_id",
        (zeile["id"],),
    ).fetchall()
    return Event(
        id=zeile["id"],
        session_id=zeile["session_id"],
        kind=zeile["kind"],
        announced_at=zeile["announced_at"],
        guild_id=zeile["guild_id"],
        channel_id=zeile["channel_id"],
        channel_name=zeile["channel_name"],
        text=zeile["text"],
        members=tuple(Member(id=z["user_id"], name=z["name"]) for z in anwesend),
    )
