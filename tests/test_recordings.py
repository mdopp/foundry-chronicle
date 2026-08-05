"""Diktat statt Tippen: hochladen, warten, übernehmen.

Aufgenommen wird nicht im Browser — die Prüfung darauf steht weiter unten und ist
Absicht, kein Vergessen: Mikrofonzugriff braucht HTTPS und wäre im Heimnetz über HTTP
tot. Kein Test lädt ein Spracherkennungsmodell; an dessen Stelle steht ein erfundenes.
"""

from __future__ import annotations

import io

import pytest

from chronicle import db, notes, recordings
from chronicle.app import create_app
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
    return notes.create_session(config.database_path, played_on="2026-08-06", title="Der Keller")


@pytest.fixture
def client(config):
    return create_app(config).test_client()


def datei(name="Sprachmemo 3.m4a", inhalt=DIKTAT):
    return {"datei": (io.BytesIO(inhalt), name)}


def hochladen(client, sitzung_id, **kwargs):
    return client.post(
        f"/sitzungen/{sitzung_id}/diktat",
        data=datei(**kwargs),
        content_type="multipart/form-data",
    )


def seite(client, sitzung_id):
    return client.get(f"/sitzungen/{sitzung_id}").get_data(as_text=True)


# --- Hochladen ----------------------------------------------------------------------


def test_ein_upload_landet_auf_der_platte_und_in_der_warteschlange(client, config, sitzung_id):
    antwort = hochladen(client, sitzung_id)

    assert antwort.status_code == 302
    spuren = list(config.recordings_dir.iterdir())
    assert [pfad.read_bytes() for pfad in spuren] == [DIKTAT]
    warteschlange = recordings.pending(config.database_path)
    assert [(zeile.session_id, zeile.filename) for zeile in warteschlange] == [
        (sitzung_id, spuren[0].name)
    ]


def test_die_spur_liegt_nicht_im_gesicherten_datenverzeichnis(client, config, sitzung_id):
    hochladen(client, sitzung_id)
    spur = next(config.recordings_dir.iterdir())
    assert not spur.resolve().is_relative_to(config.data_dir.resolve())


def test_mehrere_diktate_je_sitzung_stehen_nebeneinander(client, config, sitzung_id):
    hochladen(client, sitzung_id)
    hochladen(client, sitzung_id)

    assert len(list(config.recordings_dir.iterdir())) == 2
    assert len(recordings.for_session(config.database_path, sitzung_id)) == 2


def test_ohne_datei_wird_verstaendlich_abgewiesen(client, config, sitzung_id):
    antwort = client.post(f"/sitzungen/{sitzung_id}/diktat", data={})

    assert antwort.status_code == 400
    assert "Keine Datei ausgewählt" in antwort.get_data(as_text=True)
    assert recordings.for_session(config.database_path, sitzung_id) == ()


def test_was_kein_audio_ist_wird_verstaendlich_abgewiesen(client, config, sitzung_id):
    antwort = hochladen(client, sitzung_id, name="notizen.txt")

    assert antwort.status_code == 400
    html = antwort.get_data(as_text=True)
    assert "keine Audiodatei" in html
    assert ".m4a" in html
    assert not config.recordings_dir.exists() or list(config.recordings_dir.iterdir()) == []


def test_eine_leere_datei_hinterlaesst_nichts(client, config, sitzung_id):
    antwort = hochladen(client, sitzung_id, inhalt=b"")

    assert antwort.status_code == 400
    assert "leer" in antwort.get_data(as_text=True)
    assert list(config.recordings_dir.iterdir()) == []
    assert recordings.for_session(config.database_path, sitzung_id) == ()


def test_eine_zu_grosse_spur_wird_abgewiesen_statt_den_speicher_zu_fuellen(config, sitzung_id):
    app = create_app(config)
    app.config["MAX_CONTENT_LENGTH"] = 16

    antwort = app.test_client().post(
        f"/sitzungen/{sitzung_id}/diktat",
        data=datei(inhalt=b"x" * 4096),
        content_type="multipart/form-data",
    )

    assert antwort.status_code == 413
    assert "zu groß" in antwort.get_data(as_text=True)


def test_die_grenze_ist_grosszuegig_genug_fuer_einen_abend():
    assert recordings.MAX_BYTES >= 256 * 1024 * 1024


def test_ein_diktat_zu_einer_unbekannten_sitzung_gibt_es_nicht(client):
    assert hochladen(client, 999).status_code == 404


def test_zwei_gleich_benannte_spuren_ueberschreiben_einander_nicht(config, sitzung_id):
    erste = recordings.target_path(config.recordings_dir, sitzung_id, "memo.m4a")
    config.recordings_dir.mkdir(parents=True)
    erste.write_bytes(DIKTAT)

    zweite = recordings.target_path(config.recordings_dir, sitzung_id, "memo.m4a")

    assert zweite != erste


# --- Die Quittung: ehrlich, ohne geratenen Fortschritt -------------------------------


def test_die_seite_sagt_dass_es_im_naechsten_stapel_laeuft(client, sitzung_id):
    hochladen(client, sitzung_id)
    html = seite(client, sitzung_id)
    assert "läuft im nächsten Stapel" in html
    assert "python -m chronicle.transcribe" in html
    assert "%" not in html.split('id="diktat"')[1]


def test_die_seite_zeigt_den_stand_des_jobs_und_nicht_nur_dass_etwas_da_ist(
    client, config, sitzung_id
):
    hochladen(client, sitzung_id)
    aufnahme = recordings.pending(config.database_path)[0]
    recordings.mark(config.database_path, aufnahme.id, recordings.LAEUFT)

    assert "Läuft gerade" in seite(client, sitzung_id)


def test_ein_gescheiterter_lauf_steht_mit_grund_auf_der_seite(client, config, sitzung_id):
    hochladen(client, sitzung_id)
    aufnahme = recordings.pending(config.database_path)[0]
    recordings.mark(
        config.database_path, aufnahme.id, recordings.GESCHEITERT, "faster-whisper fehlt."
    )

    assert "faster-whisper fehlt." in seite(client, sitzung_id)


def test_kein_mikrofon_kein_aufnahmeknopf_kein_stt(client, sitzung_id):
    hochladen(client, sitzung_id)
    html = seite(client, sitzung_id)

    assert 'type="file"' in html
    assert 'accept="audio/*"' in html
    for verboten in ("getUserMedia", "MediaRecorder", "SpeechRecognition", "capture=", "<script"):
        assert verboten not in html


def test_der_upload_steht_hinter_demselben_tuersteher(config, sitzung_id):
    client = create_app(
        Config(
            data_dir=config.data_dir,
            recordings_dir=config.recordings_dir,
            require_remote_user=True,
        )
    ).test_client()

    assert client.post(f"/sitzungen/{sitzung_id}/diktat", data=datei()).status_code == 403
    assert client.post("/aufnahmen/1/notiz", data={"scene_id": "1"}).status_code == 403
    assert recordings.for_session(config.database_path, sitzung_id) == ()


# --- Derselbe Stapel, dieselbe Warteschlange ----------------------------------------


def test_der_stapel_macht_aus_dem_diktat_ein_transkript(client, config, sitzung_id):
    hochladen(client, sitzung_id)

    meldungen = service.run_queue(config, model=Erkenner())

    assert "2 Segmente" in meldungen[0]
    aufnahme = recordings.for_session(config.database_path, sitzung_id)[0]
    assert aufnahme.status == recordings.FERTIG
    assert aufnahme.text == "Auf dem Heimweg fällt mir ein: die Wirtin hat gelogen."
    assert aufnahme.text in seite(client, sitzung_id)


def test_ein_leerer_lauf_laedt_kein_modell(config, monkeypatch):
    def keins(_config):
        raise AssertionError("ohne Wartende wird kein Modell geladen")

    monkeypatch.setattr(service, "model_from_config", keins)
    assert service.run_queue(config) == ()


def test_eine_verschwundene_spur_wird_gemeldet_statt_still_zu_scheitern(client, config, sitzung_id):
    hochladen(client, sitzung_id)
    next(config.recordings_dir.iterdir()).unlink()

    service.run_queue(config, model=Erkenner())

    aufnahme = recordings.for_session(config.database_path, sitzung_id)[0]
    assert aufnahme.status == recordings.GESCHEITERT
    assert "liegt nicht mehr da" in aufnahme.detail


def test_eine_kaputte_spur_nimmt_die_uebrigen_nicht_mit(client, config, sitzung_id, monkeypatch):
    hochladen(client, sitzung_id, name="kaputt.m4a")
    hochladen(client, sitzung_id, name="heil.m4a")
    erkenner = Erkenner()
    echte_spur = service.transcribe_session

    def stolpert(config_, session_id, audio_path, **kwargs):
        if "kaputt" in audio_path.name:
            raise RuntimeError("Datei lässt sich nicht dekodieren")
        return echte_spur(config_, session_id, audio_path, **kwargs)

    monkeypatch.setattr(service, "transcribe_session", stolpert)
    service.run_queue(config, model=erkenner)

    stände = [zeile.status for zeile in recordings.for_session(config.database_path, sitzung_id)]
    assert stände == [recordings.GESCHEITERT, recordings.FERTIG]


def test_die_spur_bleibt_liegen_und_geht_nur_auf_verlangen(client, config, sitzung_id):
    hochladen(client, sitzung_id)

    service.run_queue(config, model=Erkenner())
    assert len(list(config.recordings_dir.iterdir())) == 1

    aufnahme = recordings.for_session(config.database_path, sitzung_id)[0]
    recordings.mark(config.database_path, aufnahme.id, recordings.WARTET)
    service.run_queue(config, model=Erkenner(), delete_audio=True)
    assert list(config.recordings_dir.iterdir()) == []


def test_ein_fehlendes_faster_whisper_laesst_die_warteschlange_stehen(
    client, config, sitzung_id, monkeypatch
):
    hochladen(client, sitzung_id)

    def ohne_modell(_config):
        raise TranscriberNotInstalled("faster-whisper ist nicht installiert")

    monkeypatch.setattr(service, "model_from_config", ohne_modell)
    with pytest.raises(TranscriberNotInstalled):
        service.run_queue(config)

    assert recordings.pending(config.database_path)[0].status == recordings.WARTET


# --- Vom Transkript zur Notiz -------------------------------------------------------


def test_das_transkript_laesst_sich_einer_szene_als_notiz_zuordnen(client, config, sitzung_id):
    hochladen(client, sitzung_id)
    service.run_queue(config, model=Erkenner())
    aufnahme = recordings.for_session(config.database_path, sitzung_id)[0]
    szene = notes.session(config.database_path, sitzung_id).scenes[0]

    antwort = client.post(
        f"/aufnahmen/{aufnahme.id}/notiz", data={"scene_id": str(szene.id)}, follow_redirects=True
    )

    assert antwort.status_code == 200
    notiz = notes.session(config.database_path, sitzung_id).scenes[0].notes[0]
    assert notiz.text == aufnahme.text


def test_ein_transkript_geht_nicht_in_eine_fremde_szene(client, config, sitzung_id):
    hochladen(client, sitzung_id)
    service.run_queue(config, model=Erkenner())
    aufnahme = recordings.for_session(config.database_path, sitzung_id)[0]
    fremde = notes.session(
        config.database_path,
        notes.create_session(config.database_path, played_on="2026-08-13"),
    ).scenes[0]

    assert client.post(f"/aufnahmen/{aufnahme.id}/notiz", data={"scene_id": "0"}).status_code == 404
    assert (
        client.post(
            f"/aufnahmen/{aufnahme.id}/notiz", data={"scene_id": str(fremde.id)}
        ).status_code
        == 404
    )


def test_ohne_transkript_gibt_es_nichts_zu_uebernehmen(client, config, sitzung_id):
    hochladen(client, sitzung_id)
    aufnahme = recordings.pending(config.database_path)[0]
    szene = notes.session(config.database_path, sitzung_id).scenes[0]

    antwort = client.post(f"/aufnahmen/{aufnahme.id}/notiz", data={"scene_id": str(szene.id)})

    assert antwort.status_code == 404
    assert client.post("/aufnahmen/999/notiz", data={"scene_id": "1"}).status_code == 404
