"""Läufe, die der Server führt: anstoßen, ehrlich melden, nichts hängen lassen."""

import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from conftest import GRENZE, runde, warte_bis

from chronicle import db, jobs, lebenszyklus, notes, protocol, recordings
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.discord import rueckblick
from chronicle.foundry import service as foundry
from chronicle.foundry.client import FoundryUnreachable
from chronicle.transcribe import service as transcribe

STAND = "2026-08-05T21:00:00+00:00"


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


@contextmanager
def anderer_prozess(monkeypatch):
    """Der Blick des Nachbarcontainers auf dieselbe Datei: anderes Ich, leere Merkliste.

    Genau das ist die ausgelieferte Vorlage — ``chronik`` und ``bot`` teilen ``/data``,
    und keiner der beiden sieht den Speicher des anderen.
    """
    with monkeypatch.context() as fremd:
        fremd.setattr(jobs, "_ICH", "der-andere-container")
        fremd.setattr(jobs, "_laufend", set())
        yield


def fremde_zeile(config, herzschlag, kind=jobs.ABGLEICH):
    """Eine laufende Zeile, die einem anderen Prozess gehört."""
    scope = db.scoped(runde(config))
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO job (runde_id, kind, session_id, state, started_at, besitzer, "
                "herzschlag) VALUES (?, ?, NULL, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    kind,
                    jobs.LAEUFT,
                    "2026-08-06T10:00:00+00:00",
                    "der-andere-container",
                    herzschlag,
                ),
            )
        return int(zeiger.lastrowid)
    finally:
        scope.close()


def herzschlag(config, job_id):
    scope = db.scoped(runde(config))
    try:
        return scope.execute(
            "SELECT herzschlag FROM job WHERE runde_id = ? AND id = ?", (scope.runde_id, job_id)
        ).fetchone()["herzschlag"]
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
        lambda config, eine, passwort=None: foundry.sync(config, eine, client=Abgleich(welt)),
    )
    assert "Chat-Nachrichten" in jobs.abgleich(stelle, runde(stelle))


def test_ein_ausgefallenes_foundry_beendet_den_abgleich_als_gescheitert(stelle, monkeypatch):
    ausfall = Abgleich(fehler=FoundryUnreachable("keine Antwort"))
    monkeypatch.setattr(
        jobs,
        "sync",
        lambda config, eine, passwort=None: foundry.sync(config, eine, client=ausfall),
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


def test_eine_misslungene_zustellung_steht_in_der_antwort_des_laufs(stelle, monkeypatch):
    """#182: der Rückgabewert von ``deliver`` wurde verworfen — das Schweigen las sich wie
    eine gelungene Zustellung."""
    sitzung_id = notes.create_session(runde(stelle), played_on="2026-08-05", title="Keller")
    szene = notes.session(runde(stelle), sitzung_id).scenes[0]
    notes.add_note(runde(stelle), szene.id, "Wir brechen bei Sonnenaufgang auf.")
    monkeypatch.setattr(
        jobs,
        "deliver",
        lambda config, eine, sitzung: rueckblick.Zustellung("Nirgends angekommen.", True),
    )

    assert "Nirgends angekommen." in jobs.chronik(stelle, runde(stelle), sitzung_id)


def test_der_lauf_traegt_das_transkript_selbst_in_die_szenen(stelle):
    """Der Übergang aus #140: ohne ihn erreicht kein gesprochenes Wort die Chronik."""
    gastgeber = runde(stelle)
    sitzung_id = notes.create_session(gastgeber, played_on="2026-08-05", title="Keller")
    aufnahme = recordings.enqueue(
        gastgeber,
        sitzung_id,
        "mira.wav",
        discord_user_id="4001",
        started_at="2026-08-05T20:00:00+00:00",
    )
    # Die Spur ist schon durch die Verschriftung — hier geht es um den Schritt danach.
    recordings.mark(gastgeber, aufnahme.id, recordings.FERTIG)
    scope = db.scoped(gastgeber)
    try:
        transcribe.store(scope, sitzung_id, "mira", ((0, 2000, "Da unten steht eine Tür."),), STAND)
    finally:
        scope.close()

    jobs.chronik(stelle, gastgeber, sitzung_id)

    szene = notes.session(gastgeber, sitzung_id).scenes[0]
    # Ohne Einwilligungseintrag steht der Spurname da und kein geratener Name.
    assert [notiz.text for notiz in szene.notes] == ["mira: Da unten steht eine Tür."]
    assert "Da unten steht eine Tür." in protocol.stored(gastgeber, sitzung_id).text


def test_eine_verschwundene_sitzung_beendet_den_lauf_ehrlich(stelle):
    with pytest.raises(jobs.JobError) as fehler:
        jobs.chronik(stelle, runde(stelle), 999)
    assert str(fehler.value) == jobs.OHNE_SITZUNG


def test_die_ruhende_runde_bekommt_ihren_eigenen_grund(stelle, monkeypatch):
    """Beides endet ohne Chronik, aber »gibt es nicht mehr« wäre über die Sitzung gelogen."""
    sitzung_id = eine_sitzung_mit_notiz(stelle)
    monkeypatch.setattr(lebenszyklus, "ruht", lambda _runde: True)

    with pytest.raises(jobs.JobError) as fehler:
        jobs.chronik(stelle, runde(stelle), sitzung_id)

    assert str(fehler.value) == lebenszyklus.RUHT


def eine_sitzung_mit_notiz(stelle):
    sitzung_id = notes.create_session(runde(stelle), played_on="2026-08-05", title="Keller")
    szene = notes.session(runde(stelle), sitzung_id).scenes[0]
    notes.add_note(runde(stelle), szene.id, "Wir brechen bei Sonnenaufgang auf.")
    return sitzung_id


def test_der_abschluss_holt_erst_die_zahlen_und_schreibt_dann(stelle, welt, monkeypatch):
    monkeypatch.setattr(
        jobs,
        "sync",
        lambda config, eine, passwort=None: foundry.sync(config, eine, client=Abgleich(welt)),
    )
    sitzung_id = eine_sitzung_mit_notiz(stelle)

    meldung = jobs.abschluss(stelle, runde(stelle), sitzung_id)

    assert "stehen bereit" in meldung
    assert protokollarten(stelle, sitzung_id) == {"chronik", "rueckblick"}


def test_ein_ausgefallenes_foundry_kostet_nicht_die_ganze_chronik(stelle, monkeypatch):
    """Notizen und Aufnahmen ergeben auch ohne die Zahlen eine Chronik — mit Hinweis."""
    ausfall = Abgleich(fehler=FoundryUnreachable("keine Antwort"))
    monkeypatch.setattr(
        jobs,
        "sync",
        lambda config, eine, passwort=None: foundry.sync(config, eine, client=ausfall),
    )
    sitzung_id = eine_sitzung_mit_notiz(stelle)

    meldung = jobs.abschluss(stelle, runde(stelle), sitzung_id)

    assert "nicht erreichbar" in meldung
    assert "stehen bereit" in meldung
    assert protokollarten(stelle, sitzung_id) == {"chronik", "rueckblick"}


def protokollarten(config, sitzung_id):
    scope = db.scoped(runde(config))
    try:
        return {
            zeile["kind"]
            for zeile in scope.execute(
                "SELECT kind FROM protocol WHERE runde_id = ? AND session_id = ?",
                (scope.runde_id, sitzung_id),
            )
        }
    finally:
        scope.close()


# --- Zwei Prozesse auf einer Datei (#178) ---------------------------------------------


def test_der_nachbarprozess_raeumt_einen_laufenden_lauf_nicht_weg(stelle, monkeypatch):
    """Der gemeldete Fehler: der Bot stößt an, der Nachtlauf-Faden räumt ihn binnen einer
    Minute als »unterbrochen« weg — und ließe danach einen zweiten derselben Art los."""
    tor = threading.Event()
    angestossen = jobs.start(stelle, runde(stelle), jobs.ABGLEICH, haltend(tor))

    with anderer_prozess(monkeypatch):
        assert jobs.latest(runde(stelle), jobs.ABGLEICH).laeuft
        assert jobs.running(runde(stelle), jobs.ABGLEICH)
        assert jobs.start(stelle, runde(stelle), jobs.NACHTLAUF, lambda: "zweiter") is None

    tor.set()
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.ABGLEICH).fertig)
    lauf = jobs.latest(runde(stelle), jobs.ABGLEICH)
    assert lauf.id == angestossen.id
    assert lauf.result == "durch"
    assert zeilen(stelle) == 1


def test_ein_verstummter_fremder_lauf_wird_doch_abgeraeumt(stelle):
    """Die Gegenrichtung: ein wirklich abgestürzter Nachbar darf die Maschine nicht halten."""
    laengst = (datetime.now(UTC) - jobs.VERSTUMMT - timedelta(seconds=1)).isoformat()
    fremde_zeile(stelle, laengst)

    lauf = jobs.latest(runde(stelle), jobs.ABGLEICH)
    assert lauf.gescheitert
    assert lauf.error == jobs.UNTERBROCHEN


def test_ein_fremder_lauf_mit_frischem_lebenszeichen_bleibt_stehen(stelle):
    fremde_zeile(stelle, datetime.now(UTC).isoformat())

    assert jobs.latest(runde(stelle), jobs.ABGLEICH).laeuft


def test_ein_laufender_lauf_gibt_lebenszeichen(stelle, monkeypatch):
    """Ohne den Puls hielte der Nachbar einen stundenlangen Lauf nach anderthalb Minuten
    für abgestürzt."""
    monkeypatch.setattr(jobs, "HERZSCHLAG", 0.01)
    tor = threading.Event()
    angestossen = jobs.start(stelle, runde(stelle), jobs.ABGLEICH, haltend(tor))
    erster = herzschlag(stelle, angestossen.id)

    assert erster is not None
    assert warte_bis(lambda: herzschlag(stelle, angestossen.id) > erster)

    tor.set()
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.ABGLEICH).fertig)


# --- Was der Abgewiesene zu hören bekommt (#179) --------------------------------------


def test_die_eigene_runde_im_weg_heisst_nicht_eine_andere_runde(stelle):
    """Ein laufender ``abgleich`` blockiert den Abschluss derselben Runde."""
    tor = threading.Event()
    jobs.start(stelle, runde(stelle), jobs.ABGLEICH, haltend(tor))

    assert jobs.belegt(runde(stelle)) == jobs.BELEGT_EIGEN

    tor.set()
    assert warte_bis(lambda: not jobs.running(runde(stelle)))


def test_eine_fremde_runde_im_weg_heisst_genau_das(stelle):
    fremde = runden.anlegen(stelle.database_path, "Der Krumme Ast", guild_id="1101")
    tor = threading.Event()
    jobs.start(stelle, fremde, jobs.ABGLEICH, haltend(tor))

    assert jobs.belegt(runde(stelle)) == jobs.BELEGT

    tor.set()
    assert warte_bis(lambda: not jobs.running(fremde))


def test_wer_abgewiesen_wird_bekommt_keinen_platz_in_einer_warteschlange(stelle):
    """Der alte Satz versprach, der Lauf beginne, sobald die andere Runde durch ist."""
    sitzung_id = notes.create_session(runde(stelle), played_on="2026-08-05", title="Keller")
    fremde = runden.anlegen(stelle.database_path, "Der Krumme Ast", guild_id="1101")
    tor = threading.Event()
    jobs.start(stelle, fremde, jobs.ABGLEICH, haltend(tor))

    abgewiesen = jobs.start(
        stelle, runde(stelle), jobs.CHRONIK, lambda: "nie", session_id=sitzung_id
    )

    assert abgewiesen is None
    assert zeilen(stelle) == 0
    tor.set()
    assert warte_bis(lambda: not jobs.running(fremde))
    assert jobs.latest(runde(stelle), jobs.CHRONIK) is None
