"""Der Thread ist die Sitzung — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Die Sätze, die dieser Suite ihren Sinn geben: **die Gilde bestimmt die Runde** — ohne eine
passiert nichts —, **die Szene entscheidet der Zeitpunkt der Nachricht** und nicht der des
Ablegens, und **es antwortet immer jemand**, auch wenn etwas schiefgeht.

Die Attrappen der Gateway-Seite stehen in ``test_bot``; hier kommen die dazu, die es für
Threads, Nachrichten und das Passwort-Fenster braucht.
"""

from __future__ import annotations

import asyncio
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import GRENZE, systemsprache
from conftest import runde as erste_runde
from test_bot import (
    TOKEN,
    FakeBot,
    FakeGilde,
    FakeIntents,
    FakeMitglied,
    FakePCMAudio,
    FakePermissions,
    FakeSenke,
    FakeSprachkanal,
    sprachdaten,
    spricht,
    stille,
)
from test_bot import FakeCtx as FakeSprechCtx

import chronicle.bot.__main__ as bot_eintritt
from chronicle import db, jobs, lebenszyklus, notes, recordings, settings, zugang
from chronicle import runde as runden
from chronicle.bot import ansage, chronik, einrichten, erinnern, gateway, recorder
from chronicle.config import Config
from chronicle.discord import ausgabe, rueckblick

GILDE = "1101"
FREMDE_GILDE = "9909"

# Zwei Mitglieder derselben Gilde: ``/chronik start`` steht jedem offen, und seit #96
# entscheidet die Kennung, wessen Eingabe der Abschluss ungefragt weiterreicht.
WER = 7001
ZWEITES_MITGLIED = 7002

PASSWORT = "passwort-nur-fuer-den-test"
ANDERE_EINGABE = "was-das-zweite-mitglied-tippt"

# Der Satz, den die Attrappe des Laufs zurückgibt — hier steht kein echtes Ollama dahinter.
STEHT = "Chronik und Rückblick stehen bereit."


# -- Die Attrappen ----------------------------------------------------------------------


class FakeHTTPException(Exception):
    """Was py-cord wirft, wenn Discord den Thread verweigert."""


class FakeInputText:
    def __init__(self, *, label="", placeholder="", **rest):
        self.label = label
        self.placeholder = placeholder
        self.value = ""


class FakeModal:
    def __init__(self, *children, title="", **rest):
        self.children = list(children)
        self.title = title


class FakeAnhang:
    def __init__(self, filename, inhalt=b"ton-ton-ton", groesse=None):
        self.filename = filename
        self.size = len(inhalt) if groesse is None else groesse
        self._inhalt = inhalt

    async def save(self, ziel):
        Path(ziel).write_bytes(self._inhalt)


class FakeThread:
    def __init__(self, kennung, name):
        self.id = kennung
        self.name = name
        self.mention = f"<#{kennung}>"
        self.gesendet: list[str] = []

    async def send(self, text):
        self.gesendet.append(text)


class FakeTextkanal:
    def __init__(self, kennung=900, *, darf=True):
        self.id = kennung
        self.darf = darf
        self.threads: list[FakeThread] = []

    async def create_thread(self, *, name):
        if not self.darf:
            raise FakeHTTPException("Missing Permissions")
        thread = FakeThread(5000 + len(self.threads), name)
        self.threads.append(thread)
        return thread


class FakeCtx:
    def __init__(self, *, guild_id=GILDE, kanal=None, wer=WER):
        self.guild_id = guild_id
        self.channel = kanal if kanal is not None else FakeTextkanal()
        self.channel_id = self.channel.id
        self.user = types.SimpleNamespace(id=wer)
        self.antworten: list[str] = []
        self.modale: list[FakeModal] = []
        self.aufgeschoben = False

    async def defer(self, **rest):
        self.aufgeschoben = True

    async def respond(self, text, **rest):
        self.antworten.append(text)

    async def send_modal(self, modal):
        self.modale.append(modal)


class FakeAntwort:
    """Die *erste* Antwort auf eine Interaktion — oder der Aufschub, der sie vertagt."""

    def __init__(self):
        self.gesendet: list[str] = []
        self.aufgeschoben = False

    async def send_message(self, text, **rest):
        self.gesendet.append(text)

    async def defer(self, **rest):
        self.aufgeschoben = True


class FakeNachreichen:
    """Was nach einem Aufschub kommt — getrennt geführt, damit der Weg prüfbar bleibt."""

    def __init__(self):
        self.gesendet: list[str] = []

    async def send(self, text, **rest):
        self.gesendet.append(text)


class FakeInteraction:
    def __init__(self, kanal, *, guild_id=GILDE, wer=WER):
        self.channel = kanal
        self.guild_id = guild_id
        self.user = types.SimpleNamespace(id=wer)
        self.response = FakeAntwort()
        self.followup = FakeNachreichen()

    @property
    def antworten(self) -> list[str]:
        return self.response.gesendet + self.followup.gesendet


class FakeNachricht:
    def __init__(self, kennung, text="", *, kanal, gilde=GILDE, anhaenge=(), zeit=None, bot=False):
        self.id = kennung
        self.content = text
        self.attachments = list(anhaenge)
        self.created_at = zeit if zeit is not None else datetime.now(UTC)
        self.author = types.SimpleNamespace(id=4001, bot=bot)
        self.guild = types.SimpleNamespace(id=gilde)
        self.channel = types.SimpleNamespace(id=kanal)
        self.antworten: list[str] = []

    async def reply(self, text):
        self.antworten.append(text)


def rohes_ereignis(message_id, *, gilde=GILDE, inhalt=None):
    daten = {} if inhalt is None else {"content": inhalt}
    return types.SimpleNamespace(message_id=message_id, guild_id=gilde, data=daten)


# -- Die Bühne --------------------------------------------------------------------------


@pytest.fixture
def pycord(monkeypatch):
    import sys

    modul = types.ModuleType("discord")
    modul.Intents = FakeIntents
    modul.Bot = FakeBot
    modul.Permissions = FakePermissions
    modul.PCMAudio = FakePCMAudio
    modul.HTTPException = FakeHTTPException
    senken = types.ModuleType("discord.sinks")
    senken.Sink = FakeSenke
    modul.sinks = senken
    werkzeug = types.ModuleType("discord.utils")
    werkzeug.get_missing_voice_dependencies = lambda: ()
    modul.utils = werkzeug
    oberflaeche = types.ModuleType("discord.ui")
    oberflaeche.Modal = FakeModal
    oberflaeche.InputText = FakeInputText
    modul.ui = oberflaeche
    monkeypatch.setitem(sys.modules, "discord", modul)
    monkeypatch.setattr(FakeBot, "erzeugt", [])
    return modul


@pytest.fixture
def stelle(tmp_path):
    config = Config(
        discord_bot_token=TOKEN,
        # Seit #96 fragt der Start nur, wo ein Foundry-Server im Spiel ist — eine Runde
        # ohne Adresse bekäme gar kein Fenster mehr.
        foundry_url="https://foundry.example",
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "aufnahmen",
    )
    db.init(config.database_path)
    return config, runden.anlegen(config.database_path, "Der Krumme Ast", guild_id=GILDE)


@pytest.fixture
def ohne_espeak(monkeypatch):
    """Der echte Weg durch ``datei``, nur mit einer Attrappe statt des Systempakets."""
    echt = ansage.datei
    monkeypatch.setattr(
        ansage, "datei", lambda ordner, **rest: echt(ordner, sprecher=spricht, **rest)
    )


@pytest.fixture
def bot(stelle, pycord):
    config, _unsere = stelle
    return gateway.baue(config)


def chronikbefehl(bot, name):
    return bot.gruppen[gateway.GRUPPE_CHRONIK].befehle[name]


def sitzung_starten(bot, ctx=None, titel="", *, passwort="", wer=WER):
    """``/chronik start`` samt dem Fenster, das seit #96 davor steht.

    Der Rumpf ist mit #96 aus dem Befehl in den Rückruf des Fensters gewandert und bekommt
    dort eine **Interaktion**, keinen Befehlskontext. Genau diesen Unterschied bildet der
    Helfer nach: der Befehl sieht ``ctx``, das Fenster eine eigene ``FakeInteraction``.
    Zurück kommt sie, denn dort steht, was der Benutzer am Ende zu lesen bekommt.
    """
    ctx = ctx if ctx is not None else FakeCtx(wer=wer)
    fenster_interaktion = FakeInteraction(ctx.channel, guild_id=ctx.guild_id, wer=wer)

    async def ablauf():
        await chronikbefehl(bot, "start")(ctx, titel)
        for fenster in ctx.modale:
            fenster.children[0].value = passwort
            await fenster.callback(fenster_interaktion)

    asyncio.run(ablauf())
    thread = ctx.channel.threads[-1] if ctx.channel.threads else None
    return fenster_interaktion, thread


def mitschnitt_starten(bot, gilde=GILDE):
    """``/aufnahme start`` in einem Sprachkanal dieser Gilde, mit einer Spur darin."""
    wer = FakeMitglied(4001, "Mira")
    kanal = FakeSprachkanal(FakeGilde(gilde), wer)
    wer.voice = types.SimpleNamespace(channel=kanal)
    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["start"](FakeSprechCtx(wer, guild_id=gilde)))
    kanal.verbindung.senke.write(sprachdaten(stille(480)), wer)
    return types.SimpleNamespace(kanal=kanal, wer=wer)


def notizen(runde, sitzung_id):
    """Alle Notizen der Sitzung, Szene für Szene."""
    return [
        (szene.position, notiz.text)
        for szene in notes.session(runde, sitzung_id).scenes
        for notiz in szene.notes
    ]


# -- Sitzung und Thread -----------------------------------------------------------------


def test_der_bot_bringt_die_chronik_befehle_mit(bot):
    assert set(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle) == {"start", "fertig", "loeschen"}
    assert gateway.BEFEHL_SZENE in bot.befehle
    assert set(bot.ereignisse) >= {"on_message", "on_raw_message_edit", "on_raw_message_delete"}


def test_ohne_nachrichten_absicht_bliebe_der_thread_ein_leerer_behaelter(bot):
    (gebaut,) = FakeBot.erzeugt
    assert gebaut.intents.messages and gebaut.intents.message_content


def test_start_legt_sitzung_und_thread_an_und_sagt_wie_es_weitergeht(stelle, bot):
    _config, unsere = stelle

    fenster, thread = sitzung_starten(bot, titel="Der Keller")

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert sitzung_id is not None
    assert notes.session(unsere, sitzung_id).title == "Der Keller"
    assert thread.name == "Der Keller"
    assert thread.gesendet == [chronik.ANGELEGT]
    (antwort,) = fenster.antworten
    assert chronik.THREAD_STEHT.format(thread=thread.mention) in antwort


def test_ohne_titel_traegt_der_thread_das_datum(stelle, bot):
    _ctx, thread = sitzung_starten(bot)
    assert notes.today() in thread.name


def test_ohne_runde_fuer_diesen_server_entsteht_nichts(stelle, bot):
    _config, _unsere = stelle
    ctx = FakeCtx(guild_id=FREMDE_GILDE)

    asyncio.run(chronikbefehl(bot, "start")(ctx, ""))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_RUNDE in antwort
    assert ctx.channel.threads == []
    assert ctx.modale == []


def test_ohne_thread_recht_bleibt_keine_halbe_sitzung_liegen(stelle, bot):
    _config, unsere = stelle
    ctx = FakeCtx(kanal=FakeTextkanal(darf=False))

    fenster, _thread = sitzung_starten(bot, ctx, passwort=PASSWORT)

    (antwort,) = fenster.antworten
    assert chronik.KEIN_THREAD in antwort
    assert notes.sessions(unsere) == ()
    # Kein Thread, keine Sitzung — und erst recht kein Passwort, das bis zur Frist läge.
    assert not zugang.ist_gemerkt(unsere)


# -- Das Passwort beim Sitzungsstart ----------------------------------------------------


def test_start_fragt_das_passwort_im_fenster_und_haelt_es_die_sitzung_ueber(stelle, bot):
    _config, unsere = stelle
    ctx = FakeCtx()

    fenster, thread = sitzung_starten(bot, ctx, passwort=PASSWORT)

    assert ctx.modale[0].title == chronik.START_TITEL
    assert zugang.passwort(unsere) == PASSWORT
    assert chronik.MIT_FOUNDRY in fenster.antworten[0]
    # Gezeigt wird *ob*, nie *was* — auch nicht im Thread, den die ganze Runde liest.
    assert PASSWORT not in " ".join(fenster.antworten + thread.gesendet)


def test_ohne_passwort_laeuft_die_sitzung_trotzdem(stelle, bot):
    _config, unsere = stelle

    fenster, thread = sitzung_starten(bot)

    assert notes.session_of_thread(unsere, str(thread.id)) is not None
    assert not zugang.ist_gemerkt(unsere)
    assert chronik.OHNE_FOUNDRY in fenster.antworten[0]


def test_ein_leeres_feld_wirft_ein_liegendes_passwort_nicht_weg(stelle, bot):
    """Leer heißt überspringen, nicht vergessen — sonst nähme ein zweiter Start der
    laufenden Sitzung ihren Zugang zu Foundry."""
    _config, unsere = stelle
    sitzung_starten(bot, passwort=PASSWORT)

    sitzung_starten(bot)

    assert zugang.passwort(unsere) == PASSWORT


def test_ein_altes_startfenster_merkt_das_passwort_keiner_fremden_runde(stelle, bot):
    """Wie am Fenster des Abschlusses: die Kennung wird nach einer Löschung neu vergeben,
    und ein Fenster von vorhin darf nicht in die frische Runde hineinschreiben."""
    config, unsere = stelle
    ctx = FakeCtx()
    fenster = FakeInteraction(ctx.channel)

    async def ablauf():
        await chronikbefehl(bot, "start")(ctx, "")
        lebenszyklus.loeschen(config, unsere)
        frisch = runden.anlegen(config.database_path, "Frisch", guild_id=GILDE)
        ctx.modale[0].children[0].value = PASSWORT
        await ctx.modale[0].callback(fenster)
        return frisch

    frisch = asyncio.run(ablauf())

    assert fenster.antworten == [chronik.VERALTET]
    assert ctx.channel.threads == []
    assert notes.sessions(frisch) == ()
    assert not zugang.ist_gemerkt(frisch)


def test_das_startfenster_schiebt_auf_bevor_es_am_thread_arbeitet(stelle, bot):
    """Zwei REST-Runden passen nicht verlässlich in die drei Sekunden der ersten Antwort.

    Dass die Antwort **nachgereicht** kommt, ist der Beleg: der Aufschub stand davor.
    """
    _config, _unsere = stelle

    fenster, _thread = sitzung_starten(bot, passwort=PASSWORT)

    assert fenster.response.aufgeschoben
    assert fenster.response.gesendet == []
    assert len(fenster.followup.gesendet) == 1


def test_ein_absturz_im_startfenster_laesst_niemanden_im_dunkeln(stelle, bot, monkeypatch):
    """Ohne den breiten Fang entkäme die Ausnahme in py-cords ``Modal.on_error``, das die
    Interaktion nie beantwortet — der Thread stünde, die Sitzung nicht, und niemand
    erführe es."""
    _config, unsere = stelle

    def stolpert(*rest, **auch):
        raise RuntimeError("die SQLite war weg")

    monkeypatch.setattr(chronik, "sitzung_anlegen", stolpert)

    fenster, _thread = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.antworten
    assert gateway.UNERWARTET.format(typ="RuntimeError") in antwort
    assert notes.sessions(unsere) == ()
    # Der Absturz kam vor dem Merken: nichts liegt bis zur Frist herum.
    assert not zugang.ist_gemerkt(unsere)


def test_eine_nicht_zugestellte_begruessung_sagt_dass_die_sitzung_trotzdem_steht(
    stelle, bot, monkeypatch
):
    """Thread und Sitzung stehen schon — »noch einmal versuchen« legte beides doppelt an."""
    _config, unsere = stelle

    async def stumm(self, text):
        raise RuntimeError("Discord hat die Nachricht verweigert")

    monkeypatch.setattr(FakeThread, "send", stumm)

    fenster, thread = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.antworten
    assert chronik.STUMM_ANGELEGT.format(thread=thread.mention) in antwort
    assert notes.session_of_thread(unsere, str(thread.id)) is not None
    assert zugang.ist_gemerkt(unsere)


# -- Wo kein Foundry ist, wird auch nicht danach gefragt ---------------------------------


def test_auf_der_testwelt_kommt_kein_passwortfenster(stelle, bot):
    config, unsere = stelle
    settings.save_foundry_quelle(unsere, settings.TESTWELT)
    ctx = FakeCtx()

    asyncio.run(chronikbefehl(bot, "start")(ctx, ""))

    thread = ctx.channel.threads[-1]
    assert ctx.modale == []
    assert notes.session_of_thread(unsere, str(thread.id)) is not None
    assert chronik.KEIN_FOUNDRY in ctx.antworten[0]
    assert not chronik.foundry_im_spiel(config, unsere)


def test_ohne_eingetragene_adresse_kommt_kein_passwortfenster(tmp_path, pycord):
    """Eine Runde, die nie durch ``/setup`` ging, hat kein Foundry — und wird nicht gefragt."""
    config = Config(
        discord_bot_token=TOKEN,
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "aufnahmen",
    )
    db.init(config.database_path)
    unsere = runden.anlegen(config.database_path, "Ohne Foundry", guild_id=GILDE)
    ctx = FakeCtx()

    asyncio.run(chronikbefehl(gateway.baue(config), "start")(ctx, ""))

    assert ctx.modale == []
    assert chronik.KEIN_FOUNDRY in ctx.antworten[0]
    assert not zugang.ist_gemerkt(unsere)


# -- Jede Nachricht ist eine Notiz ------------------------------------------------------


def melden(bot, nachricht):
    asyncio.run(bot.ereignisse["on_message"](nachricht))
    return nachricht


def test_eine_nachricht_im_thread_wird_zur_notiz_und_bekommt_keine_quittung(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    nachricht = melden(bot, FakeNachricht(7001, "Wir steigen hinab.", kanal=thread.id))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == [(1, "Wir steigen hinab.")]
    assert nachricht.antworten == []


def test_was_der_bot_selbst_schreibt_ist_keine_notiz(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    melden(bot, FakeNachricht(7002, chronik.ANGELEGT, kanal=thread.id, bot=True))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == []


def test_ausserhalb_eines_sitzungs_threads_wird_nichts_abgelegt(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    nachricht = melden(bot, FakeNachricht(7003, "Nur geplaudert.", kanal=8888))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == []
    assert nachricht.antworten == []


def test_aus_einem_server_ohne_runde_wird_nichts_abgelegt(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    melden(bot, FakeNachricht(7004, "Von nebenan.", kanal=thread.id, gilde=FREMDE_GILDE))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == []


def test_was_nicht_abgelegt_werden_konnte_bekommt_trotzdem_eine_antwort(stelle, bot, monkeypatch):
    _ctx, thread = sitzung_starten(bot)

    def stolpert(*args, **kwargs):
        raise RuntimeError("irgendwas in der Datenschicht")

    monkeypatch.setattr(notes, "add_note", stolpert)

    nachricht = melden(bot, FakeNachricht(7005, "Wir steigen hinab.", kanal=thread.id))

    (antwort,) = nachricht.antworten
    assert antwort.startswith("Das konnte ich nicht ablegen:")
    assert "RuntimeError" in antwort


# -- Szenen -----------------------------------------------------------------------------


def test_szene_zieht_die_trennlinie_und_das_naechste_landet_dahinter(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    melden(bot, FakeNachricht(7101, "Noch im Wirtshaus.", kanal=thread.id))

    szene_ctx = FakeCtx(kanal=types.SimpleNamespace(id=thread.id))
    asyncio.run(bot.befehle[gateway.BEFEHL_SZENE](szene_ctx, "Im Keller"))
    spaeter = datetime.now(UTC) + timedelta(seconds=5)
    melden(bot, FakeNachricht(7102, "Die Treppe knarrt.", kanal=thread.id, zeit=spaeter))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert szene_ctx.antworten == [chronik.SZENE.format(name="Im Keller")]
    assert notizen(unsere, sitzung_id) == [(1, "Noch im Wirtshaus."), (2, "Die Treppe knarrt.")]
    assert [s.title for s in notes.session(unsere, sitzung_id).scenes] == [None, "Im Keller"]


def test_eine_szene_ohne_namen_ist_trotzdem_eine_trennlinie(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=thread.id))

    asyncio.run(bot.befehle[gateway.BEFEHL_SZENE](ctx, "  "))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert ctx.antworten == [chronik.SZENE_OHNE_NAMEN]
    assert len(notes.session(unsere, sitzung_id).scenes) == 2


def test_szene_ausserhalb_eines_sitzungs_threads_sagt_es(stelle, bot):
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=8888))

    asyncio.run(bot.befehle[gateway.BEFEHL_SZENE](ctx, "Im Keller"))

    (antwort,) = ctx.antworten
    assert chronik.NUR_IM_THREAD in antwort


def test_eine_tage_spaeter_nachgetragene_nachricht_landet_in_ihrer_szene(stelle, bot):
    """Die Szene entscheidet der Zeitpunkt der Nachricht, nicht der des Ablegens."""
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    notes.add_scene(unsere, sitzung_id, title="Im Keller", at="2026-08-05T21:00:00+00:00")
    notes.add_scene(unsere, sitzung_id, title="Auf dem Dach", at="2026-08-05T23:00:00+00:00")

    melden(
        bot,
        FakeNachricht(
            7201,
            "Da unten stand die Truhe.",
            kanal=thread.id,
            zeit=datetime(2026, 8, 5, 22, 0, tzinfo=UTC),
        ),
    )

    assert notizen(unsere, sitzung_id) == [(2, "Da unten stand die Truhe.")]


def test_was_vor_jeder_trennlinie_liegt_bleibt_in_der_ersten_szene(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    notes.add_scene(unsere, sitzung_id, title="Im Keller", at="2026-08-05T21:00:00+00:00")

    melden(
        bot,
        FakeNachricht(
            7202, "Ganz am Anfang.", kanal=thread.id, zeit=datetime(2020, 1, 1, tzinfo=UTC)
        ),
    )

    assert notizen(unsere, sitzung_id) == [(1, "Ganz am Anfang.")]


# -- Ändern und Löschen spiegeln --------------------------------------------------------


def test_eine_geaenderte_nachricht_aendert_ihre_notiz(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    melden(bot, FakeNachricht(7301, "Die Wirtin heißt Mara.", kanal=thread.id))

    asyncio.run(
        bot.ereignisse["on_raw_message_edit"](rohes_ereignis(7301, inhalt="Die Wirtin heißt Mira."))
    )

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == [(1, "Die Wirtin heißt Mira.")]


def test_eine_aenderung_ohne_neuen_text_laesst_die_notiz_stehen(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    melden(bot, FakeNachricht(7302, "Die Wirtin heißt Mira.", kanal=thread.id))

    asyncio.run(bot.ereignisse["on_raw_message_edit"](rohes_ereignis(7302)))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == [(1, "Die Wirtin heißt Mira.")]


def test_eine_geloeschte_nachricht_verschwindet_auch_bei_uns(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    melden(bot, FakeNachricht(7303, "Das war falsch.", kanal=thread.id))

    asyncio.run(bot.ereignisse["on_raw_message_delete"](rohes_ereignis(7303)))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == []


def test_ein_ereignis_ohne_gilde_fasst_nichts_an(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    melden(bot, FakeNachricht(7304, "Bleibt stehen.", kanal=thread.id))

    asyncio.run(
        bot.ereignisse["on_raw_message_edit"](rohes_ereignis(7304, gilde=None, inhalt="weg"))
    )
    asyncio.run(bot.ereignisse["on_raw_message_delete"](rohes_ereignis(7304, gilde=None)))

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert notizen(unsere, sitzung_id) == [(1, "Bleibt stehen.")]


# -- Diktat im Thread -------------------------------------------------------------------


def test_eine_sprachnachricht_im_thread_geht_in_dieselbe_warteschlange(stelle, bot):
    config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    nachricht = melden(
        bot,
        FakeNachricht(7401, "", kanal=thread.id, anhaenge=(FakeAnhang("voice-message.ogg"),)),
    )

    (spur,) = recordings.pending(unsere)
    assert spur.session_id == notes.session_of_thread(unsere, str(thread.id))
    assert spur.discord_user_id == "4001"
    assert (config.recordings_dir / spur.filename).is_file()
    assert nachricht.antworten == [chronik.DIKTAT]


def test_ein_anhang_ohne_ton_ist_einfach_kein_diktat(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    nachricht = melden(
        bot,
        FakeNachricht(
            7402, "Der Notizzettel.", kanal=thread.id, anhaenge=(FakeAnhang("notizen.pdf"),)
        ),
    )

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert recordings.pending(unsere) == ()
    assert notizen(unsere, sitzung_id) == [(1, "Der Notizzettel.")]
    assert nachricht.antworten == []


def test_eine_zu_grosse_aufnahme_bleibt_liegen_und_sagt_es(stelle, bot):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    riesig = FakeAnhang("lang.m4a", groesse=recordings.MAX_BYTES + 1)

    nachricht = melden(bot, FakeNachricht(7403, "", kanal=thread.id, anhaenge=(riesig,)))

    assert recordings.pending(unsere) == ()
    assert nachricht.antworten == [
        chronik.ZU_GROSS.format(name="lang.m4a", grenze=recordings.MAX_BYTES // (1024 * 1024))
    ]


# -- Abschließen ------------------------------------------------------------------------


async def _bis_der_lauf_durch_ist(unsere):
    ende = time.monotonic() + GRENZE
    while time.monotonic() < ende and jobs.running(unsere, jobs.CHRONIK):
        await asyncio.sleep(0.01)
    # Der Lauf meldet sich aus seinem eigenen Faden; die Schleife muss ihn noch abholen.
    await asyncio.sleep(0.05)


def abschluss_fahren(bot, unsere, thread, *, passwort=PASSWORT):
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=thread.id))
    interaktion = FakeInteraction(thread)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        if ctx.modale:
            ctx.modale[0].children[0].value = passwort
            await ctx.modale[0].callback(interaktion)
            await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())
    return ctx, interaktion


def test_fertig_fragt_nach_dem_passwort_und_stoesst_den_einen_lauf_an(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    gesehen = []

    def abschluss(config, eine, session_id):
        gesehen.append((zugang.passwort(eine), session_id))
        return STEHT

    monkeypatch.setattr(jobs, "abschluss", abschluss)

    ctx, interaktion = abschluss_fahren(bot, unsere, thread)

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert ctx.modale[0].title == chronik.PASSWORT_TITEL
    assert gesehen == [(PASSWORT, sitzung_id)]
    assert interaktion.response.gesendet == [chronik.FERTIG]
    assert thread.gesendet == [chronik.ANGELEGT, STEHT]
    assert jobs.latest(unsere, jobs.CHRONIK, sitzung_id).result == STEHT


def test_nach_dem_passwort_beim_start_fragt_der_abschluss_nicht_noch_einmal(
    stelle, bot, monkeypatch
):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot, passwort=PASSWORT)
    gesehen = []

    def abschluss(config, eine, session_id):
        gesehen.append(zugang.passwort(eine))
        return STEHT

    monkeypatch.setattr(jobs, "abschluss", abschluss)
    ctx = FakeCtx(kanal=thread)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert ctx.modale == []
    assert gesehen == [PASSWORT]
    assert ctx.antworten == [chronik.FERTIG]


def test_ein_zweites_mitglied_schiebt_dem_abschluss_kein_fremdes_passwort_unter(
    stelle, bot, monkeypatch
):
    """``/chronik start`` steht jedem Mitglied offen. Ohne diese Prüfung nähme der
    Abschluss die Zeichenkette eines Zweiten und zeigte sie dem Foundry-Konto dieser Runde
    vor, ausgelöst von jemandem, der sie nie gesehen hat."""
    _config, unsere = stelle
    _fenster, thread = sitzung_starten(bot, passwort=PASSWORT, wer=WER)
    sitzung_starten(
        bot, FakeCtx(wer=ZWEITES_MITGLIED), passwort=ANDERE_EINGABE, wer=ZWEITES_MITGLIED
    )
    gesehen = []
    monkeypatch.setattr(
        jobs, "abschluss", lambda config, eine, sid: gesehen.append(zugang.passwort(eine)) or STEHT
    )
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=thread.id), wer=WER)
    interaktion = FakeInteraction(thread, wer=WER)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        ctx.modale[0].children[0].value = PASSWORT
        await ctx.modale[0].callback(interaktion)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    # Kein stiller Schnellweg: das Fenster kommt, und es sagt auch, warum.
    assert ctx.modale[0].children[0].placeholder == chronik.FREMDES_HINWEIS
    assert ctx.antworten == []
    assert gesehen == [PASSWORT]


def test_wer_selbst_hinterlegt_hat_wird_nicht_noch_einmal_gefragt(stelle, bot):
    _config, unsere = stelle
    sitzung_starten(bot, passwort=PASSWORT, wer=WER)

    assert chronik.passwort_bereit(unsere, str(WER))
    assert not chronik.passwort_bereit(unsere, str(ZWEITES_MITGLIED))
    # Und ohne Kennung erst recht nicht — sonst wäre der Schnellweg wieder für alle offen.
    assert not chronik.passwort_bereit(unsere, "")


def test_ohne_foundry_fragt_auch_der_abschluss_nicht_nach_dem_passwort(stelle, bot, monkeypatch):
    _config, unsere = stelle
    settings.save_foundry_quelle(unsere, settings.TESTWELT)
    ctx = FakeCtx()
    asyncio.run(chronikbefehl(bot, "start")(ctx, ""))
    thread = ctx.channel.threads[-1]
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid: STEHT)
    abschluss_ctx = FakeCtx(kanal=thread)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(abschluss_ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert abschluss_ctx.modale == []
    assert abschluss_ctx.antworten == [chronik.FERTIG]


def test_auch_mit_gemerktem_passwort_endet_die_aufnahme_vor_dem_lauf(
    stelle, bot, monkeypatch, ohne_espeak
):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot, passwort=PASSWORT)
    mitschnitt = mitschnitt_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid: STEHT)
    ctx = FakeCtx(kanal=thread)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert mitschnitt.kanal.verbindung.getrennt
    assert [spur.filename.split("-")[-1] for spur in recordings.pending(unsere)] == ["Mira.wav"]
    (antwort,) = ctx.antworten
    assert "wartet auf den Stapel" in antwort and chronik.FERTIG in antwort


def test_ein_altes_passwortfenster_zeigt_das_passwort_keiner_fremden_runde(
    stelle, bot, monkeypatch
):
    """Der stillste der fünf Wege: das Fenster lebt eine Viertelstunde und trägt seine Runde
    mit. Wäre die Kennung inzwischen neu vergeben, ginge das Passwort dieser Gruppe an das
    Foundry einer fremden — die Adresse dorthin steht in *ihrer* Runde."""
    config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    gesehen = []
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid: gesehen.append(eine.id))

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=thread.id))
    interaktion = FakeInteraction(thread)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        lebenszyklus.loeschen(config, unsere)
        frisch = runden.anlegen(config.database_path, "Frisch", guild_id=GILDE)
        ctx.modale[0].children[0].value = PASSWORT
        await ctx.modale[0].callback(interaktion)
        return frisch

    frisch = asyncio.run(ablauf())

    assert frisch.id == unsere.id
    assert interaktion.response.gesendet == [chronik.VERALTET]
    assert gesehen == []
    assert not zugang.ist_gemerkt(frisch)


def test_das_passwort_steht_in_keiner_antwort_und_liegt_danach_nicht_mehr(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid: STEHT)

    ctx, interaktion = abschluss_fahren(bot, unsere, thread)

    gesagt = " ".join(ctx.antworten + interaktion.response.gesendet + thread.gesendet)
    assert PASSWORT not in gesagt
    # Der Abgleich verbraucht es; hier tut das die Attrappe nicht — die harte Regel ist,
    # dass keine Antwort es zeigt.
    zugang.vergiss(unsere)
    assert not zugang.ist_gemerkt(unsere)


def test_ein_gescheiterter_lauf_meldet_sich_trotzdem_im_thread(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    def stolpert(config, eine, session_id):
        raise RuntimeError("Ollama war aus")

    monkeypatch.setattr(jobs, "abschluss", stolpert)

    abschluss_fahren(bot, unsere, thread)

    assert thread.gesendet[-1].startswith("Der Lauf ist nicht durchgekommen:")
    assert "Ollama war aus" in thread.gesendet[-1]


def test_ein_zweites_fertig_stoesst_keinen_zweiten_lauf_an(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    monkeypatch.setattr(jobs, "running", lambda eine, kind=None: True)

    interaktion = FakeInteraction(thread)
    meldung = chronik.abschluss_starten(
        _config, unsere, sitzung_id, PASSWORT, melden=lambda text: None
    )

    assert meldung == chronik.LAEUFT_SCHON
    assert interaktion.response.gesendet == []


def test_fertig_beendet_die_laufende_aufnahme_und_reiht_die_spuren_ein(
    stelle, bot, monkeypatch, ohne_espeak
):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    mitschnitt = mitschnitt_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, thread)

    assert mitschnitt.kanal.verbindung.getrennt
    assert not mitschnitt.kanal.verbindung.schneidet
    assert [spur.filename.split("-")[-1] for spur in recordings.pending(unsere)] == ["Mira.wav"]
    (antwort,) = interaktion.response.gesendet
    assert "wartet auf den Stapel" in antwort and chronik.FERTIG in antwort
    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert jobs.latest(unsere, jobs.CHRONIK, sitzung_id).result == STEHT

    danach = FakeSprechCtx(mitschnitt.wer)
    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["stop"](danach))
    assert danach.antworten == [gateway.LAEUFT_NICHT]


def test_ein_abschluss_reisst_den_mitschnitt_einer_fremden_runde_nicht_ab(
    stelle, bot, monkeypatch, ohne_espeak
):
    config, unsere = stelle
    fremde = runden.anlegen(config.database_path, "Die Andere", guild_id=FREMDE_GILDE)
    notes.create_session(fremde, played_on="2026-08-07")
    mitschnitt = mitschnitt_starten(bot, gilde=FREMDE_GILDE)
    _ctx, thread = sitzung_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, thread)

    assert not mitschnitt.kanal.verbindung.getrennt
    assert recordings.pending(fremde) == ()
    assert interaktion.response.gesendet == [chronik.FERTIG]


def test_fertig_ausserhalb_eines_sitzungs_threads_sagt_es(stelle, bot):
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=8888))

    asyncio.run(chronikbefehl(bot, "fertig")(ctx))

    (antwort,) = ctx.antworten
    assert chronik.NUR_IM_THREAD in antwort
    assert ctx.modale == []


def test_ein_stolpernder_abschluss_antwortet_trotzdem(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    def stolpert(*args, **kwargs):
        raise RuntimeError("irgendwas in der Bibliothek")

    monkeypatch.setattr(chronik, "abschluss_starten", stolpert)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, thread)

    (antwort,) = interaktion.response.gesendet
    assert antwort.startswith("Das hat nicht geklappt:")
    assert "RuntimeError" in antwort


# -- Nutzersprache, auch für den Bot ----------------------------------------------------


def test_keine_systemsprache_in_dem_was_der_bot_sagt():
    """Derselbe Sweep wie über die Seiten — eine Antwort ist genauso Oberfläche.

    Mit im Sweep: was der Bot *ausgibt*. Der Rückblick im Gruppenkanal, die Begleitzeile
    der Chronik-Datei und jeder Satz der Erinnern-Befehle werden von denselben Leuten
    gelesen wie eine Antwort auf einen Befehl.
    """
    verraten = {}
    for modul in (
        ansage,
        bot_eintritt,
        chronik,
        einrichten,
        erinnern,
        gateway,
        jobs,
        recorder,
        ausgabe,
        rueckblick,
    ):
        for name, wert in vars(modul).items():
            if name.isupper() and isinstance(wert, str) and systemsprache(wert):
                verraten[f"{modul.__name__}.{name}"] = systemsprache(wert)
    assert not verraten


def test_die_ansage_der_sitzung_sagt_was_zu_tun_ist(stelle, bot):
    _ctx, thread = sitzung_starten(bot)
    for satzteil in ("jede Nachricht", "/szene", "/chronik fertig", "Sprachnachricht"):
        assert satzteil in thread.gesendet[0]


def test_die_hilfe_nennt_auch_den_weg_in_die_sitzung(stelle, bot):
    ctx = FakeCtx()

    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["hilfe"](ctx))

    (antwort,) = ctx.antworten
    assert "/chronik start" in antwort and "/chronik fertig" in antwort


# -- Der Thread einer Runde schreibt nicht in die andere ---------------------------------


def test_der_thread_einer_fremden_runde_ist_nicht_erreichbar(stelle, bot):
    """Dieselbe Schranke wie im Isolationsgate, hier am Weg durch den Bot."""
    config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    fremde = erste_runde(config)
    assert fremde.id != unsere.id
    assert notes.session_of_thread(fremde, str(thread.id)) is None
