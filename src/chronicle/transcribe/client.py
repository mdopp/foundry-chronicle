"""Der Zugang zum Spracherkenner: faster-whisper, auf der Karte wenn eine da ist.

Die Schnittstelle ist absichtlich schmal — eine Datei hinein, Segmente heraus. Alles,
was ein echtes Modell lädt, steckt in dieser Datei; die Tests setzen hier ein erfundenes
Modell ein und laden deshalb nie etwas herunter.

Übergeben wird der **Pfad der ganzen Spur**, nie ein kleiner Puffer. Whisper ist auf
30-Sekunden-Fenster trainiert und lebt vom Kontext; pro Redebeitrag geschnittene
Schnipsel verschlechtern vor allem die Erkennung von Eigennamen — und ein Rollenspiel
besteht aus erfundenen Eigennamen.

**Die Zusage ist »läuft ohne Karte«, nicht »nutzt keine« (#84).** Ist CUDA da, läuft
``large-v3-turbo`` in ``float16``; ist keine da, bleibt es bei ``cpu``/``int8`` und
``small`` wie bisher. Erfundene Eigennamen erkennt das große Modell deutlich besser —
mehr, als der auf 224 Token gedeckelte Vorspann je gutmachen kann. Eine Karte wird
damit nirgends zur Voraussetzung; die Erkennung ist überschreibbar (``config``).

Und sie ist **nicht verlässlich frei**: Ollama hält sein Modell geladen und teilt sich
dieselben 16 GB. Schlägt das Laden auf der Karte fehl, fällt der Lauf auf die CPU
zurück, statt die Nacht abzubrechen — ein langsamer Lauf schlägt einen gescheiterten.

**Die Stille wird übersprungen (#209).** py-cord füllt die Sprechpausen jeder Spur mit
Stille auf, damit alle Spuren auf **einer** Zeitachse liegen; bei vier Stunden Sitzung
ist deshalb jede Sprecherspur vier Stunden lang, auch wenn die Person zwanzig Minuten
geredet hat. Ohne Stille-Erkennung läuft das Modell über all das hinweg — und **auf
langer Stille erfindet Whisper Sätze**: fünf Minuten reine Stille ergaben acht Segmente
im 30-Sekunden-Raster, mit ihr keins. Das trifft die oberste Hausregel, und es kostet
obendrein die Rechenzeit, die ein größeres Modell auf der CPU bräuchte (#141) — an einer
Spur mit 20 % Sprechanteil 143 s gegen 29 s.

Ein Schalter dagegen ist bewusst **nicht** vorgesehen: die Schranke gegen erfundene
Sätze (``service.MINDESTDAUER``, #142) hat auch keinen, und ein abschaltbarer Schutz ist
einer, den man eines Tages abgeschaltet vorfindet. Die Vorgabewerte der Bibliothek
bleiben stehen — an einer Schwelle wird gedreht, wenn ein echter Lauf zeigt, dass ein
leiser Sprecher verschluckt wird, nicht vorher. Das Stille-Modell liegt im Rad von
faster-whisper (``assets/silero_vad_v6.onnx``), es wird nichts nachgeladen; eine Box
ohne Netz merkt davon nichts.

**Die Zeitstempel bleiben sitzungsabsolut.** faster-whisper rechnet die Segmentzeiten
auf die Originalspur zurück; daran hängt die ganze Zusammenführung. Ausgeführt geprüft
gegen bekannte Sprechinseln, nicht der Dokumentation geglaubt: jeder Segmentbeginn liegt
in der Insel, zu der er gehört, der Einsatz auf ±0,4 s genau — die Vorlaufkante ist die
Polsterung der Stille-Erkennung. **Ohne** sie lagen fünf von zehn Einsätzen 20 bis 30
Sekunden zu früh, weil Whisper den Text auf das nächste 30-Sekunden-Fenster legt; der
erste Satz der Spur stand bei 0 s statt bei 20 s. Die Stille-Erkennung verschiebt die
Achse also nicht, sie richtet sie gerade.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Ohne Karte. Bleibt die Vorgabe, damit eine Instanz ohne CUDA genau wie bisher läuft.
DEVICE = "cpu"

COMPUTE_TYPE = "int8"

CPU_MODEL = "small"

# Mit Karte. ``large-v3-turbo`` statt ``large-v3``: fast dieselbe Güte bei deutlich
# weniger VRAM — und die 16 GB teilt sich die Transkription mit Ollama.
CUDA_DEVICE = "cuda"

CUDA_COMPUTE_TYPE = "float16"

CUDA_MODEL = "large-v3-turbo"


def cuda_verfuegbar() -> bool:
    """Ob ctranslate2 — der Unterbau von faster-whisper — eine Karte sieht.

    Ohne das Paket ist die Antwort ``False`` statt ein Fehler: eine Dev-Installation
    ohne das Extra ``transcribe`` soll die Erkennung trotzdem beantworten können.
    """
    try:
        import ctranslate2
    except ImportError:
        return False
    try:
        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:  # pragma: no cover - Treiber da, aber unbrauchbar
        return False


def geraet_und_rechenart(gewuenscht: str | None, *, cuda: bool) -> tuple[str, str]:
    """Gerät und Rechenart: der Wunsch schlägt den Fund, der Fund schlägt die Vorgabe.

    Die Rechenart ist kein eigener Regler, sondern folgt dem Gerät — ``float16`` auf der
    Karte, ``int8`` auf der CPU. Wer die Karte für Ollama frei braucht, setzt das Gerät
    auf ``cpu`` und bekommt exakt das Verhalten von früher.
    """
    if gewuenscht == CUDA_DEVICE or (gewuenscht is None and cuda):
        return CUDA_DEVICE, CUDA_COMPUTE_TYPE
    return DEVICE, COMPUTE_TYPE


def vorgabemodell(geraet: str) -> str:
    """Das Modell, das zum Gerät passt — überschreibbar über die Konfiguration."""
    return CUDA_MODEL if geraet == CUDA_DEVICE else CPU_MODEL


NICHT_INSTALLIERT = (
    "faster-whisper ist nicht installiert — im Image ist es dabei, "
    "lokal nachrüsten mit: pip install '.[transcribe]'"
)


@dataclass(frozen=True)
class Segment:
    """Ein Stück Rede: Sekunden ab Spurbeginn, dazu der Text."""

    start: float
    end: float
    text: str


class TranscriberError(RuntimeError):
    """Alles, was den Lauf verhindert — ohne Modell gibt es keinen Text."""


class TranscriberNotInstalled(TranscriberError):
    pass


class SpeechModel(Protocol):
    """Die ganze Abhängigkeit der Transkription zum Spracherkenner."""

    @property
    def name(self) -> str: ...

    def transcribe(self, audio_path: Path, *, vocabulary: str) -> Iterator[Segment]: ...


def _whisper_model(model_size: str, *, device: str, compute_type: str):
    # Lokal importiert: das Paket liegt im Image, aber nicht in jeder Dev-Installation —
    # ohne es bleibt der Rest der Anwendung startbar.
    try:
        from faster_whisper import WhisperModel
    except ImportError as fehler:
        raise TranscriberNotInstalled(NICHT_INSTALLIERT) from fehler
    return WhisperModel(model_size, device=device, compute_type=compute_type)


class FasterWhisper:
    def __init__(
        self,
        model_size: str | None = None,
        *,
        device: str | None = None,
        cuda: bool | None = None,
        loader: Callable[..., object] = _whisper_model,
    ) -> None:
        gefunden = cuda_verfuegbar() if cuda is None else cuda
        geraet, rechenart = geraet_und_rechenart(device, cuda=gefunden)
        name = model_size or vorgabemodell(geraet)
        try:
            self._model = loader(name, device=geraet, compute_type=rechenart)
        except TranscriberNotInstalled:
            # Gar kein faster-whisper — daran ändert die CPU nichts.
            raise
        except Exception as fehler:
            if geraet == DEVICE:
                raise
            # Karte belegt, zu klein oder Treiber quer: die Nacht läuft langsam weiter,
            # statt abzubrechen. Ollama teilt sich dieselben 16 GB (#84).
            logger.warning("Karte nicht nutzbar (%s) — weiter auf der CPU", fehler)
            geraet, rechenart = DEVICE, COMPUTE_TYPE
            name = model_size or vorgabemodell(geraet)
            self._model = loader(name, device=geraet, compute_type=rechenart)
        self._name = name
        self._device = geraet
        self._compute_type = rechenart

    @property
    def name(self) -> str:
        return self._name

    @property
    def device(self) -> str:
        return self._device

    @property
    def compute_type(self) -> str:
        return self._compute_type

    def transcribe(self, audio_path: Path, *, vocabulary: str = "") -> Iterator[Segment]:
        logger.info("Spracherkenner %s auf %s, %s", self._name, self._device, self._compute_type)
        segmente, _ = self._model.transcribe(
            str(audio_path), initial_prompt=vocabulary or None, vad_filter=True
        )
        for teil in segmente:
            yield Segment(start=float(teil.start), end=float(teil.end), text=str(teil.text))
