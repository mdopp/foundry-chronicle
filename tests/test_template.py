"""Die Laib-Vorlage ``templates/chronik`` — geprüft, soweit es ohne Box geht (#362).

Was ``laib template lint`` prüft, prüft Laib: dieser Lauf steht im CI-Job ``laib`` neben
dem Quadlet-``-dryrun`` und dem echten Start unter Podman (``scripts/laib_start.sh``).
Hierher gehört, was nur dieses Repo wissen kann: dass die Vorlage genau die Namen setzt, die
``Config.from_env`` liest, dass kein Geheimnis als Wert dasteht, dass die Daten in Laibs
Ordnung liegen und kein Pfad der ServiceBay-Zeit übrig ist — und dass der Erkenner daneben
ohne seinen Schlüssel niemanden hereinlässt.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from chronicle import config as konfiguration

WURZEL = Path(__file__).resolve().parents[1]
VORLAGE = WURZEL / "templates" / "chronik"

# Was Laib jedem Dienst selbst gibt (laib ``internal/state/load.go``, ``Builtins``).
EINGEBAUT = {
    "DOMAIN",
    "DATA_DIR",
    "TZ",
    "SERVICE",
    "LAN_IP",
    "MODEL_URL",
    "MODEL_KEY",
    "MODEL_LEASE",
}

PLATZHALTER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

LESER = re.compile(r"_(?:value|port|flag|schalter)\(env, (\"[A-Z_]+\"|[A-Z_]+)")


def _einheit(name: str) -> str:
    return (VORLAGE / name).read_text(encoding="utf-8")


def _umgebung(name: str) -> dict[str, str]:
    paare = (
        zeile.removeprefix("Environment=").split("=", 1)
        for zeile in _einheit(name).splitlines()
        if zeile.startswith("Environment=")
    )
    return dict(paare)


def _gelesen() -> set[str]:
    """Jede Variable, die ``Config.from_env`` liest — als Literal oder über ihre Konstante."""
    quelle = (WURZEL / "src/chronicle/config.py").read_text(encoding="utf-8")
    return {
        name.strip('"') if name.startswith('"') else getattr(konfiguration, name)
        for name in LESER.findall(quelle)
    }


@pytest.fixture
def vertrag() -> dict:
    return yaml.safe_load(_einheit("laib.yml"))


class _Werte(yaml.SafeLoader):
    pass


_Werte.add_constructor("!secret", lambda lader, knoten: ("!secret", lader.construct_scalar(knoten)))


@pytest.fixture
def werte() -> dict:
    return yaml.load(_einheit("values.example.yml"), Loader=_Werte)  # noqa: S506 — SafeLoader


def test_der_dienst_heisst_wie_sein_ordner_und_seine_daten(vertrag: dict) -> None:
    """laib ADR 0021: ein Dienst ist ein deutsches Nomen, und das Nomen ist alles an ihm."""
    assert vertrag["name"] == "chronik" == VORLAGE.name
    assert {p.name for p in VORLAGE.glob("*.container")} == {
        "chronik.container",
        "chronik-whisper.container",
    }
    for daten in vertrag["data"]:
        assert daten["path"].startswith("${DATA_DIR}/chronik/"), daten


def test_kein_pfad_der_servicebay_zeit() -> None:
    """Die Daten liegen in Laibs Ordnung; der Umzug kopiert sie einmal (README der Vorlage)."""
    for datei in VORLAGE.iterdir():
        if datei.suffix in (".container", ".yml"):
            text = datei.read_text(encoding="utf-8")
            assert "daggerheart" not in text, datei.name
            assert "{{" not in text, datei.name


def test_jeder_platzhalter_ist_ein_wert_oder_eingebaut(werte: dict) -> None:
    for einheit in VORLAGE.glob("*.container"):
        benutzt = set(PLATZHALTER.findall(einheit.read_text(encoding="utf-8")))
        assert benutzt - set(werte) - EINGEBAUT == set(), einheit.name


def test_kein_geheimnis_steht_als_wert_da(vertrag: dict, werte: dict) -> None:
    """Der Bot-Token ist extern (``laib secret set``), der Schlüssel des Erkenners erzeugt."""
    geheim = {s["name"]: s for s in vertrag["secrets"]}
    assert geheim["CHRONIK_DISCORD_TOKEN"].get("external") is True
    assert not geheim["CHRONIK_WHISPER_KEY"].get("external")
    assert werte["DISCORD_BOT_TOKEN"] == ("!secret", "CHRONIK_DISCORD_TOKEN")
    assert werte["WHISPER_KEY"] == ("!secret", "CHRONIK_WHISPER_KEY")


def test_die_vorlage_setzt_nur_was_die_konfiguration_liest() -> None:
    """Ein Name, den niemand liest, ist eine Zusage ohne Deckung — und umgekehrt."""
    bot = _umgebung("chronik.container")
    gelesen = _gelesen()
    assert set(bot) - gelesen == set()
    for name in (
        "DISCORD_BOT_TOKEN",
        konfiguration.MODEL_URL_VARIABLE,
        konfiguration.MODEL_LEASE_VARIABLE,
        konfiguration.MODEL_KEY_VARIABLE,
        konfiguration.MODEL_PRESET_VARIABLE,
        konfiguration.WHISPER_KEY_VARIABLE,
        konfiguration.HEALTH_PORT_VARIABLE,
    ):
        assert name in bot, name


def test_adresse_und_schluessel_kommen_aus_den_eingebauten_werten() -> None:
    """Abnahme von #362: kein eigenes Geheimnis, kein Literal — Laibs ``${MODEL_*}``."""
    bot = _umgebung("chronik.container")
    assert bot["MODEL_URL"] == "${MODEL_URL}"
    assert bot["MODEL_LEASE"] == "${MODEL_LEASE}"
    assert bot["MODEL_KEY"] == "${MODEL_KEY}"
    assert bot["MODEL_PRESET"] == "${MODEL_PRESET}"


def test_das_preset_ist_ein_modell_und_kein_fenster(werte: dict) -> None:
    assert werte["MODEL_PRESET"] == "gemma-4-12b"
    assert werte["MODEL_PRESET"] not in {"foundry", "shared", "full", "exclusive"}


def test_kein_container_liegt_im_host_netz(vertrag: dict) -> None:
    """Das Host-Netz der ServiceBay-Fassung (#165) fällt: die Nachbarn sind erreichbar."""
    assert not vertrag.get("hostNetwork")
    for einheit in VORLAGE.glob("*.container"):
        assert "Network=host" not in einheit.read_text(encoding="utf-8"), einheit.name


def test_das_gate_steht_nur_auf_der_schleife_und_ist_die_pruefung(vertrag: dict) -> None:
    port = _umgebung("chronik.container")["CHRONICLE_HEALTH_PORT"]
    assert vertrag["health"]["http"] == f"http://127.0.0.1:{port}/healthz"
    assert f"PublishPort=127.0.0.1:{port}:{port}" in _einheit("chronik.container")
    assert {"port": int(port), "lan": False} in vertrag["ports"]


def test_der_erkenner_ist_nie_im_lan_und_nur_mit_schluessel(vertrag: dict) -> None:
    erkenner = _umgebung("chronik-whisper.container")
    port = int(erkenner["WHISPER_BATCH_PORT"])
    assert {"port": port, "lan": False} in vertrag["ports"]
    assert erkenner["WHISPER_BATCH_KEY"] == "${WHISPER_KEY}"
    bot = _umgebung("chronik.container")
    assert bot["CHRONICLE_WHISPER_KEY"] == "${WHISPER_KEY}"
    assert bot["CHRONICLE_WHISPER_URL"] == f"http://host.containers.internal:{port}"


def test_die_spuren_gehen_nie_ins_backup_und_der_erkenner_liest_sie_nur(vertrag: dict) -> None:
    """§201 StGB: Stimmen echter Menschen, nach sieben Tagen gelöscht, nie gesichert."""
    gesichert = {d["path"]: d.get("backup", True) for d in vertrag["data"]}
    assert gesichert["${DATA_DIR}/chronik/data"] is True
    assert gesichert["${DATA_DIR}/chronik/aufnahmen"] is False
    assert "Volume=${DATA_DIR}/chronik/aufnahmen:/aufnahmen:ro,z" in _einheit(
        "chronik-whisper.container"
    )


def test_der_bot_bleibt_liegen_wenn_er_mit_null_endet() -> None:
    """``BotHaelt``: 0 heißt liegen lassen — ein Neustart wäre der nächste Anmeldeversuch."""
    assert "Restart=on-failure" in _einheit("chronik.container")
    assert "Restart=always" not in _einheit("chronik.container")


def test_die_karte_haengt_nur_am_erkenner(vertrag: dict) -> None:
    assert vertrag["gpu"] is True
    assert "AddDevice=nvidia.com/gpu=all" in _einheit("chronik-whisper.container")
    assert "AddDevice" not in _einheit("chronik.container")


def test_das_image_nennt_den_weg_zum_tag(werte: dict) -> None:
    """Zwei Stellen, ein Image — und der Weg zu einem festen Tag steht, wo man ihn einträgt."""
    spec = importlib.util.spec_from_file_location(
        "bestimme_image_tag", WURZEL / "scripts" / "bestimme_image_tag.py"
    )
    skript = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skript)

    assert werte["IMAGE"].rsplit(":", 1)[0] == skript.IMAGE
    zeile = next(z for z in _einheit("values.example.yml").splitlines() if z.startswith("IMAGE:"))
    assert "bestimme_image_tag.py" in zeile
    assert "ohne führendes v" in zeile


def test_readme_beschreibt_den_umzug() -> None:
    text = (VORLAGE / "README.md").read_text(encoding="utf-8")
    for muss in ("daggerheart-aufnahmen", "laib secret set CHRONIK_DISCORD_TOKEN", "Martin"):
        assert muss in text, muss


# --------------------------------------------------------------------------------------
# Der Erkenner daneben: der Draht, ohne faster-whisper
# --------------------------------------------------------------------------------------


def _modul():
    spec = importlib.util.spec_from_file_location("whisper_batch", VORLAGE / "whisper_batch.py")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture
def erkenner(monkeypatch, tmp_path):
    modul = _modul()
    (tmp_path / "mira.ogg").write_bytes(b"kein echtes Audio")
    monkeypatch.setattr(modul, "ROOT", str(tmp_path))
    monkeypatch.setattr(modul, "KEY", "erkenner-schluessel-nur-im-test")
    auftraege = []

    def rechnen(auftrag):
        auftraege.append(auftrag)
        return {"segments": [{"start": 0.5, "end": 1.5, "text": " Hallo."}]}

    monkeypatch.setattr(modul, "run_job", rechnen)
    server = modul.serve(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    modul.adresse = f"http://127.0.0.1:{server.server_address[1]}"
    modul.auftraege = auftraege
    yield modul
    server.shutdown()
    server.server_close()


def _fragen(erkenner, *, pfad="/transcribe", schluessel=None, rumpf=None):
    kopf = {"Content-Type": "application/json"}
    if schluessel is not None:
        kopf["Authorization"] = f"Bearer {schluessel}"
    daten = None if rumpf is None else json.dumps(rumpf).encode()
    anfrage = urllib.request.Request(erkenner.adresse + pfad, data=daten, headers=kopf)
    try:
        with urllib.request.urlopen(anfrage, timeout=5) as antwort:
            return antwort.status, json.loads(antwort.read())
    except urllib.error.HTTPError as fehler:
        return fehler.code, json.loads(fehler.read())


def test_ohne_schluessel_verschriftet_der_erkenner_nichts(erkenner) -> None:
    assert _fragen(erkenner, rumpf={"path": "mira.ogg"})[0] == 401
    assert _fragen(erkenner, schluessel="falsch", rumpf={"path": "mira.ogg"})[0] == 401
    assert erkenner.auftraege == []


def test_mit_schluessel_kommen_segmente_mit_zeitstempeln(erkenner) -> None:
    code, antwort = _fragen(
        erkenner,
        schluessel="erkenner-schluessel-nur-im-test",
        rumpf={"path": "mira.ogg", "language": "de", "hotwords": ["Mira"]},
    )
    assert code == 200
    assert antwort["segments"] == [{"start": 0.5, "end": 1.5, "text": " Hallo."}]
    (auftrag,) = erkenner.auftraege
    assert auftrag["path"].endswith("/mira.ogg") and auftrag["hotwords"] == ["Mira"]


def test_ausserhalb_der_spuren_liest_er_nichts(erkenner) -> None:
    code, _ = _fragen(
        erkenner, schluessel="erkenner-schluessel-nur-im-test", rumpf={"path": "../../etc/passwd"}
    )
    assert code == 403


def test_das_gesundheitsgate_verschriftet_nichts(erkenner) -> None:
    assert _fragen(erkenner, pfad="/healthz") == (200, {"status": "ok"})
    assert erkenner.auftraege == []


def test_ohne_schluessel_startet_er_gar_nicht(monkeypatch) -> None:
    modul = _modul()
    monkeypatch.setattr(modul, "ROOT", "/aufnahmen")
    monkeypatch.setattr(modul, "PORT", 10301)
    monkeypatch.setattr(modul, "KEY", "")
    assert modul.main() == 1
