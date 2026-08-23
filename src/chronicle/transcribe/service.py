"""Der Stapellauf: Spur einreihen, Namen mitgeben, Segmente ablegen.

Erkannt wird seit #216 nicht mehr hier, sondern bei ``solaris-whisper-batch``
(``transcribe.client``). Warteschlange, Fristen, Besitzer und Herzschlag bleiben unser —
und mit ihnen die Entscheidung, was mit einer Spur geschieht, die nicht drankam.

Ein Lauf ist eine Datei. Er darf jederzeit abgebrochen und **von vorn** wiederholt werden:
das Transkript einer Quelle wird im Ganzen ersetzt, ein zweiter Lauf hinterlässt also keine
Dubletten. Mitten in einer Datei weiterzumachen ginge nur mit einem zweiten, geschnittenen
Modelllauf — und genau davon lebt die Erkennung nicht.

Eine Sprecherspur sind seit #217 mehrere Dateien: der Mitschnitt wird in Häppchen
geschnitten, damit diese Stufe schon **während** der Sitzung arbeitet statt erst danach.
Für den Lauf ändert das nichts — jede Datei ist eine Quelle wie zuvor —, außer an einer
Stelle: ``recording.offset_ms`` sagt, wo die Datei auf der Sitzungsuhr beginnt, und
``segment_rows`` schlägt ihn auf. Was gespeichert wird, bleibt damit sitzungsabsolut.

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

Dieselbe Schranke ein zweites Mal, hinter dem Modell: ``_ohne_papagei`` verwirft, was nur
das vorgespannte Namensregister zurückgibt (#262). Die erste Schranke fragt vor dem Lauf,
ob überhaupt Ton da ist; diese fragt danach, ob das Zurückgekommene Rede war.
"""

from __future__ import annotations

import logging
import threading
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
from chronicle.transcribe.client import (
    Segment,
    SpeechModel,
    TranscriberUnreachable,
    WhisperBatch,
)

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

# **Der Erkenner verträgt keine Gleichzeitigkeit.** Gemessen am 2026-08-22: vier Anfragen
# innerhalb von sechzehn Sekunden gegen ``solaris-whisper-batch``, drei davon HTTP 500;
# dieselbe Datei allein aufgerufen kam in sechs Sekunden durch. Seit #269 arbeitet
# ``chronicle.mitlauf`` die Warteschlange schon **während** der Sitzung ab, und der
# Nachtlauf, der Abschluss und der Stapelaufruf tun es weiterhin — vier Wege in denselben
# Dienst, und der erste läuft, während die anderen jederzeit anspringen können.
#
# Also läuft immer nur ein Durchgang; wer dazukommt, wartet. Das Schloss liegt um den
# **ganzen** Durchgang und nicht um die einzelne Anfrage: die Liste der wartenden Spuren
# steht vor der Schleife fest, zwei Durchgänge nebeneinander nähmen sich sonst dieselbe
# Spur vor und verschrifteten sie zweimal. Warten kostet hier nichts — kein Aufrufer liegt
# auf der Ereignisschleife, alle vier laufen in eigenen Fäden.
_ERKENNER = threading.Lock()


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
    def stumm(self) -> bool:
        """Der Lauf fand keine einzige Äußerung — und hat damit nichts zu melden.

        Seit der Mitschnitt in Häppchen läuft (#217) ist das der Normalfall: wer eine halbe
        Stunde zuhört, hinterlässt ein Häppchen ohne ein Wort darin. Erfundene Sätze fangen
        die Stille-Erkennung des Nachbardienstes und ``_ohne_papagei`` (#209, #262) davor
        ab — der zweite Riegel, weil der erste seit #216 nicht mehr unserer ist und ein
        Häppchen aus reiner Stille auch dann noch das Register zurückgab. Bliebe die
        Meldung, läse die Runde am Ende einer Sitzung zwanzigmal denselben Satz über
        nichts. Der Stand steht weiter an der Zeile — er ist gesucht auffindbar, drängt
        sich aber niemandem auf.

        Nicht dasselbe wie ``uebersprungen``: das ist der Fall, in dem gar nicht erst
        gerechnet wurde, und **der** gehört gesagt.
        """
        return not self.uebersprungen and not self.segment_count

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


def segment_rows(
    segments: Iterable[Segment], *, offset_ms: int = 0
) -> tuple[tuple[int, int, str], ...]:
    """Sekunden werden Millisekunden; leere Segmente fallen weg.

    Ein Ende vor dem Anfang kommt aus dem Modell und nicht aus der Wirklichkeit — es
    wird auf den Anfang gezogen, damit die Zusammenführung später eine Zeitachse hat.

    ``offset_ms`` ist der Platz der **Datei** auf der Sitzungsuhr. Der Erkenner zählt ab
    dem Anfang dessen, was er bekommen hat; ein Häppchen aus der Mitte eines Abends (#217)
    fängt für ihn deshalb wieder bei null an. Hier — und nur hier — wird das gerade
    gezogen: was in ``transcript_segment`` landet, ist sitzungsabsolut, so wie
    ``chronicle.transcribe.merge`` es voraussetzt. Ohne diesen Zuschlag zerfiele die
    Verschränkung der Sprecher und jede Äußerung fiele in die falsche Szene, und zwar
    still.
    """
    zeilen = []
    for teil in segments:
        text = teil.text.strip()
        if not text:
            continue
        beginn = offset_ms + max(0, round(teil.start * 1000))
        zeilen.append((beginn, max(beginn, offset_ms + round(teil.end * 1000)), text))
    return tuple(zeilen)


def kennung(session_id: int, job_id: int | None = None) -> str:
    """Woran der Betreiber eine Zeile dieses Laufs festmacht: Zahlen statt Namen.

    Der Stamm eines Spurnamens ist der Anzeigename des Sprechers, und der Dateiname trägt
    ihn mit (#194, #199) — beides geht das Log des Betreibers nichts an. Übrig bleiben die
    Job-Id und die Sitzung: laufende Nummern, die außerhalb dieser Datenbank nichts
    bedeuten und drinnen in einem Schritt zu Datei, Stand und Meldung führen. Wer von Hand
    eine einzelne Datei verschriftet, hat keine Job-Id; dann steht die Sitzung allein da,
    denn mehr weiß dieser Aufruf nicht.
    """
    if job_id is None:
        return f"Sitzung {session_id}"
    return f"Job {job_id} in Sitzung {session_id}"


def _ohne_papagei(
    segments: Iterator[Segment], register: tuple[str, ...], marke: str
) -> Iterator[Segment]:
    """Was nur das vorgespannte Register zurückgibt, ist keine Äußerung (#262).

    **Warum diese Schranke hier steht und nicht drüben.** Die Stille-Erkennung aus #209
    war ``vad_filter`` an unserem eigenen faster-whisper; mit #216 ist das Modell in den
    Nachbardienst gezogen, und der Schalter mit ihm. ``POST /transcribe`` nimmt Pfad,
    Sprache und Wortvorgaben entgegen und sonst nichts — wir können dort weder einstellen
    noch ablesen, wie mit Stille verfahren wird. Was durchkommt, kommt mit **unserem**
    Register zurück, und nur wir kennen es. Also liegt der zweite Riegel hier, und er hält
    unabhängig davon, was der Nachbar tut.

    Gezählt wird, nicht genannt: die verworfenen Texte sind Figuren- und Spielernamen und
    haben im Log des Betreibers nichts verloren (#194, #199).
    """
    verworfen = 0
    for teil in segments:
        if vocabulary.registerpapagei(teil.text, register):
            verworfen += 1
            continue
        yield teil
    if verworfen:
        logger.info("%s: %s Segmente verworfen — nur das Register, keine Rede", marke, verworfen)


def _mit_fortschritt(segments: Iterator[Segment], marke: str) -> Iterator[Segment]:
    gemeldet = 0.0
    for teil in segments:
        if teil.end - gemeldet >= MELDEABSTAND:
            gemeldet = teil.end
            logger.info("%s: transkribiert bis %s", marke, zeitmarke(teil.end))
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
    # Der Client redet nur; gebaut ist er in einer Zeile und ohne Netz. Erst der Aufruf
    # merkt, ob der Dienst da ist.
    return WhisperBatch(config)


def transcribe_session(
    config: Config,
    runde: Runde,
    session_id: int,
    audio_path: Path,
    *,
    model: SpeechModel | None = None,
    source: str | None = None,
    job_id: int | None = None,
    delete_audio: bool = False,
    offset_ms: int = 0,
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
        marke = kennung(session_id, job_id)
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
        erkenner = model if model is not None else model_from_config(config)

        logger.info("%s: Spur beginnt, %s Namen vorgegeben", marke, len(eigennamen))
        segmente = segment_rows(
            _ohne_papagei(
                _mit_fortschritt(erkenner.transcribe(audio_path, hotwords=eigennamen), marke),
                eigennamen,
                marke,
            ),
            offset_ms=offset_ms,
        )
        store(scope, session_id, spur, segmente, _now())
    finally:
        scope.close()

    if delete_audio:
        audio_path.unlink()
        logger.info("%s: Tondatei auf Verlangen gelöscht", marke)

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
    mitlaufend: bool = False,
) -> tuple[str, ...]:
    """Arbeitet die wartenden Spuren ab — der Stapel, den die Oberfläche befüllt.

    Das Modell wird erst geladen, wenn wirklich etwas wartet: ein leerer Lauf soll
    nichts kosten, damit er stündlich stehen darf.

    In der Warteschlange steht seit #247 auch, was schon einmal gescheitert ist — bis zu
    ``recordings.MAX_VERSUCHE`` Anläufe. Wiederholt wird von vorn, und das kostet nichts:
    das Transkript einer Quelle wird ohnehin im Ganzen ersetzt. Innerhalb **eines** Laufs
    wiederholt wird nichts: die Liste steht vor der Schleife fest.

    Ein Fehlschlag an einer Spur hält die übrigen nicht auf — er wird zur Meldung und zum
    Stand an ihrer Zeile, und die Schleife geht weiter. Die eine Ausnahme ist der
    abgeschaltete Erkenner: der ist keine Eigenschaft dieser Spur, und die übrigen liefen
    in denselben Fehler.

    Am Ende wird die zugesagte Aufbewahrungsfrist durchgesetzt — auch nach einem leeren
    Lauf, denn zugesagt ist sie unabhängig davon, ob heute etwas zu tun war.

    ``mitlaufend`` ist der Durchgang **während** der Sitzung (``chronicle.mitlauf``, #269).
    Er unterscheidet sich in zweierlei, und beides hat denselben Grund — er sagt niemandem
    etwas, also darf er niemandem etwas wegnehmen:

    * Er nimmt nur, was noch **keinen** Anlauf hinter sich hat. Sonst verbrauchte eine
      gescheiterte Spur ihre drei Anläufe in drei Minuten statt in drei Nächten, und
      ``recordings.MAX_VERSUCHE`` stünde für etwas anderes als das, was daneben steht: die
      gemessenen Gründe waren vorübergehend, aber nicht binnen einer Minute vorbei.
    * Er räumt die Frist nicht ab. ``recordings.sweep`` **meldet**, was es löscht, und
      besonders die nie verschriftete Spur; diese Meldung gehört der Runde und erreicht
      sie über den Nachtlauf. Hier fiele sie in einen Rückgabewert, den niemand liest.
      Durchgesetzt wird die Frist deshalb weiter von ``recordings.taeglich`` und vom
      Nachtlauf, und zwar unverändert.
    """
    db.init(config.database_path)
    # Eine ruhende Runde verschriftet nicht mehr: die eingereihten Spuren würden sonst
    # nach dem Rauswurf noch wochenlang zu neuem Text. Die Aufbewahrungsfrist der Dateien
    # setzt ``recordings.sweep_alle`` durch, die gilt ihr weiter.
    if lebenszyklus.ruht(runde):
        return ()
    meldungen = list(_abarbeiten(config, runde, model, delete_audio, mitlaufend))
    if not mitlaufend:
        meldungen.extend(recordings.sweep(config, runde))
    return tuple(meldungen)


def _abarbeiten(
    config: Config,
    runde: Runde,
    model: SpeechModel | None,
    delete_audio: bool,
    mitlaufend: bool,
) -> tuple[str, ...]:
    """Die Schleife durch die Warteschlange — unter dem Schloss, immer nur einmal zugleich."""
    with _ERKENNER:
        # Vor dem Blick in die Warteschlange: was ein Neustart auf ``laeuft`` stehen ließ,
        # gehört wieder hinein. Sonst wäre die Spur für ``pending`` unsichtbar und verlöre
        # nach der Frist ihre Datei, ohne je verschriftet worden zu sein (#181).
        recordings.zurueckstellen(runde)
        wartend = recordings.pending(runde)
        if mitlaufend:
            wartend = tuple(spur for spur in wartend if spur.versuche == 0)
        if not wartend:
            return ()
        erkenner = model if model is not None else model_from_config(config)
        meldungen = []
        for aufnahme in wartend:
            try:
                with recordings.in_arbeit(runde, aufnahme.id):
                    meldung, gelungen, stumm = _eine_spur(
                        config, runde, aufnahme, erkenner, delete_audio
                    )
                    stand = recordings.FERTIG if gelungen else recordings.GESCHEITERT
                    recordings.mark(runde, aufnahme.id, stand, meldung)
            except TranscriberUnreachable as fehler:
                # **Nicht gescheitert, nur nicht drangekommen.** Seit der lokale Weg weg
                # ist (#216), gibt es für einen abgeschalteten Erkenner keinen Rückfall
                # mehr — also geht die Spur zurück in die Warteschlange, statt einen Stand
                # zu bekommen, der »verloren« bedeutet. Die übrigen werden gar nicht erst
                # versucht: sie liefen in denselben Fehler und stünden hinterher mit
                # derselben Meldung da.
                recordings.mark(runde, aufnahme.id, recordings.WARTET, str(fehler))
                meldungen.append(str(fehler))
                break
            # Der Stand steht an der Zeile, gemeldet wird nur, was etwas ergab.
            if not stumm:
                meldungen.append(meldung)
        return tuple(meldungen)


def _eine_spur(
    config: Config,
    runde: Runde,
    aufnahme: recordings.Recording,
    erkenner: SpeechModel,
    delete_audio: bool,
) -> tuple[str, bool, bool]:
    """Meldung, ob es gelang, und ob der Lauf stumm blieb — Letzteres bleibt ungesagt."""
    pfad = recording_path(config, aufnahme.filename)
    if not pfad.is_file():
        return f"Spur »{aufnahme.source}«: {pfad} liegt nicht mehr da.", False, False
    try:
        transkript = transcribe_session(
            config,
            runde,
            aufnahme.session_id,
            pfad,
            model=erkenner,
            source=aufnahme.source,
            job_id=aufnahme.id,
            delete_audio=delete_audio,
            offset_ms=aufnahme.offset_ms,
        )
        return transkript.message, True, transkript.stumm
    # Der Erkenner ist aus — das ist keine Eigenschaft dieser Spur, und sie darf dafür
    # keinen Stand bekommen. Der Aufrufer stellt sie zurück.
    except TranscriberUnreachable:
        raise
    # Eine kaputte Spur — abgebrochene Aufnahme, umbenannte Textdatei — darf die übrigen
    # Jobs der Nacht nicht mitnehmen.
    except Exception as fehler:  # noqa: BLE001
        # Nur die Fehlerart, wie in ``notes._tondateien_loeschen``: der Text eines
        # Datei-Fehlers führt den Pfad mit sich und damit den Sprechernamen (#199). Der
        # ganze Text geht trotzdem nicht verloren — er steht in der Meldung an die Runde
        # und im Stand der Aufnahme, wo er die Gruppe angeht und nicht den Betreiber.
        logger.warning("%s: %s", kennung(aufnahme.session_id, aufnahme.id), type(fehler).__name__)
        return f"Spur »{aufnahme.source}«: {fehler}", False, False
