"""Der Zwischenspeicher. Was hier ankommt, ist bereits gefiltert.

Foundry ist zwischen den Sitzungen oft aus; ohne diesen Stand wäre die Chronik dann
leer. Ein Abgleich ersetzt den Stand vollständig — Foundry liefert die Welt am Stück,
also gibt es nichts zusammenzuführen.
"""

from __future__ import annotations

import json
import sqlite3

from chronicle.foundry.model import Character, ChatMessage, Die, Player, Roll, WorldSnapshot

LAST_ERROR = "foundry_last_error"
LAST_ERROR_AT = "foundry_last_error_at"


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


def save(connection: sqlite3.Connection, snapshot: WorldSnapshot) -> None:
    with connection:
        for tabelle in ("foundry_player", "foundry_character", "foundry_message"):
            connection.execute(f"DELETE FROM {tabelle}")
        connection.execute(
            "INSERT INTO foundry_snapshot (id, fetched_at, system) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET fetched_at = excluded.fetched_at, "
            "system = excluded.system",
            (snapshot.fetched_at, snapshot.system),
        )
        connection.executemany(
            "INSERT INTO foundry_player (id, name, role, is_gm) VALUES (?, ?, ?, ?)",
            [(s.id, s.name, s.role, int(s.is_gm)) for s in snapshot.players],
        )
        connection.executemany(
            "INSERT INTO foundry_character (id, name, type, owner_ids, limited) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (f.id, f.name, f.type, json.dumps(list(f.owner_ids)), int(f.limited))
                for f in snapshot.characters
            ],
        )
        connection.executemany(
            "INSERT INTO foundry_message (id, timestamp, speaker_actor, speaker_alias, content, "
            "roll_title, roll_total, roll_formula, roll_kind, roll_critical, roll_modifier_total, "
            "roll_dice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (n.id, n.timestamp, n.speaker_actor, n.speaker_alias, n.content, *_roll_row(n.roll))
                for n in snapshot.messages
            ],
        )
        connection.execute(
            "DELETE FROM meta WHERE key IN (?, ?)",
            (LAST_ERROR, LAST_ERROR_AT),
        )


def load(connection: sqlite3.Connection) -> WorldSnapshot | None:
    kopf = connection.execute(
        "SELECT fetched_at, system FROM foundry_snapshot WHERE id = 1"
    ).fetchone()
    if kopf is None:
        return None
    players = connection.execute("SELECT * FROM foundry_player ORDER BY name").fetchall()
    characters = connection.execute("SELECT * FROM foundry_character ORDER BY name").fetchall()
    messages = connection.execute("SELECT * FROM foundry_message ORDER BY timestamp, id").fetchall()
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
        messages=tuple(
            ChatMessage(
                id=r["id"],
                timestamp=r["timestamp"],
                speaker_actor=r["speaker_actor"],
                speaker_alias=r["speaker_alias"],
                content=r["content"],
                roll=_roll(r),
            )
            for r in messages
        ),
    )


def record_failure(connection: sqlite3.Connection, reason: str, at: str) -> None:
    with connection:
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            ((LAST_ERROR, reason), (LAST_ERROR_AT, at)),
        )


def last_failure(connection: sqlite3.Connection) -> tuple[str | None, str | None]:
    rows = connection.execute(
        "SELECT key, value FROM meta WHERE key IN (?, ?)", (LAST_ERROR, LAST_ERROR_AT)
    ).fetchall()
    werte = {r["key"]: r["value"] for r in rows}
    return werte.get(LAST_ERROR), werte.get(LAST_ERROR_AT)
