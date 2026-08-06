"""Vorrang, Quellenauskunft und das Behalten des Passworts.

Kein Wert hier ist ein echtes Geheimnis — sie stehen alle nur in diesem Test.
"""

import pytest

from chronicle import db, settings
from chronicle.config import Config

AUS_DER_UMGEBUNG = "passwort-aus-der-umgebung"
AUS_DEM_FRONTEND = "passwort-aus-dem-frontend"


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(
        foundry_url="https://umgebung.example",
        foundry_user="umgebungs-konto",
        foundry_password=AUS_DER_UMGEBUNG,
        ollama_url="http://umgebung.example:11434",
        ollama_model="umgebungs-modell",
        data_dir=tmp_path,
    )
    db.init(gesetzt.database_path)
    return gesetzt


def test_ohne_eintrag_bleibt_die_umgebung_stehen(config):
    assert settings.effective(config) == config


def test_ein_gesetzter_wert_schlaegt_die_umgebung(config):
    settings.save(
        config.database_path,
        {"foundry_url": "https://frontend.example", "foundry_password": AUS_DEM_FRONTEND},
    )
    aktuell = settings.effective(config)
    assert aktuell.foundry_url == "https://frontend.example"
    assert aktuell.foundry_password == AUS_DEM_FRONTEND
    assert aktuell.foundry_user == "umgebungs-konto"


def test_ein_leerer_wert_nimmt_den_eintrag_zurueck(config):
    settings.save(config.database_path, {"foundry_url": "https://frontend.example"})
    settings.save(config.database_path, {"foundry_url": "   "})
    assert settings.effective(config).foundry_url == "https://umgebung.example"
    assert settings.stored(config.database_path) == {}


def test_speichern_ist_wiederholbar(config):
    settings.save(config.database_path, {"ollama_model": "gemma4:12b"})
    settings.save(config.database_path, {"ollama_model": "gemma4:e4b"})
    assert settings.effective(config).ollama_model == "gemma4:e4b"


def test_unbekannte_schluessel_landen_nicht_in_der_tabelle(config):
    settings.save(config.database_path, {"whisper_model": "nicht-hier"})
    assert settings.stored(config.database_path) == {}


def test_die_quelle_steht_je_wert_fest(config, tmp_path):
    settings.save(config.database_path, {"foundry_url": "https://frontend.example"})
    quellen = settings.sources(config)
    assert quellen["foundry_url"] == settings.FRONTEND
    assert quellen["foundry_user"] == settings.UMGEBUNG

    leer = Config(data_dir=tmp_path / "leer")
    db.init(leer.database_path)
    assert settings.sources(leer)["foundry_url"] == settings.UNGESETZT


def test_die_gespeicherten_werte_stehen_fest():
    assert settings.KEYS == (
        "foundry_url",
        "foundry_user",
        "foundry_password",
        "discord_bot_token",
        "discord_recap_channel",
        "ollama_url",
        "ollama_model",
    )
    assert settings.SECRET_KEYS == ("foundry_password", "discord_bot_token")


def test_der_zustellkanal_kommt_aus_der_oberflaeche_und_ist_kein_geheimnis(config):
    settings.save(config.database_path, {"discord_recap_channel": "chronik"})
    assert settings.effective(config).discord_recap_channel == "chronik"
    assert settings.sources(config)["discord_recap_channel"] == settings.FRONTEND


def test_der_bot_token_kommt_aus_der_oberflaeche_und_schlaegt_die_umgebung(config):
    aus_der_umgebung = Config(discord_bot_token="token-aus-der-umgebung", data_dir=config.data_dir)
    assert settings.effective(aus_der_umgebung).discord_bot_token == "token-aus-der-umgebung"

    settings.save(config.database_path, {"discord_bot_token": "token-aus-dem-frontend"})
    aktuell = settings.effective(aus_der_umgebung)
    assert aktuell.discord_bot_token == "token-aus-dem-frontend"
    assert aktuell.discord_configured
    assert settings.is_set(aus_der_umgebung, "discord_bot_token")


def test_is_set_sagt_ob_aber_nicht_was(config, tmp_path):
    assert settings.is_set(config, "foundry_password")
    leer = Config(data_dir=tmp_path / "ohne-passwort")
    db.init(leer.database_path)
    assert not settings.is_set(leer, "foundry_password")


def test_ein_zweiter_schemalauf_laesst_die_einstellungen_stehen(config):
    settings.save(config.database_path, {"foundry_user": "frontend-konto"})
    db.init(config.database_path)
    assert settings.effective(config).foundry_user == "frontend-konto"
    assert db.current_schema_version(config.database_path) == db.SCHEMA_VERSION
