"""Läufe, die der Server führt: anstoßen, ehrlich melden, nichts hängen lassen."""

import threading

import pytest
from conftest import GRENZE, runde, warte_bis

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
    scope = db.scoped(runde(config))
    try:
        return scope.execute(
            "SELECT COUNT(*) AS n FROM job WHERE runde_id = ?", (scope.runde_id,)
        ).fetchone()["n"]
    finally:
        scope.close()


def laufende_zeile(config, kind=jobs.ABGLEICH):
    """Eine Zeile, wie sie ein abgestürzter Prozess hinterlässt — ohne Faden dahinter."""
    scope = db.scoped(runde(config))
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO job (runde_id, kind, session_id, state, started_at) "
                "VALUES (?, ?, NULL, ?, ?)",
                (scope.runde_id, kind, jobs.LAEUFT, "2026-08-06T10:00:00+00:00"),
            )
        return int(zeiger.lastrowid)
    finally:
        scope.close()


def test_ein_lauf_beginnt_sofort_und_traegt_am_ende_sein_ergebnis(stelle):
    angestossen = jobs.start(stelle, runde(stelle), jobs.ABGLEICH, lambda: "Stand vom heute.")
    assert angestossen.laeuft

    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.ABGLEICH).fertig)
    lauf = jobs.latest(runde(stelle), jobs.ABGLEICH)
    assert lauf.result == "Stand vom heute."
    assert lauf.finished_at


def test_solange_einer_laeuft_startet_ein_zweiter_anstoss_keinen_zweiten(stelle):
    tor = threading.Event()
    erster = jobs.start(stelle, runde(stelle), jobs.ABGLEICH, haltend(tor))
    zweiter = jobs.start(stelle, runde(stelle), jobs.ABGLEICH, haltend(threading.Event()))

    assert zweiter.id == erster.id
    assert zeilen(stelle) == 1
    assert jobs.running(runde(stelle), jobs.ABGLEICH)

    tor.set()
    assert warte_bis(lambda: not jobs.running(runde(stelle), jobs.ABGLEICH))


def test_eine_maschine_traegt_immer_nur_einen_lauf(stelle):
    """Eine CPU, ein Ollama: der zweite Lauf beginnt nicht, auch nicht anderer Art."""
    sitzung_id = notes.create_session(runde(stelle), played_on="2026-08-05", title="Keller")
    tor = threading.Event()
    jobs.start(stelle, runde(stelle), jobs.ABGLEICH, haltend(tor))
    abgewiesen = jobs.start(
        stelle, runde(stelle), jobs.CHRONIK, haltend(tor), session_id=sitzung_id
    )

    assert abgewiesen is None
    assert zeilen(stelle) == 1
    tor.set()
    assert warte_bis(lambda: not jobs.running(runde(stelle), jobs.ABGLEICH))


def test_ein_gescheiterter_lauf_sagt_woran_es_lag(stelle):
    def klemmt():
        raise jobs.JobError("Foundry war nicht erreichbar.")

    jobs.start(stelle, runde(stelle), jobs.ABGLEICH, klemmt)
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.ABGLEICH).gescheitert)
    assert jobs.latest(runde(stelle), jobs.ABGLEICH).error == "Foundry war nicht erreichbar."


def test_ein_unerwarteter_fehler_landet_in_der_zeile_statt_im_faden(stelle):
    def platzt():
        raise ValueError("etwas ging schief")

    jobs.start(stelle, runde(stelle), jobs.ABGLEICH, platzt)
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.ABGLEICH).gescheitert)
    assert "etwas ging schief" in jobs.latest(runde(stelle), jobs.ABGLEICH).error


def test_ein_neustart_laesst_keinen_lauf_fuer_immer_laufen(stelle):
    job_id = laufende_zeile(stelle)

    lauf = jobs.latest(runde(stelle), jobs.ABGLEICH)
    assert lauf.id == job_id
    assert lauf.gescheitert
    assert lauf.error == jobs.UNTERBROCHEN
    assert not jobs.running(runde(stelle), jobs.ABGLEICH)


def test_nach_einem_unterbrochenen_lauf_geht_ein_neuer(stelle):
    laufende_zeile(stelle)
    jobs.start(stelle, runde(stelle), jobs.ABGLEICH, lambda: "durch")
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.ABGLEICH).fertig)


def test_der_lauf_haengt_an_seiner_sitzung(stelle):
    sitzung_id = notes.create_session(runde(stelle), played_on="2026-08-05", title="Keller")
    jobs.start(stelle, runde(stelle), jobs.CHRONIK, lambda: "durch", session_id=sitzung_id)
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.CHRONIK, sitzung_id))
    assert jobs.latest(runde(stelle), jobs.CHRONIK, sitzung_id + 1) is None


def test_ohne_lauf_gibt_es_nichts_zu_melden(stelle):
    assert jobs.latest(runde(stelle), jobs.CHRONIK) is None
    assert not jobs.running(runde(stelle), jobs.CHRONIK)


class Abgleich:
    def __init__(self, welt=None, fehler=None):
        self._welt = welt
        self._fehler = fehler

    def fetch_world(self):
        if self._fehler is not None:
            raise self._fehler
        return "u-chronist", self._welt


def test_der_abgleich_meldet_den_umfang(stelle, welt, monkeypatch):
    monkeypatch.setattr(
        jobs,
        "sync",
        lambda config, eine: foundry.sync(config, eine, client=Abgleich(welt)),
    )
    assert "Chat-Nachrichten" in jobs.abgleich(stelle, runde(stelle))


def test_ein_ausgefallenes_foundry_beendet_den_abgleich_als_gescheitert(stelle, monkeypatch):
    ausfall = Abgleich(fehler=FoundryUnreachable("keine Antwort"))
    monkeypatch.setattr(
        jobs, "sync", lambda config, eine: foundry.sync(config, eine, client=ausfall)
    )
    with pytest.raises(jobs.JobError) as fehler:
        jobs.abgleich(stelle, runde(stelle))
    assert "nicht erreichbar" in str(fehler.value)


def test_der_chronik_lauf_schreibt_chronik_und_rueckblick(stelle):
    sitzung_id = notes.create_session(runde(stelle), played_on="2026-08-05", title="Keller")
    szene = notes.session(runde(stelle), sitzung_id).scenes[0]
    notes.add_note(runde(stelle), szene.id, "Wir brechen bei Sonnenaufgang auf.")

    meldung = jobs.chronik(stelle, runde(stelle), sitzung_id)

    assert "stehen bereit" in meldung
    scope = db.scoped(runde(stelle))
    try:
        arten = {
            zeile["kind"]
            for zeile in scope.execute(
                "SELECT kind FROM protocol WHERE runde_id = ? AND session_id = ?",
                (scope.runde_id, sitzung_id),
            )
        }
    finally:
        scope.close()
    assert arten == {"chronik", "rueckblick"}


def test_eine_verschwundene_sitzung_beendet_den_lauf_ehrlich(stelle):
    with pytest.raises(jobs.JobError) as fehler:
        jobs.chronik(stelle, runde(stelle), 999)
    assert str(fehler.value) == jobs.OHNE_SITZUNG
