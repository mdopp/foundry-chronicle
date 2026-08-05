"""Das ServiceBay-Template — geprüft wie die Konsistenz-Suite der Plattform.

Ein Template zeigt sich erst auf der Box vollständig als richtig. Was sich vorher
mechanisch prüfen lässt, gehört aber hierher: die Platzhalter sind deklariert, das
gerenderte Manifest ist ein gültiger Pod, Mounts treffen ihre Volumes — und kein
Geheimnis hat eine Vorgabe.
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


def test_mounts_treffen_ihre_volumes(manifest: dict) -> None:
    volumes = {eintrag["name"] for eintrag in manifest["spec"]["volumes"]}
    for container in manifest["spec"]["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["name"] in volumes


def test_daten_liegen_unter_data_dir(rohtext: str) -> None:
    assert "path: {{DATA_DIR}}/daggerheart" in rohtext


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


def test_zugangsdaten_sind_geheim_und_ohne_vorgabe(variablen: dict) -> None:
    for name in ("FOUNDRY_PASSWORD", "DISCORD_BOT_TOKEN"):
        assert variablen[name]["type"] == "secret"
        assert "default" not in variablen[name]


def test_keine_echte_adresse_im_manifest(rohtext: str) -> None:
    # Host und Domain kommen zur Installationszeit; im Repo stehen nur Platzhalter.
    fundstellen = re.findall(r"https?://([A-Za-z0-9.-]+)", rohtext)
    assert set(fundstellen) <= {"localhost"}


def test_readme_vorhanden() -> None:
    assert (TEMPLATE_DIR / "README.md").read_text(encoding="utf-8").strip()
