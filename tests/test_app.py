from conftest import GM_FIGUR, UNSER_KONTO

import chronicle.__main__ as entry
from chronicle import db
from chronicle.app import create_app
from chronicle.config import Config
from chronicle.foundry import service
from chronicle.foundry.client import FoundryUnreachable

PASSWORT = "passwort-taucht-nirgends-auf"
BOT_TOKEN = "bot-token-taucht-nirgends-auf"


def seite(config):
    return create_app(config).test_client().get("/")


def test_startet_ohne_foundry_und_erklaert_was_fehlt(tmp_path):
    antwort = seite(Config(data_dir=tmp_path))
    assert antwort.status_code == 200
    html = antwort.get_data(as_text=True)
    assert "Nicht konfiguriert" in html
    for name in ("FOUNDRY_URL", "FOUNDRY_USER", "FOUNDRY_PASSWORD"):
        assert name in html


def test_ohne_discord_ist_das_kein_fehler(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "Kein Bot-Token" in html


def test_konfiguriert_zeigt_url_und_benutzer_aber_kein_geheimnis(tmp_path):
    config = Config(
        foundry_url="https://foundry.example",
        foundry_user="chronist",
        foundry_password=PASSWORT,
        discord_bot_token=BOT_TOKEN,
        data_dir=tmp_path,
    )
    html = seite(config).get_data(as_text=True)
    assert "https://foundry.example" in html
    assert "chronist" in html
    assert PASSWORT not in html
    assert BOT_TOKEN not in html


def test_healthz_meldet_ok(tmp_path):
    antwort = create_app(Config(data_dir=tmp_path)).test_client().get("/healthz")
    assert antwort.status_code == 200
    assert antwort.get_json() == {"status": "ok"}


def test_legt_die_datenbank_beim_start_an(tmp_path):
    config = Config(data_dir=tmp_path / "noch-nicht-da")
    create_app(config)
    assert config.database_path.is_file()


def test_zeigt_den_schema_stand(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert f"<dd>{db.SCHEMA_VERSION}</dd>" in html


class Abgleich:
    def __init__(self, welt=None, fehler=None):
        self._welt = welt
        self._fehler = fehler

    def fetch_world(self):
        if self._fehler is not None:
            raise self._fehler
        return UNSER_KONTO, self._welt


def test_ohne_abgleich_steht_da_warum_nichts_zu_sehen_ist(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "Noch kein Abgleich" in html


def test_zeigt_den_umfang_des_letzten_abgleichs(config, welt):
    service.sync(config, client=Abgleich(welt))
    html = seite(config).get_data(as_text=True)
    assert "Stand vom" in html
    assert "daggerheart" in html
    assert GM_FIGUR not in html


def test_bei_ausgefallenem_foundry_erklaert_die_seite_den_alten_stand(config, welt):
    service.sync(config, client=Abgleich(welt))
    service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    html = seite(config).get_data(as_text=True)
    assert "nicht erreichbar" in html
    assert "Angezeigt wird der Stand" in html


def test_main_liest_host_und_port_aus_der_umgebung(monkeypatch):
    aufruf = {}

    class Attrappe:
        def run(self, **kwargs):
            aufruf.update(kwargs)

    monkeypatch.setattr(entry, "create_app", lambda: Attrappe())
    monkeypatch.setenv("CHRONICLE_HOST", "127.0.0.2")
    monkeypatch.setenv("CHRONICLE_PORT", "9001")
    entry.main()
    assert aufruf == {"host": "127.0.0.2", "port": 9001}


def test_main_hat_vorgaben(monkeypatch):
    aufruf = {}

    class Attrappe:
        def run(self, **kwargs):
            aufruf.update(kwargs)

    monkeypatch.setattr(entry, "create_app", lambda: Attrappe())
    monkeypatch.delenv("CHRONICLE_HOST", raising=False)
    monkeypatch.delenv("CHRONICLE_PORT", raising=False)
    entry.main()
    assert aufruf == {"host": "127.0.0.1", "port": 8000}
