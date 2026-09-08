"""Die Zustellung des Rückblicks — gegen ein nachgebautes Discord, ohne Netz.

Der Token in diesen Tests ist erfunden und steht nur hier. Gepostet wird über dasselbe
REST-API wie der Diktat-Kanal; eine Gateway-Verbindung braucht Schreiben nicht.

Der Rückblick geht als **Embed** hinaus. Was diese Suite festhält: sein Text wird dabei
nicht umgeschrieben — die Überschriften, die Belegtes von Gedeutetem trennen, stehen im
Embed genauso wie im abgelegten Protokoll.

Und seit #182: der eingestellte Kanal kommt in **zwei** Formen — als Id aus ``/chronicle setup`` und
als Name aus der Zeit davor —, beide werden beliefert, keine über die Gilde der Runde
hinaus, und was nicht ankommt, steht als gescheitert im Ergebnis statt im Nichts.
"""

from __future__ import annotations

import pytest
import requests

from chronicle import db
from chronicle import runde as runden
from chronicle.compose.composer import SceneMaterial, SessionMaterial, compose, fact_line
from chronicle.compose.recap import RecapMaterial, recap
from chronicle.compose.service import KIND, RUECKBLICK
from chronicle.config import Config
from chronicle.discord import rueckblick
from chronicle.discord.ausgabe import anhaengen
from chronicle.discord.client import API, DiscordClient
from chronicle.discord.rueckblick import deliver
from chronicle.foundry.model import ChatMessage, Die, Roll

TOKEN = "bot-token-nur-fuer-den-test"

GILDE = "g-runde"
CHRONIK_KANAL = "c-chronik"
DIKTAT_KANAL = "c-diktat"

FREMDE_GILDE = "g-nachbarn"
FREMDER_KANAL = "c-nachbarchronik"

KANAL = "chronik"

STAND = "2026-08-06T20:00:00+00:00"

THREAD = "t-4711"
DATUM = "2026-08-06"

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


class Abgewiesen:
    """Was Discord antwortet, wenn der Bot in diesem Kanal nicht schreiben darf."""

    status_code = 403
    text = '{"message": "Missing Permissions", "code": 50013}'

    def raise_for_status(self):
        raise requests.HTTPError("403 Client Error", response=self)

    def json(self):
        return {}


class FakeDiscord:
    """Discords REST-API, so weit die Zustellung sie braucht.

    Zwei Gilden, und in beiden ein Kanal mit demselben Namen — genau daran hängt die
    Trennung: eine Suche über alle Gilden fände hier die falsche.

    Angenommen wird beides, Embed und Anhang, und getrennt vermerkt: die Chronik und der
    Rückblick gehen verschiedene Wege (#261), und ein Gegenüber, das nur einen davon kennt,
    könnte den Unterschied nicht zeigen. ``verweigert`` sind die Kanäle, in denen der Bot
    nicht schreiben darf — im echten Discord eine Rechteüberschreibung am einzelnen Kanal.
    """

    def __init__(self, *, kanal=KANAL, verweigert=()):
        self.kanal = kanal
        self.verweigert = set(verweigert)
        self.gepostet = []
        self.angehaengt = []

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
            ziel = pfad.split("/")[2]
            if ziel in self.verweigert:
                return Abgewiesen()
            if "files" in kwargs:
                self.angehaengt.append((ziel, kwargs["files"]["files[0]"][0]))
                return Antwort({})
            (eingebettet,) = kwargs["json"]["embeds"]
            self.gepostet.append((ziel, eingebettet))
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


def sitzung(gastgeber, *, played_on=DATUM, kanal_id=KANAL):
    """Eine Sitzung — **mit** Kanal, denn seit #359 geht der Rückblick dorthin.

    Vorher war ``None`` die Vorgabe: der Kanal der Sitzung spielte für den Rückblick keine
    Rolle, er ging in den gesondert eingestellten. Wer den Fall ohne Kanal prüfen will,
    übergibt ihn jetzt ausdrücklich.
    """
    scope = db.scoped(gastgeber)
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO session (runde_id, played_on, title, created_at, kanal_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope.runde_id, played_on, "Der Keller", STAND, kanal_id),
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


def test_der_rueckblick_geht_als_embed_in_den_kanal_seiner_sitzung(config, gastgeber):
    """Seit #359 in den Kanal der Sitzung — dorthin, wo der Abend stattfand."""
    sitzung_id = sitzung(gastgeber, kanal_id=THREAD)
    protokoll(gastgeber, sitzung_id)
    api = FakeDiscord()

    zustellung = zustellen(config, gastgeber, api)

    assert api.gepostet == [(THREAD, {"description": RUMPF, "title": TITEL})]
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


def test_ohne_zustellkanal_bleibt_die_zustellung_aus(tmp_path):
    """Seit #359 ist das die Sitzung **ohne Kanal** — aus der Zeit vor Discord.

    Vorher war es eine Runde ohne eingestellten Zustellkanal. Den gibt es nicht mehr; der
    Rückblick geht in den Kanal seiner Sitzung, und Sitzungen von damals haben keinen.
    """
    config = Config(discord_bot_token=TOKEN, data_dir=tmp_path / "daten")
    db.init(config.database_path)
    eine = runden.anlegen(config.database_path, "Der Krumme Ast", guild_id=GILDE)
    sitzung_id = sitzung(eine, kanal_id=None)
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


def test_ein_unerreichbares_discord_verschiebt_die_zustellung_ohne_token(config, gastgeber):
    sitzung_id = sitzung(gastgeber)
    protokoll(gastgeber, sitzung_id)

    class Weg:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError(f"Bot {TOKEN} abgelehnt")

    zustellung = deliver(config, gastgeber, sitzung_id, client=DiscordClient(config, http=Weg))

    assert "not delivered" in zustellung.meldung
    assert zustellung.gescheitert
    assert TOKEN not in zustellung.meldung
    assert zugestellt_am(gastgeber, sitzung_id) is None


# --- Zwei Wege, und was passiert, wenn einer davon zu ist (#261) ---------------------


def _beide_wege(config, gastgeber, api, sitzung_id):
    """Beide Zustellungen desselben Laufs, in der Reihenfolge aus ``kette.schreiben``."""
    bot = DiscordClient(config, http=lambda: api)
    zustellung = deliver(config, gastgeber, sitzung_id, client=bot)
    return zustellung, anhaengen(config, gastgeber, sitzung_id, client=bot)


def _beide_protokolle(gastgeber):
    sitzung_id = sitzung(gastgeber, kanal_id=THREAD)
    protokoll(gastgeber, sitzung_id)
    protokoll(gastgeber, sitzung_id, text="# Chronik\n\nAlles, was geschah.\n", kind=KIND)
    return sitzung_id


def test_chronik_und_rueckblick_gehen_in_denselben_kanal(config, gastgeber):
    """Betreiber-Entscheidung 2026-09-08 (#359): **ein** Ziel, weiterhin zwei Formen.

    #261 hielt hier das Gegenteil fest — verschiedenes Ziel, verschiedene Form —, damit
    niemand aus »die Chronik kam an« schließt, der Rückblick müsste es auch. Der Grund
    dafür ist eingetreten und war teuer: der Rückblick ging in einen gesondert
    eingestellten Kanal, den der Bot nicht einmal sehen durfte, und kam bei **keiner**
    Sitzung je an — drei Wochen lang unbemerkt, weil die Chronik daneben zuverlässig
    ankam.

    Zwei Ziele hießen zwei Rechtelagen, von denen eine still falsch war. Eines heißt: wer
    den Abend sieht, sieht auch den Rückblick.

    Die **Formen** bleiben getrennt: der Rückblick als Embed, die Chronik als Datei — sie
    passt in kein Embed.
    """
    sitzung_id = _beide_protokolle(gastgeber)
    api = FakeDiscord()

    _beide_wege(config, gastgeber, api, sitzung_id)

    assert [ziel for ziel, _ in api.gepostet] == [THREAD]
    assert api.angehaengt == [(THREAD, f"chronik-{DATUM}.md")]


def test_ein_verweigerter_kanal_trifft_jetzt_beide_wege(config, gastgeber, caplog):
    """Der Preis eines gemeinsamen Ziels (#359), ausdrücklich festgehalten.

    Vorher lagen Rückblick und Chronik in verschiedenen Kanälen; ein gesperrter traf nur
    einen von beiden. Seit sie denselben Kanal nehmen, trifft eine fehlende Berechtigung
    **beide**. Das ist die Kehrseite der Vereinfachung und kein Versehen: eine Rechtelage
    statt zweier, dafür ohne Rückfallweg.

    Der Tausch ist bewusst so entschieden. Zwei Ziele hießen zwei Rechtelagen, von denen
    eine drei Wochen lang still falsch war — der Rückblick kam bei keiner Sitzung an, und
    niemand bemerkte es, weil die Chronik daneben ankam. Ein Ausfall, den man sieht, ist
    besser als einer, den man nicht sieht.
    """
    sitzung_id = _beide_protokolle(gastgeber)
    api = FakeDiscord(verweigert=(THREAD,))

    with caplog.at_level("WARNING"):
        zustellung, ausgabe = _beide_wege(config, gastgeber, api, sitzung_id)

    assert zustellung.gescheitert
    assert "HTTP 403" in zustellung.meldung
    assert TOKEN not in caplog.text and TOKEN not in zustellung.meldung
    assert zugestellt_am(gastgeber, sitzung_id) is None
    # Und die Chronik ebenso — beide melden es, keine tut still so, als sei sie durch.
    assert ausgabe != ""
    assert api.angehaengt == []


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


WUERFE = 40


def _abend_mit_vierzig_wuerfen() -> tuple[str, str]:
    """Der gemessene echte Abend vom 2026-08-06: 59 Nachrichten, 40 davon mit Würfen.

    Ohne Sprachmodell — die geordnete Fassung reißt die Embed-Grenze allein aus dem
    Belegt-Block, und genau der ist hier der Prüfling.
    """
    nachrichten = [
        ChatMessage(
            id=str(nr),
            timestamp=nr,
            speaker_alias="Kraw",
            roll=Roll(
                title="Duality Roll",
                total=25,
                formula="1d12 + 1d12 + 2",
                kind="DualityRoll",
                modifier_total=2,
                dice=(Die("hope", "d12", 11), Die("fear", "d12", 12)),
            ),
        )
        for nr in range(WUERFE)
    ]
    szenen = tuple(
        SceneMaterial(
            nr + 1,
            f"Der Turm {nr}",
            notes=("Borin: wir gehen weiter",),
            facts=tuple(nachrichten[nr * 5 : (nr + 1) * 5]),
        )
        for nr in range(WUERFE // 5)
    )
    chronik = compose(SessionMaterial(1, DATUM, "Der Turm", szenen), None)
    rueckschau = recap(RecapMaterial(1, DATUM, "Der Turm", chronicle=chronik.text), None)
    assert rueckschau.fact_count == WUERFE
    return rueckschau.text, f"- {fact_line(nachrichten[0])}"


def test_ein_abend_mit_vierzig_wuerfen_verliert_keine_halbe_faktenzeile(config, gastgeber):
    """Gekappt wird an der Zeilengrenze — und der Hinweis sagt, wie viele Fakten fehlen."""
    sitzung_id = sitzung(gastgeber)
    text, ganze_zeile = _abend_mit_vierzig_wuerfen()
    assert len(text) > rueckblick.TEXT_GRENZE
    protokoll(gastgeber, sitzung_id, text=text)
    api = FakeDiscord()

    zustellen(config, gastgeber, api)

    beschreibung = api.gepostet[0][1]["description"]
    assert len(beschreibung) <= rueckblick.TEXT_GRENZE
    zugestellt = [z for z in beschreibung.splitlines() if z.startswith("- Kraw — ")]
    # Keine geköpfte Faktenzeile: was ankommt, steht ganz da — Formel, Summe und beide Würfel.
    assert 0 < len(zugestellt) < WUERFE
    assert all(zeile == ganze_zeile for zeile in zugestellt)
    assert beschreibung.endswith(
        rueckblick.GEKUERZT_FAKTEN.format(fehlend=WUERFE - len(zugestellt), gesamt=WUERFE)
    )


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
