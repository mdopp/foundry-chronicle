"""Das Einwilligungsprotokoll: wann angesagt wurde, in welchem Kanal, wer da war.

Das Aufzeichnen des nichtöffentlich gesprochenen Wortes ohne Einwilligung ist strafbar
(§201 StGB). Der Bot macht deshalb eine hörbare Ansage — und weil eine Ansage, die
niemand belegen kann, nichts wert ist, entsteht hier ihr Nachweis.

Abgelegt wird der **Wortlaut**, nicht ein Verweis auf die Konstante im Code: ändert
jemand die Ansage, darf sich das Protokoll vergangener Sitzungen nicht mit ändern. Weil
die Ansage kurz ist und für die Einzelheiten auf den Kanal verweist, steht im Eintrag
beides — der gesprochene Satz und die Bedingungen, auf die er verweist (``ansage.PROTOKOLL``).
Ein Eintrag, der auf einen Text zeigt, der später ein anderer sein kann, belegte nichts.

Gelöscht wird hier nichts automatisch, auch nicht mit der Sitzung — der Nachweis
überlebt sie (``ON DELETE SET NULL``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from chronicle import db
from chronicle.runde import Runde

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
    runde: Runde,
    *,
    session_id: int | None,
    kind: str,
    guild_id: str,
    channel_id: str,
    channel_name: str,
    text: str,
    members: tuple[Member, ...],
) -> int:
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "INSERT INTO consent_event (runde_id, session_id, kind, announced_at, guild_id, "
                "channel_id, channel_name, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    session_id,
                    kind,
                    _now(),
                    guild_id,
                    channel_id,
                    channel_name,
                    text,
                ),
            )
            kennung = int(cursor.lastrowid)
            scope.executemany(
                "INSERT INTO consent_member (runde_id, event_id, user_id, name) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (event_id, user_id) DO UPDATE SET name = excluded.name",
                [(scope.runde_id, kennung, mitglied.id, mitglied.name) for mitglied in members],
            )
    finally:
        scope.close()
    return kennung


def for_session(runde: Runde, session_id: int) -> tuple[Event, ...]:
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT * FROM consent_event WHERE runde_id = ? AND session_id = ? ORDER BY id",
            (scope.runde_id, session_id),
        ).fetchall()
        return tuple(_mit_anwesenden(scope, zeile) for zeile in zeilen)
    finally:
        scope.close()


def _mit_anwesenden(scope: db.Scope, zeile) -> Event:
    anwesend = scope.execute(
        "SELECT user_id, name FROM consent_member WHERE runde_id = ? AND event_id = ? "
        "ORDER BY user_id",
        (scope.runde_id, zeile["id"]),
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
