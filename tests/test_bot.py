"""Der Aufnahme-Bot — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Kein Test spricht mit discord.com, und keiner braucht die Sprach-Abhängigkeiten: das
Gegenüber sind ein paar Dutzend Zeilen Attrappe. Der Token in diesen Tests ist erfunden
und steht nur hier.

Die beiden Sätze, die dieser Suite ihren Sinn geben: **vor der Ansage wird nichts
geschrieben**, und **was angesagt wurde, steht im Wortlaut im Protokoll.**
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
import wave
from array import array
from pathlib import Path

import pytest
from conftest import runde as erste_runde

import chronicle.bot.__main__ as entry
from chronicle import consent, db, jobs, lebenszyklus, notes, recordings, settings
from chronicle import runde as runden
from chronicle.bot import BotFehler, BotHaelt, ansage, chronik, gateway, recorder
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


def unsere_runde(konfiguration):
    """Die Runde der Gilde, in der der Bot steht — angelegt beim ersten Blick.

    Einen Rückfall auf »die erste Runde« gibt es nicht mehr (#68): eine Gilde ohne eigene
    Runde nimmt nicht auf. Neben ihr steht in jeder dieser Datenbanken noch die erste
    Runde, die das Schema anlegt — genau deshalb wäre ein Rückfall hier nicht harmlos.
    """
    db.init(konfiguration.database_path)
    vorhanden = runden.fuer_gilde(konfiguration.database_path, KANAL.guild_id)
    if vorhanden is not None:
        return vorhanden
    return runden.anlegen(konfiguration.database_path, "Der Krumme Ast", guild_id=KANAL.guild_id)


@pytest.fixture
def sitzung_id(konfiguration):
    return notes.create_session(unsere_runde(konfiguration), played_on="2026-08-06")


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

    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))

    assert stimme.ablauf == ["ansage", "mitschnitt"]
    assert stimme.aufnahme is aufnahme
    assert aufnahme.laeuft


def test_das_einwilligungsprotokoll_haelt_kanal_wortlaut_und_anwesende(
    konfiguration, sitzung_id, ohne_espeak
):
    asyncio.run(recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration)))

    (eintrag,) = consent.for_session(unsere_runde(konfiguration), sitzung_id)
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
    aufnahme = Aufnahme(konfiguration, unsere_runde(konfiguration), sitzung_id, KANAL)

    with pytest.raises(NichtAngesagt):
        aufnahme.schreiben(MIRA, stille(10))

    assert not konfiguration.recordings_dir.exists()
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_ohne_sitzung_wird_nicht_einmal_angesagt(konfiguration, ohne_espeak):
    db.init(konfiguration.database_path)
    stimme = FakeStimme()

    with pytest.raises(recorder.AufnahmeFehler):
        asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))

    assert stimme.ablauf == []


def test_die_runde_einer_fremden_gilde_schneidet_hier_nicht_mit(
    konfiguration, sitzung_id, ohne_espeak
):
    """Es gibt keinen Rückfall auf »die erste Runde« mehr (#68): Ansage, Protokoll mit den
    Anzeigenamen und die Spuren landeten sonst in einer fremden Kampagne."""
    fremde = erste_runde(konfiguration)
    stimme = FakeStimme()

    with pytest.raises(recorder.AufnahmeFehler) as gefangen:
        asyncio.run(recorder.starten(konfiguration, stimme, fremde))

    assert recorder.FREMDE_RUNDE in str(gefangen.value)
    assert stimme.ablauf == []
    assert consent.for_session(fremde, sitzung_id) == ()


def test_je_sprecher_eine_eigene_spur(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration))
    )

    aufnahme.schreiben(MIRA, stille(480))
    aufnahme.schreiben(BROK, stille(960))
    aufnahme.schreiben(MIRA, stille(480))
    meldungen = aufnahme.beenden()

    eingereiht = recordings.pending(unsere_runde(konfiguration))
    assert len(eingereiht) == 2
    assert len(meldungen) == 2
    nach_name = {}
    for spur in eingereiht:
        assert str(sitzung_id) in spur.filename
        with wave.open(str(konfiguration.recordings_dir / spur.filename), "rb") as datei:
            nach_name[spur.filename.split("-")[-1]] = datei.getnframes()
    assert nach_name == {"Mira.wav": 960, "Brok.wav": 960}


def test_die_spur_traegt_die_discord_id_ihres_sprechers(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration))
    )

    aufnahme.schreiben(MIRA, stille(480))
    aufnahme.beenden()

    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.discord_user_id == MIRA.id


def test_wer_nichts_gesagt_hat_hinterlaesst_keine_spur(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration))
    )

    aufnahme.schreiben(MIRA, b"")

    assert aufnahme.beenden() == (recorder.NICHTS_GESPROCHEN,)
    assert list(konfiguration.recordings_dir.glob("sitzung*")) == []
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_ein_namenloser_sprecher_bekommt_seine_kennung(konfiguration, sitzung_id, ohne_espeak):
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration))
    )

    aufnahme.schreiben(consent.Member(id="4009", name="漢字"), stille(48))
    aufnahme.beenden()

    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("sprecher-4009.wav")


def test_der_nachzuegler_hoert_die_ansage_noch_einmal(konfiguration, sitzung_id, ohne_espeak):
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))

    asyncio.run(recorder.nachzuegler(konfiguration, stimme, aufnahme, SPAET))

    assert stimme.ablauf == ["ansage", "mitschnitt", "ansage"]
    erste, spaet = consent.for_session(unsere_runde(konfiguration), sitzung_id)
    assert erste.kind == consent.ANSAGE
    assert spaet.kind == consent.NACHZUEGLER
    assert [wer.name for wer in spaet.members] == [SPAET.name]
    assert spaet.text == ansage.TEXT


def test_stoppen_beendet_den_mitschnitt_trennt_und_reiht_ein(
    konfiguration, sitzung_id, ohne_espeak
):
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))
    aufnahme.schreiben(MIRA, stille(480))

    meldungen = asyncio.run(recorder.stoppen(stimme, aufnahme))

    assert stimme.ablauf == ["ansage", "mitschnitt", "mitschnitt-ende", "getrennt"]
    assert not aufnahme.laeuft
    assert len(recordings.pending(unsere_runde(konfiguration))) == 1
    assert "wartet auf den Stapel" in meldungen[0]


def sprecher(spur) -> str:
    """Der Name aus dem Spurdateinamen — ``sitzung1-20260811T…-Mira.wav`` → ``Mira``."""
    return Path(spur.filename).stem.split("-")[-1]


class StolpertBeimEinreihen:
    """``recordings.enqueue``, das bei der n-ten Spur einmal ausfällt — und dann nicht mehr."""

    def __init__(self, nummer: int):
        self.nummer = nummer
        self.versuche = 0
        self._echt = recordings.enqueue

    def __call__(self, *args, **kwargs):
        self.versuche += 1
        if self.versuche == self.nummer:
            raise sqlite3.OperationalError("database is locked")
        return self._echt(*args, **kwargs)


def test_eine_gescheiterte_spur_haelt_die_uebrigen_nicht_auf(
    konfiguration, sitzung_id, ohne_espeak, monkeypatch
):
    """Eine Spur ohne Zeile ist für ``sweep`` unsichtbar: nicht verschriftet, nicht
    gelöscht. Sie darf weder die übrigen aufhalten noch still verschwinden (#104)."""
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA, BROK, SPAET), unsere_runde(konfiguration))
    )
    for wer in (MIRA, BROK, SPAET):
        aufnahme.schreiben(wer, stille(480))
    monkeypatch.setattr(recordings, "enqueue", StolpertBeimEinreihen(2))

    with pytest.raises(recorder.AufnahmeFehler) as gefangen:
        aufnahme.beenden()

    eingereiht = [spur.filename for spur in recordings.pending(unsere_runde(konfiguration))]
    assert len(eingereiht) == 2
    assert not any(name.endswith("Brok.wav") for name in eingereiht)
    assert "Brok" in str(gefangen.value)
    assert list(konfiguration.recordings_dir.glob("*Brok.wav")) != []


def test_der_zweite_anlauf_holt_die_liegengebliebene_spur_nach(
    konfiguration, sitzung_id, ohne_espeak, monkeypatch
):
    """Der Repro aus #104: was schon drin ist, wird übersprungen statt erneut versucht.

    Ohne das Überspringen fiele der zweite Anlauf über die UNIQUE-Bedingung der ersten
    Spur, und die dahinter kämen nie in die Warteschlange.
    """
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA, BROK, SPAET), unsere_runde(konfiguration))
    )
    for wer in (MIRA, BROK, SPAET):
        aufnahme.schreiben(wer, stille(480))
    monkeypatch.setattr(recordings, "enqueue", StolpertBeimEinreihen(2))
    with pytest.raises(recorder.AufnahmeFehler):
        aufnahme.beenden()

    meldungen = aufnahme.beenden()

    assert sorted(sprecher(spur) for spur in recordings.pending(unsere_runde(konfiguration))) == [
        "Aelin",
        "Brok",
        "Mira",
    ]
    assert len(meldungen) == 3
    assert all("wartet auf den Stapel" in meldung for meldung in meldungen)


def test_eine_aufnahme_schreibt_nicht_in_die_runde_von_nachher(
    konfiguration, sitzung_id, ohne_espeak
):
    """Eine Aufnahme läuft Stunden und hält ihre Runde. Wird sie in der Zeit gelöscht und
    ihre Kennung neu vergeben, gingen Einwilligungsprotokoll und Tonspuren dieser Gruppe in
    die Kampagne einer fremden — deshalb wird vor jedem Schreiben nachgesehen."""
    unsere = unsere_runde(konfiguration)
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere))
    aufnahme.schreiben(MIRA, stille(480))

    lebenszyklus.loeschen(konfiguration, unsere)
    frisch = runden.anlegen(konfiguration.database_path, "Fremde", guild_id=KANAL.guild_id)
    assert frisch.id == unsere.id

    with pytest.raises(recorder.AufnahmeFehler):
        asyncio.run(recorder.nachzuegler(konfiguration, stimme, aufnahme, SPAET))
    meldungen = aufnahme.beenden()

    assert meldungen == (recorder.RUNDE_FORT,)
    assert recordings.pending(frisch) == ()
    assert consent.for_session(frisch, sitzung_id) == ()
    assert list(konfiguration.recordings_dir.glob("**/Mira.wav")) == []


def test_die_einwilligung_ueberlebt_das_loeschen_ihrer_sitzung(
    konfiguration, sitzung_id, ohne_espeak
):
    asyncio.run(recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration)))

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


class FakePermissions:
    """``discord.Permissions`` — hier nur der Merkzettel, welches Recht verlangt wird."""

    def __init__(self, **rechte):
        self.rechte = rechte


class FakeRechte:
    """Was Discord über ein Mitglied sagt: ``Member.guild_permissions``."""

    def __init__(self, *, manage_guild=False, administrator=False):
        self.manage_guild = manage_guild
        self.administrator = administrator


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


class FakeTokenAbgelehnt(Exception):
    """Steht für ``discord.errors.LoginFailure`` — der Token taugt nicht (mehr)."""


class FakeBot:
    erzeugt: list[FakeBot] = []

    def __init__(self, intents=None):
        FakeBot.erzeugt.append(self)
        self.intents = intents
        self.gruppen = {}
        self.befehle = {}
        self.rechte = {}
        self.ereignisse = {}
        self.kanaele = {}
        self.token = None

    def get_channel(self, kennung):
        return self.kanaele.get(kennung)

    def create_group(self, name, description=""):
        self.gruppen[name] = FakeGruppe(description)
        return self.gruppen[name]

    def slash_command(self, *, name, description="", default_member_permissions=None):
        self.rechte[name] = default_member_permissions

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
        self.trennen_stolpert = False

    def play(self, quelle, *, after):
        self.gespielt.append(quelle)
        after(None)

    def start_recording(self, senke, rueckruf):
        self.senke = senke
        self.schneidet = True

    def is_recording(self):
        return self.schneidet

    def stop_recording(self):
        # So verhält sich py-cord: der zweite Beender bekommt eine RecordingException.
        if not self.schneidet:
            raise RuntimeError("You are not recording")
        self.schneidet = False
        self.senke.cleanup()

    async def disconnect(self):
        # Das echte Trennen geht ans Netz und damit an die Schleife ab — genau hier war
        # die Lücke, durch die ein zweiter Beender kam.
        await asyncio.sleep(0)
        if self.trennen_stolpert:
            raise RuntimeError("Verbindung weg")
        self.getrennt = True


class FakeSprachkanalOhneChat:
    """Ein Sprachkanal ohne eigenen Chat — Discord kennt solche noch."""

    def __init__(self, gilde, *mitglieder):
        self.guild = gilde
        self.id = 77
        self.name = "Runde"
        self.members = list(mitglieder)
        self.verbindung = None

    async def connect(self):
        self.verbindung = FakeVoiceClient(self)
        return self.verbindung


class FakeSprachkanal(FakeSprachkanalOhneChat):
    def __init__(self, gilde, *mitglieder):
        super().__init__(gilde, *mitglieder)
        self.geschrieben = []

    async def send(self, text):
        # Mitgeschrieben wird auch, wie weit die Ansage war: die Reihenfolge ist der Punkt.
        gespielt = len(self.verbindung.gespielt) if self.verbindung else 0
        self.geschrieben.append((text, gespielt))


class FakeTextkanal:
    def __init__(self):
        self.geschrieben = []

    async def send(self, text):
        self.geschrieben.append(text)


class FakeCtx:
    def __init__(self, autor, kanal=None, *, guild_id=KANAL.guild_id):
        self.author = autor
        self.channel = kanal if kanal is not None else FakeTextkanal()
        self.guild_id = guild_id
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
    modul.Permissions = FakePermissions
    modul.PCMAudio = FakePCMAudio
    senken = types.ModuleType("discord.sinks")
    senken.Sink = FakeSenke
    modul.sinks = senken
    werkzeug = types.ModuleType("discord.utils")
    werkzeug.get_missing_voice_dependencies = lambda: ()
    modul.utils = werkzeug
    fehler = types.ModuleType("discord.errors")
    fehler.PrivilegedIntentsRequired = FakeRechteFehlen
    fehler.LoginFailure = FakeTokenAbgelehnt
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

    with pytest.raises(BotHaelt) as fehler:
        gateway.run(konfiguration)

    assert "Message Content Intent" in str(fehler.value)
    # Der Stapelauszug bleibt als Ursache erhalten, er steht nur nicht mehr im Vordergrund.
    assert isinstance(fehler.value.__cause__, FakeRechteFehlen)


def test_ein_abgelehnter_token_wird_nicht_wieder_versucht(konfiguration, pycord, monkeypatch):
    def verweigert(self, token):
        raise FakeTokenAbgelehnt("Improper token has been passed.")

    monkeypatch.setattr(FakeBot, "run", verweigert, raising=False)

    with pytest.raises(BotHaelt) as fehler:
        gateway.run(konfiguration)

    assert "Reset Token" in str(fehler.value)


def test_was_kein_neustart_heilt_endet_mit_null(konfiguration, monkeypatch, capsys):
    """Der Kern des Vorfalls vom 2026-08-10: 0 heißt »liegen lassen«, nicht »noch mal«.

    Mit einem Fehlschlag-Code hätte ``Restart=on-failure`` den Bot in dieselbe Wand
    geschickt — tausendfach in Minuten, bis Discord den Token zurücksetzte.
    """
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: konfiguration))

    def haelt():
        def run(_zugang):
            raise BotHaelt(gateway.RECHTE_FEHLEN)

        return run

    assert entry.main([], gateway=haelt) == 0
    assert "Message Content Intent" in capsys.readouterr().out


def test_eine_echte_stoerung_darf_weiter_scheitern(konfiguration, monkeypatch, capsys):
    """Was ein Neustart heilen kann, endet weiterhin mit 2 — sonst bliebe echtes Pech liegen."""
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: konfiguration))

    def stoert():
        def run(_zugang):
            raise BotFehler("Netz weg")

        return run

    assert entry.main([], gateway=stoert) == 2


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
    (eintrag,) = consent.for_session(unsere_runde(konfiguration), sitzung_id)
    assert {wer.name for wer in eintrag.members} == {MIRA.name, BROK.name}


def test_die_vorstellung_steht_im_kanal_bevor_die_ansage_laeuft(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    # Der Ausweg muss lesbar sein, bevor gesprochen — und erst recht, bevor mitgeschnitten
    # wird. Die Null ist der Beleg: beim Schreiben lief noch keine Ansage.
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert runde.kanal.geschrieben == [(gateway.VORSTELLUNG, 0)]
    assert len(runde.kanal.verbindung.gespielt) == 1


def test_die_vorstellung_sagt_frist_und_befehle_aus_einer_quelle():
    assert f"{recordings.RETENTION_TAGE} Tagen" in gateway.VORSTELLUNG
    # Ein Text, zwei Anlässe: die Liste steht nicht zweimal da.
    assert gateway.BEFEHLE in gateway.VORSTELLUNG
    assert gateway.BEFEHLE in gateway.HILFE
    for satzteil in ("hörbare Ansage", "verlässt jetzt", "nichts auf"):
        assert satzteil in gateway.VORSTELLUNG
    # Discord nimmt keine längere Nachricht an — und eine, die nicht ankommt, verhindert
    # die Aufnahme, statt sie nur zu begleiten.
    assert len(gateway.VORSTELLUNG) <= 2000


def test_ohne_kanal_chat_geht_die_vorstellung_dorthin_wo_der_befehl_kam(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    kanal = FakeSprachkanalOhneChat(runde.gilde, runde.mira, runde.brok)
    runde.mira.voice = types.SimpleNamespace(channel=kanal)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    assert ctx.channel.geschrieben == [gateway.VORSTELLUNG]
    assert kanal.verbindung.schneidet


def test_ohne_eigene_runde_nimmt_die_gilde_nicht_auf(konfiguration, ohne_espeak, runde):
    """Dieselbe Absage wie vor ``/chronik start``, und vor dem Beitreten in den Kanal."""
    db.init(konfiguration.database_path)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_RUNDE in antwort
    assert runde.kanal.verbindung is None


def test_die_ruhende_runde_nimmt_nichts_mehr_auf(konfiguration, sitzung_id, ohne_espeak, runde):
    lebenszyklus.sperren(konfiguration.database_path, KANAL.guild_id)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    (antwort,) = ctx.antworten
    assert "Diese Runde ruht" in antwort
    assert runde.kanal.verbindung is None


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
    unsere_runde(konfiguration)
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
        spur.filename.split("-")[-1] for spur in recordings.pending(unsere_runde(konfiguration))
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
    _, nachzuegler = consent.for_session(unsere_runde(konfiguration), sitzung_id)
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
    assert len(consent.for_session(unsere_runde(konfiguration), sitzung_id)) == 1


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

    assert consent.for_session(unsere_runde(konfiguration), sitzung_id) == ()


THREAD = 88


@pytest.fixture
def sitzung_im_thread(konfiguration):
    return notes.create_session(unsere_runde(konfiguration), thread_id=str(THREAD))


def zustand(kanal=None):
    """Ein ``VoiceState``, so weit der Bot ihn liest: nur der Kanal."""
    return types.SimpleNamespace(channel=kanal)


async def ruhen():
    """Bis die Fäden durch sind, die die Frist des leeren Kanals abwarten."""
    # ``return_exceptions``, weil ein abbestellter Wächter der Normalfall ist: wer den
    # Mitschnitt beendet, bricht ihn ab, und ein abgebrochener Faden risse sonst dieses
    # Warten mit sich.
    await asyncio.gather(
        *(faden for faden in asyncio.all_tasks() if faden is not asyncio.current_task()),
        return_exceptions=True,
    )


# Eine echte, kurze Frist statt null: mit null prüfte kein Test je, dass die Frist
# überhaupt gewartet und beim erneuten Gehen neu gestellt wird.
FRIST = 0.2


@pytest.fixture
def kurze_frist(monkeypatch):
    monkeypatch.setattr(gateway, "LEER_FRIST", FRIST)
    return FRIST


def nur_der_bot(kanal):
    """Alle gehen — bis auf den Bot, den ``mitglieder`` ohnehin nicht mitzählt."""
    anwesend = list(kanal.members)
    kanal.members = [wer for wer in anwesend if wer.bot]
    return anwesend


def alle_gehen(bot, runde):
    """Mitschneiden, jemand spricht, dann leert sich der Kanal bis auf den Bot."""

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        nur_der_bot(runde.kanal)
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())


def test_ist_niemand_mehr_da_hoert_der_bot_von_selbst_auf(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Die Einwilligung galt dieser Runde, nicht dem, was danach im leeren Kanal fällt."""
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread

    alle_gehen(bot, runde)

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")
    (gesagt,) = thread.geschrieben
    assert gesagt.startswith(gateway.LEER_BEENDET)
    assert "wartet auf den Stapel" in gesagt


def test_der_leere_kanal_beendet_den_mitschnitt_und_nicht_die_sitzung(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Ausdrücklich offen gelassen und ausdrücklich entschieden: der Thread bleibt offen.

    ``/chronik fertig`` verlangt ein Passwort und ist damit eine Entscheidung von Hand —
    niemand gibt es ein, der schon gegangen ist.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()

    alle_gehen(bot, runde)

    unsere = unsere_runde(konfiguration)
    assert chronik.sitzung_des_threads(unsere, str(THREAD)) == sitzung_im_thread
    assert jobs.latest(unsere, jobs.CHRONIK) is None
    assert "`/chronik fertig`" in bot.kanaele[THREAD].geschrieben[0]


def test_wer_in_der_frist_wiederkommt_findet_seine_aufnahme_vor(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Der Verbindungsabriss dauert wirklich — hier vergeht Zeit, keine null Sekunden."""
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        anwesend = nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        # Die Verbindung war weg, nicht die Runde — und das noch vor Ablauf der Frist.
        await asyncio.sleep(FRIST / 2)
        runde.kanal.members = anwesend
        await dazu(runde.mira, zustand(), zustand(runde.kanal))
        # Über die Weckzeit des alten Wächters hinaus: der darf nichts mehr abreißen.
        await asyncio.sleep(FRIST)
        await ruhen()

    asyncio.run(ablauf())

    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt
    assert thread.geschrieben == []
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_wer_wieder_geht_bekommt_die_ganze_frist_noch_einmal(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Sonst wären aus neunzig Sekunden Karenz eine halbe geworden.

    Der Wächter des ersten Gehens weckt zu seiner Zeit, nicht zu der des zweiten. Wird er
    beim erneuten Gehen nicht ersetzt, zählt die Frist ab dem ersten — und wer bei T=89
    zurückkommt und bei T=89,5 die Verbindung wieder verliert, findet die Sitzung
    geschnitten vor.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        anwesend = nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await asyncio.sleep(FRIST * 0.8)
        runde.kanal.members = anwesend
        await dazu(runde.mira, zustand(), zustand(runde.kanal))
        nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        # Jetzt ist die Weckzeit des ersten Wächters lange vorbei, die des zweiten nicht.
        await asyncio.sleep(FRIST * 0.5)
        noch_dabei = runde.kanal.verbindung.schneidet
        await ruhen()
        return noch_dabei

    assert asyncio.run(ablauf())
    # Und danach greift das Netz trotzdem — neu gestellt heißt nicht abbestellt.
    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt


def test_der_waechter_der_alten_aufnahme_verdraengt_den_der_neuen_nicht(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Genau der Zustand, gegen den dieser Wächter gebaut ist: ein leerer Kanal läuft weiter.

    Aufnahme X endet von Hand, während ihr Wächter noch schläft. Beginnt Y innerhalb der
    Frist und leert sich auch, muss Y einen **eigenen** Wächter bekommen — sonst schneidet
    Y unbegrenzt einen leeren Kanal mit, und ein zweites Ereignis kommt nicht mehr.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        anwesend = nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await befehl(bot, "stop")(FakeCtx(runde.mira))
        # Y beginnt innerhalb der Frist von X — deren Wächter schläft noch.
        runde.kanal.members = anwesend
        await befehl(bot, "start")(FakeCtx(runde.mira))
        zweite = runde.kanal.verbindung
        nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await ruhen()
        return zweite

    zweite = asyncio.run(ablauf())

    assert not zweite.schneidet
    assert zweite.getrennt
    assert thread.geschrieben[-1].startswith(gateway.LEER_BEENDET)


def test_verschoben_zaehlt_weiter_der_kanal_der_aufnahme(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """»Gegangen« und »leer« müssen denselben Kanal meinen.

    py-cord führt ``VoiceClient.channel`` nach, wenn ein Admin den Bot verschiebt. Zählte
    die Leere dort, entschiede sie über eine fremde Besetzung: der Ursprungskanal leert
    sich, nebenan sitzen Leute, und das Netz griffe nie.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        nachbar = FakeSprachkanal(runde.gilde, runde.mira, runde.brok)
        nachbar.id = 78
        runde.kanal.verbindung.channel = nachbar
        nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt


def test_der_gescheiterte_abschied_sagt_es_statt_zu_verschwinden(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist, monkeypatch
):
    """Ein Faden nebenher hat niemanden, dem er antwortet — also sagt er es im Thread."""

    async def stolpert(*args, **kwargs):
        raise RuntimeError("You are not recording")

    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    monkeypatch.setattr(recorder, "stoppen", stolpert)

    alle_gehen(bot, runde)

    assert thread.geschrieben == [gateway.LEER_GESCHEITERT]


def test_eine_misslungene_ansage_macht_aus_dem_ende_keinen_fehlschlag(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """``LEER_GESCHEITERT`` gehört dem gescheiterten Beenden, nicht der stummen Ansage.

    Umfasste ein ``try`` beides, schriebe ein zuckendes ``thread.send`` »die Aufnahme gilt
    weiter als laufend, gib `/aufnahme stop`« in den Thread — während der Lauf beendet, der
    Bot getrennt und die Spuren eingereiht sind und ``/aufnahme stop`` »keine Aufnahme«
    antwortet. Genau die falsche Anweisung, gegen die dieser Wächter gebaut ist.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    ungestoert = thread.send

    async def zuckt(text):
        if text.startswith(gateway.LEER_BEENDET):
            raise RuntimeError("thread.send zuckt")
        await ungestoert(text)

    thread.send = zuckt

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        nur_der_bot(runde.kanal)
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(runde.kanal), zustand())
        await ruhen()
        ctx = FakeCtx(runde.mira)
        await befehl(bot, "stop")(ctx)
        return ctx

    ctx = asyncio.run(ablauf())

    assert runde.kanal.verbindung.getrennt
    assert not runde.kanal.verbindung.schneidet
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")
    # Der Lauf ist zu Ende — und die Meldung, die das Gegenteil behauptet, blieb aus.
    assert gateway.LAEUFT_NICHT in ctx.antworten
    assert thread.geschrieben == []


def test_nach_dem_gescheiterten_abschied_stellt_erst_ein_neuer_gang_den_waechter(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Die Rücknahme gibt den Lauf zurück, den abbestellten Wächter aber nicht.

    Das ist entschieden und nicht vergessen: einen neuen zu stellen hieße, bei bleibendem
    Fehler alle neunzig Sekunden denselben Fehlschlag in den Thread zu schreiben. Also
    sagt ``LEER_GESCHEITERT``, was gilt — von selbst geschieht nichts mehr, und erst wer
    den Kanal betritt und wieder verlässt, bestellt einen neuen Wächter.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        verbindung = runde.kanal.verbindung
        verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        verbindung.trennen_stolpert = True
        anwesend = nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await ruhen()
        assert thread.geschrieben == [gateway.LEER_GESCHEITERT]
        # Mehrere Fristen lang von selbst: kein zweiter Versuch, kein zweiter Satz.
        await asyncio.sleep(FRIST * 3)
        assert thread.geschrieben == [gateway.LEER_GESCHEITERT]
        assert not verbindung.getrennt
        # Und jetzt das, was die Meldung ankündigt: betreten, verlassen, Wächter steht.
        runde.kanal.members = anwesend
        await dazu(runde.mira, zustand(), zustand(runde.kanal))
        verbindung.trennen_stolpert = False
        nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert runde.kanal.verbindung.getrennt
    assert not runde.kanal.verbindung.schneidet
    assert thread.geschrieben[-1].startswith(gateway.LEER_BEENDET)


def test_nach_gescheitertem_beenden_greift_aufnahme_stop_noch(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Der Anspruch vor dem Abgeben darf den Fehlerpfad nicht unrettbar machen.

    Ohne Rücknahme wäre der Lauf nach einem gescheiterten Trennen geleert: der Bot bliebe
    im Kanal, die Spuren lägen uneingereiht, und ``/aufnahme stop`` antwortete ab da immer
    »keine Aufnahme« — genau der Zustand, gegen den dieser Wächter gebaut ist, nur ohne
    jeden Befehl, der ihn beendet. Der Satz im Thread verspricht das Gegenteil; hier wird
    das Versprechen eingelöst.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        verbindung = runde.kanal.verbindung
        verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        verbindung.trennen_stolpert = True
        nur_der_bot(runde.kanal)
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(runde.kanal), zustand())
        await ruhen()
        assert not verbindung.getrennt
        # Und jetzt das, wozu die Meldung im Thread auffordert.
        verbindung.trennen_stolpert = False
        ctx = FakeCtx(runde.mira)
        await befehl(bot, "stop")(ctx)
        return ctx

    ctx = asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.LEER_GESCHEITERT]
    assert gateway.LAEUFT_NICHT not in ctx.antworten
    assert not any(antwort.startswith("Das hat nicht geklappt") for antwort in ctx.antworten)
    assert runde.kanal.verbindung.getrennt
    assert not runde.kanal.verbindung.schneidet
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")


def test_ein_zweites_aufnahme_stop_holt_die_liegengebliebene_spur_nach(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, monkeypatch
):
    """Der Repro aus #104, so wie er gemeldet wurde: ``/aufnahme stop`` ein zweites Mal.

    Vorher scheiterte der zweite Anlauf an der UNIQUE-Bedingung der schon eingereihten
    ersten Spur — und die zweite blieb für immer als Datei ohne Zeile liegen: weder
    verschriftet noch nach Frist gelöscht.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        senke = runde.kanal.verbindung.senke
        senke.write(sprachdaten(stille(480)), runde.mira)
        senke.write(sprachdaten(stille(480)), runde.brok)
        monkeypatch.setattr(recordings, "enqueue", StolpertBeimEinreihen(2))
        erster, zweiter = FakeCtx(runde.mira), FakeCtx(runde.mira)
        await befehl(bot, "stop")(erster)
        await befehl(bot, "stop")(zweiter)
        return erster, zweiter

    erster, zweiter = asyncio.run(ablauf())

    assert erster.antworten[0].startswith("Das hat nicht geklappt")
    assert BROK.name in erster.antworten[0]
    assert gateway.LAEUFT_NICHT not in zweiter.antworten
    assert not any(antwort.startswith("Das hat nicht geklappt") for antwort in zweiter.antworten)
    assert sorted(sprecher(spur) for spur in recordings.pending(unsere_runde(konfiguration))) == [
        BROK.name,
        MIRA.name,
    ]


def test_zwei_beender_zugleich_geben_dem_zweiten_keine_leere_antwort(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """``recorder.stoppen`` gibt beim Trennen ab; in der Lücke kam bisher ein zweiter durch.

    Der zweite bekam von py-cord »You are not recording« zu hören und die Runde ein »das
    hat nicht geklappt« für einen Stopp, der geklappt hat.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        erster, zweiter = FakeCtx(runde.mira), FakeCtx(runde.mira)
        await asyncio.gather(befehl(bot, "stop")(erster), befehl(bot, "stop")(zweiter))
        return erster, zweiter

    erster, zweiter = asyncio.run(ablauf())

    antworten = erster.antworten + zweiter.antworten
    assert len(antworten) == 2
    assert not any(antwort.startswith("Das hat nicht geklappt") for antwort in antworten)
    assert gateway.LAEUFT_NICHT in antworten
    assert runde.kanal.verbindung.getrennt


def test_ohne_thread_endet_der_mitschnitt_trotzdem(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_frist
):
    """Eine Sitzung aus der Zeit vor dem Thread hat keinen — gesagt wird es dann nirgends."""
    bot = gateway.baue(konfiguration)

    alle_gehen(bot, runde)

    assert runde.kanal.verbindung.getrennt
    assert bot.kanaele == {}


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
    aufnahme = Aufnahme(konfiguration, unsere_runde(konfiguration), sitzung_id, KANAL)
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

    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith(f"{MIRA.name}.wav")


def test_ein_sprecher_ohne_konto_bekommt_eine_ehrlich_benannte_spur(konfiguration, sitzung_id):
    from discord.voice.packets import VoiceData

    aufnahme, senke = echte_senke(konfiguration, sitzung_id)

    senke.write(VoiceData(packet=None, source=None, pcm=stille(480)), None)
    aufnahme.beenden()

    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith(f"{gateway.UNBEKANNT}.wav")
