"""Der Zugang zum Spracherkenner: faster-whisper auf der CPU, in int8.

Die Schnittstelle ist absichtlich schmal — eine Datei hinein, Segmente heraus. Alles,
was ein echtes Modell lädt, steckt in dieser Datei; die Tests setzen hier ein erfundenes
Modell ein und laden deshalb nie etwas herunter.

Übergeben wird der **Pfad der ganzen Spur**, nie ein kleiner Puffer. Whisper ist auf
30-Sekunden-Fenster trainiert und lebt vom Kontext; pro Redebeitrag geschnittene
Schnipsel verschlechtern vor allem die Erkennung von Eigennamen — und ein Rollenspiel
besteht aus erfundenen Eigennamen.

Kein GPU-Pfad: ``cpu``/``int8`` sind hier festverdrahtet, nicht konfigurierbar. Das
Zwei- bis Fünffache der Echtzeit ist im Stapel über Nacht kein Problem, eine
Grafikkarte im Anforderungsprofil dagegen schon.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

DEVICE = "cpu"

COMPUTE_TYPE = "int8"

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
        model_size: str,
        *,
        loader: Callable[..., object] = _whisper_model,
    ) -> None:
        self._name = model_size
        self._model = loader(model_size, device=DEVICE, compute_type=COMPUTE_TYPE)

    @property
    def name(self) -> str:
        return self._name

    def transcribe(self, audio_path: Path, *, vocabulary: str = "") -> Iterator[Segment]:
        logger.info("Spracherkenner %s auf %s, %s", self._name, DEVICE, COMPUTE_TYPE)
        segmente, _ = self._model.transcribe(str(audio_path), initial_prompt=vocabulary or None)
        for teil in segmente:
            yield Segment(start=float(teil.start), end=float(teil.end), text=str(teil.text))
