"""Erinnern per Befehl — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Die Sätze, die dieser Suite ihren Sinn geben: **ein Treffer führt dorthin zurück, wo er
steht**, **ein Vorschlag wird nie von allein zur Tatsache** — und ein zweiter Klick auf
denselben Knopf bekommt eine ehrliche Auskunft statt eines Fehlers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import types

import pytest
from conftest import runde as erste_runde
from test_bot import (
    TOKEN,
    FakeAnhang,
    FakeAntwort,
    FakeBot,
    FakeButton,
    FakeIntents,
    FakeMitglied,
    FakePCMAudio,
    FakePermissions,
    FakeSelect,
    FakeSelectOption,
    FakeSenke,
    FakeTextkanal,
    FakeView,
)
from test_chronik import FakeHTTPException, FakeInputText, FakeModal

from chronicle import (
    consent,
    db,
    lebenszyklus,
    nightly,
    notes,
    people,
    recordings,
    register,
    settings,
)
from chronicle import runde as runden
from chronicle.bot import chronik, erinnern, gateway
from chronicle.compose import service as compose_service
from chronicle.config import Config
from chronicle.discord import grenzen
from chronicle.foundry import store as foundry_store
from chronicle.foundry.model import Character, Player, WorldSnapshot

GILDE = "1101"
FREMDE_GILDE = "9909"

SITZUNGSKANAL = "5001"
NACHRICHT = "7001"

# Zahlen und keine sprechenden Kennungen: Discord vergibt Zahlen, und wer ein übernommenes
# Konto seiner Vorbesitzerin ansagt, schlägt sie damit im Zwischenspeicher des Bots nach.
MIRA = "4001"
BROK = "4002"


# -- Die Attrappen ----------------------------------------------------------------------


class FakeEmbed:
    def __init__(self, gebaut):
        self.gebaut = gebaut

    @classmethod
    def from_dict(cls, gebaut):
        return cls(gebaut)


class FakeCtx:
    def __init__(self, *, guild_id=GILDE):
        self.guild_id = guild_id
        self.channel_id = 900
        self.antworten: list[str | None] = []
        self.embeds: list = []
        self.ansichten: list = []
        self.fluechtig: list[bool] = []

    async def defer(self, **rest):
        pass

    async def respond(self, text=None, *, embed=None, view=None, ephemeral=False, **rest):
        self.antworten.append(text)
        self.embeds.append(embed)
        self.ansichten.append(view)
        self.fluechtig.append(ephemeral)

    # Die Nachfrage zum Register geht seit #272 nicht mehr als Antwort auf einen Befehl
    # hinaus, sondern in den Kanal des Abends — derselbe Doppelgänger tut beides.
    async def send(self, text=None, **rest):
        await self.respond(text, **rest)


class FakeInteraction:
    def __init__(self, *, guild_id=GILDE):
        self.guild_id = guild_id
        self.response = FakeAntwort()


class FakeZielkanal:
    """Der Zustellkanal aus dem Setup — dorthin, wo auch der Rückblick landet."""

    def __init__(self, kennung, name):
        self.id = kennung
        self.name = name
        self.antworten: list[str | None] = []
        self.ansichten: list = []

    async def send(self, text=None, *, view=None, **rest):
        self.antworten.append(text)
        self.ansichten.append(view)


class FakeZielgilde:
    def __init__(self, *kanaele, kennung=int(GILDE)):
        self.id = kennung
        self.text_channels = list(kanaele)


# -- Die Bühne --------------------------------------------------------------------------


class FakeOption:
    """Das Feld eines Slash-Befehls, so weit die Doppel es brauchen.

    Es hält seinen ``input_type`` wie das echte: dort muss eine **Klasse** stehen. Eine
    Zeichenkette ist der Fehler, an dem py-cord beim ersten echten Aufruf stirbt.
    """

    def __init__(self, input_type, description="", default=None, required=True, **rest):
        self.input_type = input_type
        self.description = description
        self.default = default
        self.required = required

    def __repr__(self) -> str:
        return f"FakeOption({self.input_type!r}, default={self.default!r})"


@pytest.fixture
def pycord(monkeypatch):
    modul = types.ModuleType("discord")
    modul.Option = FakeOption
    modul.Intents = FakeIntents
    modul.Bot = FakeBot
    modul.Permissions = FakePermissions
    modul.PCMAudio = FakePCMAudio
    modul.HTTPException = FakeHTTPException
    modul.Embed = FakeEmbed
    modul.SelectOption = FakeSelectOption
    modul.Attachment = FakeAnhang
    senken = types.ModuleType("discord.sinks")
    senken.Sink = FakeSenke
    modul.sinks = senken
    werkzeug = types.ModuleType("discord.utils")
    werkzeug.get_missing_voice_dependencies = lambda: ()
    modul.utils = werkzeug
    oberflaeche = types.ModuleType("discord.ui")
    oberflaeche.Modal = FakeModal
    oberflaeche.InputText = FakeInputText
    oberflaeche.View = FakeView
    oberflaeche.Button = FakeButton
    oberflaeche.Select = FakeSelect
    modul.ui = oberflaeche
    monkeypatch.setitem(sys.modules, "discord", modul)
    monkeypatch.setattr(FakeBot, "erzeugt", [])
    return modul


@pytest.fixture
def stelle(tmp_path):
    config = Config(
        discord_bot_token=TOKEN,
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "aufnahmen",
    )
    db.init(config.database_path)
    return config, runden.anlegen(config.database_path, "Der Krumme Ast", guild_id=GILDE)


@pytest.fixture
def bot(stelle, pycord):
    config, _unsere = stelle
    return gateway.baue(config)


def sitzung_mit_notiz(
    runde, *, kanal=SITZUNGSKANAL, nachricht=NACHRICHT, text="Im Keller lag ein Schwert."
):
    sitzung = notes.create_session(runde, played_on="2026-05-01", kanal_id=kanal)
    szene = notes.session(runde, sitzung).scenes[0]
    notes.add_note(runde, szene.id, text, message_id=nachricht)
    return sitzung


def chronik_ablegen(runde, sitzung, text):
    scope = db.scoped(runde)
    try:
        with scope:
            compose_service.save(scope, sitzung, text, "2026-05-01T21:00:00+00:00")
    finally:
        scope.close()


def eintrag_anlegen(runde, sitzung, *, kind, name, satz, state=register.VORSCHLAG):
    scope = db.scoped(runde)
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO register_entry (runde_id, kind, name, description, state, "
                "suggested_at) VALUES (?, ?, ?, ?, ?, '2026-05-01T21:00:00')",
                (scope.runde_id, kind, name, satz, state),
            )
            eintrag = int(zeiger.lastrowid)
            scope.execute(
                "INSERT INTO register_mention (runde_id, entry_id, session_id, scene_id) "
                "VALUES (?, ?, ?, NULL)",
                (scope.runde_id, eintrag, sitzung),
            )
    finally:
        scope.close()
    return eintrag


def erwaehnung_anlegen(runde, eintrag, sitzung):
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "INSERT INTO register_mention (runde_id, entry_id, session_id, scene_id) "
                "VALUES (?, ?, ?, NULL)",
                (scope.runde_id, eintrag, sitzung),
            )
    finally:
        scope.close()


# Der NSC gehört in jede Welt dieser Suite: die Spielleitung besitzt in Foundry auch die
# Wirtin, und ihr Name darf niemanden zu einer Person machen. Er liegt hier bei ``u-mira``
# und nicht bei einem Leitungskonto — dann hängt der Fall am Aktor-Typ und an sonst nichts.
WIRTIN = "Die Wirtin zum Krummen Ast"


def welt_ablegen(runde, *, spielername="Mira", weitere=0, zwilling=False, gastfigur=False):
    scope = db.scoped(runde)
    try:
        foundry_store.save(
            scope,
            WorldSnapshot(
                system="daggerheart",
                fetched_at="2026-05-01T20:00:00+00:00",
                players=(
                    Player(id="u-mira", name=spielername, role=1, is_gm=False),
                    *(
                        (Player(id="u-zwilling", name=spielername, role=1, is_gm=False),)
                        if zwilling
                        else ()
                    ),
                    # Ein Konto, das anders heißt als sein Spielername — seine **Figur**
                    # trägt den Namen. Damit trifft derselbe Discord-Name zwei Konten von
                    # zwei Seiten, ohne dass zwei Konten gleich heißen müssten.
                    *(
                        (Player(id="u-gast", name="Gastkonto", role=1, is_gm=False),)
                        if gastfigur
                        else ()
                    ),
                    *(
                        Player(id=f"u-{nummer}", name=f"Konto {nummer:02d}", role=1, is_gm=False)
                        for nummer in range(weitere)
                    ),
                ),
                characters=(
                    *(
                        (
                            Character(
                                id="a-gast",
                                name=spielername,
                                type="character",
                                owner_ids=("u-gast",),
                            ),
                        )
                        if gastfigur
                        else ()
                    ),
                    Character(
                        id="a-aelin",
                        name="Aelin Sturmwind",
                        type="character",
                        owner_ids=("u-mira",),
                    ),
                    Character(id="a-wirtin", name=WIRTIN, type="npc", owner_ids=("u-mira",)),
                ),
            ),
        )
    finally:
        scope.close()


def aufgenommen(runde, sitzung, *mitglieder):
    consent.record(
        runde,
        session_id=sitzung,
        kind=consent.ANSAGE,
        guild_id=GILDE,
        channel_id="kanal",
        channel_name="Runde",
        text="Ansage",
        members=tuple(consent.Member(id=kennung, name=name) for kennung, name in mitglieder),
    )


def befehl(bot, name):
    return bot.gruppen[gateway.GRUPPE_CHRONIK].befehle[name]


def registerfrage(stelle, ziel):
    """Was bis #272 ``/register offen`` war: die Frage stellt jetzt der Bot von selbst.

    Sie hängt am Ende des Laufs und geht in den Kanal des Abends — der Befehl davor
    wartete darauf, dass jemand ihn kennt, und am 2026-08-18 kannte ihn niemand.
    """
    config, unsere = stelle
    asyncio.run(gateway._register_nachfragen(config, unsere, ziel))


def knoepfe(view):
    return [teil for teil in view.items if isinstance(teil, FakeButton) and not teil.disabled]


def klicken(view, eintrag_id, art):
    kennung = f"{gateway.KENNUNG_ENTSCHEIDUNG}:{eintrag_id}:{art or 'nein'}"
    (knopf,) = [teil for teil in knoepfe(view) if teil.custom_id == kennung]
    interaktion = FakeInteraction()
    asyncio.run(knopf.callback(interaktion))
    return interaktion


def waehlen(view, user_id, wert):
    kennung = f"{gateway.KENNUNG_ZUORDNUNG}:{user_id}"
    (menue,) = [teil for teil in view.items if teil.custom_id == kennung]
    menue.values = [wert]
    interaktion = FakeInteraction()
    asyncio.run(menue.callback(interaktion))
    return interaktion


# -- Der Bot bringt die Befehle mit -------------------------------------------------------


def test_der_bot_bringt_die_erinnern_befehle_mit(bot):
    unter_chronicle = set(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle)

    assert {gateway.BEFEHL_SUCHE, gateway.BEFEHL_WER, "zuordnung"} <= unter_chronicle
    # `/register offen` ist fort: die Vorschläge kommen seit #272 als Nachfrage des Bots.
    assert "register" not in bot.gruppen


def test_die_hilfe_nennt_die_wege_zum_erinnern(bot):
    ctx = FakeCtx()

    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["help"](ctx))

    # Geteilt seit `/session check` — gelesen wird die Hilfe als Ganzes.
    antwort = "".join(ctx.antworten)
    assert antwort == gateway.HILFE
    for satzteil in ("/chronicle search", "/chronicle who"):
        assert satzteil in antwort
    # Zuordnung und Registervorschläge stehen nicht mehr darin: die Frage danach stellt
    # der Bot von selbst (#272).
    for fort in ("/register offen", "/chronicle zuordnung"):
        assert fort not in antwort


def test_ohne_runde_fuer_diesen_server_wird_nichts_gesucht(stelle, bot):
    ctx = FakeCtx(guild_id=FREMDE_GILDE)

    asyncio.run(befehl(bot, gateway.BEFEHL_SUCHE)(ctx, "Schwert"))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_RUNDE in antwort


# -- Suchen ------------------------------------------------------------------------------


def test_ein_treffer_fuehrt_in_die_nachricht_zurueck(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    chronik_ablegen(unsere, sitzung, "# Chronik\n\nDas Schwert lag im Keller.")
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_SUCHE)(ctx, "Schwert"))

    (embed,) = ctx.embeds
    arten = {feld["name"] for feld in embed.gebaut["fields"]}
    assert arten == {"Notizen", "Chronik"}
    notizen = next(f for f in embed.gebaut["fields"] if f["name"] == "Notizen")["value"]
    assert f"https://discord.com/channels/{GILDE}/{SITZUNGSKANAL}/{NACHRICHT}" in notizen
    assert "**Schwert**" in notizen
    chronik = next(f for f in embed.gebaut["fields"] if f["name"] == "Chronik")["value"]
    assert f"https://discord.com/channels/{GILDE}/{SITZUNGSKANAL}" in chronik
    assert ctx.fluechtig == [True]


def test_ohne_treffer_kommt_ein_ehrlicher_satz(stelle, bot):
    _config, unsere = stelle
    sitzung_mit_notiz(unsere)
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_SUCHE)(ctx, "Drachenei"))

    assert ctx.embeds == [None]
    assert erinnern.NICHTS_GEFUNDEN.format(begriff="Drachenei") in ctx.antworten[0]


def test_ohne_begriff_wird_nachgefragt(stelle, bot):
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_SUCHE)(ctx, "   "))

    assert ctx.antworten == [erinnern.OHNE_BEGRIFF]


def test_solange_nichts_geschrieben_ist_sagt_die_suche_das(stelle, bot):
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_SUCHE)(ctx, "Schwert"))

    assert ctx.antworten == [erinnern.NOCH_NICHTS]


def test_die_suche_verlaesst_die_runde_nicht(stelle, bot):
    config, unsere = stelle
    sitzung_mit_notiz(unsere)
    fremde = runden.anlegen(config.database_path, "Nebenan", guild_id=FREMDE_GILDE)
    sitzung_mit_notiz(fremde, kanal="6001", nachricht="8001", text="Nebenan lag ein Beil.")
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_SUCHE)(ctx, "Beil"))

    assert ctx.antworten[0] == erinnern.NICHTS_GEFUNDEN.format(begriff="Beil")


# -- Nachschlagen ------------------------------------------------------------------------


def test_ein_langes_embed_wird_gekappt_statt_abgewiesen(stelle, bot):
    """Ein Embed lässt sich nicht auf zwei Nachrichten verteilen — hier wird gekürzt.

    Und das muss es: ein **einziges** Feld über Discords Maß lässt die ganze Nachricht
    fallen. Nach zwei Jahren Kampagne hat eine wiederkehrende Figur so viele Erwähnungen.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere,
        sitzung,
        kind=register.FIGUR,
        name="Joras",
        satz="Ein Söldner aus dem Norden. " * 200,
        state=register.BESTAETIGT,
    )
    for nummer in range(40):
        weitere = notes.create_session(
            unsere, played_on=f"2026-06-{nummer % 28 + 1:02d}", kanal_id=f"60{nummer:02d}"
        )
        erwaehnung_anlegen(unsere, eintrag, weitere)
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_WER)(ctx, "joras"))

    (embed,) = ctx.embeds
    erwaehnt = next(
        feld["value"] for feld in embed.gebaut["fields"] if feld["name"] == erinnern.WER_ERWAEHNT
    )
    assert len(erwaehnt) <= grenzen.EMBED_FELD
    assert erwaehnt.endswith(erinnern.FELD_GEKUERZT)
    assert len(embed.gebaut["description"]) == grenzen.EMBED_TEXT
    # Gekappt wird an der Zeilengrenze: ein Schnitt mitten durch `[Titel](url)` ließe die
    # letzte Zeile als Rohtext samt halber Adresse dastehen.
    zeilen = erwaehnt.removesuffix(erinnern.FELD_GEKUERZT).split("\n")
    assert len(zeilen) < 40
    assert all(re.fullmatch(r"\[[^\]]+\]\(\S+\)", zeile) for zeile in zeilen)


def test_wer_zeigt_den_eintrag_mit_art_satz_und_sitzung(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(
        unsere,
        sitzung,
        kind=register.FIGUR,
        name="Joras",
        satz="Ein Söldner aus dem Norden.",
        state=register.BESTAETIGT,
    )
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_WER)(ctx, "joras"))

    (embed,) = ctx.embeds
    assert embed.gebaut["title"] == "Joras"
    assert embed.gebaut["description"] == "Ein Söldner aus dem Norden."
    felder = {feld["name"]: feld["value"] for feld in embed.gebaut["fields"]}
    assert felder[erinnern.WER_ART] == "Figur"
    assert f"https://discord.com/channels/{GILDE}/{SITZUNGSKANAL}" in felder[erinnern.WER_ERWAEHNT]


def test_ein_unbekannter_name_bekommt_eine_rueckfrage(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(
        unsere,
        sitzung,
        kind=register.FIGUR,
        name="Joras",
        satz="Ein Söldner.",
        state=register.BESTAETIGT,
    )
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_WER)(ctx, "Jorass"))

    (antwort,) = ctx.antworten
    assert erinnern.WER_UNBEKANNT.format(name="Jorass") in antwort
    assert "Joras" in antwort
    assert ctx.embeds == [None]


def test_ein_vorschlag_ist_noch_kein_registereintrag(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_WER)(ctx, "Joras"))

    assert ctx.embeds == [None]
    assert erinnern.WER_UNBEKANNT.format(name="Joras") in ctx.antworten[0]


# -- Bestätigen per Knopf ----------------------------------------------------------------


def test_der_lauf_zieht_die_frage_hinter_seine_meldung(stelle, bot):
    """Die Nachfrage hängt am **Ende des Laufs** und nicht mehr an einem Befehl (#272).

    Am 2026-08-18 warteten zwölf Vorschläge darauf, dass jemand ``/register offen`` kennt.
    Der Melder des Abschlusses stellt sie jetzt selbst — im Kanal des Abends, hinter dem
    Satz, mit dem sich der Lauf ohnehin meldet.
    """
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    ziel = FakeCtx()

    async def ablauf():
        gateway._melder_mit_register(config, unsere, ziel)("Die Chronik steht.")
        for _ in range(100):
            await asyncio.sleep(0)
            if len(ziel.antworten) > 1:
                return

    asyncio.run(ablauf())

    assert ziel.antworten[0] == "Die Chronik steht."
    assert [teil.label for teil in ziel.ansichten[-1].items] == [
        "Joras",
        "Figur",
        "Ort",
        "Faden",
        "Nein",
    ]


def test_ohne_kanal_bleibt_der_lauf_stumm_statt_zu_scheitern(stelle):
    """Der Abschluss von selbst kennt keinen Aufrufer, und der Kanal kann fortgeräumt sein."""
    config, unsere = stelle

    async def ablauf():
        gateway._melder_mit_register(config, unsere, None)("Die Chronik steht.")

    asyncio.run(ablauf())


def test_ohne_offene_vorschlaege_bleibt_es_still(stelle, bot):
    """Kein »nichts zu tun« nach jedem Abend (#272).

    Solange es ein Befehl war, gehörte die Antwort dazu: wer fragt, bekommt eine. Jetzt
    fragt der Bot von selbst — und eine Meldung, die nach jeder Sitzung dasselbe Nichts
    sagt, ist genau das Rauschen, wegen dem eine Runde aufhört hinzusehen.
    """
    ctx = FakeCtx()

    registerfrage(stelle, ctx)

    assert ctx.antworten == []
    assert ctx.ansichten == []


def test_je_vorschlag_eine_reihe_aus_name_arten_und_nein(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    ctx = FakeCtx()

    registerfrage(stelle, ctx)

    (view,) = ctx.ansichten
    assert [teil.label for teil in view.items] == ["Joras", "Figur", "Ort", "Faden", "Nein"]
    assert view.items[0].disabled
    assert {teil.row for teil in view.items} == {0}


# -- Und sie laufen nicht ins Leere ------------------------------------------------------


def test_die_registerknoepfe_hoeren_nicht_nach_einer_viertelstunde_auf(stelle, bot):
    """#281: mit Frist standen sie nach fünfzehn Minuten sichtbar und tot im Kanal.

    Der Befehl, der die Liste zurückholte, ist mit #272 entfallen — also darf die Ansicht
    nicht mehr ablaufen. Ohne Frist und mit fester ``custom_id`` je Knopf ist sie
    persistent: py-cord findet ihre Rückrufe darüber wieder.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    ctx = FakeCtx()

    registerfrage(stelle, ctx)

    (view,) = ctx.ansichten
    assert view.timeout is None
    assert all(teil.custom_id for teil in view.items)


def test_der_neustart_schliesst_die_offenen_vorschlaege_wieder_an(stelle, bot, monkeypatch):
    """Die Nachricht überlebt den Prozess, ihre Ansicht nicht — angeschlossen wird beim Start.

    Ohne dass jemand einen Befehl kennen muss: die Knöpfe von gestern tragen dieselben
    ``custom_id``s, und der Klick von heute entscheidet den Vorschlag von gestern.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)
    (gestern,) = ctx.ansichten

    async def nichts(config, **rest):
        return None

    monkeypatch.setattr(recordings, "taeglich", nichts)
    monkeypatch.setattr(lebenszyklus, "taeglich", nichts)

    async def anmelden():
        await bot.ereignisse["on_ready"]()
        # Die beiden Dauerläufe werden als Task gestellt; ohne diesen Takt endete die
        # Schleife mit zwei Tasks, die nie gelaufen sind.
        await asyncio.sleep(0)

    asyncio.run(anmelden())

    (frisch,) = bot.angeschlossen
    assert [teil.custom_id for teil in frisch.items] == [teil.custom_id for teil in gestern.items]
    klicken(frisch, eintrag, register.ORT)
    assert register.pending(unsere) == ()
    (gruppe,) = register.overview(unsere)
    assert (gruppe.kind, [eintrag.name for eintrag in gruppe.entries]) == (register.ORT, ["Joras"])


def test_ohne_offenen_vorschlag_wird_beim_start_nichts_angeschlossen(stelle, bot):
    """Eine Ansicht ohne Eintrag gibt es nicht — und eine leere anzuschließen hieße,
    py-cord eine Nachricht zu versprechen, die im Kanal gar nicht steht."""
    config, _unsere = stelle

    asyncio.run(gateway._vorschlaege_wieder_anschliessen(config, bot))

    assert bot.angeschlossen == []


def test_der_nachtlauf_fragt_im_zustellkanal_nach_seinen_vorschlaegen(stelle, bot):
    """#281, das zweite Loch: der Nachtlauf erzeugt dieselben Vorschläge wie ``/session done``.

    Sein Weg endete aber in der Datenbank — die Zeile »N Vorschläge warten« stand nur im
    Nachtbericht, und ein reiner Notizabend erzeugte damit Vorschläge, nach denen niemand
    je gefragt wurde. Gefragt wird jetzt dort, wo auch der Rückblick landet.
    """
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    kanal = FakeZielkanal(4242, "chronik")
    bot.gilden[int(GILDE)] = FakeZielgilde(kanal)
    settings.save(unsere, {"discord_recap_channel": "4242"})

    asyncio.run(gateway._nacht_zustellen(config, bot, unsere, ()))

    assert [teil.label for teil in kanal.ansichten[-1].items] == [
        "Joras",
        "Figur",
        "Ort",
        "Faden",
        "Nein",
    ]


def test_ohne_zustellkanal_bleiben_die_vorschlaege_der_nacht_liegen(stelle, bot, caplog):
    """»Keiner« ist eine gültige Wahl im Setup — dann wird nicht geraten, sondern geschwiegen."""
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    bot.gilden[int(GILDE)] = FakeZielgilde(FakeZielkanal(4242, "chronik"))

    with caplog.at_level(logging.WARNING):
        asyncio.run(gateway._nacht_zustellen(config, bot, unsere, ()))

    assert "Zustellkanal" in caplog.text


def test_der_nachtmelder_stellt_die_frage_in_die_ereignisschleife(stelle, bot):
    """Der Faden des Nachtlaufs schreibt nicht selbst nach Discord — er reicht durch."""
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    kanal = FakeZielkanal(4242, "chronik")
    bot.gilden[int(GILDE)] = FakeZielgilde(kanal)
    settings.save(unsere, {"discord_recap_channel": "4242"})

    async def ablauf():
        bot.loop = asyncio.get_running_loop()
        gateway._nachtmelder(config, bot)(unsere)
        for _ in range(100):
            await asyncio.sleep(0)
            if kanal.ansichten:
                return

    asyncio.run(ablauf())

    assert [teil.label for teil in kanal.ansichten[-1].items][0] == "Joras"


def test_der_bericht_der_nacht_steht_im_zustellkanal(stelle, bot):
    """#287: was die Nacht nicht geschrieben hat, stand nur in ``job.result``.

    Deren einziger Leser hat seit #231/#272 keinen Aufrufer mehr — das erste Signal der
    Gruppe wäre gewesen, dass die Chronik nie kam.
    """
    config, unsere = stelle
    kanal = FakeZielkanal(4242, "chronik")
    bot.gilden[int(GILDE)] = FakeZielgilde(kanal)
    settings.save(unsere, {"discord_recap_channel": "4242"})

    asyncio.run(gateway._nacht_zustellen(config, bot, unsere, (nightly.OHNE_SPRACHE,)))

    assert nightly.OHNE_SPRACHE in kanal.antworten[0]


def test_eine_nacht_ohne_verlust_sagt_nichts(stelle, bot):
    """Eine geschriebene Chronik steht ohnehin im Kanal — gesagt wird, was fehlt."""
    config, unsere = stelle
    kanal = FakeZielkanal(4242, "chronik")
    bot.gilden[int(GILDE)] = FakeZielgilde(kanal)
    settings.save(unsere, {"discord_recap_channel": "4242"})

    asyncio.run(gateway._nacht_zustellen(config, bot, unsere, ()))

    assert kanal.antworten == []


def test_die_frist_meldet_sich_im_zustellkanal(stelle, bot):
    """#286: ``recordings.taeglich`` warf den Rückgabewert von ``sweep_alle`` weg.

    Das ist der Weg, über den die Frist im Betrieb wirklich läuft — der Tag, an dem die
    Stimme einer echten Person gelöscht wird, war damit der stillste des Monats.
    """
    config, unsere = stelle
    kanal = FakeZielkanal(4242, "chronik")
    bot.gilden[int(GILDE)] = FakeZielgilde(kanal)
    settings.save(unsere, {"discord_recap_channel": "4242"})
    satz = recordings.OHNE_TEXT.format(sitzung=1, was="eine Aufnahme", tage=7)

    async def ablauf():
        bot.loop = asyncio.get_running_loop()
        gateway._fristmelder(config, bot)(unsere, (satz,))
        for _ in range(100):
            await asyncio.sleep(0)
            if kanal.antworten:
                return

    asyncio.run(ablauf())

    assert satz in kanal.antworten[0]


def test_ohne_laufende_schleife_bleibt_die_nachfrage_liegen(stelle, bot, caplog):
    """Vor dem ersten Verbinden gibt es keine Schleife — das ist kein Fehlschlag der Nacht."""
    config, unsere = stelle

    with caplog.at_level(logging.WARNING):
        gateway._nachtmelder(config, bot)(unsere)

    assert "Schleife" in caplog.text


def test_ein_knopf_bestaetigt_den_eintrag_in_der_gewaehlten_art(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)

    interaktion = klicken(ctx.ansichten[0], eintrag, register.ORT)

    (gruppe,) = register.overview(unsere)
    assert gruppe.kind == register.ORT
    assert gruppe.entries[0].name == "Joras"
    assert register.pending(unsere) == ()
    (bearbeitet,) = interaktion.response.bearbeitet
    assert erinnern.BESTAETIGT.format(name="Joras", art="Ort") in bearbeitet["content"]
    assert bearbeitet["view"] is None


def test_ein_nein_verwirft_den_vorschlag(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)

    interaktion = klicken(ctx.ansichten[0], eintrag, erinnern.VERWERFEN)

    assert register.pending(unsere) == ()
    assert register.overview(unsere) == ()
    assert erinnern.VERWORFEN.format(name="Joras") in interaktion.response.bearbeitet[0]["content"]


def test_derselbe_name_unter_zwei_arten_wird_nur_einmal_bestaetigt(stelle, bot):
    """#183: der zweite Klick meldete »steht jetzt im Register« und schrieb nichts.

    Das Modell schlägt einen Namen ohne Weiteres unter zwei Arten vor; beide als Ort zu
    bestätigen ist damit eine ganz normale Zwei-Klick-Folge. Der zweite Klick lief auf die
    eindeutige Spalte und wurde still verworfen — die Antwort behauptete trotzdem die
    Bestätigung, und die Liste darunter führte den Eintrag im selben Atemzug weiter als
    Vorschlag.
    """
    name = "Der Krumme Ast"
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    ort = eintrag_anlegen(
        unsere, sitzung, kind=register.ORT, name=name, satz="Die Schenke der Runde."
    )
    faden = eintrag_anlegen(
        unsere, sitzung, kind=register.FADEN, name=name, satz="Die Schenke der Runde."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)
    view = ctx.ansichten[0]

    zuerst = klicken(view, ort, register.ORT)
    danach = klicken(view, faden, register.ORT)

    bestaetigt = erinnern.BESTAETIGT.format(name=name, art="Ort")
    assert bestaetigt in zuerst.response.bearbeitet[0]["embed"].gebaut["description"]
    zweite = danach.response.bearbeitet[0]["content"]
    assert zweite.startswith(erinnern.SCHON_IM_REGISTER.format(name=name, art="Ort"))
    assert bestaetigt not in zweite
    # Und die Ansicht widerspricht der Antwort nicht mehr: der doppelte Vorschlag ist weg,
    # im Register steht der Name genau einmal.
    assert register.pending(unsere) == ()
    (gruppe,) = register.overview(unsere)
    assert (gruppe.kind, [e.name for e in gruppe.entries]) == (register.ORT, [name])


def test_ein_vergebener_name_wird_nicht_als_bestaetigt_gemeldet(stelle, bot):
    """Kollision mit einem noch offenen Vorschlag: nichts geschrieben, nichts behauptet."""
    name = "Der Krumme Ast"
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.ORT, name=name, satz="Die Schenke.")
    faden = eintrag_anlegen(unsere, sitzung, kind=register.FADEN, name=name, satz="Die Schenke.")
    ctx = FakeCtx()
    registerfrage(stelle, ctx)

    interaktion = klicken(ctx.ansichten[0], faden, register.ORT)

    beschreibung = interaktion.response.bearbeitet[0]["embed"].gebaut["description"]
    assert beschreibung.startswith(erinnern.NICHT_EINGETRAGEN.format(name=name, art="Ort"))
    assert erinnern.BESTAETIGT.format(name=name, art="Ort") not in beschreibung
    assert register.overview(unsere) == ()
    assert sorted(e.kind for e in register.pending(unsere)) == [register.FADEN, register.ORT]


def test_der_zweite_klick_auf_denselben_knopf_aendert_nichts(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)
    view = ctx.ansichten[0]
    klicken(view, eintrag, register.FIGUR)

    interaktion = klicken(view, eintrag, register.ORT)

    assert erinnern.SCHON_ENTSCHIEDEN in interaktion.response.bearbeitet[0]["content"]
    (gruppe,) = register.overview(unsere)
    assert gruppe.kind == register.FIGUR


def test_eine_alte_ansicht_entscheidet_nicht_neu(stelle, bot):
    """Die Liste von vorgestern klickt gegen den Stand von jetzt, nicht gegen ihren eigenen."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)
    alte = ctx.ansichten[0]
    register.decide(unsere, {eintrag: register.Entscheidung(ja=True, kind=register.FADEN)})

    interaktion = klicken(alte, eintrag, register.ORT)

    assert erinnern.SCHON_ENTSCHIEDEN in interaktion.response.bearbeitet[0]["content"]
    (gruppe,) = register.overview(unsere)
    assert gruppe.kind == register.FADEN


def test_eine_ansicht_ueberlebt_ihre_runde_nicht(stelle, bot):
    """Knopf und Menü leben eine Viertelstunde, und SQLite vergibt die Kennung einer
    gelöschten Runde wieder. Entschieden wird deshalb in der Runde, die *jetzt* zu dieser
    Gilde gehört — oder gar nicht."""
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)
    alte = ctx.ansichten[0]

    lebenszyklus.loeschen(config, unsere)
    frisch = runden.anlegen(config.database_path, "Frisch", guild_id=GILDE)
    assert frisch.id == unsere.id

    interaktion = klicken(alte, eintrag, register.ORT)

    assert interaktion.response.bearbeitet[0]["content"] == chronik.VERALTET
    assert register.overview(frisch) == ()


def test_mehr_vorschlaege_als_auf_eine_ansicht_passen(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    for nummer in range(erinnern.PRO_SEITE + 2):
        eintrag_anlegen(
            unsere, sitzung, kind=register.FIGUR, name=f"Joras {nummer}", satz="Ein Söldner."
        )
    ctx = FakeCtx()

    registerfrage(stelle, ctx)

    (embed,) = ctx.embeds
    assert embed.gebaut["footer"]["text"] == erinnern.OFFEN_WEITERE.format(anzahl=2)
    assert len(ctx.ansichten[0].items) == erinnern.PRO_SEITE * 5


def test_ein_stolpernder_knopf_antwortet_trotzdem(stelle, bot, monkeypatch):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    registerfrage(stelle, ctx)

    def stolpert(*args, **kwargs):
        raise RuntimeError("irgendwas in der Bibliothek")

    monkeypatch.setattr(erinnern, "entscheiden", stolpert)
    interaktion = klicken(ctx.ansichten[0], eintrag, register.FIGUR)

    (gesendet,) = interaktion.response.gesendet
    assert gesendet.startswith("Das hat nicht geklappt:")
    assert "RuntimeError" in gesendet


# -- Personen zuordnen -------------------------------------------------------------------


def test_ohne_aufnahme_gibt_es_niemanden_zuzuordnen(stelle, bot):
    ctx = FakeCtx()

    asyncio.run(befehl(bot, "zuordnung")(ctx))

    assert ctx.antworten == [erinnern.NIEMAND_AUFGENOMMEN]
    assert ctx.ansichten == [None]


def test_ohne_foundry_spieler_gibt_es_nichts_zu_waehlen(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    ctx = FakeCtx()

    asyncio.run(befehl(bot, "zuordnung")(ctx))

    assert ctx.antworten == [erinnern.KEINE_SPIELER]


def test_ein_menue_je_person_mit_den_spielern_der_runde(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), (BROK, "Brok"))
    welt_ablegen(unsere)
    ctx = FakeCtx()

    asyncio.run(befehl(bot, "zuordnung")(ctx))

    view = ctx.ansichten[0]
    assert len(view.items) == 2
    assert {teil.row for teil in view.items} == {0, 1}
    beschriftungen = [option.label for option in view.items[0].options]
    assert beschriftungen == [erinnern.ZUORDNUNG_KEINE, "Mira"]


def test_die_wahl_im_menue_schreibt_die_zuordnung_fest(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    interaktion = waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert people.speakers(unsere)[MIRA].confirmed.name == "Mira"
    beschreibung = interaktion.response.bearbeitet[0]["embed"].gebaut["description"]
    assert erinnern.ZUGEORDNET.format(name="Mira", spieler="Mira") in beschreibung


def test_niemand_nimmt_die_zuordnung_zurueck(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    people.confirm(unsere, {MIRA: "u-mira"})
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    interaktion = waehlen(ctx.ansichten[0], MIRA, erinnern.KEINE)

    assert people.speakers(unsere)[MIRA].confirmed is None
    beschreibung = interaktion.response.bearbeitet[0]["embed"].gebaut["description"]
    assert erinnern.ZUORDNUNG_GELOEST.format(name="Mira") in beschreibung


def test_im_zuordnungsmenue_laesst_sich_ein_fremdes_konto_uebernehmen(stelle, bot):
    """Das Menü bietet auch Vergebenes an — hier darf es das, denn hier wird korrigiert.

    Der Weg über die Ansicht und nicht nur über ``zuordnen``: träte das ``uebernehmen``
    hier nicht auf, stünde das Konto im Menü und würde beim Klick abgewiesen — genau der
    Zustand, den die Prüfung bemängelt hat.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira am Handy"), (BROK, "Mira"))
    welt_ablegen(unsere)
    people.confirm(unsere, {BROK: "u-mira"})
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    (menue,) = [teil for teil in ctx.ansichten[0].items if teil.custom_id.endswith(MIRA)]
    assert "u-mira" in [option.value for option in menue.options]

    interaktion = waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert people.speakers(unsere)[MIRA].confirmed.id == "u-mira"
    assert people.speakers(unsere)[BROK].confirmed is None
    beschreibung = interaktion.response.bearbeitet[0]["embed"].gebaut["description"]
    assert (
        erinnern.UEBERNOMMEN.format(name="Mira am Handy", spieler="Mira", vorher="Mira")
        in beschreibung
    )


def uebernahme_buehne(unsere, bot):
    """Die Lage der Übernahme: Broks Konto, ein Kanal zum Vermerken, ein erreichbares Postfach."""
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira am Handy"), (BROK, "Mira"))
    welt_ablegen(unsere)
    people.confirm(unsere, {BROK: "u-mira"})
    kanal = FakeTextkanal()
    bot.kanaele[int(SITZUNGSKANAL)] = kanal
    vorbesitzerin = FakeMitglied(int(BROK), "Mira")
    bot.nutzer[int(BROK)] = vorbesitzerin
    return kanal, vorbesitzerin


def test_eine_uebernahme_steht_im_sitzungskanal_und_bei_der_vorbesitzerin(stelle, bot):
    """Der Schritt mit der größten Folge war der stillste — jetzt hat er Tageslicht.

    Getragen wird die Sichtbarkeit **nicht** von der Ansicht: ``/chronicle zuordnung`` zeigt nur
    ``PRO_SEITE`` Personen, und ab der sechsten steht die Vorbesitzerin weder vorher noch
    nachher darin. Also der Kanal der Sitzung für die Runde und ein Wort an sie selbst.
    """
    _config, unsere = stelle
    kanal, vorbesitzerin = uebernahme_buehne(unsere, bot)
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert kanal.geschrieben == [
        erinnern.UEBERNAHME_VERMERK.format(name="Mira am Handy", spieler="Mira", vorher="Mira")
    ]
    assert [text for text, _ansicht in vorbesitzerin.zwiegespraech] == [
        erinnern.UEBERNAHME_ANGESAGT.format(runde=unsere.name, name="Mira am Handy", spieler="Mira")
    ]
    # In einer Direktnachricht steht sonst nirgends, welche Runde gemeint ist — eine
    # Instanz trägt mehrere, und `/chronicle zuordnung` zeigt in keine bestimmte Gilde.
    assert unsere.name in vorbesitzerin.zwiegespraech[0][0]


def test_ein_geschlossenes_postfach_verwirft_die_uebernahme_nicht(stelle, bot, caplog):
    """Der Sitzungskanal ist der belastbare Weg — eine zugemachte Direktnachricht hebt nichts auf.

    Und im Log steht der Grund, aber kein Name und keine Kennung.
    """
    _config, unsere = stelle
    kanal, vorbesitzerin = uebernahme_buehne(unsere, bot)
    vorbesitzerin.zwiegespraech_zu = True
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    with caplog.at_level(logging.WARNING):
        interaktion = waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert people.speakers(unsere)[MIRA].confirmed.id == "u-mira"
    assert people.speakers(unsere)[BROK].confirmed is None
    assert kanal.geschrieben == [
        erinnern.UEBERNAHME_VERMERK.format(name="Mira am Handy", spieler="Mira", vorher="Mira")
    ]
    # Und der Klick hat ordentlich geantwortet, statt »hat nicht geklappt« zu melden.
    assert interaktion.response.gesendet == []
    assert "RuntimeError" in caplog.text
    for geheim in ("Mira am Handy", BROK, MIRA):
        assert geheim not in caplog.text


def test_ohne_kanal_sagt_das_log_was_wirklich_geschah(stelle, bot, caplog):
    """Eine Sitzung, die nie in einem Discord-Kanal lief, hat keinen — dann trägt er nichts.

    »Der Vermerk im Kanal trägt sie« wäre hier eine Auskunft über etwas, das nicht
    geschehen ist, und genau die liest Wochen später jemand, der wissen will, ob die Runde
    es erfuhr.
    """
    _config, unsere = stelle
    sitzung = notes.create_session(unsere, played_on="2026-05-01")
    aufgenommen(unsere, sitzung, (MIRA, "Mira am Handy"), (BROK, "Mira"))
    welt_ablegen(unsere)
    people.confirm(unsere, {BROK: "u-mira"})
    bot.nutzer[int(BROK)] = vorbesitzerin = FakeMitglied(int(BROK), "Mira")
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    with caplog.at_level(logging.WARNING):
        waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert "in keinem Kanal" in caplog.text
    assert "Vermerk im Kanal trägt sie" not in caplog.text
    # Und die Vorbesitzerin erfährt es trotzdem — der eine Weg, der noch da ist.
    assert len(vorbesitzerin.zwiegespraech) == 1
    assert "Mira am Handy" not in caplog.text


def test_ohne_uebernahme_wird_nichts_gesagt(stelle, bot):
    """Gegenprobe: ein freies Konto nimmt niemandem etwas — also gibt es nichts zu sagen."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    kanal = FakeTextkanal()
    bot.kanaele[int(SITZUNGSKANAL)] = kanal
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert people.speakers(unsere)[MIRA].confirmed.id == "u-mira"
    assert kanal.geschrieben == []


def test_ein_verschwundener_spieler_aendert_nichts(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    waehlen(ctx.ansichten[0], MIRA, "u-fort")

    assert people.speakers(unsere)[MIRA].confirmed is None


def test_mehr_konten_als_in_ein_menue_passen(stelle, bot):
    """Ein Foundry-Server trägt alle Konten in jede Welt — das Menü nimmt nicht alle an."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere, weitere=erinnern.OPTIONEN_GRENZE)
    people.confirm(unsere, {MIRA: "u-mira"})
    ctx = FakeCtx()

    asyncio.run(befehl(bot, "zuordnung")(ctx))

    (menue,) = ctx.ansichten[0].items
    assert len(menue.options) == erinnern.OPTIONEN_GRENZE
    # Das Zugeordnete steht mit drin, obwohl sein Name hinter den gekappten stünde.
    gewaehlt = [option for option in menue.options if option.default]
    assert [option.label for option in gewaehlt] == ["Mira"]
    hinweis = erinnern.ZU_VIELE_SPIELER.format(anzahl=erinnern.OPTIONEN_GRENZE - 1)
    assert hinweis in ctx.embeds[0].gebaut["footer"]["text"]


def test_ein_altes_zuordnungsmenue_ordnet_nicht_in_die_frische_runde(stelle, bot):
    """Wer wen spielt, ist eine Aussage über Personen — sie darf nicht in einer fremden
    Gilde landen, bloß weil SQLite die Kennung wieder vergeben hat."""
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))
    alte = ctx.ansichten[0]

    lebenszyklus.loeschen(config, unsere)
    frisch = runden.anlegen(config.database_path, "Frisch", guild_id=GILDE)
    assert frisch.id == unsere.id

    interaktion = waehlen(alte, MIRA, "u-mira")

    assert interaktion.response.bearbeitet[0]["content"] == chronik.VERALTET
    assert people.overview(frisch).personen == ()


# -- Zuordnen beim Betreten des Sprachkanals ----------------------------------------------


def test_namensgleichheit_ordnet_beim_betreten_ohne_rueckfrage_zu(stelle):
    """Der eine Fall aus #76, der ohne Frage auskommt — und der gesagt wird.

    Entschieden ist er hier, geschrieben noch nicht: erst der Vermerk, dann ``zuordnen``.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)

    stand = erinnern.betreten(unsere, MIRA)

    assert stand.automatisch.id == "u-mira"
    assert stand.spieler == ()
    assert stand.vermerk == erinnern.BETRETEN_VERMERK.format(name="Mira", spieler="Mira")
    assert people.speakers(unsere)[MIRA].confirmed is None

    erinnern.zuordnen(unsere, MIRA, stand.automatisch.id)

    assert people.speakers(unsere)[MIRA].confirmed.name == "Mira"


def test_wer_wie_seine_figur_heisst_wird_beim_betreten_ebenso_zugeordnet(stelle):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (BROK, "Aelin Sturmwind"))
    welt_ablegen(unsere)

    assert erinnern.betreten(unsere, BROK).automatisch.id == "u-mira"


def test_wer_anders_heisst_wird_gefragt_statt_geraten(stelle):
    """Gegenprobe: ohne Namensgleichheit entsteht keine Zuordnung, sondern eine Frage."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (BROK, "Brok"))
    welt_ablegen(unsere)

    stand = erinnern.betreten(unsere, BROK)

    assert stand.automatisch is None
    assert [wer.name for wer in stand.spieler] == ["Mira"]
    assert people.speakers(unsere)[BROK].confirmed is None


@pytest.mark.parametrize("fast", ["Mirah", "Miral", "mira "])
def test_nur_1zu1_gleich_ordnet_zu_und_fast_gleich_wird_gefragt(stelle, fast):
    """Die Betreiber-Entscheidung vom 2026-08-12: exakte Übereinstimmung oder Menü.

    »Mirah« und »Miral« sind die Verwechselbaren, die eine Ähnlichkeitsschwelle von 0,8
    durchgelassen hätte. Der dritte Fall ist die Gegenprobe nach unten: ein Leerzeichen am
    Rand und eine große Anfangsminuskel sind kein anderer Name, und wer daran scheiterte,
    fragte, wo es nichts zu fragen gibt.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (BROK, fast))
    welt_ablegen(unsere)

    stand = erinnern.betreten(unsere, BROK)

    if fast.strip().casefold() == "mira":
        assert stand.automatisch.id == "u-mira"
    else:
        assert stand.automatisch is None
        assert [wer.name for wer in stand.spieler] == ["Mira"]


def test_zwei_gleichnamige_konten_sind_keine_eindeutige_gleichheit(stelle):
    """Heißen zwei Konten gleich, ist auch Gleichheit keine Antwort — dann wird gefragt."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere, zwilling=True)

    stand = erinnern.betreten(unsere, MIRA)

    assert stand.automatisch is None
    assert [wer.id for wer in stand.spieler] == ["u-mira", "u-zwilling"]


def test_ein_vergebener_zwilling_macht_die_gleichheit_nicht_eindeutig(stelle):
    """Geprüft wird gegen **alle** Konten der Runde, abgezogen wird erst danach.

    Andersherum verschwände die Mehrdeutigkeit genau dann, wenn eines der beiden
    gleichnamigen Konten schon vergeben ist: unter den freien bliebe eines übrig, und
    geschrieben würde ohne Rückfrage — obwohl »Mira« in dieser Runde zweimal vorkommt.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), ("d-anders", "Ganz anders"))
    welt_ablegen(unsere, zwilling=True)
    people.confirm(unsere, {"d-anders": "u-mira"})

    stand = erinnern.betreten(unsere, MIRA)

    assert stand.automatisch is None
    assert [wer.id for wer in stand.spieler] == ["u-zwilling"]
    assert people.speakers(unsere)[MIRA].confirmed is None


def test_ein_vergebenes_konto_und_eine_gleichnamige_figur_sind_zwei_treffer(stelle):
    """Derselbe Fehler ohne doppelten Kontonamen — der realistischere Weg hinein.

    »Mira« ist der Name eines vergebenen Kontos **und** der Figur eines freien. Wer erst
    filtert, sieht nur noch den zweiten Treffer und hält ihn für eindeutig.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), ("d-anders", "Ganz anders"))
    welt_ablegen(unsere, gastfigur=True)
    people.confirm(unsere, {"d-anders": "u-mira"})

    stand = erinnern.betreten(unsere, MIRA)

    assert stand.automatisch is None
    assert [wer.id for wer in stand.spieler] == ["u-gast"]
    assert people.speakers(unsere)[MIRA].confirmed is None


def test_der_vermerk_nennt_den_namen_der_wirklich_getroffen_hat(stelle):
    """Traf die Figur, steht die Figur im Satz — nicht der Kontoname.

    »**Aelin Sturmwind** heißt hier genauso wie **Mira**« wäre eine Behauptung über
    Namensgleichheit, die derselbe Satz sichtbar widerlegt.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (BROK, "Aelin Sturmwind"))
    welt_ablegen(unsere)

    stand = erinnern.betreten(unsere, BROK)

    assert stand.automatisch.id == "u-mira"
    assert stand.vermerk == erinnern.BETRETEN_VERMERK_FIGUR.format(
        name="Aelin Sturmwind", figur="Aelin Sturmwind", spieler="Mira"
    )
    assert "Aelin Sturmwind" in stand.vermerk


def test_der_vermerk_am_kontonamen_bleibt_der_einfache_satz(stelle):
    """Gegenprobe: traf der Kontoname, kommt keine Figur im Satz vor."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)

    stand = erinnern.betreten(unsere, MIRA)

    assert stand.vermerk == erinnern.BETRETEN_VERMERK.format(name="Mira", spieler="Mira")
    assert "Aelin Sturmwind" not in stand.vermerk


def test_der_satz_zum_menue_behauptet_nichts_ueber_die_namen(stelle):
    """Ins Menü führt auch die **Mehrdeutigkeit** — dort ist der Name gerade derselbe.

    Der Satz darf deshalb nur sagen, wie die Zuordnung zustande kam, und nichts darüber,
    ob Namen übereinstimmen: bei zwei gleichnamigen Menschen wäre das nachweislich falsch.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), (BROK, "Mira"))
    welt_ablegen(unsere)

    stand = erinnern.betreten(unsere, MIRA)

    assert stand.automatisch is None
    # Genau hier stimmen die Namen überein — und genau hier steht das Menü.
    assert people.genau(stand.person.discord_name, stand.spieler) is not None

    satz = erinnern.MENUE_VERMERK.format(name="Mira", spieler="Mira")
    assert "gewählt, nicht erkannt" in satz
    assert "überein" not in satz


def test_ein_nsc_name_macht_niemanden_zur_spielleitung(stelle):
    """Die Spielleitung besitzt in Foundry auch die Wirtin — deren Name ist kein Beleg.

    Ohne den Filter auf den Aktor-Typ machte ein Spitzname aus dem NSC-Fundus seinen
    Träger still zum Besitzer des Kontos, in jedem Protokoll dieser Kampagne.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (BROK, WIRTIN))
    welt_ablegen(unsere)

    stand = erinnern.betreten(unsere, BROK)

    assert stand.automatisch is None
    assert people.overview(unsere).spieler[0].characters == ("Aelin Sturmwind",)


def test_ein_gast_wird_nicht_auf_ein_vergebenes_konto_gesetzt(stelle):
    """Wer gerade hereinkommt, ist nicht unbedingt jemand, den wir kennen.

    Der Gast heißt hier sogar wie die Spielerin — und wird trotzdem nur gefragt: ihr Konto
    ist vergeben und steht deshalb gar nicht mehr zur Wahl.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), ("d-gast", "Mira"))
    welt_ablegen(unsere)
    people.confirm(unsere, {MIRA: "u-mira"})

    stand = erinnern.betreten(unsere, "d-gast")

    assert stand.automatisch is None
    assert stand.spieler == ()
    assert people.speakers(unsere)["d-gast"].confirmed is None
    assert people.speakers(unsere)[MIRA].confirmed.id == "u-mira"


def test_ein_gleichnamiger_gast_nimmt_der_spielerin_ihr_konto_nicht_vorweg(stelle):
    """Und derselbe Gast, bevor irgendjemand zugeordnet ist — der schärfere Fall.

    Hier ist »Mira« noch frei. Ohne die Gegenrichtung der Gleichheit bekäme sie, wer zuerst
    hereinkommt; danach stünde das Konto in keinem Menü mehr, und die echte Mira bliebe für
    immer unter ihrem Discord-Namen. Gefragt werden deshalb beide, und das Konto bleibt zu
    haben.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), ("d-gast", "Mira"))
    welt_ablegen(unsere)

    for kennung in ("d-gast", MIRA):
        stand = erinnern.betreten(unsere, kennung)
        assert stand.automatisch is None
        assert [wer.id for wer in stand.spieler] == ["u-mira"]

    assert people.speakers(unsere)["d-gast"].confirmed is None
    assert people.speakers(unsere)[MIRA].confirmed is None


def test_wer_wie_die_figur_der_anderen_heisst_macht_die_gleichheit_mehrdeutig(stelle):
    """Dieselbe Falle von zwei Seiten: die eine heißt wie das Konto, die andere wie die Figur."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), (BROK, "Aelin Sturmwind"))
    welt_ablegen(unsere)

    assert erinnern.betreten(unsere, MIRA).automatisch is None
    assert erinnern.betreten(unsere, BROK).automatisch is None


def test_das_menue_beim_betreten_traegt_nur_freie_konten(stelle):
    """Ein vergebenes Konto im Menü ist eine Einladung, sich eine Identität zu nehmen."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), (BROK, "Brok"))
    welt_ablegen(unsere, weitere=1)
    people.confirm(unsere, {MIRA: "u-mira"})

    stand = erinnern.betreten(unsere, BROK)

    assert [wer.id for wer in stand.spieler] == ["u-0"]
    assert [
        wert
        for _schrift, wert, _gewaehlt in erinnern.wahlmoeglichkeiten(stand.person, stand.spieler)
    ] == [
        erinnern.KEINE,
        "u-0",
    ]


def test_ein_vergebenes_konto_wird_nicht_ein_zweites_mal_vergeben(stelle):
    """Auch wenn es doch einmal in einer alten Ansicht steht: zwei Spuren, ein Name.

    Das ist der Weg **ohne** ``uebernehmen`` — das Menü im Zwiegespräch, wo niemand zusieht.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), (BROK, "Brok"))
    welt_ablegen(unsere)
    people.confirm(unsere, {MIRA: "u-mira"})

    ergebnis = erinnern.zuordnen(unsere, BROK, "u-mira")

    assert ergebnis == erinnern.Zugeordnet(satz=erinnern.SPIELER_VERGEBEN)
    assert people.speakers(unsere)[BROK].confirmed is None
    assert people.speakers(unsere)[MIRA].confirmed.id == "u-mira"


def test_in_der_zuordnung_laesst_sich_ein_vergebenes_konto_umhaengen(stelle):
    """Der Fall aus der Prüfung: Brok heißt »Mira« und bekam Miras Konto beim Betreten.

    Die echte Mira muss es zurückholen können, und zwar in **einem** Schritt — sonst wäre
    der Satz im Vermerk (»ändert `/chronicle zuordnung` es«) ein Verweis auf einen zugemauerten Weg.
    """
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira am Handy"), (BROK, "Mira"))
    welt_ablegen(unsere)
    people.confirm(unsere, {BROK: "u-mira"})

    ergebnis = erinnern.zuordnen(unsere, MIRA, "u-mira", uebernehmen=True)

    assert ergebnis.satz == erinnern.UEBERNOMMEN.format(
        name="Mira am Handy", spieler="Mira", vorher="Mira"
    )
    assert ergebnis.spieler.id == "u-mira"
    assert people.speakers(unsere)[MIRA].confirmed.id == "u-mira"
    # Und die Vorbesitzerin steht danach wieder offen da — nicht doppelt zugeordnet.
    assert people.speakers(unsere)[BROK].confirmed is None


def test_das_eigene_konto_noch_einmal_zu_waehlen_bleibt_erlaubt(stelle):
    """Gegenprobe: vergeben heißt an *jemand anderen* — sonst schlüge die eigene Ansicht fehl."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    people.confirm(unsere, {MIRA: "u-mira"})

    ergebnis = erinnern.zuordnen(unsere, MIRA, "u-mira")

    assert ergebnis.satz == erinnern.ZUGEORDNET.format(name="Mira", spieler="Mira")
    assert ergebnis.spieler.id == "u-mira"
    assert people.speakers(unsere)[MIRA].confirmed.id == "u-mira"


def test_wer_bestaetigt_hat_wird_beim_betreten_nicht_erneut_gefragt(stelle):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (BROK, "Brok"))
    welt_ablegen(unsere)
    people.confirm(unsere, {BROK: "u-mira"})

    assert erinnern.betreten(unsere, BROK) == erinnern.Betreten()


def test_ohne_foundry_spieler_wird_beim_betreten_nichts_gefragt(stelle):
    """Eine Frage mit leerem Menü ist keine — dann bleibt es beim Discord-Namen."""
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))

    assert erinnern.betreten(unsere, MIRA) == erinnern.Betreten()


def test_wen_wir_nicht_aufgenommen_haben_wird_nicht_zugeordnet(stelle):
    _config, unsere = stelle
    sitzung_mit_notiz(unsere)
    welt_ablegen(unsere)

    assert erinnern.betreten(unsere, "d-fremd") == erinnern.Betreten()


def test_die_zuordnung_der_fremden_runde_bleibt_unberuehrt(stelle, bot):
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    fremde = erste_runde(config)
    assert fremde.id != unsere.id
    ctx = FakeCtx()
    asyncio.run(befehl(bot, "zuordnung")(ctx))

    waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert people.overview(fremde).personen == ()
