"""Erinnern per Befehl — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Die Sätze, die dieser Suite ihren Sinn geben: **ein Treffer führt dorthin zurück, wo er
steht**, **ein Vorschlag wird nie von allein zur Tatsache** — und ein zweiter Klick auf
denselben Knopf bekommt eine ehrliche Auskunft statt eines Fehlers.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from conftest import runde as erste_runde
from test_bot import TOKEN, FakeBot, FakeIntents, FakePCMAudio, FakePermissions, FakeSenke
from test_chronik import FakeHTTPException, FakeInputText, FakeModal

from chronicle import consent, db, lebenszyklus, notes, people, register
from chronicle import runde as runden
from chronicle.bot import chronik, erinnern, gateway
from chronicle.compose import service as compose_service
from chronicle.config import Config
from chronicle.foundry import store as foundry_store
from chronicle.foundry.model import Character, Player, WorldSnapshot

GILDE = "1101"
FREMDE_GILDE = "9909"

THREAD = "5001"
NACHRICHT = "7001"

MIRA = "d-mira"
BROK = "d-brok"


# -- Die Attrappen ----------------------------------------------------------------------


class FakeEmbed:
    def __init__(self, gebaut):
        self.gebaut = gebaut

    @classmethod
    def from_dict(cls, gebaut):
        return cls(gebaut)


class FakeButton:
    def __init__(self, *, label="", row=0, custom_id="", disabled=False, **rest):
        self.label = label
        self.row = row
        self.custom_id = custom_id
        self.disabled = disabled
        self.callback = None


class FakeSelectOption:
    def __init__(self, *, label="", value="", default=False, **rest):
        self.label = label
        self.value = value
        self.default = default


class FakeSelect:
    def __init__(self, *, placeholder="", row=0, custom_id="", options=(), **rest):
        self.placeholder = placeholder
        self.row = row
        self.custom_id = custom_id
        self.options = list(options)
        self.values: list[str] = []
        self.callback = None


class FakeView:
    def __init__(self, *, timeout=None):
        self.timeout = timeout
        self.items: list = []

    def add_item(self, teil):
        self.items.append(teil)


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


class FakeAntwort:
    def __init__(self):
        self.bearbeitet: list[dict] = []
        self.gesendet: list[str] = []

    async def edit_message(self, *, content=None, embed=None, view=None):
        self.bearbeitet.append({"content": content, "embed": embed, "view": view})

    async def send_message(self, text, **rest):
        self.gesendet.append(text)


class FakeInteraction:
    def __init__(self, *, guild_id=GILDE):
        self.guild_id = guild_id
        self.response = FakeAntwort()


# -- Die Bühne --------------------------------------------------------------------------


@pytest.fixture
def pycord(monkeypatch):
    modul = types.ModuleType("discord")
    modul.Intents = FakeIntents
    modul.Bot = FakeBot
    modul.Permissions = FakePermissions
    modul.PCMAudio = FakePCMAudio
    modul.HTTPException = FakeHTTPException
    modul.Embed = FakeEmbed
    modul.SelectOption = FakeSelectOption
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
    runde, *, thread=THREAD, nachricht=NACHRICHT, text="Im Keller lag ein Schwert."
):
    sitzung = notes.create_session(runde, played_on="2026-05-01", thread_id=thread)
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


def welt_ablegen(runde, *, spielername="Mira", weitere=0):
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
                        Player(id=f"u-{nummer}", name=f"Konto {nummer:02d}", role=1, is_gm=False)
                        for nummer in range(weitere)
                    ),
                ),
                characters=(
                    Character(
                        id="a-aelin",
                        name="Aelin Sturmwind",
                        type="character",
                        owner_ids=("u-mira",),
                    ),
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
    return bot.befehle[name]


def registerbefehl(bot, name):
    return bot.gruppen[gateway.GRUPPE_REGISTER].befehle[name]


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
    assert {gateway.BEFEHL_SUCHE, gateway.BEFEHL_WER, gateway.BEFEHL_ZUORDNUNG} <= set(bot.befehle)
    assert set(bot.gruppen[gateway.GRUPPE_REGISTER].befehle) == {"offen"}


def test_die_hilfe_nennt_die_wege_zum_erinnern(bot):
    ctx = FakeCtx()

    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["hilfe"](ctx))

    (antwort,) = ctx.antworten
    for satzteil in ("/suche", "/wer", "/register offen", "/zuordnung"):
        assert satzteil in antwort


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
    assert f"https://discord.com/channels/{GILDE}/{THREAD}/{NACHRICHT}" in notizen
    assert "**Schwert**" in notizen
    chronik = next(f for f in embed.gebaut["fields"] if f["name"] == "Chronik")["value"]
    assert f"https://discord.com/channels/{GILDE}/{THREAD}" in chronik
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
    sitzung_mit_notiz(fremde, thread="6001", nachricht="8001", text="Nebenan lag ein Beil.")
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_SUCHE)(ctx, "Beil"))

    assert ctx.antworten[0] == erinnern.NICHTS_GEFUNDEN.format(begriff="Beil")


# -- Nachschlagen ------------------------------------------------------------------------


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
    assert f"https://discord.com/channels/{GILDE}/{THREAD}" in felder[erinnern.WER_ERWAEHNT]


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


def test_ohne_offene_vorschlaege_gibt_es_nichts_zu_entscheiden(stelle, bot):
    ctx = FakeCtx()

    asyncio.run(registerbefehl(bot, "offen")(ctx))

    assert ctx.antworten == [erinnern.NICHTS_OFFEN]
    assert ctx.ansichten == [None]


def test_je_vorschlag_eine_reihe_aus_name_arten_und_nein(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag_anlegen(unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner.")
    ctx = FakeCtx()

    asyncio.run(registerbefehl(bot, "offen")(ctx))

    (view,) = ctx.ansichten
    assert [teil.label for teil in view.items] == ["Joras", "Figur", "Ort", "Faden", "Nein"]
    assert view.items[0].disabled
    assert {teil.row for teil in view.items} == {0}


def test_ein_knopf_bestaetigt_den_eintrag_in_der_gewaehlten_art(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    asyncio.run(registerbefehl(bot, "offen")(ctx))

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
    asyncio.run(registerbefehl(bot, "offen")(ctx))

    interaktion = klicken(ctx.ansichten[0], eintrag, erinnern.VERWERFEN)

    assert register.pending(unsere) == ()
    assert register.overview(unsere) == ()
    assert erinnern.VERWORFEN.format(name="Joras") in interaktion.response.bearbeitet[0]["content"]


def test_der_zweite_klick_auf_denselben_knopf_aendert_nichts(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    eintrag = eintrag_anlegen(
        unsere, sitzung, kind=register.FIGUR, name="Joras", satz="Ein Söldner."
    )
    ctx = FakeCtx()
    asyncio.run(registerbefehl(bot, "offen")(ctx))
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
    asyncio.run(registerbefehl(bot, "offen")(ctx))
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
    asyncio.run(registerbefehl(bot, "offen")(ctx))
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

    asyncio.run(registerbefehl(bot, "offen")(ctx))

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
    asyncio.run(registerbefehl(bot, "offen")(ctx))

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

    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

    assert ctx.antworten == [erinnern.NIEMAND_AUFGENOMMEN]
    assert ctx.ansichten == [None]


def test_ohne_foundry_spieler_gibt_es_nichts_zu_waehlen(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

    assert ctx.antworten == [erinnern.KEINE_SPIELER]


def test_ein_menue_je_person_mit_den_spielern_der_runde(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"), (BROK, "Brok"))
    welt_ablegen(unsere)
    ctx = FakeCtx()

    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

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
    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

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
    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

    interaktion = waehlen(ctx.ansichten[0], MIRA, erinnern.KEINE)

    assert people.speakers(unsere)[MIRA].confirmed is None
    beschreibung = interaktion.response.bearbeitet[0]["embed"].gebaut["description"]
    assert erinnern.ZUORDNUNG_GELOEST.format(name="Mira") in beschreibung


def test_ein_verschwundener_spieler_aendert_nichts(stelle, bot):
    _config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    ctx = FakeCtx()
    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

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

    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

    (menue,) = ctx.ansichten[0].items
    assert len(menue.options) == erinnern.OPTIONEN_GRENZE
    # Das Zugeordnete steht mit drin, obwohl sein Name hinter den gekappten stünde.
    gewaehlt = [option for option in menue.options if option.default]
    assert [option.label for option in gewaehlt] == ["Mira"]
    hinweis = erinnern.ZU_VIELE_SPIELER.format(anzahl=erinnern.OPTIONEN_GRENZE - 1)
    assert hinweis in ctx.embeds[0].gebaut["footer"]["text"]


def test_die_zuordnung_der_fremden_runde_bleibt_unberuehrt(stelle, bot):
    config, unsere = stelle
    sitzung = sitzung_mit_notiz(unsere)
    aufgenommen(unsere, sitzung, (MIRA, "Mira"))
    welt_ablegen(unsere)
    fremde = erste_runde(config)
    assert fremde.id != unsere.id
    ctx = FakeCtx()
    asyncio.run(befehl(bot, gateway.BEFEHL_ZUORDNUNG)(ctx))

    waehlen(ctx.ansichten[0], MIRA, "u-mira")

    assert people.overview(fremde).personen == ()
