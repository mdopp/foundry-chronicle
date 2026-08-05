from conftest import GM_FIGUR, UNSER_KONTO

import chronicle.__main__ as entry
from chronicle import db, notes
from chronicle.app import create_app
from chronicle.compose.service import compose_session
from chronicle.config import Config
from chronicle.foundry import service
from chronicle.foundry.client import FoundryUnreachable

PASSWORT = "passwort-taucht-nirgends-auf"
BOT_TOKEN = "bot-token-taucht-nirgends-auf"


def seite(config):
    return create_app(config).test_client().get("/status")


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


def test_ohne_sprachmodell_sagt_der_status_was_die_chronik_dann_wird(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "geordnet statt formuliert" in html
    for name in ("OLLAMA_URL", "OLLAMA_MODEL"):
        assert name in html


def test_mit_sprachmodell_zeigt_der_status_adresse_und_modell(tmp_path):
    config = Config(
        ollama_url="http://ollama.example:11434",
        ollama_model="chronist-modell",
        data_dir=tmp_path,
    )
    html = seite(config).get_data(as_text=True)
    assert "http://ollama.example:11434" in html
    assert "chronist-modell" in html


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


def bewacht(tmp_path):
    return create_app(Config(data_dir=tmp_path, require_remote_user=True)).test_client()


def test_ohne_remote_user_kommt_niemand_durch(tmp_path):
    antwort = bewacht(tmp_path).get("/")
    assert antwort.status_code == 403
    assert "Kein Zugang" in antwort.get_data(as_text=True)


def test_auch_kein_lan_bypass_auf_die_unterseiten(tmp_path):
    client = bewacht(tmp_path)
    assert client.get("/status").status_code == 403
    assert client.post("/", data={}).status_code == 403
    assert client.post("/szenen/1/notizen", data={"text": "x"}).status_code == 403


def test_mit_remote_user_geht_es_weiter(tmp_path):
    antwort = bewacht(tmp_path).get("/", headers={"Remote-User": "mira"})
    assert antwort.status_code == 200


def test_healthz_bleibt_am_proxy_vorbei_erreichbar(tmp_path):
    assert bewacht(tmp_path).get("/healthz").status_code == 200


def test_ohne_erzwingung_laeuft_es_lokal_weiter(tmp_path):
    assert create_app(Config(data_dir=tmp_path)).test_client().get("/").status_code == 200


def test_status_erklaert_die_ungesicherte_lage(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "Header-Prüfung ist aus" in html
    assert "CHRONICLE_REQUIRE_REMOTE_USER" in html


def test_status_nennt_den_angemeldeten_menschen(tmp_path):
    antwort = bewacht(tmp_path).get("/status", headers={"Remote-User": "mira"})
    html = antwort.get_data(as_text=True)
    assert "mira" in html
    assert "Ein eigenes Login gibt es nicht" in html


class Chronist:
    name = "chronist-test"

    def write(self, *, system, prompt):
        return "Die Runde tastet sich voran."


def eine_sitzung(tmp_path, *, chronik=False):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    sitzung_id = notes.create_session(
        config.database_path, played_on="2026-08-05", title="Der Keller"
    )
    szene = notes.session(config.database_path, sitzung_id).scenes[0]
    notes.add_note(config.database_path, szene.id, "Wir brechen bei Sonnenaufgang auf.")
    if chronik:
        compose_session(config, sitzung_id, model=Chronist())
    return config, sitzung_id


def gelesen(config, pfad):
    return create_app(config).test_client().get(pfad).get_data(as_text=True)


def test_ohne_sitzung_sagt_die_protokollliste_warum_da_nichts_steht(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/protokolle")
    assert "Noch keine Sitzung" in html


def test_die_protokollliste_zeigt_auch_sitzungen_ohne_chronik(tmp_path):
    config, _ = eine_sitzung(tmp_path)
    html = gelesen(config, "/protokolle")
    assert "Der Keller" in html
    assert "Noch keine Chronik" in html


def test_ohne_lauf_erklaert_die_ansicht_wie_die_chronik_entsteht(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    html = gelesen(config, f"/sitzungen/{sitzung_id}/protokoll")
    assert "noch nicht gelaufen" in html
    assert f"python -m chronicle.compose {sitzung_id}" in html


def test_die_chronik_wird_serverseitig_gerendert(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path, chronik=True)
    html = gelesen(config, f"/sitzungen/{sitzung_id}/protokoll")
    assert "<h2>Chronik — Sitzung vom 2026-08-05: Der Keller</h2>" in html
    assert "Wir brechen bei Sonnenaufgang auf." in html
    assert "Die Runde tastet sich voran." in html


def test_die_ansicht_haelt_belegtes_und_verbindungstext_auseinander(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path, chronik=True)
    html = gelesen(config, f"/sitzungen/{sitzung_id}/protokoll")
    assert '<section class="abschnitt notizen">' in html
    assert '<section class="abschnitt verbindung">' in html


def test_die_protokollliste_verweist_auf_die_chronik(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path, chronik=True)
    assert f"/sitzungen/{sitzung_id}/protokoll" in gelesen(config, "/protokolle")
    assert f"/sitzungen/{sitzung_id}/protokoll" in gelesen(config, f"/sitzungen/{sitzung_id}")


def test_eine_unbekannte_sitzung_hat_keine_protokollseite(tmp_path):
    config = Config(data_dir=tmp_path)
    antwort = create_app(config).test_client().get("/sitzungen/999/protokoll")
    assert antwort.status_code == 404


def test_die_protokollansicht_steht_hinter_demselben_tuersteher(tmp_path):
    client = bewacht(tmp_path)
    assert client.get("/protokolle").status_code == 403
    assert client.get("/sitzungen/1/protokoll").status_code == 403


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
