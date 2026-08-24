"""Der Zwischenspeicher. Was hier ankommt, ist bereits gefiltert.

Foundry ist zwischen den Sitzungen oft aus; ohne diesen Stand wäre die Chronik dann leer.
Konten, Figuren und Karten sind **Spiegel**: ihr aktueller Stand steht in Foundry, ein
Abgleich ersetzt sie am Stück.

Chat-Nachrichten sind es nicht. Sie sind **Ereignisse** und werden hinzugefügt und
behalten, Schlüssel ist die Foundry-Id. Das Chat-Log zu leeren ist übliche Praxis der
Spielleitung — ein Spiegel nähme uns damit genau die Würfe weg, die eine Szene belegen.
Die Regel für eine Nachricht, die es noch gibt, ist trotzdem *Foundry gewinnt*: eine dort
korrigierte Nachricht wird hier übernommen, denn die Id ist stabil und meint dasselbe
Ereignis, und ein Archiv, das einer lebenden Quelle widerspricht, wäre kein Beleg. Erst
mit dem Verschwinden friert der zuletzt gesehene Stand ein und bekommt seinen Vermerk;
taucht die Nachricht wieder auf, fällt der Vermerk weg — er beschreibt die Gegenwart.

Genau deshalb braucht die **Testwelt** eine eigene Herkunft (``aus_testwelt``): was hier
liegen bleibt, bleibt für immer liegen, und eine erfundene Zahl, die kein Abgleich mehr
zurücknimmt, ist der teuerste Fehler dieses Projekts. Die Fixture wird gesondert
vorgemerkt und geht beim ersten echten Abgleich wieder heraus.
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
    Scene,
    World,
    WorldSnapshot,
)

LAST_ERROR = "foundry_last_error"
LAST_ERROR_AT = "foundry_last_error_at"

# Die Welt, an die diese Runde gebunden ist. Sie überlebt den Zwischenspeicher: ein
# Abgleich ersetzt den Stand, die Bindung bleibt, bis jemand sie ausdrücklich umhängt.
WORLD_ID = "foundry_world_id"
WORLD_TITLE = "foundry_world_title"

# Die Felder einer Nachricht neben Runde, Id und Herkunftsvermerk. Einmal aufgezählt: die
# Anweisung darunter nennt sie dreimal, und drei von Hand gepflegte Listen laufen einander
# davon.
FELDER = (
    "timestamp",
    "speaker_actor",
    "speaker_alias",
    "content",
    "roll_title",
    "roll_total",
    "roll_formula",
    "roll_kind",
    "roll_critical",
    "roll_modifier_total",
    "roll_dice",
)

NACHRICHT_SCHREIBEN = (
    f"INSERT INTO foundry_message (runde_id, id, vanished_at, aus_testwelt, {', '.join(FELDER)}) "
    f"VALUES (?, ?, NULL, ?, {', '.join('?' * len(FELDER))}) "
    "ON CONFLICT (runde_id, id) DO UPDATE SET vanished_at = NULL, "
    "aus_testwelt = excluded.aus_testwelt, "
    + ", ".join(f"{feld} = excluded.{feld}" for feld in FELDER)
)


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
        vanished_at=row["vanished_at"],
    )


def testwelt_raeumen(scope: db.Scope) -> int:
    """Nimmt die eingespielte Fixture aus dem Archiv und sagt, wie viele es waren.

    Die Testwelt ist keine Kampagne — ihre Würfe dürfen die der Runde nicht überleben. Mit
    der Nachricht geht ihre Auswahl in einer Szene: eine Zeile, die auf eine gelöschte
    Nachricht zeigt, wäre eine Belegstelle ohne Beleg.
    """
    runde_id = scope.runde_id
    with scope:
        scope.execute(
            "DELETE FROM scene_foundry_message WHERE runde_id = ? AND message_id IN "
            "(SELECT id FROM foundry_message WHERE runde_id = ? AND aus_testwelt = 1)",
            (runde_id, runde_id),
        )
        geraeumt = scope.execute(
            "DELETE FROM foundry_message WHERE runde_id = ? AND aus_testwelt = 1", (runde_id,)
        ).rowcount
    return max(geraeumt, 0)


def save(scope: db.Scope, snapshot: WorldSnapshot, *, testwelt: bool = False) -> None:
    runde_id = scope.runde_id
    if not testwelt:
        # Vor allem anderen: was aus der Fixture stammt, verschwindet beim ersten echten
        # Abgleich. Sonst stempelte der Durchlauf darunter es als »nicht mehr vorhanden«
        # und machte es damit ununterscheidbar von einem echten, geräumten Wurf.
        testwelt_raeumen(scope)
    with scope:
        for tabelle in ("foundry_player", "foundry_character", "foundry_scene"):
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
            "INSERT INTO foundry_scene (runde_id, id, name, active) VALUES (?, ?, ?, ?)",
            [(runde_id, k.id, k.name, int(k.active)) for k in snapshot.scenes],
        )
        # Erst alles vormerken, dann das Mitgelieferte wieder freistellen. Der Umweg spart
        # die Liste aller mitgelieferten Ids in einer einzigen Bedingung — die stieße bei
        # einer über Jahre gewachsenen Chronik an SQLites Parametergrenze. Wer schon einen
        # Vermerk trägt, behält seinen: er soll den ersten Abgleich ohne diese Nachricht
        # nennen, nicht den letzten.
        #
        # Vorgemerkt wird nur die eigene Herkunft: ein Ausflug in die Testwelt erklärte
        # sonst das Archiv der Runde für verschwunden, obwohl er nie bei ihrem Server war.
        scope.execute(
            "UPDATE foundry_message SET vanished_at = ? "
            "WHERE runde_id = ? AND vanished_at IS NULL AND aus_testwelt = ?",
            (snapshot.fetched_at, runde_id, int(testwelt)),
        )
        scope.executemany(
            NACHRICHT_SCHREIBEN,
            [
                (
                    runde_id,
                    n.id,
                    int(testwelt),
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
    scenes = scope.execute(
        "SELECT * FROM foundry_scene WHERE runde_id = ? ORDER BY name, id", (runde_id,)
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
        scenes=tuple(Scene(id=r["id"], name=r["name"], active=bool(r["active"])) for r in scenes),
    )


def world(scope: db.Scope) -> World | None:
    """Die Welt, an die diese Runde gebunden ist — ``None``, solange keine gebunden ist."""
    rows = scope.execute(
        "SELECT key, value FROM runde_meta WHERE runde_id = ? AND key IN (?, ?)",
        (scope.runde_id, WORLD_ID, WORLD_TITLE),
    ).fetchall()
    werte = {r["key"]: r["value"] for r in rows}
    if not werte.get(WORLD_ID):
        return None
    return World(id=werte[WORLD_ID], title=werte.get(WORLD_TITLE) or werte[WORLD_ID])


def bind_world(scope: db.Scope, gefunden: World) -> None:
    """Bindet die Runde an die gezeigte Welt — eine ohne Kennung bindet nicht.

    Ein Leerstring ist keine Bindung, sondern das Ende der Schranke: ``world`` gäbe danach
    ``World(id='')`` statt ``None`` zurück, und jeder spätere Abgleich käme an der Prüfung
    vorbei, auch einer gegen eine fremde Kampagne (#285). Eine Antwort ohne ``world.id``
    wird deshalb nicht als Wechsel gewertet **und** nicht gemerkt: die gute Bindung von
    vorher bleibt stehen, und ist noch keine da, bleibt es dabei, bis eine Antwort mit
    Kennung kommt.
    """
    if not gefunden.id:
        return
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
