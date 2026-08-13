"""Ein vorhandenes Notizdokument wird zu Sitzungen, Szenen und Notizen.

Eine Gruppe, die schon länger spielt, hat ihre Notizen meist in **einem** Dokument, ein
Abschnitt je Abend. Dieser Altbestand kommt hier herein — »wie ein Diktat vorheriger
Sitzungen«, nur ohne Ton: der Text wird übernommen, wie er dasteht.

Drei Sätze tragen alles Weitere:

* **Das Sprachmodell kommt hier nicht vor.** Nichts wird zusammengefasst, nichts ergänzt,
  kein Datum erraten. Ein Protokoll, dem man nicht ansieht, welcher Satz erfunden war, ist
  schlimmer als eine Lücke.
* **Die Ebenen sind relativ, nicht fest.** Nicht »``##`` ist eine Sitzung«: die Abende
  trennt die **oberste Ebene, deren Überschriften ein Datum tragen**; alles darunter trennt
  die Szenen. Ein Dokument mit ``#``-Vorspann und ``##`` je Abend und eines ganz ohne
  Vorspann mit ``#`` je Abend ergeben damit dieselben Sitzungen. Das Datum ist dabei das
  belastbarere Kennzeichen als bloße Mehrfachheit: ein Dokument mit **einem** Abend und
  zwei Szenen darunter hat die mehrfache Ebene bei den Szenen, und ein angehängtes
  ``# Anhang`` machte aus dem Titel der Sammlung plötzlich einen Abend.
  Was die Regel **nicht** trägt: ein Vorspann, der selbst ein Datum führt
  (``# Notizen bis 12.03.2026``), ist damit die oberste Ebene mit Datum und zieht die
  Abendebene auf sich — aus dem ganzen Dokument wird ein Abend. Das bleibt so: geraten
  wird nichts, und was entstünde, steht vor dem Anlegen in der Vorschau.
* **Kein Datum, keine Sitzung.** Eine Abendüberschrift ohne lesbares Datum wird benannt
  und übersprungen, nie geraten. Gelesen werden ``12.03.2026`` und ``2026-03-12``; ein
  ``12.03.`` ohne Jahr ist nicht sicher zu lesen und gilt deshalb als kein Datum.
* **Kein Text, keine Sitzung.** Ein Abend, unter dem kein einziger Satz steht, wird ebenso
  benannt und übersprungen (#172). Er trüge nichts in die Chronik und wäre über die Suche
  nicht zu finden — vor allem aber wäre er beim nächsten Mal nicht wiederzuerkennen: die
  Herkunft unten hängt an einer **Notiz**, und ohne Text entsteht keine. Angelegt entstünde
  er bei jedem Einlesen aufs Neue. Ein Satz irgendwo unter dem Abend genügt, auch erst unter
  einer Szenenüberschrift.

Die Herkunft der entstehenden Notizen ist **nicht** die des Transkripts: ``merge`` verwirft
vor jedem Lauf alles Abgeleitete seiner Herkunft und legt es neu. Ein eingelesenes Dokument
ist nicht wiederherstellbar — der zweite ``/chronik fertig`` löschte es. Sie trägt
stattdessen den **Abdruck des Abends**, aus dem sie stammt: derselbe Abschnitt, zweimal
hochgeladen, ist daran wiederzuerkennen und wird nicht ein zweites Mal angelegt. Am Abend
und nicht am ganzen Dokument, weil der wahrscheinliche zweite Anlauf ein **gewachsenes**
Dokument ist: die alten Abschnitte stehen dann unverändert da, der neue kommt hinzu.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from chronicle import db, notes
from chronicle.runde import Runde

HERKUNFT = "dokument"

# Was ein Notizdokument sein darf. Kein Zufall, dass keine Endung dabei ist, die etwas
# anderes als Text enthält: gelesen wird der Inhalt, nicht ein Format.
SUFFIXES = (".md", ".markdown", ".txt")

# Reichlich für fünfzehn Abende Prosa und trotzdem eine Schranke: die Datei liegt beim
# Einlesen ganz im Speicher. Ton hat seine eigene, viel größere Grenze
# (``recordings.MAX_BYTES``) — hier geht es um Text.
MAX_BYTES = 1024 * 1024

UEBERSCHRIFT = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")

DEUTSCH = re.compile(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})(?!\d)")
ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")

# Was zwischen Datum und Titel steht, gehört zu keinem von beiden.
TRENNER = " \t-–—:|·,;"

ABDRUECKE = "SELECT DISTINCT origin AS herkunft FROM note WHERE runde_id = ? AND origin LIKE ?"


@dataclass(frozen=True)
class Szene:
    titel: str | None
    text: str


@dataclass(frozen=True)
class Abend:
    datum: str
    titel: str | None
    szenen: tuple[Szene, ...]
    abdruck: str


@dataclass(frozen=True)
class Aufteilung:
    abende: tuple[Abend, ...] = ()
    # Die Überschriften, die auf der Abend-Ebene stehen und kein Datum tragen. Sie sind
    # kein Fehler des Dokuments, sondern die eine Frage, die die Vorschau stellt.
    ohne_datum: tuple[str, ...] = ()
    # Die Abendüberschriften, unter denen kein Satz steht. Auch sie sind kein Fehler,
    # sondern eine Auskunft der Vorschau — angelegt werden sie nicht (#172).
    ohne_text: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Knoten:
    ebene: int
    ueberschrift: str
    zeilen: tuple[str, ...]


def datum_aus(ueberschrift: str) -> tuple[str | None, str]:
    """Das Datum einer Überschrift und was von ihr als Titel bleibt.

    Ein unmögliches Datum — der 32. eines Monats — ist keines und wird auch nicht zum
    nächstbesten zurechtgebogen.
    """
    for muster, ordnung in ((ISO, (1, 2, 3)), (DEUTSCH, (3, 2, 1))):
        for treffer in muster.finditer(ueberschrift):
            jahr, monat, tag = (int(treffer.group(stelle)) for stelle in ordnung)
            try:
                gelesen = date(jahr, monat, tag)
            except ValueError:
                continue
            rest = ueberschrift[: treffer.start()] + ueberschrift[treffer.end() :]
            return gelesen.isoformat(), rest.strip(TRENNER)
    return None, ueberschrift.strip()


def _absatz(zeilen: Sequence[str]) -> str:
    return "\n".join(zeilen).strip()


def _knoten(text: str) -> tuple[_Knoten, ...]:
    gefunden: list[tuple[int, str, list[str]]] = []
    for zeile in text.splitlines():
        treffer = UEBERSCHRIFT.match(zeile)
        if treffer is None:
            if gefunden:
                gefunden[-1][2].append(zeile)
            continue
        gefunden.append((len(treffer.group(1)), treffer.group(2).strip(), []))
    return tuple(_Knoten(ebene, kopf, tuple(zeilen)) for ebene, kopf, zeilen in gefunden)


def _abendebene(knoten: Sequence[_Knoten]) -> int | None:
    """Die oberste Ebene, auf der ein Datum steht — sonst gibt es hier keine Abende."""
    for ebene in sorted({k.ebene for k in knoten}):
        if any(datum_aus(k.ueberschrift)[0] for k in knoten if k.ebene == ebene):
            return ebene
    return None


def _abdruck(abschnitt: Sequence[_Knoten]) -> str:
    """Woran derselbe Abend wiederzuerkennen ist — ohne seine Ebene.

    Die Ebene bleibt draußen, weil sie nichts über den Abend sagt: dasselbe Dokument
    einmal mit ``#`` und einmal mit ``##`` je Abend ist dasselbe Dokument.
    """
    roh = "\n".join(teil for k in abschnitt for teil in (k.ueberschrift, _absatz(k.zeilen)))
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()[:16]


def lesen(text: str) -> Aufteilung:
    """Ein Dokument in Abende zerlegen — gelesen wird, nichts geschrieben."""
    knoten = _knoten(text)
    ebene = _abendebene(knoten)
    if ebene is None:
        return Aufteilung()
    abschnitte: list[list[_Knoten]] = []
    offen: list[_Knoten] | None = None
    for k in knoten:
        if k.ebene == ebene:
            offen = [k]
            abschnitte.append(offen)
        elif k.ebene < ebene:
            # Ein Vorspann oder ein Anhang über der Abend-Ebene. Er gehört zu keinem Abend,
            # und was unter ihm steht, auch nicht.
            offen = None
        elif offen is not None:
            offen.append(k)

    abende: list[Abend] = []
    ohne_datum: list[str] = []
    ohne_text: list[str] = []
    for abschnitt in abschnitte:
        kopf = abschnitt[0]
        datum, titel = datum_aus(kopf.ueberschrift)
        if datum is None:
            ohne_datum.append(kopf.ueberschrift)
            continue
        szenen: list[Szene] = []
        # Text direkt unter der Abendüberschrift steht in der ersten Szene — die legt
        # ``notes.create_session`` ohnehin an.
        vorspann = _absatz(kopf.zeilen)
        if vorspann:
            szenen.append(Szene(titel=None, text=vorspann))
        for k in abschnitt[1:]:
            szenen.append(Szene(titel=k.ueberschrift or None, text=_absatz(k.zeilen)))
        if not any(szene.text for szene in szenen):
            ohne_text.append(kopf.ueberschrift)
            continue
        abende.append(
            Abend(
                datum=datum,
                titel=titel or None,
                szenen=tuple(szenen),
                abdruck=_abdruck(abschnitt),
            )
        )
    return Aufteilung(
        abende=tuple(abende), ohne_datum=tuple(ohne_datum), ohne_text=tuple(ohne_text)
    )


def herkunft(abend: Abend) -> str:
    return f"{HERKUNFT}:{abend.abdruck}"


def neu(runde: Runde, abende: Sequence[Abend]) -> tuple[Abend, ...]:
    """Die Abende, die in dieser Runde noch nicht stehen — erkannt an ihrem Abdruck."""
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(ABDRUECKE, (scope.runde_id, f"{HERKUNFT}:%")).fetchall()
    finally:
        scope.close()
    schon = {zeile["herkunft"] for zeile in zeilen}
    return tuple(abend for abend in abende if herkunft(abend) not in schon)


def anlegen(runde: Runde, abende: Sequence[Abend]) -> int:
    """Legt Sitzungen, Szenen und Notizen an und sagt, wie viele Abende entstanden sind.

    Die erste Szene eines Abends ist die, die mit der Sitzung entsteht: sie bekommt den
    Namen der ersten Überschrift, statt leer davor stehen zu bleiben.
    """
    for abend in abende:
        sitzung = notes.create_session(runde, played_on=abend.datum, title=abend.titel or "")
        erste = notes.session(runde, sitzung).scenes[0]
        for nummer, szene in enumerate(abend.szenen):
            if nummer:
                ziel = notes.add_scene(runde, sitzung, title=szene.titel or "")
            else:
                ziel = erste.id
                if szene.titel:
                    notes.rename_scene(runde, sitzung, ziel, szene.titel)
            notes.add_note(runde, ziel, szene.text, origin=herkunft(abend))
    return len(abende)
