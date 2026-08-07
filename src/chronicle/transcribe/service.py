"""Der Stapellauf: Spur lesen, Namen vorspannen, Segmente ablegen.

Ein Lauf ist eine Spur. Er läuft eine gute Stunde je Sitzungsstunde und darf jederzeit
abgebrochen und **von vorn** wiederholt werden: das Transkript einer Spur wird im
Ganzen ersetzt, ein zweiter Lauf hinterlässt also keine Dubletten. Mitten in einer Spur
weiterzumachen ginge nur mit einem zweiten, geschnittenen Modelllauf — und genau davon
lebt die Erkennung nicht.

Gemeldet wird, wo im Band der Lauf steht, nicht wann er fertig ist. Eine Restzeit wäre
geraten, und geraten wird hier nichts.

Die Audiodatei bleibt liegen. Gelöscht wird sie nur, wenn der Aufrufer es ausdrücklich
verlangt — eine Aufnahme still zu entfernen wäre der teuerste stille Fehler.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db, recordings
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.runde import Runde
from chronicle.transcribe import vocabulary
from chronicle.transcribe.client import FasterWhisper, Segment, SpeechModel

logger = logging.getLogger(__name__)

KIND = "transkript"

# So viele Sekunden Audio liegen zwischen zwei Fortschrittsmeldungen.
MELDEABSTAND = 60.0


@dataclass(frozen=True)
class Transcript:
    session_id: int
    source: str
    segment_count: int
    audio_seconds: float
    model_name: str
    vocabulary_names: int

    @property
    def message(self) -> str:
        return (
            f"Spur »{self.source}«: {self.segment_count} Segmente bis "
            f"{zeitmarke(self.audio_seconds)}, erkannt mit {self.model_name}, "
            f"{self.vocabulary_names} Namen vorgespannt."
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def zeitmarke(seconds: float) -> str:
    ganze = int(seconds)
    return f"{ganze // 3600}:{ganze // 60 % 60:02d}:{ganze % 60:02d}"


@runden.instanzweit
def recording_path(config: Config, name: str) -> Path:
    """Ein relativer Name liegt im Aufnahmeverzeichnis, nicht im Datenverzeichnis."""
    pfad = Path(name)
    return pfad if pfad.is_absolute() else config.recordings_dir / pfad


def names(scope: db.Scope, session_id: int) -> tuple[str, ...]:
    """Erst die Namen, die in dieser Sitzung gesprochen haben, dann der Zwischenspeicher."""
    gesprochen = scope.execute(
        "SELECT DISTINCT m.speaker_alias AS name FROM scene_foundry_message v "
        "JOIN scene c ON c.id = v.scene_id "
        "JOIN foundry_message m ON m.id = v.message_id AND m.runde_id = v.runde_id "
        "WHERE v.runde_id = ? AND c.session_id = ? AND m.speaker_alias IS NOT NULL "
        "ORDER BY m.speaker_alias",
        (scope.runde_id, session_id),
    ).fetchall()
    bekannt = scope.execute(
        "SELECT name FROM foundry_character WHERE runde_id = ? ORDER BY name", (scope.runde_id,)
    ).fetchall()
    return tuple(zeile["name"] for zeile in (*gesprochen, *bekannt))


def segment_rows(segments: Iterable[Segment]) -> tuple[tuple[int, int, str], ...]:
    """Sekunden werden Millisekunden; leere Segmente fallen weg.

    Ein Ende vor dem Anfang kommt aus dem Modell und nicht aus der Wirklichkeit — es
    wird auf den Anfang gezogen, damit die Zusammenführung später eine Zeitachse hat.
    """
    zeilen = []
    for teil in segments:
        text = teil.text.strip()
        if not text:
            continue
        beginn = max(0, round(teil.start * 1000))
        zeilen.append((beginn, max(beginn, round(teil.end * 1000)), text))
    return tuple(zeilen)


def _mit_fortschritt(segments: Iterator[Segment], source: str) -> Iterator[Segment]:
    gemeldet = 0.0
    for teil in segments:
        if teil.end - gemeldet >= MELDEABSTAND:
            gemeldet = teil.end
            logger.info("Spur %s: transkribiert bis %s", source, zeitmarke(teil.end))
        yield teil


def store(
    scope: db.Scope,
    session_id: int,
    source: str,
    rows: tuple[tuple[int, int, str], ...],
    at: str,
) -> int:
    with scope:
        scope.execute(
            "DELETE FROM transcript WHERE runde_id = ? AND session_id = ? AND source = ?",
            (scope.runde_id, session_id, source),
        )
        cursor = scope.execute(
            "INSERT INTO transcript (runde_id, session_id, source, created_at) VALUES (?, ?, ?, ?)",
            (scope.runde_id, session_id, source, at),
        )
        transcript_id = int(cursor.lastrowid)
        scope.executemany(
            "INSERT INTO transcript_segment (runde_id, transcript_id, start_ms, end_ms, text) "
            "VALUES (?, ?, ?, ?, ?)",
            [(scope.runde_id, transcript_id, *zeile) for zeile in rows],
        )
    return transcript_id


@runden.instanzweit
def model_from_config(config: Config) -> SpeechModel:
    return FasterWhisper(config.whisper_model)


def transcribe_session(
    config: Config,
    runde: Runde,
    session_id: int,
    audio_path: Path,
    *,
    model: SpeechModel | None = None,
    source: str | None = None,
    delete_audio: bool = False,
) -> Transcript | None:
    db.init(config.database_path)
    scope = db.scoped(runde)
    try:
        bekannt = scope.execute(
            "SELECT 1 FROM session WHERE runde_id = ? AND id = ?", (scope.runde_id, session_id)
        ).fetchone()
        if bekannt is None:
            return None
        eigennamen = vocabulary.capped(names(scope, session_id))
        vorspann = vocabulary.prompt(eigennamen)
        spur = source or audio_path.stem
        erkenner = model if model is not None else model_from_config(config)

        logger.info("Spur %s: %s beginnt, %s Namen vorgespannt", spur, audio_path, len(eigennamen))
        segmente = segment_rows(
            _mit_fortschritt(erkenner.transcribe(audio_path, vocabulary=vorspann), spur)
        )
        store(scope, session_id, spur, segmente, _now())
    finally:
        scope.close()

    if delete_audio:
        audio_path.unlink()
        logger.info("Spur %s: %s auf Verlangen gelöscht", spur, audio_path)

    return Transcript(
        session_id=session_id,
        source=spur,
        segment_count=len(segmente),
        audio_seconds=segmente[-1][1] / 1000 if segmente else 0.0,
        model_name=erkenner.name,
        vocabulary_names=len(eigennamen),
    )


def run_queue(
    config: Config,
    runde: Runde,
    *,
    model: SpeechModel | None = None,
    delete_audio: bool = False,
) -> tuple[str, ...]:
    """Arbeitet die wartenden Spuren ab — der Stapel, den die Oberfläche befüllt.

    Das Modell wird erst geladen, wenn wirklich etwas wartet: ein leerer Lauf soll
    nichts kosten, damit er stündlich stehen darf.

    Am Ende wird die zugesagte Aufbewahrungsfrist durchgesetzt — auch nach einem leeren
    Lauf, denn zugesagt ist sie unabhängig davon, ob heute etwas zu tun war.
    """
    db.init(config.database_path)
    wartend = recordings.pending(runde)
    meldungen = []
    if wartend:
        erkenner = model if model is not None else model_from_config(config)
        for aufnahme in wartend:
            recordings.mark(runde, aufnahme.id, recordings.LAEUFT)
            meldung, gelungen = _eine_spur(config, runde, aufnahme, erkenner, delete_audio)
            stand = recordings.FERTIG if gelungen else recordings.GESCHEITERT
            recordings.mark(runde, aufnahme.id, stand, meldung)
            meldungen.append(meldung)
    meldungen.extend(recordings.sweep(config, runde))
    return tuple(meldungen)


def _eine_spur(
    config: Config,
    runde: Runde,
    aufnahme: recordings.Recording,
    erkenner: SpeechModel,
    delete_audio: bool,
) -> tuple[str, bool]:
    pfad = recording_path(config, aufnahme.filename)
    if not pfad.is_file():
        return f"Spur »{aufnahme.source}«: {pfad} liegt nicht mehr da.", False
    try:
        transkript = transcribe_session(
            config,
            runde,
            aufnahme.session_id,
            pfad,
            model=erkenner,
            source=aufnahme.source,
            delete_audio=delete_audio,
        )
        return transkript.message, True
    # Eine kaputte Spur — abgebrochene Aufnahme, umbenannte Textdatei — darf die übrigen
    # Jobs der Nacht nicht mitnehmen.
    except Exception as fehler:  # noqa: BLE001
        logger.warning("Spur %s: %s", aufnahme.source, fehler)
        return f"Spur »{aufnahme.source}«: {fehler}", False
