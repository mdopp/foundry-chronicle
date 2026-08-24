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

Dass das auch dann gilt, wenn eine Spur aus mehreren **Dateien** besteht, ist keine
Selbstverständlichkeit, sondern eine Zusage einer Stufe davor: der Mitschnitt läuft seit
#217 in Häppchen, jede Datei fängt für den Erkenner wieder bei null an, und
``transcribe.service.segment_rows`` schlägt ``recording.offset_ms`` auf, bevor gespeichert
wird. Hier kommt deshalb weiter eine Zeitachse an. Wer dort etwas ändert, ändert das hier
mit — und zwar still.

Zusammengeführt werden allein die Spuren des Bots — an ihnen hängt eine Discord-Id. Ein
Diktat vom Heimweg hat keinen Bezug zu einer Sitzungsuhr; es auf dieselbe Achse zu legen
hieße, einen Zeitstempel zu erfinden. Für die Präsenzrunde bleibt die Szenenfolge die
einzige Zeitachse und »Diktat zur Szene übernehmen« der Weg dorthin.

Eine Szene bekommt ein Diktat trotzdem, nur über einen anderen Anker: den Zeitpunkt der
**Nachricht**, mit der es im Thread ankam (``recording.message_at``, #160). Das ist
dieselbe Regel, nach der eine getippte Notiz ihre Szene findet — verschränkt wird ein
Diktat deswegen nicht, und die Sitzungsuhr bleibt unberührt.

Beschriftet wird aus der festgeschriebenen Zuordnung: von Hand bestätigt oder 1:1 derselbe
Name (#76, Betreiber-Entscheidung vom 2026-08-12 — Gleichheit ist ein Beleg, keine
Vermutung). Sonst steht der Discord-Name da — nie ein Vorschlag, nie ein geratener Name,
und nie eine Ähnlichkeit. Und wenn auch der fehlt, steht dort das Wort »unbekannt« und
**nie der Dateiname** (#250): eine Spur heißt nach ihrer Quellenkennung, ein Mensch nicht.

**``marke_ms``, ``span``, ``note_text`` und ``LEERE_SPANNE`` haben seit #157 keinen
Aufrufer in ``src/``** — sie trugen den Handschnitt der Weboberfläche, mit dem ein
Abschnitt zwischen zwei Zeitmarken von Hand in eine Szene gelegt wurde. Sie stehen hier
absichtlich weiter: der Handschnitt kommt wieder, wenn das Produkt ausgebaut wird
(Betreiber-Entscheidung 2026-08-13 zu #163 — erst muss sich zeigen, wie oft die
automatische Verteilung danebenliegt). Bis dahin halten die Tests sie ehrlich. Wer hier
aufräumt, entfernt keine vergessene Leiche, sondern eine Vorhaltung — und sollte vorher
#163 lesen.

Was hier herauskommt, geht als **Notiz** in eine Szene: dieselbe Form, die die Eingabe am
Tisch liefert. Damit bleibt die Komposition unverändert und es gibt weiterhin eine
Pipeline.

Den Weg dorthin geht ``uebernehmen`` von selbst, seit Discord die Oberfläche ist (#140):
die Sitzungsuhr wird auf die Szenengrenzen gelegt, und jede Äußerung fällt in die Szene
ihres **Startzeitpunkts**. Das setzt voraus, dass der Nullpunkt der Uhr in der Datenbank
steht — ``recording.started_at``. Wo er fehlt, geschieht nichts: eine Zuordnung ohne
festgehaltenen Beginn wäre geraten, und geraten wird hier nichts.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from chronicle import consent, db, lebenszyklus, notes, people
from chronicle.runde import Runde
from chronicle.transcribe.service import zeitmarke

UNLESBAR = "“{marke}” is not a timestamp — 1:02:03 or 02:03 is expected."

LEERE_SPANNE = "In diesem Abschnitt steht nichts — die Zeitmarken stehen vor den Zeilen."

# Woran eine übernommene Notiz erkannt wird. Sie ist abgeleitet und wird bei jedem Lauf
# ersetzt; was ein Mensch geschrieben hat, trägt das nicht und bleibt liegen.
TRANSKRIPT = "transkript"

# Sortiert wird nach Zeit, nicht nach Spur: genau das ist die Verschränkung. Die Spur
# entscheidet nur bei gleicher Millisekunde, damit die Reihenfolge wiederholbar bleibt.
SPUREN = (
    "SELECT r.discord_user_id AS discord_user_id, r.discord_name AS discord_name, "
    "s.start_ms AS start_ms, s.end_ms AS end_ms, s.text AS text "
    "FROM transcript t "
    "JOIN recording r ON r.runde_id = t.runde_id AND r.session_id = t.session_id "
    "AND r.source = t.source "
    "JOIN transcript_segment s ON s.transcript_id = t.id "
    "WHERE t.runde_id = ? AND t.session_id = ? AND r.discord_user_id IS NOT NULL "
)

# Ein Diktat aus dem Thread trägt eine Discord-Id — die des Absenders — und hat trotzdem
# keine Sitzungsuhr. Wer die Uhr braucht, verlangt deshalb den festgehaltenen Beginn.
MIT_UHR = "AND r.started_at IS NOT NULL "

ORDNUNG = "ORDER BY s.start_ms, r.source, s.id"

# Die Gegenprobe zu ``MIT_UHR``: kein festgehaltener Beginn, dafür der Zeitpunkt der
# Nachricht. Sortiert wird innerhalb einer Spur, denn ein Diktat wird als **ein** Stück
# übernommen — es gibt keine zweite Stimme, mit der es zu verschränken wäre.
DIKTATE = (
    "SELECT r.id AS recording_id, r.discord_user_id AS discord_user_id, "
    "r.discord_name AS discord_name, r.message_at AS message_at, s.text AS text "
    "FROM transcript t "
    "JOIN recording r ON r.runde_id = t.runde_id AND r.session_id = t.session_id "
    "AND r.source = t.source "
    "JOIN transcript_segment s ON s.transcript_id = t.id "
    "WHERE t.runde_id = ? AND t.session_id = ? "
    "AND r.started_at IS NULL AND r.message_at IS NOT NULL "
    "ORDER BY r.id, s.start_ms, s.id"
)

# Der Nullpunkt der Sitzungsuhr. Mehrere Spuren eines Mitschnitts tragen denselben Wert;
# genommen wird der früheste, damit die Achse dort beginnt, wo die Aufnahme begann.
BEGINN = (
    "SELECT MIN(started_at) AS beginn FROM recording "
    "WHERE runde_id = ? AND session_id = ? AND discord_user_id IS NOT NULL "
    "AND started_at IS NOT NULL"
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


@dataclass(frozen=True)
class Diktat:
    """Ein verschriftetes Diktat und der Zeitpunkt, der ihm seine Szene gibt.

    Kein ``Utterance``: es liegt auf keiner Sitzungsuhr, und ein ``start_ms`` daran wäre
    genau die erfundene Zeitrechnung, gegen die dieses Modul gebaut ist.
    """

    moment: str
    speaker: str
    text: str

    @property
    def notiz(self) -> str:
        return f"{self.speaker}: {self.text}"


def _beschriftung(sprecher: dict[str, people.Person], zeile: sqlite3.Row) -> tuple[str, bool]:
    """Wie diese Spur zu beschriften ist — und nie nach ihrer Datei.

    Die Reihenfolge ist eine Rangfolge von Belegen: die festgeschriebene Zuordnung, sonst
    der protokollierte Discord-Name, sonst der an der Spur nachgetragene, sonst das Wort
    »unbekannt«. Bis #250 stand am Ende die Quellenkennung — also der Dateiname —, und in
    der Chronik eines echten Abends las sich das als
    ``sitzung4-20260818T201032-unbekannt: Geh doch selber raus``. Ein technischer
    Bezeichner gehört in keinen Text, den Menschen Wochen später als Gedächtnisstütze
    lesen; wo niemand bekannt ist, wird das gesagt und nicht ersatzweise etwas anderes
    hingeschrieben.
    """
    person = sprecher.get(zeile["discord_user_id"])
    if person is None:
        return zeile["discord_name"] or consent.UNBEKANNT, False
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


def conversation(runde: Runde, session_id: int, *, mit_uhr: bool = False) -> tuple[Utterance, ...]:
    """Die Spuren einer Sitzung als eine Unterhaltung — leer, wenn keine mitläuft.

    ``mit_uhr`` verlangt zusätzlich einen festgehaltenen Aufnahmebeginn. Das ist die
    Bedingung für alles, was die Achse *auswertet* statt sie nur anzuzeigen: ein Diktat
    hat keine, und es trotzdem bei Millisekunde 0 einzuhängen hieße, ihm den Beginn der
    Sitzung anzudichten.
    """
    sprecher = people.speakers(runde)
    scope = db.scoped(runde)
    try:
        abfrage = SPUREN + (MIT_UHR if mit_uhr else "") + ORDNUNG
        zeilen = scope.execute(abfrage, (scope.runde_id, session_id)).fetchall()
    finally:
        scope.close()
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


def _moment(nullpunkt: datetime, start_ms: int) -> str:
    return (nullpunkt + timedelta(milliseconds=start_ms)).isoformat(timespec="seconds")


def _nullpunkt(runde: Runde, session_id: int) -> datetime | None:
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(BEGINN, (scope.runde_id, session_id)).fetchone()
    finally:
        scope.close()
    if zeile is None or zeile["beginn"] is None:
        return None
    return datetime.fromisoformat(zeile["beginn"])


def _diktate(runde: Runde, session_id: int) -> tuple[Diktat, ...]:
    """Die verschrifteten Diktate einer Sitzung, je Spur eines — beschriftet wie eine Spur."""
    sprecher = people.speakers(runde)
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(DIKTATE, (scope.runde_id, session_id)).fetchall()
    finally:
        scope.close()
    je_spur: dict[int, list[sqlite3.Row]] = {}
    for zeile in zeilen:
        je_spur.setdefault(zeile["recording_id"], []).append(zeile)
    gefunden: list[Diktat] = []
    for teile in je_spur.values():
        text = " ".join(teil["text"].strip() for teil in teile if teil["text"].strip())
        if not text:
            continue
        name, _ = _beschriftung(sprecher, teile[0])
        gefunden.append(Diktat(moment=teile[0]["message_at"], speaker=name, text=text))
    return tuple(gefunden)


def uebernehmen(runde: Runde, session_id: int) -> int:
    """Legt das Gespräch der Sitzung in die Szenen und sagt, wie viele Notizen entstanden.

    Der Übergang, ohne den kein gesprochenes Wort die Chronik erreicht (#140). Die Regel
    ist die von ``notes.scene_at``: eine Äußerung gehört in die Szene ihres
    **Startzeitpunkts**, auch wenn sie über eine Trennlinie hinausredet — geteilt würde
    sonst mitten im Satz, und die Hälfte danach stünde ohne ihren Anfang da.

    Für den Mitschnitt geschieht nichts, solange der Nullpunkt der Sitzungsuhr fehlt: ohne
    ihn ließe sich ``start_ms`` nur auf die Szenen legen, indem man einen Beginn schätzt.
    Ein **Diktat** kommt trotzdem an — nicht über die Uhr, sondern über den Zeitpunkt
    seiner Nachricht (#160). Beides schreibt dieselbe Herkunft, wird also von demselben
    zweiten Lauf ersetzt statt verdoppelt. Eine ruhende Runde bekommt ohnehin nichts mehr.
    """
    if lebenszyklus.ruht(runde):
        return 0
    nullpunkt = _nullpunkt(runde, session_id)
    diktate = _diktate(runde, session_id)
    if nullpunkt is None and not diktate:
        return 0
    # Erst verwerfen, dann neu legen — und zwar auch dann, wenn nichts mehr übrig ist:
    # sonst überlebte eine Notiz aus einem Lauf, dessen Spuren inzwischen fort sind.
    notes.drop_derived(runde, session_id, TRANSKRIPT)
    je_szene: dict[int, list[Utterance]] = {}
    if nullpunkt is not None:
        for aussage in conversation(runde, session_id, mit_uhr=True):
            szene = notes.scene_at(runde, session_id, _moment(nullpunkt, aussage.start_ms))
            if szene is not None:
                je_szene.setdefault(szene, []).append(aussage)
    geschrieben = 0
    for szene, gruppe in je_szene.items():
        if notes.add_note(runde, szene, note_text(gruppe), origin=TRANSKRIPT) is not None:
            geschrieben += 1
    for diktat in diktate:
        szene = notes.scene_at(runde, session_id, diktat.moment)
        if szene is None:
            continue
        if notes.add_note(runde, szene, diktat.notiz, origin=TRANSKRIPT) is not None:
            geschrieben += 1
    return geschrieben
