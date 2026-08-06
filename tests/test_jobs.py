"""Läufe, die der Server führt: anstoßen, ehrlich melden, nichts hängen lassen."""

import threading

import pytest
from conftest import GRENZE, warte_bis

from chronicle import db, jobs, notes
from chronicle.config import Config
from chronicle.foundry import service as foundry
from chronicle.foundry.client import FoundryUnreachable


@pytest.fixture
def stelle(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    return config


def haltend(tor):
    def lauf():
        tor.wait(GRENZE)
        return "durch"

    return lauf


def zeilen(config):
    verbindung = db.connect(config.database_path)
    try:
        return verbindung.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]
    finally:
        verbindung.close()


def laufende_zeile(config, kind=jobs.ABGLEICH):
    """Eine Zeile, wie sie ein abgestürzter Prozess hinterlässt — ohne Faden dahinter."""
    verbindung = db.connect(config.database_path)
    try:
        with verbindung:
            zeiger = verbindung.execute(
                "INSERT INTO job (kind, session_id, state, started_at) VALUES (?, NULL, ?, ?)",
                (kind, jobs.LAEUFT, "2026-08-06T10:00:00+00:00"),
            )
        return int(zeiger.lastrowid)
    finally:
        verbindung.close()


def test_ein_lauf_beginnt_sofort_und_traegt_am_ende_sein_ergebnis(stelle):
    angestossen = jobs.start(stelle, jobs.ABGLEICH, lambda: "Stand vom heute.")
    assert angestossen.laeuft

    assert warte_bis(lambda: jobs.latest(stelle.database_path, jobs.ABGLEICH).fertig)
    lauf = jobs.latest(stelle.database_path, jobs.ABGLEICH)
    assert lauf.result == "Stand vom heute."
    assert lauf.finished_at


def test_solange_einer_laeuft_startet_ein_zweiter_anstoss_keinen_zweiten(stelle):
    tor = threading.Event()
    erster = jobs.start(stelle, jobs.ABGLEICH, haltend(tor))
    zweiter = jobs.start(stelle, jobs.ABGLEICH, haltend(threading.Event()))

    assert zweiter.id == erster.id
    assert zeilen(stelle) == 1
    assert jobs.running(stelle.database_path, jobs.ABGLEICH)

    tor.set()
    assert warte_bis(lambda: not jobs.running(stelle.database_path, jobs.ABGLEICH))


def test_zwei_arten_stehen_sich_nicht_im_weg(stelle):
    sitzung_id = notes.create_session(stelle.database_path, played_on="2026-08-05", title="Keller")
    tor = threading.Event()
    jobs.start(stelle, jobs.ABGLEICH, haltend(tor))
    jobs.start(stelle, jobs.CHRONIK, haltend(tor), session_id=sitzung_id)

    assert zeilen(stelle) == 2
    tor.set()
    assert warte_bis(lambda: not jobs.running(stelle.database_path, jobs.CHRONIK))


def test_ein_gescheiterter_lauf_sagt_woran_es_lag(stelle):
    def klemmt():
        raise jobs.JobError("Foundry war nicht erreichbar.")

    jobs.start(stelle, jobs.ABGLEICH, klemmt)
    assert warte_bis(lambda: jobs.latest(stelle.database_path, jobs.ABGLEICH).gescheitert)
    assert jobs.latest(stelle.database_path, jobs.ABGLEICH).error == "Foundry war nicht erreichbar."


def test_ein_unerwarteter_fehler_landet_in_der_zeile_statt_im_faden(stelle):
    def platzt():
        raise ValueError("etwas ging schief")

    jobs.start(stelle, jobs.ABGLEICH, platzt)
    assert warte_bis(lambda: jobs.latest(stelle.database_path, jobs.ABGLEICH).gescheitert)
    assert "etwas ging schief" in jobs.latest(stelle.database_path, jobs.ABGLEICH).error


def test_ein_neustart_laesst_keinen_lauf_fuer_immer_laufen(stelle):
    job_id = laufende_zeile(stelle)

    lauf = jobs.latest(stelle.database_path, jobs.ABGLEICH)
    assert lauf.id == job_id
    assert lauf.gescheitert
    assert lauf.error == jobs.UNTERBROCHEN
    assert not jobs.running(stelle.database_path, jobs.ABGLEICH)


def test_nach_einem_unterbrochenen_lauf_geht_ein_neuer(stelle):
    laufende_zeile(stelle)
    jobs.start(stelle, jobs.ABGLEICH, lambda: "durch")
    assert warte_bis(lambda: jobs.latest(stelle.database_path, jobs.ABGLEICH).fertig)


def test_der_lauf_haengt_an_seiner_sitzung(stelle):
    sitzung_id = notes.create_session(stelle.database_path, played_on="2026-08-05", title="Keller")
    jobs.start(stelle, jobs.CHRONIK, lambda: "durch", session_id=sitzung_id)
    assert warte_bis(lambda: jobs.latest(stelle.database_path, jobs.CHRONIK, sitzung_id))
    assert jobs.latest(stelle.database_path, jobs.CHRONIK, sitzung_id + 1) is None


def test_ohne_lauf_gibt_es_nichts_zu_melden(stelle):
    assert jobs.latest(stelle.database_path, jobs.CHRONIK) is None
    assert not jobs.running(stelle.database_path, jobs.CHRONIK)


class Abgleich:
    def __init__(self, welt=None, fehler=None):
        self._welt = welt
        self._fehler = fehler

    def fetch_world(self):
        if self._fehler is not None:
            raise self._fehler
        return "u-chronist", self._welt


def test_der_abgleich_meldet_den_umfang(stelle, welt, monkeypatch):
    monkeypatch.setattr(jobs, "sync", lambda config: foundry.sync(config, client=Abgleich(welt)))
    assert "Chat-Nachrichten" in jobs.abgleich(stelle)


def test_ein_ausgefallenes_foundry_beendet_den_abgleich_als_gescheitert(stelle, monkeypatch):
    ausfall = Abgleich(fehler=FoundryUnreachable("keine Antwort"))
    monkeypatch.setattr(jobs, "sync", lambda config: foundry.sync(config, client=ausfall))
    with pytest.raises(jobs.JobError) as fehler:
        jobs.abgleich(stelle)
    assert "nicht erreichbar" in str(fehler.value)


def test_der_chronik_lauf_schreibt_chronik_und_rueckblick(stelle):
    sitzung_id = notes.create_session(stelle.database_path, played_on="2026-08-05", title="Keller")
    szene = notes.session(stelle.database_path, sitzung_id).scenes[0]
    notes.add_note(stelle.database_path, szene.id, "Wir brechen bei Sonnenaufgang auf.")

    meldung = jobs.chronik(stelle, sitzung_id)

    assert "stehen bereit" in meldung
    verbindung = db.connect(stelle.database_path)
    try:
        arten = {
            zeile["kind"]
            for zeile in verbindung.execute(
                "SELECT kind FROM protocol WHERE session_id = ?", (sitzung_id,)
            )
        }
    finally:
        verbindung.close()
    assert arten == {"chronik", "rueckblick"}


def test_eine_verschwundene_sitzung_beendet_den_lauf_ehrlich(stelle):
    with pytest.raises(jobs.JobError) as fehler:
        jobs.chronik(stelle, 999)
    assert str(fehler.value) == jobs.OHNE_SITZUNG
