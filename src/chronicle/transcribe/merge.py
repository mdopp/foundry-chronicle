"""Aus getrennten Spuren wird eine Unterhaltung zurück.

Der Aufnahme-Bot legt je Sprecher eine Spur ab, und jede Spur wird für sich
transkribiert. Nebeneinander gelegt stünde im Protokoll erst alles von der einen Stimme
und danach alles von der nächsten — fünf Monologe statt einer Runde. Hier werden sie
nach Zeit verschränkt.

**Der gemeinsame Nullpunkt ist der Aufnahmebeginn, nicht die Byte-Position der einzelnen
Spur.** py-cord füllt beim Empfang die Sprechpausen anhand der RTP-Zeitstempel mit Stille
auf (siehe ``chronicle.bot.gateway``); Millisekunde 0 jeder Bot-Spur liegt deshalb auf
demselben Moment, und ``transcript_segment.start_ms`` ist bereits sitzungsabsolut. Ein
zweiter Versatz je Spur wäre eine geratene Zeitrechnung neben der vorhandenen.

Zusammengeführt werden allein die Spuren des Bots — an ihnen hängt eine Discord-Id. Ein
Diktat vom Heimweg hat keinen Bezug zu einer Sitzungsuhr; es auf dieselbe Achse zu legen
hieße, einen Zeitstempel zu erfinden. Für die Präsenzrunde bleibt die Szenenfolge die
einzige Zeitachse und »Diktat zur Szene übernehmen« der Weg dorthin.

Beschriftet wird aus der bestätigten Zuordnung. Ohne Bestätigung steht der Discord-Name
da — nie ein Vorschlag, nie ein geratener Name.

Was hier herauskommt, geht als **Notiz** in eine Szene: dieselbe Form, die die Eingabe am
Tisch liefert. Damit bleibt die Komposition unverändert und es gibt weiterhin eine
Pipeline.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from chronicle import db, people
from chronicle.transcribe.service import zeitmarke

UNLESBAR = "»{marke}« ist keine Zeitmarke — erwartet wird 1:02:03 oder 02:03."

LEERE_SPANNE = "In diesem Abschnitt steht nichts — die Zeitmarken stehen vor den Zeilen."

# Sortiert wird nach Zeit, nicht nach Spur: genau das ist die Verschränkung. Die Spur
# entscheidet nur bei gleicher Millisekunde, damit die Reihenfolge wiederholbar bleibt.
SPUREN = (
    "SELECT r.discord_user_id AS discord_user_id, r.source AS source, "
    "s.start_ms AS start_ms, s.end_ms AS end_ms, s.text AS text "
    "FROM transcript t "
    "JOIN recording r ON r.session_id = t.session_id AND r.source = t.source "
    "JOIN transcript_segment s ON s.transcript_id = t.id "
    "WHERE t.session_id = ? AND r.discord_user_id IS NOT NULL "
    "ORDER BY s.start_ms, r.source, s.id"
)


@dataclass(frozen=True)
class Utterance:
    """Ein Redebeitrag auf der Sitzungsuhr — Millisekunden ab Aufnahmebeginn."""

    start_ms: int
    end_ms: int
    speaker: str
    text: str
    zugeordnet: bool = False

    @property
    def marke(self) -> str:
        return zeitmarke(self.start_ms / 1000)


def _beschriftung(sprecher: dict[str, people.Person], zeile: sqlite3.Row) -> tuple[str, bool]:
    person = sprecher.get(zeile["discord_user_id"])
    if person is None:
        return zeile["source"], False
    if person.confirmed is None:
        return person.discord_name, False
    return person.confirmed.name, True


def _verschraenken(
    zeilen: Iterable[sqlite3.Row], sprecher: dict[str, people.Person]
) -> tuple[Utterance, ...]:
    aussagen: list[Utterance] = []
    zuletzt: str | None = None
    for zeile in zeilen:
        text = zeile["text"].strip()
        if not text:
            continue
        name, zugeordnet = _beschriftung(sprecher, zeile)
        if aussagen and zuletzt == zeile["discord_user_id"]:
            vorige = aussagen[-1]
            aussagen[-1] = replace(
                vorige,
                end_ms=max(vorige.end_ms, zeile["end_ms"]),
                text=f"{vorige.text} {text}",
            )
            continue
        aussagen.append(
            Utterance(
                start_ms=zeile["start_ms"],
                end_ms=zeile["end_ms"],
                speaker=name,
                text=text,
                zugeordnet=zugeordnet,
            )
        )
        zuletzt = zeile["discord_user_id"]
    return tuple(aussagen)


def conversation(database_path: Path, session_id: int) -> tuple[Utterance, ...]:
    """Die Spuren einer Sitzung als eine Unterhaltung — leer, wenn keine mitläuft."""
    sprecher = people.speakers(database_path)
    connection = db.connect(database_path)
    try:
        zeilen = connection.execute(SPUREN, (session_id,)).fetchall()
    finally:
        connection.close()
    return _verschraenken(zeilen, sprecher)


def marke_ms(text: str) -> int | None:
    """``1:02:03``, ``02:03`` oder ``17`` als Millisekunden; leer heißt offen."""
    roh = text.strip()
    if not roh:
        return None
    teile = roh.split(":")
    if len(teile) > 3 or not all(teil.isdigit() for teil in teile):
        raise ValueError(UNLESBAR.format(marke=roh))
    sekunden = 0
    for teil in teile:
        sekunden = sekunden * 60 + int(teil)
    return sekunden * 1000


def span(
    aussagen: Sequence[Utterance], *, von: int | None = None, bis: int | None = None
) -> tuple[Utterance, ...]:
    """Der Abschnitt zwischen zwei Marken — ``bis`` schließt die dort beginnende Zeile ein."""
    return tuple(
        a
        for a in aussagen
        if (von is None or a.start_ms >= von) and (bis is None or a.start_ms <= bis)
    )


def note_text(aussagen: Sequence[Utterance]) -> str:
    """Der Abschnitt als Notiztext — mit Sprecher, ohne Zeitmarke.

    Die Marke bleibt draußen: die Komposition leitet aus Notizen und Fakten ab, welche
    Zahlen belegt sind, und eine Uhrzeit stünde in keinem Foundry-Chat-Log.
    """
    return "\n".join(f"{a.speaker}: {a.text}" for a in aussagen)
