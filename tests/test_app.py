import pytest
from conftest import GM_FIGUR, UNSER_KONTO

import chronicle.__main__ as entry
from chronicle import db, notes, settings
from chronicle.app import create_app
from chronicle.compose import client as sprachmodell
from chronicle.compose.client import ModelUnreachable
from chronicle.compose.service import compose_session, recap_session
from chronicle.config import Config
from chronicle.foundry import service
from chronicle.foundry.client import FoundryUnreachable

PASSWORT = "passwort-taucht-nirgends-auf"
BOT_TOKEN = "bot-token-taucht-nirgends-auf"


@pytest.fixture(autouse=True)
def kein_ollama_im_netz(monkeypatch):
    """Kein Test darf an einem echten Ollama hängen bleiben."""

    def weg(adresse, **kwargs):
        raise ModelUnreachable(f"{adresse}/api/tags nicht erreichbar: ConnectionError")

    monkeypatch.setattr(sprachmodell, "installed_models", weg)


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
        if "Fäden" in system:
            return "- Die Wirtin wartet auf Antwort."
        return "Die Runde tastet sich voran."


def eine_sitzung(tmp_path, *, chronik=False, rueckblick=False):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    sitzung_id = notes.create_session(
        config.database_path, played_on="2026-08-05", title="Der Keller"
    )
    szene = notes.session(config.database_path, sitzung_id).scenes[0]
    notes.add_note(config.database_path, szene.id, "Wir brechen bei Sonnenaufgang auf.")
    if chronik or rueckblick:
        compose_session(config, sitzung_id, model=Chronist())
    if rueckblick:
        recap_session(config, sitzung_id, model=Chronist())
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


def test_der_rueckblick_steht_ueber_der_chronik(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path, rueckblick=True)
    html = gelesen(config, f"/sitzungen/{sitzung_id}/protokoll")
    assert "<h2>Rückblick — Sitzung vom 2026-08-05: Der Keller</h2>" in html
    assert html.index("Rückblick — Sitzung") < html.index("Chronik — Sitzung")
    assert '<section class="abschnitt deutung">' in html
    assert "Die Wirtin wartet auf Antwort." in html


def test_ohne_rueckblick_bleibt_die_ansicht_bei_der_chronik(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path, chronik=True)
    assert "Rückblick" not in gelesen(config, f"/sitzungen/{sitzung_id}/protokoll")


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


def test_die_suche_ist_von_jeder_seite_erreichbar(tmp_path):
    assert 'href="/suche"' in gelesen(Config(data_dir=tmp_path), "/")


def test_die_suche_findet_notiz_und_chronik_getrennt(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path, chronik=True)
    html = gelesen(config, "/suche?q=sonnenaufgang")
    assert "<h2>Notizen</h2>" in html
    assert "<h2>Chronik</h2>" in html
    assert f'href="/sitzungen/{sitzung_id}#szene-' in html
    assert f'href="/sitzungen/{sitzung_id}/protokoll"' in html
    assert "<mark>" in html


def test_ohne_treffer_sagt_die_suche_wonach_gesucht_wurde(tmp_path):
    config, _ = eine_sitzung(tmp_path)
    html = gelesen(config, "/suche?q=Drachenhort")
    assert "Keine Treffer für „Drachenhort“" in html


def test_ohne_indexinhalt_sagt_die_suche_dass_nichts_da_ist(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/suche?q=Schwert")
    assert "Es ist noch nichts abgelegt" in html


def test_ohne_eingabe_bleibt_die_suche_stumm(tmp_path):
    config, _ = eine_sitzung(tmp_path)
    html = gelesen(config, "/suche")
    assert "Keine Treffer" not in html
    assert 'name="q"' in html


def test_die_suche_steht_hinter_demselben_tuersteher(tmp_path):
    assert bewacht(tmp_path).get("/suche?q=schwert").status_code == 403


def test_die_einstellungsseite_zeigt_die_gepflegten_werte(tmp_path):
    config = Config(
        foundry_url="https://foundry.example",
        foundry_user="chronist",
        foundry_password=PASSWORT,
        data_dir=tmp_path,
    )
    html = gelesen(config, "/einstellungen")
    assert "https://foundry.example" in html
    assert 'name="foundry_user"' in html
    assert 'name="discord_bot_token"' in html
    assert 'name="ollama_url"' in html
    assert 'name="ollama_model"' in html


def test_das_passwort_steht_in_keiner_antwort(tmp_path):
    config = Config(foundry_password=PASSWORT, data_dir=tmp_path)
    client = create_app(config).test_client()
    for pfad in ("/einstellungen", "/status"):
        assert PASSWORT not in client.get(pfad).get_data(as_text=True)
    antwort = client.post("/einstellungen", data={"foundry_password": PASSWORT})
    assert antwort.status_code == 302
    assert PASSWORT not in antwort.get_data(as_text=True)
    assert PASSWORT not in antwort.headers["Location"]
    assert PASSWORT not in client.get("/einstellungen").get_data(as_text=True)


def test_die_seite_sagt_nur_ob_ein_passwort_gesetzt_ist(tmp_path):
    ohne = gelesen(Config(data_dir=tmp_path / "ohne"), "/einstellungen")
    assert "Noch kein Passwort gesetzt" in ohne
    mit = gelesen(Config(foundry_password=PASSWORT, data_dir=tmp_path / "mit"), "/einstellungen")
    assert "Das Passwort ist" in mit


def test_gespeichertes_schlaegt_die_umgebung(tmp_path):
    config = Config(
        foundry_url="https://umgebung.example",
        foundry_user="umgebungs-konto",
        foundry_password=PASSWORT,
        data_dir=tmp_path,
    )
    client = create_app(config).test_client()
    client.post("/einstellungen", data={"foundry_url": "https://frontend.example"})
    assert settings.effective(config).foundry_url == "https://frontend.example"
    for pfad in ("/status", "/einstellungen"):
        html = client.get(pfad).get_data(as_text=True)
        assert "https://frontend.example" in html
        assert "https://umgebung.example" not in html


def test_ein_leeres_passwortfeld_behaelt_das_passwort(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einstellungen", data={"foundry_password": PASSWORT})
    client.post("/einstellungen", data={"foundry_user": "chronist", "foundry_password": ""})
    aktuell = settings.effective(config)
    assert aktuell.foundry_password == PASSWORT
    assert aktuell.foundry_user == "chronist"


def test_der_bot_token_steht_in_keiner_antwort(tmp_path):
    config = Config(discord_bot_token=BOT_TOKEN, data_dir=tmp_path)
    client = create_app(config).test_client()
    for pfad in ("/einstellungen", "/status"):
        assert BOT_TOKEN not in client.get(pfad).get_data(as_text=True)
    antwort = client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN})
    assert antwort.status_code == 302
    assert BOT_TOKEN not in antwort.get_data(as_text=True)
    assert BOT_TOKEN not in antwort.headers["Location"]
    assert BOT_TOKEN not in client.get("/einstellungen").get_data(as_text=True)
    assert BOT_TOKEN not in client.get("/status").get_data(as_text=True)


def test_die_seite_sagt_nur_ob_ein_bot_token_gesetzt_ist(tmp_path):
    ohne = gelesen(Config(data_dir=tmp_path / "ohne"), "/einstellungen")
    assert "Noch kein Bot-Token gesetzt" in ohne
    mit = gelesen(Config(discord_bot_token=BOT_TOKEN, data_dir=tmp_path / "mit"), "/einstellungen")
    assert "Der Token ist" in mit


def test_ein_leeres_bot_token_feld_behaelt_den_token(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN})
    client.post("/einstellungen", data={"foundry_user": "chronist", "discord_bot_token": ""})
    aktuell = settings.effective(config)
    assert aktuell.discord_bot_token == BOT_TOKEN
    assert aktuell.foundry_user == "chronist"


def test_ein_gespeicherter_bot_token_richtet_discord_ohne_umgebung_ein(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    assert "Kein Bot-Token" in client.get("/status").get_data(as_text=True)

    client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN})

    assert settings.effective(config).discord_configured
    html = client.get("/status").get_data(as_text=True)
    assert "Bot-Token gesetzt" in html
    assert f"<dd>{settings.FRONTEND}</dd>" in html


def test_status_nennt_je_wert_die_quelle(tmp_path):
    config = Config(foundry_user="umgebungs-konto", data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einstellungen", data={"foundry_url": "https://frontend.example"})
    html = client.get("/status").get_data(as_text=True)
    assert f"<dd>{settings.FRONTEND}</dd>" in html
    assert f"<dd>{settings.UMGEBUNG}</dd>" in html
    assert f"<dd>{settings.UNGESETZT}</dd>" in html


def test_die_einstellungen_stehen_hinter_demselben_tuersteher(tmp_path):
    client = bewacht(tmp_path)
    assert client.get("/einstellungen").status_code == 403
    assert (
        client.post("/einstellungen", data={"foundry_url": "https://frontend.example"}).status_code
        == 403
    )
    assert client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN}).status_code == 403
    assert settings.stored(Config(data_dir=tmp_path).database_path) == {}


def test_erreichbares_ollama_bietet_die_modelle_zur_auswahl(tmp_path, monkeypatch):
    monkeypatch.setattr(sprachmodell, "installed_models", lambda adresse, **k: ("gemma4:12b",))
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "<select" in html
    assert '<option value="gemma4:12b"' in html


def test_ohne_ollama_bleibt_ein_textfeld_und_ein_ehrlicher_satz(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "<select" not in html
    assert 'name="ollama_model"' in html
    assert "nicht erreichbar" in html
    assert settings.DEFAULT_OLLAMA_URL in html


def test_ein_kaputtes_ollama_bricht_die_seite_nicht(tmp_path, monkeypatch):
    def platzt(adresse, **kwargs):
        raise ModelUnreachable("kein JSON")

    monkeypatch.setattr(sprachmodell, "installed_models", platzt)
    assert (
        create_app(Config(data_dir=tmp_path)).test_client().get("/einstellungen").status_code == 200
    )


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


def test_ohne_bot_token_zeigen_die_einstellungen_die_einrichtung(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "Developer Portal" in html
    assert "Message Content Intent" in html


def test_mit_bot_token_verschwindet_die_einrichtungshilfe(tmp_path):
    config = Config(discord_bot_token="token-aus-der-umgebung", data_dir=tmp_path)
    html = gelesen(config, "/einstellungen")
    assert "Developer Portal" not in html


def test_der_zustellkanal_wird_im_formular_gepflegt(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()

    assert 'name="discord_recap_channel"' in client.get("/einstellungen").get_data(as_text=True)
    client.post("/einstellungen", data={"discord_recap_channel": "chronik"})

    assert settings.effective(config).discord_recap_channel == "chronik"
    for pfad in ("/einstellungen", "/status"):
        assert "chronik" in client.get(pfad).get_data(as_text=True)


def test_ohne_zustellkanal_sagt_der_status_dass_nichts_zugestellt_wird(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "Kein Zustellkanal" in html


UNKONFIGURIERT = "Foundry ist nicht eingerichtet"
VERALTET = "Der letzte Abgleich mit Foundry ist gescheitert"


def test_ohne_foundry_traegt_jede_arbeitsseite_das_band(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    for pfad in ("/", f"/sitzungen/{sitzung_id}", "/protokolle", "/suche"):
        assert UNKONFIGURIERT in gelesen(config, pfad)


def test_status_und_einstellungen_erklaeren_es_selbst_und_tragen_kein_band(tmp_path):
    config = Config(data_dir=tmp_path)
    for pfad in ("/status", "/einstellungen"):
        assert UNKONFIGURIERT not in gelesen(config, pfad)


def test_mit_foundry_und_ohne_panne_bleibt_die_arbeitsseite_ohne_band(config, welt):
    service.sync(config, client=Abgleich(welt))
    html = gelesen(config, "/")
    assert UNKONFIGURIERT not in html
    assert VERALTET not in html


def test_ein_gescheiterter_abgleich_steht_auf_der_arbeitsseite(config, welt):
    service.sync(config, client=Abgleich(welt))
    service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    html = gelesen(config, "/")
    assert VERALTET in html
    assert '<a href="/status">' in html


def test_ein_geglueckter_abgleich_nimmt_das_band_wieder_weg(config, welt):
    service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    assert VERALTET in gelesen(config, "/")
    service.sync(config, client=Abgleich(welt))
    assert VERALTET not in gelesen(config, "/")


def test_das_band_verraet_nichts_vor_der_haustuer(tmp_path):
    antwort = bewacht(tmp_path).get("/")
    assert antwort.status_code == 403
    assert UNKONFIGURIERT not in antwort.get_data(as_text=True)


def test_das_ollama_dieser_box_steht_nicht_als_rohe_ip_da(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "Ollama dieser Box" in html
    assert "Ollama läuft woanders" in html
    assert f'value="{settings.DEFAULT_OLLAMA_URL}"' not in html


def test_eine_eigene_ollama_adresse_bekommt_das_feld(tmp_path):
    config = Config(ollama_url="http://ollama.example:11434", data_dir=tmp_path)
    html = gelesen(config, "/einstellungen")
    assert "Ollama läuft woanders" not in html
    assert 'value="http://ollama.example:11434"' in html


def test_der_diktier_knopf_haengt_verborgen_an_jedem_notizfeld(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    szene = notes.session(config.database_path, sitzung_id).scenes[0]
    html = gelesen(config, f"/sitzungen/{sitzung_id}")
    assert f'data-diktat="notiz-{szene.id}"' in html
    assert 'aria-pressed="false" hidden>Diktieren</button>' in html


def test_ohne_spracherkennung_bleibt_der_knopf_weg(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    html = gelesen(config, f"/sitzungen/{sitzung_id}")
    assert "window.SpeechRecognition || window.webkitSpeechRecognition" in html
    assert 'erkennung.lang = "de-DE"' in html
    assert 'class="gedaempft diktat-hinweis" hidden' in html
    assert "Browser-Herstellers" in html
