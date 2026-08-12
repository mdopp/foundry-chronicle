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
    started_at: str | None = None
    message_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        started_at=row["started_at"] if "started_at" in row.keys() else None,
        message_at=row["message_at"] if "message_at" in row.keys() else None,
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


def accept(config, runde: Runde, session_id: int, datei) -> Recording:
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
    return enqueue(runde, session_id, ziel.name)


def enqueue(
    runde: Runde,
    session_id: int,
    filename: str,
    *,
    discord_user_id: str | None = None,
    started_at: str | None = None,
    message_at: str | None = None,
) -> Recording:
    """Reiht eine Spur ein.

    ``discord_user_id`` steht nur bei den Spuren des Aufnahme-Bots: dort trennt Discord
    die Audiodaten je Client, wer gesprochen hat ist also bekannt und muss nicht später
    aus dem Dateinamen geraten werden — geraten stünde irgendwann der falsche Name über
    einem Absatz.

    ``started_at`` ebenso, und es ist der Nullpunkt der Sitzungsuhr: der Moment, in dem
    der Mitschnitt begann. Ohne ihn hat die Spur keine Zeitachse, die sich auf die Szenen
    legen ließe — ein Diktat vom Heimweg bekommt deshalb keinen, und zwar auch dann
    nicht, wenn es als Anhang im Thread landet und darum eine Discord-Id trägt.

    ``message_at`` ist der Zeitpunkt der **Nachricht**, mit der ein Diktat im Thread ankam
    — der Anker, über den es trotz fehlender Sitzungsuhr in eine Szene findet. Es ist
    ausdrücklich nicht ``uploaded_at``: das steht auf dem Ablegen, und ein Diktat vom
    Heimweg käme damit in der Szene an, die gerade zufällig die letzte ist.

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
                "status, updated_at, discord_user_id, started_at, message_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    session_id,
                    filename,
                    Path(filename).stem,
                    zeitpunkt,
                    WARTET,
                    zeitpunkt,
                    discord_user_id,
                    started_at,
                    message_at,
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
        started_at=started_at,
        message_at=message_at,
    )


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
    "r.deleted_at, r.discord_user_id, r.started_at, r.message_at, "
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


def pending(runde: Runde) -> tuple[Recording, ...]:
    """Was noch wartet — ohne die Spuren, deren Audio die Frist schon geholt hat."""
    scope = db.scoped(runde)
    try:
        rows = scope.execute(
            AUSWAHL + "AND r.status = ? AND r.deleted_at IS NULL ORDER BY r.id",
            (scope.runde_id, WARTET),
        ).fetchall()
    finally:
        scope.close()
    return tuple(_eintrag(row) for row in rows)


def mark(runde: Runde, recording_id: int, status: str, detail: str | None = None) -> None:
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "UPDATE recording SET status = ?, detail = ?, updated_at = ? "
                "WHERE runde_id = ? AND id = ?",
                (status, detail, _now(), scope.runde_id, recording_id),
            )
    finally:
        scope.close()


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


def _als_geloescht_vermerken(runde: Runde, recording_id: int) -> None:
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "UPDATE recording SET deleted_at = ?, updated_at = ? WHERE runde_id = ? AND id = ?",
                (zeitpunkt, zeitpunkt, scope.runde_id, recording_id),
            )
    finally:
        scope.close()


def sweep(config, runde: Runde, *, tage: int = RETENTION_TAGE) -> tuple[str, ...]:
    """Setzt die zugesagte Frist durch: Audio weg, Zeile bleibt.

    Beliebig oft aufrufbar — eine bereits vermerkte Spur wird nicht noch einmal gesucht,
    und eine schon von Hand entfernte Datei ist kein Fehlschlag.
    """
    meldungen = []
    for aufnahme in expired(runde, tage=tage):
        (config.recordings_dir / aufnahme.filename).unlink(missing_ok=True)
        _als_geloescht_vermerken(runde, aufnahme.id)
        meldung = NACH_FRIST.format(source=aufnahme.source, tage=tage)
        logger.info("%s", meldung)
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
