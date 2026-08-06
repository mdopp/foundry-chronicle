"""Echtes Audio durch den echten Dekodierpfad — bis vor das Whisper-Seam.

Alle übrigen Transkriptions-Tests setzen ein erfundenes Modell ein und sehen den
Dekoder nie. Hier entstehen echte Dateien in den Formaten, die Telefone ablegen
(m4a/AAC und ogg/opus), und laufen durch ``faster_whisper.audio.decode_audio`` —
denselben Aufruf, den ``WhisperModel.transcribe`` vor dem Modell macht. Damit fällt
eine Dekodier-Regression auf, die dem Fake nie auffiele.

Ein Modell wird dabei nicht geladen und nichts heruntergeladen: das Seam liegt hinter
``decode_audio``, und ``WhisperModel`` wird nirgends gebaut.

Ohne das Extra ``transcribe`` wird übersprungen — **außer in der CI**: dort ist es
installiert, und ein Test, der dort immer übersprungen wird, ist keiner.
"""

from __future__ import annotations

import array
import math
import os

import pytest

from chronicle import db, recordings
from chronicle.config import Config
from chronicle.transcribe import service
from chronicle.transcribe.client import Segment

RATE = 48000

DAUER = 1.5

FREQUENZ = 440.0

# Worauf faster-whisper alles bringt, bevor das Modell etwas sieht.
ZIELRATE = 16000

# Endung, Kodierer, Container, Dekodierer — die Formate der Sprachmemo-Apps. Der Name
# des Dekodierers ist ein anderer als der des Kodierers: ``libopus`` schreibt, ``opus``
# liest.
FORMATE = (
    (".m4a", "aac", None, "aac"),
    (".ogg", "libopus", "ogg", "opus"),
)

FEHLT = "Extra »transcribe« fehlt — nachrüsten mit: pip install '.[dev,transcribe]'"

STAND = "2026-08-06T20:00:00+00:00"


def _vorhanden() -> bool:
    try:
        import av  # noqa: F401
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


# In der CI ist das Extra installiert; fehlt es dort, sollen diese Tests laut scheitern
# statt still zu verschwinden.
pytestmark = pytest.mark.skipif(not _vorhanden() and not os.environ.get("CI"), reason=FEHLT)


def _sinus() -> array.array:
    anzahl = int(RATE * DAUER)
    return array.array(
        "h",
        (int(0.5 * 32767 * math.sin(2 * math.pi * FREQUENZ * i / RATE)) for i in range(anzahl)),
    )


def _schreiben(ziel, codec: str, container: str | None):
    """Eine echte Audiodatei, erzeugt mit demselben PyAV, das sie später liest."""
    import av

    pcm = _sinus()
    with av.open(str(ziel), mode="w", format=container) as behaelter:
        spur = behaelter.add_stream(codec, rate=RATE, layout="mono")
        wandler = av.AudioResampler(format=spur.format, layout=spur.layout, rate=spur.rate)
        rahmen = av.AudioFrame(format="s16", layout="mono", samples=len(pcm))
        rahmen.sample_rate = RATE
        rahmen.pts = 0
        rahmen.planes[0].update(pcm.tobytes())
        for umgewandelt in wandler.resample(rahmen):
            for paket in spur.encode(umgewandelt):
                behaelter.mux(paket)
        for umgewandelt in wandler.resample(None):
            for paket in spur.encode(umgewandelt):
                behaelter.mux(paket)
        for paket in spur.encode(None):
            behaelter.mux(paket)
    return ziel


class NurDekodieren:
    """Ein Modell, das nur dekodiert — echter Fremdcode bis zum Seam, kein Modelllauf."""

    name = "nur-dekodieren"

    def transcribe(self, audio_path, *, vocabulary=""):
        from faster_whisper.audio import decode_audio

        audio = decode_audio(str(audio_path))
        yield Segment(start=0.0, end=len(audio) / ZIELRATE, text="Ton erkannt.")


def test_die_ci_bringt_das_extra_wirklich_mit():
    assert _vorhanden(), FEHLT


@pytest.mark.parametrize(
    ("endung", "codec", "container", "dekoder"),
    FORMATE,
    ids=[endung.lstrip(".") for endung, *_ in FORMATE],
)
def test_eine_echte_spur_geht_durch_den_dekodierpfad(tmp_path, endung, codec, container, dekoder):
    import av
    from faster_whisper.audio import decode_audio

    spur = _schreiben(tmp_path / f"mira{endung}", codec, container)

    with av.open(str(spur)) as geoeffnet:
        assert [s.codec_context.name for s in geoeffnet.streams.audio] == [dekoder]

    audio = decode_audio(str(spur))

    assert audio.dtype.name == "float32"
    assert audio.ndim == 1
    assert abs(len(audio) / ZIELRATE - DAUER) < 0.2
    assert float(abs(audio).max()) > 0.1


def test_eine_echte_spur_laeuft_bis_vor_das_modell_durch_den_stapel(tmp_path):
    config = Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")
    config.recordings_dir.mkdir(parents=True)
    spur = _schreiben(config.recordings_dir / "mira.m4a", "aac", None)
    db.init(config.database_path)
    verbindung = db.connect(config.database_path)
    try:
        with verbindung:
            zeiger = verbindung.execute(
                "INSERT INTO session (played_on, title, created_at) VALUES (?, ?, ?)",
                ("2026-08-06", "Der Keller", STAND),
            )
        sitzung_id = int(zeiger.lastrowid)
    finally:
        verbindung.close()

    ergebnis = service.transcribe_session(config, sitzung_id, spur, model=NurDekodieren())

    assert ergebnis.segment_count == 1
    assert abs(ergebnis.audio_seconds - DAUER) < 0.2


def test_die_erzeugten_formate_werden_auch_angenommen():
    for endung, *_ in FORMATE:
        assert endung in recordings.SUFFIXES
