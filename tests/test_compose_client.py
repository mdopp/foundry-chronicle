"""Der Aufruf des Modelldienstes — ohne Netz, gegen eine nachgebaute HTTP-Sitzung.

Das Sitzungsfenster bei Laibs Lease steht seit #362 in ``test_laib_lease.py``: dort gegen
eine Attrappe, die den Vertrag auf dem Draht spricht.
"""

import pathlib

import pytest
import requests

from chronicle import sprache as sprachen
from chronicle.compose.client import (
    DEFAULT_TIMEOUT,
    OPENAI_CHAT_PATH,
    ZWISCHENSTAND_TIMEOUT,
    ModelNotConfigured,
    ModelUnreachable,
    OpenAIClient,
    from_config,
)
from chronicle.compose.composer import SceneMaterial, SessionMaterial, compose
from chronicle.config import Config

# Wie Laib sie nennt (``${MODEL_URL}``): die OpenAI-Basis samt ``/v1``.
ADRESSE = "http://host.containers.internal:11435/v1/"
BASIS = ADRESSE.rstrip("/")
MODELL = "gemma-4-12b"
SCHLUESSEL = "modell-schluessel-nur-im-test"


class Antwort:
    def __init__(self, payload=None, fehler=None, status_code=200):
        self._payload = payload
        self._fehler = fehler
        self.status_code = status_code

    def raise_for_status(self):
        if self._fehler is not None:
            raise self._fehler

    def json(self):
        if self._payload is None:
            raise ValueError("kein JSON")
        return self._payload


class Http:
    def __init__(self, antwort=None, fehler=None):
        self.aufrufe = []
        self._antwort = antwort
        self._fehler = fehler

    def post(self, url, **kwargs):
        self.aufrufe.append((url, kwargs))
        if self._fehler is not None:
            raise self._fehler
        return self._antwort

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)


def config(tmp_path, *, url=ADRESSE, model=MODELL, key=SCHLUESSEL):
    """Der Aufbau, den der Dienst seit #362 kennt: Laibs Adresse, ihr Schlüssel, ein Preset."""
    return Config(model_url=url, model_preset=model, model_key=key, data_dir=tmp_path)


def klient(tmp_path, http, **kwargs):
    return OpenAIClient(config(tmp_path, **kwargs), http=lambda: http)


def v1_antwort(text="Ein ruhiger Abend."):
    return Antwort({"choices": [{"message": {"content": text}}]})


def test_ruft_den_pfad_des_modelldienstes_und_traegt_keine_frist(tmp_path):
    """``keep_alive`` war Ollamas Feld — der Ablöser hat keines, und wir erfinden keines."""
    http = Http(v1_antwort("  Ein ruhiger Abend.  "))
    text = klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")

    url, kwargs = http.aufrufe[0]
    assert url == "http://host.containers.internal:11435/v1/chat/completions"
    assert url == f"{BASIS}{OPENAI_CHAT_PATH}"
    # Das Preset aus der Vorlage: Laibs Proxy lädt, was die Anfrage nennt (#362).
    assert kwargs["json"]["model"] == MODELL
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["messages"] == [
        {"role": "system", "content": "Ordne."},
        {"role": "user", "content": "Szene 1"},
    ]
    assert "keep_alive" not in kwargs["json"]
    assert kwargs["timeout"] > 0
    assert text == "Ein ruhiger Abend."


def test_ohne_gewaehltes_modell_gibt_es_keinen_klienten(tmp_path):
    with pytest.raises(ModelNotConfigured) as fehler:
        OpenAIClient(Config(data_dir=tmp_path))
    assert "Noch kein Modell gewählt" in str(fehler.value)
    assert "OLLAMA" not in str(fehler.value)


def test_ohne_adresse_der_box_gibt_es_keinen_klienten_und_keine_geratene(tmp_path):
    """#362: die Adresse nennt Laib (``${MODEL_URL}``); eine Vorgabe hier wäre geraten."""
    assert from_config(config(tmp_path, url=None)) is None
    with pytest.raises(ModelNotConfigured):
        OpenAIClient(config(tmp_path, url=None))


def test_der_schluessel_der_box_geht_als_bearer_mit(tmp_path):
    """Der Proxy vor dem Modell nimmt denselben Schlüssel wie die Lease (laib #42)."""
    http = Http(v1_antwort())
    klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")
    assert http.aufrufe[0][1]["headers"] == {"Authorization": f"Bearer {SCHLUESSEL}"}


def test_ohne_schluessel_geht_keine_leere_kopfzeile_hinaus(tmp_path):
    http = Http(v1_antwort())
    klient(tmp_path, http, key=None).write(system="Ordne.", prompt="Szene 1")
    assert "Authorization" not in http.aufrufe[0][1]["headers"]


def test_ein_nicht_erreichbarer_dienst_ist_eine_verstaendliche_meldung(tmp_path):
    http = Http(fehler=requests.ConnectionError("weg"))
    with pytest.raises(ModelUnreachable) as fehler:
        klient(tmp_path, http).write(system="", prompt="")
    assert "nicht erreichbar" in str(fehler.value)
    assert "ConnectionError" in str(fehler.value)


def test_vor_der_ersten_antwort_gibt_es_keinen_namen(tmp_path):
    """#320: die Einstellung ist eine Bitte, kein Beleg — und noch hat niemand geantwortet."""
    assert klient(tmp_path, Http()).name is None


def test_der_name_kommt_aus_der_antwort_und_nicht_aus_der_einstellung(tmp_path):
    """``llama-server`` ignoriert unseren Namen — die Antwort ist die Wahrheit.

    Gemessen am 2026-09-05 auf dieser Box: er nennt sich ohne ``--alias`` nach seiner
    GGUF-Datei. Hässlich, aber wahr; nach ``mdopp/solarisbay#1333`` wird derselbe Satz von
    selbst schön.
    """
    geladen = "/models/Gemma-4-12B-Q4_K_M.gguf"
    antwort = Antwort({"model": geladen, **v1_antwort().json()})
    modell = klient(tmp_path, Http(antwort))

    modell.write(system="Ordne.", prompt="Szene 1")

    assert modell.name == geladen
    assert modell.name != MODELL


@pytest.mark.parametrize(
    "genannt",
    [{}, {"model": ""}, {"model": "   "}, {"model": 12}, {"model": None}],
)
def test_ohne_verwertbaren_namen_bleibt_er_leer_statt_erfunden(tmp_path, genannt):
    """Eine Chronik ohne Herkunftsangabe ist ehrlich; eine mit falscher ist es nicht."""
    modell = klient(tmp_path, Http(Antwort({**genannt, **v1_antwort("Abend.").json()})))
    modell.write(system="Ordne.", prompt="Szene 1")
    assert modell.name is None


class Reihe(Http):
    """Ein Dienst, der der Reihe nach verschieden antwortet."""

    def __init__(self, antworten):
        super().__init__()
        self._reihe = list(antworten)

    def post(self, url, **kwargs):
        self.aufrufe.append((url, kwargs))
        return self._reihe.pop(0)


def test_der_name_erinnert_sich_nicht_an_eine_fruehere_antwort(tmp_path):
    """Was die letzte Antwort nicht nennt, nennt der Kopf nicht — auch nichts Gemerktes."""
    http = Reihe(
        [
            Antwort({"model": "erst", **v1_antwort("a").json()}),
            v1_antwort("b"),
        ]
    )
    modell = klient(tmp_path, http)

    modell.write(system="", prompt="")
    assert modell.name == "erst"

    modell.write(system="", prompt="")
    assert modell.name is None


def test_wer_den_klienten_baut_bestimmt_die_zeitgrenze(tmp_path):
    """#302: der Aufschrieb darf lange rechnen, der Zwischenstand ausdrücklich nicht."""
    http = Http(v1_antwort())

    klient(tmp_path, http).write(system="", prompt="")
    assert http.aufrufe[-1][1]["timeout"] == DEFAULT_TIMEOUT

    OpenAIClient(config(tmp_path), http=lambda: http, timeout=ZWISCHENSTAND_TIMEOUT).write(
        system="", prompt=""
    )
    assert http.aufrufe[-1][1]["timeout"] == ZWISCHENSTAND_TIMEOUT
    # »Deutlich knapper« ist die ganze Aussage — eine Grenze, die dem Aufschrieb gleicht,
    # löste das Problem nicht. Der Faktor stand auf vier, solange das Modell ohne Denken
    # antwortete; seit #325 kostet derselbe Lauf das Zwanzigfache, und die Grenze ist mit
    # #330 auf acht Minuten gewandert. Drei ist damit die Aussage, die noch trägt: knapper
    # bleibt knapper, aber nicht knapper als der Lauf, den sie zulassen soll.
    assert ZWISCHENSTAND_TIMEOUT * 3 < DEFAULT_TIMEOUT


def test_es_gibt_weiterhin_nur_eine_freigabestelle():
    """#300/#299: eine Stelle, an der jeder Aufschrieb endet — und dort schließt das Fenster.

    Ein zweiter Schließer liefe irgendwann gegen den ersten; deshalb hängt das Abmelden
    **in** der Freigabe und nicht daneben.
    """
    quellen = pathlib.Path("src/chronicle").rglob("*.py")
    rufer = {
        pfad.as_posix()
        for pfad in quellen
        if "modell.freigeben(" in pfad.read_text(encoding="utf-8")
    }
    assert rufer == {"src/chronicle/kette.py"}


# --------------------------------------------------------------------------------------
# Was am Klienten hängt: Naht, Fehlerbild und die Zahlensperre dahinter
# --------------------------------------------------------------------------------------


def test_ohne_ansage_antwortet_der_abloeser(tmp_path):
    """Die Naht ist ``from_config`` und sonst nichts — und es gibt nur noch einen Weg.

    Sie stand bis #329 auf Ollama, weil die Box das fuhr; mit #335 wanderte die Vorgabe,
    und jetzt ist der zweite Weg ganz gefallen. Geblieben ist die Entscheidung, *ob*
    überhaupt ein Modell antwortet.
    """
    assert isinstance(from_config(config(tmp_path)), OpenAIClient)
    assert from_config(Config(data_dir=tmp_path)) is None


@pytest.mark.parametrize(
    "antwort",
    [
        # Der gemessene Fall: ``llama-server`` kennt ``/api/chat`` nicht — hier umgekehrt,
        # ein Server, der auch den neuen Pfad nicht bedient.
        Antwort(fehler=requests.HTTPError("404")),
        Antwort(),
        Antwort({"choices": []}),
        Antwort({"choices": ["kein Rumpf"]}),
        Antwort({"choices": [{"message": {"content": "   "}}]}),
        # Ollamas Form auf dem neuen Pfad: eine Antwort, aber nicht unsere.
        Antwort({"message": {"content": "Ein ruhiger Abend."}}),
    ],
)
def test_eine_unbrauchbare_antwort_ist_dieselbe_verstaendliche_meldung(tmp_path, antwort):
    with pytest.raises(ModelUnreachable):
        klient(tmp_path, Http(antwort)).write(system="", prompt="")


def material():
    return SessionMaterial(
        session_id=1,
        played_on="2026-09-05",
        title="Der Keller",
        scenes=(SceneMaterial(position=1, title="Aufbruch", notes=("Erste Notiz.",), facts=()),),
    )


def test_ein_scheiterndes_modell_liefert_geordnet_statt_erzaehlt(tmp_path):
    """Der Fehler bleibt im Klienten: keine Ausnahme schlägt bis zur Komposition durch."""
    http = Http(Antwort(fehler=requests.HTTPError("404")))
    ergebnis = compose(material(), klient(tmp_path, http), inhaltssprache=sprachen.DEUTSCH)
    assert "Erste Notiz." in ergebnis.text
    assert ergebnis.prose_count == 0


def test_die_zahlensperre_greift_vor_der_ausgabe(tmp_path):
    """Mechanisch und vor der Ausgabe — der Weg des Aufrufs ändert daran nichts."""
    http = Http(v1_antwort("Es waren 42 Ratten."))
    ergebnis = compose(material(), klient(tmp_path, http), inhaltssprache=sprachen.DEUTSCH)
    assert "42" not in ergebnis.text
