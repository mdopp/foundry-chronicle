"""Der Zugang zum Sprachmodell: ein HTTP-Aufruf gegen den Modelldienst, mehr nicht.

Wir bauen kein Modell nach und halten keines im Prozess — die Zielplattform stellt den
Dienst, wir reden mit ihm. Die Schnittstelle ist absichtlich schmal: ein Aufruf, ein
Text zurück. Was daran scheitert, ist kein Fehler des Laufs, sondern ein Rückfall auf
die geordnete Fassung; deshalb hat jeder Fehler einen Satz, den man anzeigen kann.

**Zwei Dienste sprechen hier, nicht einer** (#316). Ollama antwortet auf sein eigenes
``/api/chat``; llama.cpps ``llama-server``, der es auf der Box ablösen soll, kennt diesen
Pfad nicht — er antwortet mit 404 und spricht stattdessen ``/v1/chat/completions``.
Beide Wege stehen deshalb nebeneinander und werden über ``CHRONICLE_LLM_BACKEND``
gewählt; die Vorgabe bleibt Ollama, denn das ist, was die Box **heute** fährt. Was den
beiden gemeinsam ist — Adresse, Modellname, Zeitgrenze, und dass jeder Fehler als
``ModelUnreachable`` endet —, steht in ``_ChatClient``; verschieden sind nur die Nutzlast
und die Stelle, an der der Text in der Antwort liegt.

**Wer geantwortet hat, sagt die Antwort — nie die Einstellung** (#320). ``llama-server``
ignoriert den Modellnamen der Anfrage und antwortet mit dem Modell, das gerade geladen
ist; die Einstellung wäre dort eine Behauptung über einen fremden Prozess. Beide Dienste
tragen den Namen des antwortenden Modells oben in der Antwort, und genau der — und nur
der — wird weitergereicht.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol

import requests

from chronicle.config import (
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    DEFAULT_OLLAMA_URL,
    DEFAULT_SOLARIS_URL,
    Config,
)

logger = logging.getLogger(__name__)

CHAT_PATH = "/api/chat"

# Der Weg des Ablösers (#316). ``llama-server`` spricht die OpenAI-Form; ein ``keep_alive``
# gibt es dort **nicht** und wird auch nicht erfunden — es ist ein Ollama-Feld, und ein
# ausgedachtes Gegenstück wäre eine Zusage, die niemand einlöst.
OPENAI_CHAT_PATH = "/v1/chat/completions"

# **Das Denken kostet hier alles und bringt nichts** (#323, gemessen am 2026-09-06 auf
# dieser Box gegen die Notiz von Sitzung 4, identischer Prompt, 2619 Prompt-Tokens).
# ``llama-server`` legt die Gedankenkette in ein eigenes Feld ``reasoning_content``, das
# wir nie lesen — sie wird trotzdem Token für Token erzeugt und bezahlt:
#
#   gemma-4-e4b   mit Denken   6,5-11,0 s   520-833 Tokens, davon ~90 % unsichtbar
#   gemma-4-e4b   ohne Denken   1,8-2,2 s    77-114 Tokens
#   gemma-4-12b   mit Denken  32,2-47,3 s  1181-1722 Tokens, davon ~93 % unsichtbar
#   gemma-4-12b   ohne Denken       6,6 s       121 Tokens
#
# Der Inhalt wurde dabei nicht schlechter, sondern in beiden Modellen gleichwertig — und
# in keinem der Läufe stand ein Name im Text, der nicht in der Notiz steht. Das ist der
# Unterschied zwischen »der Zwischenstand kommt nach einer Dreiviertelminute« und »er ist
# da, bevor die nächste Szene läuft« (#294/#296).
#
# **Nur für die OpenAI-Form.** Ollamas eigenes Gegenstück heißt ``think`` und ist ein
# anderes Feld; erfunden wird es hier nicht.
OHNE_DENKEN = {"enable_thinking": False}

TAGS_PATH = "/api/tags"

# Das Feld, in dem beide Dienste den Namen des Modells nennen, das **geantwortet** hat
# (#320). Bei Ollama ist das der angefragte Name; bei ``llama-server`` der Name des
# geladenen Modells — heute der GGUF-Pfad, gemessen am 2026-09-05 auf dieser Box
# (``/models/…gguf``), nach ``mdopp/solarisbay#1333`` der Alias des Profils. Hässlich,
# aber wahr, und wahr schlägt hier schön: der Herkunftsvermerk der Chronik wird Wochen
# später als Gedächtnisstütze gelesen.
ANTWORT_MODELL = "model"

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

# Das Profil, das der Nachbar für uns lädt (Vertrag zu ``mdopp/solarisbay#1333``, im
# Kommentar an #321). Ein **Profil**, kein Modellname: ``llama-server`` ignoriert den
# Namen der Anfrage, und der Nachbar schaltet daran, welches Modell er geladen hält. Die
# Zusage aus #299 bleibt damit wörtlich erfüllt — keine Runden-, Gilden- oder
# Sitzungskennung; »foundry« sagt, *wessen Arbeit* ansteht, nicht *wer* spielt.
LEASE_PROFIL = "foundry"

LEASE_STEHT = 200

# Kein Fehlschlag: der Nachbar lädt oder schaltet um, beim ersten Mal minutenlang (ein
# 12b will erst heruntergeladen werden). Danach wird **nicht** erneut angemeldet, sondern
# gefragt — und die Frist beginnt erst, wenn er ``ready`` sagt.
LEASE_VORBEREITUNG = 202

LEASE_BEREIT = "ready"

LEASE_ZUSTAND_FELD = "state"

LEASE_WARTEZEIT_FELD = "retry_after"

# Wie lange zwischen zwei Fragen gewartet wird, wenn der Nachbar selbst nichts nennt.
LEASE_WARTEZEIT_S = 30.0

# Und die Untergrenze dazu, auch wenn er etwas nennt: ein Takt nahe null wäre die enge
# Schleife gegen einen Dienst, der ohnehin gerade lädt — die Lektion vom 2026-08-10.
LEASE_MINDESTPAUSE_S = 5.0

# Wie lange insgesamt auf ``ready`` gewartet wird. Eine Grenze braucht es, weil sonst ein
# Nachbar, der ewig »preparing« sagt, diesen Faden für immer bindet; großzügig darf sie
# sein, weil niemand darauf wartet — die Sitzung läuft daneben weiter und schreibt zur Not
# mit dem Haushaltsmodell, was der Herkunftsvermerk dann auch sagt (#320).
LEASE_VORBEREITUNG_S = 1800.0

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
    def name(self) -> str | None:
        """Das Modell, das **geantwortet** hat — ``None``, solange keines etwas sagte.

        Nicht das eingestellte: die Einstellung ist eine Bitte, kein Beleg (#320). Wer den
        Namen in einen Text schreibt, liest ihn deshalb **nach** dem Schreiben, und wo
        ``None`` steht, entfällt er — eine Chronik ohne Herkunftsangabe ist ehrlich, eine
        mit falscher nicht.
        """
        ...

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


class _ChatClient:
    """Was beide Modelldienste teilen: ein POST hin, ein Text zurück, sonst ein Satz.

    Der Pfad und die Nutzlast gehören der Unterklasse — alles andere ist für Ollama und
    ``llama-server`` dasselbe, und die Fehlerbehandlung zweimal zu schreiben hieße, sie
    beim nächsten Mal einmal zu ändern.
    """

    PFAD = CHAT_PATH

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
        self._geantwortet: str | None = None

    @property
    def name(self) -> str | None:
        return self._geantwortet

    def write(self, *, system: str, prompt: str) -> str:
        logger.info("Sprachmodell %s auf %s%s", self._model, self._base, self.PFAD)
        try:
            antwort = self._http.post(
                self._base + self.PFAD,
                json=self._rumpf(system=system, prompt=prompt),
                timeout=self._timeout,
            )
            antwort.raise_for_status()
        except requests.RequestException as fehler:
            raise ModelUnreachable(
                f"{self._base}{self.PFAD} nicht erreichbar: {type(fehler).__name__}"
            ) from None
        return self._content(antwort)

    def _nachrichten(self, *, system: str, prompt: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

    def _rumpf(self, *, system: str, prompt: str) -> dict[str, object]:
        raise NotImplementedError

    def _content(self, antwort) -> str:
        try:
            rumpf = antwort.json()
        except ValueError:
            raise ModelUnreachable(f"{self._base}{self.PFAD} hat kein JSON geliefert") from None
        if isinstance(rumpf, Mapping):
            # Ohne Gedächtnis über die Antwort hinaus: was die letzte Antwort nicht nennt,
            # nennt der Kopf nicht. Ein gemerkter Name aus einer früheren Antwort wäre
            # wieder eine Behauptung, nur eine ältere.
            self._geantwortet = _antwortname(rumpf)
        inhalt = self._text(rumpf) if isinstance(rumpf, Mapping) else None
        if not isinstance(inhalt, str) or not inhalt.strip():
            raise ModelUnreachable(f"Das Modell {self._model} hat nichts geschrieben")
        return inhalt.strip()

    def _text(self, rumpf: Mapping) -> object:
        raise NotImplementedError


def _antwortname(rumpf: Mapping) -> str | None:
    """Der Name aus der Antwort — oder ``None``, und dann bleibt der Kopf ohne Namen."""
    wert = rumpf.get(ANTWORT_MODELL)
    return wert.strip() if isinstance(wert, str) and wert.strip() else None


class OllamaClient(_ChatClient):
    PFAD = CHAT_PATH

    def _rumpf(self, *, system: str, prompt: str) -> dict[str, object]:
        # ``keep_alive`` reist an **jedem** Aufruf mit und hängt an keiner Bedingung: Ollama
        # liest es je Anfrage, und wo das Feld fehlt, gilt die Vorgabe des Dienstes — auf
        # dieser Box vierundzwanzig Stunden. Ein Pfad ohne das Feld wäre also kein
        # »neutraler« Aufruf, sondern der längste von allen (#303).
        return {
            "model": self._model,
            "stream": False,
            "messages": self._nachrichten(system=system, prompt=prompt),
            "keep_alive": haltung(),
        }

    def _text(self, rumpf: Mapping) -> object:
        block = rumpf.get("message")
        return block.get("content") if isinstance(block, Mapping) else None


class OpenAIClient(_ChatClient):
    """``llama-server`` in der OpenAI-Form (#316) — derselbe Auftrag, andere Nutzlast.

    **Kein ``keep_alive``.** Das Feld ist Ollamas, der Ablöser hat keines, und wir denken
    ihm keines aus: was hier stünde, wäre eine Zusage über die Karte, die niemand einlöst.
    Das **Sitzungsfenster** beim Nachbarn gilt hier trotzdem (#321): dass dieser Dienst
    selbst nichts hält, ist genau der Grund, warum es jemand für ihn tun muss.

    Und ``model`` ist hier eine Bitte, kein Beleg: der Server antwortet mit dem Modell, das
    er geladen hat, und **welches das war, steht in der Antwort** (#320).

    **Und das Denken schalten wir ab** (#323). Die Fassung davor stand hier mit dem Satz,
    das sei ein Startparameter des Dienstes und kein Feld dieser Anfrage — das war geraten
    und gemessen falsch: ``llama-server`` reicht ``chat_template_kwargs`` an die Vorlage
    durch, und ``enable_thinking: false`` greift je Aufruf.
    """

    PFAD = OPENAI_CHAT_PATH

    def _rumpf(self, *, system: str, prompt: str) -> dict[str, object]:
        return {
            "model": self._model,
            "stream": False,
            "messages": self._nachrichten(system=system, prompt=prompt),
            "chat_template_kwargs": OHNE_DENKEN,
        }

    def _text(self, rumpf: Mapping) -> object:
        wahlen = rumpf.get("choices")
        erste = wahlen[0] if isinstance(wahlen, list) and wahlen else None
        block = erste.get("message") if isinstance(erste, Mapping) else None
        return block.get("content") if isinstance(block, Mapping) else None


def from_config(config: Config, *, timeout: float = DEFAULT_TIMEOUT) -> TextModel | None:
    """Der Klient zur Konfiguration — mit der Zeitgrenze des Weges, der ihn baut.

    Die Vorgabe gehört dem Aufschrieb: er darf lange rechnen. Der Zwischenstand reicht
    ``ZWISCHENSTAND_TIMEOUT`` herein, weil für ihn das Gegenteil gilt (#302).

    **Dies ist die einzige Naht zwischen den beiden Backends** (#316). Die Aufrufer sehen
    nur ``TextModel``; welcher Dienst antwortet, entscheidet sich hier und sonst nirgends.
    """
    if not config.ollama_configured:
        return None
    bauart = OpenAIClient if config.llm_backend == BACKEND_OPENAI else OllamaClient
    return bauart(config, timeout=timeout)


def fenster_oeffnen(
    config: Config,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = LEASE_TIMEOUT,
    warten: Callable[[float], None] = time.sleep,
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

    **Auch auf dem ``/v1``-Weg** (#321). Bis #316 geschah hier nichts, weil der Vertrag
    mit dem Nachbarn für diesen Weg noch nicht verabredet war; jetzt ist er es. Der
    Leerlauf hieße heute nicht mehr »wir warten auf eine Form«, sondern »wir bekommen
    still das Haushaltsmodell«. Ollama-eigen bleibt allein die *Haltung* — ein
    ``keep_alive`` hat der Ablöser nicht (``_haltung_gilt``).
    """
    if not config.gpu_lease or not config.ollama_configured:
        return False
    basis = DEFAULT_SOLARIS_URL.rstrip("/")
    try:
        antwort = http().post(
            basis + LEASE_PATH,
            json={"model": _lease_modell(config), "ttl_s": LEASE_TTL_S},
            timeout=timeout,
        )
    except requests.RequestException as fehler:
        logger.warning(
            "Das Sitzungsfenster kam beim Nachbarn nicht an (%s) — der Abend läuft trotzdem.",
            type(fehler).__name__,
        )
        return False
    if antwort.status_code == LEASE_VORBEREITUNG:
        if not _bereit_abwarten(antwort, basis, http=http, timeout=timeout, warten=warten):
            return False
    elif antwort.status_code != LEASE_STEHT:
        logger.warning(
            "Der Nachbar gibt die Karte nicht her (%s, %s) — der Abend läuft ohne Fenster.",
            antwort.status_code,
            _abgelehnt(antwort),
        )
        return False
    global _lease_bis, _lease_erneuerung_s
    _lease_bis = time.monotonic() + LEASE_TTL_S
    _lease_erneuerung_s = _genannter_takt(antwort)
    if _haltung_gilt(config):
        _anweisen(config, LEASE_HALTUNG, http=http, timeout=HALTUNG_TIMEOUT)
    return True


def _lease_modell(config: Config) -> str:
    """Was der Nachbar laden soll — ein Profil auf dem neuen Weg, ein Modellname auf dem alten.

    Kein Schalter daneben, sondern dieselbe Entscheidung wie beim Klienten: der ``/v1``-Weg
    ist der neue Vertrag (``mdopp/solarisbay#1333``, Profile), der Ollama-Weg der alte, und
    dort **wirkt** die Anmeldung bis zu dessen Merge weiterhin auf Ollama. Ein Profilname
    an das alte Ende geschickt wäre dort ein Modell, das keiner kennt — also bleibt am alten
    Weg der Modellname stehen, bis auch er stillgelegt wird.
    """
    return LEASE_PROFIL if config.llm_backend == BACKEND_OPENAI else str(config.ollama_model)


def _json(antwort) -> Mapping:
    """Der Rumpf einer Antwort, oder ein leerer — kein Aufruf hängt an seinem Inhalt."""
    try:
        rumpf = antwort.json()
    except ValueError:
        return {}
    return rumpf if isinstance(rumpf, Mapping) else {}


def _abgelehnt(antwort) -> str:
    """Warum der Nachbar ablehnt, für die eine Logzeile — mehr wird daraus nicht."""
    rumpf = _json(antwort)
    return str(rumpf.get("reason") or rumpf.get("holder") or "ohne Angabe")


def _wartezeit(antwort) -> float:
    """Wie lange bis zur nächsten Frage: was der Nachbar nennt, in vernünftigen Schranken."""
    wert = _json(antwort).get(LEASE_WARTEZEIT_FELD)
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return LEASE_WARTEZEIT_S
    return min(max(float(wert), LEASE_MINDESTPAUSE_S), LEASE_VORBEREITUNG_S)


def _bereit_abwarten(
    antwort,
    basis: str,
    *,
    http: Callable[[], object],
    timeout: float,
    warten: Callable[[float], None],
) -> bool:
    """Auf ``ready`` warten, ohne erneut anzumelden und ohne den Abend anzuhalten (#321).

    Ein zweites ``POST`` verbietet der Vertrag ausdrücklich — es setzte die Vorbereitung
    neu auf. Gefragt wird deshalb mit ``GET``, frühestens nach der genannten Wartezeit und
    nur, solange das Budget reicht. Jeder Ausgang außer ``ready`` ist derselbe wie eine
    gescheiterte Anmeldung: kein Fenster, kein Fehler, der Abend läuft weiter.
    """
    ende = time.monotonic() + LEASE_VORBEREITUNG_S
    pause = _wartezeit(antwort)
    while time.monotonic() + pause <= ende:
        warten(pause)
        try:
            stand = http().get(basis + LEASE_PATH, timeout=timeout)
            stand.raise_for_status()
        except requests.RequestException as fehler:
            logger.warning(
                "Der Nachbar sagt nichts zum vorbereiteten Fenster (%s) — der Abend läuft "
                "ohne es weiter.",
                type(fehler).__name__,
            )
            return False
        if _json(stand).get(LEASE_ZUSTAND_FELD) == LEASE_BEREIT:
            return True
        pause = _wartezeit(stand)
    logger.warning("Das Fenster wurde nicht rechtzeitig fertig — der Abend läuft ohne es weiter.")
    return False


def _genannter_takt(antwort) -> float:
    """Der Erneuerungstakt aus der Antwort des Nachbarn — oder die eigene Ableitung (#306).

    Bester Wille auch hier: eine Antwort ohne JSON, ohne das Feld oder mit einer Zahl, die
    außerhalb der angemeldeten Frist liegt, kostet kein Fenster. Sie kostet nur den
    genannten Takt, und den ersetzt die Ableitung, die vor #306 die einzige Quelle war.
    """
    wert = _json(antwort).get(LEASE_ERNEUERUNG_FELD)
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

    **Auf dem ``/v1``-Weg wird nur abgemeldet** (#321): eine Haltung, die zu beenden wäre,
    hat der Ablöser nicht — ein Fenster beim Nachbarn dagegen schon.
    """
    entladen = False
    if _haltung_gilt(config):
        entladen = _anweisen(config, FREIGABE, http=http, timeout=timeout)
    return _fenster_schliessen(config, http=http) or entladen


def _haltung_gilt(config: Config) -> bool:
    """Ob der gewählte Weg eine Modellhaltung kennt, die wir anweisen könnten (#316).

    Nur Ollama kennt eine; ``llama-server`` nimmt kein ``keep_alive`` entgegen, und ein
    ausgedachtes Gegenstück wäre eine Zusage, die niemand einlöst. Das **Fenster** beim
    Nachbarn hängt daran nicht mehr: dafür gibt es seit #321 einen Vertrag, der für beide
    Wege gilt.

    Ein **ausgesprochener** No-op und kein stilles Nichts: wer im Log nach der Haltung
    sucht, soll dort lesen, warum keine gesetzt wird, statt es für einen Fehler zu halten.
    """
    if config.llm_backend == BACKEND_OLLAMA:
        return True
    logger.info(
        "Backend %s: keine Modellhaltung — der Ablöser hält nichts, was wir anweisen "
        "könnten. Das Sitzungsfenster beim Nachbarn gilt davon unberührt.",
        config.llm_backend,
    )
    return False


def _fenster_schliessen(config: Config, *, http: Callable[[], object]) -> bool:
    """Das angemeldete Fenster abmelden — ohne offenes Fenster geschieht nichts.

    Der Vermerk fällt zuerst: ob der Nachbar es erfährt, ändert nichts daran, dass unsere
    eigenen Aufrufe ab jetzt wieder die knappe Frist tragen.
    """
    global _lease_bis
    if not lease_offen():
        return False
    _lease_bis = 0.0
    try:
        antwort = http().delete(DEFAULT_SOLARIS_URL.rstrip("/") + LEASE_PATH, timeout=LEASE_TIMEOUT)
        antwort.raise_for_status()
    except requests.RequestException as fehler:
        logger.warning(
            "Das Sitzungsfenster ließ sich nicht abmelden (%s) — es verfällt von selbst.",
            type(fehler).__name__,
        )
        return False
    return True


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
