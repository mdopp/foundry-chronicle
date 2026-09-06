"""Der Aufruf des Modelldienstes — ohne Netz, gegen eine nachgebaute HTTP-Sitzung."""

import pathlib

import pytest
import requests

from chronicle import sprache as sprachen
from chronicle.compose.client import (
    DEFAULT_TIMEOUT,
    LEASE_ERNEUERUNG_FELD,
    LEASE_ERNEUERUNG_S,
    LEASE_MINDESTPAUSE_S,
    LEASE_PATH,
    LEASE_PROFIL,
    LEASE_TTL_S,
    LEASE_VORBEREITUNG,
    LEASE_VORBEREITUNG_S,
    LEASE_WARTEZEIT_S,
    OPENAI_CHAT_PATH,
    ZWISCHENSTAND_TIMEOUT,
    ModelNotConfigured,
    ModelUnreachable,
    OpenAIClient,
    erneuerung,
    fenster_oeffnen,
    freigeben,
    from_config,
    lease_offen,
)
from chronicle.compose.composer import SceneMaterial, SessionMaterial, compose
from chronicle.config import DEFAULT_OLLAMA_URL, DEFAULT_SOLARIS_URL, Config

ADRESSE = "http://modell.example:11435/"
BASIS = ADRESSE.rstrip("/")
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
    """Der Aufbau, den der Dienst seit #329 kennt — es gibt nur noch einen Weg.

    Bis dahin stand hier ausdrücklich ``llm_backend``, damit ein auslaufender Weg in seinen
    Tests benannt war und bei seiner Entfernung genau das wegfiel, was ihn geprüft hatte.
    Genau das ist eingetreten; der Schalter ist mit ihm gefallen.
    """
    return Config(ollama_url=url, ollama_model=model, data_dir=tmp_path)


def klient(tmp_path, http, **kwargs):
    return OpenAIClient(config(tmp_path, **kwargs), http=lambda: http)


def v1_antwort(text="Ein ruhiger Abend."):
    return Antwort({"choices": [{"message": {"content": text}}]})


def test_ruft_den_pfad_des_modelldienstes_und_traegt_keine_frist(tmp_path):
    """``keep_alive`` war Ollamas Feld — der Ablöser hat keines, und wir erfinden keines."""
    http = Http(v1_antwort("  Ein ruhiger Abend.  "))
    text = klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")

    url, kwargs = http.aufrufe[0]
    assert OPENAI_CHAT_PATH == "/v1/chat/completions"
    assert url == f"{BASIS}{OPENAI_CHAT_PATH}"
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


def test_ohne_eigene_adresse_redet_der_klient_mit_dem_dienst_dieser_box(tmp_path):
    http = Http(v1_antwort())
    klient(tmp_path, http, url=None).write(system="Ordne.", prompt="Szene 1")
    assert http.aufrufe[0][0] == f"{DEFAULT_OLLAMA_URL}{OPENAI_CHAT_PATH}"


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


def test_der_beginn_meldet_das_fenster_mit_profil_und_frist_an(tmp_path):
    """Genau ein POST, und die Nutzlast sagt nur, *was* geladen wird und *wie lange*.

    Wer spielt, gehört nicht hinein: der Nachbar entscheidet daran nichts, und eine
    Runden-, Gilden- oder Sitzungskennung wäre eine Preisgabe ohne Gegenwert.
    """
    http = Fenster(Antwort({}))
    assert fenster_oeffnen(config(tmp_path), http=lambda: http) is True

    ((url, kwargs),) = anmeldungen(http)
    assert url == f"{DEFAULT_SOLARIS_URL}{LEASE_PATH}"
    assert kwargs["json"] == {"model": LEASE_PROFIL, "ttl_s": LEASE_TTL_S}
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


def test_das_profil_benennt_die_arbeit_und_nicht_die_runde(tmp_path):
    """Die Zusage aus #299 überlebt den neuen Vertrag: der Nachbar erfährt nicht, wer spielt.

    Ein **Profil**, kein Modellname: ``llama-server`` ignoriert den Namen der Anfrage, und
    der Nachbar schaltet am Profil, welches Modell er geladen hält. Bis #329 stand am
    Ollama-Weg daneben der Modellname, weil ein Profil dort ein unbekanntes Modell gewesen
    wäre; mit dem Weg ist auch diese Verzweigung gefallen.
    """
    http = Fenster(Antwort({}))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    ((_, kwargs),) = anmeldungen(http)
    assert LEASE_PROFIL == "foundry"
    assert kwargs["json"]["model"] == LEASE_PROFIL
    assert MODELL not in str(kwargs["json"])


def test_die_frist_des_fensters_und_ihr_erneuerungstakt_haengen_zusammen():
    """Zwei Zahlen liefen auseinander; das Fenster fiele dann mitten im Abend zu.

    Bis #329 war es *eine Zahl mit zwei Verwendungen*: dieselbe Frist ging als
    ``keep_alive`` an Ollama und als ``ttl_s`` an den Nachbarn. Der Ablöser kennt kein
    ``keep_alive``; geblieben ist die Verwendung, die den Vertrag trägt.
    """
    assert LEASE_TTL_S == 15 * 60
    assert LEASE_ERNEUERUNG_S == LEASE_TTL_S / 3 == 5 * 60


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


def test_die_anmeldung_ist_der_einzige_aufruf_des_beginns(tmp_path):
    """Bis #329 lud sie das Modell gleich mit — ein ``keep_alive``-Aufruf an Ollama (#299).

    Der Aufwärm-Aufruf hing an Ollamas Haltung und ist mit ihr gefallen; ``llama-server``
    lädt auf die Anmeldung hin selbst, und was er geladen hält, entscheidet das Profil.
    """
    http = Fenster(Antwort({}))
    assert fenster_oeffnen(config(tmp_path), http=lambda: http) is True

    assert [url for url, _ in http.aufrufe] == [f"{DEFAULT_SOLARIS_URL}{LEASE_PATH}"]


def test_ein_gescheitertes_fenster_haelt_den_beginn_nicht_auf(tmp_path, caplog):
    """Bester Wille, in beide Richtungen: der Abend beginnt auch ohne Zusage."""
    http = Fenster(fehler=requests.ConnectionError("weg"))
    with caplog.at_level("WARNING"):
        assert fenster_oeffnen(config(tmp_path), http=lambda: http) is False

    assert not lease_offen()
    assert klient(tmp_path, Fenster(v1_antwort("x"))).write(system="", prompt="")
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
    http = Fenster(v1_antwort())

    assert fenster_oeffnen(aus, http=lambda: http) is False
    assert http.aufrufe == []
    assert not lease_offen()

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
    """Der Fensterschluss hängt an der Freigabe — und die gibt es nur einmal je Aufschrieb.

    Bis #329 gab dieselbe Stelle davor Ollamas Haltung mit ``keep_alive: 0`` frei; der
    Ablöser hat keine, die zu beenden wäre. Geblieben ist das Abmelden — und **nur** es:
    kein Entladen, kein zweiter Aufruf.
    """
    http = Fenster(Antwort({}))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    assert freigeben(config(tmp_path), http=lambda: http) is True
    ((url, kwargs),) = http.abmeldungen
    assert url == f"{DEFAULT_SOLARIS_URL}{LEASE_PATH}"
    assert set(kwargs) == {"timeout"}
    assert not lease_offen()
    assert len(anmeldungen(http)) == len(http.aufrufe) == 1

    assert freigeben(config(tmp_path), http=lambda: http) is False
    assert len(http.abmeldungen) == 1


def test_ohne_offenes_fenster_wird_nichts_abgemeldet(tmp_path):
    http = Fenster(Antwort({}))
    assert freigeben(config(tmp_path), http=lambda: http) is False
    assert http.abmeldungen == []
    assert http.aufrufe == []


def test_ein_gescheitertes_abmelden_haelt_den_abschluss_nicht_auf(tmp_path, caplog):
    """Die andere Richtung: ein Abend darf auch am Abmelden nicht scheitern."""
    http = Fenster(Antwort({}), delete_fehler=requests.ConnectionError("weg"))
    fenster_oeffnen(config(tmp_path), http=lambda: http)

    with caplog.at_level("WARNING"):
        assert freigeben(config(tmp_path), http=lambda: http) is False

    assert not lease_offen()
    gemeldet = " ".join(eintrag.getMessage() for eintrag in caplog.records)
    assert "ConnectionError" in gemeldet
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
