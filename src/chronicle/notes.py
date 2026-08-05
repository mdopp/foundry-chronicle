"""Die Mitschrift: Sitzung, Szene, Notiz.

Geschrieben wird während des Spiels. Deshalb liegt jede Eingabe mit dem Absenden auf
der Platte und nichts davon im Browser — ein Neuladen mitten in der Sitzung darf nichts
kosten.

Die Szenenfolge ist im Präsenzfall die einzige Zeitachse: ``position`` zählt hoch, die
Zeitstempel aus Foundry taugen dafür nicht.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from chronicle import db


@dataclass(frozen=True)
class Note:
    id: int
    text: str
    created_at: str


@dataclass(frozen=True)
class Scene:
    id: int
    position: int
    title: str | None = None
    notes: tuple[Note, ...] = ()


@dataclass(frozen=True)
class Session:
    id: int
    played_on: str
    title: str | None = None
    scene_count: int = 0
    note_count: int = 0
    scenes: tuple[Scene, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def _open(database_path: Path) -> sqlite3.Connection:
    return db.connect(database_path)


def create_session(database_path: Path, *, played_on: str = "", title: str = "") -> int:
    zeitpunkt = _now()
    connection = _open(database_path)
    try:
        with connection:
            cursor = connection.execute(
                "INSERT INTO session (played_on, title, created_at) VALUES (?, ?, ?)",
                (played_on.strip() or today(), title.strip() or None, zeitpunkt),
            )
            sitzung = int(cursor.lastrowid)
            # Wer eine Sitzung anlegt, will sofort tippen — die erste Szene steht schon da.
            connection.execute(
                "INSERT INTO scene (session_id, position, title, created_at) "
                "VALUES (?, 1, NULL, ?)",
                (sitzung, zeitpunkt),
            )
        return sitzung
    finally:
        connection.close()


def sessions(database_path: Path) -> tuple[Session, ...]:
    connection = _open(database_path)
    try:
        rows = connection.execute(
            "SELECT s.id, s.played_on, s.title, "
            "(SELECT COUNT(*) FROM scene WHERE session_id = s.id) AS szenen, "
            "(SELECT COUNT(*) FROM note n JOIN scene c ON n.scene_id = c.id "
            " WHERE c.session_id = s.id) AS notizen "
            "FROM session s ORDER BY s.played_on DESC, s.id DESC"
        ).fetchall()
    finally:
        connection.close()
    return tuple(
        Session(
            id=r["id"],
            played_on=r["played_on"],
            title=r["title"],
            scene_count=r["szenen"],
            note_count=r["notizen"],
        )
        for r in rows
    )


def session(database_path: Path, session_id: int) -> Session | None:
    connection = _open(database_path)
    try:
        kopf = connection.execute(
            "SELECT id, played_on, title FROM session WHERE id = ?", (session_id,)
        ).fetchone()
        if kopf is None:
            return None
        szenen = connection.execute(
            "SELECT id, position, title FROM scene WHERE session_id = ? ORDER BY position",
            (session_id,),
        ).fetchall()
        notizen = connection.execute(
            "SELECT n.id, n.scene_id, n.text, n.created_at FROM note n "
            "JOIN scene c ON n.scene_id = c.id WHERE c.session_id = ? ORDER BY n.id",
            (session_id,),
        ).fetchall()
    finally:
        connection.close()
    je_szene: dict[int, list[Note]] = {}
    for r in notizen:
        eintrag = Note(id=r["id"], text=r["text"], created_at=r["created_at"])
        je_szene.setdefault(r["scene_id"], []).append(eintrag)
    scenes = tuple(
        Scene(
            id=r["id"],
            position=r["position"],
            title=r["title"],
            notes=tuple(je_szene.get(r["id"], ())),
        )
        for r in szenen
    )
    return Session(
        id=kopf["id"],
        played_on=kopf["played_on"],
        title=kopf["title"],
        scene_count=len(scenes),
        note_count=len(notizen),
        scenes=scenes,
    )


def add_scene(database_path: Path, session_id: int, *, title: str = "") -> int | None:
    connection = _open(database_path)
    try:
        bekannt = connection.execute("SELECT 1 FROM session WHERE id = ?", (session_id,)).fetchone()
        if bekannt is None:
            return None
        with connection:
            cursor = connection.execute(
                "INSERT INTO scene (session_id, position, title, created_at) "
                "SELECT ?, COALESCE(MAX(position), 0) + 1, ?, ? FROM scene WHERE session_id = ?",
                (session_id, title.strip() or None, _now(), session_id),
            )
        return int(cursor.lastrowid)
    finally:
        connection.close()


def session_of_scene(database_path: Path, scene_id: int) -> int | None:
    connection = _open(database_path)
    try:
        row = connection.execute(
            "SELECT session_id FROM scene WHERE id = ?", (scene_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else int(row["session_id"])


def add_note(database_path: Path, scene_id: int, text: str) -> int | None:
    inhalt = text.strip()
    if not inhalt:
        return None
    zeitpunkt = _now()
    connection = _open(database_path)
    try:
        with connection:
            cursor = connection.execute(
                "INSERT INTO note (scene_id, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (scene_id, inhalt, zeitpunkt, zeitpunkt),
            )
        return int(cursor.lastrowid)
    finally:
        connection.close()
