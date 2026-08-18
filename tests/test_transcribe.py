"""Von der Audiospur bis zu den Segmenten in der Datenbank.

Kein Test lädt ein Modell herunter — es gibt seit #216 keins mehr zu laden. Der echte
Spracherkenner steckt hinter ``client.SpeechModel``; an seiner Stelle steht hier ein
erfundenes Modell, und für den dünnen Client eine Attrappe des Dienstes.
"""

import wave

import pytest
import requests
from conftest import UNSER_KONTO, runde

import chronicle.transcribe.__main__ as entry
from chronicle import db, recordings, search
from chronicle.config import DEFAULT_WHISPER_URL, Config
from chronicle.foundry import store
from chronicle.foundry.world import project
from chronicle.transcribe import client, service, vocabulary
from chronicle.transcribe.client import (
    Segment,
    TranscriberError,
    TranscriberUnreachable,
    WhisperBatch,
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
        self.vokabular = ()

    def transcribe(self, audio_path, *, hotwords=()):
        self.vokabular = tuple(hotwords)
        yield from self.segmente


class Antwort:
    """So weit der Client in eine ``requests``-Antwort hineinsieht."""

    def __init__(self, status_code=200, rumpf=None, kaputt=False):
        self.status_code = status_code
        self._rumpf = rumpf
        self._kaputt = kaputt

    def json(self):
        if self._kaputt:
            raise ValueError("kein JSON")
        return self._rumpf


ANTWORT = {
    "segments": [{"start": 1.0, "end": 2.0, "text": " Aus dem Dienst."}],
    "hotwords_dropped_count": 0,
    "hotwords_dropped": [],
}


class Gegenstelle:
    """``solaris-whisper-batch`` als Attrappe: merkt sich den Aufruf, antwortet nach Vorgabe."""

    def __init__(self, antwort=None, fehler=None):
        self.antwort = Antwort(rumpf=ANTWORT) if antwort is None else antwort
        self.fehler = fehler
        self.aufruf = None

    def post(self, url, *, json, timeout):
        self.aufruf = {"url": url, "json": json, "timeout": timeout}
        if self.fehler is not None:
            raise self.fehler
        return self.antwort


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


def test_die_wortvorgabe_nennt_die_namen_der_sitzung():
    assert vocabulary.capped(("Aelin Sturmwind", "Brok Eisenfaust")) == (
        "Aelin Sturmwind",
        "Brok Eisenfaust",
    )


def test_das_vokabular_wird_bei_224_token_hart_gekappt():
    viele = [f"Namenlose Gestalt Nummer {nummer}" for nummer in range(200)]
    gekappt = vocabulary.capped(viele)

    assert 0 < len(gekappt) < len(viele)
    assert vocabulary.tokens(vocabulary.TRENNER.join(gekappt)) <= vocabulary.MAX_TOKEN


def test_die_rangfolge_entscheidet_was_noch_hineinpasst():
    # Budget für genau zwei kurze Namen.
    budget = 6
    assert vocabulary.capped(("Erster", "Zweiter", "Dritter"), max_tokens=budget) == (
        "Erster",
        "Zweiter",
    )


def test_doppelte_und_leere_namen_kosten_kein_budget():
    assert vocabulary.capped(("Aelin", "  ", "Aelin", "Brok  Eisenfaust")) == (
        "Aelin",
        "Brok Eisenfaust",
    )


def test_ohne_namen_gibt_es_keine_wortvorgabe():
    assert vocabulary.capped(()) == ()


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

    def transcribe(self, audio_path, *, hotwords=()):
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


def test_erkenneradresse_und_ablageort_kommen_aus_der_umgebung():
    gesetzt = Config.from_env(
        {"CHRONICLE_WHISPER_URL": "http://box:9999", "CHRONICLE_RECORDINGS_DIR": "/spuren"}
    )
    assert (gesetzt.whisper_url, str(gesetzt.recordings_dir)) == ("http://box:9999", "/spuren")
    # Leer heißt: der Nachbardienst dieser Box im Host-Netz.
    assert Config.from_env({}).whisper_url is None


# --- Der dünne Client gegen solaris-whisper-batch (#216) ----------------------------


def dienst(config, gegenstelle):
    return WhisperBatch(config, http=lambda: gegenstelle)


def test_die_spur_geht_als_pfad_und_nicht_als_bytes(config, spur):
    gegenstelle = Gegenstelle()

    list(dienst(config, gegenstelle).transcribe(spur))

    assert gegenstelle.aufruf["url"] == DEFAULT_WHISPER_URL + client.TRANSCRIBE_PATH
    # Relativ zum Aufnahmeverzeichnis: der Dienst hängt dasselbe Verzeichnis unter
    # seinem eigenen Pfad ein, gemeinsam ist allein der Name darin.
    assert gegenstelle.aufruf["json"]["path"] == "mira.ogg"


def test_die_namen_gehen_als_hotwords_mit(config, spur):
    gegenstelle = Gegenstelle()

    list(dienst(config, gegenstelle).transcribe(spur, hotwords=("Aelin", "Brok")))

    assert gegenstelle.aufruf["json"]["hotwords"] == ["Aelin", "Brok"]


def test_das_modell_waehlt_der_dienst_und_wir_behaupten_keins(config, spur):
    """Der Endpunkt nimmt ``path``, ``language`` und ``hotwords`` — kein Modellfeld.

    Welches Modell rechnet, steht in der Unit des Dienstes (``large-v3-turbo``,
    ``mdopp/solarisbay#1161``). Eine Bezeichnung, die wir nicht erfragen können, wäre
    geraten; gemeldet wird deshalb der Dienst.
    """
    gegenstelle = Gegenstelle()
    dieser = dienst(config, gegenstelle)

    list(dieser.transcribe(spur))

    assert set(gegenstelle.aufruf["json"]) == {"path", "language", "hotwords"}
    assert dieser.name == client.NAME


def test_die_segmente_kommen_aus_der_antwort(config, spur):
    teile = list(dienst(config, Gegenstelle()).transcribe(spur))
    assert teile == [Segment(start=1.0, end=2.0, text=" Aus dem Dienst.")]


def test_eine_spur_ausserhalb_des_aufnahmeverzeichnisses_wird_abgewiesen(config, tmp_path):
    fremd = tmp_path / "woanders" / "mira.ogg"
    fremd.parent.mkdir(parents=True)
    fremd.write_bytes(b"x")

    with pytest.raises(TranscriberError) as fehler:
        list(dienst(config, Gegenstelle()).transcribe(fremd))

    # Der Pfad trägt den Sprechernamen (#194, #199) und steht deshalb nicht in der Meldung.
    assert "mira" not in str(fehler.value)


def test_ein_abgeschalteter_dienst_ist_kein_fehler_dieser_spur(config, spur):
    """Kein Rückfall mehr (#216) — also muss die Nichterreichbarkeit unterscheidbar sein.

    ``TranscriberUnreachable`` heißt »später nochmal«; daran hängt, dass die Spur wartend
    bleibt statt als gescheitert vermerkt zu werden.
    """
    gegenstelle = Gegenstelle(fehler=requests.ConnectionError("connection refused"))

    with pytest.raises(TranscriberUnreachable) as fehler:
        list(dienst(config, gegenstelle).transcribe(spur))

    assert "bleibt liegen" in str(fehler.value)


def test_ein_noch_ladendes_modell_heisst_warten_und_nicht_scheitern(config, spur):
    gegenstelle = Gegenstelle(antwort=Antwort(status_code=503))

    with pytest.raises(TranscriberUnreachable):
        list(dienst(config, gegenstelle).transcribe(spur))


def test_eine_abgewiesene_spur_ist_ein_fehler_dieser_spur(config, spur):
    gegenstelle = Gegenstelle(antwort=Antwort(status_code=403))

    with pytest.raises(TranscriberError) as fehler:
        list(dienst(config, gegenstelle).transcribe(spur))

    assert not isinstance(fehler.value, TranscriberUnreachable)


def test_eine_antwort_ohne_segmente_wird_nicht_als_leere_spur_verbucht(config, spur):
    gegenstelle = Gegenstelle(antwort=Antwort(rumpf={"fehler": "nichts"}))

    with pytest.raises(TranscriberError):
        list(dienst(config, gegenstelle).transcribe(spur))


def test_eine_antwort_ohne_json_heisst_der_dienst_ist_nicht_der_gemeinte(config, spur):
    gegenstelle = Gegenstelle(antwort=Antwort(kaputt=True))

    with pytest.raises(TranscriberUnreachable):
        list(dienst(config, gegenstelle).transcribe(spur))


def test_gekappte_namen_stehen_nur_als_zahl_im_log(config, spur, caplog):
    gegenstelle = Gegenstelle(
        antwort=Antwort(
            rumpf={
                "segments": [],
                "hotwords_dropped_count": 2,
                "hotwords_dropped": ["Aelin Sturmwind", "Brok Eisenfaust"],
            }
        )
    )

    with caplog.at_level("INFO"):
        list(dienst(config, gegenstelle).transcribe(spur))

    assert "2 Namen" in caplog.text
    assert "Aelin" not in caplog.text


def test_die_adresse_kommt_aus_der_konfiguration(config, spur):
    eigene = Config(
        data_dir=config.data_dir,
        recordings_dir=config.recordings_dir,
        whisper_url="http://box:9999/",
    )
    gegenstelle = Gegenstelle()

    list(WhisperBatch(eigene, http=lambda: gegenstelle).transcribe(spur))

    assert gegenstelle.aufruf["url"] == "http://box:9999" + client.TRANSCRIBE_PATH


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


def test_der_stapelaufruf_meldet_einen_abgeschalteten_erkenner(
    config, scope, spur, monkeypatch, capsys
):
    sitzung_id = sitzung(scope)
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    gegenstelle = Gegenstelle(fehler=requests.ConnectionError("connection refused"))
    monkeypatch.setattr(service, "model_from_config", lambda _c: dienst(config, gegenstelle))

    assert entry.main([str(sitzung_id), "mira.ogg"]) == 2
    assert "nicht erreichbar" in capsys.readouterr().out
