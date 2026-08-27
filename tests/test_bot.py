"""Der Aufnahme-Bot — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Kein Test spricht mit discord.com, und keiner braucht die Sprach-Abhängigkeiten: das
Gegenüber sind ein paar Dutzend Zeilen Attrappe. Der Token in diesen Tests ist erfunden
und steht nur hier.

Die beiden Sätze, die dieser Suite ihren Sinn geben: **vor der Ansage wird nichts
geschrieben**, und **was angesagt wurde, steht im Wortlaut im Protokoll.**
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import io
import logging
import re
import sqlite3
import sys
import threading
import time
import tomllib
import types
import wave
from array import array
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests
from conftest import GRENZE, warte_bis
from conftest import runde as erste_runde
from flask import Flask, jsonify
from mocks import adresse, serve

import chronicle.bot.__main__ as entry
from chronicle import (
    consent,
    db,
    jobs,
    lebenszyklus,
    nightly,
    notes,
    people,
    protocol,
    recordings,
    settings,
)
from chronicle import runde as runden
from chronicle import sprache as sprachen
from chronicle.bot import (
    BotFehler,
    BotHaelt,
    ansage,
    chronik,
    erinnern,
    gateway,
    healthz,
    recorder,
)
from chronicle.bot.ansage import AnsageFehlt
from chronicle.bot.recorder import Aufnahme, Kanal, NichtAngesagt
from chronicle.compose import client as ollama
from chronicle.config import DEFAULT_TTS_URL, Config
from chronicle.discord import grenzen
from chronicle.foundry import store as foundry_store
from chronicle.foundry.model import Player, WorldSnapshot
from chronicle.transcribe import merge, service
from chronicle.transcribe.client import Segment

TOKEN = "aufnahme-bot-token-nur-fuer-den-test"

WURZEL = Path(__file__).resolve().parent.parent

KANAL = Kanal(guild_id="11", id="77", name="Runde")

MIRA = consent.Member(id="4001", name="Mira")
BROK = consent.Member(id="4002", name="Brok")
SPAET = consent.Member(id="4003", name="Aelin")

# Die zweite Gilde: eigene Kennung, eigene Runde, eigene Leute. Ohne sie gab es in dieser
# Suite keinen zweiten Akteur — und damit keinen Test für die Trennung zwischen Runden.
NACHBARGILDE = "12"
SILDAR = consent.Member(id="4101", name="Sildar")

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


def spricht(text: str, ziel: Path, **rest) -> None:
    _mono_wav(str(ziel), ESPEAK_RATE, ESPEAK_WERT)


def verweigert(text: str, ziel: Path, **rest) -> None:
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


@pytest.fixture(autouse=True)
def keine_nacht_nebenher(request, monkeypatch):
    """``gateway.run`` stellt den nächtlichen Zeitplan an — hier soll er nichts tun.

    Sonst führe jeder Test, der den Bot startet, zwischen vier und fünf Uhr morgens
    nebenher eine echte Nacht mit: derselbe Test wäre je nach Tageszeit ein anderer. Wer
    die Nacht prüfen will, fordert ``nachtwache`` an und bekommt den echten Faden.
    """
    if "nachtwache" in request.fixturenames:
        return
    monkeypatch.setattr(nightly, "starten", lambda config, **rest: None)


@pytest.fixture
def nachtwache(monkeypatch):
    """Der echte Zeitplan-Faden — durch die Nähte, die ``betreiben`` dafür schon hat.

    An der Kette wird nichts gestubbt; der Faden schläft nur auf einem Ereignis statt auf
    einer Minute, damit er am Ende des Tests wirklich aufhört.
    """
    echt = nightly.betreiben
    halt = threading.Event()
    monkeypatch.setattr(
        nightly,
        "betreiben",
        lambda config, **_rest: echt(config, schlafen=halt.wait, weiter=lambda: not halt.is_set()),
    )
    yield
    halt.set()


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
    """Die **laufende** Sitzung des Abends.

    ``laeuft=True`` seit #272: ``/session start`` legt seither selbst eine an, wenn keine
    offen ist. Eine geschlossene Sitzung hieße hier also »fang einen neuen Abend an« —
    gemeint ist aber der Mitschnitt in dem, der schon läuft.
    """
    return notes.create_session(unsere_runde(konfiguration), played_on="2026-08-06", laeuft=True)


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
        self.schneidet = False
        # Wen Discord auf Nachfrage hergibt — leer heißt: keiner ist nachzuschlagen.
        self.nachschlag: dict[str, str] = {}
        self.gefragt: list[tuple[str, ...]] = []

    def mitglieder(self):
        return tuple(self._anwesend)

    async def namen(self, kennungen):
        self.gefragt.append(tuple(kennungen))
        return {k: self.nachschlag[k] for k in kennungen if k in self.nachschlag}

    def im_kanal(self):
        return not self.verschoben

    async def ansagen(self, datei):
        self.ablauf.append("ansage")
        self.angesagt.append(datei)

    def mitschneiden(self, aufnahme):
        self.ablauf.append("mitschnitt")
        self.aufnahme = aufnahme
        self.schneidet = True

    def mitschnitt_beenden(self):
        self.ablauf.append("mitschnitt-ende")
        lief_noch, self.schneidet = self.schneidet, False
        return lief_noch

    async def trennen(self):
        self.ablauf.append("getrennt")


# -- Die Ansage ------------------------------------------------------------------------


# Was in **jeder** Sprache in der Ansage stehen muss. Sie trägt §201 StGB und tut das nur,
# wenn die Anwesenden sie verstehen — eine Sprache ohne diese Sätze wäre keine Ansage (#268).
TRAGENDE_ANSAGE = {
    sprachen.DEUTSCH: ("Ab jetzt wird dieses Gespräch aufgezeichnet", "verlässt", "Sprachkanal"),
    sprachen.ENGLISCH: ("this conversation is being recorded", "leave", "voice channel"),
}

TRAGENDE_BEDINGUNGEN = {
    sprachen.DEUTSCH: ("Sitzungsprotokoll", "Server der Gruppe", "einverstanden", "Tonspur"),
    sprachen.ENGLISCH: ("session log", "group's own server", "you agree", "audio track"),
}

FRIST_IM_SATZ = {sprachen.DEUTSCH: "Tage", sprachen.ENGLISCH: "days"}


@pytest.mark.parametrize("inhaltssprache", sprachen.SPRACHEN)
def test_die_gesprochene_ansage_sagt_das_tragende_und_bleibt_kurz(inhaltssprache):
    """Ab jetzt wird aufgezeichnet, so kommt man raus — der Rest steht im Kanal.

    Die Länge ist hier kein Geschmack: die Runde wartet, während die Ansage läuft, und
    den ausführlichen Text hat sie als ``gateway.VORSTELLUNG`` schon vor sich.

    Geprüft wird **jede** Sprache: eine, die den tragenden Satz nicht sagt, wäre keine
    Einwilligung, sondern ein Hinweis.
    """
    gesagt = ansage.text_fuer(inhaltssprache)
    for satzteil in TRAGENDE_ANSAGE[inhaltssprache]:
        assert satzteil in gesagt
    assert "channel" in gesagt or "Kanal" in gesagt
    assert len(gesagt.split()) <= 35


@pytest.mark.parametrize("inhaltssprache", sprachen.SPRACHEN)
def test_das_protokoll_belegt_worueber_eingewilligt_wurde(inhaltssprache):
    """Der kurze Satz belegt *dass*, die Bedingungen daneben belegen *worüber*."""
    protokoll = ansage.protokoll_fuer(inhaltssprache)
    assert ansage.text_fuer(inhaltssprache) in protokoll
    assert ansage.bedingungen_fuer(inhaltssprache) in protokoll
    for satzteil in TRAGENDE_BEDINGUNGEN[inhaltssprache]:
        assert satzteil in protokoll


@pytest.mark.parametrize("inhaltssprache", sprachen.SPRACHEN)
def test_die_zugesagte_frist_ist_die_frist_aus_dem_code(inhaltssprache):
    # Der Satz darf sich nicht von dem entfernen, was ``recordings.sweep`` durchsetzt.
    protokoll = ansage.protokoll_fuer(inhaltssprache)
    assert f"{recordings.RETENTION_TAGE} {FRIST_IM_SATZ[inhaltssprache]}" in protokoll


def test_die_ansage_einer_runde_folgt_ihrer_einstellung(konfiguration):
    """Die eine Entscheidung, für die es die Einstellung gibt (#268)."""
    unsere = unsere_runde(konfiguration)
    assert ansage.text_fuer(settings.sprache(unsere)) == ansage.text_fuer(sprachen.ENGLISCH)
    settings.save_sprache(unsere, sprachen.DEUTSCH)
    assert ansage.text_fuer(settings.sprache(unsere)) == ansage.text_fuer(sprachen.DEUTSCH)
    # Und die Datei dazu ist eine andere: der Fingerabdruck hängt am Wortlaut.
    assert ansage.kennung(ansage.text_fuer(sprachen.DEUTSCH)) != ansage.kennung(
        ansage.text_fuer(sprachen.ENGLISCH)
    )


def test_die_espeak_stimme_folgt_der_sprache():
    """Ohne ``-v`` läse espeak-ng den deutschen Satz mit englischer Aussprache vor."""
    assert ansage.stimme_fuer(sprachen.DEUTSCH) != ansage.stimme_fuer(sprachen.ENGLISCH)
    assert ansage.stimme_fuer("kli-ngo-nisch") == ansage.stimme_fuer(sprachen.DEFAULT)


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
    assert laeufe == [ansage.text_fuer(sprachen.DEFAULT)]
    assert ansage.kennung(ansage.text_fuer(sprachen.DEFAULT)) in erste.name


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
    assert aufruf.json["input"] == ansage.text_fuer(sprachen.DEFAULT)
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


def test_der_nullpunkt_der_sitzungsuhr_wird_festgehalten_und_nicht_geschaetzt(
    konfiguration, sitzung_id, ohne_espeak
):
    """Ohne diesen Anker ließe sich keine Äußerung einer Szene zuordnen, ohne zu raten (#140).

    Er entsteht **nach** der Ansage, weil erst dann mitgeschnitten wird — Millisekunde 0
    der Spuren liegt auf diesem Moment und nicht auf dem Befehl davor.
    """
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))
    aufnahme.schreiben(MIRA, stille(48))
    aufnahme.beenden()

    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.started_at == aufnahme.begonnen_at
    assert spur.started_at is not None
    # Der Beginn liegt vor dem Einreihen: dazwischen liegt die ganze Aufnahme.
    assert spur.started_at <= spur.uploaded_at


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
    assert eintrag.text == ansage.protokoll_fuer(sprachen.DEFAULT)
    assert eintrag.announced_at
    assert [(wer.id, wer.name) for wer in eintrag.members] == [
        (MIRA.id, MIRA.name),
        (BROK.id, BROK.name),
    ]


def test_vor_der_ansage_wird_keine_spur_geschrieben(konfiguration, sitzung_id):
    aufnahme = Aufnahme(
        konfiguration,
        unsere_runde(konfiguration),
        sitzung_id,
        KANAL,
        inhaltssprache=sprachen.DEFAULT,
    )

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


def test_die_begonnene_spur_steht_ohne_namen_im_log(konfiguration, sitzung_id, ohne_espeak, caplog):
    """Der Anzeigename ist der Stamm des Dateinamens (#194) — beide gehören nicht ins Log
    des Betreibers. Gezählt statt benannt: welcher Abend, die wievielte Spur.

    Geprüft wird auf DEBUG und über den ganzen Lauf, Abschluss eingeschlossen: ein
    Traceback, der den Pfad mitbrächte, stünde ebenso in ``caplog.text`` und fiele hier auf.
    """
    caplog.set_level(logging.DEBUG)
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration))
    )

    aufnahme.schreiben(MIRA, stille(480))
    aufnahme.schreiben(BROK, stille(960))
    aufnahme.beenden()

    assert f"Sitzung {sitzung_id}: 1. Spur begonnen" in caplog.text
    assert f"Sitzung {sitzung_id}: 2. Spur begonnen" in caplog.text
    assert MIRA.name not in caplog.text
    assert BROK.name not in caplog.text
    assert ".wav" not in caplog.text


def test_der_kanalname_steht_weder_bei_der_einwilligung_noch_beim_empfangstest_im_log(
    konfiguration, sitzung_id, ohne_espeak, kurze_probe, caplog
):
    """Ein Kanalname ist kein Personenname, beschreibt aber die Struktur einer fremden
    Gilde (#206) — und ins Log des Betreibers gehört er so wenig wie ein Sprechername.

    Der Empfangstest geht durch ``starten``, also stehen beide Zeilen in einem Lauf. Was
    an ihre Stelle tritt, wird mitgeprüft: eine Zeile, die nichts mehr identifiziert, wäre
    kein Fortschritt, sondern ein Loch im Reparaturmantel.

    Im **Nachweis** bleibt der Name unangetastet — dort belegt er, wo die Ansage lief, und
    das ist die Rechtsgrundlage nach §201.
    """
    caplog.set_level(logging.DEBUG)
    stimme = FakeStimme()
    stimme.kanal = Kanal(guild_id=KANAL.guild_id, id="78", name="Am-Runden-Tisch")

    asyncio.run(recorder.pruefen(konfiguration, stimme, unsere_runde(konfiguration)))

    assert stimme.kanal.name not in caplog.text
    (nachweis,) = consent.for_session(unsere_runde(konfiguration), sitzung_id)
    assert nachweis.channel_name == stimme.kanal.name
    assert f"Einwilligung {consent.ANSAGE} für Sitzung {sitzung_id}" in caplog.text
    assert f"Nachweis {nachweis.id}" in caplog.text
    assert f"Empfangstest für Sitzung {sitzung_id}" in caplog.text


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
    assert spaet.text == ansage.protokoll_fuer(sprachen.DEFAULT)


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
    assert "queued for the batch" in meldungen[0]


# Wer im Sprachkanal saß, ohne dem Bot je begegnet zu sein: py-cord reicht dann ein
# blankes ``Object`` durch, ``_mitglied`` setzt den Platzhalter, und die Kennung steht
# trotzdem an der Spur. Genau dieser Fall ist #250.
NAMENLOS = consent.Member(id="4004", name=consent.UNBEKANNT)


def test_der_namenlose_sprecher_wird_beim_einreihen_nachgeschlagen(
    konfiguration, sitzung_id, ohne_espeak, caplog
):
    """Ein Aufruf je Sprecher statt der Mitgliederliste jeder Gilde — und erst hier.

    Beim Verbinden stünde für jeden im Kanal ein Name da, auch für die, von denen nie eine
    Sekunde Ton entsteht. Beim Einreihen *ist* die Stimme zu Ton geworden.
    """
    stimme = FakeStimme()
    stimme.nachschlag = {"4004": "Samuel"}
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))
    aufnahme.schreiben(NAMENLOS, stille(480))

    with caplog.at_level(logging.INFO):
        asyncio.run(recorder.stoppen(stimme, aufnahme))

    spuren = recordings.pending(unsere_runde(konfiguration))
    assert [(spur.discord_user_id, spur.discord_name) for spur in spuren] == [("4004", "Samuel")]
    assert stimme.gefragt == [("4004",)]
    # Und kein Name im Log — die Zeile sagt, dass nachgetragen wurde, nicht wen.
    assert "Samuel" not in caplog.text


def test_wer_schon_einen_namen_hatte_wird_nicht_noch_einmal_gefragt(
    konfiguration, sitzung_id, ohne_espeak
):
    stimme = FakeStimme()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))
    aufnahme.schreiben(MIRA, stille(480))

    asyncio.run(recorder.stoppen(stimme, aufnahme))

    spuren = recordings.pending(unsere_runde(konfiguration))
    assert [spur.discord_name for spur in spuren] == ["Mira"]
    assert stimme.gefragt == []


def test_ein_gescheitertes_nachschlagen_haelt_den_abschluss_nicht_auf(
    konfiguration, sitzung_id, ohne_espeak
):
    """Discord schweigt, das Konto ist gelöscht — die Spur liegt trotzdem eingereiht da.

    Und sie heißt danach nicht nach ihrer Datei: die Zeile bleibt leer, und die Chronik
    schreibt das Wort »unbekannt«.
    """

    class Schweigt(FakeStimme):
        async def namen(self, kennungen):
            raise RuntimeError("Discord antwortet nicht")

    stimme = Schweigt()
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))
    aufnahme.schreiben(NAMENLOS, stille(480))

    meldungen = asyncio.run(recorder.stoppen(stimme, aufnahme))

    assert "queued for the batch" in meldungen[0]
    assert [spur.discord_name for spur in recordings.pending(unsere_runde(konfiguration))] == [None]


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
    assert all("queued for the batch" in meldung for meldung in meldungen)


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
    assert all("queued for the batch" in meldung for meldung in meldungen)


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


# -- Der Schnitt in Häppchen (#217) ----------------------------------------------------
#
# Die harte Bedingung steht in ``test_die_verschraenkung_ist_mit_haeppchen_dieselbe``: die
# Zeiten müssen dieselben sein wie ohne Schnitt. Alles andere hier hängt daran.

# Ein Häppchen von einer Sekunde — die echten dreißig Minuten wären 345 MB Stille je Test.
SEKUNDE_BYTES = recorder.BYTES_JE_SEKUNDE

# Wie py-cord liefert: 20 ms je Paket. Ein Häppchen von einer Sekunde endet damit auf einer
# Paketgrenze, so wie im Betrieb auch — geschnitten wird zwischen zwei Paketen, nie mitten
# in eines.
PAKET_RAHMEN = ansage.RATE // 50
PAKETE_JE_SEKUNDE = 50


def paket(wert: int) -> bytes:
    """Zwanzig Millisekunden auf einem Pegel — ``0`` ist die Stille, die py-cord einfüllt."""
    return array("h", [wert] * (PAKET_RAHMEN * ansage.KANAELE)).tobytes()


class Inselerkenner:
    """Ein Modell, das die Sprechinseln einer Datei liest — **relativ zu ihr**.

    Genau das tut der echte Erkenner: er weiß nichts von der Sitzung, sondern zählt ab dem
    Anfang dessen, was ihm gereicht wurde. Ein Häppchen aus der Mitte des Abends fängt für
    ihn wieder bei null an — und daran hängt der ganze Beweis.

    Der Text einer Insel kommt aus ihrem **Pegel** und nicht aus ihrer Lage: nur so ist er
    in beiden Läufen derselbe und die Gegenüberstellung überhaupt eine.
    """

    name = "inselerkenner"

    def transcribe(self, audio_path, *, hotwords=(), sprache="en"):
        with wave.open(str(audio_path), "rb") as datei:
            rate = datei.getframerate()
            werte = array("h", datei.readframes(datei.getnframes()))[:: ansage.KANAELE]
        if not len(werte):
            return
        anfang = 0
        for stelle in range(1, len(werte) + 1):
            if stelle < len(werte) and werte[stelle] == werte[anfang]:
                continue
            if werte[anfang]:
                yield Segment(start=anfang / rate, end=stelle / rate, text=f"Insel {werte[anfang]}")
            anfang = stelle


# Drei Sekunden Abend, drei Sprechinseln, und keine über einer Häppchengrenze: die Zeitachse
# wird hier geprüft, nicht der Preis eines Schnitts mitten im Wort. Der steht daneben in
# ``test_ein_schnitt_mitten_im_wort_zerreisst_die_zeitachse_nicht``.
ABEND_PAKETE = 150
INSELN = {
    MIRA.id: {**dict.fromkeys(range(25, 40), 1000), **dict.fromkeys(range(125, 140), 3000)},
    BROK.id: dict.fromkeys(range(60, 80), 2000),
}


def abend(konfiguration, sitzung_id, *, inseln=None) -> tuple:
    """Einen Abend mitschneiden, verschriften und die Unterhaltung zurückgeben."""
    unsere = unsere_runde(konfiguration)
    aufnahme = asyncio.run(recorder.starten(konfiguration, FakeStimme(), unsere))
    wer_wann = INSELN if inseln is None else inseln
    for nummer in range(ABEND_PAKETE):
        for wer in (MIRA, BROK):
            aufnahme.schreiben(wer, paket(wer_wann[wer.id].get(nummer, 0)))
    aufnahme.beenden()
    service.run_queue(konfiguration, unsere, model=Inselerkenner())
    return merge.conversation(unsere, sitzung_id, mit_uhr=True)


@pytest.fixture
def haeppchen(monkeypatch):
    """Die Häppchenlänge für einen Test — danach steht wieder die echte da."""

    def setzen(bytes_je_haeppchen: int) -> int:
        monkeypatch.setattr(recorder, "HAEPPCHEN_BYTES", bytes_je_haeppchen)
        return bytes_je_haeppchen

    return setzen


def test_eine_lange_spur_wird_in_haeppchen_geschnitten(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Drei Sekunden Abend, Häppchen von einer Sekunde: drei Zeilen je Sprecher.

    Die Gegenprobe steht darunter — mit der echten Länge bleibt es bei einer.
    """
    haeppchen(SEKUNDE_BYTES)

    abend(konfiguration, sitzung_id)

    spuren = recordings.for_session(unsere_runde(konfiguration), sitzung_id)
    je_sprecher = {}
    for spur in spuren:
        je_sprecher.setdefault(spur.discord_user_id, []).append(spur.offset_ms)
    assert je_sprecher == {MIRA.id: [0, 1000, 2000], BROK.id: [0, 1000, 2000]}
    assert len({spur.filename for spur in spuren}) == 6


def test_ohne_haeppchengrenze_bleibt_es_bei_einer_datei_je_sprecher(
    konfiguration, sitzung_id, ohne_espeak
):
    """Die Gegenprobe: dieselben drei Sekunden, aber die echten dreißig Minuten Grenze."""
    abend(konfiguration, sitzung_id)

    spuren = recordings.for_session(unsere_runde(konfiguration), sitzung_id)
    assert sorted(spur.offset_ms for spur in spuren) == [0, 0]


def test_der_versatz_ist_gerechnet_und_nicht_nach_der_wanduhr_geraten(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Bytes sind die Zeit: py-cord füllt die Pausen auf, also *ist* die Position die Uhr.

    Geprüft an einer Grenze, die nicht auf einer runden Sekunde liegt — eine Wanduhr käme
    hier auf einen anderen Wert, und der wäre still falsch.
    """
    krumm = haeppchen(SEKUNDE_BYTES // 2 + PAKET_RAHMEN * ansage.KANAELE * ansage.BREITE)

    abend(konfiguration, sitzung_id)

    versaetze = sorted(
        spur.offset_ms
        for spur in recordings.for_session(unsere_runde(konfiguration), sitzung_id)
        if spur.discord_user_id == MIRA.id
    )
    schritt = recorder._millisekunden(krumm)
    assert schritt == 520
    assert versaetze == [0, schritt, 2 * schritt, 3 * schritt, 4 * schritt, 5 * schritt]


def test_die_verschraenkung_ist_mit_haeppchen_dieselbe(tmp_path, ohne_espeak, haeppchen):
    """**Die Bedingung, an der alles hängt.** Ausgeführt belegt, nicht der Doku geglaubt.

    Zwei Sprecher, bekannte Sprechinseln, derselbe Abend zweimal: einmal in einem Stück,
    einmal in drei Häppchen je Sprecher. Reihenfolge, Sprecher, Text **und die
    Millisekunden** müssen dieselben sein. Sind sie es nicht, ist die Verschränkung
    zerfallen und jede Äußerung fiele in die falsche Szene — still.
    """

    def lauf(name: str):
        konfiguration = Config(
            discord_bot_token=TOKEN,
            data_dir=tmp_path / name / "daten",
            recordings_dir=tmp_path / name / "aufnahmen",
        )
        sitzung_id = notes.create_session(unsere_runde(konfiguration), played_on="2026-08-06")
        return abend(konfiguration, sitzung_id)

    am_stueck = lauf("ganz")
    haeppchen(SEKUNDE_BYTES)
    zerteilt = lauf("haeppchen")

    assert [(a.start_ms, a.end_ms, a.speaker, a.text) for a in am_stueck] == [
        (500, 800, "Mira", "Insel 1000"),
        (1200, 1600, "Brok", "Insel 2000"),
        (2500, 2800, "Mira", "Insel 3000"),
    ]
    assert zerteilt == am_stueck


def test_ein_schnitt_mitten_im_wort_zerreisst_die_zeitachse_nicht(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Was ein blinder Schnitt kostet — und was er nicht kostet.

    Die Insel liegt über der Häppchengrenze und wird deshalb zu **zwei** Äußerungen statt
    einer; das ist der bezahlte Preis, ein Wort je Häppchen. Bezahlt wird er in Text und
    nicht in Zeit: die beiden Stücke stoßen auf der Sitzungsuhr aneinander, ohne Lücke und
    ohne Überlappung, und sie beginnen dort, wo die ungeteilte Insel begann.
    """
    haeppchen(SEKUNDE_BYTES)
    ueber_die_grenze = {
        MIRA.id: dict.fromkeys(range(45, 56), 1000),
        BROK.id: {},
    }

    aussagen = abend(konfiguration, sitzung_id, inseln=ueber_die_grenze)

    # ``_verschraenken`` fasst zwei Äußerungen desselben Sprechers zusammen; die Zeiten
    # dahinter bleiben ablesbar an Anfang und Ende.
    (eine,) = aussagen
    assert (eine.start_ms, eine.end_ms) == (900, 1120)
    assert eine.text == "Insel 1000 Insel 1000"


def test_ein_haeppchen_steht_in_der_warteschlange_bevor_die_sitzung_endet(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Der Ertrag des ganzen Umbaus: die Arbeit läuft **während** der Sitzung.

    Stünde das volle Häppchen erst bei ``/session pause`` in der Warteschlange, hinge die
    Verschriftung weiter vollständig hinter dem Abend — und dieser Schnitt brächte nichts.
    """
    haeppchen(SEKUNDE_BYTES)
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration))
    )

    for _ in range(PAKETE_JE_SEKUNDE + 1):
        aufnahme.schreiben(MIRA, paket(1000))

    wartend = recordings.pending(unsere_runde(konfiguration))
    assert [spur.offset_ms for spur in wartend] == [0]
    assert (konfiguration.recordings_dir / wartend[0].filename).is_file()


def test_am_ende_steht_eine_zeile_je_sprecher_und_nicht_je_haeppchen(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Vierzig gleichlautende Zeilen im Kanal sagen weniger als fünf."""
    haeppchen(SEKUNDE_BYTES)
    aufnahme = asyncio.run(
        recorder.starten(konfiguration, FakeStimme(), unsere_runde(konfiguration))
    )
    for _ in range(3 * PAKETE_JE_SEKUNDE):
        for wer in (MIRA, BROK):
            aufnahme.schreiben(wer, paket(1000))

    meldungen = aufnahme.beenden()

    assert len(meldungen) == 2
    assert sorted(meldungen) == sorted(
        recorder.EINGEREIHT_HAEPPCHEN.format(spur=name, anzahl=3, sitzung=sitzung_id)
        for name in (MIRA.name, BROK.name)
    )


def test_ein_haeppchen_ohne_sprache_meldet_nichts_und_scheitert_nicht(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Wer eine Weile schweigt, ist der Normalfall — und kein Ereignis.

    Von sechs Häppchen tragen drei eine Äußerung. Gemeldet werden drei, gescheitert ist
    keines, und in der Datenbank steht kein erfundener Satz.
    """
    haeppchen(SEKUNDE_BYTES)
    unsere = unsere_runde(konfiguration)
    aufnahme = asyncio.run(recorder.starten(konfiguration, FakeStimme(), unsere))
    for nummer in range(ABEND_PAKETE):
        for wer in (MIRA, BROK):
            aufnahme.schreiben(wer, paket(INSELN[wer.id].get(nummer, 0)))
    aufnahme.beenden()

    meldungen = service.run_queue(konfiguration, unsere, model=Inselerkenner())

    assert len(meldungen) == 3
    spuren = recordings.for_session(unsere, sitzung_id)
    assert len(spuren) == 6
    assert {spur.status for spur in spuren} == {recordings.FERTIG}
    assert [spur.text for spur in spuren if spur.text] != []
    assert sum(1 for spur in spuren if not spur.text) == 3


def test_die_frist_meldet_je_sitzung_und_nicht_je_haeppchen(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Aus einer Spur werden viele — die Zusage gilt jeder Datei, die Meldung bleibt eine."""
    haeppchen(SEKUNDE_BYTES)
    abend(konfiguration, sitzung_id)
    unsere = unsere_runde(konfiguration)
    verbindung = db.connect(konfiguration.database_path)
    try:
        with verbindung:
            verbindung.execute("UPDATE recording SET uploaded_at = '2020-01-01T00:00:00+00:00'")
    finally:
        verbindung.close()

    meldungen = recordings.sweep(konfiguration, unsere)

    assert meldungen == (
        recordings.NACH_FRIST.format(
            sitzung=sitzung_id, was="6 recordings", tage=recordings.RETENTION_TAGE
        ),
    )
    assert list(konfiguration.recordings_dir.glob("sitzung*")) == []


def test_das_loeschen_der_sitzung_nimmt_alle_haeppchen_und_nur_ihre(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Der Glob aus #171 muss alle Häppchen finden — und keines der Nachbarsitzung.

    ``sitzung1-*`` darf ``sitzung10-*`` nicht treffen; mit Häppchen sind es mehr Dateien,
    die Grenze bleibt dieselbe.
    """
    haeppchen(SEKUNDE_BYTES)
    unsere = unsere_runde(konfiguration)
    abend(konfiguration, sitzung_id)
    nachbar = konfiguration.recordings_dir / f"sitzung{sitzung_id}0-20260806T2030-Mira.wav"
    nachbar.write_bytes(b"gehoert einem anderen Abend")
    marke = notes.sitzungsmarke(notes.session(unsere, sitzung_id))

    gefunden = notes.delete_session(konfiguration, unsere, marke)

    assert gefunden.audio == 6
    assert list(konfiguration.recordings_dir.glob(f"sitzung{sitzung_id}-*")) == []
    assert nachbar.is_file()


def test_ein_fremdes_haeppchen_wird_nicht_mit_zurueckgestellt(
    konfiguration, sitzung_id, ohne_espeak, haeppchen
):
    """Besitzer und Herzschlag gelten je Datei — mit Häppchen sind es nur mehr Zeilen.

    Das eine Häppchen gehört einem Nachbarprozess, der eben noch geatmet hat; das andere
    einem, der es nicht mehr tut. Zurück kommt genau eines.
    """
    haeppchen(SEKUNDE_BYTES)
    unsere = unsere_runde(konfiguration)
    abend(konfiguration, sitzung_id)
    spuren = [
        spur
        for spur in recordings.for_session(unsere, sitzung_id)
        if spur.discord_user_id == MIRA.id
    ]
    lebt, verstummt = spuren[0], spuren[1]
    verbindung = db.connect(konfiguration.database_path)
    try:
        with verbindung:
            verbindung.execute(
                "UPDATE recording SET status = ?, besitzer = 'nachbar', herzschlag = ? "
                "WHERE id = ?",
                (recordings.LAEUFT, datetime.now(UTC).isoformat(), lebt.id),
            )
            verbindung.execute(
                "UPDATE recording SET status = ?, besitzer = 'toter-nachbar', herzschlag = ? "
                "WHERE id = ?",
                (
                    recordings.LAEUFT,
                    (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    verstummt.id,
                ),
            )
    finally:
        verbindung.close()

    zurueck = recordings.zurueckstellen(unsere)

    assert zurueck == (verstummt.source,)
    assert recordings.get(unsere, lebt.id).status == recordings.LAEUFT


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
    assert zeile["text"] == ansage.protokoll_fuer(sprachen.DEFAULT)
    assert anwesend == 2


# -- Der Prozess -----------------------------------------------------------------------


def test_ohne_token_startet_der_bot_nicht(tmp_path, monkeypatch, capsys):
    leer = Config(data_dir=tmp_path)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: leer))

    def nie():
        raise AssertionError("ohne Token darf nichts verbinden")

    assert entry.main(gateway=nie) == 0
    assert entry.KEIN_TOKEN in capsys.readouterr().out


def test_der_token_kommt_aus_der_umgebung_und_sonst_nirgendwoher(tmp_path, monkeypatch):
    """Seit #230 gibt es keinen zweiten Ort mehr — ein gepflegter Wert schlägt nichts."""
    aus_der_umgebung = Config(discord_bot_token=TOKEN, data_dir=tmp_path)
    db.init(aus_der_umgebung.database_path)
    settings.save(erste_runde(aus_der_umgebung), {"discord_bot_token": "untergeschoben"})
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


# -- Das Install-Gate ------------------------------------------------------------------


class FakeGate:
    """Das Gate, ohne einen Port zu binden — ``warten`` bliebe sonst für immer stehen."""

    def __init__(self):
        self.gewartet = False
        self.zu = False

    def warten(self):
        self.gewartet = True

    def schliessen(self):
        self.zu = True


@pytest.fixture
def gate():
    server = healthz.starten(0)
    try:
        yield server
    finally:
        server.schliessen()


def _gate_holen(gate, pfad="/healthz"):
    host, port = gate.adresse
    return requests.get(f"http://{host}:{port}{pfad}", timeout=5)


def test_das_install_gate_antwortet_aus_dem_bot_prozess(gate):
    """Der eine Endpunkt, an dem der Dienst hängt — 200, sonst gilt die Box als krank."""
    antwort = _gate_holen(gate)
    assert antwort.status_code == 200
    assert antwort.json() == {"status": "ok"}


def test_das_gate_hoert_nur_auf_der_schleife(gate):
    """#228/#190: der Pod liegt im Host-Netz — ein 0.0.0.0 stünde im ganzen LAN offen.

    Deshalb ist die Adresse keine Einstellung, sondern festgenagelt: es gäbe keinen
    richtigen zweiten Wert. Der Poller der Box fragt ``localhost`` und liegt daneben.
    """
    assert healthz.HOST == "127.0.0.1"
    assert gate.adresse[0] == "127.0.0.1"


def test_das_gate_kennt_nur_seinen_einen_pfad(gate):
    assert _gate_holen(gate, "/einstellungen").status_code == 404


def test_ohne_token_bleibt_das_gate_erreichbar(tmp_path, monkeypatch, capsys):
    """Bei der Erstinstallation ist der fehlende Token der Normalfall.

    Bis #228 endete der Bot hier mit 0 und der Container blieb aus. Jetzt hängt am
    Prozess das Install-Gate: beendete er sich, fände der Poller niemanden und die
    Installation gälte als gescheitert, bevor jemand den Token eintragen konnte.
    """
    leer = Config(data_dir=tmp_path)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: leer))
    offen = FakeGate()

    def nie():
        raise AssertionError("ohne Token darf nichts verbinden")

    assert entry.main(gateway=nie, gesundheit=lambda _config: offen) == 0
    assert offen.gewartet
    assert entry.KEIN_TOKEN in capsys.readouterr().out


def test_ein_haltender_bot_laesst_das_gate_stehen(tmp_path, monkeypatch, capsys):
    """Ein abgelehnter Token heilt kein Neustart — verbunden wird nichts mehr, das Gate bleibt."""
    config = Config(discord_bot_token=TOKEN, data_dir=tmp_path)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    offen = FakeGate()

    def haelt():
        def run(_zugang):
            raise BotHaelt(gateway.TOKEN_ABGELEHNT)

        return run

    assert entry.main(gateway=haelt, gesundheit=lambda _config: offen) == 0
    assert offen.gewartet
    assert TOKEN not in capsys.readouterr().out


def test_eine_echte_stoerung_nimmt_das_gate_mit(tmp_path, monkeypatch):
    """Was ein Neustart heilen kann, endet weiterhin mit 2 — und lässt keinen Port liegen."""
    config = Config(discord_bot_token=TOKEN, data_dir=tmp_path)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    offen = FakeGate()

    def stoert():
        def run(_zugang):
            raise BotFehler("Netz weg")

        return run

    assert entry.main(gateway=stoert, gesundheit=lambda _config: offen) == 2
    assert offen.zu
    assert not offen.gewartet


def test_ohne_port_bindet_der_bot_nichts(tmp_path):
    """Auf einer Entwicklungsmaschine braucht das Gate niemand — die Vorlage setzt den Port."""
    assert entry._gesundheit(Config(data_dir=tmp_path)) is None


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
        self.members = False


class FakePermissions:
    """``discord.Permissions`` — hier nur der Merkzettel, welches Recht verlangt wird."""

    def __init__(self, **rechte):
        self.rechte = rechte


class FakeRechte:
    """Was Discord über ein Mitglied sagt: ``Member.guild_permissions``."""

    def __init__(self, *, manage_guild=False, administrator=False):
        self.manage_guild = manage_guild
        self.administrator = administrator


def mit_vorgaben(funktion):
    """Wie py-cord: ein nicht gegebenes Feld kommt als sein **Wert** an, nicht als ``Option``.

    Im Vorgabewert eines Befehls steht ein fertiges ``Option`` — py-cord liest daraus den
    Typ und löst es auf, bevor der Rumpf es sieht. Solange kein Befehl mit einem Feld
    etwas *tat*, fiel der Unterschied nicht auf; seit ``/session start`` den Titel in die
    Sitzung schreibt (#272), fiele stattdessen der Test um.
    """
    signatur = inspect.signature(funktion)

    @functools.wraps(funktion)
    async def aufgeloest(*args, **rest):
        gebunden = signatur.bind(*args, **rest)
        gebunden.apply_defaults()
        for name, wert in list(gebunden.arguments.items()):
            if hasattr(wert, "input_type") and hasattr(wert, "default"):
                gebunden.arguments[name] = wert.default
        return await funktion(*gebunden.args, **gebunden.kwargs)

    return aufgeloest


class FakeGruppe:
    def __init__(self, beschreibung):
        self.beschreibung = beschreibung
        self.befehle = {}

    def command(self, *, name, description=""):
        def nimm(funktion):
            self.befehle[name] = mit_vorgaben(funktion)
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
        # Wen py-cord aus seinem Zwischenspeicher hergibt, wenn jemand angeschrieben wird.
        self.nutzer = {}
        self.token = None
        # Was py-cord beim Anmelden aus dem READY-Rahmen zwischenspeichert. Leer heißt
        # hier: in keiner Gilde — nicht »noch nicht bekannt«.
        self.guilds = ()
        self.gilden = {}
        # Die Ansichten, die einen Neustart überleben sollen — py-cord verdrahtet sie
        # über ihre ``custom_id`` neu an die Nachrichten, die schon im Kanal stehen.
        self.angeschlossen = []

    def get_channel(self, kennung):
        return self.kanaele.get(kennung)

    def get_guild(self, kennung):
        return self.gilden.get(kennung)

    def add_view(self, view):
        # Das echte ``add_view`` weist eine Ansicht mit Frist ab: sie ist keine, die einen
        # Neustart überlebt. Der Doppelgänger tut es auch, sonst bliebe genau der Fehler
        # aus #281 hier grün.
        if view.timeout is not None:
            raise ValueError("Nur Ansichten ohne Frist überleben einen Neustart")
        self.angeschlossen.append(view)

    def get_user(self, kennung):
        return self.nutzer.get(kennung)

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
        # Das Zwiegespräch: je Nachricht der Text und die Ansicht darunter.
        self.zwiegespraech: list[tuple[str, object]] = []
        # Wer keine Direktnachrichten annimmt — Discord antwortet darauf mit 403.
        self.zwiegespraech_zu = False

    async def send(self, text, **rest):
        # Wie ``FakeTextkanal.send``: echtes Senden geht ans Netz und gibt dabei ab.
        await asyncio.sleep(0)
        if self.zwiegespraech_zu:
            raise RuntimeError("Cannot send messages to this user")
        self.zwiegespraech.append((text, rest.get("view")))


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
        # Was ankommt, sobald der Mitschnitt läuft: ``(pcm, sprecher)`` je Paket. In echt
        # füttert py-cords Empfangs-Router die Senke aus seinem eigenen Faden.
        self.ankommend = []
        # Ob der Empfänger dabei stirbt — dann hört der Mitschnitt von selbst auf.
        self.router_stirbt = False

    def play(self, quelle, *, after):
        self.gespielt.append(quelle)
        after(None)

    def start_recording(self, senke):
        self.senke = senke
        self.schneidet = True
        for pcm, sprecher in self.ankommend:
            senke.write(sprachdaten(pcm), sprecher)
        if self.router_stirbt:
            self.paketrouter_stirbt()

    def is_recording(self):
        return self.schneidet

    def stop_recording(self):
        # So verhält sich py-cord: der zweite Beender bekommt eine RecordingException.
        if not self.schneidet:
            raise RuntimeError("You are not recording")
        self.schneidet = False
        self.senke.cleanup()

    def paketrouter_stirbt(self):
        """Wie py-cord: der Router beendet den Mitschnitt selbst und sagt es niemandem.

        ``PacketRouter.run`` fängt, was beim Dekodieren fliegt, schreibt es nach
        ``reader.error`` und ruft im ``finally`` ``client.stop_recording()`` — alles in
        seinem eigenen Faden. Heraus kommt der Fehler seit dem festgenagelten Stand
        nirgends mehr; sichtbar bleibt allein, dass nicht mehr mitgeschnitten wird.
        """
        self.stop_recording()

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
        # Was der Kanal an Ton hergibt, sobald jemand mitschneidet — und ob der Empfänger
        # dabei stirbt. Beides wird an die Verbindung durchgereicht.
        self.ankommend = []
        self.router_stirbt = False

    async def connect(self):
        self.verbindung = FakeVoiceClient(self)
        self.verbindung.ankommend = self.ankommend
        self.verbindung.router_stirbt = self.router_stirbt
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
    # Seit #272 legt `/session start` die Sitzung selbst an, und die hängt an der Kennung
    # ihres Kanals — der Doppelgänger braucht deshalb eine.
    naechste_id = 7000

    def __init__(self):
        FakeTextkanal.naechste_id += 1
        self.id = FakeTextkanal.naechste_id
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


class FakeFortgeraeumterKanal:
    """Ein Kanal, den es in Discord nicht mehr gibt — ``send`` bleibt bei 404."""

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
        self.ephemer = []
        self.aufgeschoben = False

    async def defer(self, **rest):
        self.aufgeschoben = True

    async def respond(self, text, **rest):
        self.antworten.append(text)
        self.ephemer.append(bool(rest.get("ephemeral")))


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


class FakeAnhang:
    def __init__(self, filename, inhalt=b"ton-ton-ton", groesse=None):
        self.filename = filename
        self.size = len(inhalt) if groesse is None else groesse
        self._inhalt = inhalt

    async def save(self, ziel):
        Path(ziel).write_bytes(self._inhalt)

    async def read(self):
        return self._inhalt


class FakeButton:
    def __init__(self, *, label="", row=0, custom_id="", disabled=False, **rest):
        self.label = label
        self.row = row
        self.custom_id = custom_id
        self.disabled = disabled
        self.callback = None


class FakeView:
    def __init__(self, *, timeout=None):
        self.timeout = timeout
        self.items: list = []
        self.gestoppt = False

    def add_item(self, teil):
        self.items.append(teil)

    def stop(self):
        self.gestoppt = True


class FakeAntwort:
    """``Interaction.response`` — was ein Klick zurückschreiben kann."""

    def __init__(self):
        self.bearbeitet: list[dict] = []
        self.gesendet: list[str] = []

    def is_done(self) -> bool:
        return bool(self.gesendet or self.bearbeitet)

    async def edit_message(self, *, content=None, embed=None, view=None):
        self.bearbeitet.append({"content": content, "embed": embed, "view": view})

    async def send_message(self, text, **rest):
        self.gesendet.append(text)


class FakeZwiegespraech:
    """Ein Klick im Zwiegespräch: Discord nennt dort keine Gilde, nur den Klickenden."""

    def __init__(self, wer):
        self.guild_id = None
        self.user = wer
        self.response = FakeAntwort()


@pytest.fixture
def pycord(monkeypatch):
    modul = types.ModuleType("discord")
    modul.Option = FakeOption
    modul.Intents = FakeIntents
    modul.Bot = FakeBot
    modul.Permissions = FakePermissions
    modul.PCMAudio = FakePCMAudio
    modul.SelectOption = FakeSelectOption
    modul.Attachment = FakeAnhang
    senken = types.ModuleType("discord.sinks")
    senken.Sink = FakeSenke
    modul.sinks = senken
    werkzeug = types.ModuleType("discord.utils")
    werkzeug.get_missing_voice_dependencies = lambda: ()
    modul.utils = werkzeug
    oberflaeche = types.ModuleType("discord.ui")
    oberflaeche.View = FakeView
    oberflaeche.Select = FakeSelect
    modul.ui = oberflaeche
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


def test_der_bot_verlangt_die_mitglieder_absicht_nicht(konfiguration, pycord):
    """Die Sparsamkeit von #250, festgehalten: kein ``members``-Intent.

    Er ist privilegiert und brächte die ganze Mitgliederliste jeder Gilde dauerhaft in
    diesen Prozess. Gebraucht wird davon ein einziger Anzeigename je Sprecher, und den
    holt ``chronicle.bot.namen`` gezielt. Wer den Intent hier setzt, tauscht die
    sparsamere Lösung gegen die ungenauere.
    """
    gateway.baue(konfiguration)

    absichten = FakeBot.erzeugt[0].intents
    assert absichten.members is False
    assert absichten.voice_states is True


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


MODELL = "chronist-mock"

GESCHRIEBEN = "Die Gruppe brach bei Sonnenaufgang auf, wie sie es besprochen hatte."

# So oft schlägt die Gateway-Schleife, während der Nachtlauf im Modell hängt. Die Zahl
# selbst sagt nichts — dass sie überhaupt zustande kommt, ist die Aussage.
SCHLAEGE = 20


class ModellMitTuer:
    """Ein Ollama, das erst antwortet, wenn ihm jemand die Tür aufmacht.

    Damit hängt die Nacht dort, wo sie in Wirklichkeit hängt: minutenlang im Sprachmodell.
    Aufgemacht wird die Tür von der Gateway-Schleife — schlägt die währenddessen nicht,
    bleibt sie zu, und ``freigegeben`` sagt am Ende, dass die Antwort von der Zeitgrenze
    kam und nicht von einem lebendigen Bot.
    """

    def __init__(self) -> None:
        self.gefragt = threading.Event()
        self.freigegeben = False
        self._tuer = threading.Event()
        self._server = serve(self._http())

    @property
    def url(self) -> str:
        return adresse(self._server)

    def oeffnen(self) -> None:
        self._tuer.set()

    def stop(self) -> None:
        self._server.shutdown()

    def _http(self) -> Flask:
        app = Flask(__name__)

        @app.post("/api/chat")
        def chat():
            self.gefragt.set()
            self.freigegeben = self._tuer.wait(GRENZE)
            return jsonify({"message": {"content": GESCHRIEBEN}})

        return app


@pytest.fixture
def modell():
    eines = ModellMitTuer()
    yield eines
    eines.stop()


def _nacht_fertig(gastgeber) -> bool:
    lauf = jobs.latest(gastgeber, jobs.NACHTLAUF)
    return lauf is not None and lauf.state == jobs.FERTIG


def test_der_nachtlauf_faehrt_aus_dem_bot_prozess_und_haelt_das_gateway_nicht_auf(
    konfiguration, pycord, monkeypatch, nachtwache, modell
):
    """Die Nacht hängt seit #229 hier — und zwar neben der Schleife, nicht in ihr.

    Gefahren wird die Kette wirklich: aus einer Notiz wird über ein echtes HTTP-Ollama
    eine abgelegte Chronik. Das Modell antwortet aber erst, wenn die Gateway-Schleife ihm
    die Tür aufmacht. Liefe der Lauf auf der Schleife oder hielte er sie an, käme sie dazu
    nie — der Bot fiele in Wirklichkeit genau so vom Gateway, weil sein Herzschlag ausbliebe.
    """
    gastgeber = erste_runde(konfiguration)
    sitzung = notes.create_session(gastgeber, played_on="2026-08-06", title="Keller")
    szene = notes.session(gastgeber, sitzung).scenes[0]
    notes.add_note(gastgeber, szene.id, "Wir brechen bei Sonnenaufgang auf.")
    jetzt = datetime.now(ZoneInfo(settings.DEFAULT_NIGHTLY_ZONE))
    settings.save_nightly_time(gastgeber, jetzt.strftime("%H:%M"))
    config = replace(konfiguration, ollama_url=modell.url, ollama_model=MODELL)

    schlaege = []

    def verbindet(self, token):
        """Was py-cord an dieser Stelle tut: die Ereignisschleife fahren."""

        async def schleife():
            while not modell.gefragt.is_set():
                await asyncio.sleep(0.005)
            for _ in range(SCHLAEGE):
                schlaege.append(time.monotonic())
                await asyncio.sleep(0)
            modell.oeffnen()
            while not _nacht_fertig(gastgeber):
                await asyncio.sleep(0.005)

        asyncio.run(asyncio.wait_for(schleife(), GRENZE * 4))

    monkeypatch.setattr(FakeBot, "run", verbindet, raising=False)

    gateway.run(config)

    assert modell.freigegeben
    assert len(schlaege) == SCHLAEGE
    assert GESCHRIEBEN in protocol.stored(gastgeber, sitzung).text


def test_der_nachtlauf_faden_meldet_sich_im_log_ohne_auf_vier_uhr_zu_warten(
    konfiguration, pycord, nachtwache, caplog
):
    """Der Beleg, den der Box-Verify sucht: dass der Faden lebt, steht zu jeder Tageszeit da.

    Bis #237 schrieb er nur, wenn eine Runde fällig war — wer mittags ins Log des Bots sah,
    konnte einen gesunden Faden von einem nie gestarteten nicht unterscheiden.
    """
    with caplog.at_level(logging.INFO, logger="chronicle.nightly"):
        gateway.run(konfiguration)

        assert warte_bis(lambda: nightly.WACH % 0 in caplog.messages)


# Die neun, die in der Hilfe stehen — und damit alles, was eine spielende Gruppe kennen
# muss (#272). Was aufrufbar, aber ungenannt bleibt, steht in NOTNAEGEL.
NEUN_BEFEHLE = (
    "/session start",
    "/session scene",
    "/session done",
    "/session check",
    "/session help",
    "/chronicle search",
    "/chronicle who",
    "/chronicle delete",
    "/chronicle setup",
)

# Aufrufbar, aber bewusst ohne Eintrag in der Hilfe.
NOTNAEGEL = ("pause", "abgleich", "sitzung-loeschen", "zuordnung")

# Die obersten Namen von vorher. Discord kennt nur diese Ebene; steht einer davon noch im
# Satz, den py-cord beim Verbinden schreibt, stünden beide Befehlssätze nebeneinander.
ALTE_NAMEN = ("aufnahme", "chronik", "register", "setup", "suche", "wer", "zuordnung", "szene")


def test_der_bot_bringt_beide_befehle_mit_und_bekommt_den_token(konfiguration, pycord):
    gateway.run(konfiguration)

    (bot,) = FakeBot.erzeugt
    assert set(bot.gruppen[gateway.GRUPPE].befehle) == {
        "start",
        "pause",
        "check",
        "help",
        "done",
        "scene",
    }
    assert set(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle) == {
        "search",
        "who",
        "delete",
        "setup",
        "abgleich",
        "sitzung-loeschen",
        "zuordnung",
    }
    assert bot.intents.guilds and bot.intents.voice_states
    assert bot.token == TOKEN


def test_die_alten_befehlsnamen_sind_abgemeldet_und_nicht_nur_versteckt(konfiguration, pycord):
    """Acht oberste Namen fallen weg — und zwar bei Discord, nicht nur in der Hilfe (#272).

    py-cord schreibt beim Verbinden **alle** Anwendungsbefehle in einem Sammelaufruf und
    löscht dabei, was nicht darin steht. Genau deshalb genügt es, sie hier nicht mehr zu
    registrieren — und genau deshalb ist dieser Test der Wächter: bliebe einer stehen,
    stünden in der Gilde beide Sätze nebeneinander, und niemand wüsste, welcher gilt.
    """
    gateway.run(konfiguration)

    (bot,) = FakeBot.erzeugt
    assert set(bot.gruppen) == {gateway.GRUPPE, gateway.GRUPPE_CHRONIK}
    # Kein Befehl mehr auf oberster Ebene: alles hängt unter einer der beiden Gruppen.
    assert bot.befehle == {}
    for alt in ALTE_NAMEN:
        assert alt not in bot.gruppen


def test_die_hilfe_fuehrt_genau_die_neun_befehle_und_keinen_notnagel():
    """Das Abnahmekriterium aus #272, festgehalten.

    Die Liste wächst sonst mit jedem Befehl, bis sie niemand mehr liest — am 2026-08-18
    konnte der Betreiber selbst mehrere seiner siebzehn nicht erklären. Was ungenannt
    bleibt, bleibt trotzdem aufrufbar; ungenannt ist es, damit die neun tragen.
    """
    genannt = re.findall(r"`(/[a-z]+ [a-z-]+)[ `]", gateway.BEFEHLE)

    assert tuple(dict.fromkeys(genannt)) == NEUN_BEFEHLE
    for notnagel in NOTNAEGEL:
        assert notnagel not in gateway.BEFEHLE


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


def test_der_start_meldet_das_sitzungsfenster_beim_nachbarn_an(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """#299: der Nachbar auf dieser Karte soll waehrend des Abends nicht hin und her laden.

    Angemeldet wird in einem Faden daneben — die Anmeldung laedt das grosse Modell gleich
    mit, und die Runde schneidet inzwischen laengst mit. Kein neuer Slash-Befehl: den
    Beginn des Fensters kennt der Bot schon, es ist der Beginn der Sitzung (#265).
    """
    angemeldet = []

    def anmelden(config):
        angemeldet.append(config.ollama_model)
        return False

    monkeypatch.setattr(ollama, "fenster_oeffnen", anmelden)
    mit_modell = replace(konfiguration, ollama_model="gemma4:12b")
    bot = gateway.baue(mit_modell)

    ctx = FakeCtx(runde.mira)

    async def start_und_fenster():
        await befehl(bot, "start")(ctx)
        await der_lauf(bot, mit_modell).lease

    asyncio.run(start_und_fenster())

    assert angemeldet == ["gemma4:12b"]
    # Der Nachbar hat nicht zugesagt (``False``) — der Abend beginnt trotzdem und wird
    # auch nicht erneut angefragt: es gibt kein Fenster, das zu verlaengern waere.
    assert ctx.antworten == [recorder.GESTARTET]
    assert runde.kanal.verbindung.schneidet


def test_das_fenster_wird_erneuert_solange_es_gilt(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Erneuert wird im Takt, den der Nachbar nennt — und nur, solange das Fenster offen ist."""
    monkeypatch.setattr(ollama, "erneuerung", lambda: 0)
    monkeypatch.setattr(ollama, "fenster_oeffnen", lambda config: angemeldet.append(1) is None)
    gilt = iter([True, True, False])
    monkeypatch.setattr(ollama, "lease_offen", lambda: next(gilt))
    angemeldet = []
    bot = gateway.baue(konfiguration)

    async def start_und_fenster():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await der_lauf(bot, konfiguration).lease

    asyncio.run(start_und_fenster())

    assert len(angemeldet) == 3


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
    assert f"{recordings.RETENTION_TAGE} days" in gateway.VORSTELLUNG
    # Ein Text, zwei Anlässe: die Liste steht nicht zweimal da.
    assert gateway.BEFEHLE in gateway.VORSTELLUNG
    assert gateway.BEFEHLE in gateway.HILFE
    # Und der Ausweg ebenso wenig — er trägt rechtlich, also gibt es ihn genau einmal.
    assert gateway.AUSWEG in gateway.VORSTELLUNG
    assert gateway.AUSWEG in gateway.HILFE
    for satzteil in ("audible announcement", "Until then there is time"):
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


@pytest.mark.parametrize("welcher", ["start", "check"])
def test_ohne_eigene_runde_nimmt_die_gilde_nicht_auf(konfiguration, ohne_espeak, runde, welcher):
    """Dieselbe Absage wie vor ``/session start``, und vor dem Beitreten in den Kanal.

    Und damit vor jeder Ankündigung (#270): im Kanal steht kein Wort, das die nächste
    Zeile zurücknehmen müsste — nur die Absage an den, der den Befehl gab.
    """
    db.init(konfiguration.database_path)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, welcher)(ctx))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_RUNDE in antwort
    assert runde.kanal.verbindung is None
    assert runde.kanal.geschrieben == []


def test_die_ruhende_runde_nimmt_nichts_mehr_auf(konfiguration, sitzung_id, ohne_espeak, runde):
    lebenszyklus.sperren(konfiguration.database_path, KANAL.guild_id)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    (antwort,) = ctx.antworten
    assert "This round is resting" in antwort
    assert runde.kanal.verbindung is None


def test_start_ohne_sprachkanal_verbindet_nicht(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(FakeMitglied(4100, "Ohne Kanal"))

    asyncio.run(befehl(bot, "start")(ctx))

    # Kein Fehlschlag mehr, sondern eine Auskunft: die Sitzung läuft, der Ton fehlt (#272).
    assert ctx.antworten == [gateway.OHNE_SPRACHKANAL]
    assert runde.kanal.verbindung is None


def test_ein_zweiter_start_schneidet_nicht_doppelt(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    ctx = FakeCtx(runde.brok)

    asyncio.run(befehl(bot, "start")(ctx))

    assert ctx.antworten == [gateway.LAEUFT_SCHON]
    assert len(runde.kanal.verbindung.gespielt) == 1


def test_chronik_start_im_sprachkanal_legt_die_sitzung_genau_dort_an(konfiguration, runde):
    """Der wahrscheinlichste Weg: der Befehl kommt aus dem Chat des Sprachkanals.

    Dort steht die Gruppe beim Spielen ohnehin. Bis #241 flog hier ein roher
    ``AttributeError`` — ein ``VoiceChannel`` trägt keine Threads. Seit #271 gibt es
    keinen mehr, und genau dieser Kanal ist der richtige Ort für die Sitzung.
    """
    unsere = unsere_runde(konfiguration)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira, kanal=runde.kanal)

    asyncio.run(befehl(bot, "start")(ctx, "Der erste Abend"))

    (antwort,) = ctx.antworten
    assert chronik.SITZUNG_STEHT in antwort
    assert "AttributeError" not in antwort
    assert chronik.sitzung_im_kanal(unsere, str(runde.kanal.id)) is not None


# -- Zwei Gilden am selben Abend --------------------------------------------------------
#
# Der zweite Akteur, der hier bis #226 fehlte: eine Instanz trägt mehrere Runden (#62/#63),
# und deren Abende überschneiden sich. Was für die eine Gruppe gilt, darf die andere weder
# gesagt bekommen noch angetan.


@pytest.fixture
def nachbarn(pycord, konfiguration):
    """Eine zweite Gilde mit eigener Runde, eigenem Kanal und eigener Sitzung."""
    zweite = runden.anlegen(konfiguration.database_path, "Die Nachbarn", guild_id=NACHBARGILDE)
    notes.create_session(zweite, played_on="2026-08-06", laeuft=True)
    gilde = FakeGilde(int(NACHBARGILDE))
    wer = FakeMitglied(int(SILDAR.id), SILDAR.name)
    chronist = FakeMitglied(998, "Chronik-Bot", bot=True, kanal=None)
    kanal = FakeSprachkanal(gilde, wer, chronist)
    kanal.id = 88
    kanal.name = "Am Nachbartisch"
    chronist.voice.channel = kanal
    gilde.me = chronist
    gilde.mitglieder[wer.id] = wer
    wer.voice = types.SimpleNamespace(channel=kanal)
    return types.SimpleNamespace(gilde=gilde, kanal=kanal, sildar=wer, runde=zweite)


def nachbar_ctx(nachbarn):
    """Ein Befehl aus der zweiten Gilde — dieselbe Instanz, eine andere Runde."""
    return FakeCtx(nachbarn.sildar, guild_id=NACHBARGILDE)


def test_die_zweite_gilde_schneidet_neben_der_ersten_mit(
    konfiguration, sitzung_id, ohne_espeak, runde, nachbarn
):
    """»Ich schneide schon mit« galt bis #226 dem ganzen Prozess — und log damit.

    Bei der zweiten Gruppe schnitt der Bot nichts mit; sie bekam den Satz trotzdem und
    konnte den Abend nicht aufnehmen, solange die erste spielte.
    """
    bot = gateway.baue(konfiguration)
    ctx = nachbar_ctx(nachbarn)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await befehl(bot, "start")(ctx)

    asyncio.run(ablauf())

    assert gateway.LAEUFT_SCHON not in ctx.antworten
    assert ctx.antworten == [recorder.GESTARTET]
    assert runde.kanal.verbindung.schneidet
    assert nachbarn.kanal.verbindung.schneidet


def test_aufnahme_stop_der_zweiten_gilde_beendet_nur_ihren_eigenen_abend(
    konfiguration, sitzung_id, ohne_espeak, runde, nachbarn
):
    """Der Eingriff in eine fremde Runde: ``stop`` beendete den Mitschnitt der anderen."""
    bot = gateway.baue(konfiguration)
    ctx = nachbar_ctx(nachbarn)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await befehl(bot, "start")(nachbar_ctx(nachbarn))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        nachbarn.kanal.verbindung.senke.write(sprachdaten(stille(480)), nachbarn.sildar)
        await befehl(bot, "pause")(ctx)

    asyncio.run(ablauf())

    assert nachbarn.kanal.verbindung.getrennt
    assert not nachbarn.kanal.verbindung.schneidet
    # Und der Abend der ersten Gruppe läuft weiter, als wäre nichts gewesen.
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt
    (spur,) = recordings.pending(nachbarn.runde)
    assert spur.filename.endswith(f"{SILDAR.name}.wav")
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_aufnahme_stop_ohne_eigene_aufnahme_ruehrt_die_fremde_nicht_an(
    konfiguration, sitzung_id, ohne_espeak, runde, nachbarn
):
    """Die schärfste Fassung desselben Fehlers: die zweite Gilde nahm gar nichts auf."""
    bot = gateway.baue(konfiguration)
    ctx = nachbar_ctx(nachbarn)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        await befehl(bot, "pause")(ctx)

    asyncio.run(ablauf())

    assert ctx.antworten == [gateway.LAEUFT_NICHT]
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_der_empfangstest_der_zweiten_gilde_stoert_die_erste_nicht(
    konfiguration, sitzung_id, ohne_espeak, runde, nachbarn, kurze_probe
):
    """``PROBE_NICHT_STOEREN`` meint die Aufnahme *dieser* Runde — sonst keine."""
    bot = gateway.baue(konfiguration)
    ctx = nachbar_ctx(nachbarn)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(PAKET), runde.mira)
        await befehl(bot, "check")(ctx)

    asyncio.run(ablauf())

    assert gateway.PROBE_NICHT_STOEREN not in ctx.antworten
    assert nachbarn.kanal.name in ctx.antworten[0]
    # Die Probe der Nachbarn räumt sich selbst ab; die Aufnahme daneben läuft weiter.
    assert nachbarn.kanal.verbindung.getrennt
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt


def test_der_lauf_der_einen_gilde_ist_nicht_der_der_anderen(
    konfiguration, sitzung_id, ohne_espeak, runde, nachbarn
):
    """Kein Zugriff ohne Runde: die Läufe hängen an ``runde.id``, nicht am Prozess."""
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    laeufe = die_laeufe(bot)
    unser = laeufe.fuer(unsere_runde(konfiguration))
    ihrer = laeufe.fuer(nachbarn.runde)
    assert unser is not ihrer
    assert unser.aufnahme is not None and ihrer.aufnahme is None
    assert laeufe.fuer_gilde(NACHBARGILDE) is ihrer
    # Und eine Gilde, die hier noch nie etwas getan hat, hat auch keinen Lauf.
    assert laeufe.fuer_gilde("13") is None


def test_ohne_sitzung_wird_abgesagt_bevor_irgendetwas_angekuendigt_ist(
    konfiguration, ohne_espeak, runde
):
    """Punkt 1 aus #270: erst prüfen, dann ansagen.

    Am 2026-08-18 kam der Bot in den Sprachkanal, schrieb die vollständige
    Einwilligungs-Ansage hinein — »gleich kommt die hörbare Ansage, danach höre ich zu« —
    und stellte erst danach fest, dass es keine Sitzung gibt; die nächste Zeile nahm alles
    zurück. Eine zurückgenommene Einwilligungs-Ansage ist schlimmer als keine, also fällt
    hier weder eine Ankündigung noch ein Widerruf: der Bot betritt den Kanal gar nicht.

    Nur noch ``/session check``: ``/session start`` **legt** die fehlende Sitzung seit
    #272 selbst an, statt an ihr zu scheitern — die Reihenfolge-Falle, um die es hier
    ging, gibt es dort nicht mehr. Der Test daneben hält fest, dass auch dieser Weg erst
    die Sitzung hat und dann ansagt.
    """
    unsere_runde(konfiguration)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    assert ctx.antworten == [recorder.OHNE_SITZUNG]
    assert runde.kanal.geschrieben == []
    assert runde.kanal.verbindung is None
    assert der_lauf(bot, konfiguration).aufnahme is None


def test_start_ohne_sitzung_legt_eine_an_und_sagt_erst_dann_an(konfiguration, ohne_espeak, runde):
    """Ein Befehl statt einer Reihenfolge (#272) — und die Ansage bleibt stehen.

    Der Fall, an dem die echte Runde scheiterte: keine Sitzung, aber alle im Sprachkanal.
    Der Bot legt sie jetzt an und schneidet danach mit; die Einwilligungs-Ansage steht im
    Kanal, weil es hinter ihr nichts mehr gibt, was sie zurücknähme.
    """
    unsere = unsere_runde(konfiguration)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx, "Der erste Abend"))

    (angelegt,) = notes.sessions(unsere)
    assert notes.running_session(unsere).id == angelegt.id
    assert "".join(text for text, _gespielt in runde.kanal.geschrieben) == gateway.VORSTELLUNG
    assert runde.kanal.verbindung.schneidet
    assert der_lauf(bot, konfiguration).aufnahme is not None


def _start_stolpert(monkeypatch, grund=recorder.VERSCHOBEN_BEIM_START):
    """Der Start scheitert **nach** der Vorstellung — dort, wo es einen Widerruf braucht.

    Bis #270 genügte dafür die fehlende Sitzung. Seither prüft der Befehl sie, bevor er
    ankündigt — genau darum ging es —, und die Zweige darunter brauchen einen Anlass, der
    erst hinter der Ankündigung eintritt. ``VERSCHOBEN_BEIM_START`` ist einer davon: der
    Bot wird gezogen, während die hörbare Ansage läuft.
    """

    async def stolpert(*args, **rest):
        raise recorder.AufnahmeFehler(grund)

    monkeypatch.setattr(recorder, "starten", stolpert)
    return grund


def test_ein_gescheiterter_start_nimmt_die_vorstellung_dort_zurueck_wo_sie_steht(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Die Absage ist ephemer, die Ankündigung ist öffentlich — das war die Lücke (#189).

    Ohne den Widerruf liest die Runde »gleich schneide ich mit« und spielt den Abend in
    dem Glauben, er werde festgehalten. Nur wer den Befehl tippte, erführe je das
    Gegenteil.
    """
    grund = _start_stolpert(monkeypatch)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    gesagt = [text for text, _ in runde.kanal.geschrieben]
    widerruf = gesagt[-1]
    assert "".join(gesagt[:-1]) == gateway.VORSTELLUNG
    assert widerruf == gateway.WIDERRUF.format(grund=grund)
    assert "I am not recording" in widerruf
    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert der_lauf(bot, konfiguration).aufnahme is None


def test_der_widerruf_nennt_bei_einem_unerwarteten_fehler_nur_die_art(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Der Kanal ist der öffentlichste Ort dieses Bots — dort steht keine Innerei."""

    async def stolpert(*args, **rest):
        raise RuntimeError("sqlite3://runde-7?token=geheim")

    monkeypatch.setattr(recorder, "starten", stolpert)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    widerruf = runde.kanal.geschrieben[-1][0]
    assert gateway.UNERWARTET.format(typ="RuntimeError") in widerruf
    assert "geheim" not in widerruf
    assert runde.kanal.verbindung.getrennt


def test_ohne_zugestellte_vorstellung_wird_gar_nicht_erst_angesagt(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Die andere Richtung derselben Regel: kein Mitschnitt ohne die lesbare Hälfte.

    Die Vorstellung nennt den Ausweg, solange noch nichts läuft. Kommt sie nicht durch,
    darf die hörbare Ansage gar nicht erst spielen — sonst schnitte der Bot mit, ohne dass
    je jemand lesen konnte, wie man da herauskommt.
    """

    async def abgewiesen(text, **rest):
        raise RuntimeError("Discord hat abgelehnt")

    monkeypatch.setattr(runde.kanal, "send", abgewiesen)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    assert runde.kanal.verbindung.gespielt == []
    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert der_lauf(bot, konfiguration).aufnahme is None
    assert consent.for_session(unsere_runde(konfiguration), sitzung_id) == ()
    assert ctx.antworten[0].startswith("That did not work:")
    # Im Kanal steht nichts — auch kein Widerruf. Versucht wird er trotzdem: dieser Kanal
    # weist jede Nachricht ab, also verfällt er ins Log statt vor einer leeren Wand zu
    # stehen. Wo nur ein Teil scheitert, kommt er an — der Test darunter fährt das.
    assert runde.kanal.geschrieben == []


def test_die_halb_zugestellte_vorstellung_wird_auch_zurueckgenommen(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Der Normalfall, nicht der Randfall — die Vorstellung geht in zwei Nachrichten hinaus.

    Der tragende Satz »Erst danach schneide ich mit« steht im **ersten** Stück. Scheitert
    erst das zweite, hängt ``_abriss_melden`` nur an, dass der Text unvollständig sei —
    von einer angekündigten Aufnahme nimmt das nichts zurück. Wer allein den Kanal sieht,
    liest weiter, dass gleich mitgeschnitten wird, und das ist genau die Lücke aus #189.

    Die Teilung wird hergestellt und nicht abgewartet: seit #272 stehen neun Befehle in der
    Liste statt siebzehn, und damit passt die Vorstellung wieder in **eine** Nachricht. Der
    Fall bleibt trotzdem der Normalfall — die Liste wächst mit jedem Befehl zurück.
    """
    monkeypatch.setattr(
        gateway, "VORSTELLUNG", gateway.VORSTELLUNG.replace(gateway.BEFEHLE, gateway.BEFEHLE * 4)
    )
    stuecke = grenzen.teile(gateway.VORSTELLUNG)
    # Sonst prüfte dieser Test unbemerkt denselben Alles-oder-nichts-Fall wie der davor.
    assert len(stuecke) > 1
    echt = runde.kanal.send

    async def nur_das_erste_stueck(text, **rest):
        if text == stuecke[1]:
            raise RuntimeError("Discord hat abgelehnt")
        await echt(text, **rest)

    monkeypatch.setattr(runde.kanal, "send", nur_das_erste_stueck)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert gesagt[0] == stuecke[0]
    assert gesagt[-1] == gateway.WIDERRUF.format(
        grund=gateway.UNERWARTET.format(typ="RuntimeError")
    )
    assert runde.kanal.verbindung.gespielt == []
    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert der_lauf(bot, konfiguration).aufnahme is None
    assert consent.for_session(unsere_runde(konfiguration), sitzung_id) == ()
    assert ctx.antworten[0].startswith("That did not work:")


def test_ein_stolperndes_trennen_nimmt_den_widerruf_nicht_mit(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Der Widerruf ist die Zusage an die Aufgenommenen, das Trennen nur Aufräumen.

    ``disconnect`` geht ans Netz und kann werfen. Stand es vor dem Widerruf, blieb die
    Ankündigung im Kanal stehen, sobald es das tat — der Bot saß dann obendrein noch da.
    Deshalb wird erst geredet und danach aufgeräumt.
    """
    grund = _start_stolpert(monkeypatch)
    echt_verbinden = runde.kanal.connect

    async def verbindet_und_stolpert_beim_trennen():
        verbindung = await echt_verbinden()
        verbindung.trennen_stolpert = True
        return verbindung

    monkeypatch.setattr(runde.kanal, "connect", verbindet_und_stolpert_beim_trennen)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert "".join(gesagt[:-1]) == gateway.VORSTELLUNG
    assert gesagt[-1] == gateway.WIDERRUF.format(grund=grund)
    assert not runde.kanal.verbindung.getrennt
    assert not runde.kanal.verbindung.schneidet
    assert der_lauf(bot, konfiguration).aufnahme is None
    assert ctx.antworten[0].startswith("That did not work:")


def _stolpert_beim_trennen(kanal, monkeypatch):
    """Der Kanal verbindet wie sonst, aber ``disconnect`` geht ans Netz und stirbt dort."""
    echt = kanal.connect

    async def verbindet():
        verbindung = await echt()
        verbindung.trennen_stolpert = True
        return verbindung

    monkeypatch.setattr(kanal, "connect", verbindet)


def test_der_widerruf_kommt_auch_wenn_die_vorstellung_selbst_abreisst(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Zweig 1 von vieren: die Vorstellung bricht mittendrin ab, noch vor dem Mitschnitt.

    Bis #208 war die Reihenfolge hier ungesichert — »trennen zuerst« kam durch, ohne dass
    ein Test rot wurde. Gerade hier trägt sie: die halbe Ankündigung steht schon im Kanal,
    daneben der Abriss-Hinweis, der von einer Aufnahme nichts zurücknimmt. Stolpert das
    Trennen davor, bliebe sie für immer stehen.

    Die Teilung wird hergestellt: mit neun Befehlen statt siebzehn passt die Vorstellung
    seit #272 wieder in eine Nachricht, und der Zweig hier braucht zwei.
    """
    unsere_runde(konfiguration)
    monkeypatch.setattr(
        gateway, "VORSTELLUNG", gateway.VORSTELLUNG.replace(gateway.BEFEHLE, gateway.BEFEHLE * 4)
    )
    _stolpert_beim_trennen(runde.kanal, monkeypatch)
    echt_senden = runde.kanal.send
    versuche = []

    async def sendet(text):
        versuche.append(text)
        # Das **zweite** Stück: das erste steht dann schon im Kanal, und genau dann ist
        # etwas zurückzunehmen.
        if len(versuche) == 2:
            raise RuntimeError("Discord hat abgelehnt")
        await echt_senden(text)

    monkeypatch.setattr(runde.kanal, "send", sendet)
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert gesagt[-1] == gateway.WIDERRUF.format(
        grund=gateway.UNERWARTET.format(typ="RuntimeError")
    )
    assert not runde.kanal.verbindung.getrennt
    assert der_lauf(bot, konfiguration).aufnahme is None


def test_der_widerruf_kommt_auch_wenn_der_empfangstest_nicht_anlaeuft(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Zweig 4 von vieren: dieselbe Reihenfolge im Empfangstest, hinter der Ankündigung.

    Die Probe-Ankündigung steht öffentlich wie die vor einer Aufnahme; läuft danach nichts
    an, wird sie zurückgenommen — und zwar bevor aufgeräumt wird, weil das Aufräumen ans
    Netz geht und den Widerruf sonst mitnimmt.
    """
    grund = _start_stolpert(monkeypatch)
    _stolpert_beim_trennen(runde.kanal, monkeypatch)
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "check")(FakeCtx(runde.mira)))

    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert gesagt[0] == gateway.PROBE_VORSTELLUNG
    assert gesagt[-1] == gateway.WIDERRUF.format(grund=grund)
    assert not runde.kanal.verbindung.getrennt
    assert probespuren(konfiguration) == []


def test_die_probe_vorstellung_passt_in_eine_nachricht():
    """Die Voraussetzung, unter der der Empfangstest keinen halben Text hinterlassen kann.

    Der Widerruf steht dort trotzdem — er ist heute nur wirkungslos, weil es zwischen
    »ganz raus« und »gar nicht raus« nichts gibt. Wächst der Text über Discords Grenze,
    wird derselbe Zweig schlagartig tragend wie der in ``start``, und das fiele ohne
    diese Zeile niemandem auf: kein Test wird rot, wenn man dort den Widerruf entfernt.
    """
    assert len(grenzen.teile(gateway.PROBE_VORSTELLUNG)) == 1


def test_wer_nach_einem_gescheiterten_start_dazukommt_wird_nicht_aufgenommen(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch
):
    """Der Nachzügler zur Aufnahme, die es nicht gibt: keine Ansage, kein Protokoll."""
    _start_stolpert(monkeypatch)
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    gespielt_vorher = len(runde.kanal.verbindung.gespielt)
    spaet = FakeMitglied(int(SPAET.id), SPAET.name)

    asyncio.run(bot.ereignisse["on_voice_state_update"](spaet, zustand(), zustand(runde.kanal)))

    assert len(runde.kanal.verbindung.gespielt) == gespielt_vorher
    assert not runde.kanal.verbindung.schneidet
    assert der_lauf(bot, konfiguration).aufnahme is None


def test_der_widerruf_haelt_den_urspruenglichen_fehler_nicht_auf(
    konfiguration, sitzung_id, ohne_espeak, runde, monkeypatch, caplog
):
    """Ein zweites Mal abgewiesener Kanal darf die Absage an den Aufrufer nicht schlucken."""
    grund = _start_stolpert(monkeypatch)
    echt = runde.kanal.send
    anfang = gateway.WIDERRUF.split("{")[0]

    async def alles_ausser_dem_widerruf(text, **rest):
        if text.startswith(anfang):
            raise RuntimeError("Discord hat abgelehnt")
        await echt(text, **rest)

    monkeypatch.setattr(runde.kanal, "send", alles_ausser_dem_widerruf)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    with caplog.at_level(logging.ERROR):
        asyncio.run(befehl(bot, "start")(ctx))

    assert grund in ctx.antworten[0]
    assert runde.kanal.verbindung.getrennt
    assert "Widerruf" in caplog.text


def test_die_senke_schreibt_je_sprecher_eine_spur(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    senke = runde.kanal.verbindung.senke

    senke.write(sprachdaten(stille(480)), runde.mira)
    senke.write(sprachdaten(stille(480)), runde.brok)
    ctx = FakeCtx(runde.mira)
    asyncio.run(befehl(bot, "pause")(ctx))

    assert runde.kanal.verbindung.getrennt
    assert senke.finished
    spuren = {
        spur.filename.split("-")[-1] for spur in recordings.pending(unsere_runde(konfiguration))
    }
    assert spuren == {"Mira.wav", "Brok.wav"}
    assert "queued for the batch" in ctx.antworten[0]


def test_was_nach_dem_hinauswurf_ankommt_faellt_weg_und_nicht_in_ein_haeppchen(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    """**Die Einwilligung hängt am Rahmen, nicht am Lauf** — und auch nicht am Häppchen.

    Zwischen dem Verschieben und dem Ereignis, das den Mitschnitt beendet, liefert py-cord
    weiter. Was in dieser Lücke ankommt, wurde in einem Kanal gesprochen, für den niemand
    gefragt wurde. ``SpurSenke.write`` wirft es weg, und der Schnitt in Häppchen (#217)
    darf daran nichts ändern: der Schnitt liegt eine Ebene tiefer, hinter dieser Schranke.
    """
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    senke = runde.kanal.verbindung.senke
    senke.write(sprachdaten(stille(480)), runde.mira)

    verschieben(runde)
    senke.write(sprachdaten(stille(480)), runde.mira)
    asyncio.run(befehl(bot, "pause")(FakeCtx(runde.mira)))

    (spur,) = recordings.pending(unsere_runde(konfiguration))
    with wave.open(str(konfiguration.recordings_dir / spur.filename), "rb") as datei:
        assert datei.getnframes() == 480


def test_stop_ohne_aufnahme_sagt_es(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "pause")(ctx))

    assert ctx.antworten == [gateway.LAEUFT_NICHT]


# -- Der Empfangstest -------------------------------------------------------------------
#
# Die Frage, die er beantwortet: kommt hier überhaupt lesbarer Ton an? Sie war bis dahin
# nur im Container-Log zu beantworten — für die Gruppe sah ein kaputter Mitschnitt aus wie
# ein guter, und dass nichts geschrieben wurde, fiel Stunden später auf.


PAKET = stille(480)


@pytest.fixture
def kurze_probe(monkeypatch):
    """In echt zehn Sekunden; hier wird einmal an die Schleife abgegeben und das war es."""
    monkeypatch.setattr(recorder, "PROBE_DAUER", 0)


def probespuren(konfiguration) -> list[Path]:
    """Was von der Probe auf der Platte liegt — die Ansage heißt anders und zählt nicht."""
    return list(konfiguration.recordings_dir.glob("sitzung*"))


def test_der_empfangstest_nennt_pakete_und_spuren(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    runde.kanal.ankommend = [(PAKET, runde.mira), (PAKET, runde.mira), (PAKET, runde.brok)]
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    (antwort,) = ctx.antworten
    assert "Packets: 3" in antwort
    assert "Tracks: 2" in antwort
    assert f"• {MIRA.name}: {2 * len(PAKET)} bytes" in antwort
    assert f"• {BROK.name}: {len(PAKET)} bytes" in antwort
    assert recorder.PROBE_TRAEGT in antwort
    # Der Bericht nennt Anzeigenamen — er geht ephemer an den, der ihn ausgelöst hat.
    assert ctx.ephemer == [True]
    assert runde.kanal.verbindung.getrennt


def test_der_bericht_zaehlt_keine_dekodierfehler_und_sagt_das_auch(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Keine Zahl, die es nicht gibt — und kein Schweigen darüber, dass sie fehlt.

    Bis zum festgenagelten Stand stand hier »Dekodierfehler: 0«. Die Zahl war seit dem
    Stand aus Pycord-PR #3159 nicht mehr zu haben: ``PacketDecoder._decode_packet`` fängt
    den ``OpusError`` an der Quelle ab und dekodiert mit Paketverlust-Verschleierung
    weiter. Eine stehengebliebene Null hätte danach »nichts verloren« behauptet, wo nur
    »nicht gezählt« stimmte — der teuerste Fehler, den ein Bericht machen kann.
    """
    runde.kanal.ankommend = [(PAKET, runde.mira)]
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    (antwort,) = ctx.antworten
    assert "Dekodierfehler:" not in antwort
    assert recorder.PROBE_RAHMENVERLUST in antwort


def test_ein_empfang_der_von_selbst_aufhoert_faellt_nicht_gruen_aus(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Der Fall, für den es den Befehl gibt — und der einzige, den man nicht sehen kann.

    Der Paket-Router stirbt in einem eigenen Faden und ruft dort ``stop_recording``; der
    Befehl selbst läuft ungestört weiter. Die Zahlen von davor sind dann sämtlich in
    Ordnung — hier sogar ein angekommenes Paket und eine Spur. Nur das »trägt« daneben
    wäre falsch, und genau dafür steht der Abbruch im Urteil vor der Paketzahl.
    """
    runde.kanal.ankommend = [(PAKET, runde.mira)]
    runde.kanal.router_stirbt = True
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    (antwort,) = ctx.antworten
    assert "Packets: 1" in antwort
    assert recorder.PROBE_ABGEBROCHEN.format(dauer=recorder.PROBE_DAUER) in antwort
    assert recorder.PROBE_TRAEGT not in antwort
    assert recorder.PROBE_STILL.format(dauer=recorder.PROBE_DAUER) not in antwort


def test_ein_abbruch_ohne_paket_wird_nicht_der_stille_angelastet(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Die Gegenprobe: null Pakete heißen nicht dasselbe wie null Pakete.

    Stirbt der Empfang sofort, ist die Zahl »0« richtig — die Erklärung »dann hat wohl
    niemand gesprochen« aber falsch, und sie schickt die Runde ein zweites Mal reden.
    Deshalb steht der Abbruch im Urteil vor der Paketzahl, nicht dahinter.
    """
    runde.kanal.router_stirbt = True
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    (antwort,) = ctx.antworten
    assert "Packets: 0" in antwort
    assert recorder.PROBE_ABGEBROCHEN.format(dauer=recorder.PROBE_DAUER) in antwort
    assert recorder.PROBE_STILL.format(dauer=recorder.PROBE_DAUER) not in antwort


def test_ohne_ein_einziges_paket_faellt_das_urteil_nicht_gruen_aus(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    (antwort,) = ctx.antworten
    assert "Packets: 0" in antwort
    assert "Tracks: 0" in antwort
    assert "Not a single packet" in antwort
    assert recorder.PROBE_TRAEGT not in antwort


def test_die_probespuren_sind_danach_von_der_platte_weg(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Es sind Stimmen echter Menschen — sie verschwinden sofort, nicht erst mit der Frist."""
    runde.kanal.ankommend = [(PAKET, runde.mira), (PAKET, runde.brok)]
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    assert probespuren(konfiguration) == []
    assert recorder.PROBE_AUFGERAEUMT.format(dauer=recorder.PROBE_DAUER) in ctx.antworten[0]


def test_der_empfangstest_reiht_nichts_ein(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Keine Zeile in der Warteschlange: was hier ankam, wird nie verschriftet."""
    runde.kanal.ankommend = [(PAKET, runde.mira)]
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "check")(FakeCtx(runde.mira)))

    unsere = unsere_runde(konfiguration)
    assert list(recordings.pending(unsere)) == []
    assert list(recordings.for_session(unsere, sitzung_id)) == []


def test_auch_die_gescheiterte_probe_laesst_keine_spur_liegen(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe, monkeypatch
):
    """Gerade dann nicht: wer hinsieht, sieht die Fehlermeldung — nicht den Aufnahmeordner."""
    runde.kanal.ankommend = [(PAKET, runde.mira)]

    async def stolpert(self):
        raise RuntimeError("Discord hat aufgelegt")

    monkeypatch.setattr(gateway.Sprachverbindung, "trennen", stolpert)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    assert ctx.antworten[0].startswith("That did not work:")
    assert probespuren(konfiguration) == []
    assert list(recordings.pending(unsere_runde(konfiguration))) == []


def test_der_empfangstest_sagt_hoerbar_an_und_protokolliert_die_einwilligung(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Zehn Sekunden sind eine Aufnahme. Ein Testmodus, der leise mitschnitte, wäre strafbar."""
    bot = gateway.baue(konfiguration)

    asyncio.run(befehl(bot, "check")(FakeCtx(runde.mira)))

    assert len(runde.kanal.verbindung.gespielt) == 1
    (eintrag,) = consent.for_session(unsere_runde(konfiguration), sitzung_id)
    assert eintrag.kind == consent.ANSAGE
    assert eintrag.text == ansage.protokoll_fuer(sprachen.DEFAULT)
    assert {wer.name for wer in eintrag.members} == {MIRA.name, BROK.name}
    # Und der Ausweg stand im Kanal, bevor die Ansage lief: die Null ist der Beleg.
    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert "".join(gesagt) == gateway.PROBE_VORSTELLUNG
    assert gateway.AUSWEG in gesagt[0]
    assert [gespielt for _, gespielt in runde.kanal.geschrieben] == [0] * len(gesagt)


def test_der_empfangstest_ruehrt_eine_laufende_aufnahme_nicht_an(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Er würde ihr die Sprachverbindung wegziehen — also wird es gesagt und nichts getan."""
    bot = gateway.baue(konfiguration)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    verbindung = runde.kanal.verbindung
    verbindung.senke.write(sprachdaten(PAKET), runde.mira)
    ctx = FakeCtx(runde.brok)

    asyncio.run(befehl(bot, "check")(ctx))

    assert ctx.antworten == [gateway.PROBE_NICHT_STOEREN]
    assert verbindung.schneidet and not verbindung.getrennt
    assert len(verbindung.gespielt) == 1

    # Und die Spur der echten Aufnahme ist unversehrt — sie geht ihren Weg zu Ende.
    asyncio.run(befehl(bot, "pause")(FakeCtx(runde.mira)))
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith(f"{MIRA.name}.wav")


def test_waehrend_der_probe_beginnt_kein_mitschnitt(konfiguration, sitzung_id, ohne_espeak, runde):
    """Die Gegenrichtung: der Test hält die Verbindung und trennt sie gleich wieder."""
    bot = gateway.baue(konfiguration)
    der_lauf(bot, konfiguration).probe = True
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    assert ctx.antworten == [gateway.PROBE_LAEUFT]
    assert runde.kanal.verbindung is None


def test_eine_unloeschbare_probespur_wird_beim_namen_genannt(
    konfiguration, sitzung_id, ohne_espeak, caplog, kein_loeschen, monkeypatch
):
    """Was liegen bleibt, wird gesagt — auch hier, und ohne Klarnamen im Log.

    Eine Probespur hat keine Zeile in der Warteschlange; für ``sweep`` ist sie unsichtbar.
    Sie stillschweigend liegen zu lassen wäre die schlechteste aller Zusagen. Geschlossen
    wird sie trotzdem zuerst — und auch ein gescheitertes ``close`` hält das Löschen der
    übrigen nicht auf.
    """
    monkeypatch.setattr(recorder._Spur, "schliessen", stolpert_beim_schliessen(1))
    stimme = FakeStimme(MIRA, BROK)
    aufnahme = asyncio.run(recorder.starten(konfiguration, stimme, unsere_runde(konfiguration)))
    aufnahme.schreiben(MIRA, stille(480))
    kein_loeschen()
    caplog.clear()

    spuren = aufnahme.verwerfen()

    text = recorder.bericht(
        recorder.Probe(kanal=KANAL.name, dauer=1, pakete=1, abgebrochen=False, spuren=spuren)
    )
    assert [spur.geloescht for spur in spuren] == [False]
    assert MIRA.name in text
    assert "could not be deleted" in text
    assert recorder.PROBE_AUFGERAEUMT.format(dauer=1) not in text
    assert MIRA.name not in caplog.text


def test_ein_zweiter_empfangstest_laeuft_nicht_daneben(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    bot = gateway.baue(konfiguration)
    der_lauf(bot, konfiguration).probe = True
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    assert ctx.antworten == [gateway.PROBE_LAEUFT]
    assert runde.kanal.verbindung is None


def test_der_empfangstest_ohne_sprachkanal_verbindet_nicht(
    konfiguration, sitzung_id, ohne_espeak, runde
):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(FakeMitglied(4100, "Ohne Kanal"))

    asyncio.run(befehl(bot, "check")(ctx))

    assert ctx.antworten == [gateway.NICHT_IM_KANAL]
    assert runde.kanal.verbindung is None


def test_der_gescheiterte_empfangstest_trennt_wieder(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe, monkeypatch
):
    """Der Abbruch **vor** dem Mitschnitt: den räumt ``pruefen`` nicht mehr ab."""
    grund = _start_stolpert(monkeypatch)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    assert grund in ctx.antworten[0]
    assert runde.kanal.verbindung.getrennt
    assert der_lauf(bot, konfiguration).probe is False
    # Auch diese Ankündigung steht öffentlich im Kanal und kündigt zehn Sekunden Aufnahme
    # an — sie darf so wenig allein stehenbleiben wie die vor einer Sitzung.
    gesagt = [text for text, _ in runde.kanal.geschrieben]
    assert "".join(gesagt[:-1]) == gateway.PROBE_VORSTELLUNG
    assert gesagt[-1] == gateway.WIDERRUF.format(grund=grund)


def test_die_ruhende_runde_wird_auch_nicht_geprueft(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_probe
):
    """Dieselbe Schranke wie vor ``/session start`` — hier wird aufgezeichnet."""
    lebenszyklus.sperren(konfiguration.database_path, KANAL.guild_id)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "check")(ctx))

    assert "This round is resting" in ctx.antworten[0]
    assert runde.kanal.verbindung is None
    assert consent.for_session(unsere_runde(konfiguration), sitzung_id) == ()


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

    async def frist(config, *, melden=None):
        gelaufen.append((config, melden))

    monkeypatch.setattr(recordings, "taeglich", frist)
    bot = gateway.baue(konfiguration)

    async def anmelden():
        await bot.ereignisse["on_ready"]()
        await bot.ereignisse["on_ready"]()
        await asyncio.sleep(0)

    asyncio.run(anmelden())

    # Beim zweiten on_ready — Discord schickt es nach jedem Wiederverbinden — darf kein
    # zweiter Aufräumer danebenlaufen.
    assert [config for config, _melden in gelaufen] == [konfiguration]
    # Und die Frist bekommt einen Weg zurück in die Runde mit: was sie löscht, darf sie
    # nicht still löschen (#286).
    assert gelaufen[0][1] is not None


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


SITZUNGSKANAL = 88


@pytest.fixture
def sitzung_im_kanal(konfiguration, monkeypatch):
    """Eine laufende Sitzung mit Kanal — und der Abschluss von selbst abgeklemmt.

    Laufend muss sie sein: ``/session start`` legt seit #272 selbst eine an, wenn keine
    offen ist, und schnitte hier sonst in einen zweiten, frisch erfundenen Abend hinein.

    Abgeklemmt ist der Abschluss aus dem Grund, aus dem die Sitzung bis dahin
    **geschlossen** war: der leere Kanal zieht seit #271 den ganzen Abschluss nach sich —
    Zahlen, Verschriftung, Chronik —, und jeder Mitschnitt-Test prüfte damit nebenbei
    dasselbe. Er hat eigene Tests; sie nehmen ``laufende_sitzung_im_kanal``.
    """

    async def nichts(*args, **rest):
        return None

    monkeypatch.setattr(gateway, "_sitzung_von_selbst_abschliessen", nichts)
    return notes.create_session(
        unsere_runde(konfiguration), kanal_id=str(SITZUNGSKANAL), laeuft=True
    )


@pytest.fixture
def laufende_sitzung_im_kanal(konfiguration):
    return notes.create_session(
        unsere_runde(konfiguration), kanal_id=str(SITZUNGSKANAL), laeuft=True
    )


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
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Die Einwilligung galt dieser Runde, nicht dem, was danach im leeren Kanal fällt."""
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    alle_gehen(bot, runde)

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")
    (gesagt,) = kanal.geschrieben
    assert gesagt.startswith(gateway.LEER_BEENDET)
    assert "queued for the batch" in gesagt


def test_das_ende_im_leeren_kanal_steht_ohne_kanalnamen_im_log(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist, caplog
):
    """Wie #206, nur die Zeile, die dort nicht im Issue stand (#211).

    Der Kanalname ist kein Personenname, beschreibt aber die Struktur einer fremden
    Gilde. Was an seine Stelle tritt, wird mitgeprüft: eine Zeile, die nichts mehr
    identifiziert, wäre kein Fortschritt, sondern ein Loch im Reparaturmantel.
    """
    caplog.set_level(logging.DEBUG)
    runde.kanal.name = "Am-Runden-Tisch"
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()

    alle_gehen(bot, runde)

    assert runde.kanal.name not in caplog.text
    assert f"Sitzung {sitzung_im_kanal}: Sprachkanal leer" in caplog.text


def test_viele_spurmeldungen_fallen_nicht_still_weg(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist, monkeypatch
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
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    alle_gehen(bot, runde)

    ganz = " ".join((gateway.LEER_BEENDET, *meldungen))
    assert len(ganz) > grenzen.NACHRICHT
    assert len(kanal.geschrieben) > 1
    assert all(len(gesagt) <= grenzen.NACHRICHT for gesagt in kanal.geschrieben)
    assert "".join(kanal.geschrieben) == ganz
    assert all(meldung in ganz for meldung in meldungen)


def test_wenige_spurmeldungen_bleiben_eine_nachricht(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Gegenprobe: geteilt wird nur, was geteilt werden muss."""
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    alle_gehen(bot, runde)

    assert len(kanal.geschrieben) == 1


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
    assert kanal.geschrieben[-1] == gateway._abrisssatz(1, stuecke)
    assert f"1 von {stuecke} Stücken zugestellt, {stuecke - 1} fehlen" in caplog.text


def test_der_abrisssatz_stimmt_bei_einem_teil_wie_bei_vielen():
    """Der öffentlichste Satz des Bots — und bis #208 stand darin »1 von 2 Teilen kamen
    durch, 1 fehlen«.

    Ein einzelnes fehlendes Stück ist dabei nicht der Randfall, sondern der häufigste:
    geteilt wird ab zwei Stücken, und meistens sind es genau zwei.
    """
    einer = gateway._abrisssatz(1, 2)
    assert "only 1 of 2 parts got through, 1 is missing" in einer
    assert " are missing" not in einer

    mehrere = gateway._abrisssatz(2, 5)
    assert "only 2 of 5 parts got through, 3 are missing" in mehrere


def test_scheitert_schon_das_erste_stueck_bleibt_es_beim_alles_oder_nichts(caplog):
    """Gegenprobe: ohne angekommenen Anfang gibt es nichts kenntlich zu machen.

    Ein Hinweis auf einen Text, den niemand gesehen hat, wäre selbst die Irreführung.
    """
    kanal = FakeZuckendesDiscord(bricht_beim=1)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        asyncio.run(gateway._zustellen(kanal.send, "Zeile.\n" * 900))

    assert kanal.geschrieben == []
    assert "abgerissen" not in caplog.text


def test_der_leere_kanal_schliesst_die_sitzung_ab(
    konfiguration, laufende_sitzung_im_kanal, ohne_espeak, runde, kurze_frist, monkeypatch
):
    """Seit #271 endet hier der Abend und nicht nur der Mitschnitt.

    Den Abschluss zu vergessen war der häufigste Fehler und kostete den ganzen Abend seine
    Zahlen. Wer schon gegangen ist, tippt ihn nicht mehr — also erkennt der Bot ihn selbst.
    """
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, session_id, *, passwort=None: gesehen.append(session_id) or "steht",
    )
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()

    alle_gehen(bot, runde)

    unsere = unsere_runde(konfiguration)
    assert gesehen == [laufende_sitzung_im_kanal]
    # Und die Sitzung ist zu: was jetzt noch im Kanal steht, ist keine Notiz mehr.
    assert chronik.sitzung_im_kanal(unsere, str(SITZUNGSKANAL)) is None
    gesagt = bot.kanaele[SITZUNGSKANAL].geschrieben
    assert gesagt[0].startswith(gateway.LEER_BEENDET)
    assert any(chronik.FERTIG in satz for satz in gesagt)


def test_wer_rechtzeitig_zurueckkommt_behaelt_seine_offene_sitzung(
    konfiguration, laufende_sitzung_im_kanal, ohne_espeak, runde, kurze_frist, monkeypatch
):
    """Die zweite Prüfung nach der Frist gilt jetzt dem ganzen Abschluss.

    Ein Verbindungsabriss darf keine zerschnittene Sitzung hinterlassen: wer in der Frist
    zurückkommt, findet seinen Abend vor und nicht dessen Chronik.
    """
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, session_id, *, passwort=None: gesehen.append(session_id) or "steht",
    )
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        anwesend = nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        # Zurück, bevor die Frist um ist — genau der Fall, für den es sie gibt.
        runde.kanal.members = anwesend
        await dazu(runde.mira, zustand(), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    unsere = unsere_runde(konfiguration)
    assert gesehen == []
    assert chronik.sitzung_im_kanal(unsere, str(SITZUNGSKANAL)) == laufende_sitzung_im_kanal
    assert runde.kanal.verbindung.schneidet


class FakeZeile:
    """Eine getippte Zeile im Sitzungskanal, so weit ``on_message`` sie liest."""

    def __init__(self, kanal_id, text="12 EP für alle."):
        self.id = 9001
        self.content = text
        self.attachments = []
        self.created_at = datetime.now(UTC)
        self.author = types.SimpleNamespace(id=int(MIRA.id), bot=False)
        self.guild = types.SimpleNamespace(id=int(KANAL.guild_id))
        self.channel = types.SimpleNamespace(id=kanal_id)
        self.antworten: list[str] = []

    async def reply(self, text):
        self.antworten.append(text)


def test_wer_noch_abmoderiert_haelt_den_abend_offen(
    konfiguration, laufende_sitzung_im_kanal, ohne_espeak, runde, kurze_frist, monkeypatch
):
    """#288: das gespielte Ende und das Abmoderieren sind zweierlei.

    Der leere Sprachkanal heißt weiter »Abend fertig« (#264) — aber nach dem letzten Wurf
    fällt der Tisch aus dem Sprachkanal und tippt noch zehn Minuten EP, Beute und
    »nächstes Mal« in den Kanal. Wer tippt, ist da; die Frist läuft von der letzten Zeile
    an neu, und danach schließt der Abend doch.

    Das ist zugleich die Antwort auf die Doppelzustellung aus dem Bericht: bleibt der
    Abend **eine** Sitzung, gibt es auch nur eine ``protocol``-Zeile — die Tabelle hält
    ``UNIQUE (session_id, kind)`` — und damit genau einen Rückblick im Gruppenkanal.
    """
    gesehen = []
    monkeypatch.setattr(
        jobs,
        "abschluss",
        lambda config, eine, session_id, *, passwort=None: gesehen.append(session_id) or "steht",
    )
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
    offen = []

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        nur_der_bot(runde.kanal)
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(runde.kanal), zustand())
        # Kurz vor Ablauf der Frist tippt jemand — das stellt sie neu.
        await asyncio.sleep(FRIST * 0.75)
        await bot.ereignisse["on_message"](FakeZeile(SITZUNGSKANAL))
        # Die ursprüngliche Frist ist damit vorbei, die neue läuft noch.
        await asyncio.sleep(FRIST * 0.5)
        offen.append(chronik.sitzung_im_kanal(unsere_runde(konfiguration), str(SITZUNGSKANAL)))
        await ruhen()

    asyncio.run(ablauf())

    unsere = unsere_runde(konfiguration)
    assert offen == [laufende_sitzung_im_kanal]
    assert gesehen == [laufende_sitzung_im_kanal]
    assert [eine.id for eine in notes.sessions(unsere)] == [laufende_sitzung_im_kanal]


def einer_bleibt(kanal, wer):
    """Alle gehen bis auf einen — der Bot bleibt, er zählt ohnehin nicht mit."""
    kanal.members = [jemand for jemand in kanal.members if jemand.bot or jemand is wer]


def dritter_im_kanal(runde):
    """Eine dritte Person im Sprachkanal — die Attrappe kennt von Haus aus zwei."""
    wer = FakeMitglied(int(SPAET.id), SPAET.name, kanal=runde.kanal)
    runde.gilde.mitglieder[wer.id] = wer
    runde.kanal.members.append(wer)
    return wer


def die_laeufe(bot):
    """Die Läufe hinter den Ereignissen — sie leben in der Closure von ``baue``."""
    return inspect.getclosurevars(bot.ereignisse["on_voice_state_update"]).nonlocals["laeufe"]


def der_lauf(bot, konfiguration):
    """Der Zustand *einer* Runde — seit #226 gibt es ihn je Runde und nicht je Prozess."""
    return die_laeufe(bot).fuer(unsere_runde(konfiguration))


def test_der_satz_ans_alleinsein_zeigt_den_widerspruch_und_traegt_keinen_namen():
    """Ein Satz ohne Feld kann keinen Namen und keine Kennung tragen — auch nicht später."""
    assert "`/session pause`" in gateway.ALLEIN
    assert "{" not in gateway.ALLEIN
    # Und die Hilfe sagt es vorab — die Vorstellung nicht, denn sie ist ohnehin der längste
    # Text des Bots. Geprüft wird der Satz und nicht »allein im Sprachkanal«: das steht auch
    # im Punkt zu ``/session pause`` und meint dort die Gegenlage, den leeren Kanal.
    hinweis = "If one person is left alone in the voice channel, I carry on recording"
    assert hinweis in gateway.HILFE
    assert hinweis not in gateway.BEFEHLE
    assert hinweis not in gateway.VORSTELLUNG
    # Und die Hilfe steht seit `/session check` ebenfalls darüber — aus demselben Grund und
    # mit derselben Folge: geteilt statt abgewiesen, der Ausweg im ersten Stück.
    hilfe = grenzen.teile(gateway.HILFE)
    assert gateway.AUSWEG in hilfe[0]
    assert "".join(hilfe) == gateway.HILFE
    # Die Vorstellung steht **über** der Grenze, seit die Befehlsliste um `/chronicle abgleich`
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
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Entscheidung des Betreibers: nachfragen statt anhalten — niemand verliert ungefragt.

    Die Ansage lief für eine Gruppe, die es nicht mehr gibt; deshalb erfährt die
    Zurückgebliebene, dass sie allein ist. Angehalten wird darunter nichts.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert kanal.geschrieben == [gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_dass_sie_allein_ist_wird_genau_einmal_gesagt(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Sonst stünde bei jedem Stummschalten derselbe Satz noch einmal im Kanal."""
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
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

    assert kanal.geschrieben == [gateway.ALLEIN]


def test_gehen_zwei_im_selben_schwung_steht_der_satz_trotzdem_nur_einmal_da(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """py-cord stellt jedes Sprachereignis als eigenen Task zu — und hat den Cache der
    Mitglieder vorher aktualisiert. Beide Handler sehen deshalb dieselbe eine Verbliebene;
    wer den Vermerk erst hinter dem ``await`` setzt, schreibt den Satz zweimal.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
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

    assert kanal.geschrieben == [gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet


def test_bleiben_zwei_zurueck_sagt_der_bot_nichts(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """»Nur noch **eine** Person« ist eine Tatsachenbehauptung, an der die Einwilligung
    hängt: sie soll aufhören können, weil sie allein ist. Zu zweit wäre sie falsch.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        aelin = dritter_im_kanal(runde)
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.members.remove(aelin)
        await dazu(aelin, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert kanal.geschrieben == []
    assert runde.kanal.verbindung.schneidet


def test_ein_ortswechsel_im_selben_kanal_ist_gar_kein_gehen(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Stummschalten meldet Discord mit demselben Kanal auf beiden Seiten des Ereignisses.

    Wer nur auf »war drin« sieht, hält das für ein Gehen — und sagt der, die von Anfang an
    allein mitschneidet, beim ersten Stummschalten, es sei gerade jemand gegangen. Ohne
    vorheriges Gehen fängt das auch kein Vermerk ab.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        einer_bleibt(runde.kanal, runde.mira)
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await dazu(runde.mira, zustand(runde.kanal), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    assert kanal.geschrieben == []
    assert runde.kanal.verbindung.schneidet


def test_ein_vermerk_von_vorhin_verschluckt_den_satz_der_naechsten_aufnahme_nicht(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Dieselbe Falle wie beim Wächter des leeren Kanals: gemerkt wird die Aufnahme."""
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        anwesend = list(runde.kanal.members)
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await befehl(bot, "pause")(FakeCtx(runde.mira))
        runde.kanal.members = anwesend
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await ruhen()

    asyncio.run(ablauf())

    assert kanal.geschrieben == [gateway.ALLEIN, gateway.ALLEIN]


def test_auch_die_zweite_zurueckgebliebene_erfaehrt_es(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Genau der Fall aus dem Issue: nacheinander bleiben zwei verschiedene allein zurück.

    Hinge der Vermerk an der Aufnahme, hätte ihn die erste verbraucht — die zweite säße
    allein im Kanal und erführe es nie. Er hängt deshalb an der Person.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
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

    assert kanal.geschrieben == [gateway.ALLEIN, gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet


def test_ein_zuckender_kanal_verbrennt_den_satz_nicht(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Vermerkt wird erst, was ankam — sonst bliebe der Satz nach einem Fehlschlag aus.

    Nachgeholt wird er nirgends: käme der Vermerk vor dem Senden, wäre eine einmal
    gescheiterte Zustellung das Ende, und sie erführe es auch bei jedem weiteren Wechsel
    nicht mehr.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    kanal.stolpert = 1
    bot.kanaele[SITZUNGSKANAL] = kanal
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

    assert kanal.geschrieben == [gateway.ALLEIN]
    assert runde.kanal.verbindung.schneidet
    assert not runde.kanal.verbindung.getrennt


def test_ohne_kanal_wird_allein_nicht_weitergeschnitten(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_frist
):
    """Zugesagt war, dass sie es erfährt — nicht, dass wir es versuchen.

    Eine Sitzung ohne Kanal hat keinen Weg für den Satz. Still weiterzuschneiden wäre
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


def test_ein_fortgeraeumter_kanal_beendet_den_mitschnitt_statt_ewig_weiterzuschneiden(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Fort ist nicht »zuckt«: der 404 käme bei jedem weiteren Wechsel genauso wieder.

    Behandelte der Bot ihn wie ein Zucken, schnitte er dauerhaft weiter, ohne dass ihr je
    gesagt wurde, dass sie allein ist — derselbe Zustand, gegen den die Zusage steht, nur
    durch die andere Tür.
    """
    bot = gateway.baue(konfiguration)
    fort = FakeFortgeraeumterKanal()
    bot.kanaele[SITZUNGSKANAL] = fort
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
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Der Vermerk führt Discord-Kennungen. Nach dem Ende liest sie niemand mehr."""
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        gemerkt = der_lauf(bot, konfiguration).allein
        await befehl(bot, "pause")(FakeCtx(runde.mira))
        await ruhen()
        return gemerkt

    gemerkt = asyncio.run(ablauf())

    # Erst dass überhaupt etwas dastand — sonst prüfte der zweite Satz nur eine Lücke.
    assert gemerkt is not None
    assert MIRA.id in gemerkt[1]
    assert der_lauf(bot, konfiguration).allein is None


def test_die_rueckkehr_setzt_dieselbe_aufnahme_fort(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Entscheidung des Betreibers: kein Bruch, keine neue Sitzung, keine zweite Ansage.

    Die Spur läuft durch — dieselbe Senke, dieselbe Verbindung, dieselbe Sitzung.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
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
    assert notes.latest_session(unsere).id == sitzung_im_kanal
    angesagt = [ereignis for ereignis in consent.for_session(unsere, sitzung_im_kanal)]
    assert [ereignis.kind for ereignis in angesagt] == [consent.ANSAGE, consent.NACHZUEGLER]


def test_wer_zurueckkommt_ist_ein_nachzuegler_und_hoert_die_ansage(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Die zweite Hälfte derselben Entscheidung: fortsetzen heißt nicht ungefragt.

    Für den Kanal läuft keine zweite Ansage — für den Zurückkehrenden schon, und seine
    Einwilligung steht danach im Protokoll.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
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
    _, zurueck = consent.for_session(unsere_runde(konfiguration), sitzung_im_kanal)
    assert zurueck.kind == consent.NACHZUEGLER
    assert [wer.name for wer in zurueck.members] == [BROK.name]


def test_wer_allein_widerspricht_beendet_damit_den_mitschnitt(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """»Bis sie widerspricht« braucht einen benannten Weg — und der ist der vorhandene."""
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        einer_bleibt(runde.kanal, runde.mira)
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        ctx = FakeCtx(runde.mira)
        await befehl(bot, "pause")(ctx)
        await ruhen()
        return ctx

    ctx = asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert "queued for the batch" in ctx.antworten[0]


def test_wer_in_der_frist_wiederkommt_findet_seine_aufnahme_vor(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Der Verbindungsabriss dauert wirklich — hier vergeht Zeit, keine null Sekunden."""
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
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
    assert kanal.geschrieben == []
    assert recordings.pending(unsere_runde(konfiguration)) == ()


def test_wer_wieder_geht_bekommt_die_ganze_frist_noch_einmal(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Sonst wären aus neunzig Sekunden Karenz eine halbe geworden.

    Der Wächter des ersten Gehens weckt zu seiner Zeit, nicht zu der des zweiten. Wird er
    beim erneuten Gehen nicht ersetzt, zählt die Frist ab dem ersten — und wer bei T=89
    zurückkommt und bei T=89,5 die Verbindung wieder verliert, findet die Sitzung
    geschnitten vor.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
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
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Genau der Zustand, gegen den dieser Wächter gebaut ist: ein leerer Kanal läuft weiter.

    Aufnahme X endet von Hand, während ihr Wächter noch schläft. Beginnt Y innerhalb der
    Frist und leert sich auch, muss Y einen **eigenen** Wächter bekommen — sonst schneidet
    Y unbegrenzt einen leeren Kanal mit, und ein zweites Ereignis kommt nicht mehr.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        anwesend = nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await befehl(bot, "pause")(FakeCtx(runde.mira))
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
    assert kanal.geschrieben[-1].startswith(gateway.LEER_BEENDET)


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
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """»Gegangen« und »leer« müssen denselben Kanal meinen — den der Ansage.

    Seit #103 endet der Mitschnitt beim Verschieben ohnehin; dass hier trotzdem nichts
    hängen bleibt, hängt weiter daran, dass der Lauf seinen Ursprungskanal festhält.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
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
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Der Bot hat den Raum verlassen, für den die Einwilligung galt — also ist Schluss.

    Nebenan sitzen Leute, die die Ansage nie gehört haben. Dass der Mitschnitt endet,
    steht im Kanal, und es steht dabei, **warum**.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

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
    (gesagt,) = kanal.geschrieben
    assert gesagt.startswith(gateway.VERSCHOBEN.format(kanal=runde.kanal.name))
    assert "queued for the batch" in gesagt


def test_der_start_im_falschen_kanal_laesst_keinen_lauf_zurueck(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist, monkeypatch
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
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    ctx = FakeCtx(runde.mira)

    async def ablauf():
        await befehl(bot, "start")(ctx)
        # Erst hierdurch fiele ein hängengebliebener Lauf überhaupt auf — es gibt keinen.
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    assert runde.kanal.verbindung.getrennt
    assert consent.for_session(unsere_runde(konfiguration), sitzung_im_kanal) == ()
    assert kanal.geschrieben == []
    assert ctx.antworten[-1] == gateway.GESCHEITERT.format(grund=recorder.VERSCHOBEN_BEIM_START)


def test_was_nach_dem_verschieben_ankommt_landet_in_keiner_spur(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Für das Stück zwischen Verschiebung und Erkennung gibt es keine Zustimmung.

    Verworfen wird es nicht nachträglich, sondern gar nicht erst geschrieben: ab dem
    Rahmen, in dem py-cord den neuen Kanal führt, fällt jede Sprachdatenlieferung weg.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()

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
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Die Nebenwirkung derselben Klasse: seine Ansage liefe nebenan, er hörte sie nie.

    Kein Einwilligungseintrag darf entstehen, dessen Ansage nachweislich woanders lief.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()
    spaet = FakeMitglied(int(SPAET.id), SPAET.name)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        verschieben(runde)
        await bot.ereignisse["on_voice_state_update"](spaet, zustand(), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    (eintrag,) = consent.for_session(unsere_runde(konfiguration), sitzung_im_kanal)
    assert eintrag.kind == consent.ANSAGE
    # Und die zweite Ansage hat gar nicht erst gespielt.
    assert len(runde.kanal.verbindung.gespielt) == 1
    assert runde.kanal.verbindung.getrennt


def test_das_verschieben_beendet_genau_einmal_und_nicht_die_naechste_aufnahme(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Zwei Ereignisse, ein Ende — und der nächste Lauf bleibt davon unberührt."""
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
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

    assert len(kanal.geschrieben) == 1
    assert zweite.schneidet
    assert not zweite.getrennt


def test_der_gescheiterte_abschied_nach_dem_verschieben_sagt_es(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Auch hier gilt: lieber ein Satz zu viel als ein Lauf, von dem niemand mehr weiß."""
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.trennen_stolpert = True
        nachbar = verschieben(runde)
        await bot.ereignisse["on_voice_state_update"](
            runde.chronist, zustand(runde.kanal), zustand(nachbar)
        )
        await ruhen()

    asyncio.run(ablauf())

    assert kanal.geschrieben == [gateway.VERSCHOBEN_GESCHEITERT.format(kanal=runde.kanal.name)]


def test_ein_abriss_wird_nicht_als_verschieben_gemeldet(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Ein harter Abriss endet wie ein Verschieben, begründet sich aber anders (#120).

    Wird der Bot hinausgeworfen oder bricht die Verbindung, sitzt er in **keinem** Kanal —
    im Kanal stand trotzdem, jemand habe ihn »in einen anderen Sprachkanal gezogen«. Die
    Handlung war richtig, die Auskunft falsch, und sie schickte die Runde nebenan suchen.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        # Der Hinauswurf, wie Discord und py-cord ihn hinterlassen: der Zustand des Bots
        # ist aus dem Zwischenspeicher der Gilde heraus, die Verbindung hat keinen Kanal.
        runde.chronist.voice = None
        runde.kanal.verbindung.channel = None
        await bot.ereignisse["on_voice_state_update"](
            runde.chronist, zustand(runde.kanal), zustand()
        )
        await ruhen()

    asyncio.run(ablauf())

    assert not runde.kanal.verbindung.schneidet
    (gesagt,) = kanal.geschrieben
    assert gesagt.startswith(gateway.GETRENNT.format(kanal=runde.kanal.name))
    assert not gesagt.startswith(gateway.VERSCHOBEN.format(kanal=runde.kanal.name))


def test_der_kanalverlust_steht_ohne_kanalnamen_im_log(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist, caplog
):
    """Beide Zweige des Kanalverlusts, verschoben und abgerissen, in einem Test (#211).

    Sie unterscheiden sich nur in der Begründung, und die steht im **Kanal** — dort mit
    Kanalnamen, denn der gehört der Gilde, die ihre eigenen Kanäle kennt. Im Log des
    Betreibers steht die Sitzung, und geprüft wird beides: dass der Name fort ist und
    dass die Zeile trotzdem noch sagt, welcher Lauf hier endete.
    """
    caplog.set_level(logging.DEBUG)
    runde.kanal.name = "Am-Runden-Tisch"
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        verschieben(runde)
        await bot.ereignisse["on_voice_state_update"](
            runde.chronist, zustand(runde.kanal), zustand()
        )
        await ruhen()

    asyncio.run(ablauf())

    assert runde.kanal.name not in caplog.text
    assert f"Sitzung {sitzung_im_kanal}: der Bot wurde aus dem Sprachkanal verschoben" in (
        caplog.text
    )
    # Der Kanal bekommt den Namen weiter — dort ist er die Auskunft, wo es aufhörte.
    assert runde.kanal.name in " ".join(kanal.geschrieben)


def test_der_abriss_steht_ohne_kanalnamen_im_log(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist, caplog
):
    """Der zweite Zweig: hinausgeworfen statt verschoben, dieselbe Regel (#211)."""
    caplog.set_level(logging.DEBUG)
    runde.kanal.name = "Am-Runden-Tisch"
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        runde.chronist.voice = None
        runde.kanal.verbindung.channel = None
        await bot.ereignisse["on_voice_state_update"](
            runde.chronist, zustand(runde.kanal), zustand()
        )
        await ruhen()

    asyncio.run(ablauf())

    assert runde.kanal.name not in caplog.text
    assert f"Sitzung {sitzung_im_kanal}: die Verbindung zum Sprachkanal ist abgerissen" in (
        caplog.text
    )


def test_die_meldung_vieler_spuren_kommt_geteilt_statt_gar_nicht(
    konfiguration, sitzung_im_kanal, pycord, monkeypatch
):
    """Dreißig Spuren ergaben einen Satz über 2000 Zeichen — Discord wies ihn ab (#120).

    Das ``except`` in ``_beenden_und_sagen`` schluckte den Fehlschlag, und die Runde erfuhr
    vom Ende des Mitschnitts gar nichts. Geteilt statt gekürzt: jedes Stück nimmt Discord
    an, und aneinandergehängt steht wieder genau derselbe Satz da.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    aufnahme = Aufnahme(
        konfiguration,
        unsere_runde(konfiguration),
        sitzung_im_kanal,
        KANAL,
        inhaltssprache=sprachen.DEFAULT,
    )
    lauf = gateway._Lauf()
    lauf.aufnahme = aufnahme
    meldungen = tuple(
        f"Spur {nummer} von Sprecherin {nummer} wartet auf den Stapel." for nummer in range(40)
    )

    async def viele(*args, **kwargs):
        return meldungen

    monkeypatch.setattr(recorder, "stoppen", viele)

    asyncio.run(gateway._beenden_und_sagen(bot, lauf, aufnahme, gateway.LEER_BEENDET, "kaputt"))

    ganz = " ".join((gateway.LEER_BEENDET, *meldungen))
    assert len(ganz) > grenzen.NACHRICHT
    assert len(kanal.geschrieben) > 1
    assert all(len(stueck) <= grenzen.NACHRICHT for stueck in kanal.geschrieben)
    assert "".join(kanal.geschrieben) == ganz


def test_der_gescheiterte_abschied_sagt_es_statt_zu_verschwinden(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist, monkeypatch
):
    """Ein Faden nebenher hat niemanden, dem er antwortet — also sagt er es im Kanal."""

    async def stolpert(*args, **kwargs):
        raise RuntimeError("You are not recording")

    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    monkeypatch.setattr(recorder, "stoppen", stolpert)

    alle_gehen(bot, runde)

    assert kanal.geschrieben == [gateway.LEER_GESCHEITERT]


def test_das_langsamere_netz_meldet_kein_zweites_ende(konfiguration, sitzung_im_kanal, pycord):
    """Wer leer zurückbekommt, war der zweite — und der Satz gehört dem ersten.

    ``_mitschnitt_beenden`` gibt leer zurück, wenn ein anderer den Lauf schon beansprucht
    hat. Ein zweiter Satz behauptete ein zweites Ende und schickte zu einem
    ``/session pause``, das »keine Aufnahme« antwortet.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    aufnahme = Aufnahme(
        konfiguration,
        unsere_runde(konfiguration),
        sitzung_im_kanal,
        KANAL,
        inhaltssprache=sprachen.DEFAULT,
    )

    asyncio.run(gateway._beenden_und_sagen(bot, gateway._Lauf(), aufnahme, "beendet", "kaputt"))

    assert kanal.geschrieben == []


def test_eine_misslungene_ansage_macht_aus_dem_ende_keinen_fehlschlag(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """``LEER_GESCHEITERT`` gehört dem gescheiterten Beenden, nicht der stummen Ansage.

    Umfasste ein ``try`` beides, schriebe ein zuckendes ``kanal.send`` »die Aufnahme gilt
    weiter als laufend, gib `/session pause`« in den Kanal — während der Lauf beendet, der
    Bot getrennt und die Spuren eingereiht sind und ``/session pause`` »keine Aufnahme«
    antwortet. Genau die falsche Anweisung, gegen die dieser Wächter gebaut ist.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    ungestoert = kanal.send

    async def zuckt(text):
        if text.startswith(gateway.LEER_BEENDET):
            raise RuntimeError("kanal.send zuckt")
        await ungestoert(text)

    kanal.send = zuckt

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        runde.kanal.verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        nur_der_bot(runde.kanal)
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(runde.kanal), zustand())
        await ruhen()
        ctx = FakeCtx(runde.mira)
        await befehl(bot, "pause")(ctx)
        return ctx

    ctx = asyncio.run(ablauf())

    assert runde.kanal.verbindung.getrennt
    assert not runde.kanal.verbindung.schneidet
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")
    # Der Lauf ist zu Ende — und die Meldung, die das Gegenteil behauptet, blieb aus.
    assert gateway.LAEUFT_NICHT in ctx.antworten
    assert kanal.geschrieben == []


def test_nach_dem_gescheiterten_abschied_stellt_erst_ein_neuer_gang_den_waechter(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Die Rücknahme gibt den Lauf zurück, den abbestellten Wächter aber nicht.

    Das ist entschieden und nicht vergessen: einen neuen zu stellen hieße, bei bleibendem
    Fehler alle neunzig Sekunden denselben Fehlschlag in den Kanal zu schreiben. Also
    sagt ``LEER_GESCHEITERT``, was gilt — von selbst geschieht nichts mehr, und erst wer
    den Kanal betritt und wieder verlässt, bestellt einen neuen Wächter.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        verbindung = runde.kanal.verbindung
        verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        verbindung.trennen_stolpert = True
        anwesend = nur_der_bot(runde.kanal)
        await dazu(runde.mira, zustand(runde.kanal), zustand())
        await ruhen()
        assert kanal.geschrieben == [gateway.LEER_GESCHEITERT]
        # Mehrere Fristen lang von selbst: kein zweiter Versuch, kein zweiter Satz.
        await asyncio.sleep(FRIST * 3)
        assert kanal.geschrieben == [gateway.LEER_GESCHEITERT]
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
    assert kanal.geschrieben[-1].startswith(gateway.LEER_BEENDET)


def test_nach_gescheitertem_beenden_greift_aufnahme_stop_noch(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """Der Anspruch vor dem Abgeben darf den Fehlerpfad nicht unrettbar machen.

    Ohne Rücknahme wäre der Lauf nach einem gescheiterten Trennen geleert: der Bot bliebe
    im Kanal, die Spuren lägen uneingereiht, und ``/session pause`` antwortete ab da immer
    »keine Aufnahme« — genau der Zustand, gegen den dieser Wächter gebaut ist, nur ohne
    jeden Befehl, der ihn beendet. Der Satz im Kanal verspricht das Gegenteil; hier wird
    das Versprechen eingelöst.
    """
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        verbindung = runde.kanal.verbindung
        verbindung.senke.write(sprachdaten(stille(480)), runde.mira)
        verbindung.trennen_stolpert = True
        nur_der_bot(runde.kanal)
        await bot.ereignisse["on_voice_state_update"](runde.mira, zustand(runde.kanal), zustand())
        await ruhen()
        assert not verbindung.getrennt
        # Und jetzt das, wozu die Meldung im Kanal auffordert.
        verbindung.trennen_stolpert = False
        ctx = FakeCtx(runde.mira)
        await befehl(bot, "pause")(ctx)
        return ctx

    ctx = asyncio.run(ablauf())

    assert kanal.geschrieben == [gateway.LEER_GESCHEITERT]
    assert gateway.LAEUFT_NICHT not in ctx.antworten
    assert not any(antwort.startswith("Das hat nicht geklappt") for antwort in ctx.antworten)
    assert runde.kanal.verbindung.getrennt
    assert not runde.kanal.verbindung.schneidet
    (spur,) = recordings.pending(unsere_runde(konfiguration))
    assert spur.filename.endswith("Mira.wav")


def test_ein_zweites_aufnahme_stop_holt_die_liegengebliebene_spur_nach(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, monkeypatch
):
    """Der Repro aus #104, so wie er gemeldet wurde: ``/session pause`` ein zweites Mal.

    Vorher scheiterte der zweite Anlauf an der UNIQUE-Bedingung der schon eingereihten
    ersten Spur — und die zweite blieb für immer als Datei ohne Zeile liegen: weder
    verschriftet noch nach Frist gelöscht.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        senke = runde.kanal.verbindung.senke
        senke.write(sprachdaten(stille(480)), runde.mira)
        senke.write(sprachdaten(stille(480)), runde.brok)
        monkeypatch.setattr(recordings, "enqueue", StolpertBeimEinreihen(2))
        erster, zweiter = FakeCtx(runde.mira), FakeCtx(runde.mira)
        await befehl(bot, "pause")(erster)
        await befehl(bot, "pause")(zweiter)
        return erster, zweiter

    erster, zweiter = asyncio.run(ablauf())

    assert erster.antworten[0].startswith("That did not work")
    assert BROK.name in erster.antworten[0]
    assert gateway.LAEUFT_NICHT not in zweiter.antworten
    assert not any(antwort.startswith("Das hat nicht geklappt") for antwort in zweiter.antworten)
    assert sorted(sprecher(spur) for spur in recordings.pending(unsere_runde(konfiguration))) == [
        BROK.name,
        MIRA.name,
    ]


def test_zwei_beender_zugleich_geben_dem_zweiten_keine_leere_antwort(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde, kurze_frist
):
    """``recorder.stoppen`` gibt beim Trennen ab; in der Lücke kam bisher ein zweiter durch.

    Der zweite bekam von py-cord »You are not recording« zu hören und die Runde ein »das
    hat nicht geklappt« für einen Stopp, der geklappt hat.
    """
    bot = gateway.baue(konfiguration)
    bot.kanaele[SITZUNGSKANAL] = FakeTextkanal()

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        erster, zweiter = FakeCtx(runde.mira), FakeCtx(runde.mira)
        await asyncio.gather(befehl(bot, "pause")(erster), befehl(bot, "pause")(zweiter))
        return erster, zweiter

    erster, zweiter = asyncio.run(ablauf())

    antworten = erster.antworten + zweiter.antworten
    assert len(antworten) == 2
    assert not any(antwort.startswith("Das hat nicht geklappt") for antwort in antworten)
    assert gateway.LAEUFT_NICHT in antworten
    assert runde.kanal.verbindung.getrennt


def test_ohne_kanal_endet_der_mitschnitt_trotzdem(
    konfiguration, sitzung_id, ohne_espeak, runde, kurze_frist
):
    """Eine Sitzung aus der Zeit vor dem Kanal hat keinen — gesagt wird es dann nirgends."""
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
    assert antwort.startswith("That did not work:")
    assert "RuntimeError" in antwort
    assert "What you can do" in antwort
    # Und der Bot hängt nicht stumm im Kanal herum.
    assert runde.kanal.verbindung.getrennt


def test_die_hilfe_erklaert_die_bedienung(konfiguration, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "help")(ctx))

    # Angehängt ergeben die Stücke wieder genau die Hilfe — sie braucht seit
    # `/session check` zwei Nachrichten, und keins der Stücke darf dabei wegfallen.
    ganz = "".join(ctx.antworten)
    assert ganz == gateway.HILFE
    for satzteil in (
        "/session start",
        "/session pause",
        "/session check",
        "announcement",
        "leaves",
    ):
        assert satzteil in ganz
    # Der Ausweg kommt zuerst — er trägt rechtlich und darf nicht in Stück zwei rutschen.
    assert gateway.AUSWEG in ctx.antworten[0]


def test_eine_zu_lange_hilfe_wird_nachgereicht_statt_abgewiesen(konfiguration, runde, monkeypatch):
    """Auch die Antwort auf einen Befehl hängt an Discords 2000 Zeichen — sie wächst mit."""
    lang = gateway.HILFE.replace(gateway.BEFEHLE, gateway.BEFEHLE * 4)
    monkeypatch.setattr(gateway, "HILFE", lang)
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "help")(ctx))

    assert len(ctx.antworten) > 1
    assert all(len(antwort) <= grenzen.NACHRICHT for antwort in ctx.antworten)
    assert "".join(ctx.antworten) == lang


def test_die_bestaetigung_sagt_das_wichtigste(konfiguration, sitzung_id, ohne_espeak, runde):
    bot = gateway.baue(konfiguration)
    ctx = FakeCtx(runde.mira)

    asyncio.run(befehl(bot, "start")(ctx))

    (antwort,) = ctx.antworten
    for satzteil in (
        "announcement",
        "one track per speaker",
        "leave the voice channel",
        "/session pause",
    ):
        assert satzteil in antwort


# -- Zuordnen beim Betreten des Sprachkanals ----------------------------------------------
#
# Die Zusage aus #76: jede Äußerung gehört von Anfang an einer Figur. Heißt jemand wie ein
# Foundry-Spieler oder wie dessen Figur, wird das ohne Rückfrage gesetzt und im Kanal
# vermerkt; sonst wird **die betroffene Person** im Zwiegespräch gefragt. Was hier nie
# passieren darf: dass ein Vorschlag als Bestätigung durchgeht.


def foundry_spieler(konfiguration, *namen, wo=None):
    """Ein Foundry-Stand mit genau diesen Konten — mehr braucht die Zuordnung nicht."""
    scope = db.scoped(unsere_runde(konfiguration) if wo is None else wo)
    try:
        foundry_store.save(
            scope,
            WorldSnapshot(
                system="daggerheart",
                fetched_at="2026-08-06T20:00:00+00:00",
                players=tuple(
                    Player(id=f"u-{name.lower()}", name=name, role=1, is_gm=False) for name in namen
                ),
            ),
        )
    finally:
        scope.close()


def zugeordnet(konfiguration):
    """Wer wem zugeordnet **ist** — Vorschläge kommen hier mit Absicht nicht vor."""
    return {
        kennung: None if person.confirmed is None else person.confirmed.name
        for kennung, person in people.speakers(unsere_runde(konfiguration)).items()
    }


def gefragt_wurde(mitglied):
    """Das Menü aus der einen Frage im Zwiegespräch — oder nichts."""
    if not mitglied.zwiegespraech:
        return None
    (_text, view) = mitglied.zwiegespraech[-1]
    (menue,) = view.items
    return menue


async def antwort_geben(menue, wer, wert):
    """Wie eine Person im Zwiegespräch auf ihre Frage antwortet."""
    menue.values = [wert]
    interaktion = FakeZwiegespraech(wer)
    await menue.callback(interaktion)
    return interaktion


def antworten(menue, wer, wert):
    return asyncio.run(antwort_geben(menue, wer, wert))


@pytest.fixture
def mit_kanal(konfiguration, sitzung_im_kanal):
    """Die Sitzung hat einen Kanal — dort steht der Vermerk über eine Zuordnung."""

    def anhaengen(bot):
        kanal = FakeTextkanal()
        bot.kanaele[SITZUNGSKANAL] = kanal
        return kanal

    return anhaengen


def test_beim_start_wird_zugeordnet_wer_gleich_heisst_und_gefragt_wer_nicht(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Beides beim Betreten und nicht erst am Ende des Abends — das ist der Punkt von #76."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert zugeordnet(konfiguration) == {MIRA.id: "Mira", BROK.id: None}
    assert kanal.geschrieben == [erinnern.BETRETEN_VERMERK.format(name=MIRA.name, spieler="Mira")]
    # Wer von selbst zugeordnet wurde, wird nicht auch noch gefragt — und umgekehrt.
    assert runde.mira.zwiegespraech == []
    assert gefragt_wurde(runde.brok) is not None
    assert runde.kanal.name in runde.brok.zwiegespraech[0][0]


def test_das_menue_im_zwiegespraech_bietet_kein_fremdes_konto_an(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Der Weg, auf dem Brok privat und unbeaufsichtigt zu Mira wurde — er ist zu.

    Miras Konto ist beim Start vergeben. In Broks Menü steht es deshalb nicht, und selbst
    wenn er es doch nennt, wird nichts geschrieben: ein Konto gehört genau einer Person.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    menue = gefragt_wurde(runde.brok)

    assert [option.value for option in menue.options] == [erinnern.KEINE, "u-brok eisenfaust"]

    interaktion = antworten(menue, runde.brok, "u-mira")

    assert interaktion.response.bearbeitet[0]["content"] == erinnern.SPIELER_VERGEBEN
    assert zugeordnet(konfiguration) == {MIRA.id: "Mira", BROK.id: None}


def test_die_frage_im_zwiegespraech_ist_noch_keine_zuordnung(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Die Gegenprobe zur Namensgleichheit: gefragt heißt nicht gesetzt.

    Erst die Antwort schreibt fest. Ein Menü, das schon beim Aufklappen zuordnete, wäre
    genau die stillschweigend übernommene Vermutung, gegen die es die Bestätigung gibt.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    menue = gefragt_wurde(runde.brok)

    assert zugeordnet(konfiguration)[BROK.id] is None

    antworten(menue, runde.brok, "u-brok eisenfaust")

    assert zugeordnet(konfiguration)[BROK.id] == "Brok Eisenfaust"


def test_wer_bestaetigt_hat_wird_beim_naechsten_betreten_nicht_erneut_gefragt(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await antwort_geben(gefragt_wurde(runde.brok), runde.brok, "u-brok eisenfaust")
        await befehl(bot, "pause")(FakeCtx(runde.mira))
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await ruhen()

    asyncio.run(ablauf())

    # Je einmal aus dem ersten Lauf, kein zweites Mal aus dem zweiten.
    assert len(runde.brok.zwiegespraech) == 1
    assert kanal.geschrieben == [
        erinnern.BETRETEN_VERMERK.format(name=MIRA.name, spieler="Mira"),
        erinnern.MENUE_VERMERK.format(name=BROK.name, spieler="Brok Eisenfaust"),
    ]
    assert zugeordnet(konfiguration) == {MIRA.id: "Mira", BROK.id: "Brok Eisenfaust"}


def test_je_aufnahme_wird_genau_einmal_gefragt(
    konfiguration, mit_kanal, ohne_espeak, runde, kurze_frist
):
    """Sonst stünde bei jedem Gehen und Wiederkommen dieselbe Frage noch einmal im Postfach."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await dazu(runde.brok, zustand(runde.kanal), zustand())
        await dazu(runde.brok, zustand(), zustand(runde.kanal))
        # Und ein Ortswechsel im selben Kanal ist gar kein Betreten.
        await dazu(runde.brok, zustand(runde.kanal), zustand(runde.kanal))
        await ruhen()

    asyncio.run(ablauf())

    assert len(runde.brok.zwiegespraech) == 1


def test_ein_vermerk_von_vorhin_verschluckt_die_frage_der_naechsten_aufnahme_nicht(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Dieselbe Falle wie beim Wächter des leeren Kanals — deshalb hängt er an der Aufnahme."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        gemerkt = der_lauf(bot, konfiguration).gefragt
        await befehl(bot, "pause")(FakeCtx(runde.mira))
        leer = der_lauf(bot, konfiguration).gefragt
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await ruhen()
        return gemerkt, leer

    gemerkt, leer = asyncio.run(ablauf())

    assert gemerkt is not None and BROK.id in gemerkt[1]
    assert leer is None
    assert len(runde.brok.zwiegespraech) == 2


def test_ein_nachzuegler_wird_beim_betreten_zugeordnet(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    foundry_spieler(konfiguration, "Aelin")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    spaet = FakeMitglied(int(SPAET.id), SPAET.name)

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    asyncio.run(bot.ereignisse["on_voice_state_update"](spaet, zustand(), zustand(runde.kanal)))

    assert zugeordnet(konfiguration)[SPAET.id] == "Aelin"
    assert kanal.geschrieben == [erinnern.BETRETEN_VERMERK.format(name=SPAET.name, spieler="Aelin")]


def schon_einmal_dabei(konfiguration, wer):
    """Jemand, der an einem früheren Abend schon aufgenommen wurde.

    Der Unterschied macht den Test erst zu einem: wer in *keinem* Protokoll steht, ist auch
    ohne jede Prüfung nicht zuzuordnen. Wer in einem alten steht, ist es sehr wohl — und
    darf es trotzdem nicht, wenn er die Ansage von **heute** nie gehört hat.

    Die Sitzung von heute entsteht danach, denn ``latest_session`` nimmt die zuletzt
    **angelegte**: sonst schnitte der Start in den Abend davor mit.
    """
    unsere = unsere_runde(konfiguration)
    frueher = notes.create_session(unsere, played_on="2026-07-30", title="Der Abend davor")
    consent.record(
        unsere,
        session_id=frueher,
        kind=consent.ANSAGE,
        guild_id=KANAL.guild_id,
        channel_id=KANAL.id,
        channel_name=KANAL.name,
        text="Ansage",
        members=(wer,),
    )
    notes.create_session(unsere, kanal_id=str(SITZUNGSKANAL))


def waehrend_der_ansage_ziehen(runde, wohin):
    """Den Bot mitten in der Ansage in einen anderen Kanal verschieben — wie ein Mensch es tut.

    Vor dem Ereignis geht das nicht: dann greift der Wächter über dem ganzen Rückruf und der
    Zweig, um den es hier geht, wird nie erreicht.
    """
    echt = runde.kanal.verbindung.play

    def play(quelle, *, after):
        runde.kanal.verbindung.channel = wohin
        runde.gilde.me.voice.channel = wohin
        echt(quelle, after=after)

    runde.kanal.verbindung.play = play


def test_der_nachzuegler_im_falschen_kanal_wird_nicht_zugeordnet(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Ohne Eintrag im Einwilligungsprotokoll gibt es nichts zuzuordnen.

    Wurde der Bot während der Ansage gezogen, hat der Nachzügler sie nie gehört — dann
    entsteht kein Protokolleintrag, und eine Zuordnung dazu wäre eine Behauptung über
    jemanden, der nie gefragt wurde. Dass wir ihn von früher kennen, ändert daran nichts:
    genau daran hinge sie sonst.
    """
    foundry_spieler(konfiguration, "Aelin")
    schon_einmal_dabei(konfiguration, SPAET)
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    spaet = FakeMitglied(int(SPAET.id), SPAET.name)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    anderswo = FakeSprachkanal(runde.gilde)
    anderswo.id = 78

    async def ablauf():
        waehrend_der_ansage_ziehen(runde, anderswo)
        await bot.ereignisse["on_voice_state_update"](spaet, zustand(), zustand(runde.kanal))

    asyncio.run(ablauf())

    assert zugeordnet(konfiguration)[SPAET.id] is None
    assert spaet.zwiegespraech == []
    assert kanal.geschrieben == []


def test_ein_geschlossenes_zwiegespraech_haelt_den_mitschnitt_nicht_auf(
    konfiguration, mit_kanal, ohne_espeak, runde, caplog
):
    """Keine Antwort ist auch eine: die Spur bleibt unter dem Discord-Namen."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    runde.brok.zwiegespraech_zu = True
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)
    ctx = FakeCtx(runde.mira)

    with caplog.at_level(logging.WARNING):
        asyncio.run(befehl(bot, "start")(ctx))

    assert ctx.antworten == [recorder.GESTARTET]
    assert runde.kanal.verbindung.schneidet
    assert zugeordnet(konfiguration)[BROK.id] is None
    # Im Log steht der Grund, aber kein Name und kein Stapelauszug.
    assert "RuntimeError" in caplog.text
    assert BROK.name not in caplog.text


def test_die_frage_beim_betreten_beantwortet_nur_die_betroffene_person(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Wer wer ist, entscheidet man über sich selbst — nicht über jemand anderen."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    interaktion = antworten(gefragt_wurde(runde.brok), runde.mira, "u-brok eisenfaust")

    assert interaktion.response.bearbeitet[0]["content"] == erinnern.NUR_SELBST
    assert zugeordnet(konfiguration)[BROK.id] is None


def test_eine_frage_von_vorhin_ordnet_nicht_in_die_frische_runde(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Im Zwiegespräch nennt Discord keine Gilde — geprüft wird die Runde gegen sich selbst.

    SQLite vergibt ``runde.id`` nach einer Löschung wieder. Ohne diesen Vergleich legte
    eine Antwort von gestern die Person auf ein Konto einer fremden Gruppe.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    menue = gefragt_wurde(runde.brok)

    unsere = unsere_runde(konfiguration)
    lebenszyklus.loeschen(konfiguration, unsere)
    frisch = runden.anlegen(konfiguration.database_path, "Frisch", guild_id=KANAL.guild_id)
    assert frisch.id == unsere.id

    interaktion = antworten(menue, runde.brok, "u-brok eisenfaust")

    assert interaktion.response.bearbeitet[0]["content"] == chronik.VERALTET
    assert people.overview(frisch).personen == ()


def test_ohne_weg_in_den_kanal_entsteht_keine_zuordnung(
    konfiguration, mit_kanal, ohne_espeak, runde, caplog
):
    """Erst schreiben, dann sagen — und was nicht gesagt werden kann, wird zurückgenommen.

    Der Fehlerfall dieser Reihenfolge ist **Schweigen** und keine Lüge im Kanal. Am Ende
    steht dasselbe wie zuvor: kein Vermerk, keine Zuordnung, und die Frage steht beim
    nächsten Mitschnitt wieder an.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    kanal.stolpert = 99

    with caplog.at_level(logging.WARNING):
        asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert kanal.geschrieben == []
    assert zugeordnet(konfiguration)[MIRA.id] is None
    # Und der Mitschnitt läuft: ein verschlossener Kanal ist kein Grund, nicht zu hören.
    assert runde.kanal.verbindung.schneidet
    assert MIRA.name not in caplog.text


@pytest.fixture
def sitzung_ohne_kanal(konfiguration):
    """Eine Sitzung aus der Zeit vor dem Sitzungs-Kanal — es gibt dorthin gar keinen Weg."""
    return notes.create_session(unsere_runde(konfiguration))


def test_ohne_kanal_an_der_sitzung_entsteht_keine_zuordnung(
    konfiguration, sitzung_ohne_kanal, ohne_espeak, runde
):
    """Der zweite der drei Wege, auf denen der Vermerk nicht hinauskommt.

    ``_in_den_kanal`` gibt hier ``False`` zurück, statt zu werfen — der werfende Weg
    allein deckt die Zusage also nicht ab.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = FakeTextkanal()
    bot.kanaele[SITZUNGSKANAL] = kanal

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert kanal.geschrieben == []
    assert zugeordnet(konfiguration)[MIRA.id] is None
    assert runde.kanal.verbindung.schneidet


def test_ein_fortgeraeumter_kanal_laesst_keine_zuordnung_entstehen(
    konfiguration, sitzung_im_kanal, ohne_espeak, runde
):
    """Und der dritte: den Kanal gibt es nicht mehr, Discord antwortet mit 404.

    Auch das ist ein ``False`` und kein Wurf. Fort ist dabei nicht »zuckt« — der 404 käme
    beim nächsten Betreten genauso wieder, also entsteht die Zuordnung auch später nicht.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    fort = FakeFortgeraeumterKanal()
    bot.kanaele[SITZUNGSKANAL] = fort

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert fort.versuche == 1
    assert zugeordnet(konfiguration)[MIRA.id] is None
    assert runde.kanal.verbindung.schneidet


def test_ein_gescheitertes_festschreiben_bleibt_still(
    konfiguration, mit_kanal, ohne_espeak, runde, monkeypatch, caplog
):
    """Geht das Schreiben nicht durch — die SQLite hängt, etwa —, wird auch nichts gesagt.

    Der Gewinn der umgedrehten Reihenfolge: es gibt keinen Zustand, in dem ein Satz im
    Kanal eine Zuordnung behauptet, die nie entstanden ist.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)

    def stolpert(*_args, **_rest):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(people, "confirm", stolpert)

    with caplog.at_level(logging.ERROR):
        asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert zugeordnet(konfiguration) == {MIRA.id: None, BROK.id: None}
    assert kanal.geschrieben == []
    assert "RuntimeError" in caplog.text
    assert MIRA.name not in caplog.text


def test_vermerkt_wird_nur_was_zuordnen_auch_geschrieben_hat(
    konfiguration, mit_kanal, ohne_espeak, runde, monkeypatch
):
    """Weist ``zuordnen`` ab, steht nichts im Kanal — sonst wäre der Satz eine Behauptung."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)

    monkeypatch.setattr(
        erinnern,
        "zuordnen",
        lambda *_args, **_rest: erinnern.Zugeordnet(satz=erinnern.SPIELER_VERGEBEN),
    )

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert kanal.geschrieben == []
    assert zugeordnet(konfiguration)[MIRA.id] is None


def test_ohne_ruecknahme_bleibt_eine_wahre_zuordnung_ohne_ansage_stehen(
    konfiguration, mit_kanal, ohne_espeak, runde, monkeypatch, caplog
):
    """Der eine Fall, in dem beides danebengeht: der Vermerk **und** seine Rücknahme.

    Dann steht in der SQLite eine **wahre** Zuordnung, von der die Runde nichts erfährt.
    Sie zu verwerfen geht nicht — das Zurücknehmen ist ja gerade gescheitert —, und
    nachgeholt wird nichts. Das ist die schwächere, ehrliche Zusage: der Fehlerfall ist
    Schweigen statt einer Lüge, aber er ist echt, und er steht im Log.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    kanal.stolpert = 99
    echt = people.confirm
    geschrieben = []

    def einmal(*args, **rest):
        # Das Festschreiben geht durch, die Rücknahme dahinter nicht.
        if geschrieben:
            raise RuntimeError("database is locked")
        geschrieben.append(args)
        return echt(*args, **rest)

    monkeypatch.setattr(people, "confirm", einmal)

    with caplog.at_level(logging.ERROR):
        asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert zugeordnet(konfiguration)[MIRA.id] == "Mira"
    assert kanal.geschrieben == []
    assert "ohne Ansage" in caplog.text
    assert MIRA.name not in caplog.text


def test_die_ruecknahme_loescht_keine_entscheidung_von_zwischendurch(
    konfiguration, mit_kanal, ohne_espeak, runde, caplog
):
    """Zwischen Schreiben und Rücknahme liegt ein Gang ans Netz — und darin
    `/chronicle zuordnung`.

    Der Kanal hier tut genau das, was ein langsames Discord zulässt: er lässt jemanden
    dieselbe Person auf ein anderes Konto legen und wirft danach. Nähme die Rücknahme
    blind, was gerade dasteht, wäre diese Entscheidung still fort — niemand hat sie
    zurückgenommen, und trotzdem stünde am Ende nichts mehr da.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    unsere = unsere_runde(konfiguration)

    async def dazwischenfunken(text):
        await asyncio.sleep(0)
        people.confirm(unsere, {MIRA.id: "u-brok eisenfaust"})
        raise RuntimeError("Discord hat abgelehnt")

    kanal.send = dazwischenfunken

    with caplog.at_level(logging.INFO):
        asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    assert zugeordnet(konfiguration)[MIRA.id] == "Brok Eisenfaust"
    assert "nicht mehr, was eben geschrieben wurde" in caplog.text
    assert MIRA.name not in caplog.text


def test_wer_sich_im_zwiegespraech_waehlt_steht_danach_im_kanal(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Je schwächer der Beleg, desto mehr Tageslicht — hier stimmt gar kein Name überein.

    Der Satz sagt den Unterschied zur Namensgleichheit: gewählt, nicht erkannt.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    antworten(gefragt_wurde(runde.brok), runde.brok, "u-brok eisenfaust")

    assert kanal.geschrieben[-1] == erinnern.MENUE_VERMERK.format(
        name=BROK.name, spieler="Brok Eisenfaust"
    )
    assert erinnern.MENUE_VERMERK != erinnern.BETRETEN_VERMERK


def test_ein_verschlossener_kanal_wirft_die_eigene_antwort_nicht_weg(
    konfiguration, mit_kanal, ohne_espeak, runde, caplog
):
    """Anders als beim 1:1-Vermerk ist der Satz hier keine Bedingung — sie hat geantwortet.

    Und die Antwort auf den Klick steht schon: eine durchgereichte Ausnahme machte daraus
    ein »hat nicht geklappt«, obwohl die Zuordnung entstanden ist.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    kanal.stolpert = 99

    with caplog.at_level(logging.ERROR):
        interaktion = antworten(gefragt_wurde(runde.brok), runde.brok, "u-brok eisenfaust")

    assert zugeordnet(konfiguration)[BROK.id] == "Brok Eisenfaust"
    (bearbeitet,) = interaktion.response.bearbeitet
    assert bearbeitet["content"] == erinnern.ZUGEORDNET.format(
        name=BROK.name, spieler="Brok Eisenfaust"
    )
    assert "ungesagt" in caplog.text


def test_wer_im_zwiegespraech_abgewiesen_wird_steht_nicht_im_kanal(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Gegenprobe: vermerkt wird, was entstanden ist — nicht, was versucht wurde."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))

    antworten(gefragt_wurde(runde.brok), runde.brok, "u-mira")

    assert kanal.geschrieben == [erinnern.BETRETEN_VERMERK.format(name=MIRA.name, spieler="Mira")]


class FakeCtxMitLoeschung(FakeCtx):
    """Ein Befehl, dessen Antwort so lange braucht, dass die Runde inzwischen fort ist.

    Kein konstruierter Fall: die Frist der verabschiedeten Runde läuft als Faden auf
    derselben Schleife, und ``await ctx.respond`` gibt an sie ab. Danach fragt der Start
    reihum jede anwesende Person — mit einer ``Runde``, die es so nicht mehr gibt, und
    unter einer Kennung, die SQLite an eine **fremde Gilde** vergeben hat.

    Die fremde Runde bekommt dieselben Discord-Kennungen: derselbe Mensch spielt in zwei
    Kampagnen, das ist ausdrücklich vorgesehen. Nur heißt dort niemand wie ein Konto, also
    wäre das Ergebnis einer verwechselten Runde eine Frage — an eine Person, die in dieser
    Kampagne gerade gar nichts tut.
    """

    def __init__(self, autor, konfiguration):
        super().__init__(autor)
        self.konfiguration = konfiguration
        self.frisch = None

    async def respond(self, text, **rest):
        await super().respond(text, **rest)
        if self.frisch is not None:
            return
        alte = unsere_runde(self.konfiguration)
        lebenszyklus.loeschen(self.konfiguration, alte)
        self.frisch = runden.anlegen(self.konfiguration.database_path, "Nebenan", guild_id="12")
        assert self.frisch.id == alte.id
        sitzung = notes.create_session(self.frisch, kanal_id=str(SITZUNGSKANAL))
        consent.record(
            self.frisch,
            session_id=sitzung,
            kind=consent.ANSAGE,
            guild_id="12",
            channel_id="78",
            channel_name="Nebenan",
            text="Ansage",
            members=(MIRA, BROK),
        )
        foundry_spieler(self.konfiguration, "Aelin", wo=self.frisch)


def test_eine_aufnahme_von_vorhin_ordnet_nicht_in_die_frische_runde(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Die Aufnahme hält ihre Runde — die Kennung kann inzwischen einer fremden Gilde gehören.

    SQLite vergibt ``runde.id`` nach einer Löschung wieder, und die Sitzungskennung ebenso.
    Verglichen wird deshalb die **Kennung** der Runde und nicht ihre Id; ohne diesen
    Vergleich stünde die Frage »wer bist du in Foundry« im Postfach zweier Menschen, und
    beantwortet legte sie sie auf ein Konto einer Kampagne, in der sie gerade nicht spielen.
    """
    foundry_spieler(konfiguration, "Mira")
    bot = gateway.baue(konfiguration)
    kanal = mit_kanal(bot)
    ctx = FakeCtxMitLoeschung(runde.mira, konfiguration)

    asyncio.run(befehl(bot, "start")(ctx))

    assert ctx.antworten == [recorder.GESTARTET]
    assert [wer.confirmed for wer in people.overview(ctx.frisch).personen] == [None, None]
    assert runde.mira.zwiegespraech == []
    assert runde.brok.zwiegespraech == []
    assert kanal.geschrieben == []


def test_zwei_ereignisse_im_selben_schwung_fragen_nur_einmal(
    konfiguration, mit_kanal, ohne_espeak, runde
):
    """Deshalb steht der Vermerk vor dem ersten ``await`` und nicht dahinter.

    py-cord stellt jedes Sprachereignis als eigenen Task zu. Wer erst nach dem Zustellen
    vermerkt, findet im zweiten Task nichts vor — und die Frage stünde zweimal im Postfach.
    """
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)
    spaet = FakeMitglied(int(SPAET.id), SPAET.name)
    dazu = bot.ereignisse["on_voice_state_update"]

    async def ablauf():
        await befehl(bot, "start")(FakeCtx(runde.mira))
        await asyncio.gather(
            dazu(spaet, zustand(), zustand(runde.kanal)),
            dazu(spaet, zustand(), zustand(runde.kanal)),
        )
        await ruhen()

    asyncio.run(ablauf())

    assert len(spaet.zwiegespraech) == 1


def test_die_nachbarrunde_bekommt_von_alldem_nichts(konfiguration, mit_kanal, ohne_espeak, runde):
    """Neben der Gilden-Runde steht in jeder dieser Datenbanken noch die erste."""
    foundry_spieler(konfiguration, "Mira", "Brok Eisenfaust")
    bot = gateway.baue(konfiguration)
    mit_kanal(bot)
    fremde = erste_runde(konfiguration)
    assert fremde.id != unsere_runde(konfiguration).id

    asyncio.run(befehl(bot, "start")(FakeCtx(runde.mira)))
    antworten(gefragt_wurde(runde.brok), runde.brok, "u-brok eisenfaust")

    assert people.overview(fremde).personen == ()


# -- Gegen das echte py-cord ------------------------------------------------------------
#
# Hier steht keine Attrappe. Zweimal hintereinander fiel erst auf der Box auf, dass wir
# das Protokoll der Bibliothek verfehlt hatten — beim zweiten Mal an genau der Zeile, die
# diese Tests jetzt ausführen.


class LeererReader:
    """Der Router legt den Reader nur ab; zum Registrieren braucht er nichts von ihm."""


def echte_senke(konfiguration, sitzung_id, stimme=None):
    aufnahme = Aufnahme(
        konfiguration,
        unsere_runde(konfiguration),
        sitzung_id,
        KANAL,
        inhaltssprache=sprachen.DEFAULT,
    )
    aufnahme.ansage_protokollieren((MIRA,))
    return aufnahme, gateway._senke(stimme or FakeStimme(), aufnahme)


# Der Stand, auf dem der Sprach-Empfang unter DAVE funktioniert: Pycord-PR #3159,
# unveröffentlicht (#60). Die Begründung steht in ``pyproject.toml``.
PYCORD_COMMIT = "326b72acc8d1d952ac002fe07ca65581cf5952bc"


def _auf_den_commit_genagelt(zeile: str) -> bool:
    return f"pycord@{PYCORD_COMMIT}" in zeile


def _py_cord_zeilen() -> list[str]:
    pyproject = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    return [zeile for name in ("discord", "dev") for zeile in extras[name] if "py-cord" in zeile]


def test_beide_extras_nageln_py_cord_auf_denselben_commit():
    """Das Image installiert ``[discord]``, die CI ``[dev]`` — beide müssen dasselbe sein.

    Eine CI, die gegen eine andere Fassung prüft als das Image mitbringt, prüft die
    falsche; und ein Branchname statt eines Commits macht aus dem Bau von gestern einen
    anderen als den von heute.
    """
    zeilen = _py_cord_zeilen()

    assert len(zeilen) == 2, f"py-cord steht nicht in beiden Extras: {zeilen}"
    assert all(_auf_den_commit_genagelt(zeile) for zeile in zeilen), zeilen


def test_eine_veroeffentlichung_oder_ein_branchname_faellt_durch_dieselbe_pruefung():
    """Gegenprobe: genau die beiden Zeilen, die hier nicht stehen dürfen."""
    assert not _auf_den_commit_genagelt("py-cord[voice]>=2.8.1")
    assert not _auf_den_commit_genagelt(
        "py-cord[voice] @ git+https://github.com/Pycord-Development/pycord@fix/voice-rec-2"
    )


def test_installiert_ist_der_festgenagelte_stand_und_nicht_die_veroeffentlichung():
    """Die Zeile in ``pyproject.toml`` nützt nichts, wenn danebenher 2.8.1 liegt.

    py-cord belegt das Paket ``discord``; eine ältere Installation im selben Umfeld
    beantwortet den Import und der ganze Abschnitt hier prüft die falsche Bibliothek. Die
    Veröffentlichung meldet sich als ``2.8.1``, der festgenagelte Stand trägt den Commit
    im Versionsstring.
    """
    pytest.importorskip("discord")
    from importlib.metadata import version

    gemeldet = version("py-cord")

    assert PYCORD_COMMIT[:8] in gemeldet, f"py-cord ist {gemeldet}, nicht der Stand aus #3159"


def test_pycord_entschluesselt_vor_dem_dekodieren_und_stirbt_nicht_am_rekey():
    """Der eigentliche Grund für den festgenagelten Stand (#60).

    In der Veröffentlichung 2.8.1 ruft ``PacketDecoder._decode_packet`` ``dave.decrypt``
    auf dem bereits **dekodierten** PCM auf; der Dekoder sieht davor Rauschen und wirft
    ``OpusError: corrupted stream``, woran der Empfang stirbt — keine einzige Spur. Der
    festgenagelte Stand entschlüsselt in ``PacketDecryptor.decrypt_rtp``, also **vor** dem
    Dekodieren, und fängt den Rest — beim Rekey kann weiter ein ``OpusError`` kommen — mit
    Paketverlust-Verschleierung ab, statt den Paket-Router umfallen zu lassen.
    """
    pytest.importorskip("discord")
    from discord.opus import PacketDecoder
    from discord.voice.receive.reader import PacketDecryptor

    dekodieren = inspect.getsource(PacketDecoder._decode_packet)
    entschluesseln = inspect.getsource(PacketDecryptor.decrypt_rtp)

    assert "dave.decrypt" not in dekodieren
    assert "OpusError" in dekodieren
    assert "dave.decrypt" in entschluesseln


def test_die_fassung_von_2_8_1_faellt_durch_dieselbe_pruefung():
    """Gegenprobe: der Ausschnitt aus dem veröffentlichten ``opus.py``, wörtlich."""
    veroeffentlicht = (
        "            if user_id is not None and in_dave and dave.can_passthrough(user_id):\n"
        "                pcm = dave.decrypt(user_id, davey.MediaType.audio, pcm)\n"
    )

    assert "dave.decrypt" in veroeffentlicht
    assert "OpusError" not in veroeffentlicht


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


def test_pycords_basissenke_bringt_das_empfangs_protokoll_selbst_mit():
    """Der sichtbare Unterschied zum veröffentlichten 2.8.1 — und damit die Gegenprobe.

    In der Veröffentlichung fehlt der Basisklasse ``__sink_listeners__``; dort scheiterte
    ``SinkEventRouter`` mit einem ``AttributeError``, an einer schlichten Ableitung ebenso
    wie an py-cords eigener ``WaveSink``. Der festgenagelte Stand legt das Protokoll in
    die Basisklasse. Wer diesen Test rot sieht, sitzt wieder auf einer Fassung ohne
    Pycord-PR #3159 — dann ist auch der Empfang wieder kaputt.
    """
    pytest.importorskip("discord")
    from discord.sinks import Sink, WaveSink
    from discord.voice.receive.router import SinkEventRouter

    class SchlichteSenke(Sink):
        def write(self, data, user):
            pass

    SinkEventRouter(SchlichteSenke(), LeererReader())
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


class SpielzeugSprachclient:
    """So viel ``VoiceClient``, wie py-cords ``AudioReader`` zum Bauen und Beenden braucht."""

    mode = "xsalsa20_poly1305"
    secret_key = bytes(32)

    def __init__(self):
        self._connection = types.SimpleNamespace(
            add_socket_listener=lambda rueckruf: None,
            remove_socket_listener=lambda rueckruf: None,
            # Der Empfangspfad fragt beim Dekodieren, ob eine DAVE-Sitzung laeuft.
            dave_session=None,
        )
        # Woher der Dekoder den Sprecher zu einer SSRC nimmt; leer heisst »noch unbekannt«.
        self._ssrc_to_id = {}
        self.reader = None
        self.beendet_von = []

    def stop_recording(self):
        # Genau dieser Aufruf steht im ``finally`` von ``PacketRouter.run``.
        self.beendet_von.append(threading.current_thread())
        self.reader.stop()


class WartetBisGeweckt:
    """py-cords ``MultiDataEvent``, so weit der Router ihn braucht.

    ``register``/``unregister`` sind hier folgenlos: der echte Weckruf traegt die Decoder
    ein und aus, waehrend ``PacketRouter._do_run`` ueber genau diese Liste laeuft — was
    hier eine Schleife ueber eine Liste waere, die sich unter ihr aendert.
    """

    def __init__(self, *decoder):
        self.items = list(decoder)
        self._weckruf = threading.Event()

    def wait(self):
        self._weckruf.wait()
        self._weckruf.clear()

    def notify(self):
        self._weckruf.set()

    def clear(self):
        self.items = []

    def register(self, decoder):
        return None

    def unregister(self, decoder):
        return None


class StirbtBeimDekodieren:
    """Was ``PacketDecoder.pop_data`` tut, wenn es doch einmal durchschlaegt."""

    def __init__(self, fehler):
        self.fehler = fehler

    def pop_data(self, **rest):
        raise self.fehler


class EinPaket:
    """Der Jitter-Puffer eines Decoders, auf ein Paket eingedampft."""

    def __init__(self, paket):
        self._pakete = [paket]

    def pop(self, timeout=0):
        return self._pakete.pop() if self._pakete else None

    def is_ready(self):
        return bool(self._pakete)


class GibtAufUndVerschleiert:
    """py-cords ``Decoder`` an der einen Stelle, die zaehlt: der erste Versuch wirft.

    libopus ist in der Testumgebung nicht geladen, ein echter ``Decoder`` also nicht zu
    bauen. Der Code unter Pruefung ist ohnehin ``_decode_packet`` — was es mit dem Wurf
    anfaengt, nicht das Dekodieren selbst.
    """

    def __init__(self, fehler, ersatz):
        self.fehler = fehler
        self.ersatz = ersatz
        self.aufrufe = []

    def decode(self, data, *, fec=False):
        self.aufrufe.append(data)
        if len(self.aufrufe) == 1:
            raise self.fehler
        return self.ersatz


def _decoder_am_echten_dekodierpfad(router, opus_decoder, paket):
    """Ein ``PacketDecoder``, dessen ``pop_data``/``_decode_packet`` echt sind.

    Gebaut wird an ``__init__`` vorbei, weil das einen echten ``Decoder`` anlegt und der
    ohne libopus nicht zu haben ist. Alles, was der Empfangspfad danach liest, steht hier.
    """
    from discord.opus import PacketDecoder

    decoder = PacketDecoder.__new__(PacketDecoder)
    decoder.router = router
    decoder.ssrc = 4711
    decoder._decoder = opus_decoder
    decoder._buffer = EinPaket(paket)
    decoder._cached_id = None
    decoder._last_seq = decoder._last_ts = -1
    return decoder


def test_ein_dekodierfehler_toetet_den_paket_router_nicht(konfiguration, sitzung_id, caplog):
    """Warum wir auf einem unveroeffentlichten py-cord sitzen — gegen die echte Bibliothek.

    Echter ``AudioReader``, echter ``PacketRouter``, echter Kanal und py-cords eigenes
    ``PacketDecoder._decode_packet``. Der festgenagelte Stand aus Pycord-PR #3159 faengt
    den ``OpusError`` an der Quelle ab und dekodiert mit Paketverlust-Verschleierung
    weiter — ``decode(None)`` ist genau das. Ein Schluesselwechsel kostet damit Rahmen
    statt der ganzen Aufnahme: der Router lebt weiter, der Mitschnitt laeuft, und in der
    Spur steht der Ersatz.

    Bis 2.8.1 flog der Fehler bis in ``PacketRouter.run``, der Empfang starb, und
    `/session start` sah dabei gruen aus. Wer diesen Test rot sieht, sitzt wieder auf
    einer Fassung ohne #3159.
    """
    pytest.importorskip("discord")
    from discord.opus import OpusError
    from discord.voice.receive.reader import AudioReader

    aufnahme, senke = echte_senke(konfiguration, sitzung_id)
    client = SpielzeugSprachclient()
    client.reader = reader = AudioReader(senke, client, start=False)
    reader.active = True
    senke.init(client)

    ersatz = stille(480)
    opus_decoder = GibtAufUndVerschleiert(OpusError(-4, "corrupted stream"), ersatz)
    paket = types.SimpleNamespace(decrypted_data=b"unlesbar", sequence=7, timestamp=960)
    decoder = _decoder_am_echten_dekodierpfad(reader.packet_router, opus_decoder, paket)
    reader.packet_router.waiter = WartetBisGeweckt(decoder)

    with caplog.at_level(logging.WARNING, logger="discord.opus"):
        reader.packet_router.start()
        try:
            reader.packet_router.waiter.notify()
            for _ in range(500):
                if aufnahme.pakete:
                    break
                time.sleep(0.01)
            # Der Zustand genau nach dem verschluckten Rahmen: gleich beendet dieser Test
            # den Router selbst, und ``PacketRouter.run`` ruft in seinem ``finally``
            # immer ``stop_recording`` — auch beim geordneten Ende.
            lebt_weiter = reader.packet_router.is_alive()
            beendet_von = list(client.beendet_von)
        finally:
            reader.packet_router.stop()
            reader.packet_router.join(timeout=5)

    assert aufnahme.pakete == 1, "der verschleierte Rahmen muss in der Spur landen"
    assert opus_decoder.aufrufe == [b"unlesbar", None], (
        "auf den Wurf muss der Ersatzversuch ohne Daten folgen — das ist die Verschleierung"
    )
    assert reader.error is None, "der Fehler darf den Empfaenger nicht erreichen"
    assert lebt_weiter, "der Paket-Router muss den Dekodierfehler ueberleben"
    assert beendet_von == [], "und den Mitschnitt nicht von selbst beenden"
    # Sichtbar bleibt er allein hier — genau das sagt ``PROBE_RAHMENVERLUST`` dem Betreiber.
    warnungen = [satz for satz in caplog.messages if "Opus decode failed" in satz]
    assert warnungen, (
        "py-cord meldet den verschluckten Rahmen nicht mehr so — dann stimmt der Satz "
        f"ueber das Log im Bericht nicht mehr: {recorder.PROBE_RAHMENVERLUST}"
    )


def test_ein_sterbender_paket_router_beendet_den_mitschnitt_aus_seinem_eigenen_faden(
    konfiguration, sitzung_id
):
    """Das einzige Signal, das ein Abbruch noch hergibt — gegen die echte Bibliothek.

    ``PacketRouter`` ist ein eigener Kanal. Faellt er doch einmal um, faengt ``run`` den
    Fehler, legt ihn in ``reader.error`` und ruft aus **seinem** Faden
    ``client.stop_recording()``. Im Befehl, der den Mitschnitt gestartet hat, kommt nichts
    an; heraus gereicht wird der Fehler seit #3159 nirgends mehr. Was bleibt, ist, dass
    nicht mehr mitgeschnitten wird — daran haengt ``Sprachverbindung.mitschnitt_beenden``.
    """
    pytest.importorskip("discord")
    from discord.opus import OpusError
    from discord.voice.receive.reader import AudioReader

    _, senke = echte_senke(konfiguration, sitzung_id)
    client = SpielzeugSprachclient()
    client.reader = reader = AudioReader(senke, client, start=False)
    reader.active = True
    reader.packet_router.waiter = WartetBisGeweckt(
        StirbtBeimDekodieren(OpusError(-4, "corrupted stream"))
    )
    reader.packet_router.waiter.notify()

    reader.packet_router.start()
    reader.packet_router.join(timeout=5)

    assert not reader.packet_router.is_alive()
    assert isinstance(reader.error, OpusError)
    # Der Mitschnitt ist aus, und beendet hat ihn ein anderer Faden als dieser.
    assert not reader.is_listening()
    assert client.beendet_von == [reader.packet_router]
    assert client.beendet_von[0] is not threading.current_thread()


def test_pycord_ruft_den_abschluss_rueckruf_ohne_zusatzargumente_gar_nicht_auf(
    konfiguration, sitzung_id
):
    """Warum es hier keinen ``after``-Rueckruf mehr gibt — gegen die echte Bibliothek.

    ``AudioReader._stop`` ruft ``self.after(self.sink, *self.args)``, und nur ``if
    self.after and self.args``. ``start_recording(senke, rueckruf)`` reicht als ``args``
    das leere Tupel durch: der Rueckruf laeuft also nie. Und liefe er, bekaeme er die
    Senke als erstes Argument, nicht den Fehler — 2.8.1 rief noch ``after(self.error)``.

    Beides zusammen macht jeden Abschluss-Rueckruf hier zu totem Code, der obendrein die
    Senke fuer einen Fehler halten wuerde. Wer diesen Test rot sieht, kann den Rueckruf
    wiederhaben — dann aber mit der Signatur, die dieser Test dann zeigt.
    """
    pytest.importorskip("discord")
    from discord.voice.receive.reader import AudioReader

    _, senke = echte_senke(konfiguration, sitzung_id)
    client = SpielzeugSprachclient()
    gerufen = []

    client.reader = reader = AudioReader(
        senke, client, after=lambda *was: gerufen.append(was), args=(), start=False
    )
    reader.active = True
    reader.stop()

    assert gerufen == [], "ohne Zusatzargumente ruft py-cord den Rueckruf nicht auf"

    _, zweite = echte_senke(konfiguration, sitzung_id)
    zweiter = AudioReader(
        zweite, client, after=lambda *was: gerufen.append(was), args=("mitgegeben",), start=False
    )
    zweiter.active = True
    zweiter.stop()

    assert gerufen == [(zweite, "mitgegeben")], (
        "und mit Zusatzargumenten bekommt er die Senke, nicht den Fehler"
    )


def test_pycord_verdrahtet_die_senke_beim_wechsel_weiterhin_nicht(konfiguration, sitzung_id):
    """Warum ``mitschneiden`` ``senke.init`` selbst ruft, obwohl py-cord es inzwischen tut.

    ``AudioReader.__init__`` ruft ``sink.init(client)`` auf dem festgenagelten Stand
    selbst — ``set_sink`` aber nicht, und ``PacketDecoder._decode_packet`` beginnt
    weiterhin mit ``assert self.sink.client``. Eine Senke, die auf diesem Weg
    hereinkaeme, brachte den Empfaenger beim ersten Paket um; der Preis eines vergessenen
    ``vc`` ist eine ganze Sitzung. Also bleibt die Zeile stehen, auch wenn sie im
    Normalfall denselben Wert ein zweites Mal setzt.
    """
    pytest.importorskip("discord")
    from discord.opus import PacketDecoder
    from discord.voice.receive.reader import AudioReader

    _, erste = echte_senke(konfiguration, sitzung_id)
    _, zweite = echte_senke(konfiguration, sitzung_id)
    client = SpielzeugSprachclient()
    client.reader = reader = AudioReader(erste, client, start=False)

    assert erste.client is client, "beim Bauen verdrahtet py-cord selbst"

    reader.set_sink(zweite)

    assert zweite.client is None, "beim Wechsel nicht — deshalb bleibt ``senke.init`` stehen"
    assert "assert self.sink.client" in inspect.getsource(PacketDecoder._decode_packet), (
        "ohne diesen ``assert`` waere die Verdrahtung folgenlos — und die Zeile entbehrlich"
    )
