"""Der Zugang zum Sprachmodell: ein HTTP-Aufruf gegen Ollama, mehr nicht.

Wir bauen kein Modell nach und halten keines im Prozess — die Zielplattform stellt
Ollama, wir reden mit ihm. Die Schnittstelle ist absichtlich schmal: ein Aufruf, ein
Text zurück. Was daran scheitert, ist kein Fehler des Laufs, sondern ein Rückfall auf
die geordnete Fassung; deshalb hat jeder Fehler einen Satz, den man anzeigen kann.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Protocol

import requests

from chronicle.config import Config

logger = logging.getLogger(__name__)

CHAT_PATH = "/api/chat"

# Stapelbetrieb: ein Modell auf CPU darf für eine Szene Minuten brauchen.
DEFAULT_TIMEOUT = 600.0


class ModelError(RuntimeError):
    """Alles, was den Aufruf verhindert — die Komposition ordnet dann nur noch."""


class ModelNotConfigured(ModelError):
    pass


class ModelUnreachable(ModelError):
    pass


class TextModel(Protocol):
    """Die ganze Abhängigkeit der Komposition zum Sprachmodell."""

    @property
    def name(self) -> str: ...

    def write(self, *, system: str, prompt: str) -> str: ...


def _http_session() -> requests.Session:
    return requests.Session()


class OllamaClient:
    def __init__(
        self,
        config: Config,
        *,
        http: Callable[[], object] = _http_session,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not config.ollama_configured:
            fehlt = ", ".join(config.missing_ollama_variables)
            raise ModelNotConfigured(f"Kein Sprachmodell konfiguriert; es fehlt: {fehlt}")
        self._base = str(config.ollama_url).rstrip("/")
        self._model = str(config.ollama_model)
        self._http = http()
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._model

    def write(self, *, system: str, prompt: str) -> str:
        logger.info("Sprachmodell %s auf %s", self._model, self._base)
        try:
            antwort = self._http.post(
                self._base + CHAT_PATH,
                json={
                    "model": self._model,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self._timeout,
            )
            antwort.raise_for_status()
        except requests.RequestException as fehler:
            raise ModelUnreachable(
                f"{self._base}{CHAT_PATH} nicht erreichbar: {type(fehler).__name__}"
            ) from None
        return self._content(antwort)

    def _content(self, antwort) -> str:
        try:
            rumpf = antwort.json()
        except ValueError:
            raise ModelUnreachable(f"{self._base}{CHAT_PATH} hat kein JSON geliefert") from None
        block = rumpf.get("message") if isinstance(rumpf, Mapping) else None
        inhalt = block.get("content") if isinstance(block, Mapping) else None
        if not isinstance(inhalt, str) or not inhalt.strip():
            raise ModelUnreachable(f"Das Modell {self._model} hat nichts geschrieben")
        return inhalt.strip()


def from_config(config: Config) -> OllamaClient | None:
    return OllamaClient(config) if config.ollama_configured else None
