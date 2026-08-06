"""Läufe, die der Server führt — der Knopf stößt an, die Zeile in der Datenbank ist der Lauf.

Ein Abgleich dauert Sekunden, eine Chronik mit wartenden Aufnahmen Stunden. Beides gehört
deshalb dem Server und nicht dem Browser: das Absenden legt die Zeile an und kehrt sofort
zurück, ein Neuladen der Seite liest denselben Zustand wieder, und das Schließen des
Reiters hält nichts an.

Getragen wird der Lauf von einem Faden im Web-Prozess. Das reicht hier: es läuft je Art
höchstens einer, der Prozess ist einer (``waitress`` bedient mehrere Fäden aus einem
Prozess), und der Zustand steht ohnehin in der SQLite und nicht im Speicher. Ein eigener
Arbeiterprozess wäre eine zweite Betriebseinheit für zwei Knöpfe.

Zwei gleichzeitige Läufe derselben Art gibt es nicht: sie schrieben dieselben Zeilen, und
die zweite Chronik überschriebe die erste mitten im Satz. Ein zweiter Anstoß bekommt
deshalb den laufenden zurück.

Stirbt der Prozess mitten im Lauf, bleibt die Zeile auf ``laeuft`` stehen. Beim nächsten
Blick wird sie ehrlich als unterbrochen vermerkt statt für immer zu laufen — welche Läufe
wirklich noch laufen, weiß dieser Prozess, und er ist der einzige, der Zeilen anlegt.

Die Stapel-Einstiege (``python -m chronicle.compose`` und Geschwister) rufen dieselben
Funktionen auf. Ein Knopf ist der zweite Auslöser, nicht der zweite Weg.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db, recordings
from chronicle.compose.service import compose_session, recap_session
from chronicle.config import Config
from chronicle.discord.rueckblick import deliver
from chronicle.foundry.service import sync
from chronicle.transcribe.service import run_queue

logger = logging.getLogger(__name__)

ABGLEICH = "abgleich"
CHRONIK = "chronik"

LAEUFT = "laeuft"
FERTIG = "fertig"
GESCHEITERT = "gescheitert"

UNTERBROCHEN = (
    "Der Lauf wurde unterbrochen, weil der Dienst zwischendurch neu gestartet ist. "
    "Einfach noch einmal anstoßen."
)

OHNE_SITZUNG = "Diese Sitzung gibt es nicht mehr."

NICHT_DURCHGEKOMMEN = "Der Lauf ist nicht durchgekommen: {grund}"

STEHT = "Chronik und Rückblick stehen bereit."
STEHT_OHNE_MODELL = (
    "Chronik und Rückblick stehen bereit — ohne Sprachmodell geordnet statt formuliert."
)
VERSCHRIFTET = "{anzahl} Aufnahme{mehr} verschriftet. "

# Welche Läufe in diesem Prozess noch laufen. Zeilen legt nur der Web-Prozess an, und den
# gibt es einmal — eine ``laeuft``-Zeile, die hier fehlt, hat also einen Neustart nicht
# überlebt.
_laufend: set[int] = set()
_schloss = threading.RLock()


class JobError(RuntimeError):
    """Woran der Lauf gescheitert ist — im Wortlaut, der dem Leser gezeigt wird."""


@dataclass(frozen=True)
class Job:
    id: int
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


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        session_id=row["session_id"],
        state=row["state"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        result=row["result"],
        error=row["error"],
    )


def _aufraeumen(connection: sqlite3.Connection) -> None:
    with _schloss:
        offen = connection.execute("SELECT id FROM job WHERE state = ?", (LAEUFT,)).fetchall()
        verwaist = [int(zeile["id"]) for zeile in offen if int(zeile["id"]) not in _laufend]
        if not verwaist:
            return
        zeitpunkt = _now()
        with connection:
            connection.executemany(
                "UPDATE job SET state = ?, error = ?, finished_at = ? WHERE id = ?",
                [(GESCHEITERT, UNTERBROCHEN, zeitpunkt, job_id) for job_id in verwaist],
            )


def latest(database_path: Path, kind: str, session_id: int | None = None) -> Job | None:
    """Der jüngste Lauf dieser Art — die Wiederanbindung nach jedem Seitenaufruf."""
    connection = db.connect(database_path)
    try:
        _aufraeumen(connection)
        if session_id is None:
            row = connection.execute(
                "SELECT * FROM job WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM job WHERE kind = ? AND session_id = ? ORDER BY id DESC LIMIT 1",
                (kind, session_id),
            ).fetchone()
    finally:
        connection.close()
    return None if row is None else _job(row)


def running(database_path: Path, kind: str) -> bool:
    connection = db.connect(database_path)
    try:
        _aufraeumen(connection)
        offen = connection.execute(
            "SELECT 1 FROM job WHERE kind = ? AND state = ?", (kind, LAEUFT)
        ).fetchone()
    finally:
        connection.close()
    return offen is not None


def start(
    config: Config,
    kind: str,
    runner: Callable[[], str],
    *,
    session_id: int | None = None,
) -> Job:
    """Stößt einen Lauf an und kehrt sofort zurück; läuft schon einer, kommt der zurück."""
    connection = db.connect(config.database_path)
    try:
        _aufraeumen(connection)
        with _schloss:
            offen = connection.execute(
                "SELECT * FROM job WHERE kind = ? AND state = ?", (kind, LAEUFT)
            ).fetchone()
            if offen is not None:
                return _job(offen)
            zeitpunkt = _now()
            with connection:
                zeiger = connection.execute(
                    "INSERT INTO job (kind, session_id, state, started_at) VALUES (?, ?, ?, ?)",
                    (kind, session_id, LAEUFT, zeitpunkt),
                )
            job_id = int(zeiger.lastrowid)
            _laufend.add(job_id)
    finally:
        connection.close()

    threading.Thread(target=_ausfuehren, args=(config, job_id, runner), daemon=True).start()
    return Job(id=job_id, kind=kind, session_id=session_id, state=LAEUFT, started_at=zeitpunkt)


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


def abgleich(config: Config) -> str:
    zustand = sync(config)
    if zustand.stale:
        raise JobError(zustand.message)
    return zustand.message


def chronik(config: Config, session_id: int) -> str:
    """Erst die wartenden Aufnahmen verschriften, dann komponieren — ein Lauf."""
    wartend = len(recordings.pending(config.database_path))
    run_queue(config)
    ergebnis = compose_session(config, session_id)
    if ergebnis is None:
        raise JobError(OHNE_SITZUNG)
    recap_session(config, session_id)
    deliver(config, session_id)
    vorlauf = "" if not wartend else VERSCHRIFTET.format(anzahl=wartend, mehr=mehrzahl(wartend))
    return vorlauf + (STEHT if ergebnis.reason is None else STEHT_OHNE_MODELL)


def mehrzahl(anzahl: int) -> str:
    return "n" if anzahl > 1 else ""
