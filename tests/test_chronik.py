"""Der Thread ist die Sitzung — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Die Sätze, die dieser Suite ihren Sinn geben: **die Gilde bestimmt die Runde** — ohne eine
passiert nichts —, **die Szene entscheidet der Zeitpunkt der Nachricht** und nicht der des
Ablegens, und **es antwortet immer jemand**, auch wenn etwas schiefgeht.

Die Attrappen der Gateway-Seite stehen in ``test_bot``; hier kommen die dazu, die es für
Threads, Nachrichten und das Passwort-Fenster braucht.
"""

from __future__ import annotations

import asyncio
import threading
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
from chronicle.foundry import client as foundry_client
from chronicle.foundry import service as foundry_service
from chronicle.foundry.client import FoundryUnreachable

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

    def is_done(self) -> bool:
        return self.aufgeschoben or bool(self.gesendet)


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
    import sys

    modul = types.ModuleType("discord")
    modul.Option = FakeOption
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
        # Seit #96 fragt der Start nur, wo ein Foundry-Server im Spiel ist — und das ist
        # dieselbe Bedingung, die der Client stellt: Adresse **und** Konto. Ohne beides
        # bekäme diese Runde gar kein Fenster mehr.
        foundry_url="https://foundry.example",
        foundry_user="chronik-bot",
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
    assert set(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle) == {
        "start",
        "fertig",
        "abgleich",
        "nacherzaehlung",
        "loeschen",
    }
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


def test_auch_ein_absturz_im_vorspann_des_fensters_antwortet(stelle, bot, monkeypatch):
    """Der Fang liegt um den ganzen Rückruf, nicht nur um seinen Rumpf.

    Das Auflösen der Runde geht auf die SQLite und liegt **vor** dem Rumpf. Fiele es
    heraus, bliebe der Absender bei »This interaction failed« stehen.
    """
    _config, unsere = stelle

    def stolpert(*rest, **auch):
        raise RuntimeError("die SQLite war weg")

    monkeypatch.setattr(chronik, "dieselbe_runde", stolpert)

    fenster, thread = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.followup.gesendet
    assert gateway.UNERWARTET.format(typ="RuntimeError") in antwort
    assert thread is None
    assert not zugang.ist_gemerkt(unsere)


def test_auch_ein_gescheiterter_aufschub_antwortet_noch(stelle, bot, monkeypatch):
    """Und was vor dem Aufschub scheitert, wird als *erste* Antwort gesagt.

    Vorher gab es keine Antwort, also ist auch keine nachzureichen — ``_sagen`` muss
    beides können, sonst wäre der Fang selbst die zweite Fehlerquelle.
    """
    _config, _unsere = stelle

    async def stolpert(self, **rest):
        raise RuntimeError("Discord hat den Aufschub verweigert")

    monkeypatch.setattr(FakeAntwort, "defer", stolpert)

    fenster, _thread = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.response.gesendet
    assert gateway.UNERWARTET.format(typ="RuntimeError") in antwort
    assert fenster.followup.gesendet == []


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


def test_eine_adresse_ohne_konto_ist_noch_kein_foundry(tmp_path, pycord):
    """Dieselbe Bedingung wie im Client: ohne Konto kommt kein Handschlag zustande.

    Sonst würde ein Passwort eingesammelt, das nie jemandem vorgezeigt wird — ein
    ``FoundryError`` verbraucht es, bevor der erste Byte über die Leitung geht.
    """
    config = Config(
        discord_bot_token=TOKEN,
        foundry_url="https://foundry.example",
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "aufnahmen",
    )
    db.init(config.database_path)
    unsere = runden.anlegen(config.database_path, "Halb eingerichtet", guild_id=GILDE)
    ctx = FakeCtx()

    asyncio.run(chronikbefehl(gateway.baue(config), "start")(ctx, ""))

    assert not chronik.foundry_im_spiel(config, unsere)
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


def erwartete_meldung(unsere, thread):
    """Was der Abschluss antwortet — samt Namen der Sitzung und Verweis auf ihren Thread."""
    sitzung = notes.session(unsere, notes.session_of_thread(unsere, str(thread.id)))
    name = sitzung.title or f"Sitzung vom {sitzung.played_on}"
    kopf = chronik.SCHLIESST.format(sitzung=name, thread=f"<#{thread.id}>")
    return f"{kopf} {chronik.FERTIG}"


def test_fertig_fragt_nach_dem_passwort_und_stoesst_den_einen_lauf_an(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    gesehen = []

    def abschluss(config, eine, session_id, *, passwort=None):
        gesehen.append((passwort, session_id))
        return STEHT

    monkeypatch.setattr(jobs, "abschluss", abschluss)

    ctx, interaktion = abschluss_fahren(bot, unsere, thread)

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert ctx.modale[0].title == chronik.PASSWORT_TITEL
    assert gesehen == [(PASSWORT, sitzung_id)]
    assert interaktion.followup.gesendet == [erwartete_meldung(unsere, thread)]
    assert thread.gesendet == [chronik.ANGELEGT, STEHT]
    assert jobs.latest(unsere, jobs.CHRONIK, sitzung_id).result == STEHT


def test_nach_dem_passwort_beim_start_fragt_der_abschluss_nicht_noch_einmal(
    stelle, bot, monkeypatch
):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot, passwort=PASSWORT)
    gesehen = []

    def abschluss(config, eine, session_id, *, passwort=None):
        gesehen.append(passwort)
        return STEHT

    monkeypatch.setattr(jobs, "abschluss", abschluss)
    ctx = FakeCtx(kanal=thread)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert ctx.modale == []
    assert gesehen == [PASSWORT]
    assert ctx.antworten == [erwartete_meldung(unsere, thread)]


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
        jobs,
        "abschluss",
        lambda config, eine, sid, *, passwort=None: gesehen.append(passwort) or STEHT,
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


def test_ein_fenster_in_der_luecke_schiebt_dem_lauf_kein_fremdes_passwort_unter(
    stelle, bot, monkeypatch
):
    """Der Schnellweg prüft in der Koroutine und lief bis #96 im Auftragsfaden nach.

    Dazwischen liegen der Aufschub und das Beenden des Mitschnitts. Wer in dieser Lücke
    ein vorher geöffnetes Startfenster absendet, ersetzte den Merkzettel — geprüft wäre
    das eine gewesen, vorgezeigt das andere. Gelesen wird deshalb im Befehl, und der
    gelesene Wert reist mit dem Auftrag.
    """
    _config, unsere = stelle
    _fenster, thread = sitzung_starten(bot, passwort=PASSWORT, wer=WER)
    gesehen = []

    def abschluss(config, eine, session_id, *, passwort=None):
        # Derselbe Rückfall wie im echten Abgleich — daran zeigt sich der Unterschied
        # zwischen »mitgebracht« und »im fremden Faden nachgesehen«.
        gesehen.append(passwort or zugang.passwort(eine))
        return STEHT

    monkeypatch.setattr(jobs, "abschluss", abschluss)

    async def dazwischenfunken(lauf, eine=None):
        zugang.merken(unsere, ANDERE_EINGABE, wer=str(ZWEITES_MITGLIED))
        return ()

    monkeypatch.setattr(gateway, "_mitschnitt_beenden", dazwischenfunken)
    ctx = FakeCtx(kanal=thread, wer=WER)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert ctx.modale == []
    assert gesehen == [PASSWORT]


def test_das_passwortfenster_schiebt_auf_bevor_es_die_aufnahme_beendet(
    stelle, bot, monkeypatch, ohne_espeak
):
    """Mehr Arbeit als am Startfenster: erst der Mitschnitt, dann der Lauf.

    Auch hier ist der Beleg, dass die Antwort **nachgereicht** kommt.
    """
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    mitschnitt_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, thread)

    assert interaktion.response.aufgeschoben
    assert interaktion.response.gesendet == []
    assert len(interaktion.followup.gesendet) == 1


def test_wer_selbst_hinterlegt_hat_wird_nicht_noch_einmal_gefragt(stelle, bot):
    _config, unsere = stelle
    sitzung_starten(bot, passwort=PASSWORT, wer=WER)

    assert chronik.passwort_fuer(unsere, str(WER)) == PASSWORT
    assert chronik.passwort_fuer(unsere, str(ZWEITES_MITGLIED)) is None
    # Und ohne Kennung erst recht nicht — sonst wäre der Schnellweg wieder für alle offen.
    assert chronik.passwort_fuer(unsere, "") is None


def test_ohne_foundry_fragt_auch_der_abschluss_nicht_nach_dem_passwort(stelle, bot, monkeypatch):
    _config, unsere = stelle
    settings.save_foundry_quelle(unsere, settings.TESTWELT)
    ctx = FakeCtx()
    asyncio.run(chronikbefehl(bot, "start")(ctx, ""))
    thread = ctx.channel.threads[-1]
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)
    abschluss_ctx = FakeCtx(kanal=thread)

    async def ablauf():
        await chronikbefehl(bot, "fertig")(abschluss_ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert abschluss_ctx.modale == []
    assert abschluss_ctx.antworten == [erwartete_meldung(unsere, thread)]


def test_auch_mit_gemerktem_passwort_endet_die_aufnahme_vor_dem_lauf(
    stelle, bot, monkeypatch, ohne_espeak
):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot, passwort=PASSWORT)
    mitschnitt = mitschnitt_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)
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
    monkeypatch.setattr(
        jobs, "abschluss", lambda config, eine, sid, *, passwort=None: gesehen.append(eine.id)
    )

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
    assert interaktion.followup.gesendet == [chronik.VERALTET]
    assert gesehen == []
    assert not zugang.ist_gemerkt(frisch)


def test_das_passwort_steht_in_keiner_antwort_und_liegt_danach_nicht_mehr(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    ctx, interaktion = abschluss_fahren(bot, unsere, thread)

    gesagt = " ".join(ctx.antworten + interaktion.antworten + thread.gesendet)
    assert PASSWORT not in gesagt
    # Der Abgleich verbraucht es; hier tut das die Attrappe nicht — die harte Regel ist,
    # dass keine Antwort es zeigt.
    zugang.vergiss(unsere)
    assert not zugang.ist_gemerkt(unsere)


def test_ein_gescheiterter_lauf_meldet_sich_trotzdem_im_thread(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    def stolpert(config, eine, session_id, *, passwort=None):
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
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, thread)

    assert mitschnitt.kanal.verbindung.getrennt
    assert not mitschnitt.kanal.verbindung.schneidet
    assert [spur.filename.split("-")[-1] for spur in recordings.pending(unsere)] == ["Mira.wav"]
    (antwort,) = interaktion.followup.gesendet
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
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, thread)

    assert not mitschnitt.kanal.verbindung.getrennt
    assert recordings.pending(fremde) == ()
    assert interaktion.followup.gesendet == [erwartete_meldung(unsere, thread)]


def test_fertig_aus_dem_sprachkanal_schliesst_die_laufende_sitzung(stelle, bot, monkeypatch):
    """Nach ``/aufnahme stop`` steht man im Sprachkanal — abgewiesen wird dort nicht mehr.

    Die Abweisung riet zu ``/chronik start`` und damit zu einer **zweiten** Sitzung; die
    Aufnahmen der ersten blieben liegen (#156). Genommen wird dieselbe Sitzung wie bei der
    Aufnahme, und die Antwort sagt, welche das ist und wo ihre Chronik erscheint.
    """
    _config, unsere = stelle
    _fenster, thread = sitzung_starten(bot, titel="Der Keller", passwort=PASSWORT)
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, sid, *, passwort=None: gesehen.append(sid) or STEHT,
    )
    # Der Textkanal des Sprachkanals: nicht der Thread, und auch nicht der Kanal, in dem
    # ``/chronik start`` gegeben wurde.
    ctx = FakeCtx(kanal=FakeThread(4711, "Im Sprachkanal"))

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    sitzung_id = notes.session_of_thread(unsere, str(thread.id))
    assert gesehen == [sitzung_id]
    (antwort,) = ctx.antworten
    assert "Der Keller" in antwort and f"<#{thread.id}>" in antwort
    assert antwort == erwartete_meldung(unsere, thread)
    # Wer beim Start selbst hinterlegt hat, wird auch auf diesem Weg nicht zweimal gefragt.
    assert ctx.modale == []


def test_der_schnellweg_aus_dem_sprachkanal_schiebt_die_frist_nicht_weiter(
    stelle, bot, monkeypatch
):
    """``merken=False`` gilt auch dort, wo der Abschluss den Thread nicht mehr braucht.

    Sonst hätte #156 dem Merkzettel eine dritte Gelegenheit gegeben, die zwölf Stunden aus
    #64 durch bloßes Nachfassen weiterzuschieben.
    """
    _config, unsere = stelle
    sitzung_starten(bot)
    zugang.merken(unsere, PASSWORT, wer=str(WER))
    faellig = zugang._gemerkt[unsere.id].ablauf
    monkeypatch.setattr(jobs, "start", lambda *args, **kwargs: None)

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=4711))
    asyncio.run(chronikbefehl(bot, "fertig")(ctx))

    # Der Schnellweg wurde wirklich gegangen — sonst prüfte die Frist unten eine Abweisung.
    assert ctx.antworten == [jobs.BELEGT]
    assert ctx.modale == []
    assert zugang._gemerkt[unsere.id].ablauf == faellig


def test_fertig_ohne_thread_nennt_die_sitzung_trotzdem(stelle, bot, monkeypatch):
    """Die Weboberfläche legt Sitzungen ohne Thread an — das darf nicht scheitern."""
    _config, unsere = stelle
    settings.save_foundry_quelle(unsere, settings.TESTWELT)
    notes.create_session(unsere, title="Ohne Faden", played_on="2026-08-11")
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)
    ctx = FakeCtx(kanal=FakeThread(4711, "Im Sprachkanal"))

    async def ablauf():
        await chronikbefehl(bot, "fertig")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    ohne = chronik.SCHLIESST_OHNE_THREAD.format(sitzung="Ohne Faden")
    (antwort,) = ctx.antworten
    assert antwort == f"{ohne} {chronik.FERTIG}"
    assert "<#" not in antwort


def test_fertig_ohne_laufende_sitzung_raet_zu_chronik_start(stelle, bot):
    """Die Gegenprobe: nur wenn wirklich keine Sitzung offen ist, ist der Rat richtig."""
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=8888))

    asyncio.run(chronikbefehl(bot, "fertig")(ctx))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_SITZUNG in antwort
    assert "`/chronik start`" in antwort
    assert ctx.modale == []


def test_fertig_einer_fremden_gilde_ruehrt_unsere_sitzung_nicht_an(stelle, bot, monkeypatch):
    """Die Runde bestimmt die Gilde — der Thread einer fremden wird nie verlinkt."""
    config, unsere = stelle
    runden.anlegen(config.database_path, "Die Andere", guild_id=FREMDE_GILDE)
    _fenster, thread = sitzung_starten(bot)
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, sid, *, passwort=None: gesehen.append(sid) or STEHT,
    )
    ctx = FakeCtx(guild_id=FREMDE_GILDE, kanal=types.SimpleNamespace(id=4711))

    asyncio.run(chronikbefehl(bot, "fertig")(ctx))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_SITZUNG in antwort
    assert f"<#{thread.id}>" not in antwort
    assert gesehen == []


def test_ein_stolpernder_abschluss_antwortet_trotzdem(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    def stolpert(*args, **kwargs):
        raise RuntimeError("irgendwas in der Bibliothek")

    monkeypatch.setattr(chronik, "abschluss_starten", stolpert)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, thread)

    (antwort,) = interaktion.followup.gesendet
    assert antwort.startswith("Das hat nicht geklappt:")
    assert "RuntimeError" in antwort


# -- Der freistehende Abgleich -----------------------------------------------------------

# Was die Attrappe des Laufs meldet — ein echtes Foundry steht hier nicht dahinter.
ABGEGLICHEN = "Stand vom 2026-05-01T20:00:00+00:00 — 3 Spieler, 2 Charaktere, 8 Chat-Nachrichten."


class Ausfall:
    """Ein Foundry, das nicht antwortet — der Fall, für den es #116 überhaupt gibt."""

    def fetch_world(self):
        raise FoundryUnreachable("keine Antwort")


def abgleich_fahren(bot, unsere, *, kanal=None, passwort=PASSWORT):
    """``/chronik abgleich`` samt Fenster und dem Warten auf den Lauf, den es anstößt."""
    ctx = FakeCtx(kanal=kanal if kanal is not None else FakeThread(910, "Runde"))
    interaktion = FakeInteraction(ctx.channel)

    async def ablauf():
        await chronikbefehl(bot, "abgleich")(ctx)
        for fenster in ctx.modale:
            fenster.children[0].value = passwort
            await fenster.callback(interaktion)
        ende = time.monotonic() + GRENZE
        while time.monotonic() < ende and jobs.running(unsere, jobs.ABGLEICH):
            await asyncio.sleep(0.01)
        # Der Lauf meldet sich aus seinem eigenen Faden; die Schleife muss ihn abholen.
        await asyncio.sleep(0.05)

    asyncio.run(ablauf())
    return ctx, interaktion


def test_abgleich_fragt_das_passwort_im_fenster_und_stoesst_den_lauf_an(stelle, bot, monkeypatch):
    """Der Griff, der bisher fehlte: Zahlen nachziehen, ohne eine Sitzung zu führen (#116).

    Der Befehl wartet nicht auf den Abgleich — er stößt einen Lauf des Servers an und
    antwortet sofort; die Meldung kommt später in den Kanal, aus dem gefragt wurde.
    """
    _config, unsere = stelle
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abgleich",
        lambda config, eine, *, passwort=None: gesehen.append(passwort) or ABGEGLICHEN,
    )

    ctx, interaktion = abgleich_fahren(bot, unsere)

    assert ctx.modale[0].title == chronik.ABGLEICH_TITEL
    assert gesehen == [PASSWORT]
    assert interaktion.followup.gesendet == [chronik.ABGLEICH]
    assert ctx.channel.gesendet == [ABGEGLICHEN]
    assert jobs.latest(unsere, jobs.ABGLEICH).result == ABGEGLICHEN
    # Gezeigt wird *ob*, nie *was* — auch nicht in dem, was die ganze Runde liest.
    assert PASSWORT not in " ".join(interaktion.antworten + ctx.channel.gesendet)


def test_der_befehl_wartet_nicht_auf_den_abgleich(stelle, bot, monkeypatch):
    """Ein Foundry, das schleppt, darf den Befehl nicht in Discords Zeitfenster halten.

    Das Tor steht noch zu, wenn die Antwort schon draußen ist — anders gäbe es keine
    Meldung »ich melde mich hier«, sondern ein »denkt nach …«, das in einen Abbruch läuft.
    """
    _config, unsere = stelle
    tor = threading.Event()
    monkeypatch.setattr(
        jobs,
        "abgleich",
        lambda config, eine, *, passwort=None: tor.wait(GRENZE) and ABGEGLICHEN,
    )
    ctx = FakeCtx(kanal=FakeThread(910, "Runde"))
    interaktion = FakeInteraction(ctx.channel)

    unterwegs = []

    async def ablauf():
        await chronikbefehl(bot, "abgleich")(ctx)
        ctx.modale[0].children[0].value = PASSWORT
        await ctx.modale[0].callback(interaktion)
        # Die Antwort ist draußen, der Lauf hängt noch am Tor — genau das ist die Zusage.
        unterwegs.append((list(interaktion.followup.gesendet), jobs.running(unsere, jobs.ABGLEICH)))
        tor.set()
        ende = time.monotonic() + GRENZE
        while time.monotonic() < ende and jobs.running(unsere, jobs.ABGLEICH):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    asyncio.run(ablauf())

    assert unterwegs == [([chronik.ABGLEICH], True)]
    assert ctx.channel.gesendet == [ABGEGLICHEN]


def test_der_abgleich_verbraucht_das_passwort_auch_wenn_er_scheitert(stelle, bot, monkeypatch):
    """Die Zusage aus #64, an der neuen Stelle: ein Fehlschlag lässt keines liegen."""
    _config, unsere = stelle
    monkeypatch.setattr(
        jobs,
        "sync",
        lambda config, eine, passwort=None: foundry_service.sync(
            config, eine, passwort=passwort, client=Ausfall()
        ),
    )

    _ctx, interaktion = abgleich_fahren(bot, unsere)

    assert interaktion.followup.gesendet == [chronik.ABGLEICH]
    assert not zugang.ist_gemerkt(unsere)
    assert jobs.latest(unsere, jobs.ABGLEICH).gescheitert


def test_wer_beim_start_hinterlegt_hat_wird_vor_dem_abgleich_nicht_gefragt(
    stelle, bot, monkeypatch
):
    _config, unsere = stelle
    sitzung_starten(bot, passwort=PASSWORT)
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abgleich",
        lambda config, eine, *, passwort=None: gesehen.append(passwort) or ABGEGLICHEN,
    )

    ctx, _interaktion = abgleich_fahren(bot, unsere)

    assert ctx.modale == []
    assert gesehen == [PASSWORT]
    assert ctx.antworten == [chronik.ABGLEICH]


def test_auf_der_testwelt_verbraucht_der_abgleich_das_liegende_passwort_ohne_zu_fragen(stelle, bot):
    """Kein Server, kein Fenster — und trotzdem wird nichts liegen gelassen."""
    _config, unsere = stelle
    sitzung_starten(bot, passwort=PASSWORT)
    settings.save_foundry_quelle(unsere, settings.TESTWELT)

    ctx, _interaktion = abgleich_fahren(bot, unsere)

    assert ctx.modale == []
    assert ctx.antworten == [chronik.ABGLEICH]
    assert not zugang.ist_gemerkt(unsere)
    assert jobs.latest(unsere, jobs.ABGLEICH).fertig


def test_die_ruhende_runde_gleicht_nichts_mehr_ab(stelle, bot):
    """Dieselbe Sperre wie vor jeder anderen Stufe — und noch vor dem Passwortfenster."""
    config, unsere = stelle
    lebenszyklus.sperren(config.database_path, GILDE)

    ctx, _interaktion = abgleich_fahren(bot, unsere)

    (antwort,) = ctx.antworten
    assert "Diese Runde ruht" in antwort
    assert ctx.modale == []
    assert jobs.latest(unsere, jobs.ABGLEICH) is None


def test_ein_zweiter_abgleich_stoesst_keinen_zweiten_lauf_an(stelle, bot, monkeypatch):
    config, unsere = stelle
    monkeypatch.setattr(jobs, "running", lambda eine, kind=None: True)

    meldung = chronik.abgleich_starten(config, unsere, PASSWORT, melden=lambda text: None)

    assert meldung == chronik.ABGLEICH_LAEUFT_SCHON


def test_ein_altes_abgleichfenster_zeigt_das_passwort_keiner_fremden_runde(stelle, bot):
    """Wie am Abschluss: die Adresse, an die es ginge, steht in *ihrer* Runde."""
    config, unsere = stelle
    ctx = FakeCtx()
    asyncio.run(chronikbefehl(bot, "abgleich")(ctx))

    lebenszyklus.loeschen(config, unsere)
    frisch = runden.anlegen(config.database_path, "Nachbarn", guild_id=GILDE)
    assert frisch.id == unsere.id
    interaktion = FakeInteraction(ctx.channel)
    ctx.modale[0].children[0].value = PASSWORT
    asyncio.run(ctx.modale[0].callback(interaktion))

    assert interaktion.followup.gesendet == [chronik.VERALTET]
    assert not zugang.ist_gemerkt(frisch)
    assert jobs.latest(frisch, jobs.ABGLEICH) is None


# -- Nacherzählen über Sitzungsgrenzen ---------------------------------------------------

# Was die Attrappe des Laufs meldet — ein echtes Ollama steht hier nicht dahinter.
NACHERZAEHLT = "Nacherzählung über 2 Sitzungen — 2 davon erzählt."


def sitzungen_anlegen(unsere, *daten):
    return [notes.create_session(unsere, played_on=datum) for datum in daten]


def nacherzaehlen_fahren(bot, unsere, *, von="", bis=""):
    """``/chronik nacherzaehlung`` und das Warten auf den Lauf, den es anstößt."""
    ctx = FakeCtx(kanal=FakeThread(910, "Runde"))

    async def ablauf():
        await chronikbefehl(bot, "nacherzaehlung")(ctx, von, bis)
        ende = time.monotonic() + GRENZE
        while time.monotonic() < ende and jobs.running(unsere, jobs.NACHERZAEHLUNG):
            await asyncio.sleep(0.01)
        # Der Lauf meldet sich aus seinem eigenen Faden; die Schleife muss ihn abholen.
        await asyncio.sleep(0.05)

    asyncio.run(ablauf())
    return ctx


def test_nacherzaehlung_stoesst_einen_lauf_an_und_blockiert_den_befehl_nicht(
    stelle, bot, monkeypatch
):
    """Je Sitzung ein Modellaufruf — das ist ein Lauf des Servers, kein wartender Befehl."""
    _config, unsere = stelle
    ids = sitzungen_anlegen(unsere, "2026-05-01", "2026-05-02")
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "nacherzaehlung",
        lambda config, eine, von, bis, kanal_id: (
            gesehen.append((von, bis, kanal_id)) or NACHERZAEHLT
        ),
    )

    ctx = nacherzaehlen_fahren(bot, unsere)

    assert ctx.aufgeschoben
    assert ctx.antworten == [chronik.ERZAEHLT.format(von="2026-05-01", bis="2026-05-02")]
    assert gesehen == [(ids[0], ids[1], "910")]
    assert ctx.channel.gesendet == [NACHERZAEHLT]
    assert jobs.latest(unsere, jobs.NACHERZAEHLUNG).result == NACHERZAEHLT


def test_der_bereich_folgt_den_genannten_sitzungen(stelle, bot, monkeypatch):
    _config, unsere = stelle
    ids = sitzungen_anlegen(unsere, "2026-05-01", "2026-05-02", "2026-05-03")
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "nacherzaehlung",
        lambda config, eine, von, bis, kanal_id: gesehen.append((von, bis)) or NACHERZAEHLT,
    )

    ctx = nacherzaehlen_fahren(bot, unsere, von="2026-05-02", bis="2026-05-03")

    assert gesehen == [(ids[1], ids[2])]
    assert ctx.antworten == [chronik.ERZAEHLT.format(von="2026-05-02", bis="2026-05-03")]


def test_ein_datum_ohne_sitzung_bekommt_eine_ehrliche_auskunft(stelle, bot):
    _config, unsere = stelle
    sitzungen_anlegen(unsere, "2026-05-01", "2026-05-02")

    ctx = nacherzaehlen_fahren(bot, unsere, von="2026-09-30")

    (antwort,) = ctx.antworten
    assert "2026-09-30" in antwort
    assert "2026-05-01" in antwort and "2026-05-02" in antwort
    assert jobs.latest(unsere, jobs.NACHERZAEHLUNG) is None


def test_ohne_geschriebene_sitzung_gibt_es_nichts_nachzuerzaehlen(stelle, bot):
    _config, unsere = stelle

    ctx = nacherzaehlen_fahren(bot, unsere)

    (antwort,) = ctx.antworten
    assert chronik.OHNE_SITZUNGEN in antwort
    assert jobs.latest(unsere, jobs.NACHERZAEHLUNG) is None


def test_vertauschte_daten_nennt_die_antwort_in_gespielter_reihenfolge(stelle, bot):
    """Wer die Daten verdreht tippt, bekommt sie geordnet zurück — sonst log die Antwort.

    Geordnet wird ohnehin, aber erst beim Holen der Sitzungen; die Antwort im Kanal nennt
    die beiden Eckpunkte so, wie sie hier herauskommen.
    """
    _config, unsere = stelle
    sitzungen_anlegen(unsere, "2026-05-01", "2026-05-02")

    ctx = nacherzaehlen_fahren(bot, unsere, von="2026-05-02", bis="2026-05-01")

    (antwort, *_) = ctx.antworten
    assert antwort.index("2026-05-01") < antwort.index("2026-05-02")


def test_ein_zweites_nacherzaehlen_stoesst_keinen_zweiten_lauf_an(stelle, bot, monkeypatch):
    config, unsere = stelle
    sitzungen_anlegen(unsere, "2026-05-01")
    monkeypatch.setattr(jobs, "running", lambda eine, kind=None: True)

    meldung = chronik.nacherzaehlung_starten(
        config, unsere, "", "", "910", melden=lambda text: None
    )

    assert meldung == chronik.ERZAEHLT_SCHON


def test_die_ruhende_runde_erzaehlt_nichts_mehr_nach(stelle, bot):
    """Dieselbe Sperre wie vor jeder anderen Stufe — sie bekommt nichts und legt nichts ab."""
    config, unsere = stelle
    sitzungen_anlegen(unsere, "2026-05-01")
    lebenszyklus.sperren(config.database_path, GILDE)

    ctx = nacherzaehlen_fahren(bot, unsere)

    (antwort,) = ctx.antworten
    assert "Diese Runde ruht" in antwort
    assert jobs.latest(unsere, jobs.NACHERZAEHLUNG) is None


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

    # Geteilt seit `/aufnahme test`: Discords 2000 Zeichen sind erreicht, und was zählt,
    # ist die Hilfe als Ganzes — angehängt ergeben die Stücke wieder genau sie.
    antwort = "".join(ctx.antworten)
    assert antwort == gateway.HILFE
    assert "/chronik start" in antwort and "/chronik fertig" in antwort


# -- Der Thread einer Runde schreibt nicht in die andere ---------------------------------


def test_der_thread_einer_fremden_runde_ist_nicht_erreichbar(stelle, bot):
    """Dieselbe Schranke wie im Isolationsgate, hier am Weg durch den Bot."""
    config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)

    fremde = erste_runde(config)
    assert fremde.id != unsere.id
    assert notes.session_of_thread(fremde, str(thread.id)) is None


def test_ein_belegter_abgleich_schiebt_die_frist_des_gemerkten_passworts_nicht_weiter(
    stelle, bot, monkeypatch
):
    """Dieselbe Zusage wie beim Abschluss — der freistehende Abgleich ist der zweite Weg.

    Zwei Wege zum selben Merkzettel heißen zwei Gelegenheiten, die Frist aus #64 zu
    verlängern. Was für ``/chronik fertig`` gilt, muss hier gelten, sonst wandert der
    Fehler aus #100 einfach in den neuen Befehl.
    """
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    zugang.merken(unsere, PASSWORT, wer=str(WER))
    faellig = zugang._gemerkt[unsere.id].ablauf

    monkeypatch.setattr(jobs, "start", lambda *args, **kwargs: None)

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=thread.id))
    asyncio.run(chronikbefehl(bot, "abgleich")(ctx))

    assert ctx.modale == [], "wer selbst hinterlegt hat, wird nicht noch einmal gefragt"
    assert zugang._gemerkt[unsere.id].ablauf == faellig


def test_der_abgleich_zeigt_das_mitgebrachte_passwort_vor_und_nicht_das_gemerkte(
    stelle, monkeypatch
):
    """Der Auftragsfaden sieht nicht selbst im Merkzettel nach.

    Zwischen Prüfung und Lauf liegt ein ``defer``; in dieser Lücke kann ein zweites
    Fenster den Merkzettel überschreiben. Geprüft wäre dann das eine, vorgezeigt das
    andere — dieselbe Ersetzbarkeit, gegen die #96 gebaut wurde.
    """
    config, unsere = stelle
    gesehen = []

    def merken_und_melden(wirksam, geheim):
        gesehen.append(geheim)
        raise foundry_client.FoundryError("Foundry war aus")

    monkeypatch.setattr(foundry_service, "FoundryClient", merken_und_melden)
    zugang.merken(unsere, "das-von-jemand-anderem", wer="4002")

    with pytest.raises(jobs.JobError):
        jobs.abgleich(config, unsere, passwort="das-geprüfte")

    assert gesehen == ["das-geprüfte"]


def test_ein_belegter_lauf_schiebt_die_frist_des_gemerkten_passworts_nicht_weiter(
    stelle, bot, monkeypatch
):
    """Der Schnellweg legt nicht neu ab, was er aus dem Merkzettel geholt hat.

    Täte er es, bekäme der Merkzettel bei jedem Versuch eine frische Frist — und weil
    ``jobs.start`` leer ausgeht, sobald eine andere Runde die Maschine hält, verbrauchte
    das Passwort dann niemand. Aus den zwölf Stunden aus #64 würde eine Frist, die sich
    durch bloßes Nachfassen beliebig weit schieben lässt.
    """
    _config, unsere = stelle
    _ctx, thread = sitzung_starten(bot)
    zugang.merken(unsere, PASSWORT, wer=str(WER))
    faellig = zugang._gemerkt[unsere.id].ablauf

    # Die Maschine ist von einer anderen Runde belegt: der Auftrag entsteht nicht.
    monkeypatch.setattr(jobs, "start", lambda *args, **kwargs: None)

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=thread.id))
    asyncio.run(chronikbefehl(bot, "fertig")(ctx))

    assert ctx.modale == [], "wer selbst hinterlegt hat, wird nicht noch einmal gefragt"
    assert zugang._gemerkt[unsere.id].ablauf == faellig
