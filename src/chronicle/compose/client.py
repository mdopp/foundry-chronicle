"""Der Zugang zum Sprachmodell: ein HTTP-Aufruf gegen Ollama, mehr nicht.

Wir bauen kein Modell nach und halten keines im Prozess — die Zielplattform stellt
Ollama, wir reden mit ihm. Die Schnittstelle ist absichtlich schmal: ein Aufruf, ein
Text zurück. Was daran scheitert, ist kein Fehler des Laufs, sondern ein Rückfall auf
die geordnete Fassung; deshalb hat jeder Fehler einen Satz, den man anzeigen kann.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol

import requests

from chronicle.config import DEFAULT_OLLAMA_URL, DEFAULT_SOLARIS_URL, Config

logger = logging.getLogger(__name__)

CHAT_PATH = "/api/chat"

TAGS_PATH = "/api/tags"

# Stapelbetrieb: ein Modell darf für eine Szene Minuten brauchen. Zehn waren zu wenig —
# am 22.08. brach ein Aufschrieb nach exakt dieser Grenze ab, während der Nachbardienst
# für denselben Text ~31 Minuten reine Rechenzeit maß (#301). Nach oben gibt es hier
# nichts zu gewinnen und nichts zu verlieren: nach der Aufnahme läuft alles im Stapel,
# es gibt keine Latenzgrenze. Eine Grenze braucht es trotzdem — ohne sie hinge ein Lauf
# an einem verstummten Ollama, bis jemand ihn bemerkt.
DEFAULT_TIMEOUT = 1800.0

# Die Einstellungsseite fragt damit im Request-Pfad: lieber ein Textfeld als eine Seite,
# die auf ein abgeschaltetes Ollama wartet.
TAGS_TIMEOUT = 2.0

EMBEDDING_MARKER = "embed"

# Der Zwischenstand je Szene ist der eine Weg mit ausdrücklicher Richtung: ein bis drei
# Minuten nach dem Szenenschnitt, sonst verliert er seinen Zweck (#294/#296). Mit der
# großzügigen Grenze des Aufschriebs besetzte ein hängendes Modell seinen Job-Platz eine
# halbe Stunde und schluckte damit jeden weiteren Schnitt des Abends (#302). Reißt sie,
# fällt der Zwischenstand still aus — derselbe Fall wie ein Ollama, das nicht antwortet,
# und kein neuer.
ZWISCHENSTAND_TIMEOUT = 240.0

# Wie lange Ollama das Modell nach einem Aufruf im Speicher behält. Dass hier überhaupt
# eine Zahl steht, ist der Punkt: auf dieser Box setzt der Ollama-Dienst
# ``OLLAMA_KEEP_ALIVE=24h``, und ein Aufruf **ohne** das Feld erbt diese vierundzwanzig
# Stunden (#303). Gehalten wird nichts mehr — die Messung des Nachbardienstes vom
# 2026-08-26 hat entschieden, dass unser großes Modell und seines auf dieser Karte nicht
# nebeneinander passen, und damit greift die verabredete Rückfallebene: wir halten nicht
# und nehmen den Tausch je Szenenschnitt in Kauf. Knapp, aber nicht null: innerhalb eines
# Aufschriebs folgen Chronik, Rückblick und Nacherzählung unmittelbar aufeinander, und das
# Modell zwischen ihnen zu entladen kostete jedes Mal den Ladevorgang neu.
KNAPPE_HALTUNG = "5m"

# **Die eine Konstante des Vertrags mit ``solaris`` (#299).** Solange ein Sitzungsfenster
# angemeldet ist, antwortet der Nachbar mit *unserem* großen Modell, statt bei jeder
# Haushaltsanfrage seines zurückzuholen — ein Tausch, der auf dieser Karte ~56 s kostet,
# in beide Richtungen. Damit das zusammenpasst, muss die Frist, die wir dem Nachbarn
# nennen, dieselbe sein wie die, die Ollama von uns hört: eine Zahl, zwei Verwendungen.
LEASE_TTL_S = 900

# Die zweite Frist, die unsere Aufrufe tragen können — die benannte Ausnahme zur knappen
# Vorgabe darüber und **keine** Rücknahme von #303. Knapp bleibt die Norm; lang gilt nur,
# solange der Nachbar zugesagt hat, das Modell nicht wegzuziehen.
LEASE_HALTUNG = f"{LEASE_TTL_S}s"

# Erneuert wird dreimal je Frist — die **Rückfallebene**, falls der Nachbar keinen Takt
# nennt (#306). Abgeleitet und nicht danebengeschrieben: zwei Zahlen liefen auseinander,
# sobald jemand eine von beiden anfasst, und das Fenster fiele mitten im Abend zu.
LEASE_ERNEUERUNG_S = LEASE_TTL_S / 3

# Der Nachbar nennt den Takt in seiner Antwort; genau deshalb steht er dort. Aus der
# eigenen Frist abgeleitet stimmte er nur, solange beide Seiten dieselbe Zahl halten —
# heute zufällig 900 —, und liefe stumm auseinander, sobald einer sie anfasst (#306).
LEASE_ERNEUERUNG_FELD = "renew_after"

# **Nicht** ``/napi/*``: dieses Präfix ist beim Nachbarn Authelia-umgangen und deshalb
# token-pflichtig und fail-closed. Ein token-freier Endpunkt darin hieße »alles hier
# braucht ein Token, außer dem einen« — und zwar vor dem Präfix, über das seine App echte
# Geräte schaltet. Er hat seinen eigenen Vorschlag darum zurückgezogen; wir folgen (#306).
# ``/api/model-lease`` ist stattdessen peer-gebunden auf die Schleife und weist
# proxy-weitergeleitete Aufrufe ab: dieselbe Zusage »nur von der Box«, ohne ein fremdes
# Sicherheitsversprechen aufzuweichen. Der Feldname ``ttl_s`` blieb dagegen unserer.
LEASE_PATH = "/api/model-lease"

# Der Nachbar steht auf derselben Box, an derselben Schleife. Wartet er länger, hat der
# Abend schon begonnen — eine Anmeldung, die sich Zeit lässt, hält niemanden auf.
LEASE_TIMEOUT = 5.0

# Wann das angemeldete Fenster von selbst ausläuft, gemessen an der monotonen Uhr dieses
# Prozesses. Kein Wert in der Datenbank: ein Neustart erbt das Fenster ausdrücklich nicht,
# und der Nachbar lässt es ohnehin nach ``LEASE_TTL_S`` verfallen. Läuft es hier ab, ohne
# dass jemand erneuert, fallen unsere Aufrufe von selbst auf die knappe Frist zurück.
_lease_bis = 0.0

# Der Takt, in dem das offene Fenster erneuert wird — gesagt vom Nachbarn, sonst abgeleitet.
_lease_erneuerung_s = LEASE_ERNEUERUNG_S

# Sofort entladen — das ausdrückliche Ende der Haltung.
FREIGABE = 0

# Das Entladen eines großen Modells dauert; wer es freigibt, wartet nicht ewig darauf.
HALTUNG_TIMEOUT = 300.0

KEIN_MODELL = "Noch kein Modell gewählt — ein Modell hinterlegt der Betreiber dieser Box."


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


def lease_offen() -> bool:
    """Ob gerade ein Sitzungsfenster beim Nachbarn angemeldet ist — und noch gilt."""
    return time.monotonic() < _lease_bis


def erneuerung() -> float:
    """Der Takt, in dem das offene Fenster erneuert wird — in Sekunden (#306)."""
    return _lease_erneuerung_s


def haltung() -> str:
    """Die Frist, die der nächste Aufruf mitbringt: knapp, oder die des offenen Fensters.

    Eine Konstante, zwei Werte (#299). Die knappe ist die Norm (#303); die lange gilt nur,
    solange der Nachbar zugesagt hat, das Modell stehen zu lassen. Läuft das Fenster ab,
    fällt der nächste Aufruf ohne Zutun auf die knappe zurück.
    """
    return LEASE_HALTUNG if lease_offen() else KNAPPE_HALTUNG


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
        # ``keep_alive`` reist an **jedem** Aufruf mit und hängt an keiner Bedingung: Ollama
        # liest es je Anfrage, und wo das Feld fehlt, gilt die Vorgabe des Dienstes — auf
        # dieser Box vierundzwanzig Stunden. Ein Pfad ohne das Feld wäre also kein
        # »neutraler« Aufruf, sondern der längste von allen (#303).
        rumpf: dict[str, object] = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "keep_alive": haltung(),
        }
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


def from_config(config: Config, *, timeout: float = DEFAULT_TIMEOUT) -> OllamaClient | None:
    """Der Klient zur Konfiguration — mit der Zeitgrenze des Weges, der ihn baut.

    Die Vorgabe gehört dem Aufschrieb: er darf lange rechnen. Der Zwischenstand reicht
    ``ZWISCHENSTAND_TIMEOUT`` herein, weil für ihn das Gegenteil gilt (#302).
    """
    return OllamaClient(config, timeout=timeout) if config.ollama_configured else None


def fenster_oeffnen(
    config: Config,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = LEASE_TIMEOUT,
) -> bool:
    """Das Sitzungsfenster beim Nachbarn anmelden — und das Modell gleich mit laden (#299).

    Die Nutzlast trägt **nur** Modellname und Frist. Wer spielt, gehört nicht dazu: der
    Nachbar entscheidet daran nichts, und eine Runden-, Gilden- oder Sitzungskennung wäre
    eine Preisgabe an einen Dienst, der ohne sie auskommt. Ein Geheimnis reist auch keines
    mit — der Aufruf geht über die Schleife dieser Box, und seit #230 hat diese Instanz
    keines mehr.

    **Der Aufwärm-Aufruf gehört dazu und ist die Antwort auf den offenen Punkt aus #299.**
    Mit dem Wegfall von ``halten`` (#303) zahlte der erste Szenenschnitt eines Abends den
    Ladevorgang — ~56 s, genau die Zahl, gegen die dieses Fenster gekauft wird. Die
    Zusage, die es einholt, ist ja gerade, dass der Nachbar sein Modell in dieser
    Viertelstunde nicht zurückholt; unmittelbar danach zu laden kostet ihn also nichts,
    was er nicht schon zugesagt hätte. Geladen wird erst **nach** der Zusage: scheitert
    die Anmeldung, bleibt es beim knappen Aufruf, und wir verdrängen ihn nicht ungefragt.

    Bester Wille, wie alles auf diesem Weg: scheitert es, beginnt der Abend trotzdem.
    """
    if not config.gpu_lease or not config.ollama_configured:
        return False
    basis = DEFAULT_SOLARIS_URL.rstrip("/")
    try:
        antwort = http().post(
            basis + LEASE_PATH,
            json={"model": str(config.ollama_model), "ttl_s": LEASE_TTL_S},
            timeout=timeout,
        )
        antwort.raise_for_status()
    except requests.RequestException as fehler:
        logger.warning(
            "Das Sitzungsfenster kam beim Nachbarn nicht an (%s) — der Abend läuft trotzdem.",
            type(fehler).__name__,
        )
        return False
    global _lease_bis, _lease_erneuerung_s
    _lease_bis = time.monotonic() + LEASE_TTL_S
    _lease_erneuerung_s = _genannter_takt(antwort)
    _anweisen(config, LEASE_HALTUNG, http=http, timeout=HALTUNG_TIMEOUT)
    return True


def _genannter_takt(antwort) -> float:
    """Der Erneuerungstakt aus der Antwort des Nachbarn — oder die eigene Ableitung (#306).

    Bester Wille auch hier: eine Antwort ohne JSON, ohne das Feld oder mit einer Zahl, die
    außerhalb der angemeldeten Frist liegt, kostet kein Fenster. Sie kostet nur den
    genannten Takt, und den ersetzt die Ableitung, die vor #306 die einzige Quelle war.
    """
    try:
        rumpf = antwort.json()
    except ValueError:
        return LEASE_ERNEUERUNG_S
    wert = rumpf.get(LEASE_ERNEUERUNG_FELD) if isinstance(rumpf, Mapping) else None
    # ``bool`` ist in Python ein ``int``; ein ``True`` als Takt wäre eine Sekunde.
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return LEASE_ERNEUERUNG_S
    # Ein Takt jenseits der Frist erneuert erst, wenn das Fenster längst zu ist.
    return float(wert) if 0 < wert <= LEASE_TTL_S else LEASE_ERNEUERUNG_S


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

    **Hier geht auch das Sitzungsfenster wieder zu** (#299). Nicht an einer zweiten
    Stelle: dies ist die eine, an der jeder Aufschrieb endet (``kette.schreiben``,
    ``finally``), und ein zweiter Schließer liefe irgendwann gegen den ersten. Erst
    entladen, dann abmelden — in dieser Reihenfolge ist die Karte frei, sobald der Nachbar
    davon erfährt.
    """
    ergebnis = _anweisen(config, FREIGABE, http=http, timeout=timeout)
    _fenster_schliessen(config, http=http)
    return ergebnis


def _fenster_schliessen(config: Config, *, http: Callable[[], object]) -> None:
    """Das angemeldete Fenster abmelden — ohne offenes Fenster geschieht nichts.

    Der Vermerk fällt zuerst: ob der Nachbar es erfährt, ändert nichts daran, dass unsere
    eigenen Aufrufe ab jetzt wieder die knappe Frist tragen.
    """
    global _lease_bis
    if not lease_offen():
        return
    _lease_bis = 0.0
    try:
        antwort = http().delete(DEFAULT_SOLARIS_URL.rstrip("/") + LEASE_PATH, timeout=LEASE_TIMEOUT)
        antwort.raise_for_status()
    except requests.RequestException as fehler:
        logger.warning(
            "Das Sitzungsfenster ließ sich nicht abmelden (%s) — es verfällt von selbst.",
            type(fehler).__name__,
        )


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
