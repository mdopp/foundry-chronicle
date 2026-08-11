"""Der Aufnahme-Bot — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Kein Test spricht mit discord.com, und keiner braucht die Sprach-Abhängigkeiten: das
Gegenüber sind ein paar Dutzend Zeilen Attrappe. Der Token in diesen Tests ist erfunden
und steht nur hier.

Die beiden Sätze, die dieser Suite ihren Sinn geben: **vor der Ansage wird nichts
geschrieben**, und **was angesagt wurde, steht im Wortlaut im Protokoll.**
"""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import sqlite3
import sys
import types
import wave
from array import array
from dataclasses import replace
from pathlib import Path

import pytest
import requests
from conftest import runde as erste_runde

import chronicle.bot.__main__ as entry
from chronicle import consent, db, jobs, lebenszyklus, notes, recordings, settings
from chronicle import runde as runden
from chronicle.bot import BotFehler, BotHaelt, ansage, chronik, gateway, recorder
from chronicle.bot.ansage import AnsageFehlt
from chronicle.bot.recorder import Aufnahme, Kanal, NichtAngesagt
from chronicle.config import DEFAULT_TTS_URL, Config
from chronicle.discord import grenzen

TOKEN = "aufnahme-bot-token-nur-fuer-den-test"

KANAL = Kanal(guild_id="11", id="77", name="Runde")

MIRA = consent.Member(id="4001", name="Mira")
BROK = consent.Member(id="4002", name="Brok")
SPAET = consent.Member(id="4003", name="Aelin")

# Eine Sekunde Ton in der Form, die espeak-ng liefert: 22050 Hz, Mono, 16 Bit.
ESPEAK_RATE = 22050

# Und in der Form, die der Sprachdienst der Box liefert — an ihm gemessen, nicht geraten.
KOKORO_RATE = 24000

TTS_BASIS = "http://tts.test:8881"

# Woran im fertigen Stereo zu erkennen ist, wer gesprochen hat.
ESPEAK_WERT = 100
KOKORO_WERT = 1234


def _mono_wav(ziel, rate: int, wert: int) -> None:
    with wave.open(ziel, "wb") as datei:
        datei.setnchannels(1)
        datei.setsampwidth(2)
        datei.setframerate(rate)
        datei.writeframes(array("h", [wert, -wert] * (rate // 2)).tobytes())


def spricht(text: str, ziel: Path) -> None:
    _mono_wav(str(ziel), ESPEAK_RATE, ESPEAK_WERT)


def verweigert(text: str, ziel: Path) -> None:
    raise AssertionError("espeak-ng hätte hier nicht sprechen dürfen")


def _erster_wert(ziel: Path) -> int:
    with wave.open(str(ziel), "rb") as datei:
        return array("h", datei.readframes(1))[0]


class FakerSprachdienst:
    """Der Sprachdienst der Box: ein POST, eine WAV-Datei — oder Schweigen."""

    def __init__(self, *, antwort: bytes | None = None, fehler: Exception | None = None):
        if antwort is None:
            puffer = io.BytesIO()
            _mono_wav(puffer, KOKORO_RATE, KOKORO_WERT)
            antwort = puffer.getvalue()
        self.antwort = antwort
        self.fehler = fehler
        self.aufrufe: list[types.SimpleNamespace] = []

    def __call__(self):
        return self

    def post(self, url, *, json, timeout):
        self.aufrufe.append(types.SimpleNamespace(url=url, json=json, timeout=timeout))
        if self.fehler is not None:
            raise self.fehler
        return types.SimpleNamespace(content=self.antwort, raise_for_status=lambda: None)


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
        # Wie py-cord: der Bot sitzt da, wo er hingehört, bis ihn jemand zieht.
        self.verschoben = False

    def mitglieder(self):
        return tuple(self._anwesend)

    def im_kanal(self):
        return not self.verschoben

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


def test_die_gesprochene_ansage_sagt_das_tragende_und_bleibt_kurz():
    """Ab jetzt wird aufgezeichnet, so kommt man raus — der Rest steht im Kanal.

    Die Länge ist hier kein Geschmack: die Runde wartet, während die Ansage läuft, und
    den ausführlichen Text hat sie als ``gateway.VORSTELLUNG`` schon vor sich.
    """
    for satzteil in ("Ab jetzt wird dieses Gespräch aufgezeichnet", "verlässt", "Sprachkanal"):
        assert satzteil in ansage.TEXT
    assert "im Kanal" in ansage.TEXT
    assert len(ansage.TEXT.split()) <= 30


def test_das_protokoll_belegt_worueber_eingewilligt_wurde():
    """Der kurze Satz belegt *dass*, die Bedingungen daneben belegen *worüber*."""
    assert ansage.TEXT in ansage.PROTOKOLL
    assert ansage.BEDINGUNGEN in ansage.PROTOKOLL
    for satzteil in ("Sitzungsprotokoll", "Server der Gruppe", "einverstanden", "Tonspur"):
        assert satzteil in ansage.PROTOKOLL


def test_die_zugesagte_frist_ist_die_frist_aus_dem_code():
    # Der Satz darf sich nicht von dem entfernen, was ``recordings.sweep`` durchsetzt.
    assert f"{recordings.RETENTION_TAGE} Tage" in ansage.PROTOKOLL
    assert "aufbewahrt und dann gelöscht" in ansage.PROTOKOLL


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


def test_die_ansage_kommt_vom_sprachdienst_der_box(tmp_path, monkeypatch):
    monkeypatch.setattr(ansage, "_espeak", verweigert)
    dienst = FakerSprachdienst()

    ziel = ansage.datei(tmp_path, sprecher=ansage.mit_rueckfall(TTS_BASIS, http=dienst))

    (aufruf,) = dienst.aufrufe
    assert aufruf.url == TTS_BASIS + ansage.TTS_PFAD
    assert aufruf.json["input"] == ansage.TEXT
    assert aufruf.timeout == ansage.TTS_TIMEOUT
    assert _erster_wert(ziel) == KOKORO_WERT


def test_die_ansage_vom_sprachdienst_ist_was_discord_erwartet(tmp_path):
    ziel = ansage.datei(
        tmp_path, sprecher=ansage.mit_rueckfall(TTS_BASIS, http=FakerSprachdienst())
    )

    with wave.open(str(ziel), "rb") as datei:
        assert datei.getframerate() == ansage.RATE
        assert datei.getnchannels() == ansage.KANAELE
        assert datei.getsampwidth() == ansage.BREITE
        assert datei.getnframes() == ansage.RATE


@pytest.mark.parametrize(
    "dienst",
    [
        FakerSprachdienst(fehler=requests.ConnectionError("kein Dienst")),
        FakerSprachdienst(fehler=requests.Timeout("zu langsam")),
        FakerSprachdienst(antwort=b"<html>Ich bin kein Sprachdienst</html>"),
    ],
)
def test_schweigt_der_sprachdienst_spricht_espeak(tmp_path, monkeypatch, dienst):
    """Eine Ansage, die gar nicht kommt, verhindert die Aufnahme — das wiegt schwerer."""
    monkeypatch.setattr(ansage, "_espeak", spricht)

    ziel = ansage.datei(tmp_path, sprecher=ansage.mit_rueckfall(TTS_BASIS, http=dienst))

    assert _erster_wert(ziel) == ESPEAK_WERT


def test_ohne_eigene_adresse_fragt_die_ansage_den_dienst_dieser_box(tmp_path, monkeypatch):
    gefragt = []
    monkeypatch.setattr(
        ansage, "mit_rueckfall", lambda basis, **rest: gefragt.append(basis) or spricht
    )

    ansage.datei(tmp_path)
    ansage.datei(tmp_path / "andere", tts_url="http://tts.test:9999")

    assert gefragt == [DEFAULT_TTS_URL, "http://tts.test:9999"]


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


def test_der_start_reicht_die_konfigurierte_adresse_des_sprachdienstes_durch(
    konfiguration, sitzung_id, monkeypatch
):
    gefragt = []
    echt = ansage.datei
    monkeypatch.setattr(
        ansage,
        "datei",
        lambda ordner, **rest: (
            gefragt.append(rest.get("tts_url")) or echt(ordner, sprecher=spricht)
        ),
    )

    asyncio.run(
        recorder.starten(
            replace(konfiguration, tts_url=TTS_BASIS), FakeStimme(), unsere_runde(konfiguration)
        )
    )

    assert gefragt == [TTS_BASIS]


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
    assert eintrag.text == ansage.PROTOKOLL
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
    assert spaet.text == ansage.PROTOKOLL


def test_eine_ansage_die_woanders_lief_belegt_keine_einwilligung(
    konfiguration, sitzung_id, ohne_espeak
):
    """Der Eintrag ist der Nachweis einer *gehörten* Ansage, nicht einer gespielten.

    Wird der Bot gezogen, während die Ansage für den Nachzügler läuft, hört der sie nie —
    und ein Protokoll, das seine Zustimmung behauptet, wäre schlimmer als keines.
    """
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))
    stimme.verschoben = True

    assert asyncio.run(recorder.nachzuegler(konfiguration, stimme, aufnahme, SPAET)) is None

    (eintrag,) = consent.for_session(unsere_runde(konfiguration), sitzung_id)
    assert eintrag.kind == consent.ANSAGE


class ZiehtWaehrendDerAnsage(FakeStimme):
    """Ein Administrator zieht den Bot, während die Ansage noch spielt."""

    async def ansagen(self, datei):
        await super().ansagen(datei)
        self.verschoben = True


def test_wer_waehrend_der_ersten_ansage_gezogen_wird_faengt_gar_nicht_erst_an(
    konfiguration, sitzung_id, ohne_espeak
):
    """Dieselbe Lücke wie beim Nachzügler, nur am Anfang — und hier trägt sie alles.

    Der Ansage-Eintrag nennt Kanal und Besetzung des Ursprungs; beides wäre gelogen, wenn
    die Ansage nebenan gespielt hat. Also entsteht keiner, und mitgeschnitten wird auch
    nicht: ein Lauf, der weder schreibt noch etwas sagt, wäre das Schlechteste von beidem.
    """
    stimme = ZiehtWaehrendDerAnsage()

    with pytest.raises(recorder.AufnahmeFehler) as fehler:
        asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))

    assert str(fehler.value) == recorder.VERSCHOBEN_BEIM_START
    assert consent.for_session(unsere_runde(konfiguration), sitzung_id) == ()
    assert stimme.ablauf == ["ansage"]


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


def test_eine_stumme_spur_laesst_den_zweiten_anlauf_nicht_auflaufen(
    konfiguration, sitzung_id, ohne_espeak, monkeypatch
):
    """Wer nichts gesagt hat, hat auch keine Datei mehr — der zweite Anlauf muss das aushalten.

    Der erste Anlauf löscht die leere Spur. Fiele der zweite darüber, stürbe er **vor** der
    liegengebliebenen Spur — und die käme nie in die Warteschlange. Genau der Schaden aus
    #104, nur mit anderer Ursache.
    """
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA, BROK, SPAET), unsere_runde(konfiguration))
    )
    aufnahme.schreiben(MIRA, stille(480))
    # BROK drückt die Taste, sagt aber nichts: die Spur entsteht, bleibt leer und wird
    # beim ersten Anlauf gelöscht. Der zweite findet die Datei nicht mehr vor.
    aufnahme.schreiben(BROK, b"")
    aufnahme.schreiben(SPAET, stille(480))
    monkeypatch.setattr(recordings, "enqueue", StolpertBeimEinreihen(2))
    with pytest.raises(recorder.AufnahmeFehler):
        aufnahme.beenden()

    meldungen = aufnahme.beenden()

    assert sorted(sprecher(spur) for spur in recordings.pending(unsere_runde(konfiguration))) == [
        "Aelin",
        "Mira",
    ]
    assert len(meldungen) == 2
    assert list(konfiguration.recordings_dir.glob("*Brok.wav")) == []


def stolpert_beim_schliessen(nummer: int):
    """``_Spur.schliessen``, das bei der n-ten Spur einmal ausfällt — und dann nicht mehr.

    Geschlossen wird trotzdem, und zwar zuerst: die volle Platte reißt beim WAV-Kopf ab,
    nachdem ``wave`` die Datei im ``finally`` längst freigegeben hat. Der zweite Anlauf
    trifft deshalb auf ein stilles, erfolgreiches ``close`` — genau wie im Betrieb.
    """
    echt = recorder._Spur.schliessen
    versuche = []

    def schliessen(spur) -> None:
        echt(spur)
        versuche.append(spur)
        if len(versuche) == nummer:
            raise OSError(28, "No space left on device")

    return schliessen


def test_ein_gescheitertes_schliessen_haelt_die_uebrigen_nicht_auf(
    konfiguration, sitzung_id, ohne_espeak, monkeypatch
):
    """#115: das Schließen stand außerhalb der Absicherung — ein ``OSError`` riss alles ab.

    Die übrigen Spuren waren weder eingereiht noch benannt: Stimmen als Dateien ohne Zeile,
    für ``sweep`` unsichtbar. Die Zusage aus #104 gilt auch auf diesem Weg.
    """
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA, BROK, SPAET), unsere_runde(konfiguration))
    )
    for wer in (MIRA, BROK, SPAET):
        aufnahme.schreiben(wer, stille(480))
    monkeypatch.setattr(recorder._Spur, "schliessen", stolpert_beim_schliessen(2))

    with pytest.raises(recorder.AufnahmeFehler) as gefangen:
        aufnahme.beenden()

    assert not isinstance(gefangen.value, OSError)
    assert sorted(sprecher(spur) for spur in recordings.pending(unsere_runde(konfiguration))) == [
        "Aelin",
        "Mira",
    ]
    assert "Brok" in str(gefangen.value)
    assert list(konfiguration.recordings_dir.glob("*Brok.wav")) != []


def test_der_zweite_anlauf_holt_die_nicht_geschlossene_spur_nach(
    konfiguration, sitzung_id, ohne_espeak, monkeypatch
):
    """Die halb geschriebene Spur wird nicht gelöscht, sondern beim zweiten Mal eingereiht.

    Eine womöglich kaputte Spur, die in der Warteschlange steht, kann scheitern und wird
    nach Frist gelöscht; eine Datei ohne Zeile bleibt für immer liegen.
    """
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA, BROK, SPAET), unsere_runde(konfiguration))
    )
    for wer in (MIRA, BROK, SPAET):
        aufnahme.schreiben(wer, stille(480))
    monkeypatch.setattr(recorder._Spur, "schliessen", stolpert_beim_schliessen(2))
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


@pytest.fixture
def kein_loeschen(konfiguration):
    """Sperrt das Aufnahmeverzeichnis fürs Schreiben — ``unlink`` scheitert dann.

    Der ``OSError`` von ``unlink`` trägt den vollen Pfad und damit den Anzeigenamen des
    Sprechers; genau daran hängt, ob ein Klarname ins Log gerät.
    """
    yield lambda: konfiguration.recordings_dir.chmod(0o500)
    if konfiguration.recordings_dir.exists():
        konfiguration.recordings_dir.chmod(0o700)


def test_eine_unloeschbare_stumme_spur_nennt_weder_namen_noch_falsche_zusage(
    konfiguration, sitzung_id, ohne_espeak, caplog, kein_loeschen
):
    """Scheitert das Löschen der leeren Spur, bleibt der Anzeigename aus dem Log.

    Und die Runde bekommt keine Meldung darüber: eine leere Spur wird nie eingereiht, ein
    zweiter Anlauf scheiterte identisch weiter — »das holt genau sie nach« wäre gelogen.
    """
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA, BROK), unsere_runde(konfiguration))
    )
    aufnahme.schreiben(MIRA, stille(480))
    aufnahme.schreiben(BROK, b"")
    kein_loeschen()
    caplog.clear()

    meldungen = aufnahme.beenden()

    assert "Brok" not in caplog.text
    assert [sprecher(spur) for spur in recordings.pending(unsere_runde(konfiguration))] == ["Mira"]
    assert len(meldungen) == 1
    assert list(konfiguration.recordings_dir.glob("*Brok.wav")) != []


def test_ohne_runde_haelt_ein_gescheitertes_schliessen_das_loeschen_nicht_auf(
    konfiguration, sitzung_id, ohne_espeak, monkeypatch
):
    """#115 auf dem zweiten Weg: dort stand ``schliessen`` ganz ohne Absicherung.

    Ein roher ``OSError`` riss den Durchgang ab, und die übrigen Spuren blieben liegen —
    hier ohne jede Rettung, denn ohne Runde entsteht nie eine Zeile, die ``sweep`` fände.
    Geschlossen oder nicht: gelöscht wird trotzdem, ``close`` gibt die Datei ohnehin frei.
    """
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA, BROK), unsere_runde(konfiguration))
    )
    for wer in (MIRA, BROK):
        aufnahme.schreiben(wer, stille(480))
    # Die Runde verschwindet erst jetzt: ``lebenszyklus.loeschen`` nähme die Dateien gleich
    # mit, und dann prüfte dieser Test nichts mehr.
    monkeypatch.setattr(lebenszyklus, "dieselbe", lambda runde: None)
    monkeypatch.setattr(recorder._Spur, "schliessen", stolpert_beim_schliessen(1))

    meldungen = aufnahme.beenden()

    assert meldungen == (recorder.RUNDE_FORT,)
    assert list(konfiguration.recordings_dir.glob("*Mira.wav")) == []
    assert list(konfiguration.recordings_dir.glob("*Brok.wav")) == []


def test_ohne_runde_bleibt_eine_unloeschbare_spur_nicht_stillschweigend_liegen(
    konfiguration, sitzung_id, ohne_espeak, kein_loeschen, monkeypatch
):
    """»Die Spuren sind gelöscht« wird nicht behauptet, wenn eine noch daliegt.

    Genannt wird sie ohne Namen: die Runde ist fort, ihre Kennung kann längst einer fremden
    Gilde gehören. Der zweite Anlauf versucht es erneut.
    """
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(MIRA), unsere_runde(konfiguration))
    )
    aufnahme.schreiben(MIRA, stille(480))
    monkeypatch.setattr(lebenszyklus, "dieselbe", lambda runde: None)
    kein_loeschen()

    with pytest.raises(recorder.AufnahmeFehler) as gefangen:
        aufnahme.beenden()

    assert "Mira" not in str(gefangen.value)
    assert list(konfiguration.recordings_dir.glob("*Mira.wav")) != []
    konfiguration.recordings_dir.chmod(0o700)
    assert aufnahme.beenden() == (recorder.RUNDE_FORT,)
    assert list(konfiguration.recordings_dir.glob("*Mira.wav")) == []


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
    assert zeile["text"] == ansage.PROTOKOLL
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


class FakeNichtGefunden(Exception):
    """Steht für ``discord.NotFound`` — 404, den Kanal gibt es nicht mehr."""


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
        # Was py-cord beim Anmelden aus dem READY-Rahmen zwischenspeichert. Leer heißt
        # hier: in keiner Gilde — nicht »noch nicht bekannt«.
        self.guilds = ()

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
    """Die Senke, so weit die Doppel sie brauchen — samt der Verdrahtung, die zaehlt.

    Das echte ``discord.sinks.Sink`` liest ``client`` aus ``self.vc``, gesetzt wird es von
    ``Sink.init``. py-cord 2.8.1 ruft das im Empfangspfad nie, ``opus.py`` prueft es aber
    beim ersten Paket mit einem ``assert``. Die Attrappe haelt es deshalb genauso.
    """

    def __init__(self, **rest):
        self.finished = False
        self.vc = None

    @property
    def client(self):
        return self.vc

    def init(self, vc) -> None:
        self.vc = vc


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
        # Discords eigener Zustand des Bots. py-cord schreibt ihn, bevor es das Ereignis
        # ausliefert — auch dann, wenn der Voice-Client den Kanal gerade nicht nachträgt.
        self.me = None

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
        if self.guild.me is not None:
            # Beitreten ist auch ein Zustandswechsel: Discord führt den Bot ab jetzt hier.
            self.guild.me.voice.channel = self
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
        # Wie oft das Senden noch scheitert, bevor es durchgeht — Discord zuckt.
        self.stolpert = 0

    async def send(self, text):
        # Echtes ``send`` geht ans Netz und gibt dabei an die Schleife ab. Ohne dieses
        # Abgeben interleaven zwei Ereignis-Tasks im Test nie, und jeder Fehler, der nur
        # zwischen zwei gleichzeitigen Handlern entsteht, bliebe unsichtbar.
        await asyncio.sleep(0)
        if self.stolpert:
            self.stolpert -= 1
            raise RuntimeError("Discord hat abgelehnt")
        self.geschrieben.append(text)


class FakeFortgeraeumterThread:
    """Ein Thread, den es in Discord nicht mehr gibt — ``send`` bleibt bei 404."""

    def __init__(self):
        self.versuche = 0

    async def send(self, text):
        await asyncio.sleep(0)
        self.versuche += 1
        raise FakeNichtGefunden("404 Not Found (error code: 10003): Unknown Channel")


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


class FakeOption:
    """Das Feld eines Slash-Befehls, so weit die Doppel es brauchen.

    Es hält seinen ``input_type`` wie das echte, damit ein Test nachsehen kann, ob dort
    wirklich eine Klasse steht — eine Zeichenkette wäre der Fehler, an dem py-cord beim
    ersten Aufruf stirbt.
    """

    def __init__(self, input_type, description="", default=None, required=True, **rest):
        self.input_type = input_type
        self.description = description
        self.default = default
        self.required = required

    def __eq__(self, other):
        return isinstance(other, FakeOption) and self.default == other.default

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
    senken = types.ModuleType("discord.sinks")
    senken.Sink = FakeSenke
    modul.sinks = senken
    werkzeug = types.ModuleType("discord.utils")
    werkzeug.get_missing_voice_dependencies = lambda: ()
    modul.utils = werkzeug
    fehler = types.ModuleType("discord.errors")
    fehler.PrivilegedIntentsRequired = FakeRechteFehlen
    fehler.LoginFailure = FakeTokenAbgelehnt
    fehler.NotFound = FakeNichtGefunden
    modul.errors = fehler
    modul.NotFound = FakeNichtGefunden
    monkeypatch.setitem(sys.modules, "discord", modul)
    monkeypatch.setattr(FakeBot, "erzeugt", [])
    return modul


@pytest.fixture
def runde(pycord):
    gilde = FakeGilde()
    mira = FakeMitglied(int(MIRA.id), MIRA.name)
    brok = FakeMitglied(int(BROK.id), BROK.name)
    chronist = FakeMitglied(999, "Chronik-Bot", bot=True, kanal=None)
    kanal = FakeSprachkanal(gilde, mira, brok, chronist)
    chronist.voice.channel = kanal
    gilde.me = chronist
    for wer in (mira, brok):
        gilde.mitglieder[wer.id] = wer
        wer.voice = types.SimpleNamespace(channel=kanal)
    return types.SimpleNamespace(gilde=gilde, kanal=kanal, mira=mira, brok=brok, chronist=chronist)


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
    # wird. Die Null ist der Beleg: beim Schreiben lief noch keine Ansage. Sie gilt für
    # **jedes** Stück: die Vorstellung ist mit der Befehlsliste über Discords Grenze
    # gewachsen und geht seit #109 geteilt hinaus, statt abgewiesen zu werden.
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert "".join(gesagt) == gateway.VORSTELLUNG
    assert gateway.AUSWEG in gesagt[0]
    assert [gespielt for _, gespielt in runde.kanal.geschrieben] == [0] * len(gesagt)
    assert len(runde.kanal.verbindung.gespielt) == 1


def test_die_vorstellung_sagt_frist_und_befehle_aus_einer_quelle():
    assert f"{recordings.RETENTION_TAGE} Tagen" in gateway.VORSTELLUNG
    # Ein Text, zwei Anlässe: die Liste steht nicht zweimal da.
    assert gateway.BEFEHLE in gateway.VORSTELLUNG
    assert gateway.BEFEHLE in gateway.HILFE
    # Und der Ausweg ebenso wenig — er trägt rechtlich, also gibt es ihn genau einmal.
    assert gateway.AUSWEG in gateway.VORSTELLUNG
    assert gateway.AUSWEG in gateway.HILFE
    for satzteil in ("hörbare Ansage", "Bis dahin ist Zeit"):
        assert satzteil in gateway.VORSTELLUNG


@pytest.mark.parametrize("welcher", ["VORSTELLUNG", "HILFE"])
def test_der_ausweg_steht_in_der_ersten_nachricht(welcher):
    """Auch wenn die Befehlsliste dahinter noch zwei Nachrichten füllt.

    Genau das war der Fehler aus #109: die Liste wuchs, die Nachricht riss Discords Grenze,
    und mit ihr fiel der Satz aus, der den Ausweg aus der Aufnahme nennt. Er gilt für
    **jeden** Text, der den Ausweg führt — sonst hält die Reihenfolge nur dort, wo gerade
    jemand hingesehen hat, und der nächste Befehl reißt sie anderswo.
    """
    ganz = getattr(gateway, welcher)
    assert gateway.AUSWEG in grenzen.teile(ganz)[0]

    gewachsen = ganz.replace(gateway.BEFEHLE, gateway.BEFEHLE * 4)
    stuecke = grenzen.teile(gewachsen)

    assert len(stuecke) > 1
    assert gateway.AUSWEG in stuecke[0]
    assert "".join(stuecke) == gewachsen


def test_eine_uebergrosse_vorstellung_kommt_vollstaendig_an(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Und auch weit über der Grenze bleibt es dabei: nichts fällt weg, nichts rutscht vor.

    Die heutige Vorstellung braucht zwei Nachrichten; hier sind es vier. Der Unterschied
    darf keiner sein — sonst hinge die Zusage an der jeweiligen Länge der Befehlsliste.
    """
    lang = gateway.VORSTELLUNG.replace(gateway.BEFEHLE, gateway.BEFEHLE * 4)
    monkeypatch.setattr(gateway, "VORSTELLUNG", lang)
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert len(gesagt) > 1
    assert all(len(text) <= grenzen.NACHRICHT for text in gesagt)
    assert "".join(gesagt) == lang
    # Vor der Ansage, alle Stücke: die Reihenfolge ist der Punkt, nicht nur der Anfang.
    assert [gespielt for _, gespielt in runde.kanal.geschrieben] == [0] * len(gesagt)
    assert gateway.AUSWEG in gesagt[0]


def test_ohne_kanal_chat_geht_die_vorstellung_dorthin_wo_der_befehl_kam(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    kanal = FakeSprachkanalOhneChat(runde.gilde, runde.mira, runde.brok)
    runde.mira.voice = types.SimpleNamespace(channel=kanal)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    assert "".join(ctx.channel.geschrieben) == gateway.VORSTELLUNG
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


def test_viele_spurmeldungen_fallen_nicht_still_weg(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist, monkeypatch
):
    """Der Fall aus #120: dreißig Spuren ergaben einen Satz, den Discord nicht mehr annahm.

    Gefangen wurde die Abweisung von einem ``except`` daneben — die Runde erfuhr nichts,
    weder vom Ende des Mitschnitts noch davon, dass der Satz ausblieb.
    """
    meldungen = tuple(
        f"Spur »2026-08-11-{nummer:02d}-Sprecherin.wav« eingereiht, wartet auf den Stapel."
        for nummer in range(40)
    )

    echtes_stoppen = recorder.stoppen

    async def viele(stimme, aufnahme):
        await echtes_stoppen(stimme, aufnahme)
        return meldungen

    monkeypatch.setattr(gateway.recorder, "stoppen", viele)
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread

    alle_gehen(bot, runde)

    ganz = " ".join((gateway.LEER_BEENDET, *meldungen))
    assert len(ganz) > grenzen.NACHRICHT
    assert len(thread.geschrieben) > 1
    assert all(len(gesagt) <= grenzen.NACHRICHT for gesagt in thread.geschrieben)
    assert "".join(thread.geschrieben) == ganz
    assert all(meldung in ganz for meldung in meldungen)


def test_wenige_spurmeldungen_bleiben_eine_nachricht(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Gegenprobe: geteilt wird nur, was geteilt werden muss."""
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread

    alle_gehen(bot, runde)

    assert len(thread.geschrieben) == 1


class FakeZuckendesDiscord:
    """Nimmt jede Nachricht an — außer der einen, bei der es zuckt."""

    def __init__(self, *, bricht_beim):
        self.geschrieben = []
        self._bricht_beim = bricht_beim
        self._versuche = 0

    async def send(self, text):
        self._versuche += 1
        if self._versuche == self._bricht_beim:
            raise RuntimeError("Discord nimmt die Nachricht nicht an")
        self.geschrieben.append(text)


def test_ein_abgerissener_text_sagt_selbst_an_dass_er_abgerissen_ist(caplog):
    """Teilen tauscht »gar nichts« gegen »die Hälfte« — die darf nicht wie ein Ganzes aussehen.

    Bricht die Zustellung nach dem ersten Stück ab, endet der Absatz im Kanal mitten im
    Satz und nichts sagt der Runde, dass etwas fehlt. Ein zerrissener Text, den niemand
    als zerrissen erkennt, ist schlimmer als eine fehlende Nachricht.
    """
    text = "Zeile.\n" * 900
    stuecke = len(grenzen.teile(text))
    assert stuecke > 2
    kanal = FakeZuckendesDiscord(bricht_beim=2)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        asyncio.run(gateway._zustellen(kanal.send, text))

    # Der Hinweis steht hinter dem, was ankam — und nennt beide Zahlen, wie das Log.
    assert kanal.geschrieben[-1] == gateway.ABGERISSEN.format(
        zugestellt=1, ganz=stuecke, fehlend=stuecke - 1
    )
    assert f"1 von {stuecke} Stücken zugestellt, {stuecke - 1} fehlen" in caplog.text


def test_scheitert_schon_das_erste_stueck_bleibt_es_beim_alles_oder_nichts(caplog):
    """Gegenprobe: ohne angekommenen Anfang gibt es nichts kenntlich zu machen.

    Ein Hinweis auf einen Text, den niemand gesehen hat, wäre selbst die Irreführung.
    """
    kanal = FakeZuckendesDiscord(bricht_beim=1)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        asyncio.run(gateway._zustellen(kanal.send, "Zeile.\n" * 900))

    assert kanal.geschrieben == []
    assert "abgerissen" not in caplog.text


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


def einer_bleibt(kanal, wer):
    """Alle gehen bis auf einen — der Bot bleibt, er zählt ohnehin nicht mit."""
    kanal.members = [jemand for jemand in kanal.members if jemand.bot or jemand is wer]


def dritter_im_kanal(runde):
    """Eine dritte Person im Sprachkanal — die Attrappe kennt von Haus aus zwei."""
    wer = FakeMitglied(int(SPAET.id), SPAET.name, kanal=runde.kanal)
    runde.gilde.mitglieder[wer.id] = wer
    runde.kanal.members.append(wer)
    return wer


def der_lauf(bot):
    """Der Zustand hinter den Ereignissen — er lebt in der Closure von ``baue``."""
    return inspect.getclosurevars(bot.ereignisse["on_voice_state_update"]).nonlocals["lauf"]


def test_der_satz_ans_alleinsein_zeigt_den_widerspruch_und_traegt_keinen_namen():
    """Ein Satz ohne Feld kann keinen Namen und keine Kennung tragen — auch nicht später."""
    assert "`/aufnahme stop`" in gateway.ALLEIN
    assert "{" not in gateway.ALLEIN
    # Und die Hilfe sagt es vorab — die Vorstellung nicht, denn sie ist ohnehin der längste
    # Text des Bots. Geprüft wird der Satz und nicht »allein im Sprachkanal«: das steht auch
    # im Punkt zu ``/aufnahme stop`` und meint dort die Gegenlage, den leeren Kanal.
    hinweis = "Bleibt eine Person allein im Sprachkanal zurück, schneide ich weiter mit"
    assert hinweis in gateway.HILFE
    assert hinweis not in gateway.BEFEHLE
    assert hinweis not in gateway.VORSTELLUNG
    assert len(gateway.HILFE) <= grenzen.NACHRICHT
    # Die Vorstellung steht **über** der Grenze, seit die Befehlsliste um `/chronik abgleich`
    # gewachsen ist — und das ist kein Fehlschlag, sondern der Fall, den #109 gebaut hat:
    # ``_zustellen`` teilt, statt abweisen zu lassen, und der Kommentar an ``AUSWEG`` sagt
    # den Tag vorher an (»schiebt alles hinter sich irgendwann in eine zweite Nachricht«).
    # Vorher stand hier ``<= 2000``; das ließ 15 Zeichen Luft und damit keinen einzigen
    # weiteren Befehl zu, und die Begründung — »eine Vorstellung, die nicht ankommt« —
    # trifft seit #109 nicht mehr zu. An ihre Stelle tritt, was die Zusage wirklich trägt:
    # der Ausweg steht im **ersten** Stück, und angehängt ergeben die Stücke wieder genau
    # den Text. Beides prüfen die Tests zur Vorstellung, hier steht die Kurzfassung.
    stuecke = grenzen.teile(gateway.VORSTELLUNG)
    assert gateway.AUSWEG in stuecke[0]
    assert "".join(stuecke) == gateway.VORSTELLUNG


def test_bleibt_eine_person_allein_wird_es_ihr_gesagt_und_weiter_geschnitten(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Entscheidung des Betreibers: nachfragen statt anhalten — niemand verliert ungefragt.

    Die Ansage lief für eine Gruppe, die es nicht mehr gibt; deshalb erfährt die
    Zurückgebliebene, dass sie allein ist. Angehalten wird darunter nichts.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_dass_sie_allein_ist_wird_genau_einmal_gesagt(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Sonst stünde bei jedem Stummschalten derselbe Satz noch einmal im Thread."""
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        # Ein Nachzügler, der gleich wieder geht: die Lage ist danach dieselbe.
        runde.kanal.members.append(runde.brok)
        await dazu(runde.brok, zustand(), zustand(runde.kanal))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        # Und ein Ortswechsel im selben Kanal ist gar kein Gehen.
        await dazu(runde.mira, zustand(runde.kanal), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.ALLEIN]


def test_gehen_zwei_im_selben_schwung_steht_der_satz_trotzdem_nur_einmal_da(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """py-cord stellt jedes Sprachereignis als eigenen Task zu — und hat den Cache der
    Mitglieder vorher aktualisiert. Beide Handler sehen deshalb dieselbe eine Verbliebene;
    wer den Vermerk erst hinter dem ``await`` setzt, schreibt den Satz zweimal.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        aelin = dritter_im_kanal(runde)
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await asyncio.gather(
            dazu(runde.brok, zustand(runde.kanal), zustand()),
            dazu(aelin, zustand(runde.kanal), zustand()),
        )
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet


def test_bleiben_zwei_zurueck_sagt_der_bot_nichts(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """»Nur noch **eine** Person« ist eine Tatsachenbehauptung, an der die Einwilligung
    hängt: sie soll aufhören können, weil sie allein ist. Zu zweit wäre sie falsch.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        aelin = dritter_im_kanal(runde)
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.members.remove(aelin)
        await dazu(aelin, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == []
    assert runde.kanal.verbindung.schneidet


def test_ein_ortswechsel_im_selben_kanal_ist_gar_kein_gehen(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Stummschalten meldet Discord mit demselben Kanal auf beiden Seiten des Ereignisses.

    Wer nur auf »war drin« sieht, hält das für ein Gehen — und sagt der, die von Anfang an
    allein mitschneidet, beim ersten Stummschalten, es sei gerade jemand gegangen. Ohne
    vorheriges Gehen fängt das auch kein Vermerk ab.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        einer_bleibt(runde.kanal, runde.mira)
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await dazu(runde.mira, zustand(runde.kanal), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == []
    assert runde.kanal.verbindung.schneidet


def test_ein_vermerk_von_vorhin_verschluckt_den_satz_der_naechsten_aufnahme_nicht(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Dieselbe Falle wie beim Wächter des leeren Kanals: gemerkt wird die Aufnahme."""
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        anwesend = list(runde.kanal.members)
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await befehl(bot, "stop")(FakeCtx(runde.mira))
        runde.kanal.members = anwesend
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.ALLEIN, gateway.ALLEIN]


def test_auch_die_zweite_zurueckgebliebene_erfaehrt_es(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Genau der Fall aus dem Issue: nacheinander bleiben zwei verschiedene allein zurück.

    Hinge der Vermerk an der Aufnahme, hätte ihn die erste verbraucht — die zweite säße
    allein im Kanal und erführe es nie. Er hängt deshalb an der Person.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        runde.kanal.members.append(runde.brok)
        await dazu(runde.brok, zustand(), zustand(runde.kanal))
        einer_bleibt(runde.kanal, runde.brok)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.ALLEIN, gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet


def test_ein_zuckender_thread_verbrennt_den_satz_nicht(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Vermerkt wird erst, was ankam — sonst bliebe der Satz nach einem Fehlschlag aus.

    Nachgeholt wird er nirgends: käme der Vermerk vor dem Senden, wäre eine einmal
    gescheiterte Zustellung das Ende, und sie erführe es auch bei jedem weiteren Wechsel
    nicht mehr.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    thread.stolpert = 1
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        runde.kanal.members.append(runde.brok)
        await dazu(runde.brok, zustand(), zustand(runde.kanal))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt


def test_ohne_thread_wird_allein_nicht_weitergeschnitten(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_frist
):
    """Zugesagt war, dass sie es erfährt — nicht, dass wir es versuchen.

    Eine Sitzung ohne Thread hat keinen Weg für den Satz. Still weiterzuschneiden wäre
    genau der Zustand, gegen den die Zusage steht; also endet der Mitschnitt, und die
    Spuren gehen nicht verloren.
    """
    bot = gateway.baue(konfiguration)
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert len(recordings.pending(unsere_runde(konfiguration))) == 1


def test_ein_fortgeraeumter_thread_beendet_den_mitschnitt_statt_ewig_weiterzuschneiden(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Fort ist nicht »zuckt«: der 404 käme bei jedem weiteren Wechsel genauso wieder.

    Behandelte der Bot ihn wie ein Zucken, schnitte er dauerhaft weiter, ohne dass ihr je
    gesagt wurde, dass sie allein ist — derselbe Zustand, gegen den die Zusage steht, nur
    durch die andere Tür.
    """
    bot = gateway.baue(konfiguration)
    fort = FakeFortgeraeumterThread()
    bot.kanaele[THREAD] = fort
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert fort.versuche == 1
    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert len(recordings.pending(unsere_runde(konfiguration))) == 1


def test_der_vermerk_ans_alleinsein_ueberlebt_das_ende_der_aufnahme_nicht(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Der Vermerk führt Discord-Kennungen. Nach dem Ende liest sie niemand mehr."""
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        gemerkt = der_lauf(bot).allein
        await befehl(bot, "stop")(FakeCtx(runde.mira))
        await ruhen()
        return gemerkt

    gemerkt = asyncio.run(ablauf())

    # Erst dass überhaupt etwas dastand — sonst prüfte der zweite Satz nur eine Lücke.
    assert gemerkt is not None
    assert MIRA.id in gemerkt[1]
    assert der_lauf(bot).allein is None


def test_die_rueckkehr_setzt_dieselbe_aufnahme_fort(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Entscheidung des Betreibers: kein Bruch, keine neue Sitzung, keine zweite Ansage.

    Die Spur läuft durch — dieselbe Senke, dieselbe Verbindung, dieselbe Sitzung.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        senke = runde.kanal.verbindung.senke
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        runde.kanal.members.append(runde.brok)
        await dazu(runde.brok, zustand(), zustand(runde.kanal))
        await ruhen()
        return senke

    senke = asyncio.run(ablauf())

    unsere = unsere_runde(konfiguration)
    assert runde.kanal.verbindung.senke is senke
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt
    assert notes.latest_session(unsere).id == sitzung_im_thread
    angesagt = [ereignis for ereignis in consent.for_session(unsere, sitzung_im_thread)]
    assert [ereignis.kind for ereignis in angesagt] == [consent.ANSAGE, consent.NACHZUEGLER]


def test_wer_zurueckkommt_ist_ein_nachzuegler_und_hoert_die_ansage(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Die zweite Hälfte derselben Entscheidung: fortsetzen heißt nicht ungefragt.

    Für den Kanal läuft keine zweite Ansage — für den Zurückkehrenden schon, und seine
    Einwilligung steht danach im Protokoll.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        runde.kanal.members.append(runde.brok)
        await dazu(runde.brok, zustand(), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    assert len(runde.kanal.verbindung.gespielt) == 2
    _, zurueck = consent.for_session(unsere_runde(konfiguration), sitzung_im_thread)
    assert zurueck.kind == consent.NACHZUEGLER
    assert [wer.name for wer in zurueck.members] == [BROK.name]


def test_wer_allein_widerspricht_beendet_damit_den_mitschnitt(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """»Bis sie widerspricht« braucht einen benannten Weg — und der ist der vorhandene."""
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        ctx = FakeCtx(runde.mira)
        await befehl(bot, "stop")(ctx)
        await ruhen()
        return ctx

    ctx = asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert "wartet auf den Stapel" in ctx.antworten[0]


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


def verschieben(runde, kennung=78):
    """Ein Administrator zieht den Bot nach nebenan — beide Quellen tragen den Kanal nach.

    ``voice/state.py`` → ``_update_voice_channel`` setzt ``VoiceClient.channel``, und
    ``guild._update_voice_state`` schreibt Discords eigenen Zustand des Bots. Beides
    geschieht, bevor das Ereignis beim Bot ankommt.
    """
    nachbar = FakeSprachkanal(runde.gilde, runde.mira, runde.brok)
    nachbar.id = kennung
    runde.kanal.verbindung.channel = nachbar
    runde.chronist.voice.channel = nachbar
    return nachbar


def verbindung_zum_kanal(runde):
    """Die echte ``Sprachverbindung`` auf dem nachgebauten Voice-Client."""
    return gateway.Sprachverbindung(asyncio.run(runde.kanal.connect()))


def test_die_mitglieder_sind_die_des_aufnahmekanals_nicht_die_des_bots(pycord, runde):
    """Die Zusicherung aus #101, ohne Umweg über den Beobachter.

    Dieselbe Liste geht in den Ansage-Eintrag des Einwilligungsprotokolls und entscheidet,
    wann der Kanal als leer gilt. Zöge sie mit dem Bot mit, stünde im Protokoll die
    Besetzung eines Kanals, in dem nie etwas angesagt wurde — und die Leere entschiede
    über eine fremde Besetzung.
    """
    stimme = verbindung_zum_kanal(runde)
    nachbar = verschieben(runde)
    nachbar.members = [FakeMitglied(4404, "Fremde")]

    assert [wer.name for wer in stimme.mitglieder()] == [MIRA.name, BROK.name]


def test_die_verschiebung_faellt_auch_ohne_den_voice_client_auf(pycord, runde):
    """``VoiceClient.channel`` allein reicht nicht — py-cord trägt ihn nicht immer nach.

    Im Verbindungszustand ``got_voice_server_update`` lässt ``voice/state.py`` den Kanal
    stehen. Fällt eine Verschiebung mit einer Voice-Server-Migration zusammen, zeigt der
    Voice-Client weiter auf den alten Kanal; Discords eigener Zustand des Bots nicht.
    """
    stimme = verbindung_zum_kanal(runde)
    nachbar = FakeSprachkanal(runde.gilde, runde.mira)
    nachbar.id = 78
    runde.chronist.voice.channel = nachbar

    assert runde.kanal.verbindung.channel is runde.kanal
    assert not stimme.im_kanal()


def test_ohne_verbindung_sitzt_der_bot_in_keinem_kanal(pycord, runde):
    """Getrennt ist nicht »noch da«: ``VoiceClient.channel`` ist dann ``None``."""
    stimme = verbindung_zum_kanal(runde)
    runde.kanal.verbindung.channel = None

    assert not stimme.im_kanal()


class FakeVoiceClientMitGuildProperty:
    """Wie py-cord 2.8.1: ``guild`` ist eine Property über ``self.channel.guild``.

    ``blicke`` zählt, wie oft ``channel`` noch den Kanal hergibt; danach ist er ``None``,
    so wie py-cord ihn beim Trennen setzt. Damit liegt das Trennen zwischen den beiden
    Blicken, die ``im_kanal`` auf ``channel`` wirft.
    """

    def __init__(self, kanal, blicke):
        self._kanal = kanal
        self.blicke = blicke

    @property
    def channel(self):
        if self.blicke <= 0:
            return None
        self.blicke -= 1
        return self._kanal

    @property
    def guild(self):
        return self.channel.guild


def test_ein_trennen_zwischen_den_beiden_blicken_wirft_nicht(pycord, runde):
    """``VoiceClient.guild`` wirft, wenn py-cord den Kanal schon geräumt hat (#120).

    ``im_kanal`` liest ``channel`` zweimal: einmal selbst für ``jetzt``, einmal über
    ``.guild``. ``SpurSenke.write`` läuft im Empfangs-Thread, und dazwischen kann py-cord
    ``client.channel = None`` setzen — dann warf der zweite Blick ein ``AttributeError``
    mitten in die Senke. Ohne die zweite Quelle bleibt es bei dem, was der Voice-Client
    beim ersten Blick sagte.
    """
    # Einer für den Aufbau der ``Sprachverbindung``, einer für ``jetzt`` — der Blick über
    # ``.guild`` fällt dann schon ins Leere.
    verbindung = FakeVoiceClientMitGuildProperty(runde.kanal, blicke=2)
    stimme = gateway.Sprachverbindung(verbindung)

    assert stimme.im_kanal() is True
    assert verbindung.channel is None


def test_verschoben_zaehlt_weiter_der_kanal_der_aufnahme(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """»Gegangen« und »leer« müssen denselben Kanal meinen — den der Ansage.

    Seit #103 endet der Mitschnitt beim Verschieben ohnehin; dass hier trotzdem nichts
    hängen bleibt, hängt weiter daran, dass der Lauf seinen Ursprungskanal festhält.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        verschieben(runde)
        nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt


def test_wer_verschoben_wird_hoert_auf_und_sagt_warum(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Der Bot hat den Raum verlassen, für den die Einwilligung galt — also ist Schluss.

    Nebenan sitzen Leute, die die Ansage nie gehört haben. Dass der Mitschnitt endet,
    steht im Thread, und es steht dabei, **warum**.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        nachbar = verschieben(runde)
        # Sein eigenes Verschieben meldet Discord dem Bot wie jedes andere Ereignis.
        await bot.ereignisse["on_voice_state_update"](
            runde.chronist, zustand(runde.kanal), zustand(nachbar)
        )
        await ruhen()

    asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")
    (gesagt,) = thread.geschrieben
    assert gesagt.startswith(gateway.VERSCHOBEN.format(kanal=runde.kanal.name))
    assert "wartet auf den Stapel" in gesagt


def test_der_start_im_falschen_kanal_laesst_keinen_lauf_zurueck(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist, monkeypatch
):
    """Wird der Bot während der ersten Ansage gezogen, gilt der Lauf nicht als laufend.

    Sonst stünde er als schneidend da, schriebe nichts und sagte nichts — bis irgendein
    fremdes Voice-Ereignis vorbeikäme. Stattdessen bricht der Befehl ab, sagt es dem
    Aufrufer und lässt den Bot nicht im falschen Kanal sitzen.
    """
    echt = FakeVoiceClient.play

    def zieht(selbst, quelle, *, after):
        echt(selbst, quelle, after=after)
        verschieben(runde)

    monkeypatch.setattr(FakeVoiceClient, "play", zieht)
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    ctx = FakeCtx(runde.mira)

    async def ablauf():
        await befehl(bot, "start")(ctx)
        # Erst hierdurch fiele ein hängengebliebener Lauf überhaupt auf — es gibt keinen.
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert consent.for_session(unsere_runde(konfiguration), sitzung_im_thread) == ()
    assert thread.geschrieben == []
    assert ctx.antworten[-1] == gateway.GESCHEITERT.format(grund=recorder.VERSCHOBEN_BEIM_START)


def test_was_nach_dem_verschieben_ankommt_landet_in_keiner_spur(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Für das Stück zwischen Verschiebung und Erkennung gibt es keine Zustimmung.

    Verworfen wird es nicht nachträglich, sondern gar nicht erst geschrieben: ab dem
    Rahmen, in dem py-cord den neuen Kanal führt, fällt jede Sprachdatenlieferung weg.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        senke = runde.kanal.verbindung.senke
        senke.write(sprachdaten(stille(480)), runde.mira)
        nachbar = verschieben(runde)
        # Zwischen Verschiebung und Erkennung: der Nachbarkanal spricht, Mira auch.
        fremder = FakeMitglied(4404, "Fremde")
        senke.write(sprachdaten(stille(4800)), fremder)
        senke.write(sprachdaten(stille(4800)), runde.mira)
        await bot.ereignisse["on_voice_state_update"](
            runde.chronist, zustand(runde.kanal), zustand(nachbar)
        )
        await ruhen()

    asyncio.run(ablauf())

    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")
    with wave.open(str(konfiguration.recordings_dir / spur.filename), "rb") as datei:
        # Genau die 480 Rahmen von vor dem Verschieben — kein einziger danach.
        assert datei.getnframes() == 480


def test_der_nachzuegler_im_alten_kanal_kommt_nicht_ins_protokoll(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Die Nebenwirkung derselben Klasse: seine Ansage liefe nebenan, er hörte sie nie.

    Kein Einwilligungseintrag darf entstehen, dessen Ansage nachweislich woanders lief.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[THREAD] = FakeTextkanal()
    spaet = FakeMitglied(int(SPAET.id), SPAET.name)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        verschieben(runde)
        await bot.ereignisse["on_voice_state_update"](spaet, zustand(), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    (eintrag,) = consent.for_session(unsere_runde(konfiguration), sitzung_im_thread)
    assert eintrag.kind == consent.ANSAGE
    # Und die zweite Ansage hat gar nicht erst gespielt.
    assert len(runde.kanal.verbindung.gespielt) == 1
    assert runde.kanal.verbindung.getrennt


def test_das_verschieben_beendet_genau_einmal_und_nicht_die_naechste_aufnahme(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Zwei Ereignisse, ein Ende — und der nächste Lauf bleibt davon unberührt."""
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        nachbar = verschieben(runde)
        await dazu(runde.chronist, zustand(runde.kanal), zustand(nachbar))
        await dazu(runde.mira, zustand(), zustand(runde.kanal))
        await ruhen()
        # Der neue Lauf beginnt im Ursprungskanal — und ein Ereignis reißt ihn nicht ab.
        await befehl(bot, "start")(FakeCtx(runde.mira))
        zweite = runde.kanal.verbindung
        await dazu(runde.mira, zustand(), zustand(runde.kanal))
        await ruhen()
        return zweite

    zweite = asyncio.run(ablauf())

    assert len(thread.geschrieben) == 1
    assert zweite.schneidet
    assert not zweite.getrennt


def test_der_gescheiterte_abschied_nach_dem_verschieben_sagt_es(
    konfiguration, sitzung_im_thread, ohne_espeak, runde, kurze_frist
):
    """Auch hier gilt: lieber ein Satz zu viel als ein Lauf, von dem niemand mehr weiß."""
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.trennen_stolpert = True
        nachbar = verschieben(runde)
        await bot.ereignisse["on_voice_state_update"](
            runde.chronist, zustand(runde.kanal), zustand(nachbar)
        )
        await ruhen()

    asyncio.run(ablauf())

    assert thread.geschrieben == [gateway.VERSCHOBEN_GESCHEITERT.format(kanal=runde.kanal.name)]


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


def test_das_langsamere_netz_meldet_kein_zweites_ende(konfiguration, sitzung_im_thread, pycord):
    """Wer leer zurückbekommt, war der zweite — und der Satz gehört dem ersten.

    ``_mitschnitt_beenden`` gibt leer zurück, wenn ein anderer den Lauf schon beansprucht
    hat. Ein zweiter Satz behauptete ein zweites Ende und schickte zu einem
    ``/aufnahme stop``, das »keine Aufnahme« antwortet.
    """
    bot = gateway.baue(konfiguration)
    thread = FakeTextkanal()
    bot.kanaele[THREAD] = thread
    aufnahme = Aufnahme(konfiguration, unsere_runde(konfiguration), sitzung_im_thread, KANAL)

    asyncio.run(gateway._beenden_und_sagen(bot, gateway._Lauf(), aufnahme, "beendet", "kaputt"))

    assert thread.geschrieben == []


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

    # Gegenprobe zum Test darunter: die heutige Hilfe ist **eine** Antwort.
    (antwort,) = ctx.antworten
    for satzteil in ("/aufnahme start", "/aufnahme stop", "Ansage", "verlässt"):
        assert satzteil in antwort


def test_eine_zu_lange_hilfe_wird_nachgereicht_statt_abgewiesen(konfiguration, runde, monkeypatch):
    """Auch die Antwort auf einen Befehl hängt an Discords 2000 Zeichen — sie wächst mit."""
    lang = gateway.HILFE.replace(gateway.BEFEHLE, gateway.BEFEHLE * 4)
    monkeypatch.setattr(gateway, "HILFE", lang)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "hilfe")(ctx))

    assert len(ctx.antworten) > 1
    assert all(len(antwort) <= grenzen.NACHRICHT for antwort in ctx.antworten)
    assert "".join(ctx.antworten) == lang


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


def echte_senke(konfiguration, sitzung_id, stimme=None):
    aufnahme = Aufnahme(konfiguration, unsere_runde(konfiguration), sitzung_id, KANAL)
    aufnahme.ansage_protokollieren((MIRA,))
    return aufnahme, gateway._senke(stimme or FakeStimme(), aufnahme)


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


def test_jedes_feld_der_slash_befehle_traegt_eine_klasse_und_keine_zeichenkette(tmp_path):
    """Gegen das **echte** py-cord, nicht gegen ein Doppel — hier lag die Lücke.

    ``gateway.py`` hat ``from __future__ import annotations``; damit ist jede Annotation
    zur Laufzeit eine Zeichenkette. py-cord liest den Typ eines Feldes daraus, legt
    ``Option("str")`` an und stirbt beim **ersten Aufruf** an ``issubclass() arg 1 must be
    a class``. Discord zeigt dann »Die Anwendung reagiert nicht«.

    Kein Doppel konnte das sehen: die Tests rufen die Befehlsfunktionen direkt auf und
    überspringen py-cords Options-Maschinerie ganz. Am 2026-08-11 fiel es deshalb erst in
    der ersten echten Sitzung auf.
    """
    pytest.importorskip("discord")
    config = Config(data_dir=tmp_path, recordings_dir=tmp_path / "aufnahmen")

    # py-cord greift beim Bauen nach der Ereignisschleife; in der vollen Suite hat ein
    # vorheriger Lauf sie geschlossen.
    schleife = asyncio.new_event_loop()
    asyncio.set_event_loop(schleife)
    try:
        bot = gateway.baue(config)
    finally:
        asyncio.set_event_loop(None)
        schleife.close()

    def felder(befehl):
        for feld in getattr(befehl, "options", ()):
            yield befehl.qualified_name, feld
        for unter in getattr(befehl, "subcommands", ()):
            yield from felder(unter)

    geprueft = []
    for befehl in bot.pending_application_commands:
        for name, feld in felder(befehl):
            geprueft.append(f"{name}.{feld.name}")
            assert isinstance(feld._raw_type, (type, tuple)), (
                f"{name}.{feld.name}: {feld._raw_type!r} ist keine Klasse — "
                "py-cord stirbt damit beim ersten Aufruf"
            )
            assert not isinstance(feld._raw_type, str)

    assert geprueft, "kein einziges Feld gefunden — die Prüfung liefe ins Leere"


def test_die_senke_haengt_an_ihrem_sprachclient(konfiguration, sitzung_id, ohne_espeak, runde):
    """Ohne diese Verdrahtung stirbt der Empfaenger beim ersten Paket.

    ``discord.sinks.Sink.client`` liest ``self.vc``, gesetzt wird es von ``Sink.init``.
    py-cord 2.8.1 ruft das im Empfangspfad **nie** — die Zeile ``sink._client =
    self.client`` steht in ``voice/receive/reader.py`` auskommentiert. ``opus.py`` prueft
    es aber mit einem ``assert``, sobald das erste Paket ankommt.

    Am 2026-08-11 hat die erste echte Runde deshalb keine einzige Spur bekommen: der
    Empfaenger fiel nach 25 Sekunden mit einem nackten ``AssertionError`` um und beendete
    den Mitschnitt von sich aus.
    """
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    verbindung = runde.kanal.verbindung

    assert verbindung.schneidet, "es wird gar nicht mitgeschnitten — der Test liefe ins Leere"
    assert verbindung.senke is not None
    assert verbindung.senke.client is verbindung, (
        "die Senke kennt ihren Sprachclient nicht — py-cord stirbt damit beim ersten Paket"
    )


def test_dem_gateway_wird_dave_fassung_null_gemeldet(konfiguration, pycord):
    """Sonst kommt der Ton verschluesselt an und der Opus-Dekoder sieht Rauschen.

    py-cord schickt ``max_dave_protocol_version`` aus ``discord.voice.state``. Steht dort
    etwas groesseres als 0, handelt Discord DAVE aus — und ``opus.py`` dekodiert erst und
    entschluesselt danach. Am 2026-08-11 endete die erste echte Runde deshalb mit
    »corrupted stream« und ohne eine einzige Spur.
    """
    zustand = types.SimpleNamespace(DAVE_PROTOCOL_VERSION=1)
    pycord.voice = types.SimpleNamespace(state=zustand)

    gateway.baue(konfiguration)

    assert zustand.DAVE_PROTOCOL_VERSION == 0


def test_ohne_dave_im_paket_wird_nichts_angefasst(konfiguration, pycord):
    """Der Riegel greift nur, wenn er etwas vorfindet — sonst bleibt py-cord unberuehrt."""
    zustand = types.SimpleNamespace(DAVE_PROTOCOL_VERSION=0)
    pycord.voice = types.SimpleNamespace(state=zustand)

    gateway.baue(konfiguration)

    assert zustand.DAVE_PROTOCOL_VERSION == 0
