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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from chronicle import db, settings
from chronicle import sprache as sprachen
from chronicle.compose import client
from chronicle.compose.client import ModelError, TextModel
from chronicle.compose.composer import numbers, zitat
from chronicle.compose.nacherzaehlung import REGISTER_ZEILE
from chronicle.compose.service import KIND as CHRONIK
from chronicle.config import Config
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

FIGUR = "figur"
ORT = "ort"
FADEN = "faden"

ARTEN = (FIGUR, ORT, FADEN)

# Beschriftungen der Bedienoberfläche und deshalb englisch (#268); die **Schlüssel**
# daneben stehen so in der Datenbank und werden aus der Modellantwort zurückgelesen.
LABELS = {FIGUR: "Characters", ORT: "Places", FADEN: "Plot threads"}
EINZAHL = {FIGUR: "Character", ORT: "Place", FADEN: "Plot thread"}

VORSCHLAG = "vorschlag"
BESTAETIGT = "bestaetigt"

# Was eine Entscheidung wirklich bewirkt hat. Der Aufrufer sagt der Runde, was hier
# steht — nicht, was er angeklickt hat; alles andere wäre eine Zusage ohne Deckung.
GESCHRIEBEN = "geschrieben"
DOPPELT = "doppelt"
UNVERAENDERT = "unveraendert"

JA = "ja"
NEIN = "nein"

# Die Namen der Formularfelder je Zeile — die Eintrags-Id hängt hinten dran.
FELD = "eintrag-"
NAME_FELD = "name-"
SATZ_FELD = "satz-"

MAX_VORSCHLAEGE = 12

TRENNER = "|"

OHNE_CHRONIK = "Without a chronicle there is nothing to suggest for the register."
OHNE_MODELL = (
    "No model chosen yet — the register got no suggestion. "
    "A model is set up by whoever runs this box."
)
NICHT_ERREICHBAR = (
    "The language model could not be reached — the register got no suggestion; "
    "the next run tries again."
)
NICHTS_NEUES = "The register got no new suggestion."


def anweisung(inhaltssprache: str) -> tuple[str, str]:
    """System und Auftrag in der Sprache der Runde — die Arten bleiben Bezeichner.

    Die Vorschläge sind **Inhalt**: Namen aus dem Spiel und ein Satz dazu, und sie stehen
    später neben der Chronik. Sie folgen deshalb derselben Einstellung wie diese (#268).
    """
    texte = sprachen.register(inhaltssprache)
    return (
        texte.system.format(
            grenze=MAX_VORSCHLAEGE, trenner=TRENNER, figur=FIGUR, ort=ORT, faden=FADEN
        ),
        texte.auftrag,
    )


SZENENTEXT = (
    "SELECT c.id AS scene_id, n.text AS text FROM scene c "
    "JOIN note n ON n.scene_id = c.id WHERE c.runde_id = ? AND c.session_id = ? "
    "UNION ALL "
    "SELECT c.id AS scene_id, COALESCE(m.speaker_alias, '') || ' ' || m.content AS text "
    "FROM scene c JOIN scene_foundry_message v ON v.scene_id = c.id "
    "JOIN foundry_message m ON m.id = v.message_id AND m.runde_id = v.runde_id "
    "WHERE c.runde_id = ? AND c.session_id = ?"
)

EINTRAEGE = (
    "SELECT e.id, e.kind, e.name, e.description, e.state, a.name AS actor_name "
    "FROM register_entry e LEFT JOIN foundry_character a "
    "ON a.id = e.foundry_actor_id AND a.runde_id = e.runde_id "
    "WHERE e.runde_id = ? AND e.state = ? ORDER BY e.kind, e.name"
)

ERWAEHNUNGEN = (
    "SELECT m.entry_id, m.session_id, m.scene_id, s.played_on, s.title, s.kanal_id, c.position "
    "FROM register_mention m JOIN session s ON s.id = m.session_id "
    "LEFT JOIN scene c ON c.id = m.scene_id "
    "WHERE m.runde_id = ? ORDER BY s.played_on, s.id, c.position"
)


@dataclass(frozen=True)
class Mention:
    session_id: int
    played_on: str
    title: str | None = None
    scene_id: int | None = None
    scene_position: int | None = None
    # Der Kanal der Sitzung, in der der Name fiel — der Weg dorthin zurück.
    kanal_id: str | None = None


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
    kind: str = ""


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
            return "1 register suggestion is waiting for confirmation."
        return f"{self.count} register suggestions are waiting for confirmation."


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


def _belegt(
    kandidaten: tuple[Candidate, ...], chronik: str, inhaltssprache: str = sprachen.DEFAULT
) -> tuple[Candidate, ...]:
    """Dieselbe Zahlenschranke wie in der Komposition, nur auf den gespeicherten Satz.

    Und in derselben Sprache: gegen die falschen Zahlwörter geprüft ließe sie ein
    ausgeschriebenes »seventeen« als unbelegt durchgehen oder als belegt stehen.
    """
    belegt = numbers(chronik, inhaltssprache)
    gehalten = []
    for kandidat in kandidaten:
        unbelegt = numbers(f"{kandidat.name} {kandidat.description}", inhaltssprache) - belegt
        if unbelegt:
            logger.warning(
                "Register: Vorschlag %r verworfen, unbelegte Zahlen %s",
                kandidat.name,
                sorted(unbelegt),
            )
            continue
        gehalten.append(kandidat)
    return tuple(gehalten)


def _szenentext(scope: db.Scope, session_id: int) -> dict[int, str]:
    je_szene: dict[int, str] = {}
    werte = (scope.runde_id, session_id, scope.runde_id, session_id)
    for zeile in scope.execute(SZENENTEXT, werte):
        je_szene[zeile["scene_id"]] = je_szene.get(zeile["scene_id"], "") + " " + zeile["text"]
    return {szene: text.casefold() for szene, text in je_szene.items()}


def _aktoren(scope: db.Scope) -> dict[str, str]:
    return {
        zeile["name"].casefold(): zeile["id"]
        for zeile in scope.execute(
            "SELECT id, name FROM foundry_character WHERE runde_id = ?", (scope.runde_id,)
        )
    }


def _ablegen(scope: db.Scope, session_id: int, kandidaten: tuple[Candidate, ...]) -> int:
    je_szene = _szenentext(scope, session_id)
    aktoren = _aktoren(scope)
    zeitpunkt = _now()
    offen = 0
    with scope:
        for kandidat in kandidaten:
            scope.execute(
                "INSERT INTO register_entry "
                "(runde_id, kind, name, description, foundry_actor_id, state, suggested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (runde_id, kind, name) DO UPDATE SET "
                # Ein bestätigter Satz gehört einem Menschen und wird nicht überschrieben.
                "description = CASE WHEN register_entry.state = ? THEN excluded.description "
                "ELSE register_entry.description END, "
                "foundry_actor_id = COALESCE(register_entry.foundry_actor_id, "
                "excluded.foundry_actor_id), suggested_at = excluded.suggested_at",
                (
                    scope.runde_id,
                    kandidat.kind,
                    kandidat.name,
                    kandidat.description,
                    aktoren.get(kandidat.name.casefold()) if kandidat.kind == FIGUR else None,
                    VORSCHLAG,
                    zeitpunkt,
                    VORSCHLAG,
                ),
            )
            zeile = scope.execute(
                "SELECT id, state FROM register_entry WHERE runde_id = ? AND kind = ? AND name = ?",
                (scope.runde_id, kandidat.kind, kandidat.name),
            ).fetchone()
            eintrag_id = int(zeile["id"])
            offen += zeile["state"] == VORSCHLAG
            scope.execute(
                "DELETE FROM register_mention "
                "WHERE runde_id = ? AND entry_id = ? AND session_id = ?",
                (scope.runde_id, eintrag_id, session_id),
            )
            gesucht = kandidat.name.casefold()
            szenen = [szene for szene, text in je_szene.items() if gesucht in text]
            verweise = [(scope.runde_id, eintrag_id, session_id, szene) for szene in szenen]
            scope.executemany(
                "INSERT INTO register_mention (runde_id, entry_id, session_id, scene_id) "
                "VALUES (?, ?, ?, ?)",
                verweise or [(scope.runde_id, eintrag_id, session_id, None)],
            )
    return offen


def suggest(
    config: Config, runde: Runde, session_id: int, *, model: TextModel | None = None
) -> Suggested:
    """Nach der Komposition: was das Modell aus der Chronik als Eintrag vorschlägt.

    Ohne Modell oder ohne Chronik gibt es keine Vorschläge und einen Satz, der sagt warum
    — geraten wird nichts.
    """
    db.init(config.database_path)
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT text FROM protocol WHERE runde_id = ? AND session_id = ? AND kind = ?",
            (scope.runde_id, session_id, CHRONIK),
        ).fetchone()
        if zeile is None:
            return Suggested(reason=OHNE_CHRONIK)
        chronik = zeile["text"]
        schreiber = (
            model if model is not None else client.from_config(settings.effective(config, runde))
        )
        if schreiber is None:
            return Suggested(reason=OHNE_MODELL)
        try:
            inhaltssprache = settings.sprache(runde)
            system, auftrag = anweisung(inhaltssprache)
            # Die Chronik trägt wörtliches Tischgespräch — sie geht als Zitat hinein, der
            # Auftrag steht außerhalb der Marken.
            antwort = schreiber.write(
                system=system, prompt=zitat(chronik.strip()) + f"\n\n{auftrag}"
            )
        except ModelError as fehler:
            logger.warning("Register bleibt ohne neuen Vorschlag: %s", fehler)
            return Suggested(reason=NICHT_ERREICHBAR)
        kandidaten = _belegt(parse(antwort), chronik, inhaltssprache)
        return Suggested(count=_ablegen(scope, session_id, kandidaten))
    finally:
        scope.close()


def _erwaehnungen(scope: db.Scope) -> dict[int, list[Mention]]:
    je_eintrag: dict[int, list[Mention]] = {}
    for zeile in scope.execute(ERWAEHNUNGEN, (scope.runde_id,)):
        je_eintrag.setdefault(zeile["entry_id"], []).append(
            Mention(
                session_id=zeile["session_id"],
                played_on=zeile["played_on"],
                title=zeile["title"],
                scene_id=zeile["scene_id"],
                scene_position=zeile["position"],
                kanal_id=zeile["kanal_id"],
            )
        )
    return je_eintrag


def _lesen(runde: Runde, state: str) -> tuple[Entry, ...]:
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(EINTRAEGE, (scope.runde_id, state)).fetchall()
        je_eintrag = _erwaehnungen(scope)
    finally:
        scope.close()
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


def overview(runde: Runde) -> tuple[Group, ...]:
    """Das Register selbst — nur Bestätigtes, nach Art gruppiert."""
    eintraege = _lesen(runde, BESTAETIGT)
    return tuple(
        Group(
            kind=art,
            label=LABELS[art],
            entries=tuple(e for e in eintraege if e.kind == art),
        )
        for art in ARTEN
        if any(e.kind == art for e in eintraege)
    )


def pending(runde: Runde) -> tuple[Entry, ...]:
    """Was auf ein Ja oder Nein wartet."""
    return _lesen(runde, VORSCHLAG)


def nach_sitzung(runde: Runde) -> dict[int, tuple[str, ...]]:
    """Was das Register je Sitzung führt — fertige Zeilen, nur Bestätigtes.

    Das ist die Auswahl für die Nacherzählung: eine Sitzung, zu der hier nichts steht, wird
    dort als Lücke benannt statt erzählt. Ein Vorschlag zählt nicht mit — ein unbestätigtes
    Register verfälschte genau den Text, der Wochen später als Gedächtnisstütze gilt.

    Ein Eintrag steht je Sitzung **einmal**: die Verweise sind szenenweise, und dreimal
    dieselbe Zeile im Aufruf hieße dem Modell, der Name sei dreimal so wichtig.
    """
    je_sitzung: dict[int, list[str]] = {}
    for gruppe in overview(runde):
        for eintrag in gruppe.entries:
            zeile = REGISTER_ZEILE.format(
                label=eintrag.label, name=eintrag.name, satz=eintrag.description
            )
            for erwaehnung in eintrag.mentions:
                zeilen = je_sitzung.setdefault(erwaehnung.session_id, [])
                if zeile not in zeilen:
                    zeilen.append(zeile)
    return {sitzung: tuple(zeilen) for sitzung, zeilen in je_sitzung.items()}


def _bestaetigen(
    scope: db.Scope, eintrag_id: int, entscheidung: Entscheidung, zeitpunkt: str
) -> str:
    """Ein Ja — und die ehrliche Auskunft, ob es die Zeile wirklich erreicht hat."""
    zeile = scope.execute(
        "SELECT kind, name FROM register_entry WHERE runde_id = ? AND id = ? AND state = ?",
        (scope.runde_id, eintrag_id, VORSCHLAG),
    ).fetchone()
    if zeile is None:
        return UNVERAENDERT
    art = entscheidung.kind.strip() or zeile["kind"]
    name = entscheidung.name.strip() or zeile["name"]
    besetzt = scope.execute(
        "SELECT state FROM register_entry WHERE runde_id = ? AND kind = ? AND name = ? AND id <> ?",
        (scope.runde_id, art, name, eintrag_id),
    ).fetchone()
    if besetzt is not None:
        if besetzt["state"] != BESTAETIGT:
            return UNVERAENDERT
        # Der Name steht unter dieser Art schon im Register — das Ja ist damit bereits
        # erfüllt, und die zweite Zeile ist nur noch eine Deutung desselben Namens unter
        # einer anderen Art. Sie stehen zu lassen hieße, den Vorschlag für immer offen zu
        # halten: die Bestätigung kommt nie durch, und die Ansicht widerspräche jeder
        # Antwort. Verworfen wird ein *Vorschlag*, nichts Bestätigtes.
        scope.execute(
            "DELETE FROM register_entry WHERE runde_id = ? AND id = ? AND state = ?",
            (scope.runde_id, eintrag_id, VORSCHLAG),
        )
        return DOPPELT
    zeiger = scope.execute(
        "UPDATE OR IGNORE register_entry SET name = ?, "
        "description = COALESCE(NULLIF(?, ''), description), "
        "kind = ?, state = ?, confirmed_at = ? WHERE runde_id = ? AND id = ? AND state = ?",
        (
            name,
            entscheidung.description.strip(),
            art,
            BESTAETIGT,
            zeitpunkt,
            scope.runde_id,
            eintrag_id,
            VORSCHLAG,
        ),
    )
    return GESCHRIEBEN if zeiger.rowcount else UNVERAENDERT


def decide(runde: Runde, auswahl: Mapping[int, Entscheidung]) -> dict[int, str]:
    """Schreibt fest, was ein Mensch entschieden hat; ein Nein verwirft den Vorschlag.

    Nur Zeilen, die noch Vorschlag sind: ein bestätigter Eintrag wird hier nicht erneut
    entschieden.

    Die Art gehört zur Entscheidung: was als Figur vorgeschlagen war und ein Ort ist, wird
    als Ort bestätigt, ohne Umweg über ein Nein und einen zweiten Vorschlag. Genau daraus
    entsteht die Kollision auf ``UNIQUE (runde_id, kind, name)``: dasselbe Modell schlägt
    einen Namen ohne Weiteres unter zwei Arten vor, und beide als Ort zu bestätigen ist eine
    ganz normale Zwei-Klick-Folge (#183).

    Deshalb gibt jede Entscheidung zurück, **was wirklich geschah** — ``GESCHRIEBEN``,
    ``DOPPELT`` oder ``UNVERAENDERT``. Vorher verschluckte ``UPDATE OR IGNORE`` die
    Kollision still, und der Aufrufer meldete trotzdem »steht jetzt im Register«: eine
    Zusage ohne Deckung, unbegrenzt wiederholbar, und die Ansicht darunter listete den
    Eintrag im selben Atemzug weiter als Vorschlag.
    """
    zeitpunkt = _now()
    ergebnis: dict[int, str] = {}
    scope = db.scoped(runde)
    try:
        with scope:
            for eintrag_id, entscheidung in auswahl.items():
                if not entscheidung.ja:
                    zeiger = scope.execute(
                        "DELETE FROM register_entry WHERE runde_id = ? AND id = ? AND state = ?",
                        (scope.runde_id, eintrag_id, VORSCHLAG),
                    )
                    ergebnis[eintrag_id] = GESCHRIEBEN if zeiger.rowcount else UNVERAENDERT
                    continue
                ergebnis[eintrag_id] = _bestaetigen(scope, eintrag_id, entscheidung, zeitpunkt)
    finally:
        scope.close()
    return ergebnis
