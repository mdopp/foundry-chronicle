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

# Wie lange Ollama das Modell nach einem Aufruf im Speicher behält, solange eine Sitzung
# läuft. Auf der Box passen das große Chronik-Modell und die Nachbarn dieser Karte nicht
# nebeneinander (#295), also ist jede Verdrängung ein Tausch hin und zurück — die Haltung
# macht ihn seltener. Ollama kennt für »für immer« einen negativen Wert; den nehmen wir
# bewusst **nicht**: eine Sitzung, die nicht ordentlich endet — Absturz, Neustart, der
# leere Sprachkanal —, hielte damit acht Gigabyte bis zum nächsten Neustart fest und
# sperrte die Nachbardienste aus. Endlich und selbst erneuernd ist beides zugleich: jeder
# Aufruf setzt die Frist neu, und läuft nichts mehr, läuft sie von selbst ab.
SITZUNGSHALTUNG = "2h"

# Sofort entladen — das ausdrückliche Ende der Haltung.
FREIGABE = 0

# Das Laden eines großen Modells dauert; wer die Haltung setzt, wartet nicht darauf.
HALTUNG_TIMEOUT = 300.0

KEIN_MODELL = "Noch kein Modell gewählt — ein Modell hinterlegt der Betreiber dieser Box."

# Die Haltung gehört der Grafikkarte dieser Box und keiner Runde: es gibt eine, und wer
# sie hält, hält sie für alle. Deshalb steht sie im Prozess und in keiner Zeile der
# Datenbank — ein Neustart soll sie ausdrücklich **nicht** überleben, denn nach ihm weiß
# niemand mehr, ob der Abend noch läuft.
_haltung: str | int | None = None


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
        # Die Haltung reist am Aufruf mit und wird nicht einmalig gesetzt: Ollama liest
        # ``keep_alive`` je Anfrage, ein Aufruf ohne das Feld setzte die Frist auf die
        # Vorgabe von fünf Minuten zurück — und nähme der laufenden Sitzung genau die
        # Haltung wieder weg, die ihr Start gesetzt hat.
        rumpf: dict[str, object] = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if _haltung is not None:
            rumpf["keep_alive"] = _haltung
        try:
            antwort = self._http.post(
                self._base + CHAT_PATH,
                json=rumpf,
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


def haltung() -> str | int | None:
    """Was der nächste Aufruf mitschickt — ``None`` heißt: Ollamas eigene Vorgabe."""
    return _haltung


def halten(
    config: Config,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = HALTUNG_TIMEOUT,
) -> bool:
    """Das Modell für die Dauer einer Sitzung im Speicher festhalten."""
    global _haltung
    _haltung = SITZUNGSHALTUNG
    return _anweisen(config, SITZUNGSHALTUNG, http=http, timeout=timeout)


def freigeben(
    config: Config,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = HALTUNG_TIMEOUT,
) -> bool:
    """Die Haltung ausdrücklich beenden und das Modell sofort entladen.

    Ohne Rücksicht darauf, ob dieser Prozess sie gesetzt hat: nach einem Neustart mitten
    im Abend weiß er nichts mehr davon, und Ollama hielte trotzdem noch. Freigeben ist
    dann genau richtig, und ohne geladenes Modell kostet es nichts.
    """
    global _haltung
    _haltung = None
    return _anweisen(config, FREIGABE, http=http, timeout=timeout)


def _anweisen(
    config: Config,
    wert: str | int,
    *,
    http: Callable[[], object],
    timeout: float,
) -> bool:
    """Ollama sagen, wie lange es das Modell halten soll — ohne es schreiben zu lassen.

    Ein Aufruf ohne Nachrichten lädt oder entlädt nur. Scheitert er, bleibt es dabei: eine
    Sitzung darf weder daran hängen zu beginnen noch daran, abzuschließen.
    """
    if not config.ollama_configured:
        return False
    basis = (config.ollama_url or DEFAULT_OLLAMA_URL).rstrip("/")
    try:
        antwort = http().post(
            basis + CHAT_PATH,
            json={
                "model": str(config.ollama_model),
                "stream": False,
                "messages": [],
                "keep_alive": wert,
            },
            timeout=timeout,
        )
        antwort.raise_for_status()
    except requests.RequestException as fehler:
        logger.warning(
            "Die Haltung des Modells kam nicht durch (%s) — der Abend läuft trotzdem.",
            type(fehler).__name__,
        )
        return False
    return True


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
