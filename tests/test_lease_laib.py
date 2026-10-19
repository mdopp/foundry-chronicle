"""Das Sitzungsfenster bei Laibs Lease (mdopp/laib#422, ADR 0011 dort).

Seit dem Umzug der Box auf Laib (2026-10-19) hält ``laib-lease`` die Karte, auf
demselben Port wie das Modell (11435). Solaris' Makler auf 8787 regiert nichts mehr:
die Anmeldung dort öffnete kein Fenster, und die erste Frage nach ``gemma-4-12b`` ließ
``laib-lease`` sein weitestes Fenster nehmen (Sprache auf die CPU, Einbettungen aus).

Angemeldet wird jetzt dort, wo das Modell antwortet, **mit dem Modell als Preset**: die
Lease nimmt das engste Fenster, das es erlaubt (``shared``), Sprache bleibt auf der
Karte. Anmelden und Abmelden tragen den Modellschlüssel als Bearer (``MODEL_KEY``, Laibs
Name dafür); die Adresse kommt aus ``MODEL_LEASE``, sonst aus der des Modells.
"""

from chronicle.compose.client import (
    LEASE_HALTER,
    LEASE_HALTER_FELD,
    LEASE_TTL_S,
    fenster_oeffnen,
    freigeben,
)
from chronicle.config import Config


class Antwort:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class Lease:
    def __init__(self):
        self.aufrufe = []

    def post(self, url, **kw):
        self.aufrufe.append(("POST", url, kw))
        return Antwort({"state": "ready", "renew_after": 300})

    def get(self, url, **kw):
        self.aufrufe.append(("GET", url, kw))
        return Antwort({"state": "ready"})

    def delete(self, url, **kw):
        self.aufrufe.append(("DELETE", url, kw))
        return Antwort({})


def test_das_fenster_geht_an_laibs_lease_mit_dem_modell_und_dem_schluessel(tmp_path):
    cfg = Config.from_env(
        {
            "OLLAMA_URL": "http://127.0.0.1:11435",
            "OLLAMA_MODEL": "gemma-4-12b",
            "MODEL_KEY": "k-synthetic",
            "CHRONICLE_DATA_DIR": str(tmp_path),
        }
    )
    http = Lease()
    assert fenster_oeffnen(cfg, http=lambda: http) is True
    methode, url, kw = http.aufrufe[0]
    assert (methode, url) == ("POST", "http://127.0.0.1:11435/api/model-lease")
    assert kw["json"] == {
        "model": "gemma-4-12b",
        "ttl_s": LEASE_TTL_S,
        LEASE_HALTER_FELD: LEASE_HALTER,
    }
    assert kw["headers"] == {"Authorization": "Bearer k-synthetic"}
    assert freigeben(cfg, http=lambda: http) is True
    methode, url, kw = http.aufrufe[-1]
    assert (methode, url) == ("DELETE", "http://127.0.0.1:11435/api/model-lease")
    assert kw["json"] == {LEASE_HALTER_FELD: LEASE_HALTER}
    assert kw["headers"] == {"Authorization": "Bearer k-synthetic"}


def test_model_lease_nennt_die_adresse_und_der_schluessel_bleibt_verdeckt(tmp_path):
    cfg = Config.from_env(
        {
            "OLLAMA_MODEL": "gemma-4-12b",
            "MODEL_KEY": "k-synthetic",
            "MODEL_LEASE": "http://host.containers.internal:11435/api/model-lease",
        }
    )
    http = Lease()
    fenster_oeffnen(cfg, http=lambda: http)
    assert http.aufrufe[0][1] == "http://host.containers.internal:11435/api/model-lease"
    assert "k-synthetic" not in repr(cfg)
