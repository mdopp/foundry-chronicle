"""Die Chronik als Datei im Sitzungs-Thread — gegen ein nachgebautes Discord, ohne Netz.

Der Token in diesen Tests ist erfunden und steht nur hier.

Die Sätze, die dieser Suite ihren Sinn geben: **die Trennung zwischen Belegtem und
Verbindungstext übersteht den Weg nach Discord**, weil sie in den Überschriften des
abgelegten Textes steht und Markdown sie unverändert trägt; **der Thread ist ein
Protokoll** — eine neue Fassung kommt als neue Datei und benennt die alte; und **was
schiefgeht, wird gesagt** statt zu stürzen.
"""

from __future__ import annotations

import pytest
import requests
from conftest import runde

from chronicle import db, settings
from chronicle.compose.composer import BELEG_TITEL, NOTIZEN_TITEL, VERBINDUNG_TITEL
from chronicle.compose.service import KIND, RUECKBLICK
from chronicle.config import Config
from chronicle.discord import ausgabe
from chronicle.discord.ausgabe import anhaengen
from chronicle.discord.client import API, DiscordClient

TOKEN = "bot-token-nur-fuer-den-test"

THREAD = "t-4711"
DATUM = "2026-08-06"

STAND = "2026-08-06T20:00:00+00:00"
SPAETER = "2099-01-01T12:00:00+00:00"

CHRONIK = (
    "# Chronik — Sitzung vom 2026-08-06: Der Keller\n\n"
    "## Szene 1 — Im Wirtshaus\n\n"
    f"{NOTIZEN_TITEL}\n- Die Runde bricht auf.\n\n"
    f"{BELEG_TITEL}\n- Brok Eisenfaust — Knowledge Roll: Summe 7\n\n"
    f"{VERBINDUNG_TITEL}\nSie brachen im Morgengrauen auf.\n"
)


class Antwort:
    def raise_for_status(self):
        return None

    def json(self):
        return {}


class FakeDiscord:
    """Discords REST-API, so weit das Anhängen sie braucht."""

    def __init__(self):
        self.angehaengt = []

    def request(self, method, url, **kwargs):
        pfad = url[len(API) :]
        if method == "POST" and pfad.startswith("/channels/"):
            name, inhalt, art = kwargs["files"]["files[0]"]
            self.angehaengt.append(
                (pfad.split("/")[2], name, inhalt.decode("utf-8"), art, kwargs["data"])
            )
            return Antwort()
        raise AssertionError(f"unerwarteter Aufruf: {method} {pfad}")


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(
        discord_bot_token=TOKEN, discord_recap_channel="chronik", data_dir=tmp_path / "daten"
    )
    db.init(gesetzt.database_path)
    return gesetzt


def sitzung(config, *, thread_id=THREAD, played_on=DATUM):
    scope = db.scoped(runde(config))
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO session (runde_id, played_on, title, created_at, thread_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope.runde_id, played_on, "Der Keller", STAND, thread_id),
            )
        return zeiger.lastrowid
    finally:
        scope.close()


def protokoll(config, sitzung_id, text=CHRONIK, *, kind=KIND, created_at=STAND):
    scope = db.scoped(runde(config))
    try:
        with scope:
            scope.execute(
                "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (session_id, kind) DO UPDATE SET text = excluded.text, "
                "created_at = excluded.created_at",
                (scope.runde_id, sitzung_id, kind, text, created_at),
            )
    finally:
        scope.close()


def angehaengt_am(config, sitzung_id):
    scope = db.scoped(runde(config))
    try:
        zeile = scope.execute(
            "SELECT delivered_at FROM protocol WHERE runde_id = ? AND session_id = ? AND kind = ?",
            (scope.runde_id, sitzung_id, KIND),
        ).fetchone()
    finally:
        scope.close()
    return zeile["delivered_at"]


def haengen(config, sitzung_id, api):
    wirksam = settings.effective(config, runde(config))
    return anhaengen(
        config, runde(config), sitzung_id, client=DiscordClient(wirksam, http=lambda: api)
    )


# --- Die Datei landet im Thread ------------------------------------------------------


def test_die_chronik_haengt_als_markdown_datei_im_sitzungs_thread(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    meldung = haengen(config, sitzung_id, api)

    (kanal, name, inhalt, art, begleitung) = api.angehaengt[0]
    assert meldung == ""
    assert kanal == THREAD
    assert name == f"chronik-{DATUM}.md"
    assert art == "text/markdown"
    assert CHRONIK.strip() in inhalt
    assert ausgabe.ERSTE in begleitung["payload_json"]
    assert angehaengt_am(config, sitzung_id) is not None


def test_die_trennung_zwischen_belegt_und_verbindung_uebersteht_den_weg(config):
    """Sie steht in den Überschriften des Protokolls — Markdown trägt sie ohne HTML weiter."""
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    haengen(config, sitzung_id, api)

    inhalt = api.angehaengt[0][2]
    assert BELEG_TITEL in inhalt
    assert VERBINDUNG_TITEL in inhalt
    assert NOTIZEN_TITEL in inhalt
    assert "<" not in inhalt and "class=" not in inhalt


def test_die_datei_sagt_welche_fassung_sie_ist(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    haengen(config, sitzung_id, api)

    assert api.angehaengt[0][2].startswith("_Fassung vom ")


def test_ohne_zustellkanal_landet_die_chronik_trotzdem_im_thread(tmp_path):
    """Der Kanal gilt dem Rückblick; die Chronik gehört in den Thread und geht ihn nichts an."""
    ohne = Config(discord_bot_token=TOKEN, data_dir=tmp_path / "daten")
    db.init(ohne.database_path)
    sitzung_id = sitzung(ohne)
    protokoll(ohne, sitzung_id)
    api = FakeDiscord()

    assert haengen(ohne, sitzung_id, api) == ""
    assert api.angehaengt[0][1] == f"chronik-{DATUM}.md"


def test_ein_in_der_oberflaeche_gesetzter_token_genuegt(tmp_path):
    ohne = Config(data_dir=tmp_path / "daten")
    db.init(ohne.database_path)
    sitzung_id = sitzung(ohne)
    protokoll(ohne, sitzung_id)
    settings.save(runde(ohne), {"discord_bot_token": TOKEN})
    api = FakeDiscord()

    assert haengen(ohne, sitzung_id, api) == ""
    assert len(api.angehaengt) == 1


# --- Genau einmal je Fassung ---------------------------------------------------------


def test_ein_zweiter_lauf_haengt_dieselbe_fassung_nicht_noch_einmal_an(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    haengen(config, sitzung_id, api)
    zweite = haengen(config, sitzung_id, api)

    assert zweite == ""
    assert len(api.angehaengt) == 1


def test_eine_neu_geschriebene_fassung_kommt_als_neue_datei_und_benennt_die_alte(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()
    haengen(config, sitzung_id, api)
    erster = angehaengt_am(config, sitzung_id)

    protokoll(config, sitzung_id, text=CHRONIK + "\n## Szene 2\n", created_at=SPAETER)
    assert haengen(config, sitzung_id, api) == ""

    assert len(api.angehaengt) == 2
    zweite = api.angehaengt[1]
    assert zweite[1] == f"chronik-{DATUM}.md"
    assert f"ersetzt die Fassung vom {ausgabe._lesbar(erster)}" in zweite[2]
    assert "Szene 2" in zweite[2]
    assert ausgabe.NEUE.split("{")[0] in zweite[4]["payload_json"]
    # Die erste Datei bleibt stehen — der Thread ist ein Protokoll, keine Tafel.
    assert "Szene 2" not in api.angehaengt[0][2]


def test_der_rueckblick_geht_nicht_als_datei_hinaus(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="# Rückblick", kind=RUECKBLICK)
    api = FakeDiscord()

    assert haengen(config, sitzung_id, api) == ""
    assert api.angehaengt == []


# --- Ohne Thread, ohne Token: nichts passiert, und nichts stürzt ----------------------


def test_eine_sitzung_ohne_thread_bleibt_ohne_anhang(config):
    sitzung_id = sitzung(config, thread_id=None)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    assert haengen(config, sitzung_id, api) == ""
    assert api.angehaengt == []
    assert angehaengt_am(config, sitzung_id) is None


def test_ohne_bot_token_bleibt_die_chronik_abgelegt(tmp_path):
    ohne = Config(data_dir=tmp_path / "daten")
    db.init(ohne.database_path)
    sitzung_id = sitzung(ohne)
    protokoll(ohne, sitzung_id)

    assert anhaengen(ohne, runde(ohne), sitzung_id) == ""
    assert angehaengt_am(ohne, sitzung_id) is None


# --- Was schiefgeht, wird gesagt -----------------------------------------------------


def test_eine_zu_grosse_chronik_bekommt_eine_ehrliche_meldung_statt_eines_absturzes(
    config, monkeypatch
):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    monkeypatch.setattr(ausgabe, "MAX_BYTES", 10)
    api = FakeDiscord()

    meldung = haengen(config, sitzung_id, api)

    assert meldung == ausgabe.ZU_GROSS.format(grenze=0)
    assert api.angehaengt == []
    assert angehaengt_am(config, sitzung_id) is None


def test_ein_unerreichbares_discord_verschiebt_den_anhang_ohne_token(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)

    class Weg:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError(f"Bot {TOKEN} abgelehnt")

    meldung = anhaengen(config, runde(config), sitzung_id, client=DiscordClient(config, http=Weg))

    assert meldung.startswith("Die Chronik konnte ich hier nicht anhängen:")
    assert TOKEN not in meldung
    assert angehaengt_am(config, sitzung_id) is None


def test_der_token_steht_in_keiner_logzeile(config, caplog):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    with caplog.at_level("DEBUG"):
        haengen(config, sitzung_id, api)

    assert TOKEN not in caplog.text
