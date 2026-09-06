"""Der Zugang zum Sprachmodell: ein HTTP-Aufruf gegen den Modelldienst, mehr nicht.

Wir bauen kein Modell nach und halten keines im Prozess — die Zielplattform stellt den
Dienst, wir reden mit ihm. Die Schnittstelle ist absichtlich schmal: ein Aufruf, ein
Text zurück. Was daran scheitert, ist kein Fehler des Laufs, sondern ein Rückfall auf
die geordnete Fassung; deshalb hat jeder Fehler einen Satz, den man anzeigen kann.

**Es spricht genau ein Dienst: llama.cpps ``llama-server``** auf ``/v1/chat/completions``.
Bis #316 war es Ollama mit seinem eigenen ``/api/chat``, und beide Wege standen eine Weile
nebeneinander, über ``CHRONICLE_LLM_BACKEND`` gewählt — den Ablöser konnte man einstellen,
solange die Box noch den alten fuhr. Mit #329 hat sie ihn abgeschaltet
(``mdopp/solarisbay#1332``, gemessen am 2026-09-06: ``11434`` antwortet nicht mehr), und
damit ist der zweite Weg gefallen. Der Schalter ist mitgefallen: einer mit genau einem
gültigen Wert ist eine falsche Zusage, denn wer ``ollama`` hineinschriebe, bekäme still
den ``/v1``-Weg.

**Wer geantwortet hat, sagt die Antwort — nie die Einstellung** (#320). ``llama-server``
ignoriert den Modellnamen der Anfrage und antwortet mit dem Modell, das gerade geladen
ist; die Einstellung wäre dort eine Behauptung über einen fremden Prozess. Der Name des
antwortenden Modells steht oben in der Antwort, und genau der — und nur der — wird
weitergereicht.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol

import requests

from chronicle.config import (
    DEFAULT_OLLAMA_URL,
    DEFAULT_SOLARIS_URL,
    Config,
)

logger = logging.getLogger(__name__)

# Der einzige Weg (#316/#329). ``llama-server`` spricht die OpenAI-Form; ein ``keep_alive``
# gibt es dort **nicht** und wird auch nicht erfunden — es war ein Ollama-Feld, und ein
# ausgedachtes Gegenstück wäre eine Zusage, die niemand einlöst.
OPENAI_CHAT_PATH = "/v1/chat/completions"

# Das Feld, in dem der Dienst den Namen des Modells nennt, das **geantwortet** hat (#320)
# — bei ``llama-server`` der Name des geladenen Modells und nicht der angefragte; heute
# der GGUF-Pfad, gemessen am 2026-09-05 auf dieser Box
# (``/models/…gguf``), nach ``mdopp/solarisbay#1333`` der Alias des Profils. Hässlich,
# aber wahr, und wahr schlägt hier schön: der Herkunftsvermerk der Chronik wird Wochen
# später als Gedächtnisstütze gelesen.
ANTWORT_MODELL = "model"

# Stapelbetrieb: ein Modell darf für eine Szene Minuten brauchen. Zehn waren zu wenig —
# am 22.08. brach ein Aufschrieb nach exakt dieser Grenze ab, während der Nachbardienst
# für denselben Text ~31 Minuten reine Rechenzeit maß (#301). Nach oben gibt es hier
# nichts zu gewinnen und nichts zu verlieren: nach der Aufnahme läuft alles im Stapel,
# es gibt keine Latenzgrenze. Eine Grenze braucht es trotzdem — ohne sie hinge ein Lauf
# an einem verstummten Modelldienst, bis jemand ihn bemerkt.
DEFAULT_TIMEOUT = 1800.0

# Der Zwischenstand je Szene ist der eine Weg mit ausdrücklicher Richtung: ein bis drei
# Minuten nach dem Szenenschnitt, sonst verliert er seinen Zweck (#294/#296). Mit der
# großzügigen Grenze des Aufschriebs besetzte ein hängendes Modell seinen Job-Platz eine
# halbe Stunde und schluckte damit jeden weiteren Schnitt des Abends (#302). Reißt sie,
# fällt der Zwischenstand still aus — derselbe Fall wie ein Modelldienst, der nicht
# antwortet, und kein neuer.
#
# **Von 240 s auf 480 s gehoben** (#330, gemessen am 2026-09-06). Die vier Minuten stammen
# aus der Zeit vor #325, als das Modell noch ohne Denken antwortete. Gegen dieselbe Szene
# gemessen, seit das Denken die Erzählebene hält:
#
#   direkt gegen /v1, ohne Wettbewerb um die Karte   70,0 / 76,0 / 90,3 s
#   durch den deployten Dienst, mit Sitzungsfenster           157,2 s
#
# Der letzte Wert ist der ehrliche: er trägt, was ein echter Abend trägt. 157 s gegen 240 s
# ist kein Abstand, sondern ein Zufall — ein Abend mit längeren Szenen, und der
# Zwischenstand fällt **still** aus, was der teuerste Ausfall ist, weil ihn niemand
# bemerkt. Acht Minuten geben den dreifachen Abstand zum gemessenen Lauf und bleiben weit
# unter der Szenenlänge, um die es #302 ging: ein hängendes Modell blockiert dann acht
# Minuten und nicht eine halbe Stunde.
ZWISCHENSTAND_TIMEOUT = 480.0

# **Die Frist des Vertrags mit ``solaris`` (#299).** Solange ein Sitzungsfenster angemeldet
# ist, antwortet der Nachbar mit *unserem* großen Modell, statt bei jeder Haushaltsanfrage
# seines zurückzuholen — ein Tausch, der auf dieser Karte Sekunden kostet, in beide
# Richtungen.
#
# Bis #329 war das **eine Zahl mit zwei Verwendungen**: Ollama hielt ein Modell nur so
# lange, wie der Aufruf es mit ``keep_alive`` erbat, und wir nannten dem Nachbarn genau
# diese Frist, damit nicht zwei Zahlen auseinanderliefen. Der Ablöser kennt kein
# ``keep_alive`` und hält von sich aus; geblieben ist die eine Verwendung, die den Vertrag
# trägt.
LEASE_TTL_S = 900

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


class OpenAIClient:
    """``llama-server`` in der OpenAI-Form: ein POST hin, ein Text zurück, sonst ein Satz.

    Bis #329 stand darüber eine Basisklasse, die sich diesen Aufbau mit ``OllamaClient``
    teilte, und zwei Haken, an denen Nutzlast und Textstelle hingen. Der zweite Weg ist
    gefallen; eine Abstraktion für genau eine Unterklasse ist keine.

    **Kein ``keep_alive``.** Das Feld war Ollamas, der Ablöser hat keines, und wir denken
    ihm keines aus: was hier stünde, wäre eine Zusage über die Karte, die niemand einlöst.
    Das **Sitzungsfenster** beim Nachbarn gilt hier trotzdem (#321): dass dieser Dienst
    selbst nichts hält, ist genau der Grund, warum es jemand für ihn tun muss.

    Und ``model`` ist hier eine Bitte, kein Beleg: der Server antwortet mit dem Modell, das
    er geladen hat, und **welches das war, steht in der Antwort** (#320).

    **Und das Denken bleibt an** (#325). Die Fassung davor sagte, das Abschalten sei ein
    Startparameter des Dienstes und kein Feld dieser Anfrage — das war geraten und ist
    gemessen falsch: ``llama-server`` reicht ``chat_template_kwargs`` an die Vorlage durch,
    und ``enable_thinking: false`` greift je Aufruf. Es steht hier trotzdem nicht, und das
    ist eine Entscheidung und kein Vergessen.

    Gemessen am 2026-09-06 auf dieser Box gegen die Notizen des ersten Spielabends,
    identischer Prompt, ``gemma-4-12b``:

    ==================  ========  ========================================
    Denken              Dauer     Ergebnis
    ==================  ========  ========================================
    aus                 ~7,6 s    erzählt das Tischgespräch mit
    an                  ~70-76 s  reine Handlung, aus Sicht der Figuren
    ==================  ========  ========================================

    Abgeschaltet fiel die Nacherzählung in die Spielerebene zurück — »Die Spielgruppe
    besprach technische Details zur Aufzeichnung« als erster Satz eines Spielabends. Der
    Zwischenstand hat mit ``ZWISCHENSTAND_TIMEOUT`` acht Minuten, der Aufschrieb eine
    halbe Stunde; beide tragen die langsame Fassung mühelos, und **niemand wartet** auf
    das Ergebnis (#294). Geschwindigkeit war hier nie die knappe Ware, Genauigkeit schon.

    Wer das je umdrehen will, braucht einen Weg, an dem eine Sekunde wirklich zählt — und
    misst vorher, ob die Story-Regel aus ``sprache`` allein trägt. Sie half auch ohne
    Denken, aber nicht vollständig.
    """

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
        logger.info("Sprachmodell %s auf %s%s", self._model, self._base, OPENAI_CHAT_PATH)
        try:
            antwort = self._http.post(
                self._base + OPENAI_CHAT_PATH,
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
                f"{self._base}{OPENAI_CHAT_PATH} nicht erreichbar: {type(fehler).__name__}"
            ) from None
        return self._content(antwort)

    def _content(self, antwort) -> str:
        try:
            rumpf = antwort.json()
        except ValueError:
            raise ModelUnreachable(
                f"{self._base}{OPENAI_CHAT_PATH} hat kein JSON geliefert"
            ) from None
        if isinstance(rumpf, Mapping):
            # Ohne Gedächtnis über die Antwort hinaus: was die letzte Antwort nicht nennt,
            # nennt der Kopf nicht. Ein gemerkter Name aus einer früheren Antwort wäre
            # wieder eine Behauptung, nur eine ältere.
            self._geantwortet = _antwortname(rumpf)
        inhalt = _inhalt(rumpf) if isinstance(rumpf, Mapping) else None
        if not isinstance(inhalt, str) or not inhalt.strip():
            raise ModelUnreachable(f"Das Modell {self._model} hat nichts geschrieben")
        return inhalt.strip()


def _antwortname(rumpf: Mapping) -> str | None:
    """Der Name aus der Antwort — oder ``None``, und dann bleibt der Kopf ohne Namen."""
    wert = rumpf.get(ANTWORT_MODELL)
    return wert.strip() if isinstance(wert, str) and wert.strip() else None


def _inhalt(rumpf: Mapping) -> object:
    """Wo der geschriebene Text in einer OpenAI-förmigen Antwort liegt."""
    wahlen = rumpf.get("choices")
    erste = wahlen[0] if isinstance(wahlen, list) and wahlen else None
    block = erste.get("message") if isinstance(erste, Mapping) else None
    return block.get("content") if isinstance(block, Mapping) else None


def from_config(config: Config, *, timeout: float = DEFAULT_TIMEOUT) -> TextModel | None:
    """Der Klient zur Konfiguration — mit der Zeitgrenze des Weges, der ihn baut.

    Die Vorgabe gehört dem Aufschrieb: er darf lange rechnen. Der Zwischenstand reicht
    ``ZWISCHENSTAND_TIMEOUT`` herein, weil für ihn das Gegenteil gilt (#302).

    Bis #329 war dies auch die einzige Naht zwischen zwei Backends (#316); seit der
    Ollama-Weg gefallen ist, bleibt davon die Entscheidung, *ob* überhaupt ein Modell
    antwortet. Die Aufrufer sehen weiterhin nur ``TextModel``.
    """
    if not config.ollama_configured:
        return None
    return OpenAIClient(config, timeout=timeout)


def fenster_oeffnen(
    config: Config,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = LEASE_TIMEOUT,
    warten: Callable[[float], None] = time.sleep,
) -> bool:
    """Das Sitzungsfenster beim Nachbarn anmelden (#299).

    Die Nutzlast trägt **nur** das Profil und die Frist. Wer spielt, gehört nicht dazu: der
    Nachbar entscheidet daran nichts, und eine Runden-, Gilden- oder Sitzungskennung wäre
    eine Preisgabe an einen Dienst, der ohne sie auskommt. Ein Geheimnis reist auch keines
    mit — der Aufruf geht über die Schleife dieser Box, und seit #230 hat diese Instanz
    keines mehr.

    Bester Wille, wie alles auf diesem Weg: scheitert es, beginnt der Abend trotzdem.

    **Bis #329 lud die Anmeldung das Modell gleich mit** — der Aufwärm-Aufruf war die
    Antwort auf den offenen Punkt aus #299: mit dem Wegfall von ``halten`` (#303) zahlte
    der erste Szenenschnitt eines Abends sonst den Ladevorgang. Er hing an Ollamas
    ``keep_alive`` und ist mit ihm gefallen; ``llama-server`` lädt auf die Anmeldung hin
    selbst, und was er geladen hält, entscheidet das Profil.
    """
    if not config.gpu_lease or not config.ollama_configured:
        return False
    basis = DEFAULT_SOLARIS_URL.rstrip("/")
    try:
        antwort = http().post(
            basis + LEASE_PATH,
            json={"model": LEASE_PROFIL, "ttl_s": LEASE_TTL_S},
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
    return True


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


def freigeben(config: Config, *, http: Callable[[], object] = _http_session) -> bool:
    """Das Sitzungsfenster wieder schließen — ohne offenes Fenster geschieht nichts.

    **Dies ist die eine Freigabestelle** (#299/#300): die Stelle, an der jeder Aufschrieb
    endet (``kette.schreiben``, ``finally``). Ein zweiter Schließer liefe irgendwann gegen
    den ersten, deshalb hängt das Abmelden hier und nicht daneben.

    Bis #329 gab dieselbe Stelle davor Ollamas Modellhaltung mit ``keep_alive: 0`` frei —
    ohne Rücksicht darauf, ob dieser Prozess sie gesetzt hatte, denn nach einem Neustart
    mitten im Abend hielte Ollama trotzdem noch. Der Ablöser kennt keine Haltung, die zu
    beenden wäre; geblieben ist das Fenster.

    Der Vermerk fällt zuerst: ob der Nachbar es erfährt, ändert nichts daran, dass ab jetzt
    wieder das Haushaltsmodell antwortet. Bester Wille auch hier — ein Abend darf am
    Abmelden nicht scheitern.
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
