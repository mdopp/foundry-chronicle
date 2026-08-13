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

**Hier steht die Schranke gegen erfundene Sätze** (``MINDESTDAUER``, #142) und nicht im
Recorder, obwohl der schon prüft, ob eine Spur leer ist. Zwei Gründe: dessen Frage lautet
»ist überhaupt etwas angekommen« und wird über die geschriebenen Bytes beantwortet — eine
andere Frage als »steckt eine Äußerung darin«; und er sieht nur die eigenen Spuren, nicht
die hochgeladenen. Alles trifft sich erst hier, vor dem Modell. Die Schranke zusätzlich
dort zu ziehen hieße, dieselbe Regel an zwei Stellen zu pflegen — und es bliebe der Weg,
auf dem sie umgangen wird.
"""

from __future__ import annotations

import logging
import wave
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db, lebenszyklus, recordings
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.runde import Runde
from chronicle.transcribe import vocabulary
from chronicle.transcribe.client import FasterWhisper, Segment, SpeechModel

logger = logging.getLogger(__name__)

KIND = "transkript"

# So viele Sekunden Audio liegen zwischen zwei Fortschrittsmeldungen.
MELDEABSTAND = 60.0

# Kürzer als das ist keine Äußerung, sondern der Rest einer abgebrochenen Aufnahme — und
# **daraus erfindet Whisper Sätze**. Beim ersten echten Lauf machte es aus einer 15-KB-Spur
# mit 0,08 s Inhalt »Thank you for watching!«, ungekennzeichnet, mitten im verschränkten
# Gespräch (#142). Erfundene Zahlen fängt die Zahlenschranke der Komposition; für erfundene
# Sätze gibt es nichts, und die Hausregel ist eindeutig: eine Lücke schlägt einen Satz, dem
# man nicht ansieht, dass er erfunden ist. Also **verwerfen und sagen**, nicht verschriften
# und kennzeichnen — ein Kennzeichen überlebt das Umschreiben zur Prosa nicht zuverlässig,
# eine nicht vorhandene Zeile schon.
#
# Warum 0,3 s und warum die Länge: gemessen wurden im selben Lauf 0,08 s für die kaputte
# Spur und 18,7 s für die brauchbare. Ein gesprochenes »Ja.« liegt bei einer halben Sekunde
# — die Schwelle liegt also gut viermal über dem Bruchstück und deutlich unter der
# kürzesten echten Äußerung, trifft keins von beiden knapp und unterscheidet damit »kurz«
# von »nichts«. Der Effektivwert wäre die zweite messbare Größe (1236 gegen Spitze 17198),
# ist hier aber bewusst **nicht** die Schranke: ein leiser Sprecher hat einen niedrigen
# Effektivwert, und ihn stummzuschalten wäre derselbe Fehler mit umgekehrtem Vorzeichen.
MINDESTDAUER = 0.3

# Die Meldung geht an die Runde. Sie muss sagen, dass nichts verlorenging: »Spur
# übersprungen« liest sich sonst wie »Aufnahme kaputt«, und wer das nachts im Kanal liest,
# sucht am nächsten Tag nach einer Stunde Ton, die es nie gab.
UEBERSPRUNGEN = (
    "Spur »{source}«: nur {sekunden} Sekunden Ton — darin steckt keine Äußerung, deshalb "
    "wird sie nicht verschriftet. Verlorengegangen ist nichts, und die übrigen Spuren der "
    "Sitzung sind davon unberührt."
)


@dataclass(frozen=True)
class Transcript:
    session_id: int
    source: str
    segment_count: int
    audio_seconds: float
    model_name: str
    vocabulary_names: int
    uebersprungen: bool = False

    @property
    def message(self) -> str:
        if self.uebersprungen:
            return UEBERSPRUNGEN.format(source=self.source, sekunden=f"{self.audio_seconds:.2f}")
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


def spurdauer(pfad: Path) -> float | None:
    """Wie viele Sekunden Ton in der Spur stehen — ``None``, wenn es sich nicht ablesen lässt.

    Nur der WAV-Kopf, ohne zu dekodieren: Rahmen durch Rate. Ein hochgeladenes m4a vom
    Telefon ist ohne Dekodieren nicht messbar und läuft deshalb weiter durch — der
    gemessene Fall ist eine Bot-Spur, und die ist immer WAV. Eine Schranke zu raten, wo
    nicht gemessen werden kann, wäre schlimmer als keine.
    """
    if pfad.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(pfad), "rb") as datei:
            rate = datei.getframerate()
            return datei.getnframes() / rate if rate else None
    except (wave.Error, EOFError, OSError):
        return None


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
    # Beides darf leer sein: dann entscheidet der Fund der Karte (#84).
    return FasterWhisper(config.whisper_model, device=config.whisper_device)


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
        spur = source or audio_path.stem
        dauer = spurdauer(audio_path)
        if dauer is not None and dauer < MINDESTDAUER:
            # Vor dem Modell und vor dem Vorspann: eine Spur ohne Äußerung soll nichts
            # kosten. Die Datei bleibt liegen, auch bei ``delete_audio`` — gelöscht wird,
            # was verschriftet wurde, und die Aufbewahrungsfrist holt sie ohnehin.
            return Transcript(
                session_id=session_id,
                source=spur,
                segment_count=0,
                audio_seconds=dauer,
                model_name="",
                vocabulary_names=0,
                uebersprungen=True,
            )
        eigennamen = vocabulary.capped(names(scope, session_id))
        vorspann = vocabulary.prompt(eigennamen)
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
    # Eine ruhende Runde verschriftet nicht mehr: die eingereihten Spuren würden sonst
    # nach dem Rauswurf noch wochenlang zu neuem Text. Die Aufbewahrungsfrist der Dateien
    # setzt ``recordings.sweep_alle`` durch, die gilt ihr weiter.
    if lebenszyklus.ruht(runde):
        return ()
    # Vor dem Blick in die Warteschlange: was ein Neustart auf ``laeuft`` stehen ließ,
    # gehört wieder hinein. Sonst wäre die Spur für ``pending`` unsichtbar und verlöre
    # nach der Frist ihre Datei, ohne je verschriftet worden zu sein (#181).
    recordings.zurueckstellen(runde)
    wartend = recordings.pending(runde)
    meldungen = []
    if wartend:
        erkenner = model if model is not None else model_from_config(config)
        for aufnahme in wartend:
            with recordings.in_arbeit(runde, aufnahme.id):
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
