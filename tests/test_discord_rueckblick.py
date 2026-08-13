"""Die Zustellung des Rückblicks — gegen ein nachgebautes Discord, ohne Netz.

Der Token in diesen Tests ist erfunden und steht nur hier. Gepostet wird über dasselbe
REST-API wie der Diktat-Kanal; eine Gateway-Verbindung braucht Schreiben nicht.

Der Rückblick geht als **Embed** hinaus. Was diese Suite festhält: sein Text wird dabei
nicht umgeschrieben — die Überschriften, die Belegtes von Gedeutetem trennen, stehen im
Embed genauso wie im abgelegten Protokoll.

Und seit #182: der eingestellte Kanal kommt in **zwei** Formen — als Id aus ``/setup`` und
als Name aus der Zeit davor —, beide werden beliefert, keine über die Gilde der Runde
hinaus, und was nicht ankommt, steht als gescheitert im Ergebnis statt im Nichts.
"""

from __future__ import annotations

import pytest
import requests

from chronicle import db, settings
from chronicle import runde as runden
from chronicle.compose.service import KIND, RUECKBLICK
from chronicle.config import Config
from chronicle.discord import rueckblick
from chronicle.discord.client import API, DiscordClient
from chronicle.discord.rueckblick import deliver

TOKEN = "bot-token-nur-fuer-den-test"

GILDE = "g-runde"
CHRONIK_KANAL = "c-chronik"
DIKTAT_KANAL = "c-diktat"

FREMDE_GILDE = "g-nachbarn"
FREMDER_KANAL = "c-nachbarchronik"

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
    """Discords REST-API, so weit die Zustellung sie braucht.

    Zwei Gilden, und in beiden ein Kanal mit demselben Namen — genau daran hängt die
    Trennung: eine Suche über alle Gilden fände hier die falsche.
    """

    def __init__(self, *, kanal=KANAL):
        self.kanal = kanal
        self.gepostet = []

    def request(self, method, url, **kwargs):
        pfad = url[len(API) :]
        if pfad == "/users/@me/guilds":
            return Antwort(
                [{"id": GILDE, "name": "Die Runde"}, {"id": FREMDE_GILDE, "name": "Wer"}]
            )
        if pfad == f"/guilds/{GILDE}/channels":
            return Antwort(
                [
                    {"id": DIKTAT_KANAL, "name": "diktat", "type": 0},
                    {"id": CHRONIK_KANAL, "name": self.kanal, "type": 0},
                ]
            )
        if pfad == f"/guilds/{FREMDE_GILDE}/channels":
            return Antwort([{"id": FREMDER_KANAL, "name": KANAL, "type": 0}])
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


@pytest.fixture
def gastgeber(config):
    """Die Runde dieser Tests — mit ihrer Gilde.

    Ohne sie gäbe es keinen Ort, an den etwas gehen könnte: gesucht wird der Kanal in der
    Gilde der Runde, nicht in irgendeiner, in der der Bot zufällig auch steht.
    """
    return runden.anlegen(config.database_path, "Der Krumme Ast", guild_id=GILDE)


def sitzung(gastgeber, *, played_on="2026-08-06"):
    scope = db.scoped(gastgeber)
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
                (scope.runde_id, played_on, "Der Keller", STAND),
            )
        return zeiger.lastrowid
    finally:
        scope.close()


def protokoll(gastgeber, sitzung_id, text=TEXT, kind=RUECKBLICK):
    scope = db.scoped(gastgeber)
    try:
        with scope:
            scope.execute(
                "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope.runde_id, sitzung_id, kind, text, STAND),
            )
    finally:
        scope.close()


def zugestellt_am(gastgeber, sitzung_id):
    scope = db.scoped(gastgeber)
    try:
        zeile = scope.execute(
            "SELECT delivered_at FROM protocol WHERE runde_id = ? AND session_id = ? AND kind = ?",
            (scope.runde_id, sitzung_id, RUECKBLICK),
        ).fetchone()
    finally:
        scope.close()
    return zeile["delivered_at"]


def zustellen(config, gastgeber, api):
    return deliver(
        config,
        gastgeber,
        _einzige_sitzung(gastgeber),
        client=DiscordClient(config, http=lambda: api),
    )


def _einzige_sitzung(gastgeber):
    scope = db.scoped(gastgeber)
    try:
        return scope.execute(
            "SELECT id FROM session WHERE runde_id = ?", (scope.runde_id,)
        ).fetchone()["id"]
    finally:
        scope.close()


# --- Genau einmal --------------------------------------------------------------------


def test_der_rueckblick_geht_als_embed_in_den_gruppenkanal(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord()

    zustellung = zustellen(config, gastgeber, api)

    assert api.gepostet == [(CHRONIK_KANAL, {"description": RUMPF, "title": TITEL})]
    assert zustellung == rueckblick.Zustellung(rueckblick.ZUGESTELLT.format(sitzung=sitzung_id))
    assert zugestellt_am(gastgeber, sitzung_id) is not None


def test_die_ueberschriften_der_deutung_stehen_auch_im_embed(config, gastgeber):
    """Was belegt ist und was gedeutet, muss der Kanal genauso zeigen wie das Protokoll."""
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord()

    zustellen(config, gastgeber, api)

    beschreibung = api.gepostet[0][1]["description"]
    assert "### Offene Fäden — Deutung des Modells, keine Fakten" in beschreibung
    assert "### Was bisher geschah — vom Sprachmodell, nicht belegt" in beschreibung
    assert "<" not in beschreibung


def test_ein_zweiter_lauf_stellt_nicht_noch_einmal_zu(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord()

    zustellen(config, gastgeber, api)
    zweite = zustellen(config, gastgeber, api)

    assert len(api.gepostet) == 1
    assert zweite.meldung == rueckblick.SCHON_ZUGESTELLT.format(sitzung=sitzung_id)
    assert not zweite.gescheitert


def test_eine_neu_komponierte_fassung_wird_nicht_noch_einmal_gepostet(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord()
    zustellen(config, gastgeber, api)

    scope = db.scoped(gastgeber)
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

    zweite = zustellen(config, gastgeber, api)
    assert zweite.meldung == rueckblick.SCHON_ZUGESTELLT.format(sitzung=sitzung_id)
    assert len(api.gepostet) == 1


def test_nur_der_rueckblick_geht_hinaus_nicht_die_chronik(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id, text="Die ganze Chronik mit allen Zahlen.", kind=KIND)
    api = FakeDiscord()

    zustellung = zustellen(config, gastgeber, api)

    assert api.gepostet == []
    assert zustellung.meldung == rueckblick.KEIN_RUECKBLICK.format(sitzung=sitzung_id)
    assert not zustellung.gescheitert


# --- Zwei Formen desselben Kanals, und keine über die Gilde hinaus ---------------------


def test_ein_in_setup_gewaehlter_kanal_wird_wirklich_beliefert(config, gastgeber):
    """Der Fehler aus #182: ``/setup`` legt die Id ab, gesucht wurde nach dem Namen.

    Der Test geht bis zum echten Absenden — eine Zustellung, die nur bis zur Auflösung
    des Kanals käme, hätte den Fehler nicht gezeigt: dort war er ja.
    """
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    settings.save(gastgeber, {"discord_recap_channel": CHRONIK_KANAL})
    api = FakeDiscord()

    zustellung = zustellen(config, gastgeber, api)

    assert api.gepostet == [(CHRONIK_KANAL, {"description": RUMPF, "title": TITEL})]
    assert not zustellung.gescheitert
    assert zugestellt_am(gastgeber, sitzung_id) is not None


def test_ein_kanalname_aus_der_zeit_vor_setup_wird_weiter_beliefert(config, gastgeber):
    """Runden von vorher tragen den Namen — sie werden nicht gewandert, sondern verstanden."""
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    settings.save(gastgeber, {"discord_recap_channel": KANAL})
    api = FakeDiscord()

    zustellung = zustellen(config, gastgeber, api)

    assert api.gepostet == [(CHRONIK_KANAL, {"description": RUMPF, "title": TITEL})]
    assert not zustellung.gescheitert


def test_eine_kanal_id_aus_einer_fremden_gilde_ist_nicht_erreichbar(config, gastgeber):
    """Die Trennung zwischen Runden: der Kanal der Nachbarn bleibt der Nachbarn."""
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    settings.save(gastgeber, {"discord_recap_channel": FREMDER_KANAL})
    api = FakeDiscord()

    zustellung = zustellen(config, gastgeber, api)

    assert api.gepostet == []
    assert zustellung.gescheitert
    assert FREMDER_KANAL not in zustellung.meldung
    assert zugestellt_am(gastgeber, sitzung_id) is None


def test_ein_gleichnamiger_kanal_der_nachbarn_bekommt_den_rueckblick_nicht(config, gastgeber):
    """»chronik« heißt in jeder zweiten Gilde ein Kanal — geliefert wird in die eigene."""
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord()

    zustellen(config, gastgeber, api)

    assert [kanal for kanal, _ in api.gepostet] == [CHRONIK_KANAL]


# --- Ohne Einrichtung passiert nichts, und das steht da ------------------------------


def test_ohne_zustellkanal_bleibt_die_zustellung_aus(tmp_path):
    config = Config(discord_bot_token=TOKEN, data_dir=tmp_path / "daten")
    db.init(config.database_path)
    eine = runden.anlegen(config.database_path, "Der Krumme Ast", guild_id=GILDE)
    sitzung_id = sitzung(eine)
    protokoll(eine, sitzung_id)
    api = FakeDiscord()

    zustellung = zustellen(config, eine, api)

    assert zustellung.meldung == rueckblick.KEIN_ZUSTELLKANAL
    assert not zustellung.gescheitert
    assert api.gepostet == []
    assert zugestellt_am(eine, sitzung_id) is None


def test_ohne_bot_token_bleibt_die_zustellung_aus(tmp_path):
    config = Config(discord_recap_channel=KANAL, data_dir=tmp_path / "daten")
    db.init(config.database_path)
    eine = runden.anlegen(config.database_path, "Der Krumme Ast", guild_id=GILDE)
    sitzung_id = sitzung(eine)
    protokoll(eine, sitzung_id)

    assert deliver(config, eine, sitzung_id).meldung == rueckblick.NICHT_EINGERICHTET
    assert zugestellt_am(eine, sitzung_id) is None


def test_ohne_gilde_stellt_die_runde_nicht_zu_und_sagt_es(config):
    """Eine Runde ohne Gilde hat keinen Ort — dann wird nicht geraten, sondern gesagt."""
    ohne = runden.anlegen(config.database_path, "Aus der Zeit der Oberfläche")
    sitzung_id = sitzung(ohne)
    protokoll(ohne, sitzung_id)
    api = FakeDiscord()

    zustellung = zustellen(config, ohne, api)

    assert zustellung.meldung == rueckblick.OHNE_GILDE.format(sitzung=sitzung_id)
    assert zustellung.gescheitert
    assert api.gepostet == []
    assert zugestellt_am(ohne, sitzung_id) is None


def test_ein_in_der_oberflaeche_gesetzter_kanal_gewinnt(tmp_path):
    config = Config(
        discord_bot_token=TOKEN, discord_recap_channel="alt", data_dir=tmp_path / "daten"
    )
    db.init(config.database_path)
    eine = runden.anlegen(config.database_path, "Der Krumme Ast", guild_id=GILDE)
    sitzung_id = sitzung(eine)
    protokoll(eine, sitzung_id)
    settings.save(eine, {"discord_recap_channel": f"#{KANAL}"})
    api = FakeDiscord()

    zustellung = zustellen(config, eine, api)

    assert api.gepostet == [(CHRONIK_KANAL, {"description": RUMPF, "title": TITEL})]
    assert zustellung.meldung == rueckblick.ZUGESTELLT.format(sitzung=sitzung_id)


def test_ohne_den_kanal_sagt_der_lauf_das_statt_still_zu_stehen(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord(kanal="plauderei")

    zustellung = zustellen(config, gastgeber, api)

    assert zustellung.meldung == rueckblick.KEIN_KANAL.format(sitzung=sitzung_id)
    assert zustellung.gescheitert
    assert api.gepostet == []
    assert zugestellt_am(gastgeber, sitzung_id) is None


def test_ein_unerreichbares_discord_verschiebt_die_zustellung_ohne_token(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)

    class Weg:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError(f"Bot {TOKEN} abgelehnt")

    zustellung = deliver(config, gastgeber, sitzung_id, client=DiscordClient(config, http=Weg))

    assert "nicht zugestellt" in zustellung.meldung
    assert zustellung.gescheitert
    assert TOKEN not in zustellung.meldung
    assert zugestellt_am(gastgeber, sitzung_id) is None


# --- Die Maße eines Embeds -----------------------------------------------------------


def test_ein_zu_langer_rueckblick_wird_ehrlich_gekuerzt_und_zeigt_auf_die_datei(
    config, gastgeber, caplog
):
    """Ein Rückblick passt per Bauart hinein; passt er doch nicht, wird nicht aufgeteilt."""
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id, text="Wort " * 1200)
    api = FakeDiscord()

    with caplog.at_level("WARNING"):
        zustellen(config, gastgeber, api)

    beschreibung = api.gepostet[0][1]["description"]
    assert len(api.gepostet) == 1
    assert len(beschreibung) <= rueckblick.TEXT_GRENZE
    assert beschreibung.endswith(rueckblick.GEKUERZT)
    assert beschreibung.startswith("Wort Wort")
    assert "5999 Zeichen" in caplog.text
    assert zugestellt_am(gastgeber, sitzung_id) is not None


def test_ein_zu_langer_titel_wird_gekappt(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id, text="# " + "Titel " * 100 + "\n\nKurz.\n")
    api = FakeDiscord()

    zustellen(config, gastgeber, api)

    assert len(api.gepostet[0][1]["title"]) == rueckblick.TITEL_GRENZE


def test_ein_rueckblick_ohne_titelzeile_bekommt_keinen_leeren_titel(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id, text="Nur ein Absatz.\n")
    api = FakeDiscord()

    zustellen(config, gastgeber, api)

    assert api.gepostet[0][1] == {"description": "Nur ein Absatz."}


def test_ein_kurzer_rueckblick_bleibt_unangetastet(config, gastgeber, caplog):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord()

    with caplog.at_level("WARNING"):
        zustellen(config, gastgeber, api)

    assert api.gepostet[0][1]["description"] == RUMPF
    assert "gekürzt" not in caplog.text


def test_der_token_steht_in_keiner_meldung_und_keiner_logzeile(config, gastgeber, caplog):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id, text="Wort " * 1200)
    api = FakeDiscord()

    with caplog.at_level("DEBUG"):
        zustellung = zustellen(config, gastgeber, api)

    assert TOKEN not in caplog.text
    assert TOKEN not in zustellung.meldung
