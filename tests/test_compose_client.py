"""Der Ollama-Aufruf — ohne Netz, gegen eine nachgebaute HTTP-Sitzung."""

import pathlib

import pytest
import requests

from chronicle.compose.client import (
    CHAT_PATH,
    DEFAULT_TIMEOUT,
    FREIGABE,
    KNAPPE_HALTUNG,
    LEASE_ERNEUERUNG_FELD,
    LEASE_ERNEUERUNG_S,
    LEASE_HALTUNG,
    LEASE_PATH,
    LEASE_TTL_S,
    TAGS_PATH,
    ZWISCHENSTAND_TIMEOUT,
    ModelNotConfigured,
    ModelUnreachable,
    OllamaClient,
    erneuerung,
    fenster_oeffnen,
    freigeben,
    from_config,
    installed_models,
    lease_offen,
)
from chronicle.config import DEFAULT_OLLAMA_URL, DEFAULT_SOLARIS_URL, Config

ADRESSE = "http://ollama.example:11434/"
MODELL = "chronist-modell"


class Antwort:
    def __init__(self, payload=None, fehler=None):
        self._payload = payload
        self._fehler = fehler

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


def test_der_name_ist_das_konfigurierte_modell(tmp_path):
    assert klient(tmp_path, Http()).name == MODELL


def test_from_config_liefert_ohne_konfiguration_kein_modell(tmp_path):
    assert from_config(Config(data_dir=tmp_path)) is None
    assert from_config(config(tmp_path)).name == MODELL


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


def test_ein_fehlerstatus_beim_fenster_zaehlt_ebenfalls_als_gescheitert(tmp_path):
    http = Fenster(Antwort(fehler=requests.HTTPError("500")))
    assert fenster_oeffnen(config(tmp_path), http=lambda: http) is False
    assert not lease_offen()


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
