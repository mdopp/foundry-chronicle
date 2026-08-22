from pathlib import Path

from chronicle.config import DEFAULT_TTS_URL, FOUNDRY_VARIABLES, Config

VOLLSTAENDIG = {
    "FOUNDRY_URL": "https://foundry.example",
    "FOUNDRY_USER": "chronist",
    "DISCORD_BOT_TOKEN": "bot-token-aus-der-umgebung",
    "OLLAMA_URL": "http://ollama.example:11434",
    "OLLAMA_MODEL": "chronist-modell",
    "CHRONICLE_DATA_DIR": "/srv/chronik",
}


def test_liest_alles_aus_der_umgebung():
    config = Config.from_env(VOLLSTAENDIG)
    assert config.foundry_url == "https://foundry.example"
    assert config.foundry_user == "chronist"
    assert config.discord_bot_token == "bot-token-aus-der-umgebung"
    assert config.data_dir == Path("/srv/chronik")
    assert config.foundry_configured
    assert config.discord_configured


def test_zustellkanal_und_oeffentliche_adresse_kommen_aus_der_umgebung():
    config = Config.from_env(
        dict(
            VOLLSTAENDIG,
            DISCORD_RECAP_CHANNEL="chronik",
            CHRONICLE_PUBLIC_URL="https://chronik.example",
        )
    )
    assert config.discord_recap_channel == "chronik"
    assert config.public_url == "https://chronik.example"
    assert Config.from_env({}).discord_recap_channel is None
    assert Config.from_env({}).public_url is None


def test_ohne_umgebung_ist_foundry_unkonfiguriert():
    config = Config.from_env({})
    assert not config.foundry_configured
    assert config.missing_foundry_variables == ("FOUNDRY_URL", "FOUNDRY_USER")


def test_nennt_nur_die_fehlenden_variablen():
    config = Config.from_env({"FOUNDRY_URL": "https://foundry.example"})
    assert config.missing_foundry_variables == ("FOUNDRY_USER",)


def test_kennt_die_fehlenden_werte_auch_unter_ihrem_feldnamen():
    """Was fehlt, muss auch sagbar sein, ohne eine Umgebungsvariable zu nennen."""
    assert Config.from_env({}).missing_foundry_fields == ("die Adresse", "der Benutzer")
    halb = Config.from_env({"FOUNDRY_URL": "https://foundry.example"})
    assert halb.missing_foundry_fields == ("der Benutzer",)


def test_leerer_string_zaehlt_als_fehlend():
    config = Config.from_env(dict(VOLLSTAENDIG, FOUNDRY_USER="   "))
    assert config.foundry_user is None
    assert not config.foundry_configured


def test_das_foundry_passwort_kommt_aus_keiner_umgebungsvariable():
    """Es gibt kein Feld dafür: eine Variable stünde dauerhaft in der Dienstbeschreibung."""
    config = Config.from_env(dict(VOLLSTAENDIG, FOUNDRY_PASSWORD="steht-nur-in-diesem-test"))
    assert not hasattr(config, "foundry_password")
    assert "steht-nur-in-diesem-test" not in repr(config)
    assert "FOUNDRY_PASSWORD" not in FOUNDRY_VARIABLES
    assert config.foundry_configured


def test_discord_darf_fehlen():
    ohne_bot = {name: wert for name, wert in VOLLSTAENDIG.items() if name != "DISCORD_BOT_TOKEN"}
    config = Config.from_env(ohne_bot)
    assert config.foundry_configured
    assert not config.discord_configured


def test_ollama_kommt_aus_der_umgebung():
    config = Config.from_env(VOLLSTAENDIG)
    assert config.ollama_url == "http://ollama.example:11434"
    assert config.ollama_model == "chronist-modell"
    assert config.ollama_configured


def test_die_adresse_des_sprachdienstes_kommt_aus_der_umgebung():
    config = Config.from_env(dict(VOLLSTAENDIG, TTS_URL="http://tts.example:8881"))
    assert config.tts_url == "http://tts.example:8881"


def test_ohne_gesetzte_adresse_gilt_der_sprachdienst_dieser_box():
    """Kein Wert heißt nicht »keine Ansage« — ``ansage.datei`` nimmt dann die Vorgabe."""
    assert Config.from_env({}).tts_url is None
    assert DEFAULT_TTS_URL == "http://127.0.0.1:8881"


def test_ollama_darf_fehlen():
    assert not Config.from_env({}).ollama_configured


def test_ohne_modellnamen_ist_ollama_unkonfiguriert():
    assert not Config.from_env(dict(VOLLSTAENDIG, OLLAMA_MODEL=" ")).ollama_configured


def test_eine_fehlende_adresse_haelt_ollama_nicht_auf():
    """Die Adresse löst immer auf — offen bleibt allein die Modellwahl."""
    config = Config.from_env({"OLLAMA_MODEL": "chronist-modell"})
    assert config.ollama_configured


def test_die_ollama_adresse_ist_konfiguration_und_wird_nicht_maskiert():
    assert "http://ollama.example:11434" in repr(Config.from_env(VOLLSTAENDIG))


def test_datenverzeichnis_hat_eine_vorgabe():
    config = Config.from_env({})
    assert config.data_dir == Path("data")
    assert config.database_path == Path("data/chronicle.sqlite3")


def test_from_env_nutzt_ohne_argument_die_prozessumgebung(monkeypatch):
    monkeypatch.setenv("FOUNDRY_URL", "https://foundry.example")
    monkeypatch.delenv("FOUNDRY_USER", raising=False)
    assert Config.from_env().foundry_url == "https://foundry.example"


def test_remote_user_wird_standardmaessig_nicht_erzwungen():
    assert not Config.from_env({}).require_remote_user


def test_remote_user_erzwingen_laesst_sich_schalten():
    for wert in ("1", "true", "TRUE", "yes", "on"):
        assert Config.from_env({"CHRONICLE_REQUIRE_REMOTE_USER": wert}).require_remote_user
    for wert in ("0", "false", "nein", "", "  "):
        assert not Config.from_env({"CHRONICLE_REQUIRE_REMOTE_USER": wert}).require_remote_user


def test_ohne_eintrag_zaehlt_die_maschine_selbst():
    """Leer heißt errechnet — eine abgeschriebene Adresse wäre nach der DHCP-Lease falsch."""
    assert Config.from_env({}).trusted_proxies == ()


def test_der_proxy_laesst_sich_nachtragen():
    config = Config.from_env({"CHRONICLE_TRUSTED_PROXIES": " 192.168.178.100, 10.0.0.0/8 ,"})
    assert config.trusted_proxies == ("192.168.178.100", "10.0.0.0/8")


def test_der_port_des_install_gates_kommt_aus_der_umgebung():
    # Ohne ihn bindet der Bot nichts — auf einer Entwicklungsmaschine braucht das Gate
    # niemand, und ein belegter Port wäre nur lästig. Die Vorlage setzt ihn (#228).
    assert Config.from_env({}).health_port is None
    assert Config.from_env({"CHRONICLE_HEALTH_PORT": " 8701 "}).health_port == 8701


def test_der_mitschnitt_ist_aus_bis_ihn_jemand_einschaltet():
    # Ein Mitschnitt ist der Weltabzug in Serie — Klarnamen, Geflüster, GM-Inhalte (#242).
    # Was so etwas anlegt, fängt nicht von selbst damit an.
    assert Config.from_env({}).foundry_mitschnitt is False
    assert Config.from_env({"CHRONICLE_FOUNDRY_MITSCHNITT": "1"}).foundry_mitschnitt is True


def test_repr_maskiert_den_token():
    text = repr(Config.from_env(VOLLSTAENDIG))
    assert "bot-token-aus-der-umgebung" not in text
    assert text.count("'***'") == 1
    assert "chronist" in text


def test_str_maskiert_ebenfalls():
    assert "bot-token-aus-der-umgebung" not in str(Config.from_env(VOLLSTAENDIG))


def test_repr_ohne_geheimnisse_zeigt_none():
    assert "discord_bot_token=None" in repr(Config.from_env({}))
