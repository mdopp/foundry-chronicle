"""Die abgelegten Protokolle lesen und anzeigen — Chronik und Rückblick.

Ein Protokoll bleibt lokal — nach Foundry zurückschreiben lässt es sich nicht. Diese
Ansicht ist also der Ort, an dem es gefunden wird, und sie liest nur; erzeugt wird im
Stapel über ``python -m chronicle.compose``.

Gerendert wird ausschließlich der Ausschnitt, den ``chronicle.compose`` schreibt: drei
Überschriftsebenen, Aufzählungen, ganzzeilige Kursivschrift und ``code`` in der Zeile.
Eine Markdown-Bibliothek brächte Syntax mit, die hier nie vorkommt.

Der Zweck der Ansicht ist die Trennung: Notizen, belegte Foundry-Fakten und
Verbindungstext behalten eigene Abschnitte mit eigener Klasse, damit Wochen später noch
sichtbar ist, welcher Satz vom Sprachmodell kam und welcher belegt ist. Im Rückblick
kommt die Deutung dazu — die offenen Fäden sind weder belegt noch bloße Überleitung.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from markupsafe import Markup, escape

from chronicle import db
from chronicle.compose.composer import BELEG_TITEL, NOTIZEN_TITEL, VERBINDUNG_TITEL
from chronicle.compose.recap import CHRONIK_TITEL, FAEDEN_TITEL, HERGANG_TITEL
from chronicle.compose.service import KIND

ABSCHNITTE = {
    NOTIZEN_TITEL.lstrip("# "): "notizen",
    BELEG_TITEL.lstrip("# "): "belegt",
    VERBINDUNG_TITEL.lstrip("# "): "verbindung",
    CHRONIK_TITEL.lstrip("# "): "belegt",
    HERGANG_TITEL.lstrip("# "): "verbindung",
    FAEDEN_TITEL.lstrip("# "): "deutung",
}

UEBERSCHRIFT = re.compile(r"^(#{1,3}) +(\S.*)$")
KURSIV = re.compile(r"^_(.+)_$")
CODE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class Protocol:
    session_id: int
    text: str
    created_at: str

    @property
    def html(self) -> Markup:
        return render(self.text)


@dataclass(frozen=True)
class Entry:
    session_id: int
    played_on: str
    title: str | None = None
    created_at: str | None = None


def _open(database_path: Path) -> sqlite3.Connection:
    return db.connect(database_path)


def stored(database_path: Path, session_id: int, kind: str = KIND) -> Protocol | None:
    connection = _open(database_path)
    try:
        row = connection.execute(
            "SELECT session_id, text, created_at FROM protocol WHERE session_id = ? AND kind = ?",
            (session_id, kind),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return Protocol(session_id=row["session_id"], text=row["text"], created_at=row["created_at"])


def entries(database_path: Path) -> tuple[Entry, ...]:
    connection = _open(database_path)
    try:
        rows = connection.execute(
            "SELECT s.id, s.played_on, s.title, p.created_at FROM session s "
            "LEFT JOIN protocol p ON p.session_id = s.id AND p.kind = ? "
            "ORDER BY s.played_on DESC, s.id DESC",
            (KIND,),
        ).fetchall()
    finally:
        connection.close()
    return tuple(
        Entry(
            session_id=r["id"],
            played_on=r["played_on"],
            title=r["title"],
            created_at=r["created_at"],
        )
        for r in rows
    )


def _inline(text: str) -> str:
    return CODE.sub(r"<code>\1</code>", str(escape(text)))


def render(text: str) -> Markup:
    aus: list[str] = []
    liste = False
    abschnitt = False

    def liste_schliessen() -> None:
        nonlocal liste
        if liste:
            aus.append("</ul>")
            liste = False

    def abschnitt_schliessen() -> None:
        nonlocal abschnitt
        if abschnitt:
            aus.append("</section>")
            abschnitt = False

    for rohzeile in text.splitlines():
        zeile = rohzeile.strip()
        if not zeile:
            liste_schliessen()
            continue
        kopf = UEBERSCHRIFT.match(zeile)
        if kopf is not None:
            liste_schliessen()
            abschnitt_schliessen()
            titel = kopf.group(2)
            klasse = ABSCHNITTE.get(titel)
            if klasse is not None:
                aus.append(f'<section class="abschnitt {klasse}">')
                abschnitt = True
            # h1 gehört der Seite, die Chronik beginnt eine Ebene darunter.
            stufe = len(kopf.group(1)) + 1
            aus.append(f"<h{stufe}>{_inline(titel)}</h{stufe}>")
            continue
        if zeile.startswith("- "):
            if not liste:
                aus.append("<ul>")
                liste = True
            aus.append(f"<li>{_inline(zeile[2:])}</li>")
            continue
        liste_schliessen()
        kursiv = KURSIV.match(zeile)
        if kursiv is not None:
            aus.append(f'<p class="kursiv">{_inline(kursiv.group(1))}</p>')
        else:
            aus.append(f"<p>{_inline(zeile)}</p>")

    liste_schliessen()
    abschnitt_schliessen()
    return Markup("\n".join(aus))
