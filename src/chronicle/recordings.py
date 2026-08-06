"""Die Warteschlange der Spuren: hochgeladen, wartend, transkribiert.

Ein Diktat-Upload legt hier einen Job an — dieselbe Warteschlange und dieselbe Stufe,
die später der Recorder-Bot befüllt. Einen zweiten Verarbeitungsweg gibt es nicht.

Die Zeile *ist* der Job, ihre ``id`` die Job-Id. Gemeldet wird ihr Zustand und sonst
nichts: ein Balken, der sich füllt, wäre geraten, und geraten wird hier nichts. Der
Lauf beginnt im nächsten Stapel, nicht beim Absenden.

Geschrieben wird im Strom auf die Platte — Werkzeug spult den Hochladestrom ab einem
halben Megabyte selbst in eine temporäre Datei, und ``save`` kopiert von dort weiter.
Eine Stunde Audio steht damit nie im Speicher.

**Die Aufbewahrungsfrist steht hier**, und zwar als Zahl: der Bot sagt sie im Sprachkanal
zu (``chronicle.bot.ansage``), und derselbe Wert setzt sie durch. Ein Versprechen, das nur
im Ansagetext steht, wäre keins — deshalb formatiert die Ansage ihre Frist aus
``RETENTION_TAGE``, und ``sweep`` räumt danach. Beide können nicht auseinanderlaufen.

Gelöscht wird dabei nur die **Audiodatei**; die Zeile bleibt mit ``deleted_at`` stehen.
Sie ist der ehrliche Teil der Geschichte: dass es die Spur gab, wann sie kam, was aus ihr
wurde — und dass sie nach Frist entfernt wurde und nicht etwa verlorenging.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from werkzeug.utils import secure_filename

from chronicle import db

logger = logging.getLogger(__name__)

WARTET = "wartet"
LAEUFT = "laeuft"
FERTIG = "fertig"
GESCHEITERT = "gescheitert"

# Die Frist aus der Ansage. Kein Betriebsknopf: wer sie ändert, ändert eine Zusage an
# Menschen, deren Stimme aufgenommen wurde — das ist kein Umgebungsvariablen-Thema.
RETENTION_TAGE = 7

# Der Bot läuft ohnehin; einmal am Tag nachzusehen kostet nichts und hält die Zusage auch
# in Wochen ohne Stapellauf.
SWEEP_ABSTAND = 24 * 60 * 60

NACH_FRIST = "Spur »{source}«: Aufnahme nach {tage} Tagen gelöscht — die Frist aus der Ansage."

# Was Sprachmemo-Apps ablegen. Normalisiert wird nichts davon vorab: faster-whisper
# dekodiert über PyAV, dessen Wheel bringt die FFmpeg-Bibliotheken mit — m4a/AAC und
# ogg/opus vom Telefon gehen ohne ein ffmpeg im Image.
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


class Rejected(ValueError):
    """Was nicht angenommen wird, wird gesagt — still verschluckt wird nichts."""


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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _open(database_path: Path) -> sqlite3.Connection:
    return db.connect(database_path)


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


def accept(config, session_id: int, datei) -> Recording:
    """Nimmt eine hochgeladene Spur an und reiht sie ein."""
    name = (getattr(datei, "filename", None) or "").strip()
    if not name:
        raise Rejected("Keine Datei ausgewählt — die Sprachnotiz im Dateiwähler aussuchen.")
    if Path(name).suffix.lower() not in SUFFIXES:
        erlaubt = ", ".join(SUFFIXES)
        raise Rejected(f"»{name}« ist keine Audiodatei. Angenommen wird: {erlaubt}.")

    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = target_path(config.recordings_dir, session_id, name)
    datei.save(ziel)
    if ziel.stat().st_size == 0:
        ziel.unlink()
        raise Rejected(f"»{name}« kam leer an — die Aufnahme noch einmal hochladen.")
    return enqueue(config.database_path, session_id, ziel.name)


def enqueue(database_path: Path, session_id: int, filename: str) -> Recording:
    zeitpunkt = _now()
    connection = _open(database_path)
    try:
        with connection:
            cursor = connection.execute(
                "INSERT INTO recording (session_id, filename, source, uploaded_at, status, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, filename, Path(filename).stem, zeitpunkt, WARTET, zeitpunkt),
            )
        return Recording(
            id=int(cursor.lastrowid),
            session_id=session_id,
            filename=filename,
            source=Path(filename).stem,
            uploaded_at=zeitpunkt,
            status=WARTET,
        )
    finally:
        connection.close()


def _text(connection: sqlite3.Connection, transcript_id: int) -> str:
    zeilen = connection.execute(
        "SELECT text FROM transcript_segment WHERE transcript_id = ? ORDER BY start_ms, id",
        (transcript_id,),
    ).fetchall()
    return " ".join(zeile["text"] for zeile in zeilen)


def _mit_transkript(connection: sqlite3.Connection, row: sqlite3.Row) -> Recording:
    if row["transcript_id"] is None:
        return _eintrag(row)
    return _eintrag(row, _text(connection, row["transcript_id"]))


# Verbunden wird über (session_id, source) statt über einen Fremdschlüssel: ein zweiter
# Lauf ersetzt die Transkript-Zeile im Ganzen, ihre Id wäre also nicht haltbar.
AUSWAHL = (
    "SELECT r.id, r.session_id, r.filename, r.source, r.uploaded_at, r.status, r.detail, "
    "r.deleted_at, t.id AS transcript_id FROM recording r "
    "LEFT JOIN transcript t ON t.session_id = r.session_id AND t.source = r.source "
)


def for_session(database_path: Path, session_id: int) -> tuple[Recording, ...]:
    connection = _open(database_path)
    try:
        rows = connection.execute(
            AUSWAHL + "WHERE r.session_id = ? ORDER BY r.id", (session_id,)
        ).fetchall()
        return tuple(_mit_transkript(connection, row) for row in rows)
    finally:
        connection.close()


def get(database_path: Path, recording_id: int) -> Recording | None:
    connection = _open(database_path)
    try:
        row = connection.execute(AUSWAHL + "WHERE r.id = ?", (recording_id,)).fetchone()
        return None if row is None else _mit_transkript(connection, row)
    finally:
        connection.close()


def pending(database_path: Path) -> tuple[Recording, ...]:
    """Was noch wartet — ohne die Spuren, deren Audio die Frist schon geholt hat."""
    connection = _open(database_path)
    try:
        rows = connection.execute(
            AUSWAHL + "WHERE r.status = ? AND r.deleted_at IS NULL ORDER BY r.id", (WARTET,)
        ).fetchall()
    finally:
        connection.close()
    return tuple(_eintrag(row) for row in rows)


def mark(database_path: Path, recording_id: int, status: str, detail: str | None = None) -> None:
    connection = _open(database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE recording SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
                (status, detail, _now(), recording_id),
            )
    finally:
        connection.close()


def expired(database_path: Path, *, tage: int = RETENTION_TAGE) -> tuple[Recording, ...]:
    """Spuren, deren Audio die Frist überschritten hat und noch da ist."""
    grenze = (datetime.now(UTC) - timedelta(days=tage)).isoformat(timespec="seconds")
    connection = _open(database_path)
    try:
        rows = connection.execute(
            AUSWAHL + "WHERE r.deleted_at IS NULL AND r.uploaded_at < ? ORDER BY r.id", (grenze,)
        ).fetchall()
    finally:
        connection.close()
    return tuple(_eintrag(row) for row in rows)


def _als_geloescht_vermerken(database_path: Path, recording_id: int) -> None:
    zeitpunkt = _now()
    connection = _open(database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE recording SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (zeitpunkt, zeitpunkt, recording_id),
            )
    finally:
        connection.close()


def sweep(config, *, tage: int = RETENTION_TAGE) -> tuple[str, ...]:
    """Setzt die zugesagte Frist durch: Audio weg, Zeile bleibt.

    Beliebig oft aufrufbar — eine bereits vermerkte Spur wird nicht noch einmal gesucht,
    und eine schon von Hand entfernte Datei ist kein Fehlschlag.
    """
    meldungen = []
    for aufnahme in expired(config.database_path, tage=tage):
        (config.recordings_dir / aufnahme.filename).unlink(missing_ok=True)
        _als_geloescht_vermerken(config.database_path, aufnahme.id)
        meldung = NACH_FRIST.format(source=aufnahme.source, tage=tage)
        logger.info("%s", meldung)
        meldungen.append(meldung)
    return tuple(meldungen)


async def taeglich(config, *, schlafen=asyncio.sleep) -> None:
    """Die Frist im dauerhaften Bot-Prozess — einmal beim Start, danach täglich.

    Der nächtliche Stapel räumt ebenfalls; beides zusammen heißt, dass die Zusage auch
    dann gilt, wenn eines von beidem eine Weile nicht läuft.
    """
    while True:
        sweep(config)
        await schlafen(SWEEP_ABSTAND)
