"""Der Zugang zum Spracherkenner: ein HTTP-Aufruf gegen ``solaris-whisper-batch``.

Wir halten **kein Whisper-Modell mehr im Prozess** (Betreiber-Entscheidung 2026-08-18,
#216). Die Stapelarbeit macht der Nachbardienst auf seiner Karte; von faster-whisper,
PyAV und onnxruntime bleibt in diesem Image nichts. Die Schnittstelle ist dieselbe
schmale wie vorher — eine Datei hinein, Segmente heraus —, nur liegt das Modell jetzt
auf der anderen Seite eines Sockets.

**Das Modell wählt der Dienst, nicht wir.** ``POST /transcribe`` nimmt ``path``,
``language`` und ``hotwords`` entgegen und sonst nichts; welches Modell rechnet, steht
in seiner Unit (``WHISPER_BATCH_MODEL``, dort ``large-v3-turbo``,
``mdopp/solarisbay#1161``). Deshalb meldet ``name`` den **Dienst** und nicht ein
Modell: eine Modellbezeichnung, die wir nicht erfragen können, wäre geraten, und
geraten wird hier nichts.

**Übergeben wird ein Pfad, keine Bytes.** Beide Dienste laufen auf derselben Box, und
der Dienst hängt das Aufnahmeverzeichnis schreibgeschützt unter seinem Host-Pfad ein.
Vier Stunden × fünf Sprecher sind 439 MB je Spur auch in 16 kHz Mono — die durch einen
Socket zu schieben brächte nichts, es ist ein Stapellauf. Geschickt wird der Name
**relativ** zum Aufnahmeverzeichnis: unser Container sieht es unter ``/aufnahmen``,
der Dienst unter seinem eigenen Pfad, und der gemeinsame Nenner ist der Name darin.

**Die Namen gehen als ``hotwords`` mit, nicht als Vorspann.** ``initial_prompt`` wirkt
nur auf das erste 30-Sekunden-Fenster, ``hotwords`` wird in jedes wieder eingespeist —
bei einer Sitzungsspur ist das der Unterschied zwischen »die ersten dreißig Sekunden
kennen die Figuren« und »die ganze Spur«. Der Dienst passt die Liste selbst exakt ins
Token-Budget und meldet, was er fallen ließ; unsere Kappung (``vocabulary.capped``)
bleibt die Rangfolge davor.

**Und es gibt keinen Rückfall mehr.** Der CPU-Weg aus #84 ist ersatzlos entfallen. Ist
der Dienst aus, gibt es kein Transkript — dann bleibt die Spur liegen und sagt es,
statt still eine Chronik ohne das gesprochene Wort entstehen zu lassen (#221). Dafür
ist ``TranscriberUnreachable`` von ``TranscriberError`` getrennt: das eine heißt
»später nochmal«, das andere »an dieser Spur«.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

from chronicle import sprache as sprachen
from chronicle.config import DEFAULT_WHISPER_URL, Config

logger = logging.getLogger(__name__)

TRANSCRIBE_PATH = "/transcribe"

# Was in der Meldung steht, wenn jemand fragt, womit erkannt wurde. Der Dienst nennt
# sein Modell nicht, und wir behaupten keins.
NAME = "solaris-whisper-batch"

# Der Sprachcode reist mit der **Spur** und steht nicht mehr fest im Client (#268): der
# Erkenner ist instanzweit, die Sprache gehört der Runde. Deutsche Rede englisch zu
# verschriften ergibt Unsinn, und umgekehrt genauso — das ist der ganze Grund, warum der
# Inhalt der Einstellung folgt und nicht der Bedienoberfläche.
SPRACHE = sprachen.WHISPER[sprachen.DEFAULT]

# Stapelbetrieb: die Antwort kommt erst, wenn die ganze Datei durch ist. Auf der Karte
# ist eine Vier-Stunden-Spur ~10 min (mdopp/solarisbay#1161); eine Stunde Frist lässt
# reichlich Luft und hängt trotzdem nicht bis in den nächsten Tag.
DEFAULT_TIMEOUT = 3600.0

# Der Dienst lädt sein Modell, bevor er den Port bindet — 503 heißt also »noch nicht
# so weit«, nicht »kaputt«. Beides gehört auf denselben Weg: warten.
NICHT_BEREIT = 503

NICHT_ERREICHBAR = (
    "The speech recogniser {url} cannot be reached ({grund}) — the track stays put and "
    "will be transcribed as soon as it is back. Without it no chronicle comes out of this "
    "session; a fallback to the CPU has not existed since #216."
)

ABGEWIESEN = "The speech recogniser {url} rejected the track: HTTP {code}."

# Mit Grund, wenn die Antwort einen nennt. Ohne diesen Halbsatz kostete #248 Stunden:
# der Dienst schrieb »ValueError: The maximum decoding length must be > 0« in den Rumpf,
# und die Meldung warf ihn weg. Der Text geht in den Stand der Aufnahme und in die
# Meldung an die Runde; ins Log des Betreibers kommt weiterhin nur die Fehlerart.
ABGEWIESEN_MIT_GRUND = "The speech recogniser {url} rejected the track: HTTP {code} — {grund}"

# Fremder Text, also gekürzt und einzeilig: eine HTML-Fehlerseite soll die Statuszeile
# der Runde nicht fluten.
GRUND_MAX = 200

KEINE_SEGMENTE = "The speech recogniser {url} returned no segments."

# Der Pfad einer Spur trägt den Anzeigenamen des Sprechers (#194, #199) — er steht
# deshalb nicht in dieser Meldung, obwohl sie von einem Pfad handelt.
AUSSERHALB = (
    "This track is not in the recordings directory — the speech recogniser reads only there."
)


@dataclass(frozen=True)
class Segment:
    """Ein Stück Rede: Sekunden ab Dateibeginn, dazu der Text."""

    start: float
    end: float
    text: str


class TranscriberError(RuntimeError):
    """Was an dieser einen Spur scheitert — die übrigen laufen weiter."""


class TranscriberUnreachable(TranscriberError):
    """Der Dienst antwortet nicht. Keine Spur wird deshalb als gescheitert vermerkt."""


class SpeechModel(Protocol):
    """Die ganze Abhängigkeit der Transkription zum Spracherkenner."""

    @property
    def name(self) -> str: ...

    def transcribe(
        self, audio_path: Path, *, hotwords: Sequence[str] = (), sprache: str = SPRACHE
    ) -> Iterator[Segment]: ...


def _http_session() -> requests.Session:
    return requests.Session()


def _grund(antwort) -> str:
    """Was die Gegenstelle zu ihrem Fehler sagt — als eine gekürzte Zeile."""
    try:
        rumpf = antwort.json()
    except ValueError:
        rumpf = None
    gesagt = ""
    if isinstance(rumpf, Mapping):
        gesagt = str(rumpf.get("error") or rumpf.get("detail") or "")
    if not gesagt:
        gesagt = str(getattr(antwort, "text", "") or "")
    return " ".join(gesagt.split())[:GRUND_MAX]


class WhisperBatch:
    """Der dünne Client. Er lädt nichts, er hält nichts, er fragt."""

    def __init__(
        self,
        config: Config,
        *,
        http: Callable[[], object] = _http_session,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base = (config.whisper_url or DEFAULT_WHISPER_URL).rstrip("/")
        self._recordings_dir = config.recordings_dir
        self._http = http()
        self._timeout = timeout

    @property
    def name(self) -> str:
        return NAME

    @property
    def url(self) -> str:
        return self._base

    def _name_im_verzeichnis(self, audio_path: Path) -> str:
        try:
            return str(Path(audio_path).resolve().relative_to(self._recordings_dir.resolve()))
        except ValueError:
            raise TranscriberError(AUSSERHALB) from None

    def transcribe(
        self, audio_path: Path, *, hotwords: Sequence[str] = (), sprache: str = SPRACHE
    ) -> Iterator[Segment]:
        pfad = self._name_im_verzeichnis(audio_path)
        logger.info("Spracherkenner %s auf %s", NAME, self._base)
        try:
            antwort = self._http.post(
                self._base + TRANSCRIBE_PATH,
                json={"path": pfad, "language": sprache, "hotwords": list(hotwords)},
                timeout=self._timeout,
            )
        except requests.RequestException as fehler:
            raise TranscriberUnreachable(
                NICHT_ERREICHBAR.format(url=self._ziel, grund=type(fehler).__name__)
            ) from None
        if antwort.status_code == NICHT_BEREIT:
            raise TranscriberUnreachable(
                NICHT_ERREICHBAR.format(url=self._ziel, grund=f"HTTP {NICHT_BEREIT}")
            )
        if antwort.status_code >= 400:
            grund = _grund(antwort)
            raise TranscriberError(
                ABGEWIESEN_MIT_GRUND.format(url=self._ziel, code=antwort.status_code, grund=grund)
                if grund
                else ABGEWIESEN.format(url=self._ziel, code=antwort.status_code)
            )
        return iter(self._segmente(antwort))

    @property
    def _ziel(self) -> str:
        return self._base + TRANSCRIBE_PATH

    def _segmente(self, antwort) -> tuple[Segment, ...]:
        try:
            rumpf = antwort.json()
        except ValueError:
            raise TranscriberUnreachable(
                NICHT_ERREICHBAR.format(url=self._ziel, grund="no JSON")
            ) from None
        teile = rumpf.get("segments") if isinstance(rumpf, Mapping) else None
        if not isinstance(teile, list):
            raise TranscriberError(KEINE_SEGMENTE.format(url=self._ziel))
        # Nur die Anzahl: die gekappten Namen sind Figuren- und Spielernamen und haben
        # im Log des Betreibers nichts verloren (#194, #199).
        verworfen = rumpf.get("hotwords_dropped_count")
        if verworfen:
            logger.info("%s: %s Namen passten nicht ins Budget des Erkenners", NAME, verworfen)
        return tuple(
            Segment(
                start=float(teil.get("start") or 0.0),
                end=float(teil.get("end") or 0.0),
                text=str(teil.get("text") or ""),
            )
            for teil in teile
            if isinstance(teil, Mapping)
        )
