import pytest
from conftest import GM_FIGUR, UNSER_KONTO, laufender_job, warte_bis

import chronicle.__main__ as entry
from chronicle import db, jobs, notes, protocol, recordings, settings
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
    """Der alte Status-Pfad — er landet seit #40 auf der Einstellungsseite."""
    return create_app(config).test_client().get("/status", follow_redirects=True)


def zustand(client, **kwargs):
    return client.get("/status", follow_redirects=True, **kwargs)


def test_startet_ohne_foundry_und_erklaert_was_fehlt(tmp_path):
    antwort = seite(Config(data_dir=tmp_path))
    assert antwort.status_code == 200
    html = antwort.get_data(as_text=True)
    assert "Noch kein Zugang zu Foundry" in html
    for feld in ("Foundry-Adresse", "Foundry-Benutzer", "Foundry-Passwort"):
        assert feld in html


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
    assert "Ollama-Adresse" in html
    assert f"<dd>{settings.STANDARD}</dd>" in html


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
    antwort = bewacht(tmp_path).get("/", headers={"Remote-User": "mira"}, follow_redirects=True)
    assert antwort.status_code == 200


def test_healthz_bleibt_am_proxy_vorbei_erreichbar(tmp_path):
    assert bewacht(tmp_path).get("/healthz").status_code == 200


def test_ohne_erzwingung_laeuft_es_lokal_weiter(tmp_path):
    client = create_app(Config(data_dir=tmp_path)).test_client()
    assert client.get("/", follow_redirects=True).status_code == 200


def test_ohne_anmeldung_sagt_die_seite_was_das_heisst(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "Niemand ist angemeldet" in html
    assert "wer diese Adresse erreicht, sieht alles" in html


def test_die_einstellungen_nennen_oben_den_angemeldeten_menschen(tmp_path):
    antwort = zustand(bewacht(tmp_path), headers={"Remote-User": "mira"})
    html = antwort.get_data(as_text=True)
    assert "Angemeldet als <strong>mira</strong>" in html
    assert "Niemand ist angemeldet" not in html


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
    # Wer schon mitschreibt, hat die Einrichtung hinter sich oder beiseitegelegt —
    # sonst führte jede dieser Seiten in den Wizard statt zur Sitzung.
    settings.finish_onboarding(config.database_path)
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


def test_ohne_chronik_bietet_die_ansicht_den_knopf_an(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    html = gelesen(config, f"/sitzungen/{sitzung_id}/protokoll")
    assert "Noch keine Chronik" in html
    assert f'action="/sitzungen/{sitzung_id}/chronik"' in html
    assert ">Chronik erstellen</button>" in html


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
    config, _ = eine_sitzung(tmp_path)
    assert 'href="/suche"' in gelesen(config, "/")


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
    for pfad in ("/einstellungen", "/status", "/einrichtung/foundry"):
        assert PASSWORT not in client.get(pfad, follow_redirects=True).get_data(as_text=True)
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
        html = client.get(pfad, follow_redirects=True).get_data(as_text=True)
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
    for pfad in ("/einstellungen", "/status", "/einrichtung/discord"):
        assert BOT_TOKEN not in client.get(pfad, follow_redirects=True).get_data(as_text=True)
    antwort = client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN})
    assert antwort.status_code == 302
    assert BOT_TOKEN not in antwort.get_data(as_text=True)
    assert BOT_TOKEN not in antwort.headers["Location"]
    assert BOT_TOKEN not in client.get("/einstellungen").get_data(as_text=True)
    assert BOT_TOKEN not in zustand(client).get_data(as_text=True)


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
    assert "Kein Bot-Token" in zustand(client).get_data(as_text=True)

    client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN})

    assert settings.effective(config).discord_configured
    html = zustand(client).get_data(as_text=True)
    assert "Bot-Token gesetzt" in html
    assert f"<dd>{settings.FRONTEND}</dd>" in html


def test_status_nennt_je_wert_die_quelle(tmp_path):
    config = Config(foundry_user="umgebungs-konto", data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einstellungen", data={"foundry_url": "https://frontend.example"})
    html = zustand(client).get_data(as_text=True)
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
    assert "Nicht erreichbar — dann wird nur geordnet, nicht formuliert" in html
    assert "Modellnamen von Hand eintragen" in html
    assert settings.DEFAULT_OLLAMA_URL in html


def test_ein_erreichbares_modell_meldet_sich_als_bereit(tmp_path, monkeypatch):
    monkeypatch.setattr(sprachmodell, "installed_models", lambda adresse, **k: ("gemma4:12b",))
    config = Config(
        ollama_url="http://ollama.example:11434", ollama_model="gemma4:12b", data_dir=tmp_path
    )
    html = gelesen(config, "/einstellungen")
    assert "Bereit — <code>gemma4:12b</code>" in html
    assert "Nicht erreichbar" not in html


def test_ein_erreichbares_ollama_ohne_gewaehltes_modell_sagt_was_zu_tun_ist(tmp_path, monkeypatch):
    monkeypatch.setattr(sprachmodell, "installed_models", lambda adresse, **k: ("gemma4:12b",))
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "Kein Modell gewählt" in html
    assert "Wähle unten eins und speichere" in html


def test_ein_modell_ohne_eigene_adresse_ist_schon_bereit(tmp_path, monkeypatch):
    """Ohne gespeicherte Adresse gilt das Ollama dieser Box — kein Zustand bleibt offen."""
    monkeypatch.setattr(sprachmodell, "installed_models", lambda adresse, **k: ("gemma4:12b",))
    html = gelesen(Config(ollama_model="gemma4:12b", data_dir=tmp_path), "/einstellungen")
    assert "Bereit — <code>gemma4:12b</code>" in html
    assert "Noch keine Ollama-Adresse gespeichert" not in html


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

    monkeypatch.setattr(entry, "dienst", lambda: Attrappe())
    monkeypatch.setenv("CHRONICLE_HOST", "127.0.0.2")
    monkeypatch.setenv("CHRONICLE_PORT", "9001")
    entry.main()
    assert aufruf == {"host": "127.0.0.2", "port": 9001}


def test_main_hat_vorgaben(monkeypatch):
    aufruf = {}

    class Attrappe:
        def run(self, **kwargs):
            aufruf.update(kwargs)

    monkeypatch.setattr(entry, "dienst", lambda: Attrappe())
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
        assert "chronik" in client.get(pfad, follow_redirects=True).get_data(as_text=True)


def test_ohne_zustellkanal_sagt_der_status_dass_nichts_zugestellt_wird(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "Kein Zustellkanal" in html


UNKONFIGURIERT = "Foundry ist nicht eingerichtet"
VERALTET = "Der letzte Abgleich mit Foundry ist gescheitert"


def test_ohne_foundry_traegt_jede_arbeitsseite_das_band(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    for pfad in ("/", f"/sitzungen/{sitzung_id}", "/protokolle", "/suche"):
        assert UNKONFIGURIERT in gelesen(config, pfad)


def test_einstellungen_und_wizard_erklaeren_es_selbst_und_tragen_kein_band(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    for pfad in ("/status", "/einstellungen", "/einrichtung"):
        html = client.get(pfad, follow_redirects=True).get_data(as_text=True)
        assert UNKONFIGURIERT not in html


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
    assert '<a href="/einstellungen#zustand">' in html


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


def test_beim_ersten_mal_fuehrt_die_startseite_in_die_einrichtung(tmp_path):
    antwort = create_app(Config(data_dir=tmp_path)).test_client().get("/")
    assert antwort.status_code == 302
    assert antwort.headers["Location"] == "/einrichtung"


def test_mit_foundry_gibt_es_keinen_wizard(tmp_path):
    config = Config(
        foundry_url="https://foundry.example",
        foundry_user="chronist",
        foundry_password=PASSWORT,
        data_dir=tmp_path,
    )
    assert create_app(config).test_client().get("/").status_code == 200


def test_eine_sitzung_ersetzt_die_einrichtung_nicht(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    notes.create_session(config.database_path, played_on="2026-08-05", title="Der Keller")
    antwort = create_app(config).test_client().get("/")
    assert antwort.status_code == 302
    assert antwort.headers["Location"] == "/einrichtung"


def test_der_wizard_beginnt_bei_foundry(tmp_path):
    client = create_app(Config(data_dir=tmp_path)).test_client()
    antwort = client.get("/einrichtung")
    assert antwort.headers["Location"] == "/einrichtung/foundry"
    html = client.get("/einrichtung/foundry").get_data(as_text=True)
    assert "Schritt 1 von 3" in html
    assert 'name="foundry_url"' in html
    assert 'name="foundry_password"' in html


def test_der_discord_schritt_traegt_die_bestehende_einrichtungsanleitung(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einrichtung/discord")
    assert "Developer Portal" in html
    assert "Message Content Intent" in html


def test_ein_erfundener_schritt_gibt_es_nicht(tmp_path):
    client = create_app(Config(data_dir=tmp_path)).test_client()
    assert client.get("/einrichtung/authelia").status_code == 404
    assert client.post("/einrichtung/authelia", data={}).status_code == 404


def test_der_wizard_speichert_ueber_denselben_weg_wie_die_einstellungen(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    antwort = client.post(
        "/einrichtung/foundry",
        data={
            "foundry_url": "https://foundry.example",
            "foundry_user": "chronist",
            "foundry_password": PASSWORT,
        },
    )
    assert antwort.headers["Location"] == "/einrichtung/discord"
    aktuell = settings.effective(config)
    assert aktuell.foundry_url == "https://foundry.example"
    assert aktuell.foundry_password == PASSWORT
    assert settings.sources(config)["foundry_url"] == settings.FRONTEND


def test_ein_schritt_nimmt_den_anderen_schritten_ihre_werte_nicht_weg(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einrichtung/foundry", data={"foundry_user": "chronist"})
    client.post("/einrichtung/discord", data={"discord_recap_channel": "chronik"})
    aktuell = settings.effective(config)
    assert aktuell.foundry_user == "chronist"
    assert aktuell.discord_recap_channel == "chronik"


def test_ueberspringen_schreibt_nichts_und_geht_weiter(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    antwort = client.post(
        "/einrichtung/discord",
        data={"discord_bot_token": BOT_TOKEN, "tat": "ueberspringen"},
    )
    assert antwort.headers["Location"] == "/einrichtung/ollama"
    assert settings.effective(config).discord_bot_token is None


def test_der_letzte_schritt_setzt_das_flag_und_fuehrt_zur_sitzungsseite(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    antwort = client.post("/einrichtung/ollama", data={"ollama_model": "chronist-modell"})
    assert antwort.headers["Location"] == "/"
    assert settings.onboarding_done(config.database_path)


def test_auch_wer_den_letzten_schritt_ueberspringt_ist_fertig(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einrichtung/ollama", data={"tat": "ueberspringen"})
    assert settings.onboarding_done(config.database_path)


def test_nach_der_einrichtung_kommt_der_wizard_nie_wieder(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    for schritt in ("foundry", "discord", "ollama"):
        client.post(f"/einrichtung/{schritt}", data={"tat": "ueberspringen"})
    assert client.get("/").status_code == 200
    # Auch ein Neustart des Dienstes ändert daran nichts — das Flag steht in der SQLite.
    assert create_app(config).test_client().get("/").status_code == 200


def test_solange_nichts_eingerichtet_ist_fuehrt_das_band_in_den_wizard(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/protokolle")
    assert UNKONFIGURIERT in html
    assert '<a href="/einrichtung">Zugang eintragen</a>' in html


def test_der_wizard_bietet_spaeter_an_ausser_im_letzten_schritt(tmp_path):
    client = create_app(Config(data_dir=tmp_path)).test_client()
    assert 'value="spaeter"' in client.get("/einrichtung/foundry").get_data(as_text=True)
    assert 'value="spaeter"' not in client.get("/einrichtung/ollama").get_data(as_text=True)


def test_spaeter_legt_die_einrichtung_beiseite_ohne_etwas_zu_speichern(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    antwort = client.post(
        "/einrichtung/foundry",
        data={"foundry_user": "chronist", "foundry_password": PASSWORT, "tat": "spaeter"},
    )
    assert antwort.headers["Location"] == "/"
    assert settings.stored(config.database_path) == {}
    assert settings.onboarding_done(config.database_path)
    html = client.get("/").get_data(as_text=True)
    assert UNKONFIGURIERT in html
    assert '<a href="/einstellungen">Zugang eintragen</a>' in html


def test_nach_der_einrichtung_fuehrt_das_band_in_die_einstellungen(tmp_path):
    config, _ = eine_sitzung(tmp_path)
    settings.finish_onboarding(config.database_path)
    html = gelesen(config, "/")
    assert UNKONFIGURIERT in html
    assert '<a href="/einstellungen">Zugang eintragen</a>' in html


def test_der_wizard_steht_hinter_demselben_tuersteher(tmp_path):
    client = bewacht(tmp_path)
    assert client.get("/einrichtung").status_code == 403
    assert client.get("/einrichtung/foundry").status_code == 403
    assert (
        client.post("/einrichtung/foundry", data={"foundry_password": PASSWORT}).status_code == 403
    )
    assert settings.stored(Config(data_dir=tmp_path).database_path) == {}


def test_die_fachlichen_reiter_stehen_neben_einem_zahnrad(tmp_path):
    config, _ = eine_sitzung(tmp_path)
    html = gelesen(config, "/")
    for reiter in ("Sitzungen", "Protokolle", "Suche"):
        assert f">{reiter}</a>" in html
    assert '<span class="nur-vorlesen">Einstellungen</span>' in html
    assert "<svg" in html
    assert ">Status</a>" not in html


def test_der_alte_statuspfad_leitet_dauerhaft_in_den_zustand_um(tmp_path):
    antwort = create_app(Config(data_dir=tmp_path)).test_client().get("/status")
    assert antwort.status_code == 301
    assert antwort.headers["Location"] == "/einstellungen#zustand"


def test_der_zustand_steht_unter_seinem_anker_in_den_einstellungen(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert 'id="zustand"' in html
    assert "<h2>Woher kommen die Spieldaten?</h2>" in html


# --- Nach Nutzerfragen geschnitten ---------------------------------------------------


FRAGEN = (
    "Woher kommen die Spieldaten?",
    "Wie kommt Gesprochenes herein?",
    "Wer formuliert die Chronik?",
)


def test_die_einstellungen_sind_nach_nutzerfragen_geschnitten(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    for frage in FRAGEN:
        assert f"<h2>{frage}</h2>" in html
    assert html.index(FRAGEN[0]) < html.index(FRAGEN[1]) < html.index(FRAGEN[2])


def test_die_foundry_karte_traegt_zugang_zustand_und_den_abgleich(config, welt):
    service.sync(config, client=Abgleich(welt))
    html = gelesen(config, "/einstellungen")
    karte = html.split('id="zustand"')[1].split("</section>")[0]
    assert 'name="foundry_url"' in karte
    assert "Zugang steht" in karte
    assert "Stand vom" in karte
    assert ">Jetzt abgleichen</button>" in karte


def test_der_abgleich_knopf_haengt_am_eigenen_formular(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert '<form id="abgleich" method="post" action="/abgleich"' in html
    assert 'form="abgleich"' in html


def test_die_technikdetails_stehen_zugeklappt_am_ende(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    block = html.split('<details class="karte">')[1]
    assert "<summary>Technikdetails</summary>" in block
    assert "Datenbank" in block
    assert "Schema-Stand" in block
    assert "Foundry-Adresse" in block


# --- Anstoßen aus der Oberfläche ----------------------------------------------------


def test_die_sitzungsseite_traegt_den_chronik_knopf(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    html = gelesen(config, f"/sitzungen/{sitzung_id}")
    assert f'action="/sitzungen/{sitzung_id}/chronik"' in html
    assert ">Chronik erstellen</button>" in html


def test_der_knopf_stoesst_den_lauf_an_und_fuehrt_zur_chronik(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    client = create_app(config).test_client()

    antwort = client.post(f"/sitzungen/{sitzung_id}/chronik")

    assert antwort.status_code == 302
    assert antwort.headers["Location"] == f"/sitzungen/{sitzung_id}/protokoll"
    assert warte_bis(lambda: protocol.stored(config.database_path, sitzung_id) is not None)


def test_nach_dem_lauf_sagt_die_seite_was_dabei_herauskam(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    client = create_app(config).test_client()
    client.post(f"/sitzungen/{sitzung_id}/chronik")
    assert warte_bis(lambda: jobs.latest(config.database_path, jobs.CHRONIK, sitzung_id).fertig)

    html = client.get(f"/sitzungen/{sitzung_id}").get_data(as_text=True)
    assert "stehen bereit" in html
    assert ">Chronik ansehen</a>" in html


def test_waehrend_der_lauf_laeuft_ist_der_knopf_aus(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    laufender_job(config.database_path, jobs.CHRONIK, sitzung_id)

    html = gelesen(config, f"/sitzungen/{sitzung_id}")
    assert "Die Chronik wird erstellt" in html
    assert '<button type="button" disabled>Chronik erstellen</button>' in html
    assert f'action="/sitzungen/{sitzung_id}/chronik"' not in html


def test_eine_zweite_chronik_wartet_bis_die_erste_durch_ist(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    zweite = notes.create_session(config.database_path, played_on="2026-08-06", title="Der Hafen")
    laufender_job(config.database_path, jobs.CHRONIK, sitzung_id)

    html = gelesen(config, f"/sitzungen/{zweite}")
    assert "Gerade wird eine andere Chronik erstellt" in html


def test_ein_unterbrochener_lauf_steht_als_solcher_auf_der_seite(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path)
    # Ohne Faden-Vermerk: genau das, was ein Neustart mitten im Lauf hinterlässt.
    verbindung = db.connect(config.database_path)
    with verbindung:
        verbindung.execute(
            "INSERT INTO job (kind, session_id, state, started_at) VALUES (?, ?, ?, ?)",
            (jobs.CHRONIK, sitzung_id, jobs.LAEUFT, "2026-08-06T10:00:00+00:00"),
        )
    verbindung.close()

    html = gelesen(config, f"/sitzungen/{sitzung_id}")
    assert "wurde unterbrochen" in html
    assert f'action="/sitzungen/{sitzung_id}/chronik"' in html


def test_eine_unbekannte_sitzung_laesst_sich_nicht_anstossen(tmp_path):
    config, _ = eine_sitzung(tmp_path)
    assert create_app(config).test_client().post("/sitzungen/999/chronik").status_code == 404


def test_der_chronik_knopf_steht_hinter_demselben_tuersteher(tmp_path):
    assert bewacht(tmp_path).post("/sitzungen/1/chronik").status_code == 403


def test_ein_gescheiterter_abgleich_bietet_das_erneute_holen_an(config, welt):
    service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    html = gelesen(config, "/")
    assert 'action="/abgleich"' in html
    assert ">Jetzt abgleichen</button>" in html


def test_der_zustand_bietet_den_abgleich_immer_an(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert 'action="/abgleich"' in html
    assert ">Jetzt abgleichen</button>" in html


def test_der_abgleich_kehrt_auf_die_seite_zurueck_von_der_er_kam(config, welt):
    client = create_app(config).test_client()
    antwort = client.post("/abgleich", data={"zurueck": f"/sitzungen/{1}"})
    assert antwort.headers["Location"] == "/sitzungen/1"
    assert warte_bis(lambda: not jobs.running(config.database_path, jobs.ABGLEICH))


def test_der_abgleich_folgt_keiner_adresse_nach_draussen(config):
    client = create_app(config).test_client()
    antwort = client.post("/abgleich", data={"zurueck": "//woanders.example/"})
    assert antwort.headers["Location"] == "/einstellungen#zustand"
    assert warte_bis(lambda: not jobs.running(config.database_path, jobs.ABGLEICH))


def test_waehrend_des_abgleichs_sagt_das_band_was_laeuft(config, welt):
    service.sync(config, client=Abgleich(welt))
    laufender_job(config.database_path, jobs.ABGLEICH)

    html = gelesen(config, "/")
    assert "Zahlen aus Foundry werden gerade geholt" in html


def test_der_abgleich_steht_hinter_demselben_tuersteher(tmp_path):
    assert bewacht(tmp_path).post("/abgleich").status_code == 403


# --- Nutzersprache: was hier steht, sagt was zu tun ist ------------------------------


# Header-Namen sind Proxy-Innenleben: niemand muss sie kennen, um die Chronik zu bedienen.
SYSTEMWOERTER = (
    "python -m",
    "CHRONICLE_",
    "OLLAMA_",
    "Remote-User",
    "Forward-Auth",
    "Authelia",
)


def systemsprache(html):
    return [wort for wort in SYSTEMWOERTER if wort in html]


def test_keine_systemsprache_auf_einer_gerenderten_seite(tmp_path):
    config, sitzung_id = eine_sitzung(tmp_path, rueckblick=True)
    hochgeladen = recordings.enqueue(config.database_path, sitzung_id, "diktat.m4a")
    assert hochgeladen.status == recordings.WARTET
    app = create_app(config)
    client = app.test_client()

    for regel in app.url_map.iter_rules():
        if regel.endpoint in ("static", "healthz") or "GET" not in regel.methods:
            continue
        pfad = regel.rule.replace("<int:sitzung_id>", str(sitzung_id)).replace(
            "<schritt>", "foundry"
        )
        html = client.get(pfad, follow_redirects=True).get_data(as_text=True)
        assert systemsprache(html) == [], pfad


def test_auch_die_abweisung_spricht_nutzersprache(tmp_path):
    antwort = bewacht(tmp_path).get("/")
    assert antwort.status_code == 403
    assert systemsprache(antwort.get_data(as_text=True)) == []


def test_auch_das_abgelegte_protokoll_spricht_nutzersprache(tmp_path):
    """Der Text in der SQLite wird Wochen später gelesen — er ist selbst Oberfläche."""
    config, sitzung_id = eine_sitzung(tmp_path)
    chronik = compose_session(config, sitzung_id)
    rueckblick = recap_session(config, sitzung_id)

    for abgelegt in (chronik.text, rueckblick.text, chronik.message, rueckblick.message):
        assert systemsprache(abgelegt) == [], abgelegt
        assert "in den Einstellungen" in abgelegt

    html = gelesen(config, f"/sitzungen/{sitzung_id}/protokoll")
    assert systemsprache(html) == []
    assert "Noch kein Modell gewählt" in html
