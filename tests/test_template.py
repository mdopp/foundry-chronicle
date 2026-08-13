"""Das ServiceBay-Template — geprüft wie die Konsistenz-Suite der Plattform.

Ein Template zeigt sich erst auf der Box vollständig als richtig. Was sich vorher
mechanisch prüfen lässt, gehört aber hierher: die Platzhalter sind deklariert, das
gerenderte Manifest ist ein gültiger Pod, Mounts treffen ihre Volumes — und keine
Zugangsdaten kommen aus dem Assistenten.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "daggerheart-chronik"

# Deklariert ServiceBay global in templates/settings.json; ein Template darf sie
# benutzen, aber nicht noch einmal deklarieren.
GLOBALE_VARIABLEN = {"DATA_DIR", "PUBLIC_DOMAIN", "LAN_IP", "HOST_GATEWAY_IP"}

PLATZHALTER = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")


@pytest.fixture
def variablen() -> dict:
    return json.loads((TEMPLATE_DIR / "variables.json").read_text(encoding="utf-8"))


@pytest.fixture
def rohtext() -> str:
    return (TEMPLATE_DIR / "template.yml").read_text(encoding="utf-8")


@pytest.fixture
def manifest(rohtext: str, variablen: dict) -> dict:
    ersatz = {name: "1" for name in GLOBALE_VARIABLEN}
    ersatz.update({name: meta.get("default") or "x" for name, meta in variablen.items()})
    gerendert = PLATZHALTER.sub(lambda treffer: ersatz[treffer.group(1)], rohtext)
    return yaml.safe_load(gerendert)


def test_jeder_platzhalter_ist_deklariert(rohtext: str, variablen: dict) -> None:
    benutzt = set(PLATZHALTER.findall(rohtext))
    assert benutzt - set(variablen) - GLOBALE_VARIABLEN == set()


def test_globale_variablen_nicht_neu_deklariert(variablen: dict) -> None:
    assert set(variablen) & GLOBALE_VARIABLEN == set()


def test_public_domain_wird_referenziert(rohtext: str) -> None:
    # Ohne diese Referenz injiziert der Assembler PUBLIC_DOMAIN nicht und der Proxy-Host
    # wird still übersprungen.
    assert "{{PUBLIC_DOMAIN}}" in rohtext


def test_manifest_ist_ein_pod(manifest: dict) -> None:
    assert manifest["kind"] == "Pod"
    assert manifest["metadata"]["name"] == "daggerheart-chronik"


def test_pflichtannotationen(manifest: dict) -> None:
    annotationen = manifest["metadata"]["annotations"]
    assert annotationen["servicebay.label"]
    assert annotationen["servicebay.schema-version"] == "1"
    assert annotationen["servicebay.ports"]
    assert annotationen["servicebay.dependencies"] == "nginx,auth"


def test_healthcheck_zeigt_auf_healthz(manifest: dict, variablen: dict) -> None:
    probe = yaml.safe_load(manifest["metadata"]["annotations"]["servicebay.healthcheck"])
    port = variablen["CHRONICLE_PORT"]["default"]
    assert probe["url"] == f"http://localhost:{port}/healthz"


def test_erreichbar_ohne_stillen_fehlschlag(manifest: dict) -> None:
    # Der Pod muss hostNetwork sein oder jeder containerPort einen hostPort tragen,
    # sonst ist das Deployment still unerreichbar.
    if manifest["spec"].get("hostNetwork"):
        return
    for container in manifest["spec"]["containers"]:
        for port in container.get("ports", []):
            assert "hostPort" in port


def test_ports_sind_konsistent(manifest: dict, variablen: dict) -> None:
    port = int(variablen["CHRONICLE_PORT"]["default"])
    annotiert = manifest["metadata"]["annotations"]["servicebay.ports"]
    assert annotiert == f"{port}/tcp"
    container = manifest["spec"]["containers"][0]
    assert [eintrag["containerPort"] for eintrag in container["ports"]] == [port]
    umgebung = {eintrag["name"]: eintrag["value"] for eintrag in container["env"]}
    assert umgebung["CHRONICLE_PORT"] == str(port)


def test_der_aufnahme_bot_laeuft_als_zweiter_container(manifest: dict) -> None:
    # Ein eigener, dauerhafter Prozess neben dem Webdienst: Sprache lässt sich nur
    # mitschneiden, während sie gesprochen wird.
    container = {eintrag["name"]: eintrag for eintrag in manifest["spec"]["containers"]}
    assert set(container) == {"chronik", "bot"}
    assert container["bot"]["image"] == container["chronik"]["image"]
    assert container["bot"]["command"] == ["python", "-m", "chronicle.bot"]
    assert "ports" not in container["bot"]


def test_das_image_wird_nicht_fest_verdrahtet(rohtext: str) -> None:
    # Der Tag gehört an die Variable, damit ein Rollout einen festen Stand ansteuern kann
    # (#173). Ein hart eingetragenes ':latest' im Manifest nähme genau das wieder weg —
    # und mit ihm den Weg zurück, denn 'latest' benennt keinen früheren Stand.
    assert (
        re.findall(r"^\s*image: \S+:(\S+)$", rohtext, re.MULTILINE)
        == ["{{CHRONICLE_IMAGE_TAG}}"] * 2
    )


def test_keine_wirkungslose_karten_durchreichung(manifest: dict) -> None:
    # ``podman kube play`` (5.8.2) verwirft ``resources.limits`` für eine Pod-Spezifikation
    # still — der Pod startet ohne Karte, ohne dass etwas fehlschlägt (servicebay#2517).
    # Diese Zusicherung hält die wirkungslose Form draußen: der Weg zur Karte führt über
    # den geteilten solaris-whisper der Box (#141), nicht über dieses Manifest. Die
    # SELinux-Freigabe fällt mit ihr, weil sie ohne Durchreichung nur eine Lockerung ohne
    # Gegenwert wäre.
    for container in manifest["spec"]["containers"]:
        assert "resources" not in container
    annotationen = manifest["metadata"]["annotations"]
    assert not [name for name in annotationen if name.startswith("io.podman.annotations.label")]


def test_das_geraet_wird_nicht_erzwungen(manifest: dict) -> None:
    # Der Rückfall auf die CPU ist der Rettungsweg, wenn die geteilten 16 GB gerade nicht
    # reichen (#84). Er trägt nur, solange das Gerät nicht aus der Umgebung festgenagelt
    # ist — ein erzwungenes ``cuda`` macht aus einem langsamen Lauf einen gescheiterten.
    for container in manifest["spec"]["containers"]:
        umgebung = {eintrag["name"] for eintrag in container["env"]}
        assert "CHRONICLE_WHISPER_DEVICE" not in umgebung


def test_beide_container_teilen_dasselbe_aufnahmeverzeichnis(manifest: dict) -> None:
    for eintrag in manifest["spec"]["containers"]:
        umgebung = {wert["name"]: wert["value"] for wert in eintrag["env"]}
        assert umgebung["CHRONICLE_RECORDINGS_DIR"] == "/aufnahmen"
        pfade = {mount["mountPath"]: mount["name"] for mount in eintrag["volumeMounts"]}
        assert pfade["/aufnahmen"] == "chronik-aufnahmen"
        assert pfade["/data"] == "chronik-daten"


def test_die_aufnahmen_liegen_nicht_im_gesicherten_datenverzeichnis(manifest: dict) -> None:
    pfade = {
        eintrag["name"]: eintrag["hostPath"]["path"] for eintrag in manifest["spec"]["volumes"]
    }
    daten = pfade["chronik-daten"]
    assert pfade["chronik-aufnahmen"] != daten
    assert not pfade["chronik-aufnahmen"].startswith(daten + "/")


def test_jedes_volume_ueberdauert_den_pod(manifest: dict) -> None:
    # Der Fehler aus #27: ohne hostPath liegt das Aufnahmeverzeichnis in der
    # beschreibbaren Container-Schicht und ist beim nächsten Neuaufbau des Pods weg.
    for eintrag in manifest["spec"]["volumes"]:
        assert "hostPath" in eintrag, eintrag["name"]


def test_die_hostverzeichnisse_werden_bei_der_erstinstallation_angelegt(manifest: dict) -> None:
    # Bei der Erstinstallation existiert noch keines der beiden Verzeichnisse; mit
    # 'Directory' scheiterte der erste Start, statt sie anzulegen.
    for eintrag in manifest["spec"]["volumes"]:
        assert eintrag["hostPath"]["type"] == "DirectoryOrCreate", eintrag["name"]


def test_der_bot_bekommt_die_datenbank(manifest: dict) -> None:
    # Der Token kommt aus der SQLite, nicht aus der Umgebung — also muss der Bot
    # dieselbe Datenbank sehen wie die Oberfläche, in der er gepflegt wird.
    bot = next(e for e in manifest["spec"]["containers"] if e["name"] == "bot")
    umgebung = {wert["name"]: wert["value"] for wert in bot["env"]}
    assert umgebung["CHRONICLE_DATA_DIR"] == "/data"


def test_mounts_treffen_ihre_volumes(manifest: dict) -> None:
    volumes = {eintrag["name"] for eintrag in manifest["spec"]["volumes"]}
    for container in manifest["spec"]["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["name"] in volumes


def test_beide_hostpfade_liegen_unter_data_dir(rohtext: str) -> None:
    # Die Entscheidung zu #27, festgenagelt: die Spuren liegen *neben* dem
    # Datenverzeichnis, damit keine dienstbezogene Auswahlliste der Plattform sie je
    # einsammeln kann — und trotzdem unter DATA_DIR, weil ein absoluter Host-Pfad aus
    # deren Abdeckungsprüfung herausfiele und keiner der Box im Repo stehen soll.
    assert re.findall(r"^\s*path: (\S+)$", rohtext, re.MULTILINE) == [
        "{{DATA_DIR}}/daggerheart",
        "{{DATA_DIR}}/daggerheart-aufnahmen",
    ]


def test_remote_user_wird_erzwungen(manifest: dict) -> None:
    container = manifest["spec"]["containers"][0]
    umgebung = {eintrag["name"]: eintrag["value"] for eintrag in container["env"]}
    assert umgebung["CHRONICLE_REQUIRE_REMOTE_USER"] == "1"
    assert umgebung["CHRONICLE_HOST"] == "0.0.0.0"


def test_subdomain_hinter_authelia(variablen: dict) -> None:
    subdomain = variablen["CHRONICLE_SUBDOMAIN"]
    assert subdomain["type"] == "subdomain"
    assert subdomain["default"] == "daggerheart"
    assert subdomain["exposure"] == "public"
    assert subdomain["proxyPort"] == "CHRONICLE_PORT"
    assert subdomain["proxyConfig"]["advanced_config"] == "__authelia_forward_auth__"


def test_proxy_port_verweist_auf_eine_variable(variablen: dict) -> None:
    for meta in variablen.values():
        ziel = meta.get("proxyPort")
        if ziel is not None and not str(ziel).isdigit():
            assert ziel in variablen


def test_kein_zufallswert_fuer_fremde_zugangsdaten(variablen: dict) -> None:
    # Der Assistent würfelt für 'type: secret' einen Wert aus. Für ein Geheimnis, das
    # nur die Gegenstelle kennt, ist das kein Platzhalter, sondern ein falscher Wert:
    # er meldete sich als Bot-Token bei Discord an und scheiterte in einer
    # Neustart-Schleife (#33).
    for meta in variablen.values():
        assert meta["type"] not in ("secret", "password", "rsa-private", "bcrypt")


def test_zugangsdaten_kommen_nicht_aus_der_umgebung(variablen: dict, manifest: dict) -> None:
    # Seit #25 ist die Oberfläche die Quelle dieser sechs Werte. Deklariert das Template
    # sie trotzdem, rendert es sie bestenfalls leer und schlimmstenfalls falsch.
    gepflegt = {
        "FOUNDRY_URL",
        "FOUNDRY_USER",
        "FOUNDRY_PASSWORD",
        "DISCORD_BOT_TOKEN",
        "OLLAMA_URL",
        "OLLAMA_MODEL",
    }
    assert set(variablen) & gepflegt == set()
    for container in manifest["spec"]["containers"]:
        umgebung = {eintrag["name"] for eintrag in container["env"]}
        assert umgebung & gepflegt == set()


def test_keine_echte_adresse_im_manifest(rohtext: str) -> None:
    # Host und Domain kommen zur Installationszeit; im Repo stehen nur Platzhalter.
    fundstellen = re.findall(r"https?://([A-Za-z0-9.-]+)", rohtext)
    assert set(fundstellen) <= {"localhost"}


def test_readme_vorhanden() -> None:
    assert (TEMPLATE_DIR / "README.md").read_text(encoding="utf-8").strip()
