"""Das Sitzungsfenster gegen Laibs Lease — auf dem Draht, gegen eine Attrappe des Vertrags (#362).

Der Vertrag steht in laib ``docs/adr/0011-model-server-is-a-laib-role.md``, Ergänzung
2026-10-14 (mdopp/laib#157), und Laib nagelt ihn in ``internal/lease/contract_test.go``
fest. Die Attrappe hier spricht genau diese Felder und Ablehnungen über echtes HTTP: ein
``http.server`` in einem Faden, gegen den ``compose.client`` mit ``requests`` redet. Was
der Klient falsch schickt — ein Fenstername statt eines Presets, ein Feld zu viel, kein
Schlüssel —, weist sie ab wie die echte Lease.
"""

from __future__ import annotations

import ast
import json
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chronicle.compose import client as modell
from chronicle.config import Config

SCHLUESSEL = "lease-secret"
HAUSHALT = "gemma-4-e4b"
PRESET = "gemma-4-12b"

# Wie laibs ``templates/llama/presets.json``: Fenster sind nach dem benannt, was sie mit der
# Karte tun, und ein Verbraucher nennt nie eines davon.
FENSTER = {
    "shared": [HAUSHALT, PRESET],
    "full": [HAUSHALT, PRESET, "qwen3.6-35b-a3b"],
}
TTL_MIN, TTL_MAX, TTL_VORGABE = 300, 86400, 14400
GNADE = 600  # LEASE_RENEW, zehn Minuten
HALTER_MUSTER = set("abcdefghijklmnopqrstuvwxyz0123456789-")


class LaibLease:
    """Der Vertrag, so wie ``internal/lease/lease.go`` ihn beantwortet — ohne Karte dahinter."""

    def __init__(self, *, vorbereitung: int = 0) -> None:
        self.fenster: dict | None = None
        self.vorbereitung = vorbereitung  # wie viele Antworten noch »preparing« sagen
        self.anfragen: list[dict] = []

    # -- die drei Verben ----------------------------------------------------------------

    def status(self) -> tuple[int, dict]:
        aus = {
            "state": "none",
            "model": "",
            "alias": HAUSHALT,
            "expires_at": None,
            "holder": "",
            "last_renewed_at": None,
            "renew_after": None,
            "retry_after": 30,
            "mode": "",
            "allowed": [HAUSHALT],
        }
        f = self.fenster
        if f is None:
            return 200, aus
        aus.update(
            model=f["mode"],
            alias=f["alias"],
            holder=f["holder"],
            mode=f["mode"],
            allowed=FENSTER[f["mode"]],
            last_renewed_at=f["renewed"],
            renew_after=f["renew_after"],
        )
        if self.vorbereitung > 0:
            self.vorbereitung -= 1
            aus["state"] = "preparing"
            return 200, aus
        aus.update(state="ready", expires_at=f["deadline"])
        return 200, aus

    def nehmen(self, kopf: str | None, rumpf: bytes) -> tuple[int, dict]:
        if kopf != f"Bearer {SCHLUESSEL}":
            return 401, {"ok": False, "reason": "unauthorized"}
        try:
            roh = json.loads(rumpf)
        except ValueError:
            return 400, {"ok": False, "reason": "invalid_json"}
        if not isinstance(roh, dict):
            return 400, {"ok": False, "reason": "invalid_payload"}
        if set(roh) - {"model", "ttl_s", "holder"}:
            return 400, {"ok": False, "reason": "unexpected_field"}
        preset = roh.get("model")
        if not isinstance(preset, str) or not preset.strip():
            return 400, {"ok": False, "reason": "invalid_model"}
        ttl = roh.get("ttl_s", TTL_VORGABE)
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
            return 400, {"ok": False, "reason": "invalid_ttl"}
        halter = roh.get("holder")
        if not isinstance(halter, str) or not 1 <= len(halter) <= 64 or set(halter) - HALTER_MUSTER:
            return 400, {"ok": False, "reason": "invalid_holder"}
        # Ein Preset, nie ein Fenstername — und kein alter Name, der für einen Verbraucher
        # stehen blieb (»foundry«, »thinking«): ADR 0021, Regel 4.
        moegliche = [name for name in ("shared", "full") if preset in FENSTER[name]]
        if not moegliche:
            return 400, {"ok": False, "reason": "invalid_model"}
        jetzt = int(time.time())
        f = self.fenster
        if f is not None and f["holder"] != halter:
            return 409, {
                "ok": False,
                "reason": "held",
                "holder": f["holder"],
                "expires_at": f["deadline"],
                "mode": f["mode"],
                "allowed": FENSTER[f["mode"]],
            }
        ttl = min(max(ttl, TTL_MIN), TTL_MAX)
        renew = min(ttl // 3, GNADE // 2)
        self.fenster = {
            "holder": halter,
            "mode": moegliche[0],
            "alias": preset,
            "deadline": jetzt + ttl,
            "renewed": jetzt,
            "renew_after": renew,
        }
        if self.vorbereitung > 0:
            self.vorbereitung -= 1
            return 202, {
                "ok": True,
                "state": "preparing",
                "model": moegliche[0],
                "alias": preset,
                "retry_after": 30,
                "expires_at": None,
            }
        return 200, {
            "ok": True,
            "state": "ready",
            "model": moegliche[0],
            "alias": preset,
            "expires_at": jetzt + ttl,
            "renew_after": renew,
            "allowed": FENSTER[moegliche[0]],
        }

    def geben(self, kopf: str | None, rumpf: bytes) -> tuple[int, dict]:
        if kopf != f"Bearer {SCHLUESSEL}":
            return 401, {"ok": False, "reason": "unauthorized"}
        halter = ""
        if rumpf.strip():
            try:
                roh = json.loads(rumpf)
            except ValueError:
                return 400, {"ok": False, "reason": "invalid_json"}
            if not isinstance(roh, dict):
                return 400, {"ok": False, "reason": "invalid_payload"}
            if set(roh) - {"holder"}:
                return 400, {"ok": False, "reason": "unexpected_field"}
            halter = roh.get("holder")
            if not isinstance(halter, str) or not halter or set(halter) - HALTER_MUSTER:
                return 400, {"ok": False, "reason": "invalid_holder"}
        f = self.fenster
        if f is not None and halter and f["holder"] != halter:
            return 409, {
                "ok": False,
                "reason": "held",
                "holder": f["holder"],
                "expires_at": f["deadline"],
            }
        self.fenster = None
        return 200, {"ok": True, "state": "released"}

    def fremd(self, halter: str = "pi", preset: str = "qwen3.6-35b-a3b") -> None:
        """Ein anderer Dienst hält die Karte."""
        jetzt = int(time.time())
        self.fenster = {
            "holder": halter,
            "mode": "full",
            "alias": preset,
            "deadline": jetzt + 900,
            "renewed": jetzt,
            "renew_after": 300,
        }

    # -- der Draht ---------------------------------------------------------------------

    def handler(self):
        lease = self

        class Handler(BaseHTTPRequestHandler):
            def _antworten(self, code: int, rumpf: dict) -> None:
                daten = json.dumps(rumpf).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(daten)))
                self.end_headers()
                self.wfile.write(daten)

            def _lesen(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _merken(self, rumpf: bytes) -> None:
                lease.anfragen.append(
                    {
                        "methode": self.command,
                        "pfad": self.path,
                        "kopf": self.headers.get("Authorization"),
                        "rumpf": json.loads(rumpf) if rumpf.strip() else None,
                    }
                )

            def do_GET(self) -> None:  # noqa: N802
                self._merken(b"")
                self._antworten(*lease.status())

            def do_POST(self) -> None:  # noqa: N802
                rumpf = self._lesen()
                self._merken(rumpf)
                self._antworten(*lease.nehmen(self.headers.get("Authorization"), rumpf))

            def do_DELETE(self) -> None:  # noqa: N802
                rumpf = self._lesen()
                self._merken(rumpf)
                self._antworten(*lease.geben(self.headers.get("Authorization"), rumpf))

            def log_message(self, *args) -> None:
                pass

        return Handler

    def verben(self) -> list[str]:
        return [a["methode"] for a in self.anfragen]


@pytest.fixture
def lease():
    attrappe = LaibLease()
    server = ThreadingHTTPServer(("127.0.0.1", 0), attrappe.handler())
    faden = threading.Thread(target=server.serve_forever, daemon=True)
    faden.start()
    attrappe.url = f"http://127.0.0.1:{server.server_address[1]}/api/model-lease"
    yield attrappe
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def ohne_offenes_fenster(monkeypatch):
    """Kein Test erbt das Fenster eines anderen — und keiner lässt eines stehen."""
    monkeypatch.setattr(modell, "_lease_bis", 0.0)
    monkeypatch.setattr(modell, "_lease_erneuerung_s", modell.LEASE_ERNEUERUNG_S)


def box(lease, tmp_path, **anders) -> Config:
    """Was Laib in die Unit schreibt: ``${MODEL_URL}``, ``${MODEL_LEASE}``, ``${MODEL_KEY}``."""
    werte = {
        "model_url": "http://host.containers.internal:11435/v1",
        "model_lease": lease.url,
        "model_key": SCHLUESSEL,
        "model_preset": PRESET,
        "data_dir": tmp_path,
    }
    werte.update(anders)
    return Config(**werte)


class Uhr:
    """Eine Uhr, die nur durch Warten vorgeht — sonst liefe die Wartezeit in echt ab."""

    def __init__(self) -> None:
        self.jetzt = 1000.0
        self.pausen: list[float] = []

    def monotonic(self) -> float:
        return self.jetzt

    def warten(self, sekunden: float) -> None:
        self.pausen.append(sekunden)
        self.jetzt += sekunden


# --------------------------------------------------------------------------------------
# Anmelden: ein Preset, ein Halter, der Schlüssel
# --------------------------------------------------------------------------------------


def test_die_anmeldung_spricht_den_vertrag_und_die_lease_waehlt_das_fenster(lease, tmp_path):
    assert modell.fenster_oeffnen(box(lease, tmp_path)) is True

    (anmeldung,) = lease.anfragen
    assert anmeldung["methode"] == "POST"
    assert anmeldung["pfad"] == "/api/model-lease"
    assert anmeldung["kopf"] == f"Bearer {SCHLUESSEL}"
    assert anmeldung["rumpf"] == {
        "model": PRESET,
        "ttl_s": modell.LEASE_TTL_S,
        "holder": "foundry-chronicle",
    }
    # Das engste Fenster für das 12B neben der Sprache — gewählt von der Lease, nicht von uns.
    assert lease.fenster["mode"] == "shared"
    assert modell.lease_offen()


def test_der_erneuerungstakt_ist_der_der_lease(lease, tmp_path):
    """``renew_after`` ist ein Drittel der Frist, höchstens die halbe Gnade: 300 s bei 900."""
    modell.fenster_oeffnen(box(lease, tmp_path))
    assert modell.erneuerung() == 300


def test_dasselbe_post_erneuert(lease, tmp_path):
    konfiguration = box(lease, tmp_path)
    assert modell.fenster_oeffnen(konfiguration)
    assert modell.fenster_oeffnen(konfiguration)
    assert lease.verben() == ["POST", "POST"]
    assert lease.fenster["holder"] == "foundry-chronicle"


def test_das_preset_kommt_aus_der_vorlage(lease, tmp_path):
    """Kein Fenster im Code: wer das Preset ändert, ändert eine Variable, keinen Klienten."""
    modell.fenster_oeffnen(box(lease, tmp_path, model_preset="qwen3.6-35b-a3b"))
    assert lease.anfragen[0]["rumpf"]["model"] == "qwen3.6-35b-a3b"
    assert lease.fenster["mode"] == "full"


def test_kein_fenstername_und_kein_altes_profil_steht_im_klienten():
    """Abnahme von #362: »keine Konstante nennt ein Fenster«.

    ``foundry`` war das Profil beim Nachbarn ``solaris``; Laib weist es ab (``invalid_model``).
    Die Fensternamen der Lease (``shared``, ``full``, ``exclusive``) kennt ein Verbraucher
    nicht — er fragt nach einem Modell.
    """
    quelle = pathlib.Path("src/chronicle/compose/client.py").read_text(encoding="utf-8")
    zeichenketten = {
        knoten.value
        for knoten in ast.walk(ast.parse(quelle))
        if isinstance(knoten, ast.Constant) and isinstance(knoten.value, str)
    }
    assert zeichenketten.isdisjoint({"foundry", "shared", "full", "exclusive", "thinking"})
    assert not hasattr(modell, "LEASE_PROFIL")


def test_ein_fenstername_als_preset_wird_von_der_lease_abgewiesen(lease, tmp_path, caplog):
    """Die Attrappe weist ab wie Laib — und der Abend läuft trotzdem."""
    with caplog.at_level("WARNING"):
        assert modell.fenster_oeffnen(box(lease, tmp_path, model_preset="foundry")) is False
    assert not modell.lease_offen()
    assert "invalid_model" in caplog.text


def test_ohne_schluessel_geht_keine_anmeldung_hinaus(lease, tmp_path):
    """Ohne ``${MODEL_KEY}`` stünde das ``401`` schon fest — gefragt wird dann gar nicht."""
    assert modell.fenster_oeffnen(box(lease, tmp_path, model_key=None)) is False
    assert lease.anfragen == []


def test_ein_falscher_schluessel_ist_ein_gescheitertes_fenster_ohne_den_schluessel_im_log(
    lease, tmp_path, caplog
):
    with caplog.at_level("INFO"):
        assert modell.fenster_oeffnen(box(lease, tmp_path, model_key="falsch")) is False
    assert "unauthorized" in caplog.text
    assert "falsch" not in caplog.text
    assert SCHLUESSEL not in caplog.text


def test_ohne_lease_auf_der_box_geht_kein_aufruf_hinaus(lease, tmp_path):
    """Eine Box ohne Modellrolle nennt keine Lease — dann gibt es auch kein Fenster."""
    assert modell.fenster_oeffnen(box(lease, tmp_path, model_lease=None)) is False
    assert modell.verwaistes_fenster_schliessen(box(lease, tmp_path, model_lease=None)) is False
    assert lease.anfragen == []


def test_der_abschalter_laesst_keinen_aufruf_hinaus(lease, tmp_path):
    aus = box(lease, tmp_path, gpu_lease=False)
    assert modell.fenster_oeffnen(aus) is False
    assert modell.verwaistes_fenster_schliessen(aus) is False
    assert lease.anfragen == []


def test_hat_ein_anderer_die_karte_sagt_das_log_wer(lease, tmp_path, caplog):
    lease.fremd("pi")
    with caplog.at_level("WARNING"):
        assert modell.fenster_oeffnen(box(lease, tmp_path)) is False
    assert "held" in caplog.text and "pi" in caplog.text
    assert lease.fenster["holder"] == "pi"


def test_eine_unerreichbare_lease_haelt_den_abend_nicht_auf(tmp_path, caplog):
    tot = Config(
        model_url="http://127.0.0.1:9/v1",
        model_lease="http://127.0.0.1:9/api/model-lease",
        model_key=SCHLUESSEL,
        model_preset=PRESET,
        data_dir=tmp_path,
    )
    with caplog.at_level("WARNING"):
        assert modell.fenster_oeffnen(tot) is False
        assert modell.verwaistes_fenster_schliessen(tot) is False
    assert SCHLUESSEL not in caplog.text


def test_die_nutzlast_traegt_keine_runde_und_keine_sitzung():
    """#321: die Lease weist ein fremdes Feld ab — aber ihre Prüfung ist nicht unsere Zusage."""
    quelle = pathlib.Path("src/chronicle/compose/client.py").read_text(encoding="utf-8")
    assert '\nLEASE_HALTER = "foundry-chronicle"\n' in quelle
    baum = ast.parse(quelle)
    ((wert,),) = [
        [zuweisung.value]
        for zuweisung in ast.walk(baum)
        if isinstance(zuweisung, ast.Assign)
        and any(
            isinstance(ziel, ast.Name) and ziel.id == "LEASE_HALTER" for ziel in zuweisung.targets
        )
    ]
    assert isinstance(wert, ast.Constant) and isinstance(wert.value, str)


# --------------------------------------------------------------------------------------
# preparing: fragen, nicht neu anmelden
# --------------------------------------------------------------------------------------


@pytest.fixture
def uhr(monkeypatch):
    gestellt = Uhr()
    monkeypatch.setattr(modell.time, "monotonic", gestellt.monotonic)
    return gestellt


def test_ein_vorbereitetes_fenster_wird_mit_get_gefragt_und_nicht_neu_angemeldet(
    lease, tmp_path, uhr
):
    lease.vorbereitung = 3  # 202 auf das POST, dann zweimal »preparing« auf GET, dann ready

    assert modell.fenster_oeffnen(box(lease, tmp_path), warten=uhr.warten) is True

    assert lease.verben() == ["POST", "GET", "GET", "GET"]
    # Gefragt wird ohne Schlüssel: Lesen ist bei Laib offen.
    assert all(a["kopf"] is None for a in lease.anfragen if a["methode"] == "GET")
    assert uhr.pausen == [30.0, 30.0, 30.0]
    assert modell.erneuerung() == 300


def test_die_frist_beginnt_erst_mit_ready(lease, tmp_path, uhr):
    lease.vorbereitung = 2
    modell.fenster_oeffnen(box(lease, tmp_path), warten=uhr.warten)
    assert modell._lease_bis == uhr.jetzt + modell.LEASE_TTL_S


def test_nimmt_ein_anderer_die_karte_waehrend_sie_laedt_gibt_es_kein_fenster(lease, tmp_path, uhr):
    lease.vorbereitung = 1

    def dazwischen(sekunden):
        uhr.warten(sekunden)
        lease.fremd("pi")

    assert modell.fenster_oeffnen(box(lease, tmp_path), warten=dazwischen) is False
    assert not modell.lease_offen()


def test_eine_lease_die_nie_fertig_wird_bindet_den_abend_nicht_ewig(lease, tmp_path, uhr, caplog):
    lease.vorbereitung = 10**9
    with caplog.at_level("WARNING"):
        assert modell.fenster_oeffnen(box(lease, tmp_path), warten=uhr.warten) is False
    assert sum(uhr.pausen) <= modell.LEASE_VORBEREITUNG_S
    assert "POST" not in lease.verben()[1:]


# --------------------------------------------------------------------------------------
# Freigeben und der Start
# --------------------------------------------------------------------------------------


def test_das_ende_gibt_das_eigene_fenster_mit_halter_und_schluessel_zurueck(lease, tmp_path):
    konfiguration = box(lease, tmp_path)
    modell.fenster_oeffnen(konfiguration)

    assert modell.freigeben(konfiguration) is True

    abgabe = lease.anfragen[-1]
    assert abgabe["methode"] == "DELETE"
    assert abgabe["kopf"] == f"Bearer {SCHLUESSEL}"
    assert abgabe["rumpf"] == {"holder": "foundry-chronicle"}
    assert lease.fenster is None
    assert not modell.lease_offen()
    # Fertig, wenn die Antwort kommt: ``releasing`` gibt es bei Laib nicht.
    assert lease.status()[1]["state"] == "none"


def test_ohne_offenes_fenster_wird_nichts_zurueckgegeben(lease, tmp_path):
    assert modell.freigeben(box(lease, tmp_path)) is False
    assert lease.anfragen == []


def test_ein_fremdes_fenster_gibt_das_ende_nicht_frei(lease, tmp_path):
    """Die Lease prüft den Halter am ``DELETE`` — deshalb steht er im Rumpf."""
    konfiguration = box(lease, tmp_path)
    modell.fenster_oeffnen(konfiguration)
    lease.fremd("pi")

    assert modell.freigeben(konfiguration) is False
    assert lease.fenster["holder"] == "pi"


@pytest.mark.parametrize("vorbereitung", [0, 1])
def test_beim_start_wird_das_eigene_fenster_eines_vorgaengers_zurueckgegeben(
    lease, tmp_path, vorbereitung
):
    """Die Verbraucherregel: beim Start ``GET``, und hält unser Halter etwas, zuerst ``DELETE``.

    Das gilt für ``ready`` wie für ``preparing``: bei einem Prozessstart kann ein Fenster
    unter unserem Halter nur von einem Vorgänger stammen.
    """
    vorgaenger = box(lease, tmp_path)
    modell.fenster_oeffnen(vorgaenger)
    lease.anfragen.clear()
    lease.vorbereitung = vorbereitung

    assert modell.verwaistes_fenster_schliessen(vorgaenger) is True

    assert lease.verben() == ["GET", "DELETE"]
    assert lease.anfragen[1]["kopf"] == f"Bearer {SCHLUESSEL}"
    assert lease.anfragen[1]["rumpf"] == {"holder": "foundry-chronicle"}
    assert lease.fenster is None


def test_beim_start_bleibt_ein_fremdes_fenster_stehen(lease, tmp_path):
    lease.fremd("pi")
    assert modell.verwaistes_fenster_schliessen(box(lease, tmp_path)) is False
    assert lease.verben() == ["GET"]
    assert lease.fenster["holder"] == "pi"


def test_beim_start_ohne_fenster_geht_nur_eine_frage_hinaus(lease, tmp_path):
    assert modell.verwaistes_fenster_schliessen(box(lease, tmp_path)) is False
    assert lease.verben() == ["GET"]


def test_kein_weg_schickt_ein_delete_ohne_rumpf():
    """Der Notausgang gehört dem Betreiber, nicht unserem Normalbetrieb (#333).

    Ein ``DELETE`` ohne Rumpf gibt bei Laib frei, **ohne** den Halter zu prüfen.
    """
    ohne_rumpf = []
    for pfad in pathlib.Path("src/chronicle").rglob("*.py"):
        for knoten in ast.walk(ast.parse(pfad.read_text(encoding="utf-8"))):
            if not isinstance(knoten, ast.Call):
                continue
            if not (isinstance(knoten.func, ast.Attribute) and knoten.func.attr == "delete"):
                continue
            if not any(wort.arg == "json" for wort in knoten.keywords):
                ohne_rumpf.append(f"{pfad.as_posix()}:{knoten.lineno}")
    assert ohne_rumpf == []
