"""Die Betreiber-Seite — alles, was hier noch steht, gehört der Instanz.

Die Spielinhalte sind mit #157 nach Discord gezogen; was sie zusagten, steht seither in
``test_chronik``, ``test_erinnern``, ``test_notes``, ``test_register``, ``test_search``
und ``test_kette``. Hier bleiben die Haustür (ADR 0001) und die Frage, wer verwalten darf.

**Gepflegt werden Bot-Token, Ollama-Adresse und -Modell seit #230 nicht mehr.** Sie kommen
aus der Umgebung; diese Seite hat für sie keinen Schreibweg mehr, und der Token wird ihrem
Prozess gar nicht erst gereicht. Was hier steht, sind deshalb vor allem Zusicherungen
darüber, dass es diesen Weg **nicht** gibt.
"""

import importlib
import logging
import re
from pathlib import Path

from conftest import GM_FIGUR, UNSER_KONTO, runde, systemsprache
from flask import request

import chronicle.__main__ as entry
from chronicle import db, herkunft, instanz, settings, zugang
from chronicle.app import create_app
from chronicle.config import Config
from chronicle.foundry import service
from chronicle.foundry.client import FoundryUnreachable

WURZEL = Path(__file__).resolve().parents[1]

PASSWORT = "passwort-taucht-nirgends-auf"
BOT_TOKEN = "bot-token-taucht-nirgends-auf"

# Was eine Gilde über ``/setup`` in Discord gepflegt hat, dazu die Werte der Instanz.
# Diese Oberfläche zeigt das meiste davon gar nicht mehr — nehmen darf sie es deshalb
# erst recht nicht.
IN_DISCORD_GEPFLEGT = {
    "foundry_url": "https://foundry.example",
    "foundry_user": "chronist",
    "discord_recap_channel": "chronik",
}

# Die drei Werte der Instanz. Seit #230 kommen sie aus der Umgebung, und diese Seite hat
# für keinen von ihnen noch ein Feld oder einen Schreibweg.
AUS_DER_UMGEBUNG = ("discord_bot_token", "ollama_url", "ollama_model")


def seite(config):
    """Der alte Status-Pfad — er landet seit #40 auf der Einstellungsseite."""
    return create_app(config).test_client().get("/status", follow_redirects=True)


def zustand(client, **kwargs):
    return client.get("/status", follow_redirects=True, **kwargs)


def gelesen(config, pfad):
    return create_app(config).test_client().get(pfad).get_data(as_text=True)


def bewacht(tmp_path):
    return create_app(Config(data_dir=tmp_path, require_remote_user=True)).test_client()


# Was seit #89 nicht mehr auf die Betreiber-Seite gehört: alles, was einer Runde gehört
# und in Discord gepflegt wird.
RUNDEN_FELDER = (
    "foundry_url",
    "foundry_user",
    "foundry_password",
    "discord_recap_channel",
    "nightly_time",
    "nightly_zone",
    settings.QUELLE_KEY,
)


def test_die_betreiberseite_traegt_nur_noch_ein_feld(tmp_path):
    """Was der Runde gehört, steht in Discord — was der Instanz gehört, in der Umgebung."""
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    for feld in (*RUNDEN_FELDER, *AUS_DER_UMGEBUNG):
        assert f'name="{feld}"' not in html, feld
    assert 'name="admin_group"' in html


def test_die_betreiberseite_sagt_warum_es_sie_weiter_gibt(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "dieser Instanz" in html
    assert "<code>/setup</code> in Discord" in html


def test_ein_speichern_nimmt_der_runde_ihre_werte_nicht_weg(tmp_path):
    """Die Felder sind weg, die Werte bleiben — sonst löschte jedes Speichern die Runde leer."""
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    gruppe = runde(config)
    settings.save(gruppe, {"foundry_url": "https://foundry.example", "foundry_user": "chronist"})
    settings.save(gruppe, {"discord_recap_channel": "chronik"})
    settings.save_nightly_time(gruppe, "02:45")
    settings.save_foundry_quelle(gruppe, settings.TESTWELT)

    assert client.post("/einstellungen", data={"admin_group": ""}).status_code == 302

    aktuell = settings.effective(config, gruppe)
    assert aktuell.foundry_url == "https://foundry.example"
    assert aktuell.foundry_user == "chronist"
    assert aktuell.discord_recap_channel == "chronik"
    assert settings.nightly_time(gruppe) == "02:45"
    assert settings.foundry_quelle(gruppe) == settings.TESTWELT


def test_ohne_discord_ist_das_kein_fehler(tmp_path):
    html = seite(Config(data_dir=tmp_path)).get_data(as_text=True)
    assert "Einrichten dieses Dienstes auf der Box" in html
    assert "er läuft oder er läuft nicht" in html


def test_konfiguriert_zeigt_die_instanz_werte_aber_kein_geheimnis(tmp_path):
    config = Config(
        foundry_url="https://foundry.example",
        foundry_user="chronist",
        ollama_url="http://ollama.example:11434",
        discord_bot_token=BOT_TOKEN,
        data_dir=tmp_path,
    )
    html = seite(config).get_data(as_text=True)
    assert "http://ollama.example:11434" in html
    assert BOT_TOKEN not in html
    assert "https://foundry.example" not in html
    assert "chronist" not in html


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
    assert settings.DEFAULT_OLLAMA_URL in html


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


def test_der_stand_des_abgleichs_steht_nicht_mehr_auf_der_betreiberseite(config, welt):
    """Er gehört der Runde: die Meldungen selbst prüft ``test_foundry_service``."""
    service.sync(config, runde(config), client=Abgleich(welt))
    html = seite(config).get_data(as_text=True)
    assert "Stand vom" not in html
    assert "daggerheart" not in html
    assert GM_FIGUR not in html


# --- Die Haustür (ADR 0001) ----------------------------------------------------------
#
# Sie bleibt, obwohl das Spiel nach Discord gezogen ist: auf dieser Seite liegt der
# Bot-Token, also gibt es hier sehr wohl etwas zu schützen.

# Ein anderer Rechner im LAN. Der Testclient spricht sonst von 127.0.0.1 — das ist diese
# Maschine, und von der kommt auch der Proxy auf der Box. Die Adresse stammt aus TEST-NET-1
# (RFC 5737): eine echte LAN-Adresse könnte der Maschine gehören, auf der die Tests laufen.
AUS_DEM_LAN = {"REMOTE_ADDR": "192.0.2.55"}


def test_ohne_remote_user_kommt_niemand_durch(tmp_path):
    antwort = bewacht(tmp_path).get("/")
    assert antwort.status_code == 403
    assert "Kein Zugang" in antwort.get_data(as_text=True)


def test_auch_kein_lan_bypass_auf_die_unterseiten(tmp_path):
    client = bewacht(tmp_path)
    assert client.get("/status").status_code == 403
    assert client.get("/einstellungen").status_code == 403
    assert client.post("/einstellungen", data={}).status_code == 403


# Bis #190 hieß »kein LAN-Bypass« nur: ohne Kopfzeile kommt niemand durch. Genau das war
# die Lücke — die Kopfzeile schreibt sich jeder selbst. Was zählt, ist der Absender.


def test_die_erfundene_kopfzeile_aus_dem_lan_hilft_nicht(tmp_path):
    client = bewacht(tmp_path)
    for pfad in ("/", "/status", "/einstellungen"):
        antwort = client.get(pfad, headers={"Remote-User": "sonde"}, environ_base=AUS_DEM_LAN)
        assert antwort.status_code == 403, pfad
    assert (
        client.post(
            "/einstellungen",
            data={"discord_bot_token": "token-des-fremden"},
            headers={"Remote-User": "sonde"},
            environ_base=AUS_DEM_LAN,
        ).status_code
        == 403
    )


def test_auch_die_erfundene_gruppe_hilft_nicht(tmp_path):
    """Beide Kopfzeilen hängen an derselben Frage — eine allein zu prüfen wäre wertlos."""
    config = Config(data_dir=tmp_path, require_remote_user=True)
    db.init(config.database_path)
    instanz.save_admin_group(config.database_path, GRUPPE)
    client = create_app(config).test_client()
    kopf = {"Remote-User": "sonde", "Remote-Groups": GRUPPE}
    assert client.get("/einstellungen", headers=kopf, environ_base=AUS_DEM_LAN).status_code == 403
    antwort = client.post(
        "/einstellungen",
        data={"admin_group": "gruppe-des-fremden", "gruppe_bestaetigt": "ja"},
        headers=kopf,
        environ_base=AUS_DEM_LAN,
    )
    assert antwort.status_code == 403
    assert instanz.admin_group(config.database_path) == GRUPPE


def test_wer_den_proxy_woanders_hat_traegt_ihn_nach(tmp_path):
    """Der Weg zurück aus einer Aussperrung: eine Zeile Umgebung, kein neues Abbild."""
    config = Config(
        data_dir=tmp_path, require_remote_user=True, trusted_proxies=(AUS_DEM_LAN["REMOTE_ADDR"],)
    )
    client = create_app(config).test_client()
    antwort = client.get(
        "/einstellungen", headers={"Remote-User": "mira"}, environ_base=AUS_DEM_LAN
    )
    assert antwort.status_code == 200
    # Und die Liste ersetzt die Maschine: Loopback steht nicht mehr darin.
    assert client.get("/", headers={"Remote-User": "mira"}).status_code == 403


def test_ein_kern_der_die_pruefung_entwertet_wird_gemeldet_und_haelt_nichts_auf(
    tmp_path, monkeypatch, caplog
):
    """Steht ``ip_nonlocal_bind``, gilt jeder Absender als eigen — von hier aus unsichtbar.

    Der Dienst sagt es deshalb beim Start selbst. Starten muss er trotzdem: eine Instanz,
    die daran nicht mehr hochkommt, ist dieselbe Aussperrung noch einmal.
    """
    monkeypatch.setattr(herkunft, "ist_eigene", lambda gefragt: True)
    with caplog.at_level(logging.ERROR):
        app = create_app(Config(data_dir=tmp_path, require_remote_user=True))
    assert herkunft.NONLOCAL_SCHALTER in caplog.text
    assert herkunft.TRUSTED_VARIABLE in caplog.text
    assert app.test_client().get("/healthz").status_code == 200


def test_die_selbstprobe_bleibt_weg_wo_eine_gepflegte_liste_steht(tmp_path, monkeypatch, caplog):
    """Dort entscheidet die Liste, nicht der Kern — die Frage stellt sich gar nicht."""
    monkeypatch.setattr(herkunft, "ist_eigene", lambda gefragt: True)
    with caplog.at_level(logging.ERROR):
        create_app(
            Config(data_dir=tmp_path, require_remote_user=True, trusted_proxies=("127.0.0.1",))
        )
    assert caplog.text == ""


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


# --- Was hier gepflegt wird ----------------------------------------------------------


def test_die_einstellungsseite_zeigt_die_umgebung_und_nimmt_sie_nicht_entgegen(tmp_path):
    """Nachsehen ja, ändern nein — für die drei Instanz-Werte gibt es kein Feld mehr."""
    config = Config(ollama_url="http://ollama.example:11434", data_dir=tmp_path)
    html = gelesen(config, "/einstellungen")
    assert "http://ollama.example:11434" in html
    assert "nie zum" in html
    for feld in AUS_DER_UMGEBUNG:
        assert f'name="{feld}"' not in html, feld


def test_das_passwort_steht_in_keiner_antwort(tmp_path):
    """Eingegeben wird es in Discord — gezeigt wird es auch dann auf keiner Seite hier."""
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    zugang.merken(runde(config), PASSWORT)
    for pfad in ("/", "/einstellungen", "/status"):
        assert PASSWORT not in client.get(pfad, follow_redirects=True).get_data(as_text=True)


def test_das_passwort_wird_nirgends_gespeichert(tmp_path):
    """Es geht durch den Abgleich und ist danach weg — auch aus der Datei."""
    config = Config(
        foundry_url="https://foundry.example", foundry_user="chronist", data_dir=tmp_path
    )
    create_app(config)
    zugang.merken(runde(config), PASSWORT)
    service.sync(config, runde(config), client=Abgleich(fehler=FoundryUnreachable("aus")))
    assert settings.stored(runde(config)) == {}
    assert not zugang.ist_gemerkt(runde(config))
    roh = b""
    for datei in (config.database_path, config.database_path.with_suffix(".sqlite3-wal")):
        if datei.exists():
            roh += datei.read_bytes()
    assert PASSWORT not in roh.decode("utf-8", errors="ignore")


def test_das_formular_hat_kein_passwortfeld(tmp_path):
    """Die Betreiber-Seite kennt Foundry gar nicht mehr — und ein Passwortfeld erst recht nicht."""
    assert "foundry_password" not in gelesen(Config(data_dir=tmp_path), "/einstellungen")


def test_die_umgebung_ist_das_letzte_wort(tmp_path):
    """Umgekehrt als bis #230: ein abgesendetes Feld schreibt nichts mehr."""
    config = Config(ollama_url="http://umgebung.example:11434", data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einstellungen", data={"ollama_url": "http://untergeschoben.example:11434"})
    assert settings.effective(config, runde(config)).ollama_url == "http://umgebung.example:11434"
    assert settings.stored(runde(config)) == {}
    for pfad in ("/status", "/einstellungen"):
        html = client.get(pfad, follow_redirects=True).get_data(as_text=True)
        assert "http://umgebung.example:11434" in html
        assert "http://untergeschoben.example:11434" not in html


def test_ein_passwort_im_speichern_formular_wird_nicht_uebernommen(tmp_path):
    """Selbst wer das Feld von Hand nachbaut, bekommt keinen gespeicherten Wert."""
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post("/einstellungen", data={"admin_group": "", "foundry_password": PASSWORT})
    assert settings.stored(runde(config)) == {}
    assert not zugang.ist_gemerkt(runde(config))


def test_der_bot_token_steht_in_keiner_antwort(tmp_path):
    """Er wird diesem Prozess gar nicht mehr gereicht — aber untergeschoben schon gar nicht."""
    config = Config(discord_bot_token=BOT_TOKEN, data_dir=tmp_path)
    client = create_app(config).test_client()
    for pfad in ("/einstellungen", "/status"):
        assert BOT_TOKEN not in client.get(pfad, follow_redirects=True).get_data(as_text=True)
    antwort = client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN})
    assert antwort.status_code == 302
    assert BOT_TOKEN not in antwort.get_data(as_text=True)
    assert BOT_TOKEN not in antwort.headers["Location"]
    assert BOT_TOKEN not in client.get("/einstellungen").get_data(as_text=True)
    assert BOT_TOKEN not in zustand(client).get_data(as_text=True)


def test_die_seite_sagt_nicht_einmal_mehr_ob_der_token_steht(tmp_path):
    """Sie bekommt ihn nicht, also behauptet sie auch nichts über ihn — #230."""
    for wo, config in (
        ("ohne", Config(data_dir=tmp_path / "ohne")),
        ("mit", Config(discord_bot_token=BOT_TOKEN, data_dir=tmp_path / "mit")),
    ):
        html = gelesen(config, "/einstellungen")
        assert "er läuft oder er läuft nicht" in html, wo
        assert "Der Token ist" not in html, wo
        assert "Bot-Token gesetzt" not in html, wo


def test_kein_weg_schreibt_die_instanz_werte_in_die_datei(tmp_path):
    """Weder POST noch URL — den Schreibweg gibt es seit #230 nicht mehr."""
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    client.post(
        "/einstellungen",
        data={"discord_bot_token": BOT_TOKEN, "ollama_model": "gemma4:12b"},
    )
    antwort = client.get(f"/einstellungen?discord_bot_token={BOT_TOKEN}")
    assert antwort.status_code == 200
    assert BOT_TOKEN not in antwort.get_data(as_text=True)

    aktuell = settings.effective(config, runde(config))
    assert aktuell.discord_bot_token is None
    assert aktuell.ollama_model is None
    assert settings.stored(runde(config)) == {}
    roh = b""
    for datei in (config.database_path, config.database_path.with_suffix(".sqlite3-wal")):
        if datei.exists():
            roh += datei.read_bytes()
    assert BOT_TOKEN not in roh.decode("utf-8", errors="ignore")


def test_der_token_kommt_allein_aus_der_umgebung(tmp_path):
    config = Config(discord_bot_token=BOT_TOKEN, data_dir=tmp_path)
    assert settings.effective(config, runde(config)).discord_configured


def test_die_technikdetails_sagen_dass_hier_nichts_gespeichert_ist(tmp_path):
    config = Config(ollama_url="http://umgebung.example:11434", data_dir=tmp_path)
    html = zustand(create_app(config).test_client()).get_data(as_text=True)
    assert "gespeichert ist hier keiner von ihnen" in html
    assert "nur beim Bot, hier nicht sichtbar" in html
    # Und in Nutzersprache: kein Variablenname, obwohl es hier um die Einrichtung geht.
    assert systemsprache(html) == []


def test_die_einstellungen_stehen_hinter_demselben_tuersteher(tmp_path):
    client = bewacht(tmp_path)
    assert client.get("/einstellungen").status_code == 403
    assert client.post("/einstellungen", data={"admin_group": "wer-auch-immer"}).status_code == 403
    assert client.post("/einstellungen", data={"discord_bot_token": BOT_TOKEN}).status_code == 403
    assert settings.stored(runde(Config(data_dir=tmp_path))) == {}


def test_die_auswahlliste_der_modelle_ist_bewusst_entfallen(tmp_path):
    """Der eine Verlust aus #230 — festgehalten, damit er nicht als Fehler zurückkommt."""
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "<select" not in html
    assert "Eine Auswahlliste der vorhandenen Modelle gibt es hier nicht mehr" in html


def test_die_seite_fragt_ollama_gar_nicht_mehr(tmp_path, monkeypatch):
    """Sie hat nichts mehr zu wählen — und hängt deshalb an keinem Netzaufruf."""
    from chronicle.compose import client as sprachmodell

    def platzt(adresse, **kwargs):
        raise AssertionError("Die Betreiber-Seite darf Ollama nicht mehr fragen.")

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


def test_die_einrichtungshilfe_steht_auch_mit_token_da(tmp_path):
    """Die Seite weiß seit #230 nicht mehr, ob einer gesetzt ist — also blendet sie nichts aus."""
    config = Config(discord_bot_token=BOT_TOKEN, data_dir=tmp_path)
    html = gelesen(config, "/einstellungen")
    assert "Developer Portal" in html
    assert BOT_TOKEN not in html


def test_der_zustellkanal_wird_nicht_mehr_auf_der_betreiberseite_gepflegt(tmp_path):
    """Er gehört der Runde — gesetzt wird er in Discord (``bot.einrichten.kanal_setzen``)."""
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()

    assert 'name="discord_recap_channel"' not in client.get("/einstellungen").get_data(as_text=True)
    client.post("/einstellungen", data={"discord_recap_channel": "chronik"})

    assert settings.effective(config, runde(config)).discord_recap_channel is None


def test_das_ollama_dieser_box_wird_beim_namen_genannt(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    assert "Ollama dieser Box" in html
    assert f'value="{settings.DEFAULT_OLLAMA_URL}"' not in html


def test_eine_eigene_ollama_adresse_steht_da_ohne_feld(tmp_path):
    config = Config(ollama_url="http://ollama.example:11434", data_dir=tmp_path)
    html = gelesen(config, "/einstellungen")
    assert "http://ollama.example:11434" in html
    assert 'value="http://ollama.example:11434"' not in html


def test_der_alte_statuspfad_leitet_dauerhaft_auf_die_betreiberseite_um(tmp_path):
    antwort = create_app(Config(data_dir=tmp_path)).test_client().get("/status")
    assert antwort.status_code == 301
    assert antwort.headers["Location"] == "/einstellungen"


def test_die_wurzel_fuehrt_auf_die_einzige_seite_die_es_noch_gibt(tmp_path):
    """Der Proxy zeigt auf die Wurzel — dort ein 404 wäre eine schlechte Auskunft."""
    antwort = create_app(Config(data_dir=tmp_path)).test_client().get("/")
    assert antwort.status_code == 302
    assert antwort.headers["Location"] == "/einstellungen"


# --- Spielinhalte gibt es hier keine mehr (#157) --------------------------------------


# Was die spielende Gruppe betrifft, steht in Discord. Die Adressen sind nicht bloß leer,
# es gibt sie nicht — sonst bliebe eine halbe Oberfläche stehen, die niemand mehr pflegt.
FORT = (
    "/sitzungen/1",
    "/sitzungen/1/protokoll",
    "/protokolle",
    "/suche",
    "/register",
    "/register/vorschlaege",
    "/zuordnung",
    "/einrichtung",
    "/einrichtung/foundry",
)


def test_keine_seite_traegt_noch_spielinhalte(tmp_path):
    client = create_app(Config(data_dir=tmp_path)).test_client()
    for pfad in FORT:
        assert client.get(pfad).status_code == 404, pfad


def test_es_bleiben_genau_die_wege_der_betreiber_seite(tmp_path):
    app = create_app(Config(data_dir=tmp_path))
    wege = {regel.rule for regel in app.url_map.iter_rules() if regel.endpoint != "static"}
    assert wege == {"/", "/einstellungen", "/status", "/healthz"}


def test_die_oberflaeche_stoesst_keinen_lauf_mehr_an(config, welt):
    """Angestoßen wird in Discord — ``/chronik fertig`` und der nächtliche Lauf.

    Ein Knopf ohne Passwortfeld wäre nicht bloß tot, er warf das in Discord hinterlegte
    Passwort weg (``zugang.merken`` deutet leer als vergessen).
    """
    service.sync(config, runde(config), client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    app = create_app(config)
    wege = {regel.rule for regel in app.url_map.iter_rules()}
    assert not [weg for weg in wege if "abgleich" in weg or "chronik" in weg]

    client = app.test_client()
    assert client.post("/abgleich").status_code == 404
    for pfad in ("/", "/einstellungen", "/status"):
        html = client.get(pfad, follow_redirects=True).get_data(as_text=True)
        assert "/abgleich" not in html
        assert "Jetzt abgleichen" not in html


def test_kein_knopf_hier_wirft_weg_was_woanders_gepflegt_wurde(config):
    """Passwort **und** Einstellungen: ein Knopf ohne Feld nimmt beides nicht mit.

    Das Passwort liegt seit #96 aus ``/chronik start`` bereit, und ``zugang.merken``
    deutet ein leeres Feld als vergessen. Die Einstellungen kommen seit #89 aus
    ``/setup`` in Discord, und ``settings.save`` deutet einen leeren Wert als
    zurückgenommen. Beides trifft derselbe Fehler — ein POST ohne die Felder —, deshalb
    prüft der Rundumschlag über alle POST-Wege beides statt nur das Auffälligere (#117).

    Belegt wird über ``request.url_rule``, dass die Anfrage wirklich beim Handler ankam
    und nicht unterwegs an 404 oder 405 hängen blieb.
    """
    app = create_app(config)
    gruppe = runde(config)
    with app.test_client() as client:
        for regel in app.url_map.iter_rules():
            if "POST" not in regel.methods:
                continue
            assert not regel.arguments, f"{regel.rule}: kein Wert für {sorted(regel.arguments)}"
            zugang.merken(gruppe, PASSWORT)
            settings.save(gruppe, IN_DISCORD_GEPFLEGT)
            client.post(regel.rule)
            assert request.url_rule is regel, f"{regel.rule}: nicht beim Handler angekommen"
            assert zugang.ist_gemerkt(gruppe), regel.rule
            assert settings.stored(gruppe) == IN_DISCORD_GEPFLEGT, regel.rule


# --- Nach Nutzerfragen geschnitten ---------------------------------------------------


FRAGEN = (
    "Wie kommt Gesprochenes herein?",
    "Wer formuliert die Chronik?",
    "Wer darf verwalten?",
)


def test_die_einstellungen_sind_nach_nutzerfragen_geschnitten(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    for frage in FRAGEN:
        assert f"<h2>{frage}</h2>" in html
    assert html.index(FRAGEN[0]) < html.index(FRAGEN[1]) < html.index(FRAGEN[2])


def test_die_technikdetails_stehen_zugeklappt_am_ende(tmp_path):
    html = gelesen(Config(data_dir=tmp_path), "/einstellungen")
    block = html.split('<details class="karte">')[1]
    assert "<summary>Technikdetails</summary>" in block
    assert "Datenbank" in block
    assert "Schema-Stand" in block
    assert "Discord-Bot-Token" in block
    assert "Foundry-Adresse" not in block


# --- Nutzersprache: was hier steht, sagt was zu tun ist ------------------------------
#
# Die Wortliste steht in ``conftest``: derselbe Sweep läuft über die Antworten des Bots.


def test_keine_systemsprache_auf_einer_gerenderten_seite(tmp_path):
    config = Config(data_dir=tmp_path)
    app = create_app(config)
    client = app.test_client()

    for regel in app.url_map.iter_rules():
        if regel.endpoint in ("static", "healthz") or "GET" not in regel.methods:
            continue
        html = client.get(regel.rule, follow_redirects=True).get_data(as_text=True)
        assert systemsprache(html) == [], regel.rule


def test_auch_die_abweisung_spricht_nutzersprache(tmp_path):
    antwort = bewacht(tmp_path).get("/")
    assert antwort.status_code == 403
    assert systemsprache(antwort.get_data(as_text=True)) == []


def test_ein_abgleich_ohne_zugang_erklaert_das_ohne_variablennamen(tmp_path):
    config = Config(data_dir=tmp_path)
    stand = service.sync(config, runde(config))
    assert stand.stale
    assert "Für den Zugang zu Foundry fehlen noch" in stand.message
    assert systemsprache(stand.message) == []


# --- Verwaltungsrolle: die Betreiber-Seite ist kein Ort für alle ----------------------
#
# **Innerhalb** einer Runde entscheidet Discord über seine eigenen Rechte (#62). Was
# hier bleibt, ist die Frage, wer an den Bot-Token darf — die gehört dem Betrieb.


GRUPPE = "chronik-verwaltung"
MITSPIEL = {"Remote-Groups": "spieler"}
VERWALTUNG = {"Remote-Groups": f"Spieler, {GRUPPE.upper()} "}


def mit_gruppe(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    instanz.save_admin_group(config.database_path, GRUPPE)
    return config, create_app(config).test_client()


def test_ohne_gesetzte_gruppe_darf_jeder_alles(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    client = create_app(config).test_client()
    assert client.get("/einstellungen", headers=MITSPIEL).status_code == 200


def test_mit_gruppe_bleibt_die_verwaltung_der_gruppe_vorbehalten(tmp_path):
    _, client = mit_gruppe(tmp_path)
    assert client.get("/einstellungen", headers=MITSPIEL).status_code == 403
    assert client.get("/einstellungen", headers=VERWALTUNG).status_code == 200


def test_auch_der_speicherweg_steht_hinter_der_rolle(tmp_path):
    _, client = mit_gruppe(tmp_path)
    assert client.post("/einstellungen", data={}, headers=MITSPIEL).status_code == 403


def test_das_install_gate_bleibt_fuer_jeden_offen(tmp_path):
    """``/healthz`` fragt die Box, nicht ein Mensch — es steht vor jeder Rolle."""
    _, client = mit_gruppe(tmp_path)
    assert client.get("/healthz", headers=MITSPIEL).status_code == 200


def test_der_direkte_aufruf_sagt_in_nutzersprache_wer_helfen_kann(tmp_path):
    _, client = mit_gruppe(tmp_path)
    antwort = client.get("/einstellungen", headers=MITSPIEL)
    assert antwort.status_code == 403
    html = antwort.get_data(as_text=True)
    assert "Das macht die Verwaltung" in html
    assert "Freischalten kann dich" in html
    assert "Benutzerverwaltung der Box" in html
    assert systemsprache(html) == []


def test_wer_sich_selbst_aussperren_wuerde_wird_gewarnt(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    antwort = client.post(
        "/einstellungen",
        data={"admin_group": GRUPPE, "ollama_model": "gemma4:12b"},
        headers=MITSPIEL,
    )
    assert antwort.status_code == 200
    assert "stehst du selbst nicht" in antwort.get_data(as_text=True)
    assert instanz.admin_group(config.database_path) == ""
    # Und was daneben im Formular stand, wird seit #230 überhaupt nicht mehr gespeichert.
    assert settings.stored(runde(config)) == {}


def test_die_warnung_laesst_sich_bewusst_uebergehen(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    antwort = client.post(
        "/einstellungen",
        data={"admin_group": GRUPPE, "gruppe_bestaetigt": "ja"},
        headers=MITSPIEL,
    )
    assert antwort.status_code == 302
    assert instanz.admin_group(config.database_path) == GRUPPE


def test_wer_die_gruppe_selbst_traegt_speichert_ohne_warnung(tmp_path):
    config = Config(data_dir=tmp_path)
    client = create_app(config).test_client()
    antwort = client.post("/einstellungen", data={"admin_group": GRUPPE}, headers=VERWALTUNG)
    assert antwort.status_code == 302
    assert instanz.admin_group(config.database_path) == GRUPPE


def test_die_gruppe_zurueckzunehmen_warnt_nicht(tmp_path):
    config, client = mit_gruppe(tmp_path)
    antwort = client.post("/einstellungen", data={"admin_group": "  "}, headers=VERWALTUNG)
    assert antwort.status_code == 302
    assert instanz.admin_group(config.database_path) == ""


def test_ein_formular_ohne_das_feld_nimmt_die_rolle_nicht_still_zurueck(tmp_path):
    config, client = mit_gruppe(tmp_path)
    client.post("/einstellungen", data={"ollama_model": "gemma4:12b"}, headers=VERWALTUNG)
    assert instanz.admin_group(config.database_path) == GRUPPE


def test_das_image_ruft_eine_app_die_es_wirklich_gibt():
    """``--call`` im Dockerfile nennt einen Namen, den es hier geben muss.

    Bis #229 hieß er ``dienst`` und hing den nächtlichen Lauf an; der hängt jetzt am
    Bot-Prozess, und der Name ist mit ihm gegangen. Ein Name, der nicht mehr stimmt,
    fiele erst auf der Box auf — und dann steht die Seite nicht.
    """
    dockerfile = (WURZEL / "Dockerfile").read_text(encoding="utf-8")
    ziel = re.search(r"--call ([\w.]+:[\w.]+)", dockerfile).group(1)
    modul, _, name = ziel.partition(":")

    assert callable(getattr(importlib.import_module(modul), name))
