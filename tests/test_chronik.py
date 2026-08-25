"""Die Sitzung läuft im Kanal — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Die Sätze, die dieser Suite ihren Sinn geben: **die Gilde bestimmt die Runde** — ohne eine
passiert nichts —, **die Szene entscheidet der Zeitpunkt der Nachricht** und nicht der des
Ablegens, und **es antwortet immer jemand**, auch wenn etwas schiefgeht.

Die Attrappen der Gateway-Seite stehen in ``test_bot``; hier kommen die dazu, die es für
Kanäle, Nachrichten und das Passwort-Fenster braucht.
"""

from __future__ import annotations

import asyncio
import threading
import time
import types
from datetime import UTC, datetime, timedelta

import pytest
from conftest import (
    GM_FIGUR,
    GM_GEFLUESTER,
    GRENZE,
    LEITUNG,
    UNSER_KONTO,
    WELT,
    systemsprache,
)
from conftest import runde as erste_runde
from test_bot import (
    TOKEN,
    FakeAnhang,
    FakeBot,
    FakeButton,
    FakeGilde,
    FakeIntents,
    FakeMitglied,
    FakePCMAudio,
    FakePermissions,
    FakeSenke,
    FakeSprachkanal,
    FakeView,
    sprachdaten,
    spricht,
    stille,
)
from test_bot import FakeCtx as FakeSprechCtx
from test_dokument import BEISPIEL, DRITTER_ABEND

import chronicle.bot.__main__ as bot_eintritt
from chronicle import (
    db,
    jobs,
    lebenszyklus,
    notes,
    recordings,
    search,
    settings,
    zugang,
)
from chronicle import runde as runden
from chronicle.bot import ansage, chronik, einrichten, erinnern, gateway, recorder
from chronicle.compose import service as compose_service
from chronicle.config import Config
from chronicle.discord import ausgabe, rueckblick
from chronicle.foundry import client as foundry_client
from chronicle.foundry import service as foundry_service
from chronicle.foundry.client import FoundryUnreachable
from chronicle.transcribe import service as transcribe_service

GILDE = "1101"
FREMDE_GILDE = "9909"

# Zwei Mitglieder derselben Gilde: ``/session start`` steht jedem offen, und seit #96
# entscheidet die Kennung, wessen Eingabe der Abschluss ungefragt weiterreicht.
WER = 7001
ZWEITES_MITGLIED = 7002

PASSWORT = "passwort-nur-fuer-den-test"
ANDERE_EINGABE = "was-das-zweite-mitglied-tippt"

# Der Satz, den die Attrappe des Laufs zurückgibt — hier steht kein echtes Ollama dahinter.
STEHT = "Chronik und Rückblick stehen bereit."


# -- Die Attrappen ----------------------------------------------------------------------


class FakeHTTPException(Exception):
    """Was py-cord wirft, wenn Discord den Kanal verweigert."""


class FakeInputText:
    def __init__(self, *, label="", placeholder="", **rest):
        self.label = label
        self.placeholder = placeholder
        self.value = ""


class FakeModal:
    def __init__(self, *children, title="", **rest):
        self.children = list(children)
        self.title = title


class FakeKanal:
    """Ein Discord-Kanal, so weit der Bot ihn braucht: eine Kennung und ein Postfach.

    Seit #271 ist das zugleich der Ort der Sitzung — der Chat des Sprachkanals, an dem die
    Runde ohnehin sitzt. Einen eigenen Kanal gibt es nicht mehr.
    """

    def __init__(self, kennung=900, name="Runde"):
        self.id = kennung
        self.name = name
        self.mention = f"<#{kennung}>"
        self.gesendet: list[str] = []

    async def send(self, text):
        self.gesendet.append(text)


FakeTextkanal = FakeKanal


def _mitglied(wer, *, admin):
    """Ein Mitglied mit genau den Rechten, die Discord für diesen Server meldet."""
    return types.SimpleNamespace(
        id=wer, guild_permissions=types.SimpleNamespace(administrator=admin)
    )


class FakeCtx:
    def __init__(self, *, guild_id=GILDE, kanal=None, wer=WER, admin=True):
        self.guild_id = guild_id
        self.channel = kanal if kanal is not None else FakeTextkanal()
        self.channel_id = self.channel.id
        self.user = _mitglied(wer, admin=admin)
        self.author = self.user
        self.antworten: list[str] = []
        self.ansichten: list = []
        self.modale: list[FakeModal] = []
        self.aufgeschoben = False

    async def defer(self, **rest):
        self.aufgeschoben = True

    async def respond(self, text, *, view=None, **rest):
        self.antworten.append(text)
        self.ansichten.append(view)

    async def send_modal(self, modal):
        self.modale.append(modal)


class FakeAntwort:
    """Die *erste* Antwort auf eine Interaktion — oder der Aufschub, der sie vertagt."""

    def __init__(self):
        self.gesendet: list[str] = []
        self.bearbeitet: list[dict] = []
        self.aufgeschoben = False

    async def send_message(self, text, **rest):
        self.gesendet.append(text)

    async def edit_message(self, *, content=None, embed=None, view=None):
        self.bearbeitet.append({"content": content, "embed": embed, "view": view})

    async def defer(self, **rest):
        self.aufgeschoben = True

    def is_done(self) -> bool:
        return self.aufgeschoben or bool(self.gesendet) or bool(self.bearbeitet)


class FakeNachreichen:
    """Was nach einem Aufschub kommt — getrennt geführt, damit der Weg prüfbar bleibt."""

    def __init__(self):
        self.gesendet: list[str] = []

    async def send(self, text, **rest):
        self.gesendet.append(text)


class FakeInteraction:
    def __init__(self, kanal, *, guild_id=GILDE, wer=WER, admin=True):
        self.channel = kanal
        self.guild_id = guild_id
        self.user = _mitglied(wer, admin=admin)
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


def rohes_ereignis(message_id, *, gilde=GILDE, inhalt=None, kanal=None, zeit=None, vom_bot=False):
    """Die rohe Nutzlast eines Änderungs-Ereignisses, so weit der Handler sie liest.

    ``inhalt=None`` heißt: **kein** ``content``-Schlüssel — die Änderung betraf etwas
    anderes. ``inhalt=""`` ist der Fall, den Discord bei einer geleerten Bildunterschrift
    schickt, und ein ganz anderer.
    """
    daten = {} if inhalt is None else {"content": inhalt}
    if zeit is not None:
        daten["timestamp"] = zeit.isoformat()
    daten["author"] = {"id": "4001", "bot": vom_bot}
    return types.SimpleNamespace(
        message_id=message_id, channel_id=kanal, guild_id=gilde, data=daten
    )


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
    """Der Befehl unter seinem Namen, gleich in welcher der beiden Gruppen er hängt (#272)."""
    for gruppe in (gateway.GRUPPE, gateway.GRUPPE_CHRONIK):
        befehle = bot.gruppen[gruppe].befehle
        if name in befehle:
            return befehle[name]
    raise KeyError(name)


def sitzung_starten(bot, ctx=None, titel="", *, passwort="", wer=WER):
    """``/session start`` samt dem Fenster, das seit #96 davor steht.

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
    # Wie py-cord: den Kanal der Sitzung findet der Bot danach über seine Kennung.
    bot.kanaele[ctx.channel.id] = ctx.channel
    return fenster_interaktion, ctx.channel


def mitschnitt_starten(bot, gilde=GILDE):
    """``/session start`` in einem Sprachkanal dieser Gilde, mit einer Spur darin."""
    wer = FakeMitglied(4001, "Mira")
    kanal = FakeSprachkanal(FakeGilde(gilde), wer)
    wer.voice = types.SimpleNamespace(channel=kanal)
    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["start"](FakeSprechCtx(wer, guild_id=gilde)))
    kanal.verbindung.senke.write(sprachdaten(stille(480)), wer)
    return types.SimpleNamespace(kanal=kanal, wer=wer)


def sitzung_von(runde, kanal):
    """Die Sitzung dieses Kanals — auch die abgeschlossene, deren Kanal stehen bleibt."""
    for eine in notes.sessions(runde):
        if notes.channel_of_session(runde, eine.id) == str(kanal.id):
            return eine.id
    return None


def notizen(runde, sitzung_id):
    """Alle Notizen der Sitzung, Szene für Szene."""
    return [
        (szene.position, notiz.text)
        for szene in notes.session(runde, sitzung_id).scenes
        for notiz in szene.notes
    ]


# -- Sitzung und Kanal -----------------------------------------------------------------


def test_der_bot_bringt_die_chronik_befehle_mit(bot):
    assert set(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle) == {
        "abgleich",
        "sitzung-loeschen",
        "delete",
        "search",
        "who",
        "setup",
        "zuordnung",
    }
    # Anfang, Szene und Abschluss gehören zum Abend und damit unter `/session` (#272).
    assert {"start", "done", gateway.BEFEHL_SZENE} <= set(bot.gruppen[gateway.GRUPPE].befehle)
    assert set(bot.ereignisse) >= {"on_message", "on_raw_message_edit", "on_raw_message_delete"}


def test_ohne_nachrichten_absicht_bliebe_die_sitzung_ein_leerer_behaelter(bot):
    (gebaut,) = FakeBot.erzeugt
    assert gebaut.intents.messages and gebaut.intents.message_content


def test_start_legt_die_sitzung_im_kanal_an_und_sagt_wie_es_weitergeht(stelle, bot):
    _config, unsere = stelle

    fenster, kanal = sitzung_starten(bot, titel="Der Keller")

    sitzung_id = sitzung_von(unsere, kanal)
    assert sitzung_id is not None
    assert notes.session(unsere, sitzung_id).title == "Der Keller"
    # Die Ansage steht öffentlich im Kanal: ab jetzt wird hier jede Zeile eine Notiz, und
    # das muss lesen können, wer tippt — nicht nur, wer den Befehl gab.
    assert kanal.gesendet == [chronik.ANGELEGT]
    (antwort,) = fenster.antworten
    assert chronik.SITZUNG_STEHT in antwort


def test_eine_zweite_sitzung_wird_nicht_ueber_die_erste_gelegt(stelle, bot):
    """Das verhinderte bisher der Kanal von selbst — jetzt sagt es die Sitzungsgrenze.

    Zwei offene Sitzungen hätten zwei Ziele für dieselbe getippte Zeile, und der Mitschnitt
    hinge an der falschen.

    Seit #272 ist der zweite Aufruf kein Fehlgriff mehr, sondern der Griff, der den
    Mitschnitt nachholt: gefragt wird nicht noch einmal, angelegt wird nichts. Hier steht
    niemand in einem Sprachkanal, also bleibt genau das übrig — und wird auch so gesagt.
    """
    _config, unsere = stelle
    _erstes, kanal = sitzung_starten(bot, titel="Der Keller")
    zweiter = FakeCtx()

    asyncio.run(chronikbefehl(bot, "start")(zweiter, "Der Turm"))

    assert zweiter.antworten == [gateway.OHNE_SPRACHKANAL]
    assert zweiter.modale == []
    assert [eine.title for eine in notes.sessions(unsere)] == ["Der Keller"]
    assert chronik.sitzung_im_kanal(unsere, str(kanal.id)) == sitzung_von(unsere, kanal)


def test_ohne_runde_fuer_diesen_server_entsteht_nichts(stelle, bot):
    _config, _unsere = stelle
    ctx = FakeCtx(guild_id=FREMDE_GILDE)

    asyncio.run(chronikbefehl(bot, "start")(ctx, ""))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_RUNDE in antwort
    assert ctx.channel.gesendet == []
    assert ctx.modale == []


# -- Das Passwort beim Sitzungsstart ----------------------------------------------------


def test_start_fragt_das_passwort_im_fenster_und_haelt_es_die_sitzung_ueber(stelle, bot):
    _config, unsere = stelle
    ctx = FakeCtx()

    fenster, kanal = sitzung_starten(bot, ctx, passwort=PASSWORT)

    assert ctx.modale[0].title == chronik.START_TITEL
    assert zugang.passwort(unsere) == PASSWORT
    assert chronik.MIT_FOUNDRY in fenster.antworten[0]
    # Gezeigt wird *ob*, nie *was* — auch nicht im Kanal, den die ganze Runde liest.
    assert PASSWORT not in " ".join(fenster.antworten + kanal.gesendet)


def test_ohne_passwort_laeuft_die_sitzung_trotzdem(stelle, bot):
    _config, unsere = stelle

    fenster, kanal = sitzung_starten(bot)

    assert sitzung_von(unsere, kanal) is not None
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
    assert ctx.channel.gesendet == []
    assert notes.sessions(frisch) == ()
    assert not zugang.ist_gemerkt(frisch)


def test_das_startfenster_schiebt_auf_bevor_es_an_der_sitzung_arbeitet(stelle, bot):
    """Zwei REST-Runden passen nicht verlässlich in die drei Sekunden der ersten Antwort.

    Dass die Antwort **nachgereicht** kommt, ist der Beleg: der Aufschub stand davor.
    """
    _config, _unsere = stelle

    fenster, _kanal = sitzung_starten(bot, passwort=PASSWORT)

    assert fenster.response.aufgeschoben
    assert fenster.response.gesendet == []
    assert len(fenster.followup.gesendet) == 1


def test_ein_absturz_im_startfenster_laesst_niemanden_im_dunkeln(stelle, bot, monkeypatch):
    """Ohne den breiten Fang entkäme die Ausnahme in py-cords ``Modal.on_error``, das die
    Interaktion nie beantwortet — niemand erführe, dass die Sitzung nicht steht."""
    _config, unsere = stelle

    def stolpert(*rest, **auch):
        raise RuntimeError("die SQLite war weg")

    monkeypatch.setattr(chronik, "sitzung_anlegen", stolpert)

    fenster, _kanal = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.antworten
    assert gateway.UNERWARTET.format(typ="RuntimeError") in antwort
    assert notes.sessions(unsere) == ()
    # Der Absturz kam vor dem Merken: nichts liegt bis zur Frist herum.
    assert not zugang.ist_gemerkt(unsere)


def test_eine_nicht_zugestellte_begruessung_sagt_dass_die_sitzung_trotzdem_steht(
    stelle, bot, monkeypatch
):
    """Die Sitzung steht schon — »noch einmal versuchen« legte eine zweite an."""
    _config, unsere = stelle

    async def stumm(self, text):
        raise RuntimeError("Discord hat die Nachricht verweigert")

    monkeypatch.setattr(FakeKanal, "send", stumm)

    fenster, kanal = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.antworten
    assert chronik.STUMM_ANGELEGT in antwort
    assert sitzung_von(unsere, kanal) is not None
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

    fenster, kanal = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.followup.gesendet
    assert gateway.UNERWARTET.format(typ="RuntimeError") in antwort
    assert kanal.gesendet == []
    assert notes.sessions(unsere) == ()
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

    fenster, _kanal = sitzung_starten(bot, passwort=PASSWORT)

    (antwort,) = fenster.response.gesendet
    assert gateway.UNERWARTET.format(typ="RuntimeError") in antwort
    assert fenster.followup.gesendet == []


# -- Wo kein Foundry ist, wird auch nicht danach gefragt ---------------------------------


def test_auf_der_testwelt_kommt_kein_passwortfenster(stelle, bot):
    config, unsere = stelle
    settings.save_foundry_quelle(unsere, settings.TESTWELT)
    ctx = FakeCtx()

    asyncio.run(chronikbefehl(bot, "start")(ctx, ""))

    assert ctx.modale == []
    assert sitzung_von(unsere, ctx.channel) is not None
    assert chronik.KEIN_FOUNDRY in ctx.antworten[0]
    assert not chronik.foundry_im_spiel(config, unsere)


def test_ohne_eingetragene_adresse_kommt_kein_passwortfenster(tmp_path, pycord):
    """Eine Runde ohne ``/chronicle setup`` hat kein Foundry — und wird nicht gefragt."""
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


def test_eine_nachricht_im_kanal_wird_zur_notiz_und_bekommt_keine_quittung(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    nachricht = melden(bot, FakeNachricht(7001, "Wir steigen hinab.", kanal=kanal.id))

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == [(1, "Wir steigen hinab.")]
    assert nachricht.antworten == []


def test_was_der_bot_selbst_schreibt_ist_keine_notiz(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    melden(bot, FakeNachricht(7002, chronik.ANGELEGT, kanal=kanal.id, bot=True))

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == []


def test_ausserhalb_des_sitzungskanals_wird_nichts_abgelegt(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    nachricht = melden(bot, FakeNachricht(7003, "Nur geplaudert.", kanal=8888))

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == []
    assert nachricht.antworten == []


def test_vor_dem_start_und_nach_dem_abschluss_ist_getipptes_keine_notiz(stelle, bot):
    """Die Grenze liegt seit #271 auf der Zeit: derselbe Kanal, aber nur dazwischen.

    Vorher gab es keine Sitzung ohne Kanal, also stellte sich die Frage nicht — der
    Sprachkanal-Chat steht dagegen die ganze Woche da, und was dort sonst gesagt wird,
    gehört nicht in die Chronik.
    """
    _config, unsere = stelle
    kanal = FakeTextkanal()

    davor = melden(bot, FakeNachricht(7010, "Wann spielen wir?", kanal=kanal.id))

    _fenster, _kanal = sitzung_starten(bot, FakeCtx(kanal=kanal))
    sitzung_id = sitzung_von(unsere, kanal)
    melden(bot, FakeNachricht(7011, "Wir steigen hinab.", kanal=kanal.id))

    chronik.sitzung_schliessen(unsere, sitzung_id)
    danach = melden(bot, FakeNachricht(7012, "War ein schöner Abend.", kanal=kanal.id))

    assert notizen(unsere, sitzung_id) == [(1, "Wir steigen hinab.")]
    # Davor schweigt der Bot: in diesem Kanal lief noch nie ein Abend, die Zeile ist
    # Gerede. Danach sagt er es — seit #288 fällt sie nicht mehr stumm in nichts.
    assert davor.antworten == []
    assert danach.antworten == [gateway.ABEND_IST_ZU]


def test_wer_nach_dem_abschluss_weiterschreibt_erfaehrt_es_und_zwar_einmal(stelle, bot):
    """#288: das Abmoderieren fiel stumm in nichts — keine Antwort, kein Log, keine Zeile.

    Der Abend schließt seit #271 von selbst, sobald der Sprachkanal leer ist. Danach tippt
    der Tisch weiter, und genau dann fiel weg, was den Leuten am meisten am Herzen liegt.
    Der Bot hat den Abschluss selbst ausgelöst, also sagt er ihn auch (#265).

    Einmal je Abend: zehn Minuten Abmoderieren sind zwanzig Zeilen, und zwanzig gleiche
    Antworten wären selbst der Lärm.
    """
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)
    chronik.sitzung_schliessen(unsere, sitzung_id)

    erste = melden(bot, FakeNachricht(7020, "12 EP für alle.", kanal=kanal.id))
    zweite = melden(bot, FakeNachricht(7021, "Und der Ring geht an Mira.", kanal=kanal.id))

    assert erste.antworten == [gateway.ABEND_IST_ZU]
    assert zweite.antworten == []
    assert notizen(unsere, sitzung_id) == []


def test_aus_einem_server_ohne_runde_wird_nichts_abgelegt(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    melden(bot, FakeNachricht(7004, "Von nebenan.", kanal=kanal.id, gilde=FREMDE_GILDE))

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == []


def test_was_nicht_abgelegt_werden_konnte_bekommt_trotzdem_eine_antwort(stelle, bot, monkeypatch):
    _ctx, kanal = sitzung_starten(bot)

    def stolpert(*args, **kwargs):
        raise RuntimeError("irgendwas in der Datenschicht")

    monkeypatch.setattr(notes, "add_note", stolpert)

    nachricht = melden(bot, FakeNachricht(7005, "Wir steigen hinab.", kanal=kanal.id))

    (antwort,) = nachricht.antworten
    assert antwort.startswith("Das konnte ich nicht ablegen:")
    assert "RuntimeError" in antwort


# -- Szenen -----------------------------------------------------------------------------


def test_szene_zieht_die_trennlinie_und_das_naechste_landet_dahinter(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    melden(bot, FakeNachricht(7101, "Noch im Wirtshaus.", kanal=kanal.id))

    szene_ctx = FakeCtx(kanal=types.SimpleNamespace(id=kanal.id))
    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle[gateway.BEFEHL_SZENE](szene_ctx, "Im Keller"))
    spaeter = datetime.now(UTC) + timedelta(seconds=5)
    melden(bot, FakeNachricht(7102, "Die Treppe knarrt.", kanal=kanal.id, zeit=spaeter))

    sitzung_id = sitzung_von(unsere, kanal)
    assert szene_ctx.antworten == [chronik.SZENE.format(name="Im Keller")]
    assert notizen(unsere, sitzung_id) == [(1, "Noch im Wirtshaus."), (2, "Die Treppe knarrt.")]
    assert [s.title for s in notes.session(unsere, sitzung_id).scenes] == [None, "Im Keller"]


def test_eine_szene_ohne_namen_ist_trotzdem_eine_trennlinie(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=kanal.id))

    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle[gateway.BEFEHL_SZENE](ctx, "  "))

    sitzung_id = sitzung_von(unsere, kanal)
    assert ctx.antworten == [chronik.SZENE_OHNE_NAMEN]
    assert len(notes.session(unsere, sitzung_id).scenes) == 2


def test_szene_ausserhalb_einer_laufenden_sitzung_sagt_es(stelle, bot):
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=8888))

    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle[gateway.BEFEHL_SZENE](ctx, "Im Keller"))

    (antwort,) = ctx.antworten
    assert chronik.NUR_IN_DER_SITZUNG in antwort


def test_eine_tage_spaeter_nachgetragene_nachricht_landet_in_ihrer_szene(stelle, bot):
    """Die Szene entscheidet der Zeitpunkt der Nachricht, nicht der des Ablegens."""
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)
    notes.add_scene(unsere, sitzung_id, title="Im Keller", at="2026-08-05T21:00:00+00:00")
    notes.add_scene(unsere, sitzung_id, title="Auf dem Dach", at="2026-08-05T23:00:00+00:00")

    melden(
        bot,
        FakeNachricht(
            7201,
            "Da unten stand die Truhe.",
            kanal=kanal.id,
            zeit=datetime(2026, 8, 5, 22, 0, tzinfo=UTC),
        ),
    )

    assert notizen(unsere, sitzung_id) == [(2, "Da unten stand die Truhe.")]


def test_was_vor_jeder_trennlinie_liegt_bleibt_in_der_ersten_szene(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)
    notes.add_scene(unsere, sitzung_id, title="Im Keller", at="2026-08-05T21:00:00+00:00")

    melden(
        bot,
        FakeNachricht(
            7202, "Ganz am Anfang.", kanal=kanal.id, zeit=datetime(2020, 1, 1, tzinfo=UTC)
        ),
    )

    assert notizen(unsere, sitzung_id) == [(1, "Ganz am Anfang.")]


# -- Ändern und Löschen spiegeln --------------------------------------------------------


def aendern(bot, message_id, *, kanal=None, **rest):
    kanal = None if kanal is None else kanal.id
    asyncio.run(
        bot.ereignisse["on_raw_message_edit"](rohes_ereignis(message_id, kanal=kanal, **rest))
    )


def gesagt(kanal):
    """Was nach der Ansage in den Kanal ging — die steht dort schon vor jedem Test."""
    return kanal.gesendet[1:]


def test_eine_geaenderte_nachricht_aendert_ihre_notiz(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    melden(bot, FakeNachricht(7301, "Die Wirtin heißt Mara.", kanal=kanal.id))

    aendern(bot, 7301, kanal=kanal, inhalt="Die Wirtin heißt Mira.")

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == [(1, "Die Wirtin heißt Mira.")]
    # Die neue Fassung steht im Kanal; eine Quittung darunter wäre die zweite Hälfte
    # jedes Satzes eines ganzen Abends.
    assert gesagt(kanal) == []


def test_eine_aenderung_ohne_neuen_text_laesst_die_notiz_stehen(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    melden(bot, FakeNachricht(7302, "Die Wirtin heißt Mira.", kanal=kanal.id))

    aendern(bot, 7302, kanal=kanal)

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == [(1, "Die Wirtin heißt Mira.")]
    assert gesagt(kanal) == []


def test_eine_geleerte_nachricht_nimmt_ihre_notiz_mit(stelle, bot):
    """``content: ""`` heißt zurückgenommen — Notiz und Suchzeile gehen mit (#184)."""
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    melden(bot, FakeNachricht(7310, "Mara hat den Wirt bestochen.", kanal=kanal.id))
    assert search.find(unsere, "bestochen").groups

    aendern(bot, 7310, kanal=kanal, inhalt="")

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == []
    assert search.find(unsere, "bestochen").groups == ()
    assert gesagt(kanal) == [chronik.NOTIZ_FORT]


def test_nachgetragener_text_wird_zur_notiz_seiner_szene(stelle, bot):
    """Eine Nachricht ohne Text hatte nie eine Notiz — der Nachtrag legt sie an (#184)."""
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)
    notes.add_scene(unsere, sitzung_id, title="Im Keller", at="2026-08-05T21:00:00+00:00")
    notes.add_scene(unsere, sitzung_id, title="Auf dem Dach", at="2026-08-05T23:00:00+00:00")
    # Nur ein Anhang, kein Wort: so kommt eine Karte in den Kanal.
    melden(
        bot,
        FakeNachricht(
            7311,
            "",
            kanal=kanal.id,
            anhaenge=(FakeAnhang("karte.png"),),
            zeit=datetime(2026, 8, 5, 22, 0, tzinfo=UTC),
        ),
    )
    assert notizen(unsere, sitzung_id) == []

    aendern(
        bot,
        7311,
        kanal=kanal,
        inhalt="Der Gang hinter der Kammer.",
        zeit=datetime(2026, 8, 5, 22, 0, tzinfo=UTC),
    )

    # Die Szene der Nachricht, nicht die zuletzt gezogene.
    assert notizen(unsere, sitzung_id) == [(2, "Der Gang hinter der Kammer.")]
    assert search.find(unsere, "Kammer").groups
    assert gesagt(kanal) == [chronik.NOTIZ_NACHGETRAGEN]


def test_geschrieben_geaendert_geleert_und_wieder_nachgetragen(stelle, bot):
    """Die ganze Folge an einer Nachricht — jeder Schritt zieht die Notiz mit."""
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)
    melden(bot, FakeNachricht(7312, "Der Wirt heißt Bram.", kanal=kanal.id))

    aendern(bot, 7312, kanal=kanal, inhalt="Der Wirt heißt Brom.")
    assert notizen(unsere, sitzung_id) == [(1, "Der Wirt heißt Brom.")]

    aendern(bot, 7312, kanal=kanal, inhalt="   ")
    assert notizen(unsere, sitzung_id) == []
    assert search.find(unsere, "Brom").groups == ()

    aendern(bot, 7312, kanal=kanal, inhalt="Der Wirt heißt Bram.")
    assert notizen(unsere, sitzung_id) == [(1, "Der Wirt heißt Bram.")]
    assert gesagt(kanal) == [chronik.NOTIZ_FORT, chronik.NOTIZ_NACHGETRAGEN]


def test_steht_die_chronik_schon_wird_das_bei_jeder_aenderung_gesagt(stelle, bot):
    """Die Chronik ist ein Abzug: sie läuft sonst still von den Notizen weg (#184)."""
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)
    melden(bot, FakeNachricht(7313, "Mara hat den Wirt bestochen.", kanal=kanal.id))
    scope = db.scoped(unsere)
    try:
        compose_service.save(scope, sitzung_id, "# Chronik", "2026-08-05T22:00:00+00:00")
    finally:
        scope.close()

    aendern(bot, 7313, kanal=kanal, inhalt="Mara hat den Wirt gefragt.")
    aendern(bot, 7313, kanal=kanal, inhalt="")

    # Auch die stumme Änderung sagt es jetzt — und der Ausweg stimmt: ``/session done``
    # meint genau diese Sitzung.
    assert gesagt(kanal) == [
        chronik.CHRONIK_STEHT_SCHON,
        f"{chronik.NOTIZ_FORT} {chronik.CHRONIK_STEHT_SCHON}",
    ]


def test_an_einer_aelteren_sitzung_wird_kein_ausweg_versprochen(stelle, bot):
    """``/session done`` meint die zuletzt angelegte — für die davor wäre es gelogen."""
    _config, unsere = stelle
    kanal = FakeTextkanal()
    _ctx, kanal = sitzung_starten(bot, FakeCtx(kanal=kanal))
    sitzung_id = sitzung_von(unsere, kanal)
    melden(bot, FakeNachricht(7314, "Mara hat den Wirt bestochen.", kanal=kanal.id))
    scope = db.scoped(unsere)
    try:
        compose_service.save(scope, sitzung_id, "# Chronik", "2026-08-05T22:00:00+00:00")
    finally:
        scope.close()
    # Ein neuer Abend darüber: ``/session done`` meint jetzt ihn. Erst muss der alte zu
    # sein — zwei Sitzungen nebeneinander gibt es seit #271 nicht.
    chronik.sitzung_schliessen(unsere, sitzung_id)
    sitzung_starten(bot, FakeCtx(kanal=kanal))

    aendern(bot, 7314, kanal=kanal, inhalt="")

    # Die Notiz bleibt bei ihrem Abend, obwohl längst ein anderer läuft.
    assert notes.session_of_note(unsere, "7314") is None
    assert gesagt(kanal)[-1] == f"{chronik.NOTIZ_FORT} {chronik.CHRONIK_STEHT_SCHON_ALT}"


def test_was_der_bot_selbst_geschrieben_hat_wird_auch_beim_aendern_keine_notiz(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    aendern(bot, 7315, kanal=kanal, inhalt="Die Sitzung läuft.", vom_bot=True)

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == []
    assert gesagt(kanal) == []


def test_eine_woche_alte_notiz_bleibt_beim_bearbeiten_bei_ihrer_sitzung(stelle, bot):
    """``on_raw_message_edit`` gibt es genau dafür — und die Sitzung darf dabei nicht wandern.

    Ohne den Weg über die Notiz landete die Korrektur eines alten Abends am heutigen,
    sobald die Grenze nicht mehr am Ort hängt (#271).
    """
    _config, unsere = stelle
    kanal = FakeTextkanal()
    _ctx, _kanal = sitzung_starten(bot, FakeCtx(kanal=kanal), titel="Der Keller")
    alte = sitzung_von(unsere, kanal)
    melden(bot, FakeNachricht(7320, "Die Wirtin heißt Mara.", kanal=kanal.id))
    chronik.sitzung_schliessen(unsere, alte)

    # Eine Woche später läuft ein anderer Abend im selben Kanal.
    sitzung_starten(bot, FakeCtx(kanal=kanal), titel="Der Turm")
    neue = chronik.sitzung_im_kanal(unsere, str(kanal.id))
    aendern(bot, 7320, kanal=kanal, inhalt="Die Wirtin heißt Marla.")

    assert neue != alte
    assert notes.session_of_note(unsere, "7320") == alte
    assert notizen(unsere, alte) == [(1, "Die Wirtin heißt Marla.")]
    assert notizen(unsere, neue) == []


def test_eine_aenderung_ausserhalb_einer_laufenden_sitzung_legt_nichts_an(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)

    aendern(bot, 7316, inhalt="Im Plauderkanal.")

    assert notizen(unsere, sitzung_id) == []
    assert gesagt(kanal) == []


def test_eine_geloeschte_nachricht_verschwindet_auch_bei_uns(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    melden(bot, FakeNachricht(7303, "Das war falsch.", kanal=kanal.id))

    asyncio.run(bot.ereignisse["on_raw_message_delete"](rohes_ereignis(7303)))

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == []


def test_ein_ereignis_ohne_gilde_fasst_nichts_an(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    melden(bot, FakeNachricht(7304, "Bleibt stehen.", kanal=kanal.id))

    # Mit Kanal, aber ohne Gilde: so steht hier die Gilden-Weiche auf dem Prüfstand und
    # nicht bloß ein Ereignis, das ohnehin nirgends ankäme.
    aendern(bot, 7304, kanal=kanal, gilde=None, inhalt="weg")
    asyncio.run(
        bot.ereignisse["on_raw_message_delete"](rohes_ereignis(7304, gilde=None, kanal=kanal.id))
    )

    sitzung_id = sitzung_von(unsere, kanal)
    assert notizen(unsere, sitzung_id) == [(1, "Bleibt stehen.")]


# -- Diktat im Kanal -------------------------------------------------------------------


def test_eine_sprachnachricht_im_kanal_geht_in_dieselbe_warteschlange(stelle, bot):
    config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    nachricht = melden(
        bot,
        FakeNachricht(7401, "", kanal=kanal.id, anhaenge=(FakeAnhang("voice-message.ogg"),)),
    )

    (spur,) = recordings.pending(unsere)
    assert spur.session_id == sitzung_von(unsere, kanal)
    assert spur.discord_user_id == "4001"
    assert (config.recordings_dir / spur.filename).is_file()
    assert nachricht.antworten == [chronik.DIKTAT]


def test_das_diktat_behaelt_den_zeitpunkt_seiner_nachricht(stelle, bot):
    """Ohne ihn fände es nie eine Szene: eine Sitzungsuhr hat ein Diktat nicht (#160)."""
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    gestern = datetime(2026, 8, 6, 20, 30, tzinfo=UTC)

    melden(
        bot,
        FakeNachricht(7404, "", kanal=kanal.id, zeit=gestern, anhaenge=(FakeAnhang("memo.m4a"),)),
    )

    (spur,) = recordings.pending(unsere)
    assert spur.message_at == "2026-08-06T20:30:00+00:00"
    assert spur.started_at is None


def test_ein_anhang_ohne_ton_ist_einfach_kein_diktat(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    nachricht = melden(
        bot,
        FakeNachricht(
            7402, "Der Notizzettel.", kanal=kanal.id, anhaenge=(FakeAnhang("notizen.pdf"),)
        ),
    )

    sitzung_id = sitzung_von(unsere, kanal)
    assert recordings.pending(unsere) == ()
    assert notizen(unsere, sitzung_id) == [(1, "Der Notizzettel.")]
    assert nachricht.antworten == []


def test_eine_zu_grosse_aufnahme_bleibt_liegen_und_sagt_es(stelle, bot):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    riesig = FakeAnhang("lang.m4a", groesse=recordings.MAX_BYTES + 1)

    nachricht = melden(bot, FakeNachricht(7403, "", kanal=kanal.id, anhaenge=(riesig,)))

    assert recordings.pending(unsere) == ()
    assert nachricht.antworten == [
        chronik.ZU_GROSS.format(name="lang.m4a", grenze=recordings.MAX_BYTES // (1024 * 1024))
    ]


def test_eine_leere_aufnahme_wird_nicht_eingereiht_und_gesagt(stelle, bot):
    """0 Bytes sind keine Spur: eingereiht belegte sie einen Platz und einen Modelllauf."""
    config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    nachricht = melden(
        bot,
        FakeNachricht(7405, "", kanal=kanal.id, anhaenge=(FakeAnhang("memo.m4a", inhalt=b""),)),
    )

    assert recordings.pending(unsere) == ()
    assert list(config.recordings_dir.iterdir()) == []
    assert nachricht.antworten == [chronik.LEER.format(name="memo.m4a")]


def test_eine_winzige_aufnahme_kommt_trotzdem_durch(stelle, bot):
    """Die Gegenprobe: kurz ist nicht leer — ob eine Äußerung darin steckt, sagt #142."""
    config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    nachricht = melden(
        bot,
        FakeNachricht(7406, "", kanal=kanal.id, anhaenge=(FakeAnhang("kurz.m4a", inhalt=b"t"),)),
    )

    (spur,) = recordings.pending(unsere)
    assert (config.recordings_dir / spur.filename).read_bytes() == b"t"
    assert nachricht.antworten == [chronik.DIKTAT]


# -- Abschließen ------------------------------------------------------------------------


async def _bis_der_lauf_durch_ist(unsere):
    ende = time.monotonic() + GRENZE
    while time.monotonic() < ende and jobs.running(unsere, jobs.CHRONIK):
        await asyncio.sleep(0.01)
    # Der Lauf meldet sich aus seinem eigenen Faden; die Schleife muss ihn noch abholen.
    await asyncio.sleep(0.05)


def abschluss_fahren(bot, unsere, kanal, *, passwort=PASSWORT):
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=kanal.id))
    interaktion = FakeInteraction(kanal)

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
        if ctx.modale:
            ctx.modale[0].children[0].value = passwort
            await ctx.modale[0].callback(interaktion)
            await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())
    return ctx, interaktion


def erwartete_meldung(unsere, kanal):
    """Was der Abschluss antwortet — samt Namen der Sitzung und Verweis auf ihren Kanal."""
    sitzung = notes.session(unsere, sitzung_von(unsere, kanal))
    name = sitzung.title or f"Sitzung vom {sitzung.played_on}"
    kopf = chronik.SCHLIESST.format(sitzung=name, kanal=f"<#{kanal.id}>")
    return f"{kopf} {chronik.FERTIG}"


def test_fertig_fragt_nach_dem_passwort_und_stoesst_den_einen_lauf_an(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    gesehen = []

    def abschluss(config, eine, session_id, *, passwort=None):
        gesehen.append((passwort, session_id))
        return STEHT

    monkeypatch.setattr(jobs, "abschluss", abschluss)

    ctx, interaktion = abschluss_fahren(bot, unsere, kanal)

    sitzung_id = sitzung_von(unsere, kanal)
    assert ctx.modale[0].title == chronik.PASSWORT_TITEL
    assert gesehen == [(PASSWORT, sitzung_id)]
    assert interaktion.followup.gesendet == [erwartete_meldung(unsere, kanal)]
    assert kanal.gesendet == [chronik.ANGELEGT, STEHT]
    assert jobs.latest(unsere, jobs.CHRONIK, sitzung_id).result == STEHT


def test_nach_dem_passwort_beim_start_fragt_der_abschluss_nicht_noch_einmal(
    stelle, bot, monkeypatch
):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot, passwort=PASSWORT)
    gesehen = []

    def abschluss(config, eine, session_id, *, passwort=None):
        gesehen.append(passwort)
        return STEHT

    monkeypatch.setattr(jobs, "abschluss", abschluss)
    ctx = FakeCtx(kanal=kanal)

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert ctx.modale == []
    assert gesehen == [PASSWORT]
    assert ctx.antworten == [erwartete_meldung(unsere, kanal)]


def test_ein_zweites_mitglied_schiebt_dem_abschluss_kein_fremdes_passwort_unter(
    stelle, bot, monkeypatch
):
    """``/session start`` steht jedem Mitglied offen. Ohne diese Prüfung nähme der
    Abschluss die Zeichenkette eines Zweiten und zeigte sie dem Foundry-Konto dieser Runde
    vor, ausgelöst von jemandem, der sie nie gesehen hat."""
    _config, unsere = stelle
    _fenster, kanal = sitzung_starten(bot, passwort=PASSWORT, wer=WER)
    # Direkt in den Merkzettel: ein zweiter ``/session start`` legte seit #271 keine zweite
    # Sitzung mehr an, und der Weg über ``/chronicle abgleich`` führte am Abschluss vorbei.
    chronik.passwort_merken(unsere, ANDERE_EINGABE, str(ZWEITES_MITGLIED))
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, sid, *, passwort=None: gesehen.append(passwort) or STEHT,
    )
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=kanal.id), wer=WER)
    interaktion = FakeInteraction(kanal, wer=WER)

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
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
    _fenster, kanal = sitzung_starten(bot, passwort=PASSWORT, wer=WER)
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
    ctx = FakeCtx(kanal=kanal, wer=WER)

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
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
    _ctx, kanal = sitzung_starten(bot)
    mitschnitt_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, kanal)

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
    kanal = ctx.channel
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)
    abschluss_ctx = FakeCtx(kanal=kanal)

    async def ablauf():
        await chronikbefehl(bot, "done")(abschluss_ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    assert abschluss_ctx.modale == []
    assert abschluss_ctx.antworten == [erwartete_meldung(unsere, kanal)]


def test_auch_mit_gemerktem_passwort_endet_die_aufnahme_vor_dem_lauf(
    stelle, bot, monkeypatch, ohne_espeak
):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot, passwort=PASSWORT)
    mitschnitt = mitschnitt_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)
    ctx = FakeCtx(kanal=kanal)

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
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
    _ctx, kanal = sitzung_starten(bot)
    gesehen = []
    monkeypatch.setattr(
        jobs, "abschluss", lambda config, eine, sid, *, passwort=None: gesehen.append(eine.id)
    )

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=kanal.id))
    interaktion = FakeInteraction(kanal)

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
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
    _ctx, kanal = sitzung_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    ctx, interaktion = abschluss_fahren(bot, unsere, kanal)

    gesagt = " ".join(ctx.antworten + interaktion.antworten + kanal.gesendet)
    assert PASSWORT not in gesagt
    # Der Abgleich verbraucht es; hier tut das die Attrappe nicht — die harte Regel ist,
    # dass keine Antwort es zeigt.
    zugang.vergiss(unsere)
    assert not zugang.ist_gemerkt(unsere)


def test_ein_gescheiterter_lauf_meldet_sich_trotzdem_im_kanal(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    def stolpert(config, eine, session_id, *, passwort=None):
        raise RuntimeError("Ollama war aus")

    monkeypatch.setattr(jobs, "abschluss", stolpert)

    abschluss_fahren(bot, unsere, kanal)

    assert kanal.gesendet[-1].startswith("Der Lauf ist nicht durchgekommen:")
    assert "Ollama war aus" in kanal.gesendet[-1]


def test_ein_zweites_fertig_stoesst_keinen_zweiten_lauf_an(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    sitzung_id = sitzung_von(unsere, kanal)
    monkeypatch.setattr(jobs, "running", lambda eine, kind=None: True)

    interaktion = FakeInteraction(kanal)
    meldung = chronik.abschluss_starten(
        _config, unsere, sitzung_id, PASSWORT, melden=lambda text: None
    )

    assert meldung == chronik.LAEUFT_SCHON
    assert interaktion.response.gesendet == []


def test_fertig_beendet_die_laufende_aufnahme_und_reiht_die_spuren_ein(
    stelle, bot, monkeypatch, ohne_espeak
):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    mitschnitt = mitschnitt_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, kanal)

    assert mitschnitt.kanal.verbindung.getrennt
    assert not mitschnitt.kanal.verbindung.schneidet
    assert [spur.filename.split("-")[-1] for spur in recordings.pending(unsere)] == ["Mira.wav"]
    (antwort,) = interaktion.followup.gesendet
    assert "wartet auf den Stapel" in antwort and chronik.FERTIG in antwort
    sitzung_id = sitzung_von(unsere, kanal)
    assert jobs.latest(unsere, jobs.CHRONIK, sitzung_id).result == STEHT

    # Aus **dieser** Gilde, nicht aus irgendeiner: seit #226 sieht ``stop`` nur den Lauf
    # der eigenen Runde, und ein Aufruf von woanders erführe nichts über diesen hier.
    danach = FakeSprechCtx(mitschnitt.wer, guild_id=GILDE)
    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["pause"](danach))
    assert danach.antworten == [gateway.LAEUFT_NICHT]


def test_ein_abschluss_reisst_den_mitschnitt_einer_fremden_runde_nicht_ab(
    stelle, bot, monkeypatch, ohne_espeak
):
    config, unsere = stelle
    fremde = runden.anlegen(config.database_path, "Die Andere", guild_id=FREMDE_GILDE)
    notes.create_session(fremde, played_on="2026-08-07", laeuft=True)
    mitschnitt = mitschnitt_starten(bot, gilde=FREMDE_GILDE)
    _ctx, kanal = sitzung_starten(bot)
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, kanal)

    assert not mitschnitt.kanal.verbindung.getrennt
    assert recordings.pending(fremde) == ()
    assert interaktion.followup.gesendet == [erwartete_meldung(unsere, kanal)]


def test_fertig_aus_dem_sprachkanal_schliesst_die_laufende_sitzung(stelle, bot, monkeypatch):
    """Nach ``/session pause`` steht man im Sprachkanal — abgewiesen wird dort nicht mehr.

    Die Abweisung riet zu ``/session start`` und damit zu einer **zweiten** Sitzung; die
    Aufnahmen der ersten blieben liegen (#156). Genommen wird dieselbe Sitzung wie bei der
    Aufnahme, und die Antwort sagt, welche das ist und wo ihre Chronik erscheint.
    """
    _config, unsere = stelle
    _fenster, kanal = sitzung_starten(bot, titel="Der Keller", passwort=PASSWORT)
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, sid, *, passwort=None: gesehen.append(sid) or STEHT,
    )
    # Der Textkanal des Sprachkanals: nicht der Kanal, und auch nicht der Kanal, in dem
    # ``/session start`` gegeben wurde.
    ctx = FakeCtx(kanal=FakeKanal(4711, "Im Sprachkanal"))

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    sitzung_id = sitzung_von(unsere, kanal)
    assert gesehen == [sitzung_id]
    (antwort,) = ctx.antworten
    assert "Der Keller" in antwort and f"<#{kanal.id}>" in antwort
    assert antwort == erwartete_meldung(unsere, kanal)
    # Wer beim Start selbst hinterlegt hat, wird auch auf diesem Weg nicht zweimal gefragt.
    assert ctx.modale == []


def test_der_schnellweg_aus_dem_sprachkanal_schiebt_die_frist_nicht_weiter(
    stelle, bot, monkeypatch
):
    """``merken=False`` gilt auch dort, wo der Abschluss den Kanal nicht mehr braucht.

    Sonst hätte #156 dem Merkzettel eine dritte Gelegenheit gegeben, die zwölf Stunden aus
    #64 durch bloßes Nachfassen weiterzuschieben.
    """
    _config, unsere = stelle
    sitzung_starten(bot)
    zugang.merken(unsere, PASSWORT, wer=str(WER))
    faellig = zugang._gemerkt[unsere.id].ablauf
    monkeypatch.setattr(jobs, "start", lambda *args, **kwargs: None)

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=4711))
    asyncio.run(chronikbefehl(bot, "done")(ctx))

    # Der Schnellweg wurde wirklich gegangen — sonst prüfte die Frist unten eine Abweisung.
    assert ctx.antworten == [jobs.BELEGT]
    assert ctx.modale == []
    assert zugang._gemerkt[unsere.id].ablauf == faellig


def test_fertig_ohne_kanal_nennt_die_sitzung_trotzdem(stelle, bot, monkeypatch):
    """Die Weboberfläche legt Sitzungen ohne Kanal an — das darf nicht scheitern."""
    _config, unsere = stelle
    settings.save_foundry_quelle(unsere, settings.TESTWELT)
    notes.create_session(unsere, title="Ohne Faden", played_on="2026-08-11")
    monkeypatch.setattr(jobs, "abschluss", lambda config, eine, sid, *, passwort=None: STEHT)
    ctx = FakeCtx(kanal=FakeKanal(4711, "Im Sprachkanal"))

    async def ablauf():
        await chronikbefehl(bot, "done")(ctx)
        await _bis_der_lauf_durch_ist(unsere)

    asyncio.run(ablauf())

    ohne = chronik.SCHLIESST_OHNE_KANAL.format(sitzung="Ohne Faden")
    (antwort,) = ctx.antworten
    assert antwort == f"{ohne} {chronik.FERTIG}"
    assert "<#" not in antwort


def test_fertig_ohne_laufende_sitzung_raet_zu_chronik_start(stelle, bot):
    """Die Gegenprobe: nur wenn wirklich keine Sitzung offen ist, ist der Rat richtig."""
    ctx = FakeCtx(kanal=types.SimpleNamespace(id=8888))

    asyncio.run(chronikbefehl(bot, "done")(ctx))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_SITZUNG in antwort
    assert "`/session start`" in antwort
    assert ctx.modale == []


def test_fertig_einer_fremden_gilde_ruehrt_unsere_sitzung_nicht_an(stelle, bot, monkeypatch):
    """Die Runde bestimmt die Gilde — der Kanal einer fremden wird nie verlinkt."""
    config, unsere = stelle
    runden.anlegen(config.database_path, "Die Andere", guild_id=FREMDE_GILDE)
    _fenster, kanal = sitzung_starten(bot)
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, sid, *, passwort=None: gesehen.append(sid) or STEHT,
    )
    ctx = FakeCtx(guild_id=FREMDE_GILDE, kanal=types.SimpleNamespace(id=4711))

    asyncio.run(chronikbefehl(bot, "done")(ctx))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_SITZUNG in antwort
    assert f"<#{kanal.id}>" not in antwort
    assert gesehen == []


def test_ein_stolpernder_abschluss_antwortet_trotzdem(stelle, bot, monkeypatch):
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    def stolpert(*args, **kwargs):
        raise RuntimeError("irgendwas in der Bibliothek")

    monkeypatch.setattr(chronik, "abschluss_starten", stolpert)

    _ctx2, interaktion = abschluss_fahren(bot, unsere, kanal)

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
    """``/chronicle abgleich`` samt Fenster und dem Warten auf den Lauf, den es anstößt."""
    ctx = FakeCtx(kanal=kanal if kanal is not None else FakeKanal(910, "Runde"))
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
    ctx = FakeCtx(kanal=FakeKanal(910, "Runde"))
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
    _ctx, kanal = sitzung_starten(bot)
    for satzteil in ("jede Nachricht", "/session scene", "/session done", "Sprachnachricht"):
        assert satzteil in kanal.gesendet[0]


def test_die_hilfe_nennt_auch_den_weg_in_die_sitzung(stelle, bot):
    ctx = FakeCtx()

    asyncio.run(bot.gruppen[gateway.GRUPPE].befehle["help"](ctx))

    # Geteilt seit `/session check`: Discords 2000 Zeichen sind erreicht, und was zählt,
    # ist die Hilfe als Ganzes — angehängt ergeben die Stücke wieder genau sie.
    antwort = "".join(ctx.antworten)
    assert antwort == gateway.HILFE
    assert "/session start" in antwort and "/session done" in antwort


# -- Der Kanal einer Runde schreibt nicht in die andere ---------------------------------


def test_der_kanal_einer_fremden_runde_ist_nicht_erreichbar(stelle, bot):
    """Dieselbe Schranke wie im Isolationsgate, hier am Weg durch den Bot."""
    config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    fremde = erste_runde(config)
    assert fremde.id != unsere.id
    assert sitzung_von(fremde, kanal) is None


def test_ein_belegter_abgleich_schiebt_die_frist_des_gemerkten_passworts_nicht_weiter(
    stelle, bot, monkeypatch
):
    """Dieselbe Zusage wie beim Abschluss — der freistehende Abgleich ist der zweite Weg.

    Zwei Wege zum selben Merkzettel heißen zwei Gelegenheiten, die Frist aus #64 zu
    verlängern. Was für ``/session done`` gilt, muss hier gelten, sonst wandert der
    Fehler aus #100 einfach in den neuen Befehl.
    """
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)
    zugang.merken(unsere, PASSWORT, wer=str(WER))
    faellig = zugang._gemerkt[unsere.id].ablauf

    monkeypatch.setattr(jobs, "start", lambda *args, **kwargs: None)

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=kanal.id))
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
    _ctx, kanal = sitzung_starten(bot)
    zugang.merken(unsere, PASSWORT, wer=str(WER))
    faellig = zugang._gemerkt[unsere.id].ablauf

    # Die Maschine ist von einer anderen Runde belegt: der Auftrag entsteht nicht.
    monkeypatch.setattr(jobs, "start", lambda *args, **kwargs: None)

    ctx = FakeCtx(kanal=types.SimpleNamespace(id=kanal.id))
    asyncio.run(chronikbefehl(bot, "done")(ctx))

    assert ctx.modale == [], "wer selbst hinterlegt hat, wird nicht noch einmal gefragt"
    assert zugang._gemerkt[unsere.id].ablauf == faellig


def test_ein_dokument_im_kanal_faellt_nicht_mehr_still_durch(stelle, bot):
    """Der Fall aus #169: weder Notiz noch Diktat — und bis dahin sagte niemand etwas."""
    _config, unsere = stelle
    _ctx, kanal = sitzung_starten(bot)

    nachricht = melden(
        bot,
        FakeNachricht(7407, "", kanal=kanal.id, anhaenge=(FakeAnhang("notizen.md"),)),
    )

    assert nachricht.antworten == [chronik.DOKUMENT_IM_THREAD.format(name="notizen.md")]
    assert recordings.pending(unsere) == ()
    sitzung = sitzung_von(unsere, kanal)
    assert notes.session(unsere, sitzung).note_count == 0


def _verschriftetes_diktat(unsere, sitzung):
    """Eine Spur, die schon Text ist — ohne sie käme ``merge`` gar nicht bis zum Verwerfen."""
    spur = recordings.enqueue(
        unsere,
        sitzung,
        "memo.m4a",
        discord_user_id="4001",
        discord_name="Mira",
        message_at="2026-04-09T20:00:00+00:00",
    )
    recordings.mark(unsere, spur.id, recordings.FERTIG)
    scope = db.scoped(unsere)
    try:
        transcribe_service.store(
            scope, sitzung, "memo", ((0, 1000, "Diktiert am Heimweg."),), "2026-04-09T20:05:00"
        )
    finally:
        scope.close()


def _gelesen(text: str):
    async def lesen() -> bytes:
        return text.encode("utf-8")

    return lesen


async def _fertig_und_warten(bot, unsere, ctx):
    await chronikbefehl(bot, "done")(ctx)
    await _bis_der_lauf_durch_ist(unsere)


def test_ein_zweiter_abschluss_loescht_das_eingelesene_nicht(stelle, bot):
    """Die Falle aus #169: ``merge`` verwirft vor jedem Lauf alles seiner Herkunft.

    Trüge das Eingelesene die Herkunft des Transkripts, wäre es nach ``/session done``
    fort — und anders als ein Transkript entsteht es nicht neu. Der Lauf läuft hier
    wirklich, zweimal, und mit einer verschrifteten Spur in der Sitzung: ohne sie kehrte
    ``merge.uebernehmen`` vorher um und der Test bliebe grün, egal was hier steht.
    """
    _config, unsere = stelle
    settings.save_foundry_quelle(unsere, settings.TESTWELT)
    # Angelegt wird seit #272 unmittelbar: der Befehl ``/chronik einlesen`` ist fort, die
    # Herkunft dahinter nicht — und genau um sie geht es hier.
    vorschau = asyncio.run(
        chronik.dokument_vorschau(
            unsere,
            chronik.Notizdatei(
                filename="notizen.md",
                size=len(BEISPIEL.encode("utf-8")),
                lesen=_gelesen(BEISPIEL),
            ),
        )
    )
    chronik.dokument_anlegen(unsere, vorschau.abende)
    sitzung = notes.latest_session(unsere).id
    _verschriftetes_diktat(unsere, sitzung)

    for _ in range(2):
        asyncio.run(_fertig_und_warten(bot, unsere, FakeCtx(kanal=types.SimpleNamespace(id=4711))))

    texte = [text for _position, text in notizen(unsere, sitzung)]
    assert DRITTER_ABEND in texte
    # Und die Gegenprobe: das Abgeleitete war wirklich im Spiel und steht genau einmal da.
    assert [text for text in texte if "Diktiert am Heimweg." in text] == [
        "Mira: Diktiert am Heimweg."
    ]


# -- Der Ereignisstrom aus Foundry ------------------------------------------------------


class FoundryAttrappe:
    """Das Foundry der Runde, ohne Netz — Welt und Erreichbarkeit ändern sich zwischen Blicken."""

    def __init__(self, welt=None, fehler=None, konto=UNSER_KONTO):
        self.welt = welt
        self.fehler = fehler
        self.konto = konto
        self.blicke = 0

    def __call__(self, _wirksam, _geheim):
        if self.fehler is not None:
            raise self.fehler
        return self

    def fetch_world(self):
        self.blicke += 1
        return self.konto, self.welt


def ohne(welt, *kennungen):
    """Dieselbe Welt, bevor diese Nachrichten fielen — so wird der erste Blick zur Grundlinie."""
    return dict(welt, messages=[n for n in welt["messages"] if n["_id"] not in kennungen])


def ohne_wurf(welt):
    """Dieselbe Welt, bevor gewürfelt wurde — sie stellt die Grundlinie des ersten Blicks."""
    return ohne(welt, "m-wurf")


# Ein Wurf, den die Spielleitung genau **unserem** Konto zuflüstert. Er passiert den
# Rechtefilter des Archivs, denn wir sind gemeint — für die Runde bestimmt ist er trotzdem
# nicht.
GEFLUESTERTER_WURF = {
    "_id": "m-fluesterwurf",
    "timestamp": 6000,
    "author": LEITUNG,
    "whisper": [UNSER_KONTO, LEITUNG],
    "content": "",
    "speaker": {},
    "system": {"roll": {"title": "Heimlicher Fallenwurf", "total": 19}},
}


def mit(welt, *nachrichten):
    return dict(welt, messages=[*welt["messages"], *nachrichten])


def archiv(gestellt):
    """Was der Strom in die Chronik gelegt hat — sie bleibt voll, auch wo der Kanal leer bleibt."""
    return {
        n.id for n in foundry_service.current(gestellt.config, gestellt.runde).snapshot.messages
    }


def buehne(stelle, bot, monkeypatch, welt=WELT, fehler=None, konto=UNSER_KONTO):
    """Eine laufende Sitzung mit hinterlegtem Passwort und einem Foundry dahinter."""
    config, unsere = stelle
    _fenster, kanal = sitzung_starten(bot, passwort=PASSWORT)
    foundry = FoundryAttrappe(welt, fehler, konto)
    monkeypatch.setattr(foundry_service, "FoundryClient", foundry)
    sitzung = chronik.sitzung_im_kanal(unsere, str(kanal.id))
    return types.SimpleNamespace(
        config=config,
        runde=unsere,
        kanal=kanal,
        sitzung=sitzung,
        foundry=foundry,
        strom=chronik.Strom(runde=unsere, session_id=sitzung),
    )


def blick(gestellt):
    """Ein Durchlauf des Stroms — das, was ohne die Wartezeit dazwischen übrig bleibt."""
    return chronik.ereignisse_abholen(gestellt.config, gestellt.strom)


def fakten(gestellt):
    """Die Foundry-Ereignisse der Sitzung, Szene für Szene — so, wie die Chronik sie liest."""
    scope = db.scoped(gestellt.runde)
    try:
        stoff = compose_service.material(scope, gestellt.sitzung)
    finally:
        scope.close()
    return [(szene.position, [fakt.id for fakt in szene.facts]) for szene in stoff.scenes]


def test_der_erste_blick_stellt_nur_die_grundlinie(stelle, bot, monkeypatch):
    """Sonst stünde beim Anfang das ganze Chat-Log der Welt im Kanal.

    Eine Runde, die seit Wochen nicht abgeglichen hat, brächte damit hunderte Würfe von
    Abenden mit, die längst geschrieben sind.
    """
    gestellt = buehne(stelle, bot, monkeypatch)
    assert blick(gestellt).text == ""
    assert gestellt.foundry.blicke == 1


def test_neue_wuerfe_landen_im_kanal_mit_genau_den_zahlen_aus_dem_log(stelle, bot, monkeypatch):
    gestellt = buehne(stelle, bot, monkeypatch, welt=ohne_wurf(WELT))
    blick(gestellt)
    gestellt.foundry.welt = WELT

    gesagt = blick(gestellt).text

    assert "Brok Eisenfaust" in gesagt
    assert "Knowledge Roll" in gesagt
    assert "**7**" in gesagt
    assert "hope 3" in gesagt and "fear 1" in gesagt
    # Was Foundry nicht liefert, steht auch nicht da: die Summe des blinden Wurfs ist 18,
    # und sie hat in diesem Kanal nichts verloren.
    assert "18" not in gesagt


def test_der_blinde_wurf_der_spielleitung_bleibt_draussen(stelle, bot, monkeypatch):
    """Der Server filtert nicht — im Kanal läse ihn sonst die ganze Gruppe."""
    gestellt = buehne(stelle, bot, monkeypatch, welt=ohne_wurf(WELT))
    blick(gestellt)
    gestellt.foundry.welt = WELT

    gesagt = blick(gestellt).text

    assert "Verborgener Wurf" not in gesagt
    assert GM_FIGUR not in gesagt and GM_GEFLUESTER not in gesagt


def test_ein_wurf_der_uns_zugefluestert_wird_bleibt_draussen(stelle, bot, monkeypatch):
    """Der Kanal ist die **Gruppe**, nicht unser Konto.

    Mit dem empfohlenen Spielerkonto flüstert die Spielleitung genau diesem Konto einen Wurf
    zu. Der Rechtefilter des Archivs lässt ihn durch — wir sind ja gemeint —, und dort soll
    er auch stehen. In den Kanal gehört er nicht: dort läse ihn die ganze Runde.
    """
    gestellt = buehne(stelle, bot, monkeypatch, welt=ohne_wurf(WELT))
    blick(gestellt)
    gestellt.foundry.welt = mit(WELT, GEFLUESTERTER_WURF)

    gesagt = blick(gestellt).text

    assert "Heimlicher Fallenwurf" not in gesagt and "19" not in gesagt
    assert "Knowledge Roll" in gesagt, "der offene Wurf daneben kommt weiterhin an"
    assert "m-fluesterwurf" in archiv(gestellt)


def test_ein_blinder_wurf_bleibt_auch_mit_gm_konto_draussen(stelle, bot, monkeypatch):
    """Dass ein GM-Konto mehr sieht, ist die Entscheidung der Gruppe (#78) — der Kanal nicht.

    Neu ist an diesem Weg, dass GM-Inhalt **während** des Spiels ankäme statt hinterher in
    der Chronik: genau dann, wenn ein verborgener Wurf verborgen bleiben muss. Er fällt
    deshalb erst **nach** der Grundlinie — sonst hielte ihn schon die zurück.
    """
    gestellt = buehne(stelle, bot, monkeypatch, welt=ohne(WELT, "m-wurf", "m-blind"), konto=LEITUNG)
    blick(gestellt)
    gestellt.foundry.welt = WELT

    gesagt = blick(gestellt).text

    assert "Verborgener Wurf" not in gesagt and "18" not in gesagt
    assert GM_GEFLUESTER not in gesagt
    assert "Knowledge Roll" in gesagt, "der offene Wurf daneben kommt weiterhin an"
    assert "m-blind" in archiv(gestellt), "im Archiv steht er, weil dieses Konto ihn sieht"


def test_jedes_ereignis_genau_einmal(stelle, bot, monkeypatch):
    gestellt = buehne(stelle, bot, monkeypatch, welt=ohne_wurf(WELT))
    blick(gestellt)
    gestellt.foundry.welt = WELT

    assert "Knowledge Roll" in blick(gestellt).text
    assert blick(gestellt).text == ""


def test_der_wurf_haengt_an_der_szene_in_der_er_faellt(stelle, bot, monkeypatch):
    """Das ist der Ursprungsvermerk: ein Fakt an der Szene, keine Notiz im Text.

    Damit steht die Reihenfolge nicht mehr aus Zeitstempeln rekonstruiert da — und die
    Komposition kann Belegtes weiter von Verbindungssätzen trennen.
    """
    gestellt = buehne(stelle, bot, monkeypatch, welt=ohne_wurf(WELT))
    blick(gestellt)
    chronik.szene_setzen(gestellt.runde, gestellt.sitzung, "Im Keller")
    gestellt.foundry.welt = WELT

    blick(gestellt)

    assert fakten(gestellt) == [(1, []), (2, ["m-wurf"])]
    assert notizen(gestellt.runde, gestellt.sitzung) == []


def test_ein_ausfall_wird_einmal_gesagt_und_dann_still_weiter_versucht(stelle, bot, monkeypatch):
    """Sonst bestünde der Kanal einer Sitzung mit totem Server aus nichts anderem."""
    gestellt = buehne(stelle, bot, monkeypatch, fehler=FoundryUnreachable("Verbindung abgelehnt"))

    erste = blick(gestellt)
    zweite = blick(gestellt)

    assert erste.text.startswith("Ich komme gerade nicht an euer Foundry")
    assert erste.weiter
    assert zweite.text == "" and zweite.weiter


def test_kommt_foundry_zurueck_wird_auch_das_gesagt(stelle, bot, monkeypatch):
    gestellt = buehne(stelle, bot, monkeypatch, fehler=FoundryUnreachable("Verbindung abgelehnt"))
    blick(gestellt)
    gestellt.foundry.fehler = None
    gestellt.foundry.welt = ohne_wurf(WELT)

    zurueck = blick(gestellt)
    gestellt.foundry.welt = WELT

    assert zurueck.text == chronik.WIEDER_DA
    assert "Knowledge Roll" in blick(gestellt).text, "was in der Zwischenzeit fiel, kommt nach"


def test_ohne_passwort_endet_der_strom_mit_einem_satz(stelle, bot, monkeypatch):
    """Nach dem Abschluss ist das Passwort eingelöst — dann hört der Beobachter auf."""
    gestellt = buehne(stelle, bot, monkeypatch)
    zugang.vergiss(gestellt.runde)

    ende = blick(gestellt)

    assert not ende.weiter
    assert ende.text.startswith("Ich sehe von hier an nicht mehr nach Foundry")
    assert gestellt.foundry.blicke == 0


def test_eine_ruhende_runde_bekommt_keinen_strom_und_keinen_satz(stelle, bot, monkeypatch):
    gestellt = buehne(stelle, bot, monkeypatch)
    lebenszyklus.sperren(gestellt.config.database_path, GILDE)

    ende = blick(gestellt)

    assert not ende.weiter and ende.text == ""
    assert gestellt.foundry.blicke == 0


def test_der_strom_beginnt_mit_der_sitzung_und_endet_mit_ihr(stelle, bot, monkeypatch):
    """Bestellt wird er beim Start mit Passwort und abbestellt beim Abschluss.

    Der Abschluss holt die Zahlen selbst und verbraucht dabei das Passwort; ein Beobachter
    daneben fände beim nächsten Blick keines mehr und sagte das in einen Kanal, dessen
    Sitzung gerade geschrieben wird.
    """
    _config, unsere = stelle
    gestellt: list = []
    abbestellt: list = []
    monkeypatch.setattr(
        gateway, "_strom_stellen", lambda _c, _b, _lauf, r, s: gestellt.append((r.id, s))
    )

    async def abbestellen(_lauf, s):
        abbestellt.append(s)

    monkeypatch.setattr(gateway, "_strom_abbestellen", abbestellen)

    _fenster, kanal = sitzung_starten(bot, passwort=PASSWORT)
    sitzung = chronik.sitzung_im_kanal(unsere, str(kanal.id))
    asyncio.run(chronikbefehl(bot, "done")(FakeCtx(kanal=types.SimpleNamespace(id=kanal.id))))

    assert gestellt == [(unsere.id, sitzung)]
    assert abbestellt == [sitzung]


def test_ohne_passwort_wird_kein_beobachter_bestellt(stelle, bot, monkeypatch):
    """Gegenprobe: ohne Passwort käme er an keinen Server und beendete sich sofort."""
    gestellt: list = []
    monkeypatch.setattr(
        gateway, "_strom_stellen", lambda _c, _b, _lauf, r, s: gestellt.append((r.id, s))
    )

    sitzung_starten(bot, passwort="")

    assert gestellt == []


def test_der_beobachter_stellt_in_den_kanal_bis_es_nichts_mehr_zu_sehen_gibt(
    stelle, bot, monkeypatch
):
    """Der Faden selbst: warten, nachsehen, einstellen — und aufhören, wenn Schluss ist."""
    config, unsere = stelle
    _fenster, kanal = sitzung_starten(bot, passwort=PASSWORT)
    sitzung = chronik.sitzung_im_kanal(unsere, str(kanal.id))
    bot.kanaele[kanal.id] = kanal
    monkeypatch.setattr(chronik, "STROM_ABSTAND", 0)
    meldungen = iter(
        (chronik.Meldung(text="🎲 erster Wurf"), chronik.Meldung(text="Schluss", weiter=False))
    )
    monkeypatch.setattr(chronik, "ereignisse_abholen", lambda _config, _strom: next(meldungen))
    lauf = gateway._Lauf()

    async def fahren():
        # Über ``_strom_stellen`` und nicht am Faden vorbei: sonst stünde der Beobachter nie
        # im Lauf, und die Frage »ist er hinterher ausgetragen« wäre keine.
        gateway._strom_stellen(config, bot, lauf, unsere, sitzung)
        faden = lauf.stroeme[sitzung]
        waehrenddessen = dict(lauf.stroeme)
        await faden
        return waehrenddessen

    waehrenddessen = asyncio.run(fahren())

    assert kanal.gesendet[-2:] == ["🎲 erster Wurf", "Schluss"]
    assert list(waehrenddessen) == [sitzung], "solange er läuft, steht er im Lauf"
    assert lauf.stroeme == {}, "und danach trägt er sich selbst wieder aus"


def test_das_abbestellen_wartet_auf_den_laufenden_blick(stelle, bot, monkeypatch):
    """``cancel`` erreicht keinen Faden, der gerade in ``asyncio.to_thread`` sitzt.

    Ohne das Warten liefe der Blick zu Ende und schriebe seinen Stand **hinter** den des
    Abschlusses; ein Wurf aus diesem Zwischenraum stünde danach als »nicht mehr vorhanden«
    in der Chronik — eine Unwahrheit über einen Beleg.
    """
    config, unsere = stelle
    _fenster, kanal = sitzung_starten(bot, passwort=PASSWORT)
    sitzung = chronik.sitzung_im_kanal(unsere, str(kanal.id))
    monkeypatch.setattr(chronik, "STROM_ABSTAND", 0)
    angefangen = threading.Event()
    geschrieben: list = []

    def blick_der_dauert(_config, _strom):
        angefangen.set()
        time.sleep(0.05)
        geschrieben.append("Stand des laufenden Blicks")
        return chronik.Meldung()

    monkeypatch.setattr(chronik, "ereignisse_abholen", blick_der_dauert)
    lauf = gateway._Lauf()

    async def fahren():
        gateway._strom_stellen(config, bot, lauf, unsere, sitzung)
        await asyncio.to_thread(angefangen.wait, GRENZE)
        await gateway._strom_abbestellen(lauf, sitzung)
        return list(geschrieben)

    assert asyncio.run(fahren()) == ["Stand des laufenden Blicks"]
    assert lauf.stroeme == {}
