"""Der Stapellauf: lesen, komponieren, ablegen.

Die Fakten hängen über ``scene_foundry_message`` an der Szene — ohne Zuordnung trägt
die Chronik allein die Notizen. Das ist der erwartete Normalfall einer Präsenzrunde und
kein Fehler: eine Zuordnung zu erraten wäre bereits Erfinden.

Ein zweiter Lauf ersetzt das Protokoll der Sitzung; es gibt je Sitzung genau eine
Chronik und genau einen Rückblick. Der Rückblick liest die abgelegte Chronik, nicht das
Rohmaterial — er ist der zweite Ausgang derselben Komposition.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from chronicle import db, settings
from chronicle.compose import client
from chronicle.compose.client import TextModel
from chronicle.compose.composer import Composition, SceneMaterial, SessionMaterial, compose
from chronicle.compose.recap import Recap, RecapMaterial, recap
from chronicle.config import Config
from chronicle.foundry import store

KIND = "chronik"
RUECKBLICK = "rueckblick"

# So viele frühere Rückblicke gehen in den Aufruf: genug für den Faden über mehrere
# Sitzungen, wenig genug für ein Kontextfenster.
FRUEHERE = 3


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def material(connection: sqlite3.Connection, session_id: int) -> SessionMaterial | None:
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
        "SELECT n.scene_id, n.text FROM note n JOIN scene c ON n.scene_id = c.id "
        "WHERE c.session_id = ? ORDER BY n.id",
        (session_id,),
    ).fetchall()
    fakten = connection.execute(
        "SELECT v.scene_id AS scene_id, m.* FROM scene_foundry_message v "
        "JOIN scene c ON c.id = v.scene_id "
        "JOIN foundry_message m ON m.id = v.message_id "
        "WHERE c.session_id = ? ORDER BY m.timestamp, m.id",
        (session_id,),
    ).fetchall()

    je_szene_notizen: dict[int, list[str]] = {}
    for zeile in notizen:
        je_szene_notizen.setdefault(zeile["scene_id"], []).append(zeile["text"])
    je_szene_fakten: dict[int, list] = {}
    for zeile in fakten:
        je_szene_fakten.setdefault(zeile["scene_id"], []).append(store.message(zeile))

    return SessionMaterial(
        session_id=kopf["id"],
        played_on=kopf["played_on"],
        title=kopf["title"],
        scenes=tuple(
            SceneMaterial(
                position=s["position"],
                title=s["title"],
                notes=tuple(je_szene_notizen.get(s["id"], ())),
                facts=tuple(je_szene_fakten.get(s["id"], ())),
            )
            for s in szenen
        ),
    )


def recap_material(connection: sqlite3.Connection, session_id: int) -> RecapMaterial | None:
    kopf = connection.execute(
        "SELECT s.id, s.played_on, s.title, p.text FROM session s "
        "JOIN protocol p ON p.session_id = s.id AND p.kind = ? WHERE s.id = ?",
        (KIND, session_id),
    ).fetchone()
    if kopf is None:
        return None
    frueher = connection.execute(
        "SELECT p.text FROM protocol p JOIN session s ON s.id = p.session_id "
        "WHERE p.kind = ? AND (s.played_on < ? OR (s.played_on = ? AND s.id < ?)) "
        "ORDER BY s.played_on DESC, s.id DESC LIMIT ?",
        (RUECKBLICK, kopf["played_on"], kopf["played_on"], session_id, FRUEHERE),
    ).fetchall()
    return RecapMaterial(
        session_id=kopf["id"],
        played_on=kopf["played_on"],
        title=kopf["title"],
        chronicle=kopf["text"],
        previous=tuple(zeile["text"] for zeile in frueher),
    )


def save(
    connection: sqlite3.Connection, session_id: int, text: str, at: str, kind: str = KIND
) -> None:
    with connection:
        connection.execute(
            "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (session_id, kind) DO UPDATE SET text = excluded.text, "
            "created_at = excluded.created_at",
            (session_id, kind, text, at),
        )


def compose_session(
    config: Config, session_id: int, *, model: TextModel | None = None
) -> Composition | None:
    db.init(config.database_path)
    connection = db.connect(config.database_path)
    try:
        stoff = material(connection, session_id)
        if stoff is None:
            return None
        gewaehlt = model if model is not None else client.from_config(settings.effective(config))
        ergebnis = compose(stoff, gewaehlt)
        save(connection, session_id, ergebnis.text, _now())
        return ergebnis
    finally:
        connection.close()


def recap_session(
    config: Config, session_id: int, *, model: TextModel | None = None
) -> Recap | None:
    db.init(config.database_path)
    connection = db.connect(config.database_path)
    try:
        stoff = recap_material(connection, session_id)
        if stoff is None:
            return None
        gewaehlt = model if model is not None else client.from_config(settings.effective(config))
        ergebnis = recap(stoff, gewaehlt)
        save(connection, session_id, ergebnis.text, _now(), kind=RUECKBLICK)
        return ergebnis
    finally:
        connection.close()
