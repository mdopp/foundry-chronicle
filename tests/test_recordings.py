"""Diktat statt Tippen: annehmen, warten, übernehmen.

Geprüft wird gegen die Module, nicht gegen eine Seite: Spielinhalte hat die Weboberfläche
seit #157 keine mehr. Kein Test lädt ein Spracherkennungsmodell; an dessen Stelle steht
ein erfundenes.
"""

from __future__ import annotations

import io
import sqlite3
import wave
from datetime import UTC, datetime, timedelta

import pytest
from conftest import runde
from werkzeug.datastructures import FileStorage

from chronicle import db, notes, recordings
from chronicle.config import Config
from chronicle.transcribe import service
from chronicle.transcribe.client import Segment, TranscriberNotInstalled

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

    def transcribe(self, audio_path, *, vocabulary=""):
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


def diktat(name="Sprachmemo 3.m4a", inhalt=DIKTAT):
    return FileStorage(stream=io.BytesIO(inhalt), filename=name)


def hochladen(config, sitzung_id, **kwargs):
    return recordings.accept(config, runde(config), sitzung_id, diktat(**kwargs))


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


def test_ohne_datei_wird_verstaendlich_abgewiesen(config, sitzung_id):
    with pytest.raises(recordings.Rejected) as fehler:
        recordings.accept(config, runde(config), sitzung_id, None)

    assert "Keine Datei ausgewählt" in str(fehler.value)
    assert recordings.for_session(runde(config), sitzung_id) == ()


def test_was_kein_audio_ist_wird_verstaendlich_abgewiesen(config, sitzung_id):
    with pytest.raises(recordings.Rejected) as fehler:
        hochladen(config, sitzung_id, name="notizen.txt")

    meldung = str(fehler.value)
    assert "keine Audiodatei" in meldung
    assert ".m4a" in meldung
    assert not config.recordings_dir.exists() or list(config.recordings_dir.iterdir()) == []


def test_eine_leere_datei_hinterlaesst_nichts(config, sitzung_id):
    with pytest.raises(recordings.Rejected) as fehler:
        hochladen(config, sitzung_id, inhalt=b"")

    assert "leer" in str(fehler.value)
    assert list(config.recordings_dir.iterdir()) == []
    assert recordings.for_session(runde(config), sitzung_id) == ()


def test_die_grenze_ist_grosszuegig_genug_fuer_einen_abend():
    assert recordings.MAX_BYTES >= 256 * 1024 * 1024


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

    recordings.mark(runde(config), aufnahme.id, recordings.GESCHEITERT, "faster-whisper fehlt.")

    nachher = recordings.get(runde(config), aufnahme.id)
    assert nachher.status == recordings.GESCHEITERT
    assert nachher.detail == "faster-whisper fehlt."


# --- Derselbe Stapel, dieselbe Warteschlange ----------------------------------------


def test_der_stapel_macht_aus_dem_diktat_ein_transkript(config, sitzung_id):
    hochladen(config, sitzung_id)

    meldungen = service.run_queue(config, runde(config), model=Erkenner())

    assert "2 Segmente" in meldungen[0]
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
    assert "liegt nicht mehr da" in aufnahme.detail


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

    assert "nicht verschriftet" in meldung
    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    assert aufnahme.status == recordings.FERTIG
    assert aufnahme.text == ""
    assert "Verlorengegangen ist nichts" in aufnahme.detail


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


def test_die_spur_bleibt_liegen_und_geht_nur_auf_verlangen(config, sitzung_id):
    hochladen(config, sitzung_id)

    service.run_queue(config, runde(config), model=Erkenner())
    assert len(list(config.recordings_dir.iterdir())) == 1

    aufnahme = recordings.for_session(runde(config), sitzung_id)[0]
    recordings.mark(runde(config), aufnahme.id, recordings.WARTET)
    service.run_queue(config, runde(config), model=Erkenner(), delete_audio=True)
    assert list(config.recordings_dir.iterdir()) == []


def test_ein_fehlendes_faster_whisper_laesst_die_warteschlange_stehen(
    config, sitzung_id, monkeypatch
):
    hochladen(config, sitzung_id)

    def ohne_modell(_config):
        raise TranscriberNotInstalled("faster-whisper ist nicht installiert")

    monkeypatch.setattr(service, "model_from_config", ohne_modell)
    with pytest.raises(TranscriberNotInstalled):
        service.run_queue(config, runde(config))

    assert recordings.pending(runde(config))[0].status == recordings.WARTET


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

    assert "gelöscht" in meldung
    assert list(config.recordings_dir.iterdir()) == []
    # Das Transkript bleibt — gelöscht wird die Audiodatei, nicht die Geschichte.
    assert recordings.get(runde(config), aufnahme.id).text


def test_die_meldung_sagt_warum_die_aufnahme_weg_ist(config, sitzung_id):
    hochladen(config, sitzung_id)
    (aufnahme,) = recordings.for_session(runde(config), sitzung_id)
    altern(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    (meldung,) = recordings.sweep(config, runde(config))

    assert meldung == recordings.NACH_FRIST.format(
        source=aufnahme.source, tage=recordings.RETENTION_TAGE
    )
    assert recordings.get(runde(config), aufnahme.id).deleted_at
    assert recordings.pending(runde(config)) == ()


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
