"""Die Warteschlange der Spuren: angenommen, wartend, transkribiert.

Ein Diktat aus dem Sitzungs-Thread legt hier einen Job an — dieselbe Warteschlange und
dieselbe Stufe, die auch der Recorder-Bot befüllt. Einen zweiten Verarbeitungsweg gibt es
nicht.

Die Zeile *ist* der Job, ihre ``id`` die Job-Id. Gemeldet wird ihr Zustand und sonst
nichts: ein Balken, der sich füllt, wäre geraten, und geraten wird hier nichts. Der
Lauf beginnt im nächsten Stapel, nicht beim Absenden.

Angenommen wird woanders: der Thread in ``chronicle.bot.chronik``, der Mitschnitt in
``chronicle.bot.recorder``. Beide legen die Datei ab und rufen dann ``enqueue`` — was
leer ankam, kommt hier gar nicht erst an.

**Eine Sprecherspur ist mehrere Zeilen** (#217): der Mitschnitt wird in Häppchen
geschnitten und jedes einzeln eingereiht, damit die Verschriftung schon während der
Sitzung laufen kann. Jede Zeile trägt dann denselben ``started_at`` — den Nullpunkt der
Sitzungsuhr — und ihr eigenes ``offset_ms``, den Abstand ihres Dateianfangs von diesem
Nullpunkt. Alles hier gilt der einzelnen Datei: Frist, Besitzer, Herzschlag und Stand
hängen an der Zeile, nicht am Sprecher.

**Die Aufbewahrungsfrist steht hier**, und zwar als Zahl: der Bot sagt sie im Sprachkanal
zu (``chronicle.bot.ansage``), und derselbe Wert setzt sie durch. Ein Versprechen, das nur
im Ansagetext steht, wäre keins — deshalb formatiert die Ansage ihre Frist aus
``RETENTION_TAGE``, und ``sweep`` räumt danach. Beide können nicht auseinanderlaufen.

Gelöscht wird dabei nur die **Audiodatei**; die Zeile bleibt mit ``deleted_at`` stehen.
Sie ist der ehrliche Teil der Geschichte: dass es die Spur gab, wann sie kam, was aus ihr
wurde — und dass sie nach Frist entfernt wurde und nicht etwa verlorenging.

**Eine Spur auf ``laeuft`` gehört einem Prozess**, und der sagt selbst, ob er noch lebt
(``besitzer``/``herzschlag``, dieselbe Bauart wie bei ``chronicle.jobs``). Das Verschriften
läuft eine gute Stunde je Sitzungsstunde; ein Deploy oder ein OOM mittendrin ließ die Zeile
früher für immer auf ``laeuft`` stehen — ``pending`` sah sie nie wieder an, und nach sieben
Tagen holte die Frist die Audiodatei, ohne dass je ein Transkript entstand (#181). Jetzt
kommt eine verwaiste Zeile in die Warteschlange zurück, und was die Frist ohne Transkript
holt, wird laut gemeldet statt still abgeräumt.

**Und eine gescheiterte Spur bekommt drei Anläufe** (#247). Derselbe Ausgang wie bei #181,
nur eine Tür weiter: ``gescheitert`` hieß für immer, obwohl der Grund jedes Mal
vorübergehend war — der Nachbardienst rechnete gerade an einer anderen Spur und wies die
nächste mit HTTP 500 ab. Die Frist holte den Ton trotzdem. ``pending`` fragt deshalb nicht
mehr nach dem Stand allein, sondern über ``kommt_wieder_dran``; ``unverschriftet`` steht
daneben und zählt auch die aufgegebenen, damit kein Lauf Erfolg meldet, während Ton ohne
Text daliegt.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from werkzeug.utils import secure_filename

from chronicle import db
from chronicle import runde as runden
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

WARTET = "wartet"
LAEUFT = "laeuft"
FERTIG = "fertig"
GESCHEITERT = "gescheitert"

# Die Frist aus der Ansage. Kein Betriebsknopf: wer sie ändert, ändert eine Zusage an
# Menschen, deren Stimme aufgenommen wurde — das ist kein Umgebungsvariablen-Thema.
RETENTION_TAGE = 7

# Wie oft eine Spur scheitern darf, bevor sie liegen bleibt. Drei, weil die gemessenen
# Gründe alle vorübergehend waren — ein beschäftigter Nachbardienst, ein Neustart, ein
# Deploy — und beim nächsten Lauf nicht mehr da sind. Weniger wäre kein Wiederversuch
# (#247: ein einziges HTTP 500 hätte drei Aufnahmen gekostet); mehr hieße, bis zur Frist
# weiterzuprobieren, und dann bliebe die Chronik dieser Sitzung sieben Tage ungeschrieben,
# weil ``nightly._chronik`` einen Abend überspringt, dessen Ton noch kommen kann. Drei
# Anläufe decken drei Nächte ab und lassen vier Tage, in denen die Chronik ohne diese Spur
# entstehen kann — und der Ton liegt bis zur Frist trotzdem noch da.
MAX_VERSUCHE = 3

# Der Bot läuft ohnehin; einmal am Tag nachzusehen kostet nichts und hält die Zusage auch
# in Wochen ohne Stapellauf.
SWEEP_ABSTAND = 24 * 60 * 60

# Gezählt je Sitzung und nicht je Datei: eine Spur wird in Häppchen geschnitten (#217),
# und vier Stunden mit fünf Sprechern sind vierzig Dateien. Vierzigmal denselben Satz zu
# schreiben verdeckt genau das, was daneben steht — und der Stamm eines Spurnamens ist der
# Anzeigename des Sprechers (#194), der hier in einer Meldung stünde, die auch ins Log geht.
NACH_FRIST = "Sitzung {sitzung}: {was} nach {tage} Tagen gelöscht — die Frist aus der Ansage."

# Die Frist gilt auch der Spur, die es nie durch die Verschriftung geschafft hat: sie ist
# eine Zusage an Menschen, deren Stimme aufgenommen wurde, und keine Belohnung für einen
# geglückten Lauf. Aber sie darf nicht **still** greifen — hier geht eine Stunde
# gesprochener Abend, von der es keinen Text gibt und nie einen geben wird.
OHNE_TEXT = (
    "Sitzung {sitzung}: {was} nach {tage} Tagen gelöscht — die Frist aus der Ansage — und "
    "nie verschriftet. Von diesem Ton gibt es keinen Text, und nachholen lässt sich das "
    "nicht."
)

# Was an der Zeile stehen bleibt. Ohne diesen Satz sagte sie »wartet« über eine Aufnahme,
# auf die niemand mehr wartet.
NIE_VERSCHRIFTET = "Die Frist hat die Aufnahme geholt, bevor ein Transkript entstand."

# Der Vermerk an einer Spur, deren Lauf einen Neustart nicht überlebt hat. Er sagt beides:
# was passiert ist und dass nichts verloren ist — die Datei liegt noch da.
UNTERBROCHEN = (
    "Der Lauf wurde unterbrochen, weil der Dienst zwischendurch neu gestartet ist. Die "
    "Aufnahme liegt noch da und steht wieder in der Warteschlange."
)

# Wer dieser Prozess ist — für die Dauer genau eines Starts. Ein Zufallswert und nicht die
# Prozess-Id: die vergibt die Box nach einem Neustart wieder, und ein Neustart ist genau
# der Fall, um den es hier geht. ``chronicle.jobs`` führt denselben Wert für seine eigene
# Tabelle; geteilt wird er nicht, weil ``jobs`` dieses Modul einliest und nicht umgekehrt —
# verglichen wird er ohnehin nur mit sich selbst.
_ICH = uuid4().hex

# Wie oft die laufende Spur ihr Lebenszeichen schreibt, und wie lange ein fremder Prozess
# darauf wartet, bevor er die Zeile für verwaist hält. Der Abstand ist mit Absicht groß:
# eine Lastspitze darf keine laufende Verschriftung als abgestürzt erscheinen lassen — sie
# liefe sonst ein zweites Mal, auf derselben einen CPU.
HERZSCHLAG = 20.0
VERSTUMMT = timedelta(seconds=90)

# Welche Spuren in **diesem** Prozess gerade verschriftet werden. Was hier steht, läuft;
# was fehlt und uns gehört, hat einen Neustart nicht überlebt. Über fremde Zeilen sagt
# diese Liste nichts — dafür ist der Herzschlag da.
_laufend: set[int] = set()
_schloss = threading.RLock()

# Was Sprachmemo-Apps ablegen. Normalisiert wird nichts davon vorab und seit #216 auch
# nichts mehr dekodiert: die Datei geht als Pfad an ``solaris-whisper-batch``, und dessen
# faster-whisper bringt die FFmpeg-Bibliotheken mit. In diesem Image ist keine.
SUFFIXES = (
    ".m4a",
    ".mp3",
    ".aac",
    ".mp4",
    ".ogg",
    ".opus",
    ".oga",
    ".wav",
    ".flac",
    ".webm",
    ".amr",
    ".3gp",
)

# Großzügig: zwei Stunden AAC vom Telefon liegen bei gut hundert Megabyte, und eine
# abgewiesene Sprachnotiz um halb zwölf nachts spricht niemand ein zweites Mal ein.
MAX_BYTES = 512 * 1024 * 1024


class BereitsEingereiht(Exception):
    """Diese Datei liegt schon in der Warteschlange — ein zweiter Versuch wäre keiner."""


# ``recording.filename`` ist die einzige UNIQUE-Bedingung dieser Tabelle; an ihrem Namen
# hängt der Unterschied zwischen »liegt schon da« und einem echten Fehlschlag. Am Text der
# Meldung hinge er nicht: der gehört SQLite und darf sich mit jeder Fassung ändern.
DOPPELT = "SQLITE_CONSTRAINT_UNIQUE"


@dataclass(frozen=True)
class Recording:
    id: int
    session_id: int
    filename: str
    source: str
    uploaded_at: str
    status: str
    detail: str | None = None
    transcript_id: int | None = None
    text: str = ""
    deleted_at: str | None = None
    discord_user_id: str | None = None
    discord_name: str | None = None
    started_at: str | None = None
    message_at: str | None = None
    offset_ms: int = 0
    versuche: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _lebenszeichen() -> str:
    """Feiner als ``_now``: zwei Schläge dürfen sich nicht auf dieselbe Sekunde runden."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _eintrag(row: sqlite3.Row, text: str = "") -> Recording:
    return Recording(
        id=row["id"],
        session_id=row["session_id"],
        filename=row["filename"],
        source=row["source"],
        uploaded_at=row["uploaded_at"],
        status=row["status"],
        detail=row["detail"],
        transcript_id=row["transcript_id"] if "transcript_id" in row.keys() else None,
        text=text,
        deleted_at=row["deleted_at"],
        discord_user_id=row["discord_user_id"],
        discord_name=row["discord_name"] if "discord_name" in row.keys() else None,
        started_at=row["started_at"] if "started_at" in row.keys() else None,
        message_at=row["message_at"] if "message_at" in row.keys() else None,
        offset_ms=row["offset_ms"] if "offset_ms" in row.keys() else 0,
        versuche=row["versuche"] if "versuche" in row.keys() else 0,
    )


def target_path(recordings_dir: Path, session_id: int, name: str) -> Path:
    """Ein sprechender, kollisionsfreier Name im Aufnahmeverzeichnis.

    Der Stamm wird die Quellenkennung der Spur, und die ist je Sitzung eindeutig — zwei
    Diktate derselben Sekunde dürfen einander deshalb nicht überschreiben.
    """
    stamm = secure_filename(Path(name).stem) or "diktat"
    zeit = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    suffix = Path(name).suffix.lower()
    ziel = recordings_dir / f"sitzung{session_id}-{zeit}-{stamm}{suffix}"
    zaehler = 2
    while ziel.exists():
        ziel = recordings_dir / f"sitzung{session_id}-{zeit}-{stamm}-{zaehler}{suffix}"
        zaehler += 1
    return ziel


def enqueue(
    runde: Runde,
    session_id: int,
    filename: str,
    *,
    discord_user_id: str | None = None,
    discord_name: str | None = None,
    started_at: str | None = None,
    message_at: str | None = None,
    offset_ms: int = 0,
) -> Recording:
    """Reiht eine Spur ein.

    ``discord_user_id`` steht nur bei den Spuren des Aufnahme-Bots: dort trennt Discord
    die Audiodaten je Client, wer gesprochen hat ist also bekannt und muss nicht später
    aus dem Dateinamen geraten werden — geraten stünde irgendwann der falsche Name über
    einem Absatz.

    ``discord_name`` ist der Anzeigename zu dieser Kennung, sofern er beim Einreihen
    schon feststand. Er darf fehlen — ohne den privilegierten ``members``-Intent kennt
    der Bot nur, wer ihm einmal begegnet ist —, und dann trägt ihn ``bot.namen`` gezielt
    nach. Was auch danach leer bleibt, heißt in der Chronik »unbekannt« (#250).

    ``started_at`` ebenso, und es ist der Nullpunkt der Sitzungsuhr: der Moment, in dem
    der Mitschnitt begann. Ohne ihn hat die Spur keine Zeitachse, die sich auf die Szenen
    legen ließe — ein Diktat vom Heimweg bekommt deshalb keinen, und zwar auch dann
    nicht, wenn es als Anhang im Thread landet und darum eine Discord-Id trägt.

    ``message_at`` ist der Zeitpunkt der **Nachricht**, mit der ein Diktat im Thread ankam
    — der Anker, über den es trotz fehlender Sitzungsuhr in eine Szene findet. Es ist
    ausdrücklich nicht ``uploaded_at``: das steht auf dem Ablegen, und ein Diktat vom
    Heimweg käme damit in der Szene an, die gerade zufällig die letzte ist.

    ``offset_ms`` sagt, wo diese Datei auf der Sitzungsuhr beginnt. Ein Mitschnitt wird in
    Häppchen geschnitten (#217); das zweite fängt als Datei wieder bei null an, liegt auf
    der Uhr aber eine halbe Stunde später. Wer den Versatz nicht mitgibt, bekommt ``0`` —
    für ein Diktat und für die erste Datei einer Spur ist das die Wahrheit.

    Liegt die Datei schon in der Warteschlange, kommt ``BereitsEingereiht`` — ein zweiter
    Anlauf über mehrere Spuren soll die schon eingereihten überspringen können, statt an
    ihnen hängenzubleiben.
    """
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "INSERT INTO recording (runde_id, session_id, filename, source, uploaded_at, "
                "status, updated_at, discord_user_id, discord_name, started_at, message_at, "
                "offset_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    session_id,
                    filename,
                    Path(filename).stem,
                    zeitpunkt,
                    WARTET,
                    zeitpunkt,
                    discord_user_id,
                    discord_name,
                    started_at,
                    message_at,
                    offset_ms,
                ),
            )
    except sqlite3.IntegrityError as fehler:
        if fehler.sqlite_errorname != DOPPELT:
            raise
        raise BereitsEingereiht(filename) from fehler
    finally:
        scope.close()
    return Recording(
        id=int(cursor.lastrowid),
        session_id=session_id,
        filename=filename,
        source=Path(filename).stem,
        uploaded_at=zeitpunkt,
        status=WARTET,
        discord_user_id=discord_user_id,
        discord_name=discord_name,
        started_at=started_at,
        message_at=message_at,
        offset_ms=offset_ms,
    )


def namenlose_sprecher(runde: Runde, session_id: int | None = None) -> tuple[str, ...]:
    """Die Discord-Kennungen, zu denen noch kein Anzeigename dasteht — je Kennung einmal.

    Das ist die Arbeitsliste des Nachtragens (#250) und zugleich seine Sparsamkeit: gefragt
    wird Discord nur nach den Sprechern, deren Ton wirklich in der Warteschlange liegt, und
    nach jedem nur einmal, auch wenn seine Spur in zehn Häppchen zerfällt.
    """
    scope = db.scoped(runde)
    try:
        bedingung = "" if session_id is None else "AND session_id = ? "
        werte = (scope.runde_id,) if session_id is None else (scope.runde_id, session_id)
        zeilen = scope.execute(
            "SELECT DISTINCT discord_user_id FROM recording WHERE runde_id = ? "
            + bedingung
            + "AND discord_user_id IS NOT NULL AND discord_name IS NULL "
            "ORDER BY discord_user_id",
            werte,
        ).fetchall()
    finally:
        scope.close()
    return tuple(zeile["discord_user_id"] for zeile in zeilen)


def namen_eintragen(runde: Runde, namen: Mapping[str, str]) -> int:
    """Trägt Anzeigenamen zu Kennungen nach und sagt, wie viele Zeilen das traf.

    Nur wo noch keiner steht: ein einmal festgehaltener Name wird nicht von einer späteren
    Umbenennung überschrieben — die Spur gehört dem Abend, an dem sie entstand.
    """
    if not namen:
        return 0
    scope = db.scoped(runde)
    try:
        with scope:
            getroffen = 0
            for user_id, name in namen.items():
                zeiger = scope.execute(
                    "UPDATE recording SET discord_name = ? WHERE runde_id = ? "
                    "AND discord_user_id = ? AND discord_name IS NULL",
                    (name, scope.runde_id, user_id),
                )
                getroffen += zeiger.rowcount
    finally:
        scope.close()
    return getroffen


def _text(scope: db.Scope, transcript_id: int) -> str:
    zeilen = scope.execute(
        "SELECT text FROM transcript_segment WHERE runde_id = ? AND transcript_id = ? "
        "ORDER BY start_ms, id",
        (scope.runde_id, transcript_id),
    ).fetchall()
    return " ".join(zeile["text"] for zeile in zeilen)


def _mit_transkript(scope: db.Scope, row: sqlite3.Row) -> Recording:
    if row["transcript_id"] is None:
        return _eintrag(row)
    return _eintrag(row, _text(scope, row["transcript_id"]))


# Verbunden wird über (session_id, source) statt über einen Fremdschlüssel: ein zweiter
# Lauf ersetzt die Transkript-Zeile im Ganzen, ihre Id wäre also nicht haltbar.
AUSWAHL = (
    "SELECT r.id, r.session_id, r.filename, r.source, r.uploaded_at, r.status, r.detail, "
    "r.deleted_at, r.discord_user_id, r.discord_name, r.started_at, r.message_at, "
    "r.offset_ms, r.versuche, "
    "t.id AS transcript_id FROM recording r "
    "LEFT JOIN transcript t ON t.runde_id = r.runde_id AND t.session_id = r.session_id "
    "AND t.source = r.source WHERE r.runde_id = ? "
)


def for_session(runde: Runde, session_id: int) -> tuple[Recording, ...]:
    scope = db.scoped(runde)
    try:
        rows = scope.execute(
            AUSWAHL + "AND r.session_id = ? ORDER BY r.id", (scope.runde_id, session_id)
        ).fetchall()
        return tuple(_mit_transkript(scope, row) for row in rows)
    finally:
        scope.close()


def get(runde: Runde, recording_id: int) -> Recording | None:
    scope = db.scoped(runde)
    try:
        row = scope.execute(AUSWAHL + "AND r.id = ?", (scope.runde_id, recording_id)).fetchone()
        return None if row is None else _mit_transkript(scope, row)
    finally:
        scope.close()


def unverschriftet(runde: Runde) -> tuple[Recording, ...]:
    """Ton ohne Text: die wartenden **und** die gescheiterten Spuren.

    Der Unterschied zu ``pending``: dort steht, was noch einen Anlauf bekommt, hier, was
    noch keinen Text hat. Eine aufgegebene Spur fällt aus der Warteschlange, aber nicht aus
    der Rechnung — sonst meldete der Lauf Erfolg, während drei Stunden Ton unverschriftet
    der Frist entgegenliegen (#247).
    """
    scope = db.scoped(runde)
    try:
        rows = scope.execute(
            AUSWAHL + "AND r.status IN (?, ?) AND r.deleted_at IS NULL ORDER BY r.id",
            (scope.runde_id, WARTET, GESCHEITERT),
        ).fetchall()
    finally:
        scope.close()
    return tuple(_eintrag(row) for row in rows)


def kommt_wieder_dran(aufnahme: Recording) -> bool:
    """Bekommt diese Spur noch einen Anlauf?

    Die eine Stelle, an der die Regel steht — ``pending`` filtert damit, und ``kette``
    zählt damit, was es beim nächsten Lauf wieder versucht. Eine gescheiterte Spur ist
    nicht verloren: der Grund war bisher jedes Mal vorübergehend, und der Ton liegt bis zur
    Frist noch da. Endlos wiederholt wird trotzdem nicht — ``MAX_VERSUCHE`` sagt, warum.
    """
    if aufnahme.deleted_at is not None:
        return False
    if aufnahme.status == WARTET:
        return True
    return aufnahme.status == GESCHEITERT and aufnahme.versuche < MAX_VERSUCHE


def pending(runde: Runde) -> tuple[Recording, ...]:
    """Was noch einen Anlauf bekommt — ohne die Spuren, deren Audio die Frist schon holte."""
    return tuple(aufnahme for aufnahme in unverschriftet(runde) if kommt_wieder_dran(aufnahme))


def mark(runde: Runde, recording_id: int, status: str, detail: str | None = None) -> None:
    """Der neue Stand der Spur — und damit auch, wem sie gehört.

    Der Besitzvermerk hängt am Stand und wird nirgends von Hand gepflegt: ``laeuft`` gehört
    diesem Prozess, jeder andere Stand gehört niemandem mehr. So kann keine fertige Zeile
    einen Besitzer behalten, dessen Herzschlag nie wieder kommt.

    Ebenso der Zähler: ``gescheitert`` zählt einen Versuch hoch, jeder andere Stand lässt
    ihn stehen. Er wird nirgends zurückgesetzt — nach ``MAX_VERSUCHE`` bleibt die Spur
    liegen. Wer sie doch noch einmal will, setzt sie von Hand auf ``wartet``; das steht
    außerhalb des Zählers und ist genau der Griff, der am 22.08. drei Aufnahmen gerettet
    hat (#247).
    """
    laeuft = status == LAEUFT
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "UPDATE recording SET status = ?, detail = ?, updated_at = ?, besitzer = ?, "
                "herzschlag = ?, versuche = versuche + ? WHERE runde_id = ? AND id = ?",
                (
                    status,
                    detail,
                    _now(),
                    _ICH if laeuft else None,
                    _lebenszeichen() if laeuft else None,
                    1 if status == GESCHEITERT else 0,
                    scope.runde_id,
                    recording_id,
                ),
            )
    finally:
        scope.close()


def _verwaist(zeile: sqlite3.Row, grenze: datetime) -> bool:
    """Gehört diese ``laeuft``-Zeile niemandem mehr?

    Dieselben vier Fälle wie bei den Läufen (#178), in der Reihenfolge, in der sie sich
    sicher entscheiden lassen: was in der eigenen Merkliste steht, wird hier und jetzt
    verschriftet. Was uns gehört und **nicht** darin steht, hat einen Neustart nicht
    überlebt. Alles übrige gehört einem anderen Prozess, und über den entscheidet allein
    sein Lebenszeichen. Eine Zeile ohne beides stammt von vor diesen Spalten — oder aus
    einem Lauf, der starb, bevor der erste Schlag geschrieben war.
    """
    if int(zeile["id"]) in _laufend:
        return False
    if zeile["besitzer"] == _ICH:
        return True
    herzschlag = zeile["herzschlag"]
    if not herzschlag:
        return True
    return datetime.fromisoformat(herzschlag) < grenze


def zurueckstellen(runde: Runde) -> tuple[str, ...]:
    """Spuren, deren Verschriftung niemand mehr betreibt, zurück in die Warteschlange.

    Zurück auf ``wartet`` und nicht auf ``gescheitert``: die Audiodatei liegt noch da, der
    Lauf ist beliebig oft wiederholbar, und ein Transkript aus dem zweiten Anlauf ist
    dasselbe wie eines aus dem ersten. Ein Fehlschlag stünde hier für einen Fehler, den es
    nicht gab — und die Spur bekäme nie einen zweiten Versuch.
    """
    zurueck: list[str] = []
    gemeldet: list[tuple[int, int]] = []
    scope = db.scoped(runde)
    try:
        with _schloss:
            offen = scope.execute(
                "SELECT id, session_id, source, besitzer, herzschlag FROM recording "
                "WHERE runde_id = ? AND status = ? AND deleted_at IS NULL",
                (scope.runde_id, LAEUFT),
            ).fetchall()
            grenze = datetime.now(UTC) - VERSTUMMT
            verwaist = [zeile for zeile in offen if _verwaist(zeile, grenze)]
            if not verwaist:
                return ()
            zeitpunkt = _now()
            with scope:
                scope.executemany(
                    "UPDATE recording SET status = ?, detail = ?, updated_at = ?, "
                    "besitzer = NULL, herzschlag = NULL WHERE runde_id = ? AND id = ?",
                    [
                        (WARTET, UNTERBROCHEN, zeitpunkt, scope.runde_id, int(zeile["id"]))
                        for zeile in verwaist
                    ],
                )
            zurueck = [str(zeile["source"]) for zeile in verwaist]
            gemeldet = [(int(zeile["id"]), int(zeile["session_id"])) for zeile in verwaist]
    finally:
        scope.close()
    # Im Log steht die Job-Id, nicht die Quelle: der Stamm eines Spurnamens ist der
    # Anzeigename des Sprechers (#194), und den geht das Log des Betreibers nichts an. Die
    # Id nennt niemanden — sie ist eine laufende Nummer, die außerhalb dieser Datenbank
    # nichts bedeutet —, und drinnen führt sie in einem Schritt zu Datei, Stand und Text.
    # Die Sitzung steht daneben, weil der Betreiber sonst nicht sähe, welcher Abend es traf.
    for kennung, sitzung in gemeldet:
        logger.warning("Spur %s aus Sitzung %s: %s", kennung, sitzung, UNTERBROCHEN)
    return tuple(zurueck)


def _takt(halt: threading.Event) -> bool:
    """Ein Schlag Wartezeit; ``True`` heißt: Schluss.

    Eigene Funktion, damit ein Test den Takt vorgeben kann. Ein Puls, den man nur über
    Wartezeiten beobachtet, macht den Test wetterabhängig — und ein Test, der unter Last
    rot wird und beim Wiederholen grün, ist schlimmer als keiner.
    """
    return halt.wait(HERZSCHLAG)


def _pulsen(runde: Runde, recording_id: int, halt: threading.Event) -> None:
    """Das Lebenszeichen der laufenden Spur — in einem eigenen Faden.

    Nicht im Arbeitsfaden: der steckt stundenlang im Modell, und genau währenddessen muss
    ein Nachbarprozess sehen, dass hier noch jemand an dieser Spur sitzt.
    """
    while not _takt(halt):
        scope = db.scoped(runde)
        try:
            with scope:
                # ``status = laeuft`` gehört in die Bedingung: zwischen dem Ende der
                # Wartezeit und diesem Schreiben kann der Arbeitsfaden die Spur längst auf
                # ``fertig`` gesetzt haben, und dann trüge eine abgeschlossene Zeile wieder
                # einen Herzschlag.
                scope.execute(
                    "UPDATE recording SET herzschlag = ? "
                    "WHERE runde_id = ? AND id = ? AND status = ?",
                    (_lebenszeichen(), scope.runde_id, recording_id, LAEUFT),
                )
        # Ein verpasster Schlag darf die Spur nicht mitnehmen; bis ``VERSTUMMT`` bleibt
        # Platz für mehrere.
        except Exception as fehler:  # noqa: BLE001
            logger.warning("Lebenszeichen für Spur %s nicht geschrieben: %s", recording_id, fehler)
        finally:
            scope.close()


@contextmanager
def in_arbeit(runde: Runde, recording_id: int) -> Iterator[None]:
    """Die Spur läuft — mit Besitzer, Lebenszeichen und Eintrag in der Merkliste.

    Der abschließende ``mark`` gehört **in** den Block: dazwischen stünde die Zeile sonst
    für einen Wimpernschlag auf ``laeuft``, ohne dass jemand sie noch führt.
    """
    with _schloss:
        _laufend.add(recording_id)
    mark(runde, recording_id, LAEUFT)
    halt = threading.Event()
    threading.Thread(
        target=_pulsen,
        args=(runde, recording_id, halt),
        name=f"chronicle-spur-{recording_id}",
        daemon=True,
    ).start()
    try:
        yield
    finally:
        halt.set()
        with _schloss:
            _laufend.discard(recording_id)


def expired(runde: Runde, *, tage: int = RETENTION_TAGE) -> tuple[Recording, ...]:
    """Spuren, deren Audio die Frist überschritten hat und noch da ist."""
    grenze = (datetime.now(UTC) - timedelta(days=tage)).isoformat(timespec="seconds")
    scope = db.scoped(runde)
    try:
        rows = scope.execute(
            AUSWAHL + "AND r.deleted_at IS NULL AND r.uploaded_at < ? ORDER BY r.id",
            (scope.runde_id, grenze),
        ).fetchall()
    finally:
        scope.close()
    return tuple(_eintrag(row) for row in rows)


def _als_geloescht_vermerken(runde: Runde, recording_id: int, status: str | None = None) -> None:
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        with scope:
            if status is not None:
                scope.execute(
                    "UPDATE recording SET status = ?, detail = ? WHERE runde_id = ? AND id = ?",
                    (status, NIE_VERSCHRIFTET, scope.runde_id, recording_id),
                )
            scope.execute(
                "UPDATE recording SET deleted_at = ?, updated_at = ? WHERE runde_id = ? AND id = ?",
                (zeitpunkt, zeitpunkt, scope.runde_id, recording_id),
            )
    finally:
        scope.close()


def _verschriftet(aufnahme: Recording) -> bool:
    """Hat diese Spur bekommen, wofür sie eingereiht wurde?

    ``fertig`` ohne Transkript ist der eine gewollte Fall: eine Spur ohne Äußerung wird
    absichtlich nicht verschriftet (``MINDESTDAUER``), und ihr Vermerk sagt das auch. Alles
    andere ohne Transkript ist eine Aufnahme, die nie zu Text wurde.
    """
    return aufnahme.transcript_id is not None or aufnahme.status == FERTIG


def _was(anzahl: int) -> str:
    return "eine Aufnahme" if anzahl == 1 else f"{anzahl} Aufnahmen"


def sweep(config, runde: Runde, *, tage: int = RETENTION_TAGE) -> tuple[str, ...]:
    """Setzt die zugesagte Frist durch: Audio weg, Zeile bleibt.

    Beliebig oft aufrufbar — eine bereits vermerkte Spur wird nicht noch einmal gesucht,
    und eine schon von Hand entfernte Datei ist kein Fehlschlag.

    Die Frist gilt **jeder** Spur, auch der nie verschrifteten: sie ist eine Zusage an
    Menschen, deren Stimme aufgenommen wurde, und keine Belohnung für einen geglückten
    Lauf. Still darf sie eine solche Spur aber nicht abräumen — hier geht eine Stunde
    gesprochener Abend, von der es keinen Text gibt und nie mehr einen geben wird. Also
    eine eigene Meldung und ein ehrlicher Stand an der Zeile statt eines »wartet«, auf das
    niemand mehr wartet (#181).

    Gelöscht wird Datei für Datei, **gemeldet** wird je Sitzung: seit dem Schnitt in
    Häppchen (#217) hat ein Abend keine fünf Zeilen mehr, sondern vierzig, und vierzigmal
    derselbe Satz ist keine Meldung mehr, sondern eine Wand.
    """
    geraeumt: dict[tuple[int, bool], int] = {}
    for aufnahme in expired(runde, tage=tage):
        (config.recordings_dir / aufnahme.filename).unlink(missing_ok=True)
        verschriftet = _verschriftet(aufnahme)
        _als_geloescht_vermerken(runde, aufnahme.id, status=None if verschriftet else GESCHEITERT)
        schluessel = (aufnahme.session_id, verschriftet)
        geraeumt[schluessel] = geraeumt.get(schluessel, 0) + 1
    meldungen = []
    for (session_id, verschriftet), anzahl in geraeumt.items():
        vorlage = NACH_FRIST if verschriftet else OHNE_TEXT
        meldung = vorlage.format(sitzung=session_id, was=_was(anzahl), tage=tage)
        if verschriftet:
            logger.info("%s", meldung)
        else:
            logger.warning("%s", meldung)
        meldungen.append(meldung)
    return tuple(meldungen)


@runden.instanzweit
def sweep_alle(config, *, tage: int = RETENTION_TAGE) -> tuple[str, ...]:
    """Die Frist gilt jeder Stimme auf dieser Box und nicht nur der einen Runde."""
    meldungen: list[str] = []
    for eine in runden.alle(config.database_path):
        meldungen.extend(sweep(config, eine, tage=tage))
    return tuple(meldungen)


@runden.instanzweit
async def taeglich(config, *, schlafen=asyncio.sleep) -> None:
    """Die Frist im dauerhaften Bot-Prozess — einmal beim Start, danach täglich.

    Der nächtliche Stapel räumt ebenfalls; beides zusammen heißt, dass die Zusage auch
    dann gilt, wenn eines von beidem eine Weile nicht läuft.
    """
    while True:
        sweep_alle(config)
        await schlafen(SWEEP_ABSTAND)
