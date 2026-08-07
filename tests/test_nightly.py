"""Der nächtliche Lauf: zur angesetzten Zeit, in einer Reihenfolge, ohne Doppelstart."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import laufender_job, runde, warte_bis

from chronicle import db, jobs, nightly, notes, settings, zugang
from chronicle.app import create_app
from chronicle.config import Config
from chronicle.discord import service as diktat
from chronicle.foundry.model import SyncState


@pytest.fixture
def stelle(tmp_path):
    config = Config(data_dir=tmp_path, recordings_dir=tmp_path / "spuren")
    db.init(config.database_path)
    return config


BERLIN = ZoneInfo(settings.DEFAULT_NIGHTLY_ZONE)


def uhr(stunde, minute=0, tag=6):
    """Die Uhr der Runde, nicht die der Maschine — sonst prüften diese Tests auf einem
    UTC-Läufer etwas anderes als auf einem deutschen Schreibtisch."""
    return datetime(2026, 8, tag, stunde, minute, tzinfo=BERLIN)


def mit_notiz(config, played_on="2026-08-05"):
    sitzung_id = notes.create_session(runde(config), played_on=played_on, title="Keller")
    szene = notes.session(runde(config), sitzung_id).scenes[0]
    notes.add_note(runde(config), szene.id, "Wir brechen bei Sonnenaufgang auf.")
    return sitzung_id


def chronik_altern(config, sitzung_id, sekunden=60):
    """Eine Chronik von vorhin — in der Wirklichkeit liegen zwischen ihr und der nächsten
    Notiz Stunden, im Test dieselbe Sekunde."""
    scope = db.scoped(runde(config))
    try:
        zeile = scope.execute(
            "SELECT created_at FROM protocol "
            "WHERE runde_id = ? AND session_id = ? AND kind = 'chronik'",
            (scope.runde_id, sitzung_id),
        ).fetchone()
        frueher = datetime.fromisoformat(zeile["created_at"]) - timedelta(seconds=sekunden)
        with scope:
            scope.execute(
                "UPDATE protocol SET created_at = ? "
                "WHERE runde_id = ? AND session_id = ? AND kind = 'chronik'",
                (frueher.isoformat(timespec="seconds"), scope.runde_id, sitzung_id),
            )
    finally:
        scope.close()


def abgelegter_lauf(config, schritte, *, state=jobs.FERTIG, started="2026-08-06T04:00:00+00:00"):
    ergebnis = json.dumps(
        [{"name": name, "text": text, "gelungen": True} for name, text in schritte]
    )
    scope = db.scoped(runde(config))
    try:
        with scope:
            scope.execute(
                "INSERT INTO job (runde_id, kind, session_id, state, started_at, result) "
                "VALUES (?, ?, NULL, ?, ?, ?)",
                (scope.runde_id, jobs.NACHTLAUF, state, started, ergebnis),
            )
    finally:
        scope.close()


# --- Die Kette ----------------------------------------------------------------------


def test_die_kette_laeuft_in_ihrer_reihenfolge(stelle, monkeypatch):
    gesehen = []

    def merken(name, rueckgabe):
        def gerufen(*_args, **_kwargs):
            gesehen.append(name)
            return rueckgabe

        return gerufen

    monkeypatch.setattr(nightly, "diktat_abholen", merken("diktat", ("nichts",)))
    monkeypatch.setattr(nightly, "run_queue", merken("transkript", ()))
    monkeypatch.setattr(nightly, "sync", merken("abgleich", None))
    monkeypatch.setattr(nightly, "compose_session", merken("chronik", None))
    monkeypatch.setattr(nightly, "recap_session", lambda *a, **k: None)
    monkeypatch.setattr(nightly, "deliver", lambda *a, **k: "")
    mit_notiz(stelle)

    nightly.lauf(stelle, runde(stelle))

    assert gesehen == ["diktat", "transkript", "chronik"]


def test_mit_foundry_steht_der_abgleich_zwischen_aufnahmen_und_chronik(stelle, monkeypatch):
    gesehen = []

    def merken(name, rueckgabe):
        def gerufen(*_args, **_kwargs):
            gesehen.append(name)
            return rueckgabe

        return gerufen

    monkeypatch.setattr(nightly, "diktat_abholen", merken("diktat", ("nichts",)))
    monkeypatch.setattr(nightly, "run_queue", merken("transkript", ()))
    monkeypatch.setattr(nightly, "sync", merken("abgleich", SyncState(message="Stand vom heute.")))
    monkeypatch.setattr(nightly, "compose_session", merken("chronik", None))
    monkeypatch.setattr(nightly, "recap_session", lambda *a, **k: None)
    monkeypatch.setattr(nightly, "deliver", lambda *a, **k: "")
    eingerichtet = Config(
        data_dir=stelle.data_dir,
        foundry_url="https://foundry.example/",
        foundry_user="Chronist",
    )
    # Der Nachtlauf kann kein Passwort erfragen; er gleicht nur ab, wenn eines daliegt.
    zugang.merken(runde(eingerichtet), "passwort-nur-in-diesem-test")
    mit_notiz(eingerichtet)

    nightly.lauf(eingerichtet, runde(eingerichtet))

    assert gesehen == ["diktat", "transkript", "abgleich", "chronik"]


def test_ohne_gemerktes_passwort_wird_nachts_nicht_abgeglichen(stelle, monkeypatch):
    """Kein Fehlschlag, sondern der Normalfall — der Abgleich gehört ans Sitzungsende."""
    monkeypatch.setattr(
        nightly, "sync", lambda *a, **k: pytest.fail("ohne Passwort wird nicht abgeglichen")
    )
    eingerichtet = Config(
        data_dir=stelle.data_dir,
        foundry_url="https://foundry.example/",
        foundry_user="Chronist",
    )
    mit_notiz(eingerichtet)

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(eingerichtet, runde(eingerichtet)))}

    assert schritte[nightly.ABGLEICH]["text"] == nightly.OHNE_PASSWORT
    assert schritte[nightly.ABGLEICH]["gelungen"]


def test_ohne_bot_token_bleibt_der_briefkasten_zu_und_der_rest_laeuft(stelle):
    mit_notiz(stelle)

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}

    assert schritte[nightly.DIKTAT]["text"] == diktat.NICHT_EINGERICHTET
    assert schritte[nightly.DIKTAT]["gelungen"]
    assert schritte[nightly.TRANSKRIPT]["text"] == nightly.WARTESCHLANGE_LEER
    assert "Chronik und Rückblick geschrieben" in schritte[nightly.CHRONIK]["text"]


def test_ohne_foundry_zugang_wird_nicht_abgeglichen(stelle, monkeypatch):
    monkeypatch.setattr(
        nightly, "sync", lambda *a, **k: pytest.fail("ohne Zugang wird nicht abgeglichen")
    )

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}

    assert schritte[nightly.ABGLEICH]["text"] == nightly.OHNE_FOUNDRY


def test_ein_gescheiterter_schritt_nimmt_die_uebrigen_nicht_mit(stelle, monkeypatch):
    def klemmt(*_args, **_kwargs):
        raise RuntimeError("Discord antwortet nicht")

    monkeypatch.setattr(nightly, "diktat_abholen", klemmt)
    mit_notiz(stelle)

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}

    assert not schritte[nightly.DIKTAT]["gelungen"]
    assert "Discord antwortet nicht" in schritte[nightly.DIKTAT]["text"]
    assert "Chronik und Rückblick geschrieben" in schritte[nightly.CHRONIK]["text"]


# --- Welche Sitzung neues Material hat -----------------------------------------------


def test_geschrieben_wird_nur_wo_material_juenger_ist_als_die_chronik(stelle):
    sitzung_id = mit_notiz(stelle)
    assert [eintrag[0] for eintrag in nightly.offen(runde(stelle))] == [sitzung_id]

    nightly.lauf(stelle, runde(stelle))
    assert nightly.offen(runde(stelle)) == ()

    chronik_altern(stelle, sitzung_id)
    szene = notes.session(runde(stelle), sitzung_id).scenes[0]
    notes.add_note(runde(stelle), szene.id, "Und dann kam der Drache.")
    assert [eintrag[0] for eintrag in nightly.offen(runde(stelle))] == [sitzung_id]


def test_eine_leere_sitzung_bekommt_keine_chronik(stelle):
    notes.create_session(runde(stelle), played_on="2026-08-05", title="Noch leer")
    assert nightly.offen(runde(stelle)) == ()
    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}
    assert schritte[nightly.CHRONIK]["text"] == nightly.NICHTS_ZU_SCHREIBEN


# --- Der Zeitplan -------------------------------------------------------------------


def test_zur_angesetzten_zeit_beginnt_die_nacht(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")

    angestossen = nightly.tick(stelle, jetzt=uhr(4))

    assert angestossen is not None
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.NACHTLAUF).fertig)


def test_vor_der_angesetzten_zeit_passiert_nichts(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    assert nightly.tick(stelle, jetzt=uhr(3, 59)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF) is None


def test_eine_verpasste_nacht_wird_nicht_nachgeholt(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    assert nightly.tick(stelle, jetzt=uhr(10)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF) is None


def test_wer_kurz_nach_der_zeit_einschaltet_bekommt_seine_nacht_noch(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    assert nightly.tick(stelle, jetzt=uhr(4, 30)) is not None


def test_in_derselben_nacht_wird_nicht_zweimal_gestartet(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    erster = nightly.tick(stelle, jetzt=uhr(4))
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.NACHTLAUF).fertig)

    assert nightly.tick(stelle, jetzt=uhr(4, 20)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF).id == erster.id


def test_in_der_naechsten_nacht_laeuft_es_wieder(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    erster = nightly.tick(stelle, jetzt=uhr(4))
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.NACHTLAUF).fertig)

    # Die nächste Nacht **nach** dem eben gelaufenen Lauf: der trägt den echten Zeitpunkt,
    # ein fest eingetragenes Datum wäre irgendwann seine eigene Vergangenheit.
    morgen = datetime.now(BERLIN) + timedelta(days=1)
    zweiter = nightly.tick(stelle, jetzt=morgen.replace(hour=4, minute=0, second=0, microsecond=0))
    assert zweiter is not None
    assert zweiter.id != erster.id


def test_solange_ein_anderer_lauf_laeuft_beginnt_die_nacht_nicht(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    sitzung_id = mit_notiz(stelle)
    laufender_job(stelle.database_path, jobs.CHRONIK, sitzung_id)

    assert nightly.tick(stelle, jetzt=uhr(4)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF) is None


def test_die_gespeicherte_uhrzeit_bestimmt_den_zeitpunkt(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    settings.save_nightly_time(runde(stelle), "23:30")

    assert nightly.tick(stelle, jetzt=uhr(4)) is None
    assert nightly.tick(stelle, jetzt=uhr(23, 30)) is not None


def test_der_faden_dreht_sich_weiter_auch_wenn_ein_blick_scheitert(stelle, monkeypatch):
    runden = []

    def platzt(_config, **_kwargs):
        runden.append(len(runden))
        raise ValueError("Datenbank kurz weg")

    monkeypatch.setattr(nightly, "tick", platzt)
    nightly.betreiben(stelle, schlafen=lambda _: None, weiter=lambda: len(runden) < 3)

    assert len(runden) == 3


# --- Welche Uhr gemeint ist -----------------------------------------------------------


def test_die_uhrzeit_meint_die_zone_der_runde_und_nicht_die_des_prozesses(stelle, monkeypatch):
    """Der Container läuft auf der Box in UTC — 04:00 Berlin ist dort 02:00."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")

    assert nightly.tick(stelle, jetzt=datetime(2026, 8, 6, 2, 0, tzinfo=UTC)) is not None


def test_um_vier_uhr_utc_ist_die_sommernacht_laengst_vorbei(stelle, monkeypatch):
    """Genau der gemeldete Fehler: 04:00 UTC sind 06:00 in Berlin, zwei Stunden zu spät."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")

    assert nightly.tick(stelle, jetzt=datetime(2026, 8, 6, 4, 0, tzinfo=UTC)) is None


def test_ueber_die_sommerzeitgrenze_bleibt_04_00_dieselbe_wanduhrzeit():
    """Im Sommer sind 04:00 Berlin 02:00 UTC, im Winter 03:00 — die Einstellung bleibt."""
    assert nightly.faellig(datetime(2026, 8, 6, 2, tzinfo=UTC), "04:00", None, "Europe/Berlin")
    assert nightly.faellig(datetime(2026, 1, 6, 3, tzinfo=UTC), "04:00", None, "Europe/Berlin")

    assert not nightly.faellig(datetime(2026, 8, 6, 4, tzinfo=UTC), "04:00", None, "Europe/Berlin")
    assert not nightly.faellig(datetime(2026, 1, 6, 2, tzinfo=UTC), "04:00", None, "Europe/Berlin")


def test_zwei_runden_duerfen_in_verschiedenen_zonen_liegen(stelle, monkeypatch):
    """Der Grund gegen ein festes TZ im Pod: eine Instanz trägt beide."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine: "durch")
    settings.save_nightly_zone(runde(stelle), "Pacific/Auckland")

    assert nightly.tick(stelle, jetzt=datetime(2026, 8, 6, 2, 0, tzinfo=UTC)) is None
    assert nightly.tick(stelle, jetzt=datetime(2026, 8, 5, 16, 0, tzinfo=UTC)) is not None


def test_der_letzte_lauf_steht_in_der_zone_der_runde(stelle):
    abgelegter_lauf(stelle, [], started="2026-08-06T02:00:00+00:00")

    assert nightly.letzter(runde(stelle)).zeitpunkt == "06.08.2026 um 04:00"


# --- Die Uhrzeit als Einstellung -----------------------------------------------------


def test_ohne_eintrag_gilt_die_vorgabe(stelle):
    assert settings.nightly_time(runde(stelle)) == settings.DEFAULT_NIGHTLY_TIME


def test_ohne_eintrag_gilt_die_vorgegebene_zone(stelle):
    assert settings.nightly_zone(runde(stelle)) == settings.DEFAULT_NIGHTLY_ZONE


def test_eine_zone_wird_gespeichert(stelle):
    assert settings.save_nightly_zone(runde(stelle), "Pacific/Auckland")
    assert settings.nightly_zone(runde(stelle)) == "Pacific/Auckland"


@pytest.mark.parametrize(
    "unsinn", ["", "  ", "Europe/Wolkenkuckucksheim", "MEZ", "../../etc/passwd"]
)
def test_was_keine_zone_ist_laesst_die_bisherige_stehen(stelle, unsinn):
    settings.save_nightly_zone(runde(stelle), "Pacific/Auckland")

    assert not settings.save_nightly_zone(runde(stelle), unsinn)
    assert settings.nightly_zone(runde(stelle)) == "Pacific/Auckland"


def test_die_zone_kommt_aus_dem_formular(stelle):
    client = create_app(stelle).test_client()
    client.post("/einstellungen", data={"nightly_zone": "Pacific/Auckland"})
    assert settings.nightly_zone(runde(stelle)) == "Pacific/Auckland"


def test_die_seite_nennt_die_zone_zu_der_uhrzeit(stelle):
    html = create_app(stelle).test_client().get("/einstellungen").get_data(as_text=True)

    assert 'name="nightly_zone"' in html
    assert f'<option value="{settings.DEFAULT_NIGHTLY_ZONE}" selected>' in html


def test_eine_uhrzeit_wird_gespeichert(stelle):
    assert settings.save_nightly_time(runde(stelle), "22:15")
    assert settings.nightly_time(runde(stelle)) == "22:15"


@pytest.mark.parametrize("unsinn", ["", "  ", "vier Uhr", "25:00", "4", "04:60"])
def test_was_keine_uhrzeit_ist_laesst_die_bisherige_stehen(stelle, unsinn):
    settings.save_nightly_time(runde(stelle), "22:15")

    assert not settings.save_nightly_time(runde(stelle), unsinn)
    assert settings.nightly_time(runde(stelle)) == "22:15"


def test_die_uhrzeit_kommt_aus_dem_formular(stelle):
    client = create_app(stelle).test_client()
    client.post("/einstellungen", data={"nightly_time": "02:45"})
    assert settings.nightly_time(runde(stelle)) == "02:45"


# --- Was in den Einstellungen steht --------------------------------------------------


def test_jede_karte_zeigt_das_ergebnis_ihres_eigenen_schritts(stelle):
    abgelegter_lauf(
        stelle,
        [
            (nightly.DIKTAT, "Zwei Diktate abgelegt."),
            (nightly.TRANSKRIPT, "Eine Spur verschriftet."),
            (nightly.ABGLEICH, "Stand vom Mittwoch geholt."),
            (nightly.CHRONIK, "Sitzung vom 05.08. geschrieben."),
        ],
    )

    html = create_app(stelle).test_client().get("/einstellungen").get_data(as_text=True)

    assert "Zwei Diktate abgelegt." in html
    assert "Eine Spur verschriftet." in html
    assert "Stand vom Mittwoch geholt." in html
    assert "Sitzung vom 05.08. geschrieben." in html
    assert nightly.letzter(runde(stelle)).zeitpunkt in html


def test_ohne_gelaufene_nacht_sagt_die_seite_wann_die_erste_kommt(stelle):
    html = create_app(stelle).test_client().get("/einstellungen").get_data(as_text=True)

    assert "Noch keine Nacht gelaufen" in html
    assert settings.DEFAULT_NIGHTLY_TIME in html
    assert nightly.letzter(runde(stelle)) is None


def test_ein_unterbrochener_lauf_steht_ehrlich_da(stelle):
    abgelegter_lauf(stelle, [], state=jobs.LAEUFT)

    letzter = nightly.letzter(runde(stelle))

    assert letzter.fehler == jobs.UNTERBROCHEN
    assert not letzter.laeuft
    html = create_app(stelle).test_client().get("/einstellungen").get_data(as_text=True)
    assert jobs.UNTERBROCHEN in html


def test_ein_laufender_lauf_zeigt_sich_als_unterwegs(stelle):
    abgelegter_lauf(stelle, [], state=jobs.LAEUFT)
    verbindung = db.connect(stelle.database_path)
    try:
        zeile = verbindung.execute("SELECT id FROM job").fetchone()
    finally:
        verbindung.close()
    jobs._laufend.add(int(zeile["id"]))

    letzter = nightly.letzter(runde(stelle))

    assert letzter.laeuft
    html = create_app(stelle).test_client().get("/einstellungen").get_data(as_text=True)
    assert "noch unterwegs" in html


# --- Wo der Zeitplan hängt -----------------------------------------------------------


def test_der_dienst_stellt_den_zeitplan_an_die_blosse_app_nicht(stelle, monkeypatch):
    gestartet = []
    monkeypatch.setattr(nightly, "starten", lambda config: gestartet.append(config))

    create_app(stelle)
    assert gestartet == []

    create_app(stelle, zeitplan=True)
    assert len(gestartet) == 1


def test_das_image_startet_den_dienst_und_nicht_die_blosse_app():
    """Startete der Container ``create_app``, liefe nachts nie etwas — und niemand merkte es."""
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "chronicle.app:dienst" in dockerfile


def test_der_faden_laeuft_neben_dem_dienst(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "betreiben", lambda config, **kwargs: None)
    faden = nightly.starten(stelle)
    faden.join(timeout=5)
    assert faden.daemon


def test_das_fenster_ist_eine_stunde():
    assert nightly.FENSTER == timedelta(hours=1)
