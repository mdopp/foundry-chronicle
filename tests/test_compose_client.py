"""Der Aufruf des Modelldienstes — ohne Netz, gegen eine nachgebaute HTTP-Sitzung."""

import pathlib
from dataclasses import replace

import pytest
import requests

from chronicle import sprache as sprachen
from chronicle.compose.client import (
    CHAT_PATH,
    DEFAULT_TIMEOUT,
    FREIGABE,
    KNAPPE_HALTUNG,
    LEASE_ERNEUERUNG_FELD,
    LEASE_ERNEUERUNG_S,
    LEASE_HALTUNG,
    LEASE_MINDESTPAUSE_S,
    LEASE_PATH,
    LEASE_PROFIL,
    LEASE_TTL_S,
    LEASE_VORBEREITUNG,
    LEASE_VORBEREITUNG_S,
    LEASE_WARTEZEIT_S,
    OPENAI_CHAT_PATH,
    TAGS_PATH,
    ZWISCHENSTAND_TIMEOUT,
    ModelNotConfigured,
    ModelUnreachable,
    OllamaClient,
    OpenAIClient,
    erneuerung,
    fenster_oeffnen,
    freigeben,
    from_config,
    installed_models,
    lease_offen,
)
from chronicle.compose.composer import SceneMaterial, SessionMaterial, compose
from chronicle.config import (
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    DEFAULT_OLLAMA_URL,
    DEFAULT_SOLARIS_URL,
    Config,
)

ADRESSE = "http://ollama.example:11434/"
MODELL = "chronist-modell"


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


def config(tmp_path, *, url=ADRESSE, model=MODELL):
    return Config(ollama_url=url, ollama_model=model, data_dir=tmp_path)


def klient(tmp_path, http, **kwargs):
    return OllamaClient(config(tmp_path, **kwargs), http=lambda: http)


def test_baut_den_aufruf_wie_ollama_ihn_erwartet(tmp_path):
    http = Http(Antwort({"message": {"content": "  Ein ruhiger Abend.  "}}))
    text = klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")

    url, kwargs = http.aufrufe[0]
    assert url == f"http://ollama.example:11434{CHAT_PATH}"
    assert kwargs["json"]["model"] == MODELL
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["messages"] == [
        {"role": "system", "content": "Ordne."},
        {"role": "user", "content": "Szene 1"},
    ]
    assert kwargs["json"]["keep_alive"] == KNAPPE_HALTUNG
    assert kwargs["timeout"] > 0
    assert text == "Ein ruhiger Abend."


def test_ohne_gewaehltes_modell_gibt_es_keinen_klienten(tmp_path):
    with pytest.raises(ModelNotConfigured) as fehler:
        OllamaClient(Config(data_dir=tmp_path))
    assert "Noch kein Modell gewählt" in str(fehler.value)
    assert "OLLAMA" not in str(fehler.value)


def test_ohne_eigene_adresse_redet_der_klient_mit_dem_ollama_dieser_box(tmp_path):
    http = Http(Antwort({"message": {"content": "Ein ruhiger Abend."}}))
    klient(tmp_path, http, url=None).write(system="Ordne.", prompt="Szene 1")
    assert http.aufrufe[0][0] == f"{DEFAULT_OLLAMA_URL}{CHAT_PATH}"


def test_ein_nicht_erreichbares_ollama_ist_eine_verstaendliche_meldung(tmp_path):
    http = Http(fehler=requests.ConnectionError("weg"))
    with pytest.raises(ModelUnreachable) as fehler:
        klient(tmp_path, http).write(system="", prompt="")
    assert "nicht erreichbar" in str(fehler.value)
    assert "ConnectionError" in str(fehler.value)


def test_ein_fehlerstatus_zaehlt_ebenfalls_als_unerreichbar(tmp_path):
    http = Http(Antwort(fehler=requests.HTTPError("500")))
    with pytest.raises(ModelUnreachable):
        klient(tmp_path, http).write(system="", prompt="")


def test_eine_antwort_ohne_json_ist_kein_text(tmp_path):
    with pytest.raises(ModelUnreachable):
        klient(tmp_path, Http(Antwort())).write(system="", prompt="")


@pytest.mark.parametrize("rumpf", [{}, {"message": {}}, {"message": {"content": "   "}}])
def test_eine_leere_antwort_wird_nicht_als_absatz_ausgegeben(tmp_path, rumpf):
    with pytest.raises(ModelUnreachable):
        klient(tmp_path, Http(Antwort(rumpf))).write(system="", prompt="")


def test_vor_der_ersten_antwort_gibt_es_keinen_namen(tmp_path):
    """#320: die Einstellung ist eine Bitte, kein Beleg — und noch hat niemand geantwortet."""
    assert klient(tmp_path, Http()).name is None


@pytest.mark.parametrize(
    ("bauart", "antwort"),
    [
        (OllamaClient, lambda name: Antwort({"model": name, "message": {"content": "Abend."}})),
        (OpenAIClient, lambda name: Antwort({"model": name, **v1_antwort().json()})),
    ],
)
def test_der_name_kommt_aus_der_antwort_und_nicht_aus_der_einstellung(tmp_path, bauart, antwort):
    """Auf dem ``/v1``-Weg ignoriert der Server unseren Namen — dort ist die Antwort die Wahrheit.

    Gemessen am 2026-09-05 auf dieser Box: ``llama-server`` nennt sich ohne ``--alias`` nach
    seiner GGUF-Datei. Hässlich, aber wahr; nach ``mdopp/solarisbay#1333`` wird derselbe Satz
    von selbst schön.
    """
    geladen = "/models/Gemma-4-12B-Q4_K_M.gguf"
    modell = bauart(config(tmp_path), http=lambda: Http(antwort(geladen)))

    modell.write(system="Ordne.", prompt="Szene 1")

    assert modell.name == geladen
    assert modell.name != MODELL


@pytest.mark.parametrize(
    "rumpf",
    [
        {"message": {"content": "Abend."}},
        {"model": "", "message": {"content": "Abend."}},
        {"model": "   ", "message": {"content": "Abend."}},
        {"model": 12, "message": {"content": "Abend."}},
        {"model": None, "message": {"content": "Abend."}},
    ],
)
def test_ohne_verwertbaren_namen_bleibt_er_leer_statt_erfunden(tmp_path, rumpf):
    """Eine Chronik ohne Herkunftsangabe ist ehrlich; eine mit falscher ist es nicht."""
    modell = klient(tmp_path, Http(Antwort(rumpf)))
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
            Antwort({"model": "erst", "message": {"content": "a"}}),
            Antwort({"message": {"content": "b"}}),
        ]
    )
    modell = klient(tmp_path, http)

    modell.write(system="", prompt="")
    assert modell.name == "erst"

    modell.write(system="", prompt="")
    assert modell.name is None


def test_from_config_liefert_ohne_konfiguration_kein_modell(tmp_path):
    assert from_config(Config(data_dir=tmp_path)) is None
    assert isinstance(from_config(config(tmp_path)), OllamaClient)


def test_wer_den_klienten_baut_bestimmt_die_zeitgrenze(tmp_path):
    """#302: der Aufschrieb darf lange rechnen, der Zwischenstand ausdrücklich nicht."""
    http = Http(Antwort({"message": {"content": "Ein ruhiger Abend."}}))

    OllamaClient(config(tmp_path), http=lambda: http).write(system="", prompt="")
    assert http.aufrufe[-1][1]["timeout"] == DEFAULT_TIMEOUT

    OllamaClient(config(tmp_path), http=lambda: http, timeout=ZWISCHENSTAND_TIMEOUT).write(
        system="", prompt=""
    )
    assert http.aufrufe[-1][1]["timeout"] == ZWISCHENSTAND_TIMEOUT
    # »Deutlich knapper« ist die ganze Aussage — eine Grenze, die dem Aufschrieb gleicht,
    # löste das Problem nicht.
    assert ZWISCHENSTAND_TIMEOUT * 4 < DEFAULT_TIMEOUT


TAGS = {
    "models": [
        {"name": "gemma4:e4b"},
        {"name": "gemma4:12b"},
        {"name": "nomic-embed-text:latest"},
        {},
    ]
}


def test_die_installierten_modelle_kommen_aus_api_tags():
    http = Http(Antwort(TAGS))
    namen = installed_models(ADRESSE, http=lambda: http)

    url, kwargs = http.aufrufe[0]
    assert url == f"http://ollama.example:11434{TAGS_PATH}"
    assert kwargs["timeout"] <= 5
    # Einbettungsmodelle schreiben keinen Text und werden nicht angeboten.
    assert namen == ("gemma4:12b", "gemma4:e4b")


def test_ein_abgeschaltetes_ollama_ist_eine_verstaendliche_meldung():
    http = Http(fehler=requests.ConnectionError("weg"))
    with pytest.raises(ModelUnreachable) as fehler:
        installed_models(ADRESSE, http=lambda: http)
    assert "nicht erreichbar" in str(fehler.value)


def test_ein_fehlerstatus_auf_tags_zaehlt_ebenfalls_als_unerreichbar():
    with pytest.raises(ModelUnreachable):
        installed_models(ADRESSE, http=lambda: Http(Antwort(fehler=requests.HTTPError("500"))))


@pytest.mark.parametrize("rumpf", [None, {}, {"models": "keine Liste"}])
def test_eine_unerwartete_antwort_ist_keine_modellliste(rumpf):
    antwort = Antwort() if rumpf is None else Antwort(rumpf)
    with pytest.raises(ModelUnreachable):
        installed_models(ADRESSE, http=lambda: Http(antwort))


def test_ein_ollama_ohne_textmodelle_liefert_eine_leere_liste():
    http = Http(Antwort({"models": [{"name": "nomic-embed-text"}]}))
    assert installed_models(ADRESSE, http=lambda: http) == ()


def test_kein_aufruf_laesst_die_frist_weg(tmp_path):
    """#303: ein Aufruf ohne ``keep_alive`` erbt die Vorgabe der Box — vierundzwanzig Stunden.

    Deshalb steht das Feld an *jedem* Aufruf und an keiner Bedingung. Der Test läuft die
    Wege ab, an denen früher ein Zweig hing: mit und ohne eigene Adresse, mit der knappen
    Zeitgrenze des Zwischenstands, und noch einmal nach einer Freigabe.
    """
    http = Http(Antwort({"message": {"content": "Ein ruhiger Abend."}}))

    klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")
    klient(tmp_path, http, url=None).write(system="Ordne.", prompt="Szene 2")
    OllamaClient(config(tmp_path), http=lambda: http, timeout=ZWISCHENSTAND_TIMEOUT).write(
        system="Ordne.", prompt="Szene 3"
    )
    freigeben(config(tmp_path), http=lambda: http)
    klient(tmp_path, http).write(system="Ordne.", prompt="Szene 4")

    geschrieben = [kwargs["json"] for _, kwargs in http.aufrufe if kwargs["json"]["messages"]]
    assert len(geschrieben) == 4
    assert {rumpf["keep_alive"] for rumpf in geschrieben} == {KNAPPE_HALTUNG}


def test_die_frist_am_aufruf_ist_knapp_und_endlich():
    """Gehalten wird seit #303 nicht mehr — die Frist überbrückt nur noch den nächsten Aufruf.

    Ollama kennt für »für immer« einen negativen Wert; er käme hier einer Sperre gleich.
    Null wäre die andere Übertreibung: innerhalb eines Aufschriebs folgen Chronik und
    Rückblick unmittelbar aufeinander, und dazwischen zu entladen kostete den Ladevorgang
    zweimal.
    """
    zahl, einheit = KNAPPE_HALTUNG[:-1], KNAPPE_HALTUNG[-1]
    assert einheit in {"m", "h"}
    minuten = float(zahl) if einheit == "m" else float(zahl) * 60
    assert 0 < minuten <= 15


def test_der_abschluss_gibt_das_modell_mit_null_wieder_frei(tmp_path):
    http = Http(Antwort({"message": {"content": "Ein ruhiger Abend."}}))

    assert freigeben(config(tmp_path), http=lambda: http) is True

    url, kwargs = http.aufrufe[-1]
    assert url == f"http://ollama.example:11434{CHAT_PATH}"
    # Kein Wort schreiben lassen: der Aufruf entlädt nur.
    assert kwargs["json"]["messages"] == []
    assert kwargs["json"]["keep_alive"] == FREIGABE
    assert FREIGABE == 0


def test_ein_abgeschaltetes_ollama_haelt_den_abend_nicht_auf(tmp_path):
    """Die Freigabe ist bester Wille: ein Abend darf weder daran hängen noch daran scheitern."""
    http = Http(fehler=requests.ConnectionError("weg"))
    assert freigeben(config(tmp_path), http=lambda: http) is False


def test_ein_fehlerstatus_bei_der_freigabe_zaehlt_ebenfalls_als_gescheitert(tmp_path):
    http = Http(Antwort(fehler=requests.HTTPError("500")))
    assert freigeben(config(tmp_path), http=lambda: http) is False


def test_ohne_gewaehltes_modell_gibt_es_nichts_freizugeben(tmp_path):
    http = Http()
    assert freigeben(Config(data_dir=tmp_path), http=lambda: http) is False
    assert http.aufrufe == []


def test_ohne_eigene_adresse_geht_die_freigabe_an_das_ollama_dieser_box(tmp_path):
    http = Http(Antwort({}))
    freigeben(config(tmp_path, url=None), http=lambda: http)
    assert http.aufrufe[0][0] == f"{DEFAULT_OLLAMA_URL}{CHAT_PATH}"


# ---------------------------------------------------------------------------
# Das Sitzungsfenster gegen den Nachbardienst (#299)
# ---------------------------------------------------------------------------


class Fenster(Http):
    """Wie ``Http``, kennt aber auch das Abmelden — und wer wohin gefragt hat."""

    def __init__(self, antwort=None, fehler=None, delete_fehler=None):
        super().__init__(antwort, fehler)
        self._delete_fehler = delete_fehler
        self.abmeldungen = []

    def delete(self, url, **kwargs):
        self.abmeldungen.append((url, kwargs))
        if self._delete_fehler is not None:
            raise self._delete_fehler
        return self._antwort if self._antwort is not None else Antwort({})


@pytest.fixture(autouse=True)
def ohne_offenes_fenster(monkeypatch):
    """Kein Test erbt das Fenster eines anderen — und keiner lässt eines stehen."""
    monkeypatch.setattr("chronicle.compose.client._lease_bis", 0.0)
    monkeypatch.setattr("chronicle.compose.client._lease_erneuerung_s", LEASE_ERNEUERUNG_S)


def anmeldungen(http):
    return [(url, kwargs) for url, kwargs in http.aufrufe if url.endswith(LEASE_PATH)]


def test_der_beginn_meldet_das_fenster_mit_modell_und_frist_an(tmp_path):
    """Genau ein POST, und die Nutzlast sagt nur, *was* geladen wird und *wie lange*.

    Wer spielt, gehört nicht hinein: der Nachbar entscheidet daran nichts, und eine
    Runden-, Gilden- oder Sitzungskennung wäre eine Preisgabe ohne Gegenwert.
    """
    http = Fenster(Antwort({}))
    assert fenster_oeffnen(config(tmp_path), http=lambda: http) is True

    ((url, kwargs),) = anmeldungen(http)
    assert url == f"{DEFAULT_SOLARIS_URL}{LEASE_PATH}"
    assert kwargs["json"] == {"model": MODELL, "ttl_s": LEASE_TTL_S}
    assert kwargs["timeout"] > 0


def test_die_anmeldung_traegt_keine_kennung_und_kein_geheimnis(tmp_path):
    http = Fenster(Antwort({}))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    ((_, kwargs),) = anmeldungen(http)
    assert set(kwargs["json"]) == {"model", "ttl_s"}
    for verboten in ("runde", "runde_id", "guild", "guild_id", "session", "session_id"):
        assert verboten not in kwargs["json"]
    # Kein Token, kein Header, keine Anmeldung — die Schleife dieser Box ist der Beleg.
    assert set(kwargs) == {"json", "timeout"}
    assert DEFAULT_SOLARIS_URL.startswith("http://127.0.0.1:")


def test_bei_offenem_fenster_tragen_die_aufrufe_die_frist_des_fensters(tmp_path):
    """Eine Konstante, zwei Werte: die knappe Frist bleibt die Norm, das Fenster die Ausnahme."""
    http = Fenster(Antwort({"message": {"content": "Ein ruhiger Abend."}}))

    klient(tmp_path, http).write(system="Ordne.", prompt="Vor dem Fenster")
    assert http.aufrufe[-1][1]["json"]["keep_alive"] == KNAPPE_HALTUNG

    fenster_oeffnen(config(tmp_path), http=lambda: http)
    klient(tmp_path, http).write(system="Ordne.", prompt="Im Fenster")
    assert http.aufrufe[-1][1]["json"]["keep_alive"] == LEASE_HALTUNG

    freigeben(config(tmp_path), http=lambda: http)
    klient(tmp_path, http).write(system="Ordne.", prompt="Nach dem Fenster")
    assert http.aufrufe[-1][1]["json"]["keep_alive"] == KNAPPE_HALTUNG


def test_die_frist_des_fensters_und_das_keep_alive_kommen_aus_derselben_konstante():
    """Zwei Zahlen liefen auseinander; das Fenster fiele dann mitten im Abend zu."""
    assert LEASE_TTL_S == 15 * 60
    assert LEASE_HALTUNG == f"{LEASE_TTL_S}s"
    assert LEASE_ERNEUERUNG_S == LEASE_TTL_S / 3 == 5 * 60
    assert LEASE_HALTUNG != "24h"
    assert KNAPPE_HALTUNG != LEASE_HALTUNG


def test_der_pfad_liegt_nicht_im_token_pflichtigen_praefix_des_nachbarn():
    """#306: ``/napi/*`` ist beim Nachbarn Authelia-umgangen und deshalb token-pflichtig.

    Ein token-freier Endpunkt darin schlüge ein Loch in genau das Präfix, über das seine
    App echte Geräte schaltet. Er hat den eigenen Vorschlag darum zurückgezogen.
    """
    assert LEASE_PATH == "/api/model-lease"
    assert not LEASE_PATH.startswith("/napi/")


def test_der_erneuerungstakt_kommt_aus_der_antwort_des_nachbarn(tmp_path):
    """#306: abgeleitet stimmte er nur, solange beide Seiten zufällig dieselbe Zahl halten."""
    http = Fenster(Antwort({LEASE_ERNEUERUNG_FELD: 120}))
    assert fenster_oeffnen(config(tmp_path), http=lambda: http) is True
    assert erneuerung() == 120


@pytest.mark.parametrize(
    "antwort",
    [
        Antwort({}),
        Antwort({LEASE_ERNEUERUNG_FELD: None}),
        Antwort({LEASE_ERNEUERUNG_FELD: "bald"}),
        Antwort({LEASE_ERNEUERUNG_FELD: True}),
        Antwort({LEASE_ERNEUERUNG_FELD: 0}),
        Antwort({LEASE_ERNEUERUNG_FELD: -60}),
        Antwort({LEASE_ERNEUERUNG_FELD: LEASE_TTL_S + 1}),
        Antwort(["kein Rumpf"]),
        Antwort(),
    ],
)
def test_ein_fehlender_oder_unsinniger_takt_faellt_auf_die_eigene_ableitung_zurueck(
    tmp_path, antwort
):
    """Bester Wille in beide Richtungen (#299/#306): das kostet den Takt, nie das Fenster."""
    http = Fenster(antwort)
    assert fenster_oeffnen(config(tmp_path), http=lambda: http) is True
    assert erneuerung() == LEASE_ERNEUERUNG_S


def test_die_anmeldung_laedt_das_modell_gleich_mit(tmp_path):
    """Der offene Punkt aus #299: der erste Szenenschnitt soll den Ladevorgang nicht zahlen.

    Geladen wird erst **nach** der Zusage — ohne sie verdrängten wir den Nachbarn
    ungefragt, und genau das kauft das Fenster ja ab.
    """
    http = Fenster(Antwort({}))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    url, kwargs = http.aufrufe[-1]
    assert url == f"http://ollama.example:11434{CHAT_PATH}"
    assert kwargs["json"]["messages"] == []
    assert kwargs["json"]["keep_alive"] == LEASE_HALTUNG


def test_ein_gescheitertes_fenster_haelt_den_beginn_nicht_auf(tmp_path, caplog):
    """Bester Wille, in beide Richtungen: der Abend beginnt auch ohne Zusage."""
    http = Fenster(fehler=requests.ConnectionError("weg"))
    with caplog.at_level("WARNING"):
        assert fenster_oeffnen(config(tmp_path), http=lambda: http) is False

    assert not lease_offen()
    assert klient(tmp_path, Fenster(Antwort({"message": {"content": "x"}}))).write(
        system="", prompt=""
    )
    gemeldet = " ".join(eintrag.getMessage() for eintrag in caplog.records)
    assert "ConnectionError" in gemeldet
    assert "127.0.0.1" not in gemeldet and MODELL not in gemeldet


@pytest.mark.parametrize(
    ("status", "rumpf"),
    [
        (400, {"ok": False, "reason": "ttl_s out of range"}),
        (409, {"ok": False, "reason": "held", "holder": "coding", "expires_at": 1_000}),
        (503, {"ok": False, "reason": "disabled"}),
        (500, None),
    ],
)
def test_eine_abgelehnte_anmeldung_zaehlt_als_gescheitert(tmp_path, caplog, status, rumpf):
    """400, 409, 503 — jeder Ausgang außer ``ready`` ist derselbe: kein Fenster, kein Fehler.

    Der Grund steht in der einen Logzeile, damit später erklärbar ist, warum eine Chronik
    mit dem Haushaltsmodell geschrieben wurde — sagen tut das ohnehin ihr Kopf (#320).
    """
    http = Fenster(Antwort(rumpf, status_code=status))

    with caplog.at_level("WARNING"):
        assert fenster_oeffnen(config(tmp_path), http=lambda: http) is False

    assert not lease_offen()
    gemeldet = " ".join(eintrag.getMessage() for eintrag in caplog.records)
    assert str(status) in gemeldet
    assert "127.0.0.1" not in gemeldet


def test_ohne_gewaehltes_modell_gibt_es_nichts_anzumelden(tmp_path):
    http = Fenster(Antwort({}))
    assert fenster_oeffnen(Config(data_dir=tmp_path), http=lambda: http) is False
    assert http.aufrufe == []


def test_der_abschalter_laesst_keinen_einzigen_aufruf_hinausgehen(tmp_path):
    """#299: der Vertrag muss ohne Neubau zu verlassen sein — dann auch vollständig."""
    from dataclasses import replace

    aus = replace(config(tmp_path), gpu_lease=False)
    http = Fenster(Antwort({"message": {"content": "Ein ruhiger Abend."}}))

    assert fenster_oeffnen(aus, http=lambda: http) is False
    assert http.aufrufe == []
    assert not lease_offen()

    OllamaClient(aus, http=lambda: http).write(system="Ordne.", prompt="Szene 1")
    assert http.aufrufe[-1][1]["json"]["keep_alive"] == KNAPPE_HALTUNG

    freigeben(aus, http=lambda: http)
    assert http.abmeldungen == []


class Uhr:
    """Eine Uhr, die nur durch Warten vorgeht — sonst liefe die Wartezeit in echt ab."""

    def __init__(self) -> None:
        self.jetzt = 1000.0
        self.pausen: list[float] = []

    def monotonic(self) -> float:
        return self.jetzt

    def warten(self, sekunden: float) -> None:
        self.pausen.append(sekunden)
        self.jetzt += sekunden


class Vorbereitung(Fenster):
    """Ein Nachbar, der erst vorbereitet und auf Nachfrage seinen Zustand nennt."""

    def __init__(self, anmeldung, *staende):
        super().__init__(anmeldung)
        self._staende = list(staende)
        self.fragen = []

    def get(self, url, **kwargs):
        self.fragen.append((url, kwargs))
        return self._staende.pop(0) if len(self._staende) > 1 else self._staende[0]


@pytest.fixture
def uhr(monkeypatch):
    gestellt = Uhr()
    monkeypatch.setattr("chronicle.compose.client.time", gestellt)
    return gestellt


def test_ein_vorbereitetes_fenster_wird_gefragt_und_nicht_neu_angemeldet(tmp_path, uhr):
    """202 ist kein Fehlschlag: der Nachbar lädt, und ein zweites POST setzte ihn zurück.

    Beim ersten Mal dauert das Minuten — ein 12b will heruntergeladen werden. Gefragt wird
    deshalb per GET, frühestens nach der genannten Wartezeit.
    """
    http = Vorbereitung(
        Antwort({"state": "preparing", "retry_after": 30}, status_code=LEASE_VORBEREITUNG),
        Antwort({"state": "preparing", "retry_after": 45}),
        Antwort({"state": "ready", "alias": "gemma-4-12b"}),
    )

    assert fenster_oeffnen(config(tmp_path), http=lambda: http, warten=uhr.warten) is True

    assert len(anmeldungen(http)) == 1
    assert [url for url, _ in http.fragen] == [f"{DEFAULT_SOLARIS_URL}{LEASE_PATH}"] * 2
    assert uhr.pausen == [30, 45]


def test_die_frist_beginnt_erst_mit_ready(tmp_path, uhr):
    """Der Vertrag ist ausdrücklich: gezählt wird ab ``ready``, nicht ab der Anmeldung."""
    http = Vorbereitung(
        Antwort({"state": "preparing", "retry_after": 600}, status_code=LEASE_VORBEREITUNG),
        Antwort({"state": "ready"}),
    )

    fenster_oeffnen(config(tmp_path), http=lambda: http, warten=uhr.warten)
    assert lease_offen()

    uhr.jetzt += LEASE_TTL_S - 1
    assert lease_offen()
    uhr.jetzt += 2
    assert not lease_offen()


def test_ein_nachbar_der_nie_fertig_wird_bindet_den_abend_nicht_ewig(tmp_path, uhr, caplog):
    """Ein Budget, und keine enge Schleife: gefragt wird selten, und irgendwann gar nicht mehr."""
    http = Vorbereitung(
        Antwort({"state": "preparing", "retry_after": 0.001}, status_code=LEASE_VORBEREITUNG),
        Antwort({"state": "preparing", "retry_after": 0.001}),
    )

    with caplog.at_level("WARNING"):
        assert fenster_oeffnen(config(tmp_path), http=lambda: http, warten=uhr.warten) is False

    assert not lease_offen()
    assert min(uhr.pausen) >= LEASE_MINDESTPAUSE_S
    assert sum(uhr.pausen) <= LEASE_VORBEREITUNG_S
    assert len(http.fragen) <= LEASE_VORBEREITUNG_S / LEASE_MINDESTPAUSE_S
    assert "nicht rechtzeitig" in " ".join(e.getMessage() for e in caplog.records)


def test_ein_nachbar_ohne_auskunft_kostet_genau_eine_frage(tmp_path, uhr):
    """Gemessen am 2026-09-05: heute kennt ``/api/model-lease`` nur DELETE und POST.

    Ein GET läuft dort in 405. Das ist derselbe Ausgang wie jede gescheiterte Anmeldung —
    kein Fenster, kein Fehler, und vor allem kein zweiter Versuch gegen eine Wand.
    """
    http = Vorbereitung(
        Antwort({"state": "preparing"}, status_code=LEASE_VORBEREITUNG),
        Antwort(fehler=requests.HTTPError("405")),
    )

    assert fenster_oeffnen(config(tmp_path), http=lambda: http, warten=uhr.warten) is False

    assert len(http.fragen) == 1
    assert uhr.pausen == [LEASE_WARTEZEIT_S]
    assert not lease_offen()


@pytest.mark.parametrize(
    ("genannt", "erwartet"),
    [
        ({}, LEASE_WARTEZEIT_S),
        ({"retry_after": None}, LEASE_WARTEZEIT_S),
        ({"retry_after": "bald"}, LEASE_WARTEZEIT_S),
        ({"retry_after": True}, LEASE_WARTEZEIT_S),
        ({"retry_after": 0}, LEASE_MINDESTPAUSE_S),
        ({"retry_after": -5}, LEASE_MINDESTPAUSE_S),
        ({"retry_after": 90}, 90),
        ({"retry_after": LEASE_VORBEREITUNG_S * 10}, LEASE_VORBEREITUNG_S),
    ],
)
def test_die_wartezeit_kommt_vom_nachbarn_und_bleibt_in_schranken(tmp_path, uhr, genannt, erwartet):
    http = Vorbereitung(
        Antwort(dict(genannt, state="preparing"), status_code=LEASE_VORBEREITUNG),
        Antwort({"state": "ready"}),
    )

    fenster_oeffnen(config(tmp_path), http=lambda: http, warten=uhr.warten)

    assert uhr.pausen[0] == erwartet


def test_das_ende_meldet_das_fenster_genau_einmal_ab(tmp_path):
    """Der Fensterschluss hängt an der Freigabe — und die gibt es nur einmal je Aufschrieb."""
    http = Fenster(Antwort({}))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    freigeben(config(tmp_path), http=lambda: http)
    ((url, kwargs),) = http.abmeldungen
    assert url == f"{DEFAULT_SOLARIS_URL}{LEASE_PATH}"
    assert set(kwargs) == {"timeout"}
    assert not lease_offen()

    freigeben(config(tmp_path), http=lambda: http)
    assert len(http.abmeldungen) == 1


def test_ohne_offenes_fenster_wird_nichts_abgemeldet(tmp_path):
    http = Fenster(Antwort({}))
    freigeben(config(tmp_path), http=lambda: http)
    assert http.abmeldungen == []


def test_ein_gescheitertes_abmelden_haelt_den_abschluss_nicht_auf(tmp_path, caplog):
    """Die andere Richtung: ein Abend darf auch am Abmelden nicht scheitern."""
    http = Fenster(Antwort({}), delete_fehler=requests.ConnectionError("weg"))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    with caplog.at_level("WARNING"):
        assert freigeben(config(tmp_path), http=lambda: http) is True

    assert not lease_offen()
    gemeldet = " ".join(eintrag.getMessage() for eintrag in caplog.records)
    assert "127.0.0.1" not in gemeldet


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
# Der zweite Weg: ``llama-server`` in der OpenAI-Form (#316). Ollama bleibt die Vorgabe;
# beide Wege stehen nebeneinander, weil die Box heute noch den ersten fährt.
# --------------------------------------------------------------------------------------


def openai_config(tmp_path, **kwargs):
    return replace(config(tmp_path, **kwargs), llm_backend=BACKEND_OPENAI)


def openai_klient(tmp_path, http, **kwargs):
    return OpenAIClient(openai_config(tmp_path, **kwargs), http=lambda: http)


def v1_antwort(text="Ein ruhiger Abend."):
    return Antwort({"choices": [{"message": {"content": text}}]})


def test_der_v1_weg_ruft_den_pfad_des_abloesers_und_traegt_keine_frist(tmp_path):
    """``keep_alive`` ist Ollamas Feld — der Ablöser hat keines, und wir erfinden keines."""
    http = Http(v1_antwort("  Ein ruhiger Abend.  "))
    text = openai_klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")

    url, kwargs = http.aufrufe[0]
    assert OPENAI_CHAT_PATH == "/v1/chat/completions"
    assert url == f"http://ollama.example:11434{OPENAI_CHAT_PATH}"
    assert kwargs["json"]["model"] == MODELL
    assert kwargs["json"]["stream"] is False
    assert kwargs["json"]["messages"] == [
        {"role": "system", "content": "Ordne."},
        {"role": "user", "content": "Szene 1"},
    ]
    assert "keep_alive" not in kwargs["json"]
    assert kwargs["timeout"] > 0
    assert text == "Ein ruhiger Abend."


def test_ohne_eigene_adresse_redet_auch_der_v1_klient_mit_dieser_box(tmp_path):
    http = Http(v1_antwort())
    openai_klient(tmp_path, http, url=None).write(system="Ordne.", prompt="Szene 1")
    assert http.aufrufe[0][0] == f"{DEFAULT_OLLAMA_URL}{OPENAI_CHAT_PATH}"


def test_ohne_ansage_antwortet_weiterhin_ollama(tmp_path):
    """Die Naht ist ``from_config`` und sonst nichts — und ihre Vorgabe ist der alte Weg.

    Ein Umbau, der nur noch den neuen Weg spräche, legte den Dienst still, bevor die
    Plattform überhaupt umzieht.
    """
    assert Config().llm_backend == BACKEND_OLLAMA
    assert isinstance(from_config(config(tmp_path)), OllamaClient)
    assert isinstance(from_config(openai_config(tmp_path)), OpenAIClient)
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
def test_eine_unbrauchbare_v1_antwort_ist_dieselbe_verstaendliche_meldung(tmp_path, antwort):
    with pytest.raises(ModelUnreachable):
        openai_klient(tmp_path, Http(antwort)).write(system="", prompt="")


def test_auf_dem_v1_weg_gilt_das_fenster_und_schweigt_nur_die_haltung(tmp_path, caplog):
    """#321: der Leerlauf von #316 hieße heute »wir bekommen still das Haushaltsmodell«.

    Der Vertrag steht jetzt, also wird angemeldet und abgemeldet — nur eine Haltung gibt es
    auf diesem Weg nicht, und das bleibt ein ausgesprochener No-op statt eines stillen
    Nichts.
    """
    http = Fenster(Antwort({}))
    aufbau = openai_config(tmp_path)

    with caplog.at_level("INFO"):
        assert fenster_oeffnen(aufbau, http=lambda: http) is True
        assert freigeben(aufbau, http=lambda: http) is True

    # Genau ein Aufruf: die Anmeldung. Kein Aufwärmen, kein ``keep_alive``, kein Entladen.
    ((url, kwargs),) = http.aufrufe
    assert url == f"{DEFAULT_SOLARIS_URL}{LEASE_PATH}"
    assert kwargs["json"] == {"model": LEASE_PROFIL, "ttl_s": LEASE_TTL_S}
    assert len(http.abmeldungen) == 1
    assert not lease_offen()
    gemeldet = " ".join(eintrag.getMessage() for eintrag in caplog.records)
    assert BACKEND_OPENAI in gemeldet
    assert "127.0.0.1" not in gemeldet and MODELL not in gemeldet


def test_das_profil_benennt_die_arbeit_und_nicht_die_runde(tmp_path):
    """Die Zusage aus #299 überlebt den neuen Vertrag: der Nachbar erfährt nicht, wer spielt."""
    http = Fenster(Antwort({}))
    fenster_oeffnen(openai_config(tmp_path), http=lambda: http)

    ((_, kwargs),) = anmeldungen(http)
    assert LEASE_PROFIL == "foundry"
    assert kwargs["json"]["model"] == LEASE_PROFIL
    assert MODELL not in str(kwargs["json"])
    assert set(kwargs["json"]) == {"model", "ttl_s"}
    assert set(kwargs) == {"json", "timeout"}


def test_auf_dem_alten_weg_bleibt_der_modellname_stehen(tmp_path):
    """Bis ``mdopp/solarisbay#1333`` wirkt die Anmeldung dort auf Ollama — und ein Profil
    wäre für Ollama ein Modell, das es nicht kennt."""
    http = Fenster(Antwort({}))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    ((_, kwargs),) = anmeldungen(http)
    assert kwargs["json"]["model"] == MODELL


@pytest.mark.parametrize(
    ("bauart", "openai", "antwort"),
    [
        (OllamaClient, False, Antwort({"message": {"content": "Ein ruhiger Abend."}})),
        (OpenAIClient, True, v1_antwort()),
    ],
)
def test_der_zwischenstand_behaelt_auf_beiden_wegen_seine_kuerzere_frist(
    tmp_path, bauart, openai, antwort
):
    """#302: die eigene Grenze des Zwischenstands hängt am Klienten, nicht am Backend."""
    assert ZWISCHENSTAND_TIMEOUT < DEFAULT_TIMEOUT
    http = Http(antwort)
    aufbau = openai_config(tmp_path) if openai else config(tmp_path)
    bauart(aufbau, http=lambda: http, timeout=ZWISCHENSTAND_TIMEOUT).write(system="", prompt="")
    assert http.aufrufe[-1][1]["timeout"] == ZWISCHENSTAND_TIMEOUT


def material():
    return SessionMaterial(
        session_id=1,
        played_on="2026-09-05",
        title="Der Keller",
        scenes=(SceneMaterial(position=1, title="Aufbruch", notes=("Erste Notiz.",), facts=()),),
    )


def test_ein_scheiterndes_v1_modell_liefert_geordnet_statt_erzaehlt(tmp_path):
    """Der Fehler bleibt im Klienten: keine Ausnahme schlägt bis zur Komposition durch."""
    http = Http(Antwort(fehler=requests.HTTPError("404")))
    ergebnis = compose(material(), openai_klient(tmp_path, http), inhaltssprache=sprachen.DEUTSCH)
    assert "Erste Notiz." in ergebnis.text
    assert ergebnis.prose_count == 0


def test_die_zahlensperre_greift_auch_auf_dem_v1_weg(tmp_path):
    """Mechanisch und vor der Ausgabe — der Weg des Aufrufs ändert daran nichts."""
    http = Http(v1_antwort("Es waren 42 Ratten."))
    ergebnis = compose(material(), openai_klient(tmp_path, http), inhaltssprache=sprachen.DEUTSCH)
    assert "42" not in ergebnis.text
