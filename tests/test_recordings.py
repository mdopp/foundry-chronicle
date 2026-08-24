"""Diktat statt Tippen: annehmen, warten, übernehmen.

Geprüft wird gegen die Module, nicht gegen eine Seite: Spielinhalte hat die Weboberfläche
seit #157 keine mehr. Kein Test lädt ein Spracherkennungsmodell; an dessen Stelle steht
ein erfundenes.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import queue
import sqlite3
import threading
import wave
from datetime import UTC, datetime, timedelta

import pytest
from conftest import GRENZE, runde

from chronicle import db, notes, recordings
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.transcribe import service
from chronicle.transcribe.client import Segment, TranscriberError, TranscriberUnreachable

DIKTAT = b"kein echtes Audio, aber Bytes"


class Erkenner:
    """Ein Modell, das nichts lädt und liefert, was der Test verlangt."""

    name = "erfundenes-modell"

    def __init__(self, *segmente, fehler=None):
        self.segmente = segmente or (
            Segment(start=0.0, end=2.5, text=" Auf dem Heimweg fällt mir ein:"),
            Segment(start=2.5, end=7.0, text=" die Wirtin hat gelogen."),
        )
        self.fehler = fehler

    def transcribe(self, audio_path, *, hotwords=(), sprache="en"):
        if self.fehler is not None:
            raise self.fehler
        yield from self.segmente


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")


@pytest.fixture
def sitzung_id(config):
    db.init(config.database_path)
    return notes.create_session(runde(config), played_on="2026-08-06", title="Der Keller")


def hochladen(config, sitzung_id, name="Sprachmemo 3.m4a", inhalt=DIKTAT):
    """Eine angenommene Spur, so wie der Sitzungs-Thread sie ablegt: Datei, dann Zeile."""
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings.target_path(config.recordings_dir, sitzung_id, name)
    ziel.write_bytes(inhalt)
    return recordings.enqueue(runde(config), sitzung_id, ziel.name)


# --- Hochladen ----------------------------------------------------------------------


def test_ein_upload_landet_auf_der_platte_und_in_der_warteschlange(config, sitzung_id):
    aufnahme = hochladen(config, sitzung_id)

    assert aufnahme.status == recordings.WARTET
    spuren = list(config.recordings_dir.iterdir())
    assert [pfad.read_bytes() for pfad in spuren] == [DIKTAT]
    warteschlange = recordings.pending(runde(config))
    assert [(zeile.session_id, zeile.filename) for zeile in warteschlange] == [
        (sitzung_id, spuren[0].name)
    ]


def test_die_spur_liegt_nicht_im_gesicherten_datenverzeichnis(config, sitzung_id):
    hochladen(config, sitzung_id)
    spur = next(config.recordings_dir.iterdir())
    assert not spur.resolve().is_relative_to(config.data_dir.resolve())


def test_mehrere_diktate_je_sitzung_stehen_nebeneinander(config, sitzung_id):
    hochladen(config, sitzung_id)
    hochladen(config, sitzung_id)

    assert len(list(config.recordings_dir.iterdir())) == 2
    assert len(recordings.for_session(runde(config), sitzung_id)) == 2


def test_die_grenze_ist_grosszuegig_genug_fuer_einen_abend():
    assert recordings.MAX_BYTES >= 256 * 1024 * 1024


def test_die_grenze_ist_ein_halbes_gigabyte():
    """Die Zahl benannt, nicht eingesetzt — sonst wandert der Test mit ihr mit."""
    assert recordings.MAX_BYTES == 512 * 1024 * 1024


def test_ein_diktat_zu_einer_unbekannten_sitzung_gibt_es_nicht(config, sitzung_id):
    with pytest.raises(sqlite3.IntegrityError):
        hochladen(config, 999)

    assert recordings.pending(runde(config)) == ()


def test_zwei_gleich_benannte_spuren_ueberschreiben_einander_nicht(config, sitzung_id):
    erste = recordings.target_path(config.recordings_dir, sitzung_id, "memo.m4a")
    config.recordings_dir.mkdir(parents=True)
    erste.write_bytes(DIKTAT)

    zweite = recordings.target_path(config.recordings_dir, sitzung_id, "memo.m4a")

    assert zweite != erste


def test_eine_schon_eingereihte_spur_wird_als_solche_gemeldet(config, sitzung_id):
    """Ein zweiter Anlauf über mehrere Spuren muss die durchgekommenen überspringen können.

    Ohne diesen Unterschied bliebe er an der ersten hängen, und die Spuren dahinter kämen
    nie in die Warteschlange (#104).
    """
    recordings.enqueue(runde(config), sitzung_id, "sitzung1-Mira.wav")

    with pytest.raises(recordings.BereitsEingereiht):
        recordings.enqueue(runde(config), sitzung_id, "sitzung1-Mira.wav")

    assert len(recordings.pending(runde(config))) == 1


def test_ein_anderer_verstoss_bleibt_ein_fehlschlag(config, sitzung_id):
    """»Übersprungen« gilt nur der doppelten Datei — sonst verschwände ein echter Fehler."""
    with pytest.raises(sqlite3.IntegrityError) as gefangen:
        recordings.enqueue(runde(config), 4711, "sitzung4711-Brok.wav")

    assert not isinstance(gefangen.value, recordings.BereitsEingereiht)
    assert recordings.pending(runde(config)) == ()


# --- Die Quittung: ehrlich, ohne geratenen Fortschritt -------------------------------


def test_eine_angenommene_spur_wartet_und_meldet_keinen_geratenen_fortschritt(config, sitzung_id):
    hochladen(config, sitzung_id)

    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)

    assert aufnahme.status == recordings.WARTET
    assert aufnahme.detail is None
    assert aufnahme.text == ""


def test_die_spur_traegt_den_stand_des_jobs_und_nicht_nur_dass_etwas_da_ist(config, sitzung_id):
    hochladen(config, sitzung_id)
    aufnahme = recordings.pending(runde(config))[0]

    recordings.mark(runde(config), aufnahme.id, recordings.LAEUFT)

    assert recordings.get(runde(config), aufnahme.id).status == recordings.LAEUFT
    assert recordings.pending(runde(config)) == ()


def test_ein_gescheiterter_lauf_steht_mit_grund_an_der_spur(config, sitzung_id):
    hochladen(config, sitzung_id)
    aufnahme = recordings.pending(runde(config))[0]

    recordings.mark(
        runde(config), aufnahme.id, recordings.GESCHEITERT, "Die Tondatei liegt nicht mehr da."
    )

    nachher = recordings.get(runde(config), aufnahme.id)
    assert nachher.status == recordings.GESCHEITERT
    assert nachher.detail == "Die Tondatei liegt nicht mehr da."


# --- Derselbe Stapel, dieselbe Warteschlange ----------------------------------------


def test_der_stapel_macht_aus_dem_diktat_ein_transkript(config, sitzung_id):
    hochladen(config, sitzung_id)

    meldungen = service.run_queue(config, runde(config), model=Erkenner())

    assert "2 segments" in meldungen[0]
    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    assert aufnahme.status == recordings.FERTIG
    assert aufnahme.text == "Auf dem Heimweg fällt mir ein: die Wirtin hat gelogen."


def test_ein_leerer_lauf_laedt_kein_modell(config, monkeypatch):
    def keins(_config):
        raise AssertionError("ohne Wartende wird kein Modell geladen")

    monkeypatch.setattr(service, "model_from_config", keins)
    assert service.run_queue(config, runde(config)) == ()


def test_eine_verschwundene_spur_wird_gemeldet_statt_still_zu_scheitern(config, sitzung_id):
    hochladen(config, sitzung_id)
    next(config.recordings_dir.iterdir()).unlink()

    service.run_queue(config, runde(config), model=Erkenner())

    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    assert aufnahme.status == recordings.GESCHEITERT
    assert "is no longer there" in aufnahme.detail


def test_eine_spur_ohne_aeusserung_ist_kein_fehlschlag(config, sitzung_id):
    """Übersprungen heißt fertig, nicht gescheitert — und die Meldung sagt das auch (#142)."""
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    pfad = config.recordings_dir / f"sitzung{sitzung_id}-teek.wav"
    with wave.open(str(pfad), "wb") as datei:
        datei.setnchannels(2)
        datei.setsampwidth(2)
        datei.setframerate(48000)
        datei.writeframes(bytes(int(48000 * 0.08) * 4))
    recordings.enqueue(runde(config), sitzung_id, pfad.name)

    (meldung,) = service.run_queue(config, runde(config), model=Erkenner())

    assert "not transcribed" in meldung
    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    assert aufnahme.status == recordings.FERTIG
    assert aufnahme.text == ""
    assert "Nothing was lost" in aufnahme.detail


def test_eine_kaputte_spur_nimmt_die_uebrigen_nicht_mit(config, sitzung_id, monkeypatch):
    hochladen(config, sitzung_id, name="kaputt.m4a")
    hochladen(config, sitzung_id, name="heil.m4a")
    erkenner = Erkenner()
    echte_spur = service.transcribe_session

    def stolpert(config_, runde_, session_id, audio_path, **kwargs):
        if "kaputt" in audio_path.name:
            raise RuntimeError("Datei lässt sich nicht dekodieren")
        return echte_spur(config_, runde_, session_id, audio_path, **kwargs)

    monkeypatch.setattr(service, "transcribe_session", stolpert)
    service.run_queue(config, runde(config), model=erkenner)

    stände = [zeile.status for zeile in recordings.for_session(runde(config), sitzung_id)]
    assert stände == [recordings.GESCHEITERT, recordings.FERTIG]


def test_im_log_des_stapellaufs_steht_kein_name_und_kein_dateiname(config, sitzung_id, caplog):
    """Der Stamm eines Spurnamens ist der Anzeigename des Sprechers (#194, #199).

    Der Stapellauf schreibt bei **jeder** Spur, nicht nur im Fehlerfall: Beginn,
    Fortschritt, Löschung. Gemeldet wird deshalb die Job-Id samt Sitzung — damit findet
    der Betreiber Zeile, Datei und Stand in einem Schritt, und wer das Log liest, erfährt
    trotzdem nicht, wer an dem Abend sprach. Geprüft wird auf DEBUG und über den ganzen
    ``caplog.text``: ein Traceback brächte den Pfad ebenfalls dorthin.
    """
    caplog.set_level(logging.DEBUG)
    aufnahme = hochladen(config, sitzung_id, name="Mira.m4a")

    service.run_queue(
        config,
        runde(config),
        model=Erkenner(
            Segment(start=0.0, end=2.5, text=" Wir brechen bei Sonnenaufgang auf."),
            Segment(start=2.5, end=61.25, text=" Das Schwert bleibt in der Scheide."),
        ),
        delete_audio=True,
    )

    assert f"Job {aufnahme.id} in Sitzung {sitzung_id}" in caplog.text
    assert "transkribiert bis 0:01:01" in caplog.text
    assert "auf Verlangen gelöscht" in caplog.text
    assert "Mira" not in caplog.text
    assert aufnahme.source not in caplog.text
    assert aufnahme.filename not in caplog.text


def test_die_gescheiterte_spur_meldet_dem_log_nur_die_fehlerart(config, sitzung_id, caplog):
    """Der Text eines Datei-Fehlers führt den Pfad mit sich — und damit den Namen (#199).

    Der Runde wird der ganze Fehler trotzdem gesagt: dort geht er sie an, im Log des
    Betreibers nicht.
    """
    caplog.set_level(logging.DEBUG)
    aufnahme = hochladen(config, sitzung_id, name="Mira.m4a")
    pfad = config.recordings_dir / aufnahme.filename
    erkenner = Erkenner(fehler=RuntimeError(f"{pfad} lässt sich nicht dekodieren"))

    (meldung,) = service.run_queue(config, runde(config), model=erkenner)

    assert f"Job {aufnahme.id} in Sitzung {sitzung_id}: RuntimeError" in caplog.text
    assert "Mira" not in caplog.text
    assert str(pfad) not in caplog.text
    assert "Mira" in meldung


def test_die_spur_bleibt_liegen_und_geht_nur_auf_verlangen(config, sitzung_id):
    hochladen(config, sitzung_id)

    service.run_queue(config, runde(config), model=Erkenner())
    assert len(list(config.recordings_dir.iterdir())) == 1

    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    recordings.mark(runde(config), aufnahme.id, recordings.WARTET)
    service.run_queue(config, runde(config), model=Erkenner(), delete_audio=True)
    assert list(config.recordings_dir.iterdir()) == []


def test_ein_abgeschalteter_erkenner_laesst_die_spur_wartend(config, sitzung_id):
    """Ohne Rueckfall (#216) darf »nicht drangekommen« nicht wie »verloren« aussehen.

    Die Spur behaelt ``wartet`` und bekommt eine Meldung, die das sagt — nur so holt die
    naechste Nacht sie nach, statt eine Chronik ohne das gesprochene Wort zu schreiben.
    """
    hochladen(config, sitzung_id, name="Sprachmemo 3.m4a")
    hochladen(config, sitzung_id, name="Sprachmemo 4.m4a")
    aus = Erkenner(fehler=TranscriberUnreachable("Der Spracherkenner ist nicht erreichbar"))

    meldungen = service.run_queue(config, runde(config), model=aus)

    wartend = recordings.pending(runde(config))
    assert [eine.status for eine in wartend] == [recordings.WARTET, recordings.WARTET]
    assert "nicht erreichbar" in " ".join(meldungen)
    # Die zweite Spur wird gar nicht erst versucht — sie liefe in denselben Fehler.
    assert wartend[1].detail is None


# --- Der zweite Anlauf --------------------------------------------------------------


def test_eine_gescheiterte_spur_kommt_beim_naechsten_lauf_wieder_dran(config, sitzung_id):
    """#247: ein HTTP 500 hieß »für immer verloren«, und die Frist holte den Ton trotzdem.

    Der Grund war jedes Mal vorübergehend — der Nachbardienst rechnete gerade an einer
    anderen Spur. Am 22.08. standen drei Aufnahmen so da, obwohl der Erkenner längst wieder
    antwortete; gerettet hat sie nur ein Griff von Hand.
    """
    hochladen(config, sitzung_id)
    beschaeftigt = Erkenner(fehler=TranscriberError("abgewiesen: HTTP 500"))

    service.run_queue(config, runde(config), model=beschaeftigt)

    (gescheitert,) = recordings.for_session(runde(config), sitzung_id)
    assert gescheitert.status == recordings.GESCHEITERT
    assert gescheitert.versuche == 1
    assert [spur.id for spur in recordings.pending(runde(config))] == [gescheitert.id]

    service.run_queue(config, runde(config), model=Erkenner())

    (nachher,) = recordings.for_session(runde(config), sitzung_id)
    assert nachher.status == recordings.FERTIG
    assert "die Wirtin hat gelogen" in nachher.text
    assert recordings.pending(runde(config)) == ()


def test_nach_drei_fehlschlaegen_bleibt_die_spur_liegen(config, sitzung_id):
    """Wiederholt wird begrenzt: sonst bliebe die Chronik des Abends bis zur Frist ungeschrieben.

    Liegenbleiben heißt nicht weg — der Ton liegt bis zur Frist da, und wer die Spur doch
    noch will, setzt sie von Hand auf ``wartet``.
    """
    hochladen(config, sitzung_id)
    kaputt = Erkenner(fehler=TranscriberError("abgewiesen: HTTP 500"))

    for _ in range(recordings.MAX_VERSUCHE):
        service.run_queue(config, runde(config), model=kaputt)

    (aufgegeben,) = recordings.for_session(runde(config), sitzung_id)
    assert aufgegeben.versuche == recordings.MAX_VERSUCHE
    assert recordings.pending(runde(config)) == ()
    assert [spur.id for spur in recordings.unverschriftet(runde(config))] == [aufgegeben.id]
    assert len(list(config.recordings_dir.iterdir())) == 1

    recordings.mark(runde(config), aufgegeben.id, recordings.WARTET)

    assert [spur.id for spur in recordings.pending(runde(config))] == [aufgegeben.id]


def test_eine_verschriftete_spur_steht_in_keiner_der_beiden_listen(config, sitzung_id):
    hochladen(config, sitzung_id)

    service.run_queue(config, runde(config), model=Erkenner())

    assert recordings.pending(runde(config)) == ()
    assert recordings.unverschriftet(runde(config)) == ()


# --- Die zugesagte Frist ------------------------------------------------------------


def altern(config, aufnahme_id, tage):
    zeitpunkt = (datetime.now(UTC) - timedelta(days=tage)).isoformat(timespec="seconds")
    verbindung = db.connect(config.database_path)
    try:
        with verbindung:
            verbindung.execute(
                "UPDATE recording SET uploaded_at = ? WHERE id = ?", (zeitpunkt, aufnahme_id)
            )
    finally:
        verbindung.close()


def test_die_frist_ist_sieben_tage():
    """Die Zusage aus der Ansage an Menschen, deren Stimme mitgeschnitten wurde.

    Jeder andere Test setzt die Konstante ein und verschöbe sich mit ihr; hier steht die
    Zahl selbst, damit sie nicht still auf einen beliebigen Wert fallen kann.
    """
    assert recordings.RETENTION_TAGE == 7


def test_was_aelter_ist_als_die_frist_wird_geloescht(config, sitzung_id):
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    (meldung,) = recordings.sweep(config, runde(config))

    assert list(config.recordings_dir.iterdir()) == []
    assert str(recordings.RETENTION_TAGE) in meldung
    nachher = recordings.get(runde(config), aufnahme.id)
    assert nachher is not None
    assert nachher.deleted_at
    assert nachher.filename == aufnahme.filename


def test_was_juenger_ist_bleibt_liegen(config, sitzung_id):
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE - 1)

    assert recordings.sweep(config, runde(config)) == ()
    assert len(list(config.recordings_dir.iterdir())) == 1
    assert recordings.get(runde(config), aufnahme.id).deleted_at is None


def test_der_lauf_darf_beliebig_oft_kommen(config, sitzung_id):
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    zuerst = recordings.sweep(config, runde(config))

    assert len(zuerst) == 1
    assert recordings.sweep(config, runde(config)) == ()
    assert recordings.expired(runde(config)) == ()


def test_eine_abgeraeumte_spur_kommt_nicht_in_die_warteschlange_zurueck(config, sitzung_id):
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)
    recordings.sweep(config, runde(config))

    assert recordings.pending(runde(config)) == ()
    assert service.run_queue(config, runde(config), model=Erkenner()) == ()


def test_der_naechtliche_stapel_setzt_die_frist_auch_ohne_arbeit_durch(config, sitzung_id):
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    service.run_queue(config, runde(config), model=Erkenner())
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    (meldung,) = service.run_queue(config, runde(config))

    assert "deleted" in meldung
    assert list(config.recordings_dir.iterdir()) == []
    # Das Transkript bleibt — gelöscht wird die Audiodatei, nicht die Geschichte.
    assert recordings.get(runde(config), aufnahme.id).text


def test_die_meldung_sagt_warum_die_aufnahme_weg_ist(config, sitzung_id):
    hochladen(config, sitzung_id)
    service.run_queue(config, runde(config), model=Erkenner())
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    (meldung,) = recordings.sweep(config, runde(config))

    assert meldung == recordings.NACH_FRIST.format(
        sitzung=sitzung_id, was="one recording", tage=recordings.RETENTION_TAGE
    )
    assert recordings.get(runde(config), aufnahme.id).deleted_at
    assert recordings.pending(runde(config)) == ()


def test_ein_ganzer_abend_bleibt_auch_bei_240_haeppchen_eine_zeile(config, sitzung_id):
    """Seit dem Häppchen von fünf Minuten (#269) sind vier Stunden mit fünf Sprechern
    rund 240 Dateien statt vierzig. Gezählt wird je Sitzung, die Rechnung trägt das: was
    wächst, ist die Zahl **in** der Zeile und nicht die Zahl der Zeilen.
    """
    for nummer in range(240):
        aufnahme = hochladen(config, sitzung_id, name=f"haeppchen-{nummer}.wav")
        altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    meldungen = recordings.sweep(config, runde(config))

    assert meldungen == (
        recordings.OHNE_TEXT.format(
            sitzung=sitzung_id, was="240 recordings", tage=recordings.RETENTION_TAGE
        ),
    )
    assert list(config.recordings_dir.iterdir()) == []


def test_eine_nie_verschriftete_spur_verschwindet_nicht_stillschweigend(config, sitzung_id):
    """Die Frist gilt auch ihr — aber niemand darf es erst hinterher merken (#181).

    Hier geht eine Stunde gesprochener Abend, von der es keinen Text gibt und nie mehr
    einen geben wird. Die Zeile sagte danach »wartet«, und die Meldung klang wie jede
    andere.
    """
    aufnahme = hochladen(config, sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    (meldung,) = recordings.sweep(config, runde(config))

    assert meldung == recordings.OHNE_TEXT.format(
        sitzung=sitzung_id, was="one recording", tage=recordings.RETENTION_TAGE
    )
    nachher = recordings.get(runde(config), aufnahme.id)
    assert nachher.status == recordings.GESCHEITERT
    assert nachher.detail == recordings.NIE_VERSCHRIFTET
    assert nachher.deleted_at
    assert list(config.recordings_dir.iterdir()) == []


def test_eine_absichtlich_uebersprungene_spur_ist_kein_solcher_verlust(config, sitzung_id):
    """``fertig`` ohne Transkript ist der eine gewollte Fall (``MINDESTDAUER``, #142)."""
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    pfad = config.recordings_dir / f"sitzung{sitzung_id}-teek.wav"
    with wave.open(str(pfad), "wb") as datei:
        datei.setnchannels(2)
        datei.setsampwidth(2)
        datei.setframerate(48000)
        datei.writeframes(bytes(int(48000 * 0.08) * 4))
    aufnahme = recordings.enqueue(runde(config), sitzung_id, pfad.name)
    service.run_queue(config, runde(config), model=Erkenner())
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    (meldung,) = recordings.sweep(config, runde(config))

    assert meldung == recordings.NACH_FRIST.format(
        sitzung=sitzung_id, was="one recording", tage=recordings.RETENTION_TAGE
    )
    assert recordings.get(runde(config), aufnahme.id).status == recordings.FERTIG


# --- Eine Datei ohne Warteschlangenzeile (#289) --------------------------------------


def waise(config, sitzung_id, *, tage_alt, name="Runa"):
    """Das offene Häppchen, das ein Neustart mitten in der Sitzung liegen lässt.

    Datei ja, ``recording``-Zeile nein — genau so sieht es auf der Platte aus, wenn der
    Bot abgeschossen wird, bevor ``_weiterschneiden`` oder ``beenden`` einreihen konnte.
    """
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    pfad = config.recordings_dir / f"sitzung{sitzung_id}-20260806T201500-{name}.wav"
    pfad.write_bytes(DIKTAT)
    alt = (datetime.now(UTC) - timedelta(days=tage_alt)).timestamp()
    os.utime(pfad, (alt, alt))
    return pfad


def test_eine_datei_ohne_zeile_ueberlebt_die_frist_nicht(config, sitzung_id):
    """Die Sieben-Tage-Zusage hängt an der Stimme auf der Platte, nicht an der Zeile.

    Bis #289 las der Aufräumlauf nur aus ``recording``; eine ``.wav``, die nie eingereiht
    wurde, blieb liegen, solange es die Runde gab.
    """
    pfad = waise(config, sitzung_id, tage_alt=recordings.RETENTION_TAGE + 1)

    (meldung,) = recordings.sweep(config, runde(config))

    assert not pfad.exists()
    assert meldung == recordings.OHNE_ZEILE.format(
        sitzung=sitzung_id, was="one recording", tage=recordings.RETENTION_TAGE
    )


def test_der_verlust_ohne_zeile_wird_gemeldet_statt_still_zu_geschehen(config, sitzung_id, caplog):
    """Hier geht Ton, von dem es nie einen Text gab — und niemand hat darauf gewartet.

    Der Dateiname darf dabei nicht in der Logzeile stehen: sein Stamm ist der
    Anzeigename des Sprechers (#194).
    """
    pfad = waise(config, sitzung_id, tage_alt=recordings.RETENTION_TAGE + 1)
    erreicht = []

    with caplog.at_level(logging.WARNING, logger="chronicle.recordings"):
        recordings.sweep_alle(config, melden=lambda _eine, saetze: erreicht.extend(saetze))

    ((satz,), gelogt) = (tuple(erreicht), caplog.text)
    assert "never queued" in satz
    assert satz in gelogt
    assert pfad.stem not in gelogt


def test_zwei_waisen_derselben_sitzung_bleiben_eine_zeile(config, sitzung_id):
    """Dieselbe Rechnung wie bei den eingereihten: gezählt wird je Sitzung."""
    for nummer in range(3):
        waise(config, sitzung_id, tage_alt=recordings.RETENTION_TAGE + 1, name=f"Spur{nummer}")

    meldungen = recordings.sweep(config, runde(config))

    assert meldungen == (
        recordings.OHNE_ZEILE.format(
            sitzung=sitzung_id, was="3 recordings", tage=recordings.RETENTION_TAGE
        ),
    )
    assert list(config.recordings_dir.iterdir()) == []


def test_eine_datei_ohne_zeile_vor_der_frist_bleibt_liegen(config, sitzung_id):
    pfad = waise(config, sitzung_id, tage_alt=recordings.RETENTION_TAGE - 1)

    assert recordings.sweep(config, runde(config)) == ()
    assert pfad.exists()


def test_die_datei_einer_laufenden_sitzung_wird_nicht_fortgenommen(config):
    """Ein Abend, der länger dauert als die Frist, verlöre sonst seinen Ton beim Sprechen.

    In eine laufende Sitzung schreibt der Mitschnitt gerade hinein; ihre Datei hat noch
    keine Zeile, *weil* das Häppchen noch offen ist.
    """
    db.init(config.database_path)
    laufend = notes.create_session(
        runde(config), played_on="2026-08-06", title="Der Keller", laeuft=True
    )
    pfad = waise(config, laufend, tage_alt=recordings.RETENTION_TAGE + 1)

    assert recordings.sweep(config, runde(config)) == ()
    assert pfad.exists()


def test_die_waisen_bleiben_bei_ihrer_eigenen_runde(config, sitzung_id):
    """Der ``glob`` sieht nur einen Namen, keine Runde — die Schranke ist die Sitzungsliste."""
    fremde = runden.anlegen(config.database_path, "Nachbarrunde", guild_id="99")
    pfad = waise(config, sitzung_id, tage_alt=recordings.RETENTION_TAGE + 1)

    assert recordings.sweep(config, fremde) == ()
    assert pfad.exists()


# --- Ein Neustart mitten im Verschriften (#181) -------------------------------------


def test_die_frist_sagt_jeder_runde_ihr_eigenes(config, sitzung_id):
    """#286: ``sweep_alle`` bündelte alle Runden in einen Rückgabewert ohne Adressat.

    Über diesen Weg läuft die Frist im Betrieb — ``recordings.taeglich`` warf ihn weg, und
    damit wurde am siebten Tag die Stimme einer echten Person still gelöscht.
    """
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)
    erreicht = []

    recordings.sweep_alle(config, melden=lambda eine, saetze: erreicht.append((eine.id, saetze)))

    ((wer, saetze),) = erreicht
    assert wer == runde(config).id
    assert saetze == (
        recordings.OHNE_TEXT.format(
            sitzung=sitzung_id, was="one recording", tage=recordings.RETENTION_TAGE
        ),
    )


def test_eine_runde_ohne_abgelaufene_spur_hoert_nichts(config, sitzung_id):
    """Eine Meldung »heute nichts gelöscht« wäre täglicher Lärm im Kanal der Gruppe."""
    hochladen(config, sitzung_id)
    erreicht = []

    recordings.sweep_alle(config, melden=lambda eine, saetze: erreicht.append(eine))

    assert erreicht == []


def test_der_taegliche_lauf_reicht_den_weg_zurueck_durch(config, sitzung_id):
    """Nachweisbar ohne auf den nächsten Tag zu warten — der Schlaf gehört dem Test."""
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)
    erreicht = []

    class Schluss(Exception):
        pass

    async def schlafen(_sekunden):
        raise Schluss

    with pytest.raises(Schluss):
        asyncio.run(
            recordings.taeglich(
                config,
                schlafen=schlafen,
                melden=lambda eine, saetze: erreicht.append(saetze),
            )
        )

    assert erreicht and "never transcribed" in erreicht[0][0]


def laeuft_bei(config, aufnahme_id, besitzer, herzschlag):
    """Die Zeile so, wie ein anderer Prozess sie hinterlassen hat.

    Roh geschrieben und nicht über ``mark``: ``mark`` trüge diesen Prozess als Besitzer
    ein, und dann wäre es keine fremde Zeile mehr.
    """
    verbindung = db.connect(config.database_path)
    try:
        with verbindung:
            verbindung.execute(
                "UPDATE recording SET status = ?, besitzer = ?, herzschlag = ? WHERE id = ?",
                (recordings.LAEUFT, besitzer, herzschlag, aufnahme_id),
            )
    finally:
        verbindung.close()


def gestorben_vor(sekunden):
    return (datetime.now(UTC) - timedelta(seconds=sekunden)).isoformat(timespec="milliseconds")


def herzschlag_von(config, aufnahme_id):
    verbindung = db.connect(config.database_path)
    try:
        zeile = verbindung.execute(
            "SELECT herzschlag FROM recording WHERE id = ?", (aufnahme_id,)
        ).fetchone()
    finally:
        verbindung.close()
    return zeile["herzschlag"]


def test_ein_neustart_mitten_im_verschriften_verliert_die_spur_nicht(config, sitzung_id):
    """Der gemeldete Fehler: ein Deploy mitten im Lauf, und die Spur steht für immer.

    Verschriftet wird eine gute Stunde je Sitzungsstunde — der Prozess, der dabei stirbt,
    ist kein seltener Fall. Danach startet ein **anderer**; für ihn ist die Zeile fremd,
    und ihr Lebenszeichen ist verstummt.
    """
    aufnahme = hochladen(config, sitzung_id)
    laeuft_bei(
        config,
        aufnahme.id,
        "der-prozess-von-vorhin",
        gestorben_vor(recordings.VERSTUMMT.total_seconds() + 1),
    )

    meldungen = service.run_queue(config, runde(config), model=Erkenner())

    assert "2 segments" in meldungen[0]
    nachher = recordings.get(runde(config), aufnahme.id)
    assert nachher.status == recordings.FERTIG
    assert nachher.text == "Auf dem Heimweg fällt mir ein: die Wirtin hat gelogen."
    assert len(list(config.recordings_dir.iterdir())) == 1


def test_die_zurueckgestellte_spur_sagt_an_der_zeile_was_ihr_geschah(config, sitzung_id):
    aufnahme = hochladen(config, sitzung_id)
    laeuft_bei(config, aufnahme.id, "der-prozess-von-vorhin", gestorben_vor(3600))

    assert recordings.zurueckstellen(runde(config)) == (aufnahme.source,)

    nachher = recordings.get(runde(config), aufnahme.id)
    assert nachher.status == recordings.WARTET
    assert nachher.detail == recordings.UNTERBROCHEN
    assert [zeile.id for zeile in recordings.pending(runde(config))] == [aufnahme.id]


def test_im_log_der_zurueckgestellten_spur_steht_kein_name_und_kein_dateiname(
    config, sitzung_id, caplog
):
    """Der Stamm eines Spurnamens ist der Anzeigename des Sprechers (#194).

    Gemeldet wird deshalb die Job-Id samt Sitzung: der Betreiber findet damit Zeile, Datei
    und Stand, und wer das Log liest, erfährt trotzdem nicht, wer an dem Abend sprach.
    Geprüft wird auf DEBUG und über den ganzen ``caplog.text`` — ein Traceback, der den
    Pfad mitbrächte, stünde ebenfalls darin.
    """
    caplog.set_level(logging.DEBUG)
    aufnahme = hochladen(config, sitzung_id, name="Mira.m4a")
    laeuft_bei(config, aufnahme.id, "der-prozess-von-vorhin", gestorben_vor(3600))

    recordings.zurueckstellen(runde(config))

    assert f"Spur {aufnahme.id} aus Sitzung {sitzung_id}" in caplog.text
    assert recordings.UNTERBROCHEN in caplog.text
    assert "Mira" not in caplog.text
    assert aufnahme.source not in caplog.text


def test_die_eigene_verwaiste_spur_kommt_ohne_wartezeit_zurueck(config, sitzung_id):
    """Was uns gehört und nicht in der Merkliste steht, läuft nirgends mehr."""
    aufnahme = hochladen(config, sitzung_id)
    recordings.mark(runde(config), aufnahme.id, recordings.LAEUFT)

    assert recordings.zurueckstellen(runde(config)) == (aufnahme.source,)
    assert recordings.get(runde(config), aufnahme.id).status == recordings.WARTET


def test_eine_spur_die_ein_anderer_prozess_gerade_verschriftet_bleibt_ihm(config, sitzung_id):
    """Die Gegenrichtung: zwei Container liegen auf einer Datei (#178).

    Ein frisches Lebenszeichen heißt, dass dort jemand sitzt — die Spur ein zweites Mal
    anzufassen hieße, dieselbe Stunde ein zweites Mal auf derselben einen CPU zu rechnen.
    """
    aufnahme = hochladen(config, sitzung_id)
    laeuft_bei(config, aufnahme.id, "der-nachbarcontainer", gestorben_vor(1))

    assert recordings.zurueckstellen(runde(config)) == ()
    assert service.run_queue(config, runde(config), model=Erkenner()) == ()
    assert recordings.get(runde(config), aufnahme.id).status == recordings.LAEUFT


def test_die_spur_in_arbeit_gibt_lebenszeichen(config, sitzung_id, monkeypatch):
    """Ohne den Puls hielte der Nachbarprozess eine stundenlange Spur für abgestürzt.

    Takt und Uhr kommen aus dem Test, nicht von der Wanduhr: der Puls schlägt genau dann,
    wenn hier ein Anstoß gegeben wird, und quittiert jeden Schlag, sobald er in der
    Datenbank steht. Die frühere Fassung stellte ``HERZSCHLAG`` klein und wartete in
    Schleifen darauf, dass sich etwas ändert — unter Last wurde sie rot und beim
    Wiederholen grün, und ein Test, dem man nicht glaubt, ist schlimmer als keiner.
    """
    takte = itertools.count()
    monkeypatch.setattr(
        recordings, "_lebenszeichen", lambda: f"2026-08-13T21:00:00.{next(takte):03d}+00:00"
    )
    anstoss: queue.Queue = queue.Queue()
    quittung: queue.Queue = queue.Queue()

    def takt(halt):
        quittung.put(None)
        anstoss.get()
        return halt.is_set()

    monkeypatch.setattr(recordings, "_takt", takt)
    aufnahme = hochladen(config, sitzung_id)
    tor = threading.Event()

    class Langsam(Erkenner):
        def transcribe(self, audio_path, *, hotwords=(), sprache="en"):
            tor.wait(GRENZE)
            yield from ()

    faden = threading.Thread(
        target=lambda: service.run_queue(config, runde(config), model=Langsam())
    )
    faden.start()
    try:
        quittung.get(timeout=GRENZE)
        assert recordings.get(runde(config), aufnahme.id).status == recordings.LAEUFT
        erster = herzschlag_von(config, aufnahme.id)
        assert erster is not None

        anstoss.put(None)
        quittung.get(timeout=GRENZE)

        assert herzschlag_von(config, aufnahme.id) > erster
    finally:
        tor.set()
        faden.join(GRENZE)
        # Der Puls hängt im Anstoß und kommt erst hier frei — deshalb steht fest, dass
        # zwischen ``fertig`` und der letzten Prüfung kein Schlag mehr dazwischenfährt.
        anstoss.put(None)

    assert recordings.get(runde(config), aufnahme.id).status == recordings.FERTIG
    assert herzschlag_von(config, aufnahme.id) is None


def test_ein_nachzuegler_belebt_die_fertige_spur_nicht(config, sitzung_id, monkeypatch):
    """Zwischen dem Ende der Wartezeit und dem Schreiben kann die Spur längst fertig sein."""
    aufnahme = hochladen(config, sitzung_id)
    recordings.mark(runde(config), aufnahme.id, recordings.FERTIG)
    schlaege = iter([False, True])
    monkeypatch.setattr(recordings, "_takt", lambda halt: next(schlaege))

    recordings._pulsen(runde(config), aufnahme.id, threading.Event())

    assert herzschlag_von(config, aufnahme.id) is None


def test_eine_alte_datenbank_bekommt_die_spalte_nachgetragen(tmp_path):
    # ``CREATE TABLE IF NOT EXISTS`` erreicht eine bestehende Tabelle nicht — hier steht
    # deshalb die Fassung vor Schema 10, samt einer Zeile, die den Umbau überleben muss.
    pfad = tmp_path / "alt.sqlite3"
    verbindung = db.connect(pfad)
    try:
        with verbindung:
            verbindung.execute(
                "CREATE TABLE recording (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, "
                "filename TEXT NOT NULL UNIQUE, source TEXT NOT NULL, uploaded_at TEXT NOT NULL, "
                "status TEXT NOT NULL, detail TEXT, updated_at TEXT NOT NULL)"
            )
            verbindung.execute(
                "INSERT INTO recording (session_id, filename, source, uploaded_at, status, "
                "updated_at) VALUES (1, 'memo.m4a', 'memo', '2026-01-01T00:00:00+00:00', "
                "'fertig', '2026-01-01T00:00:00+00:00')"
            )
    finally:
        verbindung.close()

    db.init(pfad)

    verbindung = db.connect(pfad)
    try:
        zeile = verbindung.execute(
            "SELECT filename, deleted_at, started_at FROM recording"
        ).fetchone()
    finally:
        verbindung.close()
    assert (zeile["filename"], zeile["deleted_at"], zeile["started_at"]) == ("memo.m4a", None, None)
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


# --- Vom Transkript zur Notiz -------------------------------------------------------


def test_das_transkript_laesst_sich_einer_szene_als_notiz_zuordnen(config, sitzung_id):
    hochladen(config, sitzung_id)
    service.run_queue(config, runde(config), model=Erkenner())
    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    szene = notes.session(runde(config), sitzung_id).scenes[0]

    assert notes.session_of_scene(runde(config), szene.id) == aufnahme.session_id
    assert notes.add_note(runde(config), szene.id, aufnahme.text) is not None

    notiz = notes.session(runde(config), sitzung_id).scenes[0].notes[0]
    assert notiz.text == aufnahme.text


def test_ein_transkript_geht_nicht_in_eine_fremde_szene(config, sitzung_id):
    hochladen(config, sitzung_id)
    service.run_queue(config, runde(config), model=Erkenner())
    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    fremde = notes.session(
        runde(config),
        notes.create_session(runde(config), played_on="2026-08-13"),
    ).scenes[0]

    assert notes.session_of_scene(runde(config), 0) is None
    assert notes.session_of_scene(runde(config), fremde.id) != aufnahme.session_id
    assert notes.add_note(runde(config), 0, aufnahme.text) is None


def test_ohne_transkript_gibt_es_nichts_zu_uebernehmen(config, sitzung_id):
    hochladen(config, sitzung_id)
    aufnahme = recordings.pending(runde(config))[0]
    szene = notes.session(runde(config), sitzung_id).scenes[0]

    assert aufnahme.text == ""
    assert notes.add_note(runde(config), szene.id, aufnahme.text) is None
    assert notes.session(runde(config), sitzung_id).scenes[0].notes == ()
    assert recordings.get(runde(config), 999) is None
