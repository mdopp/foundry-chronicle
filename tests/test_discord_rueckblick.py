"""Die Zustellung des Rückblicks — gegen ein nachgebautes Discord, ohne Netz.

Der Token in diesen Tests ist erfunden und steht nur hier. Gepostet wird über dasselbe
REST-API wie der Diktat-Kanal; eine Gateway-Verbindung braucht Schreiben nicht.

Der Rückblick geht als **Embed** hinaus. Was diese Suite festhält: sein Text wird dabei
nicht umgeschrieben — die Überschriften, die Belegtes von Gedeutetem trennen, stehen im
Embed genauso wie im abgelegten Protokoll.
"""

from __future__ import annotations

import pytest
import requests
from conftest import runde

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

STAND = "2026-08-06T20:00:00+00:00"

TITEL = "Rückblick — Sitzung vom 2026-08-06"
RUMPF = (
    "### Was bisher geschah — vom Sprachmodell, nicht belegt\n"
    "Die Runde tastete sich voran.\n\n"
    "### Offene Fäden — Deutung des Modells, keine Fakten\n"
    "- Wer die Wirtin bezahlt hat, blieb offen."
)
TEXT = f"# {TITEL}\n\n{RUMPF}\n"


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
            (eingebettet,) = kwargs["json"]["embeds"]
            self.gepostet.append((pfad.split("/")[2], eingebettet))
            return Antwort({})
        raise AssertionError(f"unerwarteter Aufruf: {method} {pfad}")


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(
        discord_bot_token=TOKEN,
        discord_recap_channel=KANAL,
        data_dir=tmp_path / "daten",
    )
    db.init(gesetzt.database_path)
    return gesetzt


def sitzung(config, *, played_on="2026-08-06"):
    scope = db.scoped(runde(config))
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
                (scope.runde_id, played_on, "Der Keller", STAND),
            )
        return zeiger.lastrowid
    finally:
        scope.close()


def protokoll(config, sitzung_id, text=TEXT, kind=RUECKBLICK):
    scope = db.scoped(runde(config))
    try:
        with scope:
            scope.execute(
                "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope.runde_id, sitzung_id, kind, text, STAND),
            )
    finally:
        scope.close()


def zugestellt_am(config, sitzung_id):
    scope = db.scoped(runde(config))
    try:
        zeile = scope.execute(
            "SELECT delivered_at FROM protocol WHERE runde_id = ? AND session_id = ? AND kind = ?",
            (scope.runde_id, sitzung_id, RUECKBLICK),
        ).fetchone()
    finally:
        scope.close()
    return zeile["delivered_at"]


def zustellen(config, api):
    return deliver(
        config,
        runde(config),
        _einzige_sitzung(config),
        client=DiscordClient(config, http=lambda: api),
    )


def _einzige_sitzung(config):
    scope = db.scoped(runde(config))
    try:
        return scope.execute(
            "SELECT id FROM session WHERE runde_id = ?", (scope.runde_id,)
        ).fetchone()["id"]
    finally:
        scope.close()


# --- Genau einmal --------------------------------------------------------------------


def test_der_rueckblick_geht_als_embed_in_den_gruppenkanal(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    meldung = zustellen(config, api)

    assert api.gepostet == [(CHRONIK_KANAL, {"description": RUMPF, "title": TITEL})]
    assert meldung == rueckblick.ZUGESTELLT.format(sitzung=sitzung_id, kanal=KANAL)
    assert zugestellt_am(config, sitzung_id) is not None


def test_die_ueberschriften_der_deutung_stehen_auch_im_embed(config):
    """Was belegt ist und was gedeutet, muss der Kanal genauso zeigen wie das Protokoll."""
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    zustellen(config, api)

    beschreibung = api.gepostet[0][1]["description"]
    assert "### Offene Fäden — Deutung des Modells, keine Fakten" in beschreibung
    assert "### Was bisher geschah — vom Sprachmodell, nicht belegt" in beschreibung
    assert "<" not in beschreibung


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

    scope = db.scoped(runde(config))
    with scope:
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (session_id, kind) DO UPDATE SET text = excluded.text, "
            "created_at = excluded.created_at",
            (
                scope.runde_id,
                sitzung_id,
                RUECKBLICK,
                "Zweiter Anlauf.",
                "2026-08-07T20:00:00+00:00",
            ),
        )
    scope.close()

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

    assert deliver(config, runde(config), sitzung_id) == rueckblick.NICHT_EINGERICHTET
    assert zugestellt_am(config, sitzung_id) is None


def test_ein_in_der_oberflaeche_gesetzter_kanal_gewinnt(tmp_path):
    config = Config(
        discord_bot_token=TOKEN, discord_recap_channel="alt", data_dir=tmp_path / "daten"
    )
    db.init(config.database_path)
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    settings.save(runde(config), {"discord_recap_channel": f"#{KANAL}"})
    api = FakeDiscord()

    meldung = zustellen(config, api)

    assert api.gepostet == [(CHRONIK_KANAL, {"description": RUMPF, "title": TITEL})]
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

    meldung = deliver(config, runde(config), sitzung_id, client=DiscordClient(config, http=Weg))

    assert "nicht zugestellt" in meldung
    assert TOKEN not in meldung
    assert zugestellt_am(config, sitzung_id) is None


# --- Die Maße eines Embeds -----------------------------------------------------------


def test_ein_zu_langer_rueckblick_wird_ehrlich_gekuerzt_und_zeigt_auf_die_datei(config, caplog):
    """Ein Rückblick passt per Bauart hinein; passt er doch nicht, wird nicht aufgeteilt."""
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="Wort " * 1200)
    api = FakeDiscord()

    with caplog.at_level("WARNING"):
        zustellen(config, api)

    beschreibung = api.gepostet[0][1]["description"]
    assert len(api.gepostet) == 1
    assert len(beschreibung) <= rueckblick.TEXT_GRENZE
    assert beschreibung.endswith(rueckblick.GEKUERZT)
    assert beschreibung.startswith("Wort Wort")
    assert "5999 Zeichen" in caplog.text
    assert zugestellt_am(config, sitzung_id) is not None


def test_ein_zu_langer_titel_wird_gekappt(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="# " + "Titel " * 100 + "\n\nKurz.\n")
    api = FakeDiscord()

    zustellen(config, api)

    assert len(api.gepostet[0][1]["title"]) == rueckblick.TITEL_GRENZE


def test_ein_rueckblick_ohne_titelzeile_bekommt_keinen_leeren_titel(config):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="Nur ein Absatz.\n")
    api = FakeDiscord()

    zustellen(config, api)

    assert api.gepostet[0][1] == {"description": "Nur ein Absatz."}


def test_ein_kurzer_rueckblick_bleibt_unangetastet(config, caplog):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id)
    api = FakeDiscord()

    with caplog.at_level("WARNING"):
        zustellen(config, api)

    assert api.gepostet[0][1]["description"] == RUMPF
    assert "gekürzt" not in caplog.text


def test_der_token_steht_in_keiner_meldung_und_keiner_logzeile(config, caplog):
    sitzung_id = sitzung(config)
    protokoll(config, sitzung_id, text="Wort " * 1200)
    api = FakeDiscord()

    with caplog.at_level("DEBUG"):
        meldung = zustellen(config, api)

    assert TOKEN not in caplog.text
    assert TOKEN not in meldung
