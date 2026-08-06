"""Die Zustellung des Rückblicks — gegen ein nachgebautes Discord, ohne Netz.

Der Token in diesen Tests ist erfunden und steht nur hier. Gepostet wird über dasselbe
REST-API wie der Diktat-Kanal; eine Gateway-Verbindung braucht Schreiben nicht.
"""

from __future__ import annotations

import pytest
import requests

from chronicle import db, settings
from chronicle.compose.service import KIND, RUECKBLICK
from chronicle.config import Config
from chronicle.discord import rueckblick
from chronicle.discord.client import API, DiscordClient
from chronicle.discord.rueckblick import deliver

TOKEN = "bot-token-nur-fuer-den-test"

GILDE = "g-runde"
CHRONIK_KANAL = "c-chronik"
DIKTAT_KANAL = "c-diktat"

KANAL = "chronik"

ADRESSE = "https://chronik.example"

STAND = "2026-08-06T20:00:00+00:00"

TEXT = "# Rückblick — Sitzung vom 2026-08-06\n\nDie Runde tastete sich voran.\n"


class Antwort:
    def __init__(self, payload=None):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeDiscord:
    """Discords REST-API, so weit die Zustellung sie braucht."""

    def __init__(self, *, kanal=KANAL):
        self.kanal = kanal
        self.gepostet = []

    def request(self, method, url, **kwargs):
        pfad = url[len(API) :]
        if pfad == "/users/@me/guilds":
            return Antwort([{"id": GILDE, "name": "Die Runde"}])
        if pfad == f"/guilds/{GILDE}/channels":
            return Antwort(
                [
                    {"id": DIKTAT_KANAL, "name": "diktat", "type": 0},
                    {"id": CHRONIK_KANAL, "name": self.kanal, "type": 0},
                ]
            )
        if method == "POST" and pfad.startswith("/channels/"):
            self.gepostet.append((pfad.split("/")[2], kwargs["json"]["content"]))
            return Antwort({})
        raise AssertionError(f"unerwarteter Aufruf: {method} {pfad}")


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(
        discord_bot_token=TOKEN,
        discord_recap_channel=KANAL,
        public_url=ADRESSE,
        data_dir=tmp_path / "daten",
    )
    db.init(gesetzt.database_path)
    return gesetzt


def sitzung(config, *, played_on="2026-08-06"):
    connection = db.connect(config.database_path)
    try:
        with connection:
            zeiger = connection.execute(
                "INSERT INTO session (played_on, title, created_at) VALUES (?, ?, ?)",
                (played_on, "Der Keller", STAND),
            )
        return zeiger.lastrowid
    finally:
        connection.close()


def protokoll(config, sitzung_id, text=TEXT, kind=RUECKBLICK):
    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                (sitzung_id, kind, text, STAND),
            )
    finally:
        connection.close()


def zugestellt_am(config, sitzung_id):
    connection = db.connect(config.database_path)
    try:
        zeile = connection.execute(
            "SELECT delivered_at FROM protocol WHERE session_id = ? AND kind = ?",
            (sitzung_id, RUECKBLICK),
        ).fetchone()
    finally:
        connection.close()
    return zeile["delivered_at"]


def zustellen(config, api):
    return deliver(config, _einzige_sitzung(config), client=DiscordClient(config, http=lambda: api))


def _einzige_sitzung(config):
    connection = db.connect(config.database_path)
    try:
        return connection.execute("SELECT id FROM session").fetchone()["id"]
    finally:
        connection.close()


# --- Genau einmal --------------------------------------------------------------------


def test_der_rueckblick_geht_unveraendert_in_den_gruppenkanal(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    meldung = zustellen(config, api)

    assert api.gepostet == [(CHRONIK_KANAL, TEXT)]
    assert meldung == rueckblick.ZUGESTELLT.format(sitzung=sitzung_id, kanal=KANAL)
    assert zugestellt_am(config, sitzung_id) is not None


def test_ein_zweiter_lauf_stellt_nicht_noch_einmal_zu(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    zustellen(config, api)
    zweite = zustellen(config, api)

    assert len(api.gepostet) == 1
    assert zweite == rueckblick.SCHON_ZUGESTELLT.format(sitzung=sitzung_id)


def test_eine_neu_komponierte_fassung_wird_nicht_noch_einmal_gepostet(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()
    zustellen(config, api)

    connection = db.connect(config.database_path)
    with connection:
        connection.execute(
            "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (session_id, kind) DO UPDATE SET text = excluded.text, "
            "created_at = excluded.created_at",
            (sitzung_id, RUECKBLICK, "Zweiter Anlauf.", "2026-08-07T20:00:00+00:00"),
        )
    connection.close()

    assert zustellen(config, api) == rueckblick.SCHON_ZUGESTELLT.format(sitzung=sitzung_id)
    assert len(api.gepostet) == 1


def test_nur_der_rueckblick_geht_hinaus_nicht_die_chronik(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="Die ganze Chronik mit allen Zahlen.", kind=KIND)
    api = FakeDiscord()

    meldung = zustellen(config, api)

    assert api.gepostet == []
    assert meldung == rueckblick.KEIN_RUECKBLICK.format(sitzung=sitzung_id)


# --- Ohne Einrichtung passiert nichts, und das steht da ------------------------------


def test_ohne_zustellkanal_bleibt_die_zustellung_aus(tmp_path):
    config = Config(discord_bot_token=TOKEN, data_dir=tmp_path / "daten")
    db.init(config.database_path)
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    meldung = zustellen(config, api)

    assert meldung == rueckblick.KEIN_ZUSTELLKANAL
    assert api.gepostet == []
    assert zugestellt_am(config, sitzung_id) is None


def test_ohne_bot_token_bleibt_die_zustellung_aus(tmp_path):
    config = Config(discord_recap_channel=KANAL, data_dir=tmp_path / "daten")
    db.init(config.database_path)
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)

    assert deliver(config, sitzung_id) == rueckblick.NICHT_EINGERICHTET
    assert zugestellt_am(config, sitzung_id) is None


def test_ein_in_der_oberflaeche_gesetzter_kanal_gewinnt(tmp_path):
    config = Config(
        discord_bot_token=TOKEN, discord_recap_channel="alt", data_dir=tmp_path / "daten"
    )
    db.init(config.database_path)
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    settings.save(config.database_path, {"discord_recap_channel": f"#{KANAL}"})
    api = FakeDiscord()

    meldung = zustellen(config, api)

    assert api.gepostet == [(CHRONIK_KANAL, TEXT)]
    assert meldung == rueckblick.ZUGESTELLT.format(sitzung=sitzung_id, kanal=KANAL)


def test_ohne_den_kanal_sagt_der_lauf_das_statt_still_zu_stehen(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord(kanal="plauderei")

    meldung = zustellen(config, api)

    assert meldung == rueckblick.KEIN_KANAL.format(kanal=KANAL)
    assert api.gepostet == []
    assert zugestellt_am(config, sitzung_id) is None


def test_ein_unerreichbares_discord_verschiebt_die_zustellung_ohne_token(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)

    class Weg:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError(f"Bot {TOKEN} abgelehnt")

    meldung = deliver(config, sitzung_id, client=DiscordClient(config, http=Weg))

    assert "nicht zugestellt" in meldung
    assert TOKEN not in meldung
    assert zugestellt_am(config, sitzung_id) is None


# --- Die 2000-Zeichen-Grenze ---------------------------------------------------------


def test_ein_zu_langer_rueckblick_wird_gekuerzt_und_verlinkt(config, caplog):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="Wort " * 600)
    api = FakeDiscord()

    with caplog.at_level("WARNING"):
        zustellen(config, api)

    gepostet = api.gepostet[0][1]
    assert len(gepostet) <= rueckblick.GRENZE
    assert gepostet.endswith(f"{ADRESSE}/sitzungen/{sitzung_id}/protokoll")
    assert gepostet.startswith("Wort Wort")
    assert "3000 Zeichen" in caplog.text


def test_ohne_oeffentliche_adresse_wird_gekuerzt_ohne_link(tmp_path):
    config = Config(
        discord_bot_token=TOKEN, discord_recap_channel=KANAL, data_dir=tmp_path / "daten"
    )
    db.init(config.database_path)
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="Wort " * 600)
    api = FakeDiscord()

    zustellen(config, api)

    gepostet = api.gepostet[0][1]
    assert len(gepostet) <= rueckblick.GRENZE
    assert gepostet.endswith(rueckblick.GEKUERZT_OHNE_ADRESSE)


def test_ein_kurzer_rueckblick_bleibt_unangetastet(config, caplog):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    with caplog.at_level("WARNING"):
        zustellen(config, api)

    assert api.gepostet[0][1] == TEXT
    assert "gekürzt" not in caplog.text


def test_der_token_steht_in_keiner_meldung_und_keiner_logzeile(config, caplog):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="Wort " * 600)
    api = FakeDiscord()

    with caplog.at_level("DEBUG"):
        meldung = zustellen(config, api)

    assert TOKEN not in caplog.text
    assert TOKEN not in meldung
