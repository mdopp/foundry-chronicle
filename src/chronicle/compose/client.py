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

**Seit #362 steht der Dienst hinter Laibs Lease** (laib ADR 0011). Adresse, Schlüssel und
Lease sind eingebaute Werte der Box, und ``model`` ist ein **Preset**: der Proxy vor dem
Router lädt, was die Anfrage nennt, sofern das stehende Fenster es erlaubt. Das Fenster
selbst nennen wir nie — wir fragen nach einem Modell, und die Lease wählt das engste.

**Wer geantwortet hat, sagt die Antwort — nie die Einstellung** (#320). Was wir erbitten,
ist eine Bitte und kein Beleg; der Name des antwortenden Modells steht oben in der
Antwort, und genau der — und nur der — wird weitergereicht.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol

import requests

from chronicle.config import Config

logger = logging.getLogger(__name__)

# Der einzige Weg (#316/#329). ``llama-server`` spricht die OpenAI-Form; ein ``keep_alive``
# gibt es dort **nicht** und wird auch nicht erfunden — es war ein Ollama-Feld, und ein
# ausgedachtes Gegenstück wäre eine Zusage, die niemand einlöst. Seit #362 hängt der Pfad
# an ``MODEL_URL``, und die nennt Laib samt ``/v1`` — die übliche OpenAI-Basis.
OPENAI_CHAT_PATH = "/chat/completions"

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

# **Die Frist, die wir für ein Fenster erbitten** (#299). Solange es steht, antwortet die
# Lease der Box mit *unserem* Modell, statt bei jeder Haushaltsanfrage ihres
# zurückzuholen — ein Tausch, der auf dieser Karte Sekunden kostet, in beide Richtungen.
# Laib nimmt 300 bis 86400 s und nennt den Takt selbst (``renew_after``, ein Drittel der
# Frist); eine Viertelstunde heißt: ein toter Prozess hält die Karte nicht länger als das.
LEASE_TTL_S = 900

# Erneuert wird dreimal je Frist — die **Rückfallebene**, falls die Antwort keinen Takt
# nennt (#306). Abgeleitet und nicht danebengeschrieben: zwei Zahlen liefen auseinander,
# sobald jemand eine von beiden anfasst, und das Fenster fiele mitten im Abend zu.
LEASE_ERNEUERUNG_S = LEASE_TTL_S / 3

LEASE_ERNEUERUNG_FELD = "renew_after"

# Wie lange auf die Lease gewartet wird. Sie steht auf derselben Box; wartet sie länger,
# hat der Abend schon begonnen — eine Anmeldung, die sich Zeit lässt, hält niemanden auf.
LEASE_TIMEOUT = 5.0

# **Wer das Fenster hält** — laut Vertrag der Name des fragenden Dienstes,
# ``[a-z0-9-]{1,64}``, Pflicht bei jedem ``POST`` und jedem eigenen ``DELETE``. Nur wer
# eingetragen ist, gibt frei; jeder andere bekommt ``409`` mit dem echten Halter.
#
# Eine **Konstante**, und das ist der Punkt: die Zusage aus #321, dass die Nutzlast keine
# Runden-, Gilden- oder Sitzungskennung trägt, hält nur, solange hier nichts Berechnetes
# steht. Laibs Lease weist ein unbekanntes Feld ohnehin ab (``unexpected_field``) — aber
# ihre Prüfung ist nicht unsere Zusage.
LEASE_HALTER = "foundry-chronicle"

LEASE_HALTER_FELD = "holder"

LEASE_STEHT = 200

# Kein Fehlschlag: die Lease lädt die Gewichte. Danach wird **nicht** erneut angemeldet,
# sondern mit ``GET`` gefragt — und die Frist beginnt erst, wenn sie ``ready`` sagt.
LEASE_VORBEREITUNG = 202

LEASE_BEREIT = "ready"

# Der Zustand, in dem kein Fenster steht. ``releasing`` gibt es bei Laib nicht: ein
# ``DELETE`` ist fertig, wenn die Antwort kommt.
LEASE_LEER = "none"

LEASE_ZUSTAND_FELD = "state"

LEASE_WARTEZEIT_FELD = "retry_after"

# Wie lange zwischen zwei Fragen gewartet wird, wenn die Lease selbst nichts nennt.
LEASE_WARTEZEIT_S = 30.0

# Und die Untergrenze dazu, auch wenn sie etwas nennt: ein Takt nahe null wäre die enge
# Schleife gegen einen Dienst, der ohnehin gerade lädt — die Lektion vom 2026-08-10.
LEASE_MINDESTPAUSE_S = 5.0

# Wie lange insgesamt auf ``ready`` gewartet wird. Eine Grenze braucht es, weil sonst eine
# Lease, die ewig »preparing« sagt, diesen Faden für immer bindet; großzügig darf sie
# sein, weil niemand darauf wartet — die Sitzung läuft daneben weiter und schreibt zur Not
# mit dem Haushaltsmodell, was der Herkunftsvermerk dann auch sagt (#320).
LEASE_VORBEREITUNG_S = 1800.0

# Wann das angemeldete Fenster von selbst ausläuft, gemessen an der monotonen Uhr dieses
# Prozesses. Kein Wert in der Datenbank: ein Neustart erbt das Fenster ausdrücklich nicht,
# und die Lease lässt es ohnehin verfallen, wenn niemand erneuert.
_lease_bis = 0.0

# Der Takt, in dem das offene Fenster erneuert wird — gesagt von der Lease, sonst abgeleitet.
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
        if not config.model_configured:
            raise ModelNotConfigured(KEIN_MODELL)
        self._base = str(config.model_url).rstrip("/")
        self._model = str(config.model_preset)
        self._kopf = _schluessel(config)
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
                headers=self._kopf,
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
    if not config.model_configured:
        return None
    return OpenAIClient(config, timeout=timeout)


def _schluessel(config: Config) -> dict[str, str]:
    """Der Schlüssel der Box als Kopfzeile — oder keiner, wenn sie keinen nennt.

    Er geht an genau zwei Stellen hinaus: an den Modelldienst und an die Lease derselben
    Box. Nie in eine Logzeile — ``Config.__repr__`` maskiert ihn, und keine Meldung hier
    nennt mehr als die Fehlerart.
    """
    return {"Authorization": f"Bearer {config.model_key}"} if config.model_key else {}


def _lease_moeglich(config: Config) -> bool:
    """Ob es überhaupt eine Lease gibt, mit der wir reden dürfen.

    Ohne Adresse gibt es keine; ohne Schlüssel weist sie jedes ``POST`` und ``DELETE`` mit
    ``401`` ab — ein Aufruf, dessen Ausgang feststeht, geht gar nicht erst hinaus.
    """
    return bool(config.gpu_lease and config.model_lease and config.model_key)


def fenster_oeffnen(
    config: Config,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = LEASE_TIMEOUT,
    warten: Callable[[float], None] = time.sleep,
) -> bool:
    """Das Sitzungsfenster bei der Lease der Box anmelden (#299, Vertrag seit #362).

    **Gefragt wird nach einem Modell, nicht nach einem Fenster** (laib ADR 0011, Ergänzung
    2026-10-14): ``model`` ist das Preset aus der Vorlage, und welches Fenster es erlaubt,
    entscheidet die Lease. Die Nutzlast ist genau ``{model, ttl_s, holder}`` — wer spielt,
    gehört nicht dazu: eine Runden-, Gilden- oder Sitzungskennung wäre eine Preisgabe an
    einen Dienst, der ohne sie auskommt, und Laib weist ein unbekanntes Feld ohnehin ab.

    **Der Schlüssel reist mit.** ``POST`` und ``DELETE`` verlangen
    ``Authorization: Bearer <MODEL_KEY>``; das ist die eine benannte Ausnahme von »ohne
    gemeinsames Geheimnis« (``CLAUDE.md``). Er ist kein Geheimnis dieser Instanz, sondern
    ein eingebauter Wert der Box, den jeder ihrer Dienste bekommt.

    Bester Wille, wie alles auf diesem Weg: scheitert es, beginnt der Abend trotzdem.
    """
    if not _lease_moeglich(config) or not config.model_configured:
        return False
    ziel = str(config.model_lease)
    try:
        antwort = http().post(
            ziel,
            json={
                "model": config.model_preset,
                "ttl_s": LEASE_TTL_S,
                LEASE_HALTER_FELD: LEASE_HALTER,
            },
            headers=_schluessel(config),
            timeout=timeout,
        )
    except requests.RequestException as fehler:
        logger.warning(
            "Das Sitzungsfenster kam bei der Lease nicht an (%s) — der Abend läuft trotzdem.",
            type(fehler).__name__,
        )
        return False
    if antwort.status_code == LEASE_VORBEREITUNG:
        stand = _bereit_abwarten(antwort, ziel, http=http, timeout=timeout, warten=warten)
        if stand is None:
            return False
        antwort = stand
    elif antwort.status_code != LEASE_STEHT:
        logger.warning(
            "Die Lease gibt die Karte nicht her (%s, %s) — der Abend läuft ohne Fenster.",
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
    """Warum die Lease ablehnt, für die eine Logzeile — und wer die Karte hält, wenn sie es sagt.

    Laib antwortet auf jede Ablehnung mit ``{ok: false, reason}``; bei ``held`` steht der
    Halter daneben. Ein Halter ist ein Dienstname, kein Mensch — er darf ins Log.
    """
    rumpf = _json(antwort)
    grund = str(rumpf.get("reason") or "ohne Angabe")
    halter = rumpf.get(LEASE_HALTER_FELD)
    return f"{grund}, gehalten von {halter}" if halter else grund


def _wartezeit(antwort) -> float:
    """Wie lange bis zur nächsten Frage: was die Lease nennt, in vernünftigen Schranken."""
    wert = _json(antwort).get(LEASE_WARTEZEIT_FELD)
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return LEASE_WARTEZEIT_S
    return min(max(float(wert), LEASE_MINDESTPAUSE_S), LEASE_VORBEREITUNG_S)


def _bereit_abwarten(
    antwort,
    ziel: str,
    *,
    http: Callable[[], object],
    timeout: float,
    warten: Callable[[float], None],
):
    """Auf ``ready`` warten, ohne erneut anzumelden und ohne den Abend anzuhalten (#321).

    Ein zweites ``POST`` verbietet der Vertrag ausdrücklich. Gefragt wird deshalb mit
    ``GET`` — ohne Schlüssel, Lesen ist bei Laib offen —, frühestens nach der genannten
    Wartezeit und nur, solange das Budget reicht. Zählt nur ein ``ready``, das **unser**
    ist: sagt die Lease es für einen anderen Halter, hat uns jemand die Karte genommen,
    während die Gewichte luden. Gibt die Antwort mit ``ready`` zurück, sonst ``None``.
    """
    ende = time.monotonic() + LEASE_VORBEREITUNG_S
    pause = _wartezeit(antwort)
    while time.monotonic() + pause <= ende:
        warten(pause)
        try:
            stand = http().get(ziel, timeout=timeout)
            stand.raise_for_status()
        except requests.RequestException as fehler:
            logger.warning(
                "Die Lease sagt nichts zum vorbereiteten Fenster (%s) — der Abend läuft "
                "ohne es weiter.",
                type(fehler).__name__,
            )
            return None
        rumpf = _json(stand)
        if rumpf.get(LEASE_HALTER_FELD) != LEASE_HALTER:
            logger.warning(
                "Das vorbereitete Fenster gehört nicht mehr uns (%s) — der Abend läuft ohne es "
                "weiter.",
                rumpf.get(LEASE_HALTER_FELD) or "niemand",
            )
            return None
        if rumpf.get(LEASE_ZUSTAND_FELD) == LEASE_BEREIT:
            return stand
        pause = _wartezeit(stand)
    logger.warning("Das Fenster wurde nicht rechtzeitig fertig — der Abend läuft ohne es weiter.")
    return None


def _genannter_takt(antwort) -> float:
    """Der Erneuerungstakt aus der Antwort der Lease — oder die eigene Ableitung (#306).

    Bester Wille auch hier: eine Antwort ohne JSON, ohne das Feld oder mit einer Zahl, die
    außerhalb der angemeldeten Frist liegt, kostet kein Fenster. Sie kostet nur den
    genannten Takt, und den ersetzt die Ableitung.
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

    Der Vermerk fällt zuerst: ob die Lease es erfährt, ändert nichts daran, dass ab jetzt
    wieder das Haushaltsmodell antwortet. Bester Wille auch hier — ein Abend darf am
    Abmelden nicht scheitern.

    **Der Rumpf ist nicht Zierrat, sondern der Unterschied zum Notausgang** (#333). Ein
    ``DELETE`` *ohne* Rumpf ist bei Laib der Weg des Betreibers aus einem Fenster, das
    niemand erneuert: er gibt frei, **ohne** den Halter zu prüfen. Mit ``{holder}`` lehnt
    die Lease ab, was nicht uns gehört.
    """
    global _lease_bis
    if not lease_offen():
        return False
    _lease_bis = 0.0
    if not _lease_moeglich(config):
        return False
    try:
        antwort = http().delete(
            str(config.model_lease),
            json={LEASE_HALTER_FELD: LEASE_HALTER},
            headers=_schluessel(config),
            timeout=LEASE_TIMEOUT,
        )
        antwort.raise_for_status()
    except requests.RequestException as fehler:
        logger.warning(
            "Das Sitzungsfenster ließ sich nicht abmelden (%s) — es verfällt von selbst.",
            type(fehler).__name__,
        )
        return False
    return True


def verwaistes_fenster_schliessen(
    config: Config,
    *,
    http: Callable[[], object] = _http_session,
    timeout: float = LEASE_TIMEOUT,
) -> bool:
    """Beim Start ein Fenster abräumen, das ein **toter Vorgänger** offen ließ (#333).

    Die Verbraucherregel des Vertrags: beim Start ``GET``, und hält unser eigener Halter
    ein Fenster, wird es zuerst mit ``DELETE`` zurückgegeben. ``lease_offen`` ist
    prozesslokal: stirbt der Prozess mit offenem Fenster, gibt sonst niemand frei, und die
    Box hält bis zum Ablauf unser Modell statt ihres.

    Kein zweiter Schließer neben der einen Freigabestelle (#299/#300): hier gibt es noch
    kein eigenes Fenster, gegen das dieser Aufruf laufen könnte, und danach nie wieder.
    Und das gilt für ``preparing`` wie für ``ready`` — bei einem Prozessstart kann ein
    Fenster unter unserem Halter nur von einem Vorgänger stammen. (Bis #362 blieb ein
    ``preparing`` stehen; das war eine Regel des Nachbarn ``solaris``, bei dem eine
    Freigabe Zeit brauchte. Laibs ``DELETE`` ist fertig, wenn die Antwort kommt.)

    Geschlossen wird **nur**, was uns gehört. Ein fremdes Fenster bleibt stehen: es zu
    schließen kostete einem anderen Dienst seinen laufenden Lauf, und das wiegt schwerer
    als unsere Viertelstunde.

    Bester Wille wie alles auf diesem Weg: scheitert es, startet der Bot trotzdem.
    """
    if not _lease_moeglich(config):
        return False
    ziel = str(config.model_lease)
    try:
        stand = http().get(ziel, timeout=timeout)
        stand.raise_for_status()
        rumpf = _json(stand)
        if (
            rumpf.get(LEASE_HALTER_FELD) != LEASE_HALTER
            or rumpf.get(LEASE_ZUSTAND_FELD, LEASE_LEER) == LEASE_LEER
        ):
            return False
        antwort = http().delete(
            ziel,
            json={LEASE_HALTER_FELD: LEASE_HALTER},
            headers=_schluessel(config),
            timeout=timeout,
        )
        antwort.raise_for_status()
    except requests.RequestException as fehler:
        logger.warning(
            "Ein verwaistes Sitzungsfenster ließ sich nicht abräumen (%s) — es verfällt von "
            "selbst.",
            type(fehler).__name__,
        )
        return False
    logger.info("Ein Sitzungsfenster eines toten Vorgängers wurde beim Start abgeräumt.")
    return True
