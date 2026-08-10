"""Der Aufnahme-Bot — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Kein Test spricht mit discord.com, und keiner braucht die Sprach-Abhängigkeiten: das
Gegenüber sind ein paar Dutzend Zeilen Attrappe. Der Token in diesen Tests ist erfunden
und steht nur hier.

Die beiden Sätze, die dieser Suite ihren Sinn geben: **vor der Ansage wird nichts
geschrieben**, und **was angesagt wurde, steht im Wortlaut im Protokoll.**
"""

from __future__ import annotations

import asyncio
import sys
import types
import wave
from array import array
from pathlib import Path

import pytest
from conftest import runde as erste_runde

import chronicle.bot.__main__ as entry
from chronicle import consent, db, notes, recordings, settings
from chronicle.bot import BotFehler, ansage, gateway, recorder
from chronicle.bot.ansage import AnsageFehlt
from chronicle.bot.recorder import Aufnahme, Kanal, NichtAngesagt
from chronicle.config import Config

TOKEN = "aufnahme-bot-token-nur-fuer-den-test"

KANAL = Kanal(guild_id="11", id="77", name="Runde")

MIRA = consent.Member(id="4001", name="Mira")
BROK = consent.Member(id="4002", name="Brok")
SPAET = consent.Member(id="4003", name="Aelin")

# Eine Sekunde Ton in der Form, die espeak-ng liefert: 22050 Hz, Mono, 16 Bit.
ESPEAK_RATE = 22050


def spricht(text: str, ziel: Path) -> None:
    with wave.open(str(ziel), "wb") as datei:
        datei.setnchannels(1)
        datei.setsampwidth(2)
        datei.setframerate(ESPEAK_RATE)
        datei.writeframes(array("h", [100, -100] * (ESPEAK_RATE // 2)).tobytes())


def stille(rahmen: int) -> bytes:
    return bytes(rahmen * ansage.KANAELE * ansage.BREITE)


def sprachdaten(pcm: bytes):
    """Die Form, in der py-cords Empfangs-Router die Senke füttert: ``VoiceData``."""
    return types.SimpleNamespace(pcm=pcm, packet=None, source=None)


@pytest.fixture
def konfiguration(tmp_path):
    return Config(
        discord_bot_token=TOKEN,
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "aufnahmen",
    )


@pytest.fixture
def sitzung_id(konfiguration):
    db.init(konfiguration.database_path)
    return notes.create_session(erste_runde(konfiguration), played_on="2026-08-06")


@pytest.fixture
def ohne_espeak(monkeypatch):
    """Der echte Weg durch ``datei``, nur mit einer Attrappe statt des Systempakets."""
    echt = ansage.datei
    monkeypatch.setattr(
        ansage, "datei", lambda ordner, **rest: echt(ordner, sprecher=spricht, **rest)
    )


class FakeStimme:
    def __init__(self, *anwesend):
        self.kanal = KANAL
        self._anwesend = anwesend or (MIRA, BROK)
        self.ablauf: list[str] = []
        self.angesagt: list[Path] = []
        self.aufnahme = None

    def mitglieder(self):
        return tuple(self._anwesend)

    async def ansagen(self, datei):
        self.ablauf.append("ansage")
        self.angesagt.append(datei)

    def mitschneiden(self, aufnahme):
        self.ablauf.append("mitschnitt")
        self.aufnahme = aufnahme

    def mitschnitt_beenden(self):
        self.ablauf.append("mitschnitt-ende")

    async def trennen(self):
        self.ablauf.append("getrennt")


# -- Die Ansage ------------------------------------------------------------------------


def test_die_ansage_nennt_aufnahme_zweck_und_den_ausweg():
    for satzteil in ("aufgezeichnet", "Sitzungsprotokoll", "verlässt", "Sprachkanal"):
        assert satzteil in ansage.TEXT


def test_die_zugesagte_frist_ist_die_frist_aus_dem_code():
    # Der Satz darf sich nicht von dem entfernen, was ``recordings.sweep`` durchsetzt.
    assert f"{recordings.RETENTION_TAGE} Tage" in ansage.TEXT
    assert "aufbewahrt und dann gelöscht" in ansage.TEXT


def test_die_frist_wird_beim_start_und_danach_taeglich_geprueft(konfiguration, sitzung_id):
    class Schluss(Exception):
        pass

    laeufe = []

    async def schlafen(sekunden):
        laeufe.append(sekunden)
        if len(laeufe) == 2:
            raise Schluss

    with pytest.raises(Schluss):
        asyncio.run(recordings.taeglich(konfiguration, schlafen=schlafen))

    assert laeufe == [recordings.SWEEP_ABSTAND, recordings.SWEEP_ABSTAND]


def test_die_ansage_wird_einmal_erzeugt_und_danach_wiederverwendet(tmp_path):
    laeufe = []

    def zaehlend(text, ziel):
        laeufe.append(text)
        spricht(text, ziel)

    erste = ansage.datei(tmp_path, sprecher=zaehlend)
    zweite = ansage.datei(tmp_path, sprecher=zaehlend)

    assert erste == zweite
    assert laeufe == [ansage.TEXT]
    assert ansage.kennung() in erste.name


def test_ein_anderer_wortlaut_ergibt_eine_andere_datei(tmp_path):
    erste = ansage.datei(tmp_path, sprecher=spricht)
    zweite = ansage.datei(tmp_path, text="Ein anderer Wortlaut.", sprecher=spricht)

    assert erste != zweite


def test_die_erzeugte_ansage_ist_was_discord_erwartet(tmp_path):
    with wave.open(str(ansage.datei(tmp_path, sprecher=spricht)), "rb") as datei:
        assert datei.getframerate() == ansage.RATE
        assert datei.getnchannels() == ansage.KANAELE
        assert datei.getsampwidth() == ansage.BREITE
        assert datei.getnframes() == ansage.RATE


def test_ohne_espeak_gibt_es_keine_ansage_und_damit_keine_aufnahme(tmp_path):
    def fehlt(text, ziel):
        raise FileNotFoundError("espeak-ng")

    with pytest.raises(AnsageFehlt):
        ansage.datei(tmp_path, sprecher=fehlt)
    assert list(tmp_path.glob("*.wav")) == []


def test_eine_halbe_spur_bleibt_nicht_als_fertige_ansage_liegen(tmp_path):
    def bricht_ab(text, ziel):
        spricht(text, ziel)
        raise OSError("abgebrochen")

    with pytest.raises(AnsageFehlt):
        ansage.datei(tmp_path, sprecher=bricht_ab)
    assert list(tmp_path.iterdir()) == []


def test_mono_wird_stereo_und_die_rate_verdoppelt_sich():
    quelle = array("h", [1, 2, 3, 4]).tobytes()

    umgerechnet = array("h", ansage.zu_discord(quelle, ansage.RATE // 2, 1))

    assert len(umgerechnet) == 16
    assert umgerechnet[:4].tolist() == [1, 1, 1, 1]


def test_aus_stereo_wird_nur_ein_kanal_gelesen():
    quelle = array("h", [7, -7, 9, -9]).tobytes()

    umgerechnet = array("h", ansage.zu_discord(quelle, ansage.RATE, 2))

    assert umgerechnet.tolist() == [7, 7, 9, 9]


# -- Der Ablauf ------------------------------------------------------------------------


def test_erst_ist_die_ansage_zu_ende_dann_beginnt_der_mitschnitt(
    konfiguration, sitzung_id, ohne_espeak
):
    stimme = FakeStimme()

    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme))

    assert stimme.ablauf == ["ansage", "mitschnitt"]
    assert stimme.aufnahme is aufnahme
    assert aufnahme.laeuft


def test_das_einwilligungsprotokoll_haelt_kanal_wortlaut_und_anwesende(
    konfiguration, sitzung_id, ohne_espeak
):
    asyncio.run(recorder.starten(konfiguration, FakeStimme()))

    (eintrag,) = consent.for_session(erste_runde(konfiguration), sitzung_id)
    assert eintrag.kind == consent.ANSAGE
    assert (eintrag.guild_id, eintrag.channel_id, eintrag.channel_name) == (
        KANAL.guild_id,
        KANAL.id,
        KANAL.name,
    )
    assert eintrag.text == ansage.TEXT
    assert eintrag.announced_at
    assert [(wer.id, wer.name) for wer in eintrag.members] == [
        (MIRA.id, MIRA.name),
        (BROK.id, BROK.name),
    ]


def test_vor_der_ansage_wird_keine_spur_geschrieben(konfiguration, sitzung_id):
    aufnahme = Aufnahme(konfiguration, erste_runde(konfiguration), sitzung_id, KANAL)

    with pytest.raises(NichtAngesagt):
        aufnahme.schreiben(MIRA, stille(10))

    assert not konfiguration.recordings_dir.exists()
    assert recordings.pending(erste_runde(konfiguration)) == ()


def test_ohne_sitzung_wird_nicht_einmal_angesagt(konfiguration, ohne_espeak):
    db.init(konfiguration.database_path)
    stimme = FakeStimme()

    with pytest.raises(recorder.AufnahmeFehler):
        asyncio.run(recorder.starten(konfiguration, stimme))

    assert stimme.ablauf == []


def test_je_sprecher_eine_eigene_spur(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(recorder.starten(konfiguration, FakeStimme()))

    aufnahme.schreiben(MIRA, stille(480))
    aufnahme.schreiben(BROK, stille(960))
    aufnahme.schreiben(MIRA, stille(480))
    meldungen = aufnahme.beenden()

    eingereiht = recordings.pending(erste_runde(konfiguration))
    assert len(eingereiht) == 2
    assert len(meldungen) == 2
    nach_name = {}
    for spur in eingereiht:
        assert str(sitzung_id) in spur.filename
        with wave.open(str(konfiguration.recordings_dir / spur.filename), "rb") as datei:
            nach_name[spur.filename.split("-")[-1]] = datei.getnframes()
    assert nach_name == {"Mira.wav": 960, "Brok.wav": 960}


def test_die_spur_traegt_die_discord_id_ihres_sprechers(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(recorder.starten(konfiguration, FakeStimme()))

    aufnahme.schreiben(MIRA, stille(480))
    aufnahme.beenden()

    (spur,) = recordings.pending(erste_runde(konfiguration))
    assert spur.discord_user_id == MIRA.id


def test_wer_nichts_gesagt_hat_hinterlaesst_keine_spur(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(recorder.starten(konfiguration, FakeStimme()))

    aufnahme.schreiben(MIRA, b"")

    assert aufnahme.beenden() == (recorder.NICHTS_GESPROCHEN,)
    assert list(konfiguration.recordings_dir.glob("sitzung*")) == []
    assert recordings.pending(erste_runde(konfiguration)) == ()


def test_ein_namenloser_sprecher_bekommt_seine_kennung(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(recorder.starten(konfiguration, FakeStimme()))

    aufnahme.schreiben(consent.Member(id="4009", name="漢字"), stille(48))
    aufnahme.beenden()

    (spur,) = recordings.pending(erste_runde(konfiguration))
    assert spur.filename.endswith("sprecher-4009.wav")


def test_der_nachzuegler_hoert_die_ansage_noch_einmal(konfiguration, sitzung_id, ohne_espeak):
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme))

    asyncio.run(recorder.nachzuegler(konfiguration, stimme, aufnahme, SPAET))

    assert stimme.ablauf == ["ansage", "mitschnitt", "ansage"]
    erste, spaet = consent.for_session(erste_runde(konfiguration), sitzung_id)
    assert erste.kind == consent.ANSAGE
    assert spaet.kind == consent.NACHZUEGLER
    assert [wer.name for wer in spaet.members] == [SPAET.name]
    assert spaet.text == ansage.TEXT


def test_stoppen_beendet_den_mitschnitt_trennt_und_reiht_ein(
    konfiguration, sitzung_id, ohne_espeak
):
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme))
    aufnahme.schreiben(MIRA, stille(480))

    meldungen = asyncio.run(recorder.stoppen(stimme, aufnahme))

    assert stimme.ablauf == ["ansage", "mitschnitt", "mitschnitt-ende", "getrennt"]
    assert not aufnahme.laeuft
    assert len(recordings.pending(erste_runde(konfiguration))) == 1
    assert "wartet auf den Stapel" in meldungen[0]


def test_die_einwilligung_ueberlebt_das_loeschen_ihrer_sitzung(
    konfiguration, sitzung_id, ohne_espeak
):
    asyncio.run(recorder.starten(konfiguration, FakeStimme()))

    verbindung = db.connect(konfiguration.database_path)
    try:
        with verbindung:
            verbindung.execute("DELETE FROM session WHERE id = ?", (sitzung_id,))
        zeile = verbindung.execute("SELECT session_id, text FROM consent_event").fetchone()
        anwesend = verbindung.execute("SELECT COUNT(*) FROM consent_member").fetchone()[0]
    finally:
        verbindung.close()

    assert zeile["session_id"] is None
    assert zeile["text"] == ansage.TEXT
    assert anwesend == 2


# -- Der Prozess -----------------------------------------------------------------------


def test_ohne_token_startet_der_bot_nicht(tmp_path, monkeypatch, capsys):
    leer = Config(data_dir=tmp_path)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: leer))

    def nie():
        raise AssertionError("ohne Token darf nichts verbinden")

    assert entry.main(gateway=nie) == 0
    assert entry.KEIN_TOKEN in capsys.readouterr().out


def test_der_token_aus_der_oberflaeche_schlaegt_die_umgebung(tmp_path, monkeypatch):
    aus_der_umgebung = Config(discord_bot_token="alt", data_dir=tmp_path)
    db.init(aus_der_umgebung.database_path)
    settings.save(erste_runde(aus_der_umgebung), {"discord_bot_token": TOKEN})
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: aus_der_umgebung))
    gesehen = []

    assert entry.main(gateway=lambda: gesehen.append) == 0
    assert gesehen[0].discord_bot_token == TOKEN


def test_eine_fehlende_bibliothek_wird_gesagt_statt_gestuerzt(tmp_path, monkeypatch, capsys):
    config = Config(discord_bot_token=TOKEN, data_dir=tmp_path)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))

    def stolpert():
        def run(_config):
            raise BotFehler(gateway.NICHT_INSTALLIERT)

        return run

    assert entry.main(gateway=stolpert) == 2
    ausgabe = capsys.readouterr().out
    assert "py-cord" in ausgabe
    assert TOKEN not in ausgabe


def test_ohne_pycord_bleibt_der_rest_importierbar(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", None)

    with pytest.raises(BotFehler):
        gateway._discord()


# -- Das Gateway -----------------------------------------------------------------------


class FakeIntents:
    @classmethod
    def none(cls):
        return cls()

    def __init__(self):
        self.guilds = False
        self.voice_states = False
        self.messages = False
        self.message_content = False


class FakeGruppe:
    def __init__(self, beschreibung):
        self.beschreibung = beschreibung
        self.befehle = {}

    def command(self, *, name, description=""):
        def nimm(funktion):
            self.befehle[name] = funktion
            return funktion

        return nimm


class FakeRechteFehlen(Exception):
    """Steht für ``discord.errors.PrivilegedIntentsRequired`` — Discord schließt mit 4014."""


class FakeBot:
    erzeugt: list[FakeBot] = []

    def __init__(self, intents=None):
        FakeBot.erzeugt.append(self)
        self.intents = intents
        self.gruppen = {}
        self.befehle = {}
        self.ereignisse = {}
        self.token = None

    def create_group(self, name, description=""):
        self.gruppen[name] = FakeGruppe(description)
        return self.gruppen[name]

    def slash_command(self, *, name, description=""):
        def nimm(funktion):
            self.befehle[name] = funktion
            return funktion

        return nimm

    def event(self, funktion):
        self.ereignisse[funktion.__name__] = funktion
        return funktion

    def run(self, token):
        self.token = token


class FakeSenke:
    def __init__(self, **rest):
        self.finished = False


class FakePCMAudio:
    def __init__(self, strom):
        self.strom = strom


class FakeMitglied:
    def __init__(self, kennung, name, *, bot=False, kanal=None):
        self.id = kennung
        self.display_name = name
        self.bot = bot
        self.voice = types.SimpleNamespace(channel=kanal)


class FakeGilde:
    def __init__(self, kennung=11):
        self.id = kennung
        self.mitglieder = {}

    def get_member(self, kennung):
        return self.mitglieder.get(kennung)


class FakeVoiceClient:
    def __init__(self, kanal):
        self.channel = kanal
        self.guild = kanal.guild
        self.gespielt = []
        self.senke = None
        self.schneidet = False
        self.getrennt = False

    def play(self, quelle, *, after):
        self.gespielt.append(quelle)
        after(None)

    def start_recording(self, senke, rueckruf):
        self.senke = senke
        self.schneidet = True

    def stop_recording(self):
        self.schneidet = False
        self.senke.cleanup()

    async def disconnect(self):
        self.getrennt = True


class FakeSprachkanal:
    def __init__(self, gilde, *mitglieder):
        self.guild = gilde
        self.id = 77
        self.name = "Runde"
        self.members = list(mitglieder)
        self.verbindung = None

    async def connect(self):
        self.verbindung = FakeVoiceClient(self)
        return self.verbindung


class FakeCtx:
    def __init__(self, autor):
        self.author = autor
        self.antworten = []
        self.aufgeschoben = False

    async def defer(self, **rest):
        self.aufgeschoben = True

    async def respond(self, text, **rest):
        self.antworten.append(text)


@pytest.fixture
def pycord(monkeypatch):
    modul = types.ModuleType("discord")
    modul.Intents = FakeIntents
    modul.Bot = FakeBot
    modul.PCMAudio = FakePCMAudio
    senken = types.ModuleType("discord.sinks")
    senken.Sink = FakeSenke
    modul.sinks = senken
    werkzeug = types.ModuleType("discord.utils")
    werkzeug.get_missing_voice_dependencies = lambda: ()
    modul.utils = werkzeug
    fehler = types.ModuleType("discord.errors")
    fehler.PrivilegedIntentsRequired = FakeRechteFehlen
    modul.errors = fehler
    monkeypatch.setitem(sys.modules, "discord", modul)
    monkeypatch.setattr(FakeBot, "erzeugt", [])
    return modul


@pytest.fixture
def runde(pycord):
    gilde = FakeGilde()
    mira = FakeMitglied(int(MIRA.id), MIRA.name)
    brok = FakeMitglied(int(BROK.id), BROK.name)
    chronist = FakeMitglied(999, "Chronik-Bot", bot=True)
    kanal = FakeSprachkanal(gilde, mira, brok, chronist)
    for wer in (mira, brok):
        gilde.mitglieder[wer.id] = wer
        wer.voice = types.SimpleNamespace(channel=kanal)
    return types.SimpleNamespace(gilde=gilde, kanal=kanal, mira=mira, brok=brok)


def befehl(bot, name):
    return bot.gruppen[gateway.GRUPPE].befehle[name]


def test_ohne_sprach_abhaengigkeiten_startet_der_bot_gar_nicht(konfiguration, pycord):
    # Genau der Fall, der auf der Box rot war: py-cord verbindet sich anstandslos und
    # schreibt eine Warnzeile, aber hören kann der Bot nichts. Das gehört an den Start,
    # nicht mitten in den Befehl.
    pycord.utils.get_missing_voice_dependencies = lambda: ("davey",)

    with pytest.raises(BotFehler) as fehler:
        gateway.baue(konfiguration)

    assert "davey" in str(fehler.value)
    assert FakeBot.erzeugt == []


def test_die_fehlende_inhalts_freigabe_wird_gesagt_statt_geworfen(
    konfiguration, pycord, monkeypatch
):
    # Auf der Box lief der Bot deswegen eine Nacht lang in die Neustartschleife — sichtbar
    # war nur ein Stapelauszug. Wer hier steht, soll den Schalter finden, nicht die Zeile.
    def verweigert(self, token):
        raise FakeRechteFehlen("Shard ID None is requesting privileged intents")

    monkeypatch.setattr(FakeBot, "run", verweigert, raising=False)

    with pytest.raises(BotFehler) as fehler:
        gateway.run(konfiguration)

    assert "Message Content Intent" in str(fehler.value)
    # Der Stapelauszug bleibt als Ursache erhalten, er steht nur nicht mehr im Vordergrund.
    assert isinstance(fehler.value.__cause__, FakeRechteFehlen)


def test_der_bot_bringt_beide_befehle_mit_und_bekommt_den_token(konfiguration, pycord):
    gateway.run(konfiguration)

    (bot,) = FakeBot.erzeugt
    assert set(bot.gruppen[gateway.GRUPPE].befehle) == {"start", "stop", "hilfe"}
    assert bot.intents.guilds and bot.intents.voice_states
    assert bot.token == TOKEN


def test_start_tritt_bei_sagt_an_und_schneidet_dann_mit(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    verbindung = runde.kanal.verbindung
    assert len(verbindung.gespielt) == 1
    assert verbindung.schneidet
    assert ctx.antworten == [recorder.GESTARTET]
    (eintrag,) = consent.for_session(erste_runde(konfiguration), sitzung_id)
    assert {wer.name for wer in eintrag.members} == {MIRA.name, BROK.name}


def test_start_ohne_sprachkanal_verbindet_nicht(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(FakeMitglied(4100, "Ohne Kanal"))

    asyncio.run(befehl(bot, "start")(ctx))

    assert ctx.antworten == [gateway.NICHT_IM_KANAL]
    assert runde.kanal.verbindung is None


def test_ein_zweiter_start_schneidet_nicht_doppelt(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    ctx = FakeCtx(runde.brok)

    asyncio.run(befehl(bot, "start")(ctx))

    assert ctx.antworten == [gateway.LAEUFT_SCHON]
    assert len(runde.kanal.verbindung.gespielt) == 1


def test_start_ohne_sitzung_trennt_wieder(konfiguration, ohne_espeak, runde):
    db.init(konfiguration.database_path)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    (antwort,) = ctx.antworten
    assert antwort.startswith("Das hat nicht geklappt:")
    assert recorder.OHNE_SITZUNG in antwort
    assert runde.kanal.verbindung.getrennt


def test_die_senke_schreibt_je_sprecher_eine_spur(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    senke = runde.kanal.verbindung.senke

    senke.write(sprachdaten(stille(480)), runde.mira)
    senke.write(sprachdaten(stille(480)), runde.brok)
    ctx = FakeCtx(runde.mira)
    asyncio.run(befehl(bot, "stop")(ctx))

    assert runde.kanal.verbindung.getrennt
    assert senke.finished
    spuren = {
        spur.filename.split("-")[-1] for spur in recordings.pending(erste_runde(konfiguration))
    }
    assert spuren == {"Mira.wav", "Brok.wav"}
    assert "wartet auf den Stapel" in ctx.antworten[0]


def test_stop_ohne_aufnahme_sagt_es(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "stop")(ctx))

    assert ctx.antworten == [gateway.LAEUFT_NICHT]


def test_wer_spaeter_dazukommt_hoert_die_ansage_noch_einmal(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    spaet = FakeMitglied(int(SPAET.id), SPAET.name)
    dazu = bot.ereignisse["on_voice_state_update"]

    asyncio.run(
        dazu(
            spaet,
            types.SimpleNamespace(channel=None),
            types.SimpleNamespace(channel=runde.kanal),
        )
    )

    assert len(runde.kanal.verbindung.gespielt) == 2
    _, nachzuegler = consent.for_session(erste_runde(konfiguration), sitzung_id)
    assert nachzuegler.kind == consent.NACHZUEGLER
    assert [wer.name for wer in nachzuegler.members] == [SPAET.name]


def test_ein_ortswechsel_im_selben_kanal_sagt_nichts_noch_einmal(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    dazu = bot.ereignisse["on_voice_state_update"]

    asyncio.run(
        dazu(
            runde.mira,
            types.SimpleNamespace(channel=runde.kanal),
            types.SimpleNamespace(channel=runde.kanal),
        )
    )

    assert len(runde.kanal.verbindung.gespielt) == 1
    assert len(consent.for_session(erste_runde(konfiguration), sitzung_id)) == 1


def test_der_bot_setzt_die_frist_selbst_durch(konfiguration, sitzung_id, runde, monkeypatch):
    gelaufen = []

    async def frist(config):
        gelaufen.append(config)

    monkeypatch.setattr(recordings, "taeglich", frist)
    bot = gateway.baue(konfiguration)

    async def anmelden():
        await bot.ereignisse["on_ready"]()
        await bot.ereignisse["on_ready"]()
        await asyncio.sleep(0)

    asyncio.run(anmelden())

    # Beim zweiten on_ready — Discord schickt es nach jedem Wiederverbinden — darf kein
    # zweiter Aufräumer danebenlaufen.
    assert gelaufen == [konfiguration]


def test_ohne_laufende_aufnahme_bleibt_der_beitritt_folgenlos(konfiguration, sitzung_id, runde):
    bot = gateway.baue(konfiguration)
    dazu = bot.ereignisse["on_voice_state_update"]

    asyncio.run(
        dazu(
            runde.mira,
            types.SimpleNamespace(channel=None),
            types.SimpleNamespace(channel=runde.kanal),
        )
    )

    assert consent.for_session(erste_runde(konfiguration), sitzung_id) == ()


def test_ein_stolpernder_befehl_antwortet_trotzdem(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    # Der Fehler von der Box: der Befehl stürzte ab und Discord zeigte ewig
    # »denkt nach …«. Schweigen ist der schlechteste Ausgang — mitten in der Runde weiß
    # dann niemand, ob aufgenommen wird.
    async def stolpert(*args, **kwargs):
        raise RuntimeError("irgendwas in der Bibliothek")

    monkeypatch.setattr(recorder, "starten", stolpert)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    (antwort,) = ctx.antworten
    assert antwort.startswith("Das hat nicht geklappt:")
    assert "RuntimeError" in antwort
    assert "Was du tun kannst" in antwort
    # Und der Bot hängt nicht stumm im Kanal herum.
    assert runde.kanal.verbindung.getrennt


def test_die_hilfe_erklaert_die_bedienung(konfiguration, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "hilfe")(ctx))

    (antwort,) = ctx.antworten
    for satzteil in ("/aufnahme start", "/aufnahme stop", "Ansage", "verlässt"):
        assert satzteil in antwort


def test_die_bestaetigung_sagt_das_wichtigste(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    (antwort,) = ctx.antworten
    for satzteil in ("Ansage", "eigene Spur", "verlässt den Sprachkanal", "/aufnahme stop"):
        assert satzteil in antwort


# -- Gegen das echte py-cord ------------------------------------------------------------
#
# Hier steht keine Attrappe. Zweimal hintereinander fiel erst auf der Box auf, dass wir
# das Protokoll der Bibliothek verfehlt hatten — beim zweiten Mal an genau der Zeile, die
# diese Tests jetzt ausführen.


class LeererReader:
    """Der Router legt den Reader nur ab; zum Registrieren braucht er nichts von ihm."""


def echte_senke(konfiguration, sitzung_id):
    aufnahme = Aufnahme(konfiguration, erste_runde(konfiguration), sitzung_id, KANAL)
    aufnahme.ansage_protokollieren((MIRA,))
    return aufnahme, gateway._senke(aufnahme)


def test_die_senke_laesst_sich_an_pycords_echtem_router_anmelden(konfiguration, sitzung_id):
    from discord.sinks import Sink
    from discord.voice.receive.router import PacketRouter, SinkEventRouter

    _, senke = echte_senke(konfiguration, sitzung_id)

    # Genau der Aufruf aus dem Absturzbericht: SinkEventRouter.__init__ registriert die
    # Ereignisse der Senke und griff dabei ins Leere.
    SinkEventRouter(senke, LeererReader())
    PacketRouter(senke, LeererReader())

    # start_recording lässt nur echte Sink-Ableitungen durch.
    assert isinstance(senke, Sink)
    assert senke.is_opus() is False


def test_die_alte_senke_ohne_das_protokoll_scheitert_weiter(konfiguration, sitzung_id):
    """Der rote Gegenbeweis — ohne ihn wäre der Test oben nicht zu unterscheiden.

    So sah unsere Senke vor diesem Fix aus: eine reine Ableitung der Basisklasse.
    """
    from discord.sinks import Sink
    from discord.voice.receive.router import SinkEventRouter

    class AlteSpurSenke(Sink):
        def write(self, data, user):
            pass

    with pytest.raises(AttributeError, match="__sink_listeners__"):
        SinkEventRouter(AlteSpurSenke(), LeererReader())


def test_auch_pycords_eigene_senke_scheitert_daran(konfiguration, sitzung_id):
    """Warum das Protokoll hier von Hand steht statt geerbt zu werden.

    In py-cord 2.8.1 erfüllt **keine** mitgelieferte Senke die Erwartung des neuen
    Empfangs-Routers — auch ``WaveSink`` nicht. Es gibt also keine Basisklasse, von der
    man das erben könnte; wer diesen Test rot sieht, darf den Handbetrieb wegräumen.
    """
    from discord.sinks import WaveSink
    from discord.voice.receive.router import SinkEventRouter

    with pytest.raises(AttributeError, match="__sink_listeners__"):
        SinkEventRouter(WaveSink(), LeererReader())


def test_pycords_datenform_landet_in_der_spur_des_sprechers(konfiguration, sitzung_id):
    from discord.voice.packets import VoiceData

    aufnahme, senke = echte_senke(konfiguration, sitzung_id)
    sprecher = types.SimpleNamespace(id=int(MIRA.id), display_name=MIRA.name)
    daten = VoiceData(packet=None, source=sprecher, pcm=stille(480))

    # So ruft PacketRouter die Senke: write(data, data.source).
    senke.write(daten, daten.source)
    aufnahme.beenden()

    (spur,) = recordings.pending(erste_runde(konfiguration))
    assert spur.filename.endswith(f"{MIRA.name}.wav")


def test_ein_sprecher_ohne_konto_bekommt_eine_ehrlich_benannte_spur(konfiguration, sitzung_id):
    from discord.voice.packets import VoiceData

    aufnahme, senke = echte_senke(konfiguration, sitzung_id)

    senke.write(VoiceData(packet=None, source=None, pcm=stille(480)), None)
    aufnahme.beenden()

    (spur,) = recordings.pending(erste_runde(konfiguration))
    assert spur.filename.endswith(f"{gateway.UNBEKANNT}.wav")
