"""Verschriftet wird während der Sitzung — nicht auf der Schleife, nicht gleichzeitig.

Die drei Randbedingungen aus #269 stehen hier je als Test: der Erkenner verträgt keine
Gleichzeitigkeit (gemessen am 2026-08-22), die Arbeit darf nicht auf der Ereignisschleife
liegen, und ein Fehlschlag darf die Schlange nicht anhalten. Kein Test lädt ein
Spracherkennungsmodell; an dessen Stelle steht ein erfundenes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import GRENZE, runde

from chronicle import db, mitlauf, notes, recordings
from chronicle.bot import recorder
from chronicle.config import Config
from chronicle.transcribe import service
from chronicle.transcribe.client import Segment, TranscriberError

HAEPPCHEN = b"kein echtes Audio, aber Bytes"


class Erkenner:
    """Ein Modell, das nichts lädt und liefert, was der Test verlangt."""

    name = "erfundenes-modell"

    def __init__(self):
        self.gerufen = []

    def transcribe(self, audio_path, *, hotwords=()):
        self.gerufen.append(audio_path.name)
        yield Segment(start=0.0, end=2.5, text=" Da unten steht eine Tür.")


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")


@pytest.fixture
def sitzung_id(config):
    db.init(config.database_path)
    return notes.create_session(runde(config), played_on="2026-08-23", title="Der Keller")


def einreihen(config, sitzung_id, name):
    """Ein Häppchen, so wie der Recorder es mitten in der Sitzung ablegt."""
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings.target_path(config.recordings_dir, sitzung_id, name)
    ziel.write_bytes(HAEPPCHEN)
    return recordings.enqueue(runde(config), sitzung_id, ziel.name)


def mit_erkenner(monkeypatch, erkenner):
    monkeypatch.setattr(service, "model_from_config", lambda _config: erkenner)
    return erkenner


def einmal(config, **kwargs):
    """Genau ein Blick in die Warteschlange, dann ist der Faden durch."""
    laeufe = iter([True])
    return threading.Thread(
        target=mitlauf.betreiben,
        args=(config,),
        kwargs={"schlafen": lambda _: None, "weiter": lambda: next(laeufe, False), **kwargs},
        daemon=True,
    )


# --- Die kürzeren Häppchen ------------------------------------------------------------


def test_ein_haeppchen_ist_fuenf_minuten_lang():
    """Die Zahl selbst, nicht die Konstante: sonst verschöbe sich der Test mit ihr.

    An ihr hängt, wie lange es dauert, bis das erste gesprochene Wort als Text dasteht —
    bei dreißig Minuten eine halbe Stunde, in der nichts geschieht (#269).
    """
    assert recorder.HAEPPCHEN_MINUTEN == 5
    assert recorder.HAEPPCHEN_BYTES == 5 * 60 * recorder.BYTES_JE_SEKUNDE


# --- Während der Sitzung --------------------------------------------------------------


def test_ein_eingereihtes_haeppchen_wird_ohne_abschluss_verschriftet(
    config, sitzung_id, monkeypatch
):
    """Die zweite Hälfte von #217: die Schlange füllte sich, abgearbeitet hat sie niemand."""
    aufnahme = einreihen(config, sitzung_id, "mira-1.wav")
    mit_erkenner(monkeypatch, Erkenner())

    mitlauf.tick(config)

    nachher = recordings.get(runde(config), aufnahme.id)
    assert nachher.status == recordings.FERTIG
    assert "Da unten steht eine Tür." in nachher.text
    assert recordings.pending(runde(config)) == ()


def test_der_mitlauf_sagt_der_runde_nichts(config, sitzung_id, monkeypatch, caplog):
    """240 Zeilen »Spur X: 12 Segmente« während des Spiels sähen aus wie eine Chronik.

    Und im Log des Betreibers hat der Spurname nichts verloren (#194): sein Stamm ist der
    Anzeigename des Sprechers.
    """
    aufnahme = einreihen(config, sitzung_id, "Mira.wav")
    mit_erkenner(monkeypatch, Erkenner())

    with caplog.at_level("INFO", logger="chronicle.mitlauf"):
        mitlauf.tick(config)

    assert caplog.messages == []
    assert "Mira" not in caplog.text
    # Gesagt wird es trotzdem — an der Zeile, wo es die Runde nachschlagen kann.
    assert "Segmente" in recordings.get(runde(config), aufnahme.id).detail


# --- Der Erkenner verträgt keine Gleichzeitigkeit -------------------------------------


def test_nie_zwei_anfragen_zugleich_gegen_den_erkenner(config, sitzung_id, monkeypatch):
    """Gemessen am 2026-08-22: vier Anfragen in sechzehn Sekunden, drei davon HTTP 500.

    Dieselbe Datei allein aufgerufen kam in sechs Sekunden durch. Seit #269 laufen der
    Mitlauf und der Nachtlauf im selben Prozess nebeneinander — also darf immer nur ein
    Durchgang mit dem Dienst reden.

    Der zweite Durchgang meldet sich an, bevor er anfängt; der erste wartet darauf, ehe er
    in den Erkenner geht. Ohne Schloss stünden beide gleichzeitig darin, und der Zähler
    sähe es. Der Test kann daran nur grün werden, wenn niemand überholt — schneller
    laufende Maschinen machen ihn nicht rot.
    """
    for nummer in range(4):
        einreihen(config, sitzung_id, f"haeppchen-{nummer}.wav")

    angemeldet = threading.Event()
    schloss = threading.Lock()
    gleichzeitig = []
    drin = 0

    class Zaehlend(Erkenner):
        def transcribe(self, audio_path, *, hotwords=()):
            nonlocal drin
            angemeldet.wait(GRENZE)
            with schloss:
                drin += 1
                gleichzeitig.append(drin)
            time.sleep(0.02)
            with schloss:
                drin -= 1
            yield from super().transcribe(audio_path, hotwords=hotwords)

    erkenner = mit_erkenner(monkeypatch, Zaehlend())
    mitlaufend = threading.Thread(target=mitlauf.tick, args=(config,), daemon=True)
    mitlaufend.start()

    angemeldet.set()
    service.run_queue(config, runde(config))
    mitlaufend.join(GRENZE)

    assert max(gleichzeitig) == 1
    # Und jede Datei genau einmal: zwei Durchgänge dürfen sich nicht dieselbe Spur nehmen.
    assert sorted(erkenner.gerufen) == sorted({name for name in erkenner.gerufen})
    assert len(erkenner.gerufen) == 4
    assert recordings.pending(runde(config)) == ()


# --- Nicht auf der Ereignisschleife ---------------------------------------------------


def test_die_verschriftung_liegt_nicht_auf_der_ereignisschleife(config, sitzung_id, monkeypatch):
    """Der Bot hält währenddessen das Gateway und schneidet mit.

    Eine Verschriftung dauert Minuten; läge sie auf der Schleife, bliebe der Herzschlag zu
    Discord aus und der Bot fiele mitten in der Sitzung ab. Geprüft wird deshalb nicht die
    Bauart des Fadens, sondern die Wirkung: die Schleife dreht sich weiter, **während** im
    Erkenner noch gerechnet wird.
    """
    einreihen(config, sitzung_id, "mira-1.wav")
    im_erkenner = threading.Event()
    freigabe = threading.Event()

    class Langsam(Erkenner):
        def transcribe(self, audio_path, *, hotwords=()):
            im_erkenner.set()
            freigabe.wait(GRENZE)
            yield from super().transcribe(audio_path, hotwords=hotwords)

    mit_erkenner(monkeypatch, Langsam())
    faden = einmal(config)

    async def bedienbar():
        faden.start()
        await asyncio.get_running_loop().run_in_executor(None, im_erkenner.wait, GRENZE)
        takte = 0
        while im_erkenner.is_set() and not freigabe.is_set() and takte < 3:
            await asyncio.sleep(0)
            takte += 1
        freigabe.set()
        return takte

    assert asyncio.run(bedienbar()) == 3
    assert im_erkenner.is_set()
    faden.join(GRENZE)
    assert recordings.pending(runde(config)) == ()


def test_der_faden_laeuft_neben_dem_bot(config, monkeypatch):
    monkeypatch.setattr(mitlauf, "betreiben", lambda config, **kwargs: None)
    faden = mitlauf.starten(config)
    faden.join(GRENZE)
    assert faden.daemon


def test_der_faden_dreht_sich_weiter_auch_wenn_ein_blick_scheitert(config, monkeypatch):
    """Sonst liefe der Dienst weiter und verschriftete nie wieder während einer Sitzung."""
    blicke = []

    def platzt(_config):
        blicke.append(len(blicke))
        raise ValueError("Datenbank kurz weg")

    monkeypatch.setattr(mitlauf, "tick", platzt)
    mitlauf.betreiben(config, schlafen=lambda _: None, weiter=lambda: len(blicke) < 3)

    assert len(blicke) == 3


def test_der_faden_sagt_ohne_arbeit_dass_er_lebt(config, monkeypatch, caplog):
    """Ein Blick, der nichts findet, schreibt nichts — sonst sähe ein toter Faden gleich aus."""
    gesehen = []
    monkeypatch.setattr(mitlauf, "tick", lambda _config: gesehen.append(1))

    with caplog.at_level("INFO", logger="chronicle.mitlauf"):
        mitlauf.betreiben(
            config,
            schlafen=lambda _: None,
            weiter=lambda: len(gesehen) < mitlauf.LEBENSZEICHEN + 1,
        )

    assert caplog.messages == [mitlauf.WACH % 0, mitlauf.WACH % mitlauf.LEBENSZEICHEN]


# --- Ein Fehlschlag hält die Schlange nicht an ----------------------------------------


def test_ein_gescheitertes_haeppchen_haelt_die_schlange_nicht_an(config, sitzung_id, monkeypatch):
    """Ein HTTP 500 gilt einer Datei, nicht dem Abend — die übrigen laufen weiter."""

    class Launisch(Erkenner):
        def transcribe(self, audio_path, *, hotwords=()):
            if "mitte" in audio_path.name:
                raise TranscriberError("abgewiesen: HTTP 500")
            yield from super().transcribe(audio_path, hotwords=hotwords)

    einreihen(config, sitzung_id, "erstes.wav")
    einreihen(config, sitzung_id, "mitte.wav")
    einreihen(config, sitzung_id, "letztes.wav")
    mit_erkenner(monkeypatch, Launisch())

    mitlauf.tick(config)

    stände = [zeile.status for zeile in recordings.for_session(runde(config), sitzung_id)]
    assert stände == [recordings.FERTIG, recordings.GESCHEITERT, recordings.FERTIG]


def test_der_mitlauf_wiederholt_nichts_sondern_laesst_es_der_nacht(config, sitzung_id, monkeypatch):
    """Sonst wären die drei Anläufe aus #247 in drei Minuten verbraucht statt in drei Nächten.

    Der Zähler greift erst beim nächsten Lauf, und die gemessenen Gründe waren zwar
    vorübergehend, aber nicht binnen einer Minute vorbei.
    """
    aufnahme = einreihen(config, sitzung_id, "mira-1.wav")

    class Kaputt(Erkenner):
        def transcribe(self, audio_path, *, hotwords=()):
            raise TranscriberError("abgewiesen: HTTP 500")
            yield  # pragma: no cover

    service.run_queue(config, runde(config), model=Kaputt())
    gescheitert = recordings.get(runde(config), aufnahme.id)
    assert gescheitert.versuche == 1

    erkenner = mit_erkenner(monkeypatch, Erkenner())
    for _ in range(3):
        mitlauf.tick(config)

    assert erkenner.gerufen == []
    assert recordings.get(runde(config), aufnahme.id).versuche == 1

    # Der Nachtlauf holt sie — dort steht der Zähler für drei Nächte.
    service.run_queue(config, runde(config), model=Erkenner())
    assert recordings.get(runde(config), aufnahme.id).status == recordings.FERTIG


def test_der_mitlauf_setzt_die_frist_nicht_durch(config, sitzung_id, monkeypatch):
    """``sweep`` **meldet**, was es löscht, und diese Meldung gehört der Runde.

    Hier fiele sie in einen Rückgabewert, den niemand liest — besonders die über eine nie
    verschriftete Spur. Durchgesetzt wird die Frist weiter vom Nachtlauf und von
    ``recordings.taeglich``.
    """
    aufnahme = einreihen(config, sitzung_id, "mira-1.wav")
    mit_erkenner(monkeypatch, Erkenner())
    mitlauf.tick(config)
    ueberfaellig(config, aufnahme.id, recordings.RETENTION_TAGE + 1)

    mitlauf.tick(config)

    assert len(list(config.recordings_dir.iterdir())) == 1
    assert recordings.get(runde(config), aufnahme.id).deleted_at is None

    (meldung,) = service.run_queue(config, runde(config))

    assert "gelöscht" in meldung
    assert list(config.recordings_dir.iterdir()) == []


def ueberfaellig(config, aufnahme_id, tage):
    zeitpunkt = (datetime.now(UTC) - timedelta(days=tage)).isoformat(timespec="seconds")
    verbindung = db.connect(config.database_path)
    try:
        with verbindung:
            verbindung.execute(
                "UPDATE recording SET uploaded_at = ? WHERE id = ?", (zeitpunkt, aufnahme_id)
            )
    finally:
        verbindung.close()
