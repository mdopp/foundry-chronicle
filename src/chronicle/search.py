"""Wiederfinden: eine Volltextsuche über das, was ohnehin im Speicher liegt.

»Was war da nochmal mit dem Schwert?« braucht kein Sprachmodell — FTS5 steckt in
SQLite, der Index kostet keine Abhängigkeit und keine GPU.

Gesucht wird mit dem einfachen Tokenizer und Präfix-Suche. Deutsches Stemming ist in
FTS5 mäßig und bleibt draußen, bis jemand es vermisst.

Der Index ist abgeleitete Datenhaltung: Trigger schreiben ihn fort, ``schema.sql`` baut
ihn beim Start neu auf. Hier wird nur gelesen.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from markupsafe import Markup, escape

from chronicle import db
from chronicle.compose.service import KIND, RUECKBLICK
from chronicle.transcribe.service import KIND as TRANSKRIPT

NOTIZ = "notiz"

# Die Reihenfolge ist die Reihenfolge der Ergebnisgruppen auf der Seite. Das Transkript
# steht zuletzt: es ist das Rohmaterial, aus dem die anderen entstanden sind.
LABELS = {
    NOTIZ: "Notizen",
    KIND: "Chronik",
    RUECKBLICK: "Rückblick",
    TRANSKRIPT: "Transkripte",
}

TREFFER = 50

# Der Ausschnitt kommt als Text aus SQLite und wird erst nach dem Maskieren zu Markup.
# Die Marken sind deshalb Steuerzeichen, die in keiner Notiz vorkommen.
AUF, ZU = "\x02", "\x03"

WORT = re.compile(r"\w+")

SUCHE = (
    "SELECT search_index.kind AS kind, search_index.ref_id AS ref_id, "
    "search_index.session_id AS session_id, search_index.scene_id AS scene_id, "
    "session.played_on AS played_on, session.title AS title, "
    f"snippet(search_index, 0, '{AUF}', '{ZU}', ' … ', 12) AS ausschnitt "
    "FROM search_index JOIN session ON session.id = search_index.session_id "
    "WHERE search_index MATCH ? ORDER BY rank LIMIT ?"
)


@dataclass(frozen=True)
class Hit:
    kind: str
    ref_id: int
    session_id: int
    scene_id: int | None
    played_on: str
    title: str | None
    snippet: Markup


@dataclass(frozen=True)
class Group:
    kind: str
    label: str
    hits: tuple[Hit, ...]


@dataclass(frozen=True)
class Result:
    query: str
    groups: tuple[Group, ...]
    indexed: bool


def expression(query: str) -> str:
    """Die Eingabe als FTS5-Ausdruck: jedes Wort ein Präfix, alle zusammen verlangt.

    Nur Wortzeichen überleben — was ein Mensch tippt, ist keine Abfragesprache, und ein
    Anführungszeichen darin wäre sonst ein Syntaxfehler statt einer Suche.
    """
    return " ".join(f'"{wort}"*' for wort in WORT.findall(query))


def _snippet(text: str) -> Markup:
    return escape(text).replace(AUF, Markup("<mark>")).replace(ZU, Markup("</mark>"))


def _groups(rows: list[sqlite3.Row]) -> tuple[Group, ...]:
    je_art: dict[str, list[Hit]] = {}
    for zeile in rows:
        je_art.setdefault(zeile["kind"], []).append(
            Hit(
                kind=zeile["kind"],
                ref_id=zeile["ref_id"],
                session_id=zeile["session_id"],
                scene_id=zeile["scene_id"],
                played_on=zeile["played_on"],
                title=zeile["title"],
                snippet=_snippet(zeile["ausschnitt"]),
            )
        )
    return tuple(
        Group(kind=art, label=LABELS.get(art, art), hits=tuple(je_art[art]))
        for art in LABELS
        if art in je_art
    )


def find(database_path: Path, query: str, *, limit: int = TREFFER) -> Result:
    ausdruck = expression(query)
    connection = db.connect(database_path)
    try:
        gefuellt = connection.execute("SELECT EXISTS (SELECT 1 FROM search_index)").fetchone()[0]
        zeilen = connection.execute(SUCHE, (ausdruck, limit)).fetchall() if ausdruck else []
    finally:
        connection.close()
    return Result(query=query.strip(), groups=_groups(zeilen), indexed=bool(gefuellt))
