"""Läufe, die der Server führt — der Knopf stößt an, die Zeile in der Datenbank ist der Lauf.

Ein Abgleich dauert Sekunden, eine Chronik mit wartenden Aufnahmen Stunden. Beides gehört
deshalb dem Server und nicht dem Browser: das Absenden legt die Zeile an und kehrt sofort
zurück, ein Neuladen der Seite liest denselben Zustand wieder, und das Schließen des
Reiters hält nichts an.

Getragen wird der Lauf von einem Faden — im Aufnahme-Bot oder im Stapelaufruf, je nachdem,
wer angestoßen hat. Der Zustand steht ohnehin in der SQLite und nicht im Speicher.

Zwei gleichzeitige Läufe derselben Art gibt es nicht: sie schrieben dieselben Zeilen, und
die zweite Chronik überschriebe die erste mitten im Satz. Ein zweiter Anstoß bekommt
deshalb den laufenden zurück.

Stirbt der Prozess mitten im Lauf, bleibt die Zeile auf ``laeuft`` stehen. Beim nächsten
Blick wird sie ehrlich als unterbrochen vermerkt statt für immer zu laufen. Welcher das
ist, steht in der Zeile und nicht im Speicher eines Prozesses: **mehrere Prozesse legen
Zeilen auf derselben Datei an** — ``python -m chronicle.bot`` mit dem nächtlichen Lauf
darin (#229) und daneben jeder Stapelaufruf —, und keiner sieht die Merkliste des anderen.
Die frühere Annahme »Zeilen legt nur der Web-Prozess an« machte aus jedem laufenden Lauf
des einen einen »unterbrochenen« im Auge des anderen (#178).

Getragen wird die Unterscheidung von zwei Spalten. ``besitzer`` ist ein Zufallswert je
Prozess**start** — nicht die Prozess-Id, denn die vergibt die Box nach einem Neustart
wieder, und ein Neustart ist genau der Fall, um den es geht. ``herzschlag`` ist das
Lebenszeichen, das der laufende Auftrag alle ``HERZSCHLAG`` Sekunden in seine eigene
Zeile schreibt. Damit sagt jede ``laeuft``-Zeile von selbst, was sie ist: meine und noch
in der Merkliste (läuft), meine und nicht mehr darin (verloren), eine fremde mit frischem
Lebenszeichen (läuft anderswo) oder eine fremde, die seit ``VERSTUMMT`` schweigt
(abgestürzt). Der Preis ist, dass ein wirklich abgestürzter fremder Lauf die Maschine
noch anderthalb Minuten hält — deutlich billiger als der Satz, der Dienst sei neu
gestartet, während der Lauf in voller Fahrt ist.

Die Stapel-Einstiege (``python -m chronicle.compose`` und Geschwister) rufen dieselben
Funktionen auf. Ein Knopf ist der zweite Auslöser, nicht der zweite Weg.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from chronicle import db, kette, lebenszyklus, recordings, register
from chronicle.compose.service import erzaehlen
from chronicle.config import Config
from chronicle.discord.ausgabe import erzaehlung_zustellen
from chronicle.foundry.service import sync
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

ABGLEICH = "abgleich"
CHRONIK = "chronik"
NACHTLAUF = "nachtlauf"
NACHERZAEHLUNG = "nacherzaehlung"

LAEUFT = "laeuft"
FERTIG = "fertig"
GESCHEITERT = "gescheitert"

# Am Namen ist der Faden eines Laufs erkennbar — im Log wie in einem Test, der abwarten
# muss, bis kein Lauf mehr nebenher zu Ende geht.
FADEN = "chronicle-lauf-"

# Der Faden, der für einen laufenden Auftrag das Lebenszeichen schreibt.
PULS = "chronicle-puls-"

# Wie oft ein laufender Auftrag sein Lebenszeichen in die eigene Zeile schreibt, und wie
# lange ein fremder Prozess darauf wartet, bevor er die Zeile für verwaist hält. Der
# Abstand ist mit Absicht groß: eine kurze Lastspitze oder ein hängender SQLite-Schreiber
# darf keinen laufenden Lauf als abgestürzt erscheinen lassen.
HERZSCHLAG = 20.0
VERSTUMMT = timedelta(seconds=90)

UNTERBROCHEN = (
    "Der Lauf wurde unterbrochen, weil der Dienst zwischendurch neu gestartet ist. "
    "Einfach noch einmal anstoßen."
)

OHNE_BEREICH = "In diesem Bereich liegt keine Sitzung mehr — nachzuerzählen gibt es nichts."

# Es gibt keine Warteschlange, und der Satz verspricht auch keine mehr: ``start`` legt in
# diesem Fall **keine** Zeile an und merkt sich nichts. Wer wartet, wartet auf nichts —
# deshalb steht hier, was wirklich zu tun ist (#179). Und wer im Weg steht, ist oft die
# eigene Runde mit einem Lauf anderer Art; »eine andere Runde« war dann schlicht falsch.
BELEGT = (
    "Eine andere Runde ist gerade an der Maschine. Der Lauf beginnt nicht von allein — "
    "bitte später noch einmal anstoßen."
)

BELEGT_EIGEN = (
    "In dieser Runde läuft gerade schon ein anderer Lauf. Der neue beginnt nicht von "
    "allein — bitte anstoßen, sobald der erste durch ist."
)

NICHT_DURCHGEKOMMEN = "Der Lauf ist nicht durchgekommen: {grund}"

# Was der Abschluss nachträglich an die Szenen gehängt hat (#219). Gesagt wird es, weil es
# sonst niemand sähe: die Zahlen erscheinen still in der Chronik, und ob welche kamen, ist
# genau die Frage, die an einem Abend ohne Ereignisstrom offen war.
NACHGETRAGEN = "{anzahl} Würfe aus Foundry sind den Szenen dieser Sitzung zugeordnet. "
NACHGETRAGEN_EINER = "Ein Wurf aus Foundry ist einer Szene dieser Sitzung zugeordnet. "

# Und der Gegenfall, ebenso ausdrücklich: ohne Abgleich kommt keine Zahl mehr dazu. Das
# still zu übergehen hieße, eine Chronik ohne Belege als fertig zu melden.
OHNE_ZAHLEN = (
    "Aus Foundry kommt damit nichts mehr dazu: in dieser Chronik stehen nur die Zahlen, "
    "die der Ereignisstrom während der Sitzung geholt hat — lief keiner, steht keine. "
)

STEHT = "Chronik und Rückblick stehen bereit."
STEHT_OHNE_MODELL = (
    "Chronik und Rückblick stehen bereit — ohne Sprachmodell geordnet statt formuliert."
)
VERSCHRIFTET = "{anzahl} Aufnahme{mehr} verschriftet. "

# Der Gegenfall, und er gehört gesagt: die Tonspuren werden nach der Frist gelöscht, ob
# ein Transkript entstand oder nicht. Wer »4 Aufnahmen verschriftet« liest, sieht nicht
# nach — und hat eine Woche später weder Text noch Ton (#244).
NICHT_VERSCHRIFTET = (
    "{anzahl} Aufnahme{mehr} konnte ich nicht verschriften; der Ton liegt weiter bereit "
    "und kommt beim nächsten Lauf wieder dran, wird aber nach {tage} Tagen gelöscht — "
    "verschriftet oder nicht. "
)

# Wer dieser Prozess ist — für die Dauer genau eines Starts. Ein Zufallswert und nicht
# die Prozess-Id: die vergibt die Box nach einem Neustart wieder, und dann hielte der
# neue Prozess die Zeilen des alten für seine eigenen.
_ICH = uuid4().hex

# Welche Läufe in **diesem** Prozess noch laufen. Was hier steht, läuft; was fehlt und uns
# gehört, hat einen Neustart nicht überlebt. Über fremde Zeilen sagt diese Liste nichts —
# dafür ist der Herzschlag da.
_laufend: set[int] = set()
_schloss = threading.RLock()


class JobError(RuntimeError):
    """Woran der Lauf gescheitert ist — im Wortlaut, der dem Leser gezeigt wird."""


@dataclass(frozen=True)
class Job:
    id: int
    runde_id: int
    kind: str
    session_id: int | None
    state: str
    started_at: str
    finished_at: str | None = None
    result: str | None = None
    error: str | None = None

    @property
    def laeuft(self) -> bool:
        return self.state == LAEUFT

    @property
    def fertig(self) -> bool:
        return self.state == FERTIG

    @property
    def gescheitert(self) -> bool:
        return self.state == GESCHEITERT


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _lebenszeichen() -> str:
    """Feiner als ``_now``: zwei Schläge dürfen sich nicht auf dieselbe Sekunde runden."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        runde_id=row["runde_id"],
        kind=row["kind"],
        session_id=row["session_id"],
        state=row["state"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=row["result"],
        error=row["error"],
    )


def _verwaist(zeile: sqlite3.Row, grenze: datetime) -> bool:
    """Gehört diese ``laeuft``-Zeile niemandem mehr?

    Die drei Fälle in der Reihenfolge, in der sie sich sicher entscheiden lassen: was in
    der eigenen Merkliste steht, läuft hier und jetzt. Was uns gehört und **nicht** darin
    steht, hat einen Neustart nicht überlebt — der Zufallswert in ``besitzer`` stammt
    dann noch von dieser Prozess-Inkarnation, kann es aber nicht, also ist es ein Rest.
    Alles übrige gehört einem anderen Prozess, und über den entscheidet allein sein
    Lebenszeichen. Eine Zeile ohne beides ist von vor dieser Spalte und damit alt.
    """
    if int(zeile["id"]) in _laufend:
        return False
    if zeile["besitzer"] == _ICH:
        return True
    herzschlag = zeile["herzschlag"]
    if not herzschlag:
        return True
    return datetime.fromisoformat(herzschlag) < grenze


# Bewusst über alle Runden: ob ein Lauf abgestürzt ist, hängt am Prozess und nicht an
# einer Runde. Eine stehengebliebene ``laeuft``-Zeile einer fremden Runde blockierte sonst
# die Maschine für alle.
def _aufraeumen(database_path: Path) -> None:
    connection = db.connect(database_path)
    try:
        with _schloss:
            offen = connection.execute(
                "SELECT id, besitzer, herzschlag FROM job WHERE state = ?", (LAEUFT,)
            ).fetchall()
            grenze = datetime.now(UTC) - VERSTUMMT
            verwaist = [int(zeile["id"]) for zeile in offen if _verwaist(zeile, grenze)]
            if not verwaist:
                return
            zeitpunkt = _now()
            with connection:
                connection.executemany(
                    "UPDATE job SET state = ?, error = ?, finished_at = ? WHERE id = ?",
                    [(GESCHEITERT, UNTERBROCHEN, zeitpunkt, job_id) for job_id in verwaist],
                )
    finally:
        connection.close()


def latest(runde: Runde, kind: str, session_id: int | None = None) -> Job | None:
    """Der jüngste Lauf dieser Art in dieser Runde — die Wiederanbindung nach jedem Aufruf."""
    scope = db.scoped(runde)
    try:
        _aufraeumen(runde.database_path)
        if session_id is None:
            row = scope.execute(
                "SELECT * FROM job WHERE runde_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                (scope.runde_id, kind),
            ).fetchone()
        else:
            row = scope.execute(
                "SELECT * FROM job WHERE runde_id = ? AND kind = ? AND session_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (scope.runde_id, kind, session_id),
            ).fetchone()
    finally:
        scope.close()
    return None if row is None else _job(row)


def running(runde: Runde, kind: str | None = None) -> bool:
    """Läuft in dieser Runde einer — ohne ``kind`` einer beliebiger Art."""
    scope = db.scoped(runde)
    try:
        _aufraeumen(runde.database_path)
        if kind is None:
            offen = scope.execute(
                "SELECT 1 FROM job WHERE runde_id = ? AND state = ?", (scope.runde_id, LAEUFT)
            ).fetchone()
        else:
            offen = scope.execute(
                "SELECT 1 FROM job WHERE runde_id = ? AND kind = ? AND state = ?",
                (scope.runde_id, kind, LAEUFT),
            ).fetchone()
    finally:
        scope.close()
    return offen is not None


def start(
    config: Config,
    runde: Runde,
    kind: str,
    runner: Callable[[], str],
    *,
    session_id: int | None = None,
) -> Job | None:
    """Stößt einen Lauf an und kehrt sofort zurück.

    Läuft in dieser Runde schon einer derselben Art, kommt der zurück — ein zweiter Klick
    ist keine zweite Chronik. Läuft irgendwo sonst einer, beginnt hier keiner: es gibt eine
    CPU und ein Ollama, und zwei Läufe nebeneinander machen beide langsam. Der Aufrufer
    bekommt dann ``None``, holt sich mit ``belegt`` den passenden Satz und sagt es ehrlich
    statt eine Warteschlange zu erfinden — es wird hier **nichts** gemerkt und nichts
    nachgeholt.
    """
    _aufraeumen(config.database_path)
    connection = db.connect(config.database_path)
    try:
        with _schloss:
            offen = connection.execute(
                "SELECT * FROM job WHERE state = ? ORDER BY id LIMIT 1", (LAEUFT,)
            ).fetchone()
            if offen is not None:
                gleicher = offen["runde_id"] == runde.id and offen["kind"] == kind
                return _job(offen) if gleicher else None
            zeitpunkt = _now()
            with connection:
                zeiger = connection.execute(
                    "INSERT INTO job (runde_id, kind, session_id, state, started_at, "
                    "besitzer, herzschlag) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        runde.id,
                        kind,
                        session_id,
                        LAEUFT,
                        zeitpunkt,
                        _ICH,
                        _lebenszeichen(),
                    ),
                )
            job_id = int(zeiger.lastrowid)
            _laufend.add(job_id)
    finally:
        connection.close()

    threading.Thread(
        target=_ausfuehren,
        args=(config, job_id, runner),
        name=f"{FADEN}{job_id}",
        daemon=True,
    ).start()
    return Job(
        id=job_id,
        runde_id=runde.id,
        kind=kind,
        session_id=session_id,
        state=LAEUFT,
        started_at=zeitpunkt,
    )


def belegt(runde: Runde) -> str:
    """Warum hier gerade kein Lauf begann — im Wortlaut für den, der angestoßen hat.

    Der Unterschied ist keine Feinheit: die eigene Runde blockiert sich häufig selbst, ein
    laufender ``abgleich`` etwa den Abschluss. »Eine andere Runde« zu melden schickt die
    Gruppe dann auf die Suche nach einer fremden Gruppe, die es nicht gibt.
    """
    return BELEGT_EIGEN if running(runde) else BELEGT


def _pulsen(database_path: Path, job_id: int, halt: threading.Event) -> None:
    """Das Lebenszeichen des laufenden Auftrags — in einem eigenen Faden.

    Nicht im Arbeitsfaden: der steckt stundenlang in einer Verschriftung, und genau
    währenddessen muss der Nachbarprozess sehen, dass hier noch jemand atmet.
    """
    while not halt.wait(HERZSCHLAG):
        connection = db.connect(database_path)
        try:
            with connection:
                connection.execute(
                    "UPDATE job SET herzschlag = ? WHERE id = ?", (_lebenszeichen(), job_id)
                )
        # Ein verpasster Schlag darf den Lauf nicht mitnehmen; bis ``VERSTUMMT`` bleibt
        # Platz für mehrere.
        except Exception as fehler:  # noqa: BLE001
            logger.warning("Lebenszeichen für Lauf %s nicht geschrieben: %s", job_id, fehler)
        finally:
            connection.close()


def _abschliessen(
    database_path: Path,
    job_id: int,
    state: str,
    *,
    result: str | None = None,
    error: str | None = None,
) -> None:
    connection = db.connect(database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE job SET state = ?, finished_at = ?, result = ?, error = ? WHERE id = ?",
                (state, _now(), result, error, job_id),
            )
    finally:
        connection.close()
    # Erst die Zeile, dann die Merkliste: andersherum sähe ein gleichzeitiger Blick eine
    # laufende Zeile ohne Faden und schriebe sie als unterbrochen um.
    with _schloss:
        _laufend.discard(job_id)


def _ausfuehren(config: Config, job_id: int, runner: Callable[[], str]) -> None:
    halt = threading.Event()
    threading.Thread(
        target=_pulsen,
        args=(config.database_path, job_id, halt),
        name=f"{PULS}{job_id}",
        daemon=True,
    ).start()
    try:
        meldung = runner()
    except JobError as fehler:
        _abschliessen(config.database_path, job_id, GESCHEITERT, error=str(fehler))
    # Ein Lauf darf den Dienst nicht mitnehmen: was hier ankommt, wird zur Zeile, die der
    # Leser sieht, statt in einem Faden zu verpuffen.
    except Exception as fehler:  # noqa: BLE001
        logger.warning("Lauf %s gescheitert: %s", job_id, fehler)
        _abschliessen(
            config.database_path,
            job_id,
            GESCHEITERT,
            error=NICHT_DURCHGEKOMMEN.format(grund=fehler),
        )
    else:
        _abschliessen(config.database_path, job_id, FERTIG, result=meldung)
    finally:
        halt.set()


def abgleich(config: Config, runde: Runde, *, passwort: str | None = None) -> str:
    """``passwort`` reicht der Auslöser durch — wie beim Abschluss, aus demselben Grund.

    Dieser Faden sieht nicht selbst im Merkzettel nach: dort kann inzwischen die Eingabe
    eines anderen liegen. ``None`` heißt »keines mitgebracht«, dann liest ``sync`` den
    Merkzettel wie eh und je.
    """
    zustand = sync(config, runde, passwort=passwort)
    if zustand.stale:
        raise JobError(zustand.message)
    return zustand.message


def chronik(config: Config, runde: Runde, session_id: int) -> str:
    """Der Durchgang aus ``kette.schreiben``, in einem Satz beantwortet.

    Die Reihenfolge steht dort und nicht hier: sie ist dieselbe, die der Nachtlauf und der
    Stapelaufruf gehen, und dreimal gepflegt lief sie auseinander (#221).
    """
    lauf = kette.schreiben(config, runde, session_id)
    if lauf is None:
        raise JobError(kette.warum_nicht(runde))
    zustellung, ausgabe = lauf.zustellung, lauf.ausgabe
    vorschlaege = lauf.vorschlaege
    vorlauf = ""
    if lauf.verschriftet:
        vorlauf += VERSCHRIFTET.format(anzahl=lauf.verschriftet, mehr=mehrzahl(lauf.verschriftet))
    if lauf.offen:
        vorlauf += NICHT_VERSCHRIFTET.format(
            anzahl=lauf.offen, mehr=mehrzahl(lauf.offen), tage=recordings.RETENTION_TAGE
        )
    stand = STEHT if lauf.chronik.reason is None else STEHT_OHNE_MODELL
    # Der Hinweis auf offene Vorschläge steht bewusst im Ergebnis des Laufs: wird das
    # Bestätigen nicht angestoßen, wird es übersprungen, und das Register verfällt.
    nachsatz = "" if not vorschlaege.count else f" {vorschlaege.message}"
    # Und eine misslungene Zustellung genauso: sie war der stille Ausfall aus #182.
    liegengeblieben = f" {zustellung.meldung}" if zustellung.gescheitert else ""
    return vorlauf + stand + liegengeblieben + nachsatz + (f" {ausgabe}" if ausgabe else "")


def nacherzaehlung(config: Config, runde: Runde, von: int, bis: int, kanal_id: str) -> str:
    """Einen Sitzungsbereich nacherzählen und die Datei dorthin stellen, wo gefragt wurde.

    Das Register wird **hier** gelesen und mitgegeben: die Auswahl gehört an den Aufruf,
    nicht in die Komposition. Ohne bestätigten Eintrag bleibt eine Sitzung eine benannte
    Lücke — der Lauf ist damit nicht gescheitert.
    """
    ergebnis = erzaehlen(config, runde, von, bis, register.nach_sitzung(runde))
    if ergebnis is None:
        raise JobError(lebenszyklus.RUHT if lebenszyklus.ruht(runde) else OHNE_BEREICH)
    ausgabe = erzaehlung_zustellen(
        config, runde, kanal_id, ergebnis.text, ergebnis.von, ergebnis.bis
    )
    return ergebnis.message + (f" {ausgabe}" if ausgabe else "")


def abschluss(config: Config, runde: Runde, session_id: int, *, passwort: str | None = None) -> str:
    """Zahlen holen, verschriften, schreiben — der eine Lauf am Ende einer Sitzung.

    Ein misslungener Abgleich bricht ihn nicht ab: Notizen und Aufnahmen ergeben auch ohne
    die Zahlen aus Foundry eine Chronik, und der Grund steht dann vorne in der Meldung.
    Andersherum verlöre man das Geschriebene, weil der Server aus war.

    ``passwort`` reicht der Auslöser durch, damit dieser Faden nicht selbst im Merkzettel
    nachsieht: dort kann inzwischen die Eingabe eines anderen liegen. ``None`` heißt
    »keines mitgebracht« — dann liest der Abgleich den Merkzettel wie eh und je.

    Der Abgleich bekommt die Sitzung mit: er holt das Chat-Log ohnehin, und seit #219 hängt
    er die Würfe dieses Abends auch an ihre Szenen. Vorher tat das allein der
    Ereignisstrom, und ein Abend ohne Strom bekam eine Chronik ohne eine einzige Zahl.
    Kommt der Abgleich nicht durch, kommt auch keine Zahl — und dann steht das im Satz.
    """
    zustand = sync(config, runde, passwort=passwort, session_id=session_id)
    if zustand.stale:
        vorlauf = f"{zustand.message} {OHNE_ZAHLEN}"
    elif zustand.nachgetragen == 1:
        vorlauf = NACHGETRAGEN_EINER
    elif zustand.nachgetragen:
        vorlauf = NACHGETRAGEN.format(anzahl=zustand.nachgetragen)
    else:
        vorlauf = ""
    return vorlauf + chronik(config, runde, session_id)


def mehrzahl(anzahl: int) -> str:
    return "n" if anzahl > 1 else ""
