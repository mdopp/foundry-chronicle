"""Der Ollama-Aufruf — ohne Netz, gegen eine nachgebaute HTTP-Sitzung."""

import pytest
import requests

from chronicle.compose import client as modell
from chronicle.compose.client import (
    CHAT_PATH,
    FREIGABE,
    SITZUNGSHALTUNG,
    TAGS_PATH,
    ModelNotConfigured,
    ModelUnreachable,
    OllamaClient,
    freigeben,
    from_config,
    halten,
    haltung,
    installed_models,
)
from chronicle.config import DEFAULT_OLLAMA_URL, Config

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


@pytest.fixture(autouse=True)
def ohne_haltung(monkeypatch):
    """Die Haltung ist Prozesszustand — kein Test erbt die eines anderen."""
    monkeypatch.setattr(modell, "_haltung", None)


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


def test_ohne_laufende_sitzung_bestimmt_ollama_selbst_wie_lange_es_haelt(tmp_path):
    http = Http(Antwort({"message": {"content": "Ein ruhiger Abend."}}))
    klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")
    assert "keep_alive" not in http.aufrufe[0][1]["json"]
    assert haltung() is None


def test_der_start_haelt_das_modell_und_jeder_aufruf_erneuert_die_haltung(tmp_path):
    """Die Frist hängt am Aufruf, nicht am Startbefehl — sonst liefe sie mitten im Abend ab."""
    http = Http(Antwort({"message": {"content": "Ein ruhiger Abend."}}))

    assert halten(config(tmp_path), http=lambda: http) is True

    url, kwargs = http.aufrufe[0]
    assert url == f"http://ollama.example:11434{CHAT_PATH}"
    assert kwargs["json"]["model"] == MODELL
    # Kein Wort schreiben lassen: der Aufruf lädt nur und setzt die Frist.
    assert kwargs["json"]["messages"] == []
    assert kwargs["json"]["keep_alive"] == SITZUNGSHALTUNG
    assert haltung() == SITZUNGSHALTUNG

    klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")
    assert http.aufrufe[1][1]["json"]["keep_alive"] == SITZUNGSHALTUNG


def test_der_abschluss_gibt_das_modell_mit_null_wieder_frei(tmp_path):
    http = Http(Antwort({"message": {"content": "Ein ruhiger Abend."}}))
    halten(config(tmp_path), http=lambda: http)

    assert freigeben(config(tmp_path), http=lambda: http) is True

    assert http.aufrufe[-1][1]["json"]["keep_alive"] == FREIGABE
    assert FREIGABE == 0
    assert haltung() is None
    klient(tmp_path, http).write(system="Ordne.", prompt="Szene 1")
    assert "keep_alive" not in http.aufrufe[-1][1]["json"]


def test_eine_sitzung_die_nicht_ordentlich_endet_laeuft_von_selbst_aus():
    """Die offene Frage aus #295: eine Sperre, die niemand löst, darf keine Sperre sein.

    Ollama hielte bei einem negativen Wert bis zum nächsten Neustart — und acht Gigabyte
    festzuhalten sperrte die Nachbardienste dieser Karte aus. Also eine endliche Frist,
    die jeder Aufruf erneuert: stirbt der Prozess, hört das Erneuern auf, und die Haltung
    läuft von selbst aus.
    """
    zahl, einheit = SITZUNGSHALTUNG[:-1], SITZUNGSHALTUNG[-1]
    assert einheit in {"m", "h"}
    stunden = float(zahl) / 60 if einheit == "m" else float(zahl)
    assert 0 < stunden <= 4


def test_ein_abgeschaltetes_ollama_haelt_den_abend_nicht_auf(tmp_path):
    http = Http(fehler=requests.ConnectionError("weg"))
    assert halten(config(tmp_path), http=lambda: http) is False
    # Die Haltung steht trotzdem: der nächste Aufruf, der durchkommt, setzt sie mit.
    assert haltung() == SITZUNGSHALTUNG
    assert freigeben(config(tmp_path), http=lambda: http) is False
    assert haltung() is None


def test_ein_fehlerstatus_beim_halten_zaehlt_ebenfalls_als_gescheitert(tmp_path):
    http = Http(Antwort(fehler=requests.HTTPError("500")))
    assert halten(config(tmp_path), http=lambda: http) is False


def test_ohne_gewaehltes_modell_gibt_es_nichts_zu_halten(tmp_path):
    http = Http()
    assert halten(Config(data_dir=tmp_path), http=lambda: http) is False
    assert freigeben(Config(data_dir=tmp_path), http=lambda: http) is False
    assert http.aufrufe == []


def test_ohne_eigene_adresse_geht_die_haltung_an_das_ollama_dieser_box(tmp_path):
    http = Http(Antwort({}))
    halten(config(tmp_path, url=None), http=lambda: http)
    assert http.aufrufe[0][0] == f"{DEFAULT_OLLAMA_URL}{CHAT_PATH}"
