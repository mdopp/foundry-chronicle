"""Das Register: Figuren, Orte, Handlungsfäden über alle Sitzungen hinweg.

Chroniken sind sitzungsweise, die Geschichte ist es nicht — ohne Gedächtnis über die
Sitzungsgrenze gibt es keine Antwort auf »wer war nochmal der Händler aus Sitzung 4?«.

**Index, kein Wiki.** Ein Eintrag ist ein Name, ein Satz und Verweise. Was über eine
Figur zu wissen ist, steht in Foundry; hier steht nur ein Zeiger darauf. Aus demselben
Grund bleiben die Journals draußen: sie sind GM-Material und stehen nicht einmal im
Zwischenspeicher — verknüpft wird ausschließlich mit dem bereits gefilterten
``foundry_character``, also nie mehr, als der konfigurierte Foundry-Benutzer sehen darf.

Vorschläge entstehen nach der Komposition aus der **fertigen Chronik**, nicht aus dem
Rohmaterial: was dort steht, hat die Zahlenschranke schon passiert. Ein Vorschlag ist
eine Deutung und wird **nie von allein bestätigt** — dasselbe Muster wie die
Personen-Zuordnung. Bestätigt wird in einer Liste mit Ja/Nein je Zeile; das
Richtigstellen von Name und Satz liegt zugeklappt daneben, damit die Liste klein bleibt.
Wird das Bestätigen lästig, wird es übersprungen, und ein unbestätigtes Register
verfälscht das Nacherzählen.

Die Verweise sind nicht gedeutet, sondern gefunden: eine Szene wird genannt, wenn der
Name in ihren Notizen oder Foundry-Fakten wörtlich vorkommt. Ein Handlungsfaden kommt
so meist nur bei seiner Sitzung an — er ist gedeutet und nicht zitiert.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db, settings
from chronicle.compose import client
from chronicle.compose.client import ModelError, TextModel
from chronicle.compose.composer import numbers
from chronicle.compose.service import KIND as CHRONIK
from chronicle.config import Config

logger = logging.getLogger(__name__)

FIGUR = "figur"
ORT = "ort"
FADEN = "faden"

ARTEN = (FIGUR, ORT, FADEN)

LABELS = {FIGUR: "Figuren", ORT: "Orte", FADEN: "Handlungsfäden"}
EINZAHL = {FIGUR: "Figur", ORT: "Ort", FADEN: "Handlungsfaden"}

VORSCHLAG = "vorschlag"
BESTAETIGT = "bestaetigt"

JA = "ja"
NEIN = "nein"

# Die Namen der Formularfelder je Zeile — die Eintrags-Id hängt hinten dran.
FELD = "eintrag-"
NAME_FELD = "name-"
SATZ_FELD = "satz-"

MAX_VORSCHLAEGE = 12

TRENNER = "|"

OHNE_CHRONIK = "Ohne Chronik gibt es fürs Register nichts vorzuschlagen."
OHNE_MODELL = (
    "Noch kein Modell gewählt — das Register bekam keinen Vorschlag. "
    "Ein Modell wählst du in den Einstellungen."
)
NICHT_ERREICHBAR = (
    "Das Sprachmodell war nicht erreichbar — das Register bekam keinen Vorschlag; "
    "beim nächsten Lauf wird es erneut versucht."
)
NICHTS_NEUES = "Das Register bekam keinen neuen Vorschlag."

SYSTEM = (
    "Du führst das Register einer Tisch-Rollenspiel-Runde: Figuren, Orte, "
    "Handlungsfäden. Du benennst nur, was in der Vorlage steht, und erfindest nichts.\n"
    f"- Höchstens {MAX_VORSCHLAEGE} Zeilen, je ein Eintrag pro Zeile.\n"
    f"- Jede Zeile genau so: art {TRENNER} Name {TRENNER} ein Satz.\n"
    f"- Die Art ist {FIGUR}, {ORT} oder {FADEN}.\n"
    "- Der Satz ist kurz und nennt keine Ziffer und keine Zahl.\n"
    "- Erfinde keine Figur, keinen Ort und keinen Faden.\n"
    "- Gibt die Vorlage nichts her, antworte mit: keine\n"
    "- Antworte mit den Zeilen selbst, ohne Überschrift und ohne Vorrede."
)

AUFTRAG = "Nenne die Registereinträge dieser Sitzung."

SZENENTEXT = (
    "SELECT c.id AS scene_id, n.text AS text FROM scene c "
    "JOIN note n ON n.scene_id = c.id WHERE c.session_id = ? "
    "UNION ALL "
    "SELECT c.id AS scene_id, COALESCE(m.speaker_alias, '') || ' ' || m.content AS text "
    "FROM scene c JOIN scene_foundry_message v ON v.scene_id = c.id "
    "JOIN foundry_message m ON m.id = v.message_id WHERE c.session_id = ?"
)

EINTRAEGE = (
    "SELECT e.id, e.kind, e.name, e.description, e.state, a.name AS actor_name "
    "FROM register_entry e LEFT JOIN foundry_character a ON a.id = e.foundry_actor_id "
    "WHERE e.state = ? ORDER BY e.kind, e.name"
)

ERWAEHNUNGEN = (
    "SELECT m.entry_id, m.session_id, m.scene_id, s.played_on, s.title, c.position "
    "FROM register_mention m JOIN session s ON s.id = m.session_id "
    "LEFT JOIN scene c ON c.id = m.scene_id "
    "ORDER BY s.played_on, s.id, c.position"
)


@dataclass(frozen=True)
class Mention:
    session_id: int
    played_on: str
    title: str | None = None
    scene_id: int | None = None
    scene_position: int | None = None


@dataclass(frozen=True)
class Entry:
    id: int
    kind: str
    name: str
    description: str
    state: str = VORSCHLAG
    actor_name: str | None = None
    mentions: tuple[Mention, ...] = ()

    @property
    def label(self) -> str:
        return EINZAHL.get(self.kind, self.kind)


@dataclass(frozen=True)
class Group:
    kind: str
    label: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Candidate:
    kind: str
    name: str
    description: str


@dataclass(frozen=True)
class Entscheidung:
    ja: bool
    name: str = ""
    description: str = ""


@dataclass(frozen=True)
class Suggested:
    count: int = 0
    reason: str | None = None

    @property
    def message(self) -> str:
        if self.reason is not None:
            return self.reason
        if not self.count:
            return NICHTS_NEUES
        if self.count == 1:
            return "1 Vorschlag fürs Register wartet auf Bestätigung."
        return f"{self.count} Vorschläge fürs Register warten auf Bestätigung."


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse(text: str) -> tuple[Candidate, ...]:
    """Die Zeilen des Modells als Kandidaten — alles Unförmige fällt still weg.

    Eine halb verstandene Zeile zu retten hieße raten; der Vorschlag geht dann eben
    verloren, und der nächste Lauf schlägt ihn wieder vor.
    """
    gefunden: list[Candidate] = []
    gesehen: set[tuple[str, str]] = set()
    for rohzeile in text.splitlines():
        teile = [stueck.strip() for stueck in rohzeile.strip().lstrip("-*").split(TRENNER)]
        if len(teile) != 3:
            continue
        art, name, satz = teile[0].casefold(), teile[1], teile[2]
        if art not in ARTEN or not name or not satz:
            continue
        schluessel = (art, name.casefold())
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        gefunden.append(Candidate(kind=art, name=name, description=satz))
    return tuple(gefunden[:MAX_VORSCHLAEGE])


def _belegt(kandidaten: tuple[Candidate, ...], chronik: str) -> tuple[Candidate, ...]:
    """Dieselbe Zahlenschranke wie in der Komposition, nur auf den gespeicherten Satz."""
    belegt = numbers(chronik)
    gehalten = []
    for kandidat in kandidaten:
        unbelegt = numbers(f"{kandidat.name} {kandidat.description}") - belegt
        if unbelegt:
            logger.warning(
                "Register: Vorschlag %r verworfen, unbelegte Zahlen %s",
                kandidat.name,
                sorted(unbelegt),
            )
            continue
        gehalten.append(kandidat)
    return tuple(gehalten)


def _szenentext(connection: sqlite3.Connection, session_id: int) -> dict[int, str]:
    je_szene: dict[int, str] = {}
    for zeile in connection.execute(SZENENTEXT, (session_id, session_id)):
        je_szene[zeile["scene_id"]] = je_szene.get(zeile["scene_id"], "") + " " + zeile["text"]
    return {szene: text.casefold() for szene, text in je_szene.items()}


def _aktoren(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        zeile["name"].casefold(): zeile["id"]
        for zeile in connection.execute("SELECT id, name FROM foundry_character")
    }


def _ablegen(
    connection: sqlite3.Connection,
    session_id: int,
    kandidaten: tuple[Candidate, ...],
) -> int:
    je_szene = _szenentext(connection, session_id)
    aktoren = _aktoren(connection)
    zeitpunkt = _now()
    offen = 0
    with connection:
        for kandidat in kandidaten:
            connection.execute(
                "INSERT INTO register_entry "
                "(kind, name, description, foundry_actor_id, state, suggested_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (kind, name) DO UPDATE SET "
                # Ein bestätigter Satz gehört einem Menschen und wird nicht überschrieben.
                "description = CASE WHEN register_entry.state = ? THEN excluded.description "
                "ELSE register_entry.description END, "
                "foundry_actor_id = COALESCE(register_entry.foundry_actor_id, "
                "excluded.foundry_actor_id), suggested_at = excluded.suggested_at",
                (
                    kandidat.kind,
                    kandidat.name,
                    kandidat.description,
                    aktoren.get(kandidat.name.casefold()) if kandidat.kind == FIGUR else None,
                    VORSCHLAG,
                    zeitpunkt,
                    VORSCHLAG,
                ),
            )
            zeile = connection.execute(
                "SELECT id, state FROM register_entry WHERE kind = ? AND name = ?",
                (kandidat.kind, kandidat.name),
            ).fetchone()
            eintrag_id = int(zeile["id"])
            offen += zeile["state"] == VORSCHLAG
            connection.execute(
                "DELETE FROM register_mention WHERE entry_id = ? AND session_id = ?",
                (eintrag_id, session_id),
            )
            gesucht = kandidat.name.casefold()
            szenen = [szene for szene, text in je_szene.items() if gesucht in text]
            verweise = [(eintrag_id, session_id, szene) for szene in szenen]
            connection.executemany(
                "INSERT INTO register_mention (entry_id, session_id, scene_id) VALUES (?, ?, ?)",
                verweise or [(eintrag_id, session_id, None)],
            )
    return offen


def suggest(config: Config, session_id: int, *, model: TextModel | None = None) -> Suggested:
    """Nach der Komposition: was das Modell aus der Chronik als Eintrag vorschlägt.

    Ohne Modell oder ohne Chronik gibt es keine Vorschläge und einen Satz, der sagt warum
    — geraten wird nichts.
    """
    db.init(config.database_path)
    connection = db.connect(config.database_path)
    try:
        zeile = connection.execute(
            "SELECT text FROM protocol WHERE session_id = ? AND kind = ?",
            (session_id, CHRONIK),
        ).fetchone()
        if zeile is None:
            return Suggested(reason=OHNE_CHRONIK)
        chronik = zeile["text"]
        schreiber = model if model is not None else client.from_config(settings.effective(config))
        if schreiber is None:
            return Suggested(reason=OHNE_MODELL)
        try:
            antwort = schreiber.write(system=SYSTEM, prompt=f"{chronik.strip()}\n\n{AUFTRAG}")
        except ModelError as fehler:
            logger.warning("Register bleibt ohne neuen Vorschlag: %s", fehler)
            return Suggested(reason=NICHT_ERREICHBAR)
        kandidaten = _belegt(parse(antwort), chronik)
        return Suggested(count=_ablegen(connection, session_id, kandidaten))
    finally:
        connection.close()


def _erwaehnungen(connection: sqlite3.Connection) -> dict[int, list[Mention]]:
    je_eintrag: dict[int, list[Mention]] = {}
    for zeile in connection.execute(ERWAEHNUNGEN):
        je_eintrag.setdefault(zeile["entry_id"], []).append(
            Mention(
                session_id=zeile["session_id"],
                played_on=zeile["played_on"],
                title=zeile["title"],
                scene_id=zeile["scene_id"],
                scene_position=zeile["position"],
            )
        )
    return je_eintrag


def _lesen(database_path: Path, state: str) -> tuple[Entry, ...]:
    connection = db.connect(database_path)
    try:
        zeilen = connection.execute(EINTRAEGE, (state,)).fetchall()
        je_eintrag = _erwaehnungen(connection)
    finally:
        connection.close()
    return tuple(
        Entry(
            id=zeile["id"],
            kind=zeile["kind"],
            name=zeile["name"],
            description=zeile["description"],
            state=zeile["state"],
            actor_name=zeile["actor_name"],
            mentions=tuple(je_eintrag.get(zeile["id"], ())),
        )
        for zeile in zeilen
    )


def overview(database_path: Path) -> tuple[Group, ...]:
    """Das Register selbst — nur Bestätigtes, nach Art gruppiert."""
    eintraege = _lesen(database_path, BESTAETIGT)
    return tuple(
        Group(
            kind=art,
            label=LABELS[art],
            entries=tuple(e for e in eintraege if e.kind == art),
        )
        for art in ARTEN
        if any(e.kind == art for e in eintraege)
    )


def pending(database_path: Path) -> tuple[Entry, ...]:
    """Was auf ein Ja oder Nein wartet."""
    return _lesen(database_path, VORSCHLAG)


def decide(database_path: Path, auswahl: Mapping[int, Entscheidung]) -> None:
    """Schreibt fest, was ein Mensch entschieden hat; ein Nein verwirft den Vorschlag.

    Nur Zeilen, die noch Vorschlag sind: ein bestätigter Eintrag wird hier nicht erneut
    entschieden. ``UPDATE OR IGNORE`` lässt eine Zeile stehen, deren richtiggestellter
    Name schon vergeben ist — sie bleibt Vorschlag, statt das Formular scheitern zu lassen.
    """
    zeitpunkt = _now()
    connection = db.connect(database_path)
    try:
        with connection:
            for eintrag_id, entscheidung in auswahl.items():
                if not entscheidung.ja:
                    connection.execute(
                        "DELETE FROM register_entry WHERE id = ? AND state = ?",
                        (eintrag_id, VORSCHLAG),
                    )
                    continue
                connection.execute(
                    "UPDATE OR IGNORE register_entry SET "
                    "name = COALESCE(NULLIF(?, ''), name), "
                    "description = COALESCE(NULLIF(?, ''), description), "
                    "state = ?, confirmed_at = ? WHERE id = ? AND state = ?",
                    (
                        entscheidung.name.strip(),
                        entscheidung.description.strip(),
                        BESTAETIGT,
                        zeitpunkt,
                        eintrag_id,
                        VORSCHLAG,
                    ),
                )
    finally:
        connection.close()
