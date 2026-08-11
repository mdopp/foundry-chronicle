"""Die Runde: der Mandant dieser Instanz.

Eine Instanz bedient mehrere Runden. Der Schlüssel nach außen ist die Discord-Gilde — dort
wird gespielt, dort steht der Bot, von dort kommen Identität und Berechtigungen. Der
Schlüssel nach innen bleibt die ``id``: eine Gilde kann verschwinden, umziehen oder
neu beansprucht werden, die Chronik dahinter überlebt das.

Die ``Runde`` ist zugleich das **Zugriffsrecht**: jede Funktion der Datenschicht nimmt sie
als erstes Argument, und ohne sie kommt man an keine Zeile. Sie trägt deshalb auch den
Pfad zur Datenbank — ein Aufrufer, der beides getrennt weiterreicht, kann sie
auseinanderbringen, und genau das darf hier niemand können.

Der Lebenszyklus — einladen, sperren, nach Frist löschen — steht in
``chronicle.lebenszyklus``. Hier steht, was es braucht, um eine Runde zu finden, eine
anzulegen und ihr anzusehen, ob sie noch spricht.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db


@dataclass(frozen=True)
class Runde:
    id: int
    name: str
    guild_id: str | None
    created_at: str
    database_path: Path
    token: str
    locked_at: str | None = None
    delete_after: str | None = None

    @property
    def gesperrt(self) -> bool:
        """Der Bot ist nicht mehr in der Gilde: es wird nichts mehr abgelegt und nichts
        mehr herausgegeben, bis jemand ihn zurückholt."""
        return bool(self.locked_at)


def instanzweit(funktion):
    """Kennzeichnet einen Aufruf, der der Instanz gehört und keiner Runde.

    Zwei Sorten gibt es: die Schleife über *alle* Runden — die zugesagte Löschfrist gilt
    jeder Stimme auf dieser Box, nicht der einen Gruppe — und der Griff auf etwas, das die
    Box nur einmal hat, etwa das Aufnahmeverzeichnis. Keiner davon liest oder schreibt
    runden-eigene Zeilen; sie rufen die gescopte Schicht auf oder rechnen mit Pfaden.

    Das Kennzeichen steht an der Funktion und nicht in einer Liste anderswo: der
    Isolationstest verlangt sonst die Runde als Argument, und eine Ausnahme soll man dort
    sehen, wo sie gemacht wird.
    """
    funktion.instanzweit = True
    return funktion


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _runde(zeile: sqlite3.Row, database_path: Path) -> Runde:
    return Runde(
        id=int(zeile["id"]),
        name=zeile["name"],
        guild_id=zeile["guild_id"],
        created_at=zeile["created_at"],
        database_path=database_path,
        token=zeile["token"],
        locked_at=zeile["locked_at"],
        delete_after=zeile["delete_after"],
    )


def erste(database_path: Path) -> Runde:
    """Die Runde, die es immer gibt — das stille Ziel der Oberfläche bis zu ihrem Ende.

    Die Weboberfläche kennt keine Runden und wird mit #69 abgeschaltet; bis dahin
    arbeitet sie hier. Ein Aufruf legt sie an, falls die Datenbank frisch ist.
    """
    db.init(database_path)
    connection = db.connect(database_path)
    try:
        with connection:
            runde_id = db.erste_runde_id(connection)
        zeile = connection.execute("SELECT * FROM runde WHERE id = ?", (runde_id,)).fetchone()
    finally:
        connection.close()
    return _runde(zeile, database_path)


def alle(database_path: Path) -> tuple[Runde, ...]:
    connection = db.connect(database_path)
    try:
        zeilen = connection.execute("SELECT * FROM runde ORDER BY id").fetchall()
    finally:
        connection.close()
    return tuple(_runde(zeile, database_path) for zeile in zeilen)


def get(database_path: Path, runde_id: int) -> Runde | None:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute("SELECT * FROM runde WHERE id = ?", (runde_id,)).fetchone()
    finally:
        connection.close()
    return None if zeile is None else _runde(zeile, database_path)


def fuer_gilde(database_path: Path, guild_id: str) -> Runde | None:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute(
            "SELECT * FROM runde WHERE guild_id = ?", (str(guild_id),)
        ).fetchone()
    finally:
        connection.close()
    return None if zeile is None else _runde(zeile, database_path)


def anlegen(database_path: Path, name: str, *, guild_id: str | None = None) -> Runde:
    """Eine neue Runde — mit einem Zufallswert, an dem sie wiederzuerkennen ist.

    Der Wert entscheidet später, ob ein Knopf von vorhin noch dieselbe Runde meint. Die
    ``id`` allein trägt das nicht: SQLite vergibt sie nach einer Löschung wieder, und
    ``created_at`` steht nur auf die Sekunde genau.
    """
    db.init(database_path)
    connection = db.connect(database_path)
    try:
        with connection:
            zeiger = connection.execute(
                "INSERT INTO runde (name, guild_id, created_at, token) VALUES (?, ?, ?, ?)",
                (name.strip() or db.ERSTE_RUNDE, guild_id, _now(), db.kennung()),
            )
        zeile = connection.execute(
            "SELECT * FROM runde WHERE id = ?", (int(zeiger.lastrowid),)
        ).fetchone()
    finally:
        connection.close()
    return _runde(zeile, database_path)
