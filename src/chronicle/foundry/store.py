"""Der Zwischenspeicher. Was hier ankommt, ist bereits gefiltert.

Foundry ist zwischen den Sitzungen oft aus; ohne diesen Stand wäre die Chronik dann
leer. Ein Abgleich ersetzt den Stand vollständig — Foundry liefert die Welt am Stück,
also gibt es nichts zusammenzuführen.
"""

from __future__ import annotations

import json
import sqlite3

from chronicle import db
from chronicle.foundry.model import (
    Character,
    ChatMessage,
    Die,
    Player,
    Roll,
    World,
    WorldSnapshot,
)

LAST_ERROR = "foundry_last_error"
LAST_ERROR_AT = "foundry_last_error_at"

# Die Welt, an die diese Runde gebunden ist. Sie überlebt den Zwischenspeicher: ein
# Abgleich ersetzt den Stand, die Bindung bleibt, bis jemand sie ausdrücklich umhängt.
WORLD_ID = "foundry_world_id"
WORLD_TITLE = "foundry_world_title"


def _dice_json(roll: Roll) -> str:
    return json.dumps(
        [{"name": w.name, "faces": w.faces, "value": w.value} for w in roll.dice],
        ensure_ascii=False,
    )


def _roll_row(roll: Roll | None) -> tuple:
    if roll is None:
        return (None, None, None, None, None, None, None)
    return (
        roll.title,
        roll.total,
        roll.formula,
        roll.kind,
        int(roll.critical),
        roll.modifier_total,
        _dice_json(roll),
    )


def _roll(row: sqlite3.Row) -> Roll | None:
    if row["roll_dice"] is None:
        return None
    dice = tuple(
        Die(name=w["name"], faces=w["faces"], value=w["value"])
        for w in json.loads(row["roll_dice"])
    )
    return Roll(
        title=row["roll_title"],
        total=row["roll_total"],
        formula=row["roll_formula"],
        kind=row["roll_kind"],
        critical=bool(row["roll_critical"]),
        modifier_total=row["roll_modifier_total"],
        dice=dice,
    )


def message(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        timestamp=row["timestamp"],
        speaker_actor=row["speaker_actor"],
        speaker_alias=row["speaker_alias"],
        content=row["content"],
        roll=_roll(row),
    )


def save(scope: db.Scope, snapshot: WorldSnapshot) -> None:
    runde_id = scope.runde_id
    with scope:
        for tabelle in ("foundry_player", "foundry_character", "foundry_message"):
            scope.execute(f"DELETE FROM {tabelle} WHERE runde_id = ?", (runde_id,))
        scope.execute(
            "INSERT INTO foundry_snapshot (runde_id, fetched_at, system) VALUES (?, ?, ?) "
            "ON CONFLICT (runde_id) DO UPDATE SET fetched_at = excluded.fetched_at, "
            "system = excluded.system",
            (runde_id, snapshot.fetched_at, snapshot.system),
        )
        scope.executemany(
            "INSERT INTO foundry_player (runde_id, id, name, role, is_gm) VALUES (?, ?, ?, ?, ?)",
            [(runde_id, s.id, s.name, s.role, int(s.is_gm)) for s in snapshot.players],
        )
        scope.executemany(
            "INSERT INTO foundry_character (runde_id, id, name, type, owner_ids, limited) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (runde_id, f.id, f.name, f.type, json.dumps(list(f.owner_ids)), int(f.limited))
                for f in snapshot.characters
            ],
        )
        scope.executemany(
            "INSERT INTO foundry_message (runde_id, id, timestamp, speaker_actor, speaker_alias, "
            "content, roll_title, roll_total, roll_formula, roll_kind, roll_critical, "
            "roll_modifier_total, roll_dice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    runde_id,
                    n.id,
                    n.timestamp,
                    n.speaker_actor,
                    n.speaker_alias,
                    n.content,
                    *_roll_row(n.roll),
                )
                for n in snapshot.messages
            ],
        )
        scope.execute(
            "DELETE FROM runde_meta WHERE runde_id = ? AND key IN (?, ?)",
            (runde_id, LAST_ERROR, LAST_ERROR_AT),
        )


def load(scope: db.Scope) -> WorldSnapshot | None:
    runde_id = scope.runde_id
    kopf = scope.execute(
        "SELECT fetched_at, system FROM foundry_snapshot WHERE runde_id = ?", (runde_id,)
    ).fetchone()
    if kopf is None:
        return None
    players = scope.execute(
        "SELECT * FROM foundry_player WHERE runde_id = ? ORDER BY name", (runde_id,)
    ).fetchall()
    characters = scope.execute(
        "SELECT * FROM foundry_character WHERE runde_id = ? ORDER BY name", (runde_id,)
    ).fetchall()
    messages = scope.execute(
        "SELECT * FROM foundry_message WHERE runde_id = ? ORDER BY timestamp, id", (runde_id,)
    ).fetchall()
    return WorldSnapshot(
        system=kopf["system"],
        fetched_at=kopf["fetched_at"],
        players=tuple(
            Player(id=r["id"], name=r["name"], role=r["role"], is_gm=bool(r["is_gm"]))
            for r in players
        ),
        characters=tuple(
            Character(
                id=r["id"],
                name=r["name"],
                type=r["type"],
                owner_ids=tuple(json.loads(r["owner_ids"])),
                limited=bool(r["limited"]),
            )
            for r in characters
        ),
        messages=tuple(message(r) for r in messages),
    )


def world(scope: db.Scope) -> World | None:
    """Die Welt, an die diese Runde gebunden ist — ``None``, solange keine gebunden ist."""
    rows = scope.execute(
        "SELECT key, value FROM runde_meta WHERE runde_id = ? AND key IN (?, ?)",
        (scope.runde_id, WORLD_ID, WORLD_TITLE),
    ).fetchall()
    werte = {r["key"]: r["value"] for r in rows}
    if WORLD_ID not in werte:
        return None
    return World(id=werte[WORLD_ID], title=werte.get(WORLD_TITLE) or werte[WORLD_ID])


def bind_world(scope: db.Scope, gefunden: World) -> None:
    with scope:
        scope.executemany(
            "INSERT INTO runde_meta (runde_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (runde_id, key) DO UPDATE SET value = excluded.value",
            (
                (scope.runde_id, WORLD_ID, gefunden.id),
                (scope.runde_id, WORLD_TITLE, gefunden.title),
            ),
        )


def record_failure(scope: db.Scope, reason: str, at: str) -> None:
    with scope:
        scope.executemany(
            "INSERT INTO runde_meta (runde_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (runde_id, key) DO UPDATE SET value = excluded.value",
            ((scope.runde_id, LAST_ERROR, reason), (scope.runde_id, LAST_ERROR_AT, at)),
        )


def last_failure(scope: db.Scope) -> tuple[str | None, str | None]:
    rows = scope.execute(
        "SELECT key, value FROM runde_meta WHERE runde_id = ? AND key IN (?, ?)",
        (scope.runde_id, LAST_ERROR, LAST_ERROR_AT),
    ).fetchall()
    werte = {r["key"]: r["value"] for r in rows}
    return werte.get(LAST_ERROR), werte.get(LAST_ERROR_AT)
