"""Die abgelegten Protokolle lesen — Chronik und Rückblick.

Ein Protokoll bleibt lokal — nach Foundry zurückschreiben lässt es sich nicht. Hier wird
es gefunden, und hier wird nur gelesen; erzeugt wird im Stapel über
``python -m chronicle.compose``, zugestellt wird es nach Discord.

Der Text ist Markdown, und er bleibt es: seit #157 geht er als Datei in den Thread, wo
Discord ihn selbst darstellt. Das eigene Rendern der Weboberfläche ist damit fort. Was es
zeigen sollte — dass Notizen, belegte Foundry-Fakten und Verbindungstext getrennt bleiben
— steht als Überschrift im Text selbst und wird auf Textebene geprüft.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronicle import db
from chronicle.compose.service import KIND
from chronicle.runde import Runde


@dataclass(frozen=True)
class Protocol:
    session_id: int
    text: str
    created_at: str


@dataclass(frozen=True)
class Entry:
    session_id: int
    played_on: str
    title: str | None = None
    created_at: str | None = None


def stored(runde: Runde, session_id: int, kind: str = KIND) -> Protocol | None:
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT session_id, text, created_at FROM protocol "
            "WHERE runde_id = ? AND session_id = ? AND kind = ?",
            (scope.runde_id, session_id, kind),
        ).fetchone()
    finally:
        scope.close()
    if row is None:
        return None
    return Protocol(session_id=row["session_id"], text=row["text"], created_at=row["created_at"])


def entries(runde: Runde) -> tuple[Entry, ...]:
    scope = db.scoped(runde)
    try:
        rows = scope.execute(
            "SELECT s.id, s.played_on, s.title, p.created_at FROM session s "
            "LEFT JOIN protocol p ON p.session_id = s.id AND p.kind = ? "
            "WHERE s.runde_id = ? ORDER BY s.played_on DESC, s.id DESC",
            (KIND, scope.runde_id),
        ).fetchall()
    finally:
        scope.close()
    return tuple(
        Entry(
            session_id=r["id"],
            played_on=r["played_on"],
            title=r["title"],
            created_at=r["created_at"],
        )
        for r in rows
    )
