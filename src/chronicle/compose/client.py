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

from chronicle.config import DEFAULT_OLLAMA_URL, Config

logger = logging.getLogger(__name__)

CHAT_PATH = "/api/chat"

TAGS_PATH = "/api/tags"

# Stapelbetrieb: ein Modell auf CPU darf für eine Szene Minuten brauchen.
DEFAULT_TIMEOUT = 600.0

# Die Einstellungsseite fragt damit im Request-Pfad: lieber ein Textfeld als eine Seite,
# die auf ein abgeschaltetes Ollama wartet.
TAGS_TIMEOUT = 2.0

EMBEDDING_MARKER = "embed"

KEIN_MODELL = "Noch kein Modell gewählt — ein Modell wählst du in den Einstellungen."


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
            raise ModelNotConfigured(KEIN_MODELL)
        self._base = (config.ollama_url or DEFAULT_OLLAMA_URL).rstrip("/")
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


def installed_models(
    base_url: str,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = TAGS_TIMEOUT,
) -> tuple[str, ...]:
    """Was auf diesem Ollama liegt — für die Auswahl in der Oberfläche.

    Wirft ``ModelUnreachable``; der Aufrufer zeigt dann ein Textfeld statt einer Liste.
    """
    basis = base_url.rstrip("/")
    try:
        antwort = http().get(basis + TAGS_PATH, timeout=timeout)
        antwort.raise_for_status()
        rumpf = antwort.json()
    except (requests.RequestException, ValueError) as fehler:
        raise ModelUnreachable(
            f"{basis}{TAGS_PATH} nicht erreichbar: {type(fehler).__name__}"
        ) from None
    eintraege = rumpf.get("models") if isinstance(rumpf, Mapping) else None
    if not isinstance(eintraege, list):
        raise ModelUnreachable(f"{basis}{TAGS_PATH} hat keine Modellliste geliefert")
    namen = (
        str(eintrag.get("name"))
        for eintrag in eintraege
        if isinstance(eintrag, Mapping) and eintrag.get("name")
    )
    # Einbettungsmodelle schreiben keinen Text; sie zur Auswahl zu stellen wäre eine Falle.
    return tuple(sorted(name for name in namen if EMBEDDING_MARKER not in name.lower()))
