"""Die Warteschlange der Spuren: hochgeladen, wartend, transkribiert.

Ein Diktat-Upload legt hier einen Job an — dieselbe Warteschlange und dieselbe Stufe,
die später der Recorder-Bot befüllt. Einen zweiten Verarbeitungsweg gibt es nicht.

Die Zeile *ist* der Job, ihre ``id`` die Job-Id. Gemeldet wird ihr Zustand und sonst
nichts: ein Balken, der sich füllt, wäre geraten, und geraten wird hier nichts. Der
Lauf beginnt im nächsten Stapel, nicht beim Absenden.

Geschrieben wird im Strom auf die Platte — Werkzeug spult den Hochladestrom ab einem
halben Megabyte selbst in eine temporäre Datei, und ``save`` kopiert von dort weiter.
Eine Stunde Audio steht damit nie im Speicher.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from werkzeug.utils import secure_filename

from chronicle import db

WARTET = "wartet"
LAEUFT = "laeuft"
FERTIG = "fertig"
GESCHEITERT = "gescheitert"

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
    "t.id AS transcript_id FROM recording r "
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
    connection = _open(database_path)
    try:
        rows = connection.execute(
            AUSWAHL + "WHERE r.status = ? ORDER BY r.id", (WARTET,)
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
