from pathlib import Path

from chronicle.config import (
    DEFAULT_TTS_URL,
    FOUNDRY_VARIABLES,
    Config,
)

VOLLSTAENDIG = {
    "FOUNDRY_URL": "https://foundry.example",
    "FOUNDRY_USER": "chronist",
    "DISCORD_BOT_TOKEN": "bot-token-aus-der-umgebung",
    "MODEL_URL": "http://host.containers.internal:11435/v1",
    "MODEL_PRESET": "gemma-4-12b",
    "MODEL_LEASE": "http://host.containers.internal:11435/api/model-lease",
    "MODEL_KEY": "modell-schluessel-aus-der-umgebung",
    "CHRONICLE_WHISPER_KEY": "erkenner-schluessel-aus-der-umgebung",
    "CHRONICLE_DATA_DIR": "/srv/chronicle",
}


def test_liest_alles_aus_der_umgebung():
    config = Config.from_env(VOLLSTAENDIG)
    assert config.foundry_url == "https://foundry.example"
    assert config.foundry_user == "chronist"
    assert config.discord_bot_token == "bot-token-aus-der-umgebung"
    assert config.data_dir == Path("/srv/chronicle")
    assert config.foundry_configured
    assert config.discord_configured


def test_der_zustellkanal_kommt_aus_der_umgebung():
    config = Config.from_env(dict(VOLLSTAENDIG, DISCORD_RECAP_CHANNEL="chronik"))
    assert config.discord_recap_channel == "chronik"
    assert Config.from_env({}).discord_recap_channel is None


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


def test_der_modelldienst_kommt_aus_den_eingebauten_werten_der_box():
    """#362: Laib gibt jedem Dienst ``${MODEL_URL}``, ``${MODEL_LEASE}``, ``${MODEL_KEY}``."""
    config = Config.from_env(VOLLSTAENDIG)
    assert config.model_url == "http://host.containers.internal:11435/v1"
    assert config.model_lease == "http://host.containers.internal:11435/api/model-lease"
    assert config.model_key == "modell-schluessel-aus-der-umgebung"
    assert config.model_preset == "gemma-4-12b"
    assert config.whisper_key == "erkenner-schluessel-aus-der-umgebung"
    assert config.model_configured


def test_die_alten_ollama_namen_liest_niemand_mehr():
    """Keine zweite Quelle: was die Box nicht unter Laibs Namen nennt, gilt nicht."""
    alt = Config.from_env({"OLLAMA_URL": "http://alt.example:11434", "OLLAMA_MODEL": "x"})
    assert not alt.model_configured
    assert alt.model_url is None and alt.model_preset is None


def test_die_adresse_des_sprachdienstes_kommt_aus_der_umgebung():
    config = Config.from_env(dict(VOLLSTAENDIG, TTS_URL="tcp://host.containers.internal:10200"))
    assert config.tts_url == "tcp://host.containers.internal:10200"


def test_ohne_gesetzte_adresse_gilt_der_sprachdienst_dieser_box():
    """Kein Wert heißt nicht »keine Ansage« — ``ansage.datei`` nimmt dann die Vorgabe."""
    assert Config.from_env({}).tts_url is None
    assert DEFAULT_TTS_URL == "tcp://127.0.0.1:10200"


def test_der_modelldienst_darf_fehlen():
    assert not Config.from_env({}).model_configured


def test_ohne_preset_ist_der_modelldienst_unkonfiguriert():
    assert not Config.from_env(dict(VOLLSTAENDIG, MODEL_PRESET=" ")).model_configured


def test_ohne_adresse_wird_keine_geraten():
    """Bis #362 löste die Adresse immer auf: die Schleife dieser Box. Laib nennt sie selbst."""
    assert not Config.from_env({"MODEL_PRESET": "gemma-4-12b"}).model_configured


def test_die_adressen_sind_konfiguration_und_werden_nicht_maskiert():
    text = repr(Config.from_env(VOLLSTAENDIG))
    assert "http://host.containers.internal:11435/v1" in text
    assert "http://host.containers.internal:11435/api/model-lease" in text


def test_datenverzeichnis_hat_eine_vorgabe():
    config = Config.from_env({})
    assert config.data_dir == Path("data")
    assert config.database_path == Path("data/chronicle.sqlite3")


def test_from_env_nutzt_ohne_argument_die_prozessumgebung(monkeypatch):
    monkeypatch.setenv("FOUNDRY_URL", "https://foundry.example")
    monkeypatch.delenv("FOUNDRY_USER", raising=False)
    assert Config.from_env().foundry_url == "https://foundry.example"


def test_die_haustuer_hinterlaesst_keinen_schalter():
    """Was der Betreiber-Seite gehörte, liest hier niemand mehr (#231).

    ``CHRONICLE_REQUIRE_REMOTE_USER`` und ``CHRONICLE_TRUSTED_PROXIES`` gehörten zum
    Türsteher (#190), ``CHRONICLE_PUBLIC_URL`` zur Subdomain davor. Ein Feld, das die
    Variable weiter einliest, ohne dass sie etwas bewirkt, ist eine falsche Zusage — und
    genau die Sorte, die drei Monate später jemand für einen Schutz hält.
    """
    gesetzt = {
        "CHRONICLE_REQUIRE_REMOTE_USER": "1",
        "CHRONICLE_TRUSTED_PROXIES": "192.168.178.100",
        "CHRONICLE_PUBLIC_URL": "https://chronicle.example",
    }
    config = Config.from_env(gesetzt)
    for feld in ("require_remote_user", "trusted_proxies", "public_url"):
        assert not hasattr(config, feld), feld
    assert "192.168.178.100" not in repr(config)


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


def test_das_sitzungsfenster_ist_an_bis_es_jemand_abschaltet():
    # Umgekehrt als beim Mitschnitt (#299): der Vertrag mit dem Nachbardienst ist von
    # beiden Betreibern entschieden, der Schalter ist der Weg **heraus**. Eine leere
    # Variable heißt deshalb »an« — sonst schaltete jede Box ohne die Variable stumm ab.
    assert Config.from_env({}).gpu_lease is True
    assert Config.from_env({"CHRONICLE_GPU_LEASE": ""}).gpu_lease is True
    assert Config.from_env({"CHRONICLE_GPU_LEASE": "an"}).gpu_lease is True
    assert Config.from_env({"CHRONICLE_GPU_LEASE": "true"}).gpu_lease is True
    assert Config.from_env({"CHRONICLE_GPU_LEASE": "aus"}).gpu_lease is False
    assert Config.from_env({"CHRONICLE_GPU_LEASE": "0"}).gpu_lease is False


def test_es_gibt_keinen_backend_schalter_mehr():
    # #316 stellte ihn auf, weil zwei Wege nebeneinander liefen; #329 hat den zweiten
    # abgeschaltet. Ein Schalter mit genau einem gültigen Wert bleibt nicht als Höflichkeit
    # stehen: wer »ollama« in das Textfeld tippte, bekäme still den einen Weg, und die Box
    # täte etwas anderes als das, was dort steht. Der Test hält fest, dass die Variable
    # wirklich nirgends mehr gelesen wird — sonst käme sie über eine Vorgabe zurück.
    assert not hasattr(Config(), "llm_backend")
    assert "CHRONICLE_LLM_BACKEND" not in repr(Config.from_env({"CHRONICLE_LLM_BACKEND": "ollama"}))
    assert "llm_backend" not in repr(Config())


def test_repr_maskiert_den_token():
    text = repr(Config.from_env(VOLLSTAENDIG))
    assert "bot-token-aus-der-umgebung" not in text
    # Der Bot-Token und die beiden Schlüssel (#362) — keiner davon im Klartext.
    assert "modell-schluessel-aus-der-umgebung" not in text
    assert "erkenner-schluessel-aus-der-umgebung" not in text
    assert text.count("'***'") == 3
    assert "chronist" in text


def test_str_maskiert_ebenfalls():
    assert "bot-token-aus-der-umgebung" not in str(Config.from_env(VOLLSTAENDIG))


def test_repr_ohne_geheimnisse_zeigt_none():
    assert "discord_bot_token=None" in repr(Config.from_env({}))
