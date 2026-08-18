"""Vorrang, Quellenauskunft — und dass das Foundry-Passwort hier keinen Platz mehr hat.

Kein Wert hier ist ein echtes Geheimnis — sie stehen alle nur in diesem Test.
"""

import pytest
from conftest import runde

from chronicle import db, settings
from chronicle.config import Config

AUS_DEM_FRONTEND = "konto-aus-dem-frontend"


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(
        foundry_url="https://umgebung.example",
        foundry_user="umgebungs-konto",
        ollama_url="http://umgebung.example:11434",
        ollama_model="umgebungs-modell",
        data_dir=tmp_path,
    )
    db.init(gesetzt.database_path)
    return gesetzt


def test_ohne_eintrag_bleibt_die_umgebung_stehen(config):
    assert settings.effective(config, runde(config)) == config


def test_ein_gesetzter_wert_schlaegt_die_umgebung(config):
    settings.save(
        runde(config),
        {"foundry_url": "https://frontend.example", "foundry_user": AUS_DEM_FRONTEND},
    )
    aktuell = settings.effective(config, runde(config))
    assert aktuell.foundry_url == "https://frontend.example"
    assert aktuell.foundry_user == AUS_DEM_FRONTEND
    assert aktuell.ollama_model == "umgebungs-modell"


def test_ein_leerer_wert_nimmt_den_eintrag_zurueck(config):
    settings.save(runde(config), {"foundry_url": "https://frontend.example"})
    settings.save(runde(config), {"foundry_url": "   "})
    assert settings.effective(config, runde(config)).foundry_url == "https://umgebung.example"
    assert settings.stored(runde(config)) == {}


def test_speichern_ist_wiederholbar(config):
    settings.save(runde(config), {"ollama_model": "gemma4:12b"})
    settings.save(runde(config), {"ollama_model": "gemma4:e4b"})
    assert settings.effective(config, runde(config)).ollama_model == "gemma4:e4b"


def test_unbekannte_schluessel_landen_nicht_in_der_tabelle(config):
    settings.save(runde(config), {"whisper_model": "nicht-hier"})
    assert settings.stored(runde(config)) == {}


def test_die_quelle_steht_je_wert_fest(config, tmp_path):
    settings.save(runde(config), {"foundry_url": "https://frontend.example"})
    quellen = settings.sources(config, runde(config))
    assert quellen["foundry_url"] == settings.FRONTEND
    assert quellen["foundry_user"] == settings.UMGEBUNG

    leer = Config(data_dir=tmp_path / "leer")
    db.init(leer.database_path)
    assert settings.sources(leer, runde(leer))["foundry_url"] == settings.UNGESETZT
    assert settings.sources(leer, runde(leer))["ollama_url"] == settings.STANDARD


def test_ohne_eigene_adresse_gilt_das_ollama_dieser_box(tmp_path):
    leer = Config(data_dir=tmp_path / "leer")
    db.init(leer.database_path)
    assert settings.effective(leer, runde(leer)).ollama_url == settings.DEFAULT_OLLAMA_URL


def test_eine_eigene_adresse_schlaegt_den_standard(config):
    assert settings.effective(config, runde(config)).ollama_url == "http://umgebung.example:11434"
    settings.save(runde(config), {"ollama_url": "http://frontend.example:11434"})
    assert settings.effective(config, runde(config)).ollama_url == "http://frontend.example:11434"


def test_eine_zurueckgenommene_adresse_faellt_auf_den_standard_zurueck(tmp_path):
    leer = Config(data_dir=tmp_path / "zurueck")
    db.init(leer.database_path)
    settings.save(runde(leer), {"ollama_url": "http://frontend.example:11434"})
    settings.save(runde(leer), {"ollama_url": "  "})
    assert settings.effective(leer, runde(leer)).ollama_url == settings.DEFAULT_OLLAMA_URL


@pytest.mark.parametrize(
    "wert",
    [
        "http://foundry.example",
        "https://foundry.example/",
        "http://foundry.example:30000",
        "http://192.0.2.7:30000/",
    ],
)
def test_die_wurzel_mit_schema_ist_eine_brauchbare_adresse(wert):
    assert settings.brauchbare_adresse(wert)


@pytest.mark.parametrize(
    "wert",
    [
        "",
        "foundry.example:30000",
        "foundry.example:30000/modules/login.html?world=daggerheart",
        "http://foundry.example:30000/join",
        "https://foundry.example/?world=daggerheart",
        "https://foundry.example/#anmelden",
        "ftp://foundry.example",
        "http://",
        "http://foundry.example:dreissigtausend",
    ],
)
def test_was_sich_nicht_zerlegen_laesst_wird_abgewiesen(config, wert):
    """Abgewiesen heißt: die bisherige bleibt stehen — wie bei einer unbekannten Zone."""
    assert not settings.brauchbare_adresse(wert)
    assert not settings.save_foundry_url(runde(config), wert)
    assert settings.effective(config, runde(config)).foundry_url == "https://umgebung.example"


def test_die_gespeicherten_werte_stehen_fest():
    assert settings.KEYS == (
        "foundry_url",
        "foundry_user",
        "discord_bot_token",
        "discord_recap_channel",
        "ollama_url",
        "ollama_model",
    )
    assert settings.SECRET_KEYS == ("discord_bot_token",)


def test_der_runde_bleibt_ihr_spiel_der_instanz_ihre_infrastruktur():
    """#87: Ollama steht neben dem Bot-Token, nicht neben der Foundry-Adresse."""
    assert settings.INSTANZ_KEYS == ("discord_bot_token", "ollama_url", "ollama_model")
    assert settings.RUNDEN_KEYS == ("foundry_url", "foundry_user", "discord_recap_channel")


def test_der_zustellkanal_kommt_aus_der_oberflaeche_und_ist_kein_geheimnis(config):
    settings.save(runde(config), {"discord_recap_channel": "chronik"})
    assert settings.effective(config, runde(config)).discord_recap_channel == "chronik"
    assert settings.sources(config, runde(config))["discord_recap_channel"] == settings.FRONTEND


def test_der_bot_token_kommt_aus_der_oberflaeche_und_schlaegt_die_umgebung(config):
    aus_der_umgebung = Config(discord_bot_token="token-aus-der-umgebung", data_dir=config.data_dir)
    assert (
        settings.effective(aus_der_umgebung, runde(aus_der_umgebung)).discord_bot_token
        == "token-aus-der-umgebung"
    )

    settings.save(runde(config), {"discord_bot_token": "token-aus-dem-frontend"})
    aktuell = settings.effective(aus_der_umgebung, runde(aus_der_umgebung))
    assert aktuell.discord_bot_token == "token-aus-dem-frontend"
    assert aktuell.discord_configured
    assert settings.is_set(aus_der_umgebung, runde(aus_der_umgebung), "discord_bot_token")


def test_is_set_sagt_ob_aber_nicht_was(config, tmp_path):
    settings.save(runde(config), {"discord_bot_token": "platzhalter-token"})
    assert settings.is_set(config, runde(config), "discord_bot_token")
    leer = Config(data_dir=tmp_path / "ohne-token")
    db.init(leer.database_path)
    assert not settings.is_set(leer, runde(leer), "discord_bot_token")


def test_das_foundry_passwort_laesst_sich_gar_nicht_erst_speichern(config):
    """Der Schlüsselraum kennt es nicht — ein Aufrufer kann es nicht hineinschreiben."""
    settings.save(runde(config), {"foundry_password": "steht-nur-in-diesem-test"})
    assert settings.stored(runde(config)) == {}
    assert "foundry_password" not in settings.KEYS
    assert not hasattr(settings.effective(config, runde(config)), "foundry_password")


def test_ein_alter_bestand_wird_beim_start_geloescht(config):
    """Was vor #64 gepflegt wurde, liegt im Klartext in der Datei — es wird verworfen."""
    verworfen = "altbestand-nur-in-diesem-test"
    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO settings (runde_id, key, value) VALUES (?, 'foundry_password', ?)",
                (runde(config).id, verworfen),
            )
    finally:
        connection.close()

    db.init(config.database_path)

    connection = db.connect(config.database_path)
    try:
        zeilen = connection.execute("SELECT key, value FROM settings").fetchall()
    finally:
        connection.close()
    assert [z["key"] for z in zeilen if z["key"] == "foundry_password"] == []
    assert verworfen not in config.database_path.read_bytes().decode("utf-8", errors="ignore")
    # Ein zweiter Lauf findet nichts mehr zu tun und beschwert sich nicht.
    db.init(config.database_path)


def test_ein_zweiter_schemalauf_laesst_die_einstellungen_stehen(config):
    settings.save(runde(config), {"foundry_user": "frontend-konto"})
    db.init(config.database_path)
    assert settings.effective(config, runde(config)).foundry_user == "frontend-konto"
    assert db.current_schema_version(config.database_path) == db.SCHEMA_VERSION
