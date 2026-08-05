"""Von der Audiospur bis zu den Segmenten in der Datenbank.

Kein Test lädt ein Modell herunter: der echte Spracherkenner steckt hinter
``client.SpeechModel``, und hier steht ein erfundenes Modell an seiner Stelle.
"""

import sys

import pytest
from conftest import UNSER_KONTO

import chronicle.transcribe.__main__ as entry
from chronicle import db, search
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.foundry.world import project
from chronicle.transcribe import service, vocabulary
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
def connection(config):
    db.init(config.database_path)
    verbindung = db.connect(config.database_path)
    yield verbindung
    verbindung.close()


def sitzung(connection, *, title="Der Keller"):
    zeiger = connection.execute(
        "INSERT INTO session (played_on, title, created_at) VALUES ('2026-08-05', ?, ?)",
        (title, STAND),
    )
    connection.commit()
    return int(zeiger.lastrowid)


def segmente(connection, session_id):
    return connection.execute(
        "SELECT s.start_ms, s.end_ms, s.text FROM transcript_segment s "
        "JOIN transcript t ON t.id = s.transcript_id "
        "WHERE t.session_id = ? ORDER BY s.start_ms",
        (session_id,),
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


def test_die_namen_der_sitzung_stehen_vor_dem_zwischenspeicher(connection, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(connection)
    zeiger = connection.execute(
        "INSERT INTO scene (session_id, position, created_at) VALUES (?, 1, ?)",
        (sitzung_id, STAND),
    )
    connection.execute(
        "INSERT INTO scene_foundry_message (scene_id, message_id) VALUES (?, 'm-wurf')",
        (int(zeiger.lastrowid),),
    )
    connection.commit()

    namen = service.names(connection, sitzung_id)

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


def test_eine_spur_wird_zu_segmenten_mit_zeitstempeln(config, connection, spur):
    sitzung_id = sitzung(connection)

    ergebnis = service.transcribe_session(config, sitzung_id, spur, model=Erkenner())

    assert ergebnis.source == "mira"
    assert ergebnis.segment_count == 2
    zeilen = segmente(connection, sitzung_id)
    assert [(z["start_ms"], z["end_ms"]) for z in zeilen] == [(0, 2500), (2500, 61250)]
    assert zeilen[0]["text"] == "Wir brechen bei Sonnenaufgang auf."


def test_das_vokabular_der_sitzung_geht_in_den_erkenner(config, connection, spur, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(connection)
    erkenner = Erkenner()

    ergebnis = service.transcribe_session(config, sitzung_id, spur, model=erkenner)

    assert "Aelin Sturmwind" in erkenner.vokabular
    erwartet = vocabulary.capped(service.names(connection, sitzung_id))
    assert ergebnis.vocabulary_names == len(erwartet)


def test_ein_zweiter_lauf_ersetzt_die_spur(config, connection, spur):
    sitzung_id = sitzung(connection)
    service.transcribe_session(config, sitzung_id, spur, model=Erkenner())

    service.transcribe_session(
        config,
        sitzung_id,
        spur,
        model=Erkenner(Segment(start=0.0, end=1.0, text="Noch einmal von vorn.")),
    )

    zeilen = segmente(connection, sitzung_id)
    assert [z["text"] for z in zeilen] == ["Noch einmal von vorn."]
    assert connection.execute("SELECT COUNT(*) FROM transcript").fetchone()[0] == 1


def test_zwei_spuren_einer_sitzung_stehen_nebeneinander(config, connection, spur):
    sitzung_id = sitzung(connection)
    zweite = spur.with_name("brok.ogg")
    zweite.write_bytes(b"auch kein echtes Audio")

    service.transcribe_session(config, sitzung_id, spur, model=Erkenner())
    service.transcribe_session(config, sitzung_id, zweite, model=Erkenner())

    quellen = connection.execute("SELECT source FROM transcript ORDER BY source").fetchall()
    assert [z["source"] for z in quellen] == ["brok", "mira"]


def test_eine_unbekannte_sitzung_bekommt_kein_transkript(config, connection, spur):
    assert service.transcribe_session(config, 999, spur, model=Erkenner()) is None


def test_die_aufnahme_bleibt_liegen(config, connection, spur):
    service.transcribe_session(config, sitzung(connection), spur, model=Erkenner())
    assert spur.exists()


def test_geloescht_wird_nur_auf_verlangen(config, connection, spur):
    service.transcribe_session(
        config, sitzung(connection), spur, model=Erkenner(), delete_audio=True
    )
    assert not spur.exists()


def test_der_fortschritt_meldet_die_stelle_im_band_ohne_restzeit(config, connection, spur, caplog):
    with caplog.at_level("INFO"):
        service.transcribe_session(config, sitzung(connection), spur, model=Erkenner())
    assert "transkribiert bis 0:01:01" in caplog.text


def test_die_segmente_sind_ueber_die_suche_zu_finden(config, connection, spur):
    sitzung_id = sitzung(connection)
    service.transcribe_session(config, sitzung_id, spur, model=Erkenner())

    ergebnis = search.find(config.database_path, "Schwert")

    assert [gruppe.kind for gruppe in ergebnis.groups] == [service.KIND]
    treffer = ergebnis.groups[0].hits[0]
    assert treffer.session_id == sitzung_id
    assert "<mark>Schwert</mark>" in str(treffer.snippet)


def test_ein_zweiter_lauf_laesst_nichts_verworfenes_im_index(config, connection, spur):
    sitzung_id = sitzung(connection)
    service.transcribe_session(config, sitzung_id, spur, model=Erkenner())

    service.transcribe_session(
        config,
        sitzung_id,
        spur,
        model=Erkenner(Segment(start=0.0, end=1.0, text="Eine Kerze statt eines Schwertes.")),
    )

    assert len(search.find(config.database_path, "Schwert").groups[0].hits) == 1


def test_eine_datenbank_ohne_index_traegt_die_transkripte_beim_start_nach(config, connection, spur):
    service.transcribe_session(config, sitzung(connection), spur, model=Erkenner())
    with connection:
        connection.execute("DELETE FROM search_index")

    db.init(config.database_path)

    assert search.find(config.database_path, "Schwert").groups


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
    assert Config.from_env({}).whisper_model == "small"


# --- Der echte Erkenner, ohne echtes Modell -----------------------------------------


def test_der_erkenner_laeuft_auf_der_cpu_in_int8():
    geladen = {}

    def loader(model_size, *, device, compute_type):
        geladen.update(size=model_size, device=device, compute_type=compute_type)
        return Spur()

    erkenner = FasterWhisper("small", loader=loader)

    assert geladen == {"size": "small", "device": DEVICE, "compute_type": COMPUTE_TYPE}
    assert erkenner.name == "small"


def test_der_erkenner_bekommt_die_ganze_spur_und_den_vorspann(tmp_path):
    modell = Spur()
    erkenner = FasterWhisper("small", loader=lambda *a, **k: modell)

    teile = list(erkenner.transcribe(tmp_path / "mira.ogg", vocabulary="Namen: Aelin."))

    assert modell.aufruf == {
        "pfad": str(tmp_path / "mira.ogg"),
        "initial_prompt": "Namen: Aelin.",
    }
    assert teile == [Segment(start=1.0, end=2.0, text="Aus dem Modell.")]


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


def test_der_stapelaufruf_transkribiert_eine_spur(config, connection, spur, monkeypatch, capsys):
    sitzung_id = sitzung(connection)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "model_from_config", lambda _config: Erkenner())

    assert entry.main([str(sitzung_id), "mira.ogg"]) == 0

    assert "2 Segmente" in capsys.readouterr().out
    assert len(segmente(connection, sitzung_id)) == 2


def test_der_stapelaufruf_loescht_die_spur_nur_mit_schalter(
    config, connection, spur, monkeypatch, capsys
):
    sitzung_id = sitzung(connection)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "model_from_config", lambda _config: Erkenner())

    entry.main([str(sitzung_id), "mira.ogg"])
    assert spur.exists()

    entry.main([str(sitzung_id), "mira.ogg", "--loeschen"])
    assert not spur.exists()


def test_der_stapelaufruf_weist_falsche_argumente_ab(config, connection, spur, monkeypatch, capsys):
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))

    assert entry.main([]) == 2
    assert entry.main(["1"]) == 2
    assert entry.main(["keine-zahl", "mira.ogg"]) == 2
    assert entry.main(["1", "fehlt.ogg"]) == 2
    assert "gibt es nicht" in capsys.readouterr().out


def test_der_stapelaufruf_meldet_eine_unbekannte_sitzung(config, spur, monkeypatch, capsys):
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "model_from_config", lambda _config: Erkenner())

    assert entry.main(["999", "mira.ogg"]) == 2
    assert "Sitzung 999 gibt es nicht" in capsys.readouterr().out


def test_der_stapelaufruf_meldet_ein_fehlendes_faster_whisper(
    config, connection, spur, monkeypatch, capsys
):
    sitzung_id = sitzung(connection)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    assert entry.main([str(sitzung_id), "mira.ogg"]) == 2
    assert "faster-whisper" in capsys.readouterr().out
