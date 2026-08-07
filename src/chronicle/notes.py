"""Die Mitschrift: Sitzung, Szene, Notiz.

Geschrieben wird während des Spiels. Deshalb liegt jede Eingabe mit dem Absenden auf
der Platte und nichts davon im Browser — ein Neuladen mitten in der Sitzung darf nichts
kosten.

Die Szenenfolge ist im Präsenzfall die einzige Zeitachse: ``position`` zählt hoch, die
Zeitstempel aus Foundry taugen dafür nicht.

Seit die Sitzung ein Discord-Thread ist, hat eine Szene außerdem einen **Zeitpunkt**: den
ihrer Trennlinie. Eine Notiz gehört in die letzte Szene, deren Trennlinie vor ihr liegt —
nicht in »die aktuelle«. Nur so landet eine Nachricht, die Tage später nachgetragen wird,
dort, wo sie hingehört, statt in der Szene, die gerade zufällig die letzte ist.

Alles hier gehört einer Runde und wird nur über sie erreicht — auch dort, wo eine Id
allein schon eindeutig wäre. Eine Szenen-Id aus einer fremden Runde ist kein Fund,
sondern ein Datenleck.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from chronicle import db
from chronicle.runde import Runde


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


def create_session(
    runde: Runde, *, played_on: str = "", title: str = "", thread_id: str = ""
) -> int:
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "INSERT INTO session (runde_id, played_on, title, created_at, thread_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    played_on.strip() or today(),
                    title.strip() or None,
                    zeitpunkt,
                    str(thread_id).strip() or None,
                ),
            )
            sitzung = int(cursor.lastrowid)
            # Wer eine Sitzung anlegt, will sofort tippen — die erste Szene steht schon da.
            scope.execute(
                "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
                "VALUES (?, ?, 1, NULL, ?)",
                (scope.runde_id, sitzung, zeitpunkt),
            )
        return sitzung
    finally:
        scope.close()


def sessions(runde: Runde) -> tuple[Session, ...]:
    scope = db.scoped(runde)
    try:
        rows = scope.execute(
            "SELECT s.id, s.played_on, s.title, "
            "(SELECT COUNT(*) FROM scene WHERE session_id = s.id) AS szenen, "
            "(SELECT COUNT(*) FROM note n JOIN scene c ON n.scene_id = c.id "
            " WHERE c.session_id = s.id) AS notizen "
            "FROM session s WHERE s.runde_id = ? ORDER BY s.played_on DESC, s.id DESC",
            (scope.runde_id,),
        ).fetchall()
    finally:
        scope.close()
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


def session(runde: Runde, session_id: int) -> Session | None:
    scope = db.scoped(runde)
    try:
        kopf = scope.execute(
            "SELECT id, played_on, title FROM session WHERE runde_id = ? AND id = ?",
            (scope.runde_id, session_id),
        ).fetchone()
        if kopf is None:
            return None
        szenen = scope.execute(
            "SELECT id, position, title FROM scene WHERE runde_id = ? AND session_id = ? "
            "ORDER BY position",
            (scope.runde_id, session_id),
        ).fetchall()
        notizen = scope.execute(
            "SELECT n.id, n.scene_id, n.text, n.created_at FROM note n "
            "JOIN scene c ON n.scene_id = c.id "
            "WHERE n.runde_id = ? AND c.session_id = ? ORDER BY n.id",
            (scope.runde_id, session_id),
        ).fetchall()
    finally:
        scope.close()
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


def latest_session(runde: Runde) -> Session | None:
    """Die zuletzt angelegte Sitzung — das Ziel für alles, was ohne Adresse hereinkommt.

    Nicht die zuletzt gespielte: ein nachgetragenes Datum würde sonst die Zielsitzung
    eines Diktats verschieben, das längst unterwegs ist.
    """
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT id FROM session WHERE runde_id = ? ORDER BY id DESC LIMIT 1",
            (scope.runde_id,),
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else session(runde, int(zeile["id"]))


def session_of_thread(runde: Runde, thread_id: str) -> int | None:
    """Die Sitzung hinter einem Discord-Thread — der Thread *ist* die Sitzung."""
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT id FROM session WHERE runde_id = ? AND thread_id = ?",
            (scope.runde_id, str(thread_id)),
        ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["id"])


def add_scene(runde: Runde, session_id: int, *, title: str = "", at: str = "") -> int | None:
    scope = db.scoped(runde)
    try:
        bekannt = scope.execute(
            "SELECT 1 FROM session WHERE runde_id = ? AND id = ?", (scope.runde_id, session_id)
        ).fetchone()
        if bekannt is None:
            return None
        with scope:
            cursor = scope.execute(
                "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
                "SELECT ?, ?, COALESCE(MAX(position), 0) + 1, ?, ? FROM scene "
                "WHERE session_id = ?",
                (
                    scope.runde_id,
                    session_id,
                    title.strip() or None,
                    at.strip() or _now(),
                    session_id,
                ),
            )
        return int(cursor.lastrowid)
    finally:
        scope.close()


def scene_at(runde: Runde, session_id: int, moment: str) -> int | None:
    """In welche Szene eine Notiz dieses Zeitpunkts gehört.

    Die letzte Trennlinie **vor** dem Zeitpunkt, nicht die zuletzt gezogene: eine
    Nachricht von Dienstag bleibt damit in der Szene von Dienstag, auch wenn sie erst
    Freitag im Thread landet. Liegt sie vor jeder Trennlinie, bleibt die erste Szene —
    eine Notiz ohne Szene gäbe es sonst nicht zu speichern.
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT id FROM scene WHERE runde_id = ? AND session_id = ? AND created_at <= ? "
            "ORDER BY position DESC LIMIT 1",
            (scope.runde_id, session_id, moment),
        ).fetchone()
        if row is None:
            row = scope.execute(
                "SELECT id FROM scene WHERE runde_id = ? AND session_id = ? "
                "ORDER BY position LIMIT 1",
                (scope.runde_id, session_id),
            ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["id"])


def session_of_scene(runde: Runde, scene_id: int) -> int | None:
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT session_id FROM scene WHERE runde_id = ? AND id = ?",
            (scope.runde_id, scene_id),
        ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["session_id"])


def add_note(runde: Runde, scene_id: int, text: str, *, message_id: str = "") -> int | None:
    inhalt = text.strip()
    if not inhalt:
        return None
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        bekannt = scope.execute(
            "SELECT 1 FROM scene WHERE runde_id = ? AND id = ?", (scope.runde_id, scene_id)
        ).fetchone()
        if bekannt is None:
            return None
        with scope:
            cursor = scope.execute(
                "INSERT INTO note (runde_id, scene_id, text, created_at, updated_at, "
                "discord_message_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    scene_id,
                    inhalt,
                    zeitpunkt,
                    zeitpunkt,
                    str(message_id).strip() or None,
                ),
            )
        return int(cursor.lastrowid)
    finally:
        scope.close()


def update_note(runde: Runde, message_id: str, text: str) -> bool:
    """Zieht eine im Thread geänderte Nachricht nach."""
    inhalt = text.strip()
    if not inhalt:
        return False
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "UPDATE note SET text = ?, updated_at = ? "
                "WHERE runde_id = ? AND discord_message_id = ?",
                (inhalt, _now(), scope.runde_id, str(message_id)),
            )
        return cursor.rowcount > 0
    finally:
        scope.close()


def remove_note(runde: Runde, message_id: str) -> bool:
    """Was im Thread gelöscht wurde, verschwindet auch hier — sonst hielten wir es fest."""
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "DELETE FROM note WHERE runde_id = ? AND discord_message_id = ?",
                (scope.runde_id, str(message_id)),
            )
        return cursor.rowcount > 0
    finally:
        scope.close()
