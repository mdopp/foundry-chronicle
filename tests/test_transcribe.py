"""Von der Audiospur bis zu den Segmenten in der Datenbank.

Kein Test lädt ein Modell herunter: der echte Spracherkenner steckt hinter
``client.SpeechModel``, und hier steht ein erfundenes Modell an seiner Stelle.
"""

import sys
import wave

import pytest
from conftest import UNSER_KONTO, runde

import chronicle.transcribe.__main__ as entry
from chronicle import db, recordings, search
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.foundry.world import project
from chronicle.transcribe import client, service, vocabulary
from chronicle.transcribe.client import (
    COMPUTE_TYPE,
    DEVICE,
    FasterWhisper,
    Segment,
    TranscriberNotInstalled,
    _whisper_model,
)

STAND = "2026-08-05T20:00:00+00:00"


class Erkenner:
    """Ein Modell, das nichts lädt und liefert, was der Test verlangt."""

    name = "erfundenes-modell"

    def __init__(self, *segmente):
        self.segmente = segmente or (
            Segment(start=0.0, end=2.5, text=" Wir brechen bei Sonnenaufgang auf."),
            Segment(start=2.5, end=61.25, text=" Aelin Sturmwind zieht das Schwert."),
        )
        self.vokabular = None

    def transcribe(self, audio_path, *, vocabulary=""):
        self.vokabular = vocabulary
        yield from self.segmente


class Spur:
    """Der Platzhalter für ein geladenes faster-whisper-Modell."""

    def __init__(self):
        self.aufruf = {}

    def transcribe(self, pfad, **argumente):
        self.aufruf = {"pfad": pfad, **argumente}
        teile = [Segment(start=1.0, end=2.0, text="Aus dem Modell.")]
        return iter(teile), {"duration": 2.0}


@pytest.fixture
def spur(tmp_path):
    pfad = tmp_path / "aufnahmen" / "mira.ogg"
    pfad.parent.mkdir(parents=True)
    pfad.write_bytes(b"kein echtes Audio")
    return pfad


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")


@pytest.fixture
def scope(config):
    zugang = db.scoped(runde(config))
    yield zugang
    zugang.close()


def sitzung(scope, *, title="Der Keller"):
    zeiger = scope.execute(
        "INSERT INTO session (runde_id, played_on, title, created_at) "
        "VALUES (?, '2026-08-05', ?, ?)",
        (scope.runde_id, title, STAND),
    )
    scope.commit()
    return int(zeiger.lastrowid)


def segmente(scope, session_id):
    return scope.execute(
        "SELECT s.start_ms, s.end_ms, s.text FROM transcript_segment s "
        "JOIN transcript t ON t.id = s.transcript_id "
        "WHERE s.runde_id = ? AND t.session_id = ? ORDER BY s.start_ms",
        (scope.runde_id, session_id),
    ).fetchall()


# --- Vokabular: die harte Kappung ---------------------------------------------------


def test_der_vorspann_nennt_die_namen_der_sitzung():
    gekappt = vocabulary.capped(("Aelin Sturmwind", "Brok Eisenfaust"))
    assert vocabulary.prompt(gekappt) == (
        "In dieser Sitzung kommen vor: Aelin Sturmwind, Brok Eisenfaust."
    )


def test_das_vokabular_wird_bei_224_token_hart_gekappt():
    viele = [f"Namenlose Gestalt Nummer {nummer}" for nummer in range(200)]
    gekappt = vocabulary.capped(viele)

    assert 0 < len(gekappt) < len(viele)
    assert vocabulary.tokens(vocabulary.prompt(gekappt)) <= vocabulary.MAX_TOKEN


def test_die_rangfolge_entscheidet_was_noch_hineinpasst():
    # Budget für die Einleitung und genau zwei kurze Namen.
    budget = vocabulary.tokens(vocabulary.EINLEITUNG) + 6
    assert vocabulary.capped(("Erster", "Zweiter", "Dritter"), max_tokens=budget) == (
        "Erster",
        "Zweiter",
    )


def test_doppelte_und_leere_namen_kosten_kein_budget():
    assert vocabulary.capped(("Aelin", "  ", "Aelin", "Brok  Eisenfaust")) == (
        "Aelin",
        "Brok Eisenfaust",
    )


def test_ohne_namen_gibt_es_keinen_vorspann():
    assert vocabulary.capped(()) == ()
    assert vocabulary.prompt(()) == ""


def test_die_namen_der_sitzung_stehen_vor_dem_zwischenspeicher(scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(scope)
    zeiger = scope.execute(
        "INSERT INTO scene (runde_id, session_id, position, created_at) VALUES (?, ?, 1, ?)",
        (scope.runde_id, sitzung_id, STAND),
    )
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) "
        "VALUES (?, ?, 'm-wurf')",
        (scope.runde_id, int(zeiger.lastrowid)),
    )
    scope.commit()

    namen = service.names(scope, sitzung_id)

    assert namen[0] == "Brok Eisenfaust"
    assert "Aelin Sturmwind" in namen[1:]


# --- Zeitstempel --------------------------------------------------------------------


def test_sekunden_werden_millisekunden():
    zeilen = service.segment_rows([Segment(start=1.2345, end=2.5, text=" Ein Satz. ")])
    assert zeilen == ((1234, 2500, "Ein Satz."),)


def test_ein_leeres_segment_kommt_nicht_in_die_datenbank():
    assert service.segment_rows([Segment(start=0.0, end=1.0, text="   ")]) == ()


def test_ein_ende_vor_dem_anfang_wird_auf_den_anfang_gezogen():
    zeilen = service.segment_rows([Segment(start=5.0, end=4.0, text="Verdreht")])
    assert zeilen == ((5000, 5000, "Verdreht"),)


def test_die_zeitmarke_ist_lesbar():
    assert service.zeitmarke(3723.4) == "1:02:03"


# --- Der Lauf -----------------------------------------------------------------------


def test_eine_spur_wird_zu_segmenten_mit_zeitstempeln(config, scope, spur):
    sitzung_id = sitzung(scope)

    ergebnis = service.transcribe_session(config, runde(config), sitzung_id, spur, model=Erkenner())

    assert ergebnis.source == "mira"
    assert ergebnis.segment_count == 2
    zeilen = segmente(scope, sitzung_id)
    assert [(z["start_ms"], z["end_ms"]) for z in zeilen] == [(0, 2500), (2500, 61250)]
    assert zeilen[0]["text"] == "Wir brechen bei Sonnenaufgang auf."


def test_das_vokabular_der_sitzung_geht_in_den_erkenner(config, scope, spur, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(scope)
    erkenner = Erkenner()

    ergebnis = service.transcribe_session(config, runde(config), sitzung_id, spur, model=erkenner)

    assert "Aelin Sturmwind" in erkenner.vokabular
    erwartet = vocabulary.capped(service.names(scope, sitzung_id))
    assert ergebnis.vocabulary_names == len(erwartet)


def test_ein_zweiter_lauf_ersetzt_die_spur(config, scope, spur):
    sitzung_id = sitzung(scope)
    service.transcribe_session(config, runde(config), sitzung_id, spur, model=Erkenner())

    service.transcribe_session(
        config,
        runde(config),
        sitzung_id,
        spur,
        model=Erkenner(Segment(start=0.0, end=1.0, text="Noch einmal von vorn.")),
    )

    zeilen = segmente(scope, sitzung_id)
    assert [z["text"] for z in zeilen] == ["Noch einmal von vorn."]
    anzahl = scope.execute(
        "SELECT COUNT(*) FROM transcript WHERE runde_id = ?", (scope.runde_id,)
    ).fetchone()[0]
    assert anzahl == 1


def test_zwei_spuren_einer_sitzung_stehen_nebeneinander(config, scope, spur):
    sitzung_id = sitzung(scope)
    zweite = spur.with_name("brok.ogg")
    zweite.write_bytes(b"auch kein echtes Audio")

    service.transcribe_session(config, runde(config), sitzung_id, spur, model=Erkenner())
    service.transcribe_session(config, runde(config), sitzung_id, zweite, model=Erkenner())

    quellen = scope.execute(
        "SELECT source FROM transcript WHERE runde_id = ? ORDER BY source", (scope.runde_id,)
    ).fetchall()
    assert [z["source"] for z in quellen] == ["brok", "mira"]


def test_eine_unbekannte_sitzung_bekommt_kein_transkript(config, scope, spur):
    assert service.transcribe_session(config, runde(config), 999, spur, model=Erkenner()) is None


def test_die_aufnahme_bleibt_liegen(config, scope, spur):
    service.transcribe_session(config, runde(config), sitzung(scope), spur, model=Erkenner())
    assert spur.exists()


def test_geloescht_wird_nur_auf_verlangen(config, scope, spur):
    service.transcribe_session(
        config, runde(config), sitzung(scope), spur, model=Erkenner(), delete_audio=True
    )
    assert not spur.exists()


def test_der_fortschritt_meldet_die_stelle_im_band_ohne_restzeit(config, scope, spur, caplog):
    with caplog.at_level("INFO"):
        service.transcribe_session(config, runde(config), sitzung(scope), spur, model=Erkenner())
    assert "transkribiert bis 0:01:01" in caplog.text


def test_die_segmente_sind_ueber_die_suche_zu_finden(config, scope, spur):
    sitzung_id = sitzung(scope)
    service.transcribe_session(config, runde(config), sitzung_id, spur, model=Erkenner())

    ergebnis = search.find(runde(config), "Schwert")

    assert [gruppe.kind for gruppe in ergebnis.groups] == [service.KIND]
    treffer = ergebnis.groups[0].hits[0]
    assert treffer.session_id == sitzung_id
    assert "<mark>Schwert</mark>" in str(treffer.snippet)


def test_ein_zweiter_lauf_laesst_nichts_verworfenes_im_index(config, scope, spur):
    sitzung_id = sitzung(scope)
    service.transcribe_session(config, runde(config), sitzung_id, spur, model=Erkenner())

    service.transcribe_session(
        config,
        runde(config),
        sitzung_id,
        spur,
        model=Erkenner(Segment(start=0.0, end=1.0, text="Eine Kerze statt eines Schwertes.")),
    )

    assert len(search.find(runde(config), "Schwert").groups[0].hits) == 1


def test_eine_datenbank_ohne_index_traegt_die_transkripte_beim_start_nach(config, scope, spur):
    service.transcribe_session(config, runde(config), sitzung(scope), spur, model=Erkenner())
    with scope:
        scope.execute("DELETE FROM search_index WHERE runde_id = ?", (scope.runde_id,))

    db.init(config.database_path)

    assert search.find(runde(config), "Schwert").groups


# --- Die Schranke gegen erfundene Sätze (#142) --------------------------------------


class NieGefragt:
    """Ein Erkenner, der beweist, dass er gar nicht erst gefragt wurde."""

    name = "nie-gefragt"

    def transcribe(self, audio_path, *, vocabulary=""):
        raise AssertionError("eine Spur ohne Äußerung darf das Modell nicht erreichen")


def wav_spur(pfad, sekunden):
    """Eine WAV-Spur im Format des Aufnahme-Bots: 48 kHz, Stereo, 16 Bit."""
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(pfad), "wb") as datei:
        datei.setnchannels(2)
        datei.setsampwidth(2)
        datei.setframerate(48000)
        datei.writeframes(bytes(int(48000 * sekunden) * 4))
    return pfad


def test_eine_spur_ohne_aeusserung_erreicht_das_modell_nicht(config, scope, tmp_path):
    """Der gemessene Fall: 0,08 s Rauschen, aus denen Whisper einen Satz machte."""
    sitzung_id = sitzung(scope)
    bruchstueck = wav_spur(tmp_path / "aufnahmen" / "teek.wav", 0.08)

    ergebnis = service.transcribe_session(
        config, runde(config), sitzung_id, bruchstueck, model=NieGefragt()
    )

    assert ergebnis.uebersprungen
    assert ergebnis.segment_count == 0
    assert segmente(scope, sitzung_id) == []
    assert bruchstueck.is_file()


def test_eine_kurze_echte_aeusserung_ueberlebt_die_schranke(config, scope, tmp_path):
    """Die Gegenprobe: »Ja.« ist kurz, aber gesprochen — und muss durchkommen."""
    sitzung_id = sitzung(scope)
    ja = wav_spur(tmp_path / "aufnahmen" / "mira.wav", 0.5)

    ergebnis = service.transcribe_session(
        config,
        runde(config),
        sitzung_id,
        ja,
        model=Erkenner(Segment(start=0.0, end=0.5, text=" Ja.")),
    )

    assert not ergebnis.uebersprungen
    assert [zeile["text"] for zeile in segmente(scope, sitzung_id)] == ["Ja."]


def test_die_meldung_zur_uebersprungenen_spur_behauptet_keinen_verlust(config, scope, tmp_path):
    sitzung_id = sitzung(scope)
    bruchstueck = wav_spur(tmp_path / "aufnahmen" / "teek.wav", 0.08)

    meldung = service.transcribe_session(
        config, runde(config), sitzung_id, bruchstueck, model=NieGefragt()
    ).message

    assert "0.08 Sekunden" in meldung
    assert "keine Äußerung" in meldung
    assert "Verlorengegangen ist nichts" in meldung


def test_was_sich_nicht_messen_laesst_wird_nicht_geraten(spur, tmp_path):
    """Ein m4a vom Telefon und ein abgeschnittener Kopf: keine Zahl, also keine Schranke."""
    kaputt = tmp_path / "halb.wav"
    kaputt.write_bytes(b"RIFF")

    assert service.spurdauer(spur) is None
    assert service.spurdauer(kaputt) is None


# --- Ablageort und Umgebung ---------------------------------------------------------


def test_die_aufnahmen_liegen_nicht_im_gesicherten_datenverzeichnis():
    vorgabe = Config.from_env({})
    assert not vorgabe.recordings_dir.resolve().is_relative_to(vorgabe.data_dir.resolve())
    assert vorgabe.database_path.parent.resolve() == vorgabe.data_dir.resolve()


def test_ein_relativer_name_liegt_im_aufnahmeverzeichnis(config):
    assert service.recording_path(config, "mira.ogg") == config.recordings_dir / "mira.ogg"
    anderswo = service.recording_path(config, "/anderswo/mira.ogg")
    assert anderswo.as_posix() == "/anderswo/mira.ogg"


def test_modellgroesse_und_ablageort_kommen_aus_der_umgebung():
    gesetzt = Config.from_env(
        {"CHRONICLE_WHISPER_MODEL": "medium", "CHRONICLE_RECORDINGS_DIR": "/spuren"}
    )
    assert (gesetzt.whisper_model, str(gesetzt.recordings_dir)) == ("medium", "/spuren")
    # Leer heißt automatisch: über das Gerät entschieden, nicht hier festgelegt (#84).
    assert Config.from_env({}).whisper_model is None


def test_das_geraet_kommt_aus_der_umgebung_und_ist_sonst_offen():
    assert Config.from_env({"CHRONICLE_WHISPER_DEVICE": "cpu"}).whisper_device == "cpu"
    assert Config.from_env({}).whisper_device is None


# --- Der echte Erkenner, ohne echtes Modell -----------------------------------------


def test_der_erkenner_laeuft_auf_der_cpu_in_int8():
    geladen = {}

    def loader(model_size, *, device, compute_type):
        geladen.update(size=model_size, device=device, compute_type=compute_type)
        return Spur()

    erkenner = FasterWhisper("small", loader=loader, cuda=False)

    assert geladen == {"size": "small", "device": DEVICE, "compute_type": COMPUTE_TYPE}
    assert erkenner.name == "small"


# --- Karte nutzen, ohne sie zur Pflicht zu machen (#84) ------------------------------


def test_ohne_karte_bleibt_alles_wie_bisher():
    assert client.geraet_und_rechenart(None, cuda=False) == (DEVICE, COMPUTE_TYPE)
    assert client.vorgabemodell(DEVICE) == client.CPU_MODEL


def test_mit_karte_laeuft_das_grosse_modell_in_float16():
    assert client.geraet_und_rechenart(None, cuda=True) == (
        client.CUDA_DEVICE,
        client.CUDA_COMPUTE_TYPE,
    )
    assert client.vorgabemodell(client.CUDA_DEVICE) == client.CUDA_MODEL


def test_der_wunsch_schlaegt_den_fund_in_beide_richtungen():
    # Karte da, aber für Ollama frei gehalten.
    assert client.geraet_und_rechenart("cpu", cuda=True) == (DEVICE, COMPUTE_TYPE)
    # Karte nicht gefunden, trotzdem verlangt — der Fehlschlag fällt später auf CPU.
    assert client.geraet_und_rechenart("cuda", cuda=False) == (
        client.CUDA_DEVICE,
        client.CUDA_COMPUTE_TYPE,
    )


def test_ein_gesetztes_modell_schlaegt_die_vorgabe_des_geraets():
    geladen = {}

    def loader(model_size, *, device, compute_type):
        geladen.update(size=model_size, device=device)
        return Spur()

    FasterWhisper("medium", loader=loader, cuda=True)

    assert geladen == {"size": "medium", "device": client.CUDA_DEVICE}


def test_ohne_gesetztes_modell_entscheidet_das_geraet():
    geladen = {}

    def loader(model_size, *, device, compute_type):
        geladen.update(size=model_size, device=device, compute_type=compute_type)
        return Spur()

    erkenner = FasterWhisper(loader=loader, cuda=True)

    assert geladen == {
        "size": client.CUDA_MODEL,
        "device": client.CUDA_DEVICE,
        "compute_type": client.CUDA_COMPUTE_TYPE,
    }
    assert (erkenner.name, erkenner.device) == (client.CUDA_MODEL, client.CUDA_DEVICE)


def test_eine_belegte_karte_bricht_die_nacht_nicht_ab():
    versuche = []

    def loader(model_size, *, device, compute_type):
        versuche.append((model_size, device))
        if device == client.CUDA_DEVICE:
            raise RuntimeError("CUDA out of memory")
        return Spur()

    erkenner = FasterWhisper(loader=loader, cuda=True)

    # Erst die Karte, dann die CPU — mit dem Modell, das zur CPU passt.
    assert versuche == [
        (client.CUDA_MODEL, client.CUDA_DEVICE),
        (client.CPU_MODEL, DEVICE),
    ]
    assert (erkenner.device, erkenner.compute_type) == (DEVICE, COMPUTE_TYPE)


def test_ein_fehlschlag_auf_der_cpu_wird_nicht_verschluckt():
    def loader(model_size, *, device, compute_type):
        raise RuntimeError("kaputt")

    with pytest.raises(RuntimeError, match="kaputt"):
        FasterWhisper(loader=loader, cuda=False)


def test_ohne_ctranslate2_meldet_die_erkennung_keine_karte(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    assert client.cuda_verfuegbar() is False


def test_der_erkenner_bekommt_die_ganze_spur_und_den_vorspann(tmp_path):
    modell = Spur()
    erkenner = FasterWhisper("small", loader=lambda *a, **k: modell)

    teile = list(erkenner.transcribe(tmp_path / "mira.ogg", vocabulary="Namen: Aelin."))

    assert modell.aufruf == {
        "pfad": str(tmp_path / "mira.ogg"),
        "initial_prompt": "Namen: Aelin.",
        "vad_filter": True,
    }
    assert teile == [Segment(start=1.0, end=2.0, text="Aus dem Modell.")]


def test_die_stille_erreicht_das_modell_nicht(tmp_path):
    """Die Spuren des Bots bestehen ueberwiegend aus Stille — und darauf erfindet Whisper.

    Gemessen an einer Spur aus reiner Stille: ohne Stille-Erkennung fuenf Minuten Nichts,
    acht erfundene Segmente; mit ihr keins (#209).
    """
    modell = Spur()
    erkenner = FasterWhisper("small", loader=lambda *a, **k: modell)

    list(erkenner.transcribe(tmp_path / "mira.wav"))

    assert modell.aufruf["vad_filter"] is True


def test_ohne_vorspann_wird_keiner_gesetzt(tmp_path):
    modell = Spur()
    list(FasterWhisper("small", loader=lambda *a, **k: modell).transcribe(tmp_path / "x.ogg"))
    assert modell.aufruf["initial_prompt"] is None


def test_ein_fehlendes_faster_whisper_wird_verstaendlich_gemeldet(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(TranscriberNotInstalled) as fehler:
        _whisper_model("small", device=DEVICE, compute_type=COMPUTE_TYPE)
    assert "faster-whisper" in str(fehler.value)


# --- Der Stapelaufruf ---------------------------------------------------------------


def test_der_stapelaufruf_transkribiert_eine_spur(config, scope, spur, monkeypatch, capsys):
    sitzung_id = sitzung(scope)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "model_from_config", lambda _config: Erkenner())

    assert entry.main([str(sitzung_id), "mira.ogg"]) == 0

    assert "2 Segmente" in capsys.readouterr().out
    assert len(segmente(scope, sitzung_id)) == 2


def test_der_stapelaufruf_loescht_die_spur_nur_mit_schalter(
    config, scope, spur, monkeypatch, capsys
):
    sitzung_id = sitzung(scope)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "model_from_config", lambda _config: Erkenner())

    entry.main([str(sitzung_id), "mira.ogg"])
    assert spur.exists()

    entry.main([str(sitzung_id), "mira.ogg", "--loeschen"])
    assert not spur.exists()


def test_der_stapelaufruf_weist_falsche_argumente_ab(config, scope, spur, monkeypatch, capsys):
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))

    assert entry.main(["1"]) == 2
    assert entry.main(["keine-zahl", "mira.ogg"]) == 2
    assert entry.main(["1", "fehlt.ogg"]) == 2
    assert "gibt es nicht" in capsys.readouterr().out


def test_ohne_argumente_wird_die_warteschlange_abgearbeitet(
    config, scope, spur, monkeypatch, capsys
):
    sitzung_id = sitzung(scope)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "model_from_config", lambda _config: Erkenner())

    assert entry.main([]) == 0
    assert entry.LEER in capsys.readouterr().out

    recordings.enqueue(runde(config), sitzung_id, spur.name)
    assert entry.main([]) == 0

    assert "2 Segmente" in capsys.readouterr().out
    assert len(segmente(scope, sitzung_id)) == 2


def test_der_stapelaufruf_meldet_eine_unbekannte_sitzung(config, spur, monkeypatch, capsys):
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "model_from_config", lambda _config: Erkenner())

    assert entry.main(["999", "mira.ogg"]) == 2
    assert "Sitzung 999 gibt es nicht" in capsys.readouterr().out


def test_der_stapelaufruf_meldet_ein_fehlendes_faster_whisper(
    config, scope, spur, monkeypatch, capsys
):
    sitzung_id = sitzung(scope)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    assert entry.main([str(sitzung_id), "mira.ogg"]) == 2
    assert "faster-whisper" in capsys.readouterr().out
