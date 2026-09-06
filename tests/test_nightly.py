"""Der nächtliche Lauf: zur angesetzten Zeit, in einer Reihenfolge, ohne Doppelstart."""

import json
import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests
from conftest import GRENZE, laufender_job, runde, warte_bis

from chronicle import (
    db,
    jobs,
    kette,
    lebenszyklus,
    nightly,
    notes,
    protocol,
    recordings,
    settings,
    zugang,
)
from chronicle import runde as runden
from chronicle.compose import client as modellklient
from chronicle.config import DEFAULT_SOLARIS_URL, Config
from chronicle.discord import rueckblick
from chronicle.discord import service as diktat
from chronicle.foundry.model import SyncState
from chronicle.transcribe import client as erkenner
from chronicle.transcribe import service as transcribe


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


def haltend(tor):
    """Eine Nacht, die noch dauert — das Fenster der zweiten Runde läuft derweil ab."""
    tor.wait(GRENZE)
    return "durch"


def mit_notiz(config, played_on="2026-08-05"):
    sitzung_id = notes.create_session(runde(config), played_on=played_on, title="Keller")
    szene = notes.session(runde(config), sitzung_id).scenes[0]
    notes.add_note(runde(config), szene.id, "Wir brechen bei Sonnenaufgang auf.")
    return sitzung_id


GESPROCHEN = "Da unten steht eine Tür."
STAND = "2026-08-05T21:00:00+00:00"


def mit_spur(config, sitzung_id, text=GESPROCHEN):
    """Eine verschriftete Bot-Spur, wie sie nach dem Verschriften in der Datenbank steht."""
    gastgeber = runde(config)
    scope = db.scoped(gastgeber)
    try:
        beginn = scope.execute(
            "SELECT created_at FROM scene WHERE runde_id = ? AND session_id = ? "
            "ORDER BY position LIMIT 1",
            (scope.runde_id, sitzung_id),
        ).fetchone()["created_at"]
        aufnahme = recordings.enqueue(
            gastgeber,
            sitzung_id,
            "mira.wav",
            discord_user_id="4001",
            discord_name="Mira",
            started_at=beginn,
        )
        # Verschriftet ist sie schon — sonst lüde der Nachtlauf ein echtes Modell.
        recordings.mark(gastgeber, aufnahme.id, recordings.FERTIG)
        transcribe.store(scope, sitzung_id, "mira", ((0, 2000, text),), STAND)
    finally:
        scope.close()


def mit_wartender_spur(config, sitzung_id, name="mira.m4a"):
    """Eine angenommene, noch nicht verschriftete Spur — Datei da, Zeile auf ``wartet``."""
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    (config.recordings_dir / name).write_bytes(b"kein echtes Audio, aber Bytes")
    return recordings.enqueue(runde(config), sitzung_id, name, discord_user_id="4001")


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
    monkeypatch.setattr(nightly, "run_queue", merken("transkript", transcribe.Meldungen()))
    monkeypatch.setattr(nightly, "sync", merken("abgleich", None))
    monkeypatch.setattr(kette, "compose_session", merken("chronik", None))
    monkeypatch.setattr(kette, "recap_session", lambda *a, **k: None)
    monkeypatch.setattr(kette, "deliver", lambda *a, **k: "")
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
    monkeypatch.setattr(nightly, "run_queue", merken("transkript", transcribe.Meldungen()))
    monkeypatch.setattr(nightly, "sync", merken("abgleich", SyncState(message="Stand vom heute.")))
    monkeypatch.setattr(kette, "compose_session", merken("chronik", None))
    monkeypatch.setattr(kette, "recap_session", lambda *a, **k: None)
    monkeypatch.setattr(kette, "deliver", lambda *a, **k: "")
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
    assert jobs.STEHT_OHNE_MODELL in schritte[nightly.CHRONIK]["text"]


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
    assert jobs.STEHT_OHNE_MODELL in schritte[nightly.CHRONIK]["text"]


def test_der_nachtlauf_traegt_das_gesprochene_wort_in_die_chronik(stelle):
    """Der Nachtlauf, wirklich gefahren — kein Schritt gestubbt.

    Bis #221 fehlte ihm die Übernahme der Transkripte: er verschriftete die Aufnahmen und
    komponierte daraus eine Chronik, in der kein gesprochenes Wort stand. Danach war die
    Sitzung nicht mehr fällig — der Abend blieb dauerhaft stumm, bis jemand von Hand
    ``/session done`` gab. Ein Test mit gestubbten Schritten sieht das nie.
    """
    gastgeber = runde(stelle)
    sitzung_id = mit_notiz(stelle)
    mit_spur(stelle, sitzung_id)

    nightly.lauf(stelle, gastgeber)

    szene = notes.session(gastgeber, sitzung_id).scenes[0]
    # Ohne Einwilligungseintrag steht der an der Spur nachgetragene Name da (#250) — kein
    # geratener, und erst recht kein Dateiname.
    assert f"Mira: {GESPROCHEN}" in [notiz.text for notiz in szene.notes]
    assert GESPROCHEN in protocol.stored(gastgeber, sitzung_id).text
    # Und danach ist die Sitzung von der Fälligkeitsliste — mit dem gesprochenen Wort
    # darin, nicht ohne.
    assert nightly.offen(gastgeber) == ()


def test_ohne_erkenner_bleibt_die_spur_liegen_und_die_chronik_ungeschrieben(stelle, monkeypatch):
    """Der Nachtlauf, wirklich gefahren, mit abgeschaltetem ``solaris-whisper-batch``.

    Seit #216 gibt es keinen CPU-Rückfall mehr — das ist so entschieden. Dann darf die
    Nacht aber **nichts** schreiben: eine Chronik ohne das gesprochene Wort sieht fertig
    aus, die Sitzung fiele von der Fälligkeitsliste, und der Abend bliebe stumm. Genau
    diese Gestalt war #221, nur von der anderen Seite.
    """
    gastgeber = runde(stelle)
    sitzung_id = mit_notiz(stelle)
    mit_wartender_spur(stelle, sitzung_id)

    class Aus:
        def post(self, *_args, **_kwargs):
            raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(erkenner, "_http_session", Aus)

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, gastgeber))}

    assert "cannot be reached" in schritte[nightly.TRANSKRIPT]["text"]
    assert not schritte[nightly.TRANSKRIPT]["gelungen"]
    assert not schritte[nightly.CHRONIK]["gelungen"]
    assert "without the spoken word" in schritte[nightly.CHRONIK]["text"]
    # Nichts geschrieben, nichts als gescheitert vermerkt — und die Sitzung bleibt fällig.
    assert protocol.stored(gastgeber, sitzung_id) is None
    assert recordings.pending(gastgeber)[0].status == recordings.WARTET
    assert nightly.offen(gastgeber) != ()


def test_die_nacht_sagt_der_runde_was_sie_nicht_geschrieben_hat(stelle, monkeypatch):
    """#287: der Bericht ging in ``job.result``, und den liest seit #231/#272 niemand mehr.

    Nachweisbar ohne auf 04:00 zu warten (#237): der Lauf bekommt denselben Weg zurück
    mit, den ihm auch der Faden mitgibt.
    """
    gastgeber = runde(stelle)
    sitzung_id = mit_notiz(stelle)
    mit_wartender_spur(stelle, sitzung_id)

    class Aus:
        def post(self, *_args, **_kwargs):
            raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(erkenner, "_http_session", Aus)
    gemeldet = []

    nightly.lauf(
        stelle, gastgeber, danach=lambda eine, bericht: gemeldet.append((eine.id, bericht))
    )

    ((wer, bericht),) = gemeldet
    assert wer == gastgeber.id
    assert any(nightly.OHNE_SPRACHE in zeile for zeile in bericht)
    assert any("without text" in zeile for zeile in bericht)


def test_eine_gelungene_nacht_hat_der_runde_nichts_zu_melden(stelle):
    """Die geschriebene Chronik steht ohnehin im Kanal — der Bericht sagt, was fehlt."""
    gastgeber = runde(stelle)
    sitzung_id = mit_notiz(stelle)
    mit_spur(stelle, sitzung_id)
    gemeldet = []

    nightly.lauf(stelle, gastgeber, danach=lambda _eine, bericht: gemeldet.append(bericht))

    assert gemeldet == [()]
    assert protocol.stored(gastgeber, sitzung_id) is not None


def test_eine_von_der_frist_geholte_spur_steht_im_bericht_der_nacht(stelle):
    """Der zweite Weg aus #286: ``run_queue`` räumt am Ende die Frist ab.

    Was sie dabei löscht, meldet ``recordings.sweep`` seit jeher — bis hierher fiel der
    Satz in einen Rückgabewert, den niemand las.
    """
    gastgeber = runde(stelle)
    sitzung_id = mit_notiz(stelle)
    aufnahme = mit_wartender_spur(stelle, sitzung_id)
    veraltet = (datetime.now(UTC) - timedelta(days=recordings.RETENTION_TAGE + 1)).isoformat(
        timespec="seconds"
    )
    scope = db.scoped(gastgeber)
    try:
        with scope:
            scope.execute(
                "UPDATE recording SET uploaded_at = ? WHERE runde_id = ? AND id = ?",
                (veraltet, scope.runde_id, aufnahme.id),
            )
    finally:
        scope.close()
    gemeldet = []

    nightly.lauf(stelle, gastgeber, danach=lambda _eine, bericht: gemeldet.append(bericht))

    (bericht,) = gemeldet
    assert any("never transcribed" in zeile for zeile in bericht)


class Antwort:
    """So weit der Client in eine ``requests``-Antwort hineinsieht."""

    def __init__(self, status_code, rumpf):
        self.status_code = status_code
        self._rumpf = rumpf

    def json(self):
        return self._rumpf


class Beschaeftigt:
    """``solaris-whisper-batch``, während es schon an einer Spur rechnet.

    Die erste Anfrage kommt durch, jede weitere bekommt HTTP 500 — so am 22.08. gemessen,
    vier Spuren in sechzehn Sekunden, eine mit 200 und drei mit 500. Es liegt nicht an der
    Größe: dieselbe Spur allein aufgerufen antwortet in Sekunden.
    """

    def __init__(self):
        self.aufrufe = 0

    def post(self, _url, *, json, timeout):
        self.aufrufe += 1
        if self.aufrufe == 1:
            return Antwort(200, {"segments": [{"start": 0.0, "end": 2.0, "text": GESPROCHEN}]})
        return Antwort(500, {"error": "another transcription is running"})


def test_ein_teilweise_gescheiterter_lauf_meldet_keinen_erfolg(stelle, monkeypatch):
    """#247: die Karte sagte »gelungen«, während ihr eigener Text dreimal HTTP 500 aufzählte.

    Eine **gescheiterte** Spur wartet nicht mehr; ``not pending`` war damit wahr, sobald
    alles entweder fertig oder gescheitert war. Zusammen mit dem zweiten Fehler — eine
    gescheiterte Spur kam nie wieder dran — hätte das drei Aufnahmen gekostet, ohne dass
    irgendwo etwas dazu stand.
    """
    gastgeber = runde(stelle)
    sitzung_id = mit_notiz(stelle)
    mit_wartender_spur(stelle, sitzung_id, name="mira.m4a")
    mit_wartender_spur(stelle, sitzung_id, name="brok.m4a")
    monkeypatch.setattr(
        transcribe,
        "model_from_config",
        lambda _config: erkenner.WhisperBatch(stelle, http=Beschaeftigt),
    )

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, gastgeber))}

    assert "HTTP 500" in schritte[nightly.TRANSKRIPT]["text"]
    assert not schritte[nightly.TRANSKRIPT]["gelungen"]
    staende = {spur.source: spur.status for spur in recordings.for_session(gastgeber, sitzung_id)}
    assert staende == {"mira": recordings.FERTIG, "brok": recordings.GESCHEITERT}
    # Und sie ist nicht verloren: die nächste Nacht versucht es wieder.
    assert [spur.source for spur in recordings.pending(gastgeber)] == ["brok"]


def test_eine_aufgegebene_spur_haelt_die_karte_rot(stelle):
    """Auch nach dem letzten Anlauf: Ton ohne Text ist kein gelungener Lauf.

    Sonst wäre der Fehler bloß verschoben — die Karte würde grün, sobald die Spur ihre
    Anläufe verbraucht hat, und niemand sähe in den Tagen bis zur Frist hin.
    """
    gastgeber = runde(stelle)
    sitzung_id = mit_notiz(stelle)
    spur = mit_wartender_spur(stelle, sitzung_id)
    for _ in range(recordings.MAX_VERSUCHE):
        recordings.mark(gastgeber, spur.id, recordings.GESCHEITERT, "abgewiesen: HTTP 500")

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, gastgeber))}

    assert recordings.pending(gastgeber) == ()
    assert not schritte[nightly.TRANSKRIPT]["gelungen"]


def test_ohne_sitzung_meldet_die_nacht_keine_chronik(stelle, monkeypatch):
    """Eine Zusage ohne Deckung war der Fehler: »geschrieben« stand auch dann da, wenn
    nichts entstand (#221, dieselbe Gestalt wie #182)."""
    mit_notiz(stelle)
    monkeypatch.setattr(lebenszyklus, "ruht", lambda _runde: True)

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}

    assert lebenszyklus.RUHT in schritte[nightly.CHRONIK]["text"]
    assert not schritte[nightly.CHRONIK]["gelungen"]


# --- Welche Sitzung neues Material hat -----------------------------------------------


class Draht:
    """Ein Modelldienst am Draht — was die Nacht hinausschickt, steht in ``rumpf``."""

    def __init__(self):
        self.rumpf = []
        self.abmeldungen = []

    def post(self, _url, **kwargs):
        self.rumpf.append(kwargs["json"])
        return Draht.Antwort()

    def delete(self, url, **kwargs):
        self.abmeldungen.append(url)
        return Draht.Antwort()

    class Antwort:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Die Runde tastet sich voran."}}]}


def test_die_nacht_geht_durch_die_eine_freigabestelle(stelle, monkeypatch):
    """#300: die Nacht geht nicht über ``jobs.abschluss`` und ließ die Karte deshalb stehen.

    Gemessen hat es der Nachbardienst an der Karte dieser Box: 4 h 27 min belegt für rund
    31 Minuten gerechnete Arbeit. Der Beleg steht hier auf dem Draht und nicht in einer
    Merkliste — bis #329 als ``keep_alive: 0`` an Ollama, seither als Abmeldung des
    Sitzungsfensters, weil der Ablöser keine Haltung kennt, die zu beenden wäre.
    """
    mit_notiz(stelle)
    gastgeber = runde(stelle)
    # Die beiden Werte kommen seit #230 aus der Umgebung und nicht aus der Datei.
    mit_modell = replace(
        stelle,
        ollama_url="http://modell.example:11435",
        ollama_model="chronist-test",
    )
    draht = Draht()
    # An ``requests.Session`` und nicht an ``_http_session``: dessen Funktionsobjekt steckt
    # als Vorgabewert in den Signaturen und lässt sich dort nicht mehr austauschen.
    monkeypatch.setattr(modellklient.requests, "Session", lambda: draht)
    monkeypatch.setattr(
        "chronicle.compose.client._lease_bis", time.monotonic() + modellklient.LEASE_TTL_S
    )

    nightly.lauf(mit_modell, gastgeber)

    assert protocol.stored(gastgeber, 1) is not None
    assert draht.abmeldungen == [DEFAULT_SOLARIS_URL + modellklient.LEASE_PATH]
    assert not modellklient.lease_offen()


def test_geschrieben_wird_nur_wo_material_juenger_ist_als_die_chronik(stelle):
    sitzung_id = mit_notiz(stelle)
    assert [eintrag[0] for eintrag in nightly.offen(runde(stelle))] == [sitzung_id]

    nightly.lauf(stelle, runde(stelle))
    assert nightly.offen(runde(stelle)) == ()

    chronik_altern(stelle, sitzung_id)
    szene = notes.session(runde(stelle), sitzung_id).scenes[0]
    notes.add_note(runde(stelle), szene.id, "Und dann kam der Drache.")
    assert [eintrag[0] for eintrag in nightly.offen(runde(stelle))] == [sitzung_id]


def test_eine_misslungene_zustellung_steht_auf_der_karte_der_nacht(stelle, monkeypatch):
    """Der stille Ausfall aus #182: die Nacht meldete »geschrieben« und verschwieg, dass
    der Rückblick nirgends ankam."""
    mit_notiz(stelle)
    monkeypatch.setattr(
        kette,
        "deliver",
        lambda config, eine, sitzung: rueckblick.Zustellung("Nirgends angekommen.", True),
    )

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}

    assert "Nirgends angekommen." in schritte[nightly.CHRONIK]["text"]


def test_eine_gelungene_zustellung_bleibt_von_der_karte_weg(stelle, monkeypatch):
    mit_notiz(stelle)
    monkeypatch.setattr(
        kette,
        "deliver",
        lambda config, eine, sitzung: rueckblick.Zustellung("Alles gut gegangen."),
    )

    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}

    assert "Alles gut gegangen." not in schritte[nightly.CHRONIK]["text"]


def test_eine_leere_sitzung_bekommt_keine_chronik(stelle):
    notes.create_session(runde(stelle), played_on="2026-08-05", title="Noch leer")
    assert nightly.offen(runde(stelle)) == ()
    schritte = {s["name"]: s for s in json.loads(nightly.lauf(stelle, runde(stelle)))}
    assert schritte[nightly.CHRONIK]["text"] == nightly.NICHTS_ZU_SCHREIBEN


def test_die_nacht_meldet_sich_hinterher_zurueck(stelle, monkeypatch):
    """#281: die Kette erzeugt dieselben Registervorschläge wie ``/session done``.

    Wer danach fragt, weiß diese Datei nicht — sie reicht nur durch, wenn ein Weg zurück
    mitgegeben wurde.
    """
    monkeypatch.setattr(kette, "compose_session", lambda *a, **k: None)
    mit_notiz(stelle)
    gemeldet = []

    nightly.lauf(stelle, runde(stelle), danach=lambda eine, bericht: gemeldet.append(eine))

    assert [eine.id for eine in gemeldet] == [runde(stelle).id]


def test_eine_gescheiterte_nachfrage_macht_die_nacht_nicht_rot(stelle, monkeypatch, caplog):
    """Die Nacht ist gelaufen und ihre Karte geschrieben — die Nachfrage ist ein Angebot."""
    monkeypatch.setattr(kette, "compose_session", lambda *a, **k: None)
    mit_notiz(stelle)

    def stolpert(_runde, _bericht):
        raise RuntimeError("Discord schweigt")

    with caplog.at_level(logging.ERROR):
        ergebnis = nightly.lauf(stelle, runde(stelle), danach=stolpert)

    assert [s["name"] for s in json.loads(ergebnis)] == [
        nightly.DIKTAT,
        nightly.TRANSKRIPT,
        nightly.ABGLEICH,
        nightly.CHRONIK,
    ]
    assert "Nachfrage" in caplog.text


def test_der_zurueckweg_haengt_am_faden_und_kommt_bei_jeder_nacht_an(stelle, monkeypatch):
    """Der Blick auf die Uhr reicht ihn bis in den Lauf durch — sonst bliebe er am Faden
    hängen und die Nacht meldete sich nie."""
    gemeldet = []
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: str(rest.get("danach")))
    nightly.tick(stelle, jetzt=uhr(4), danach=gemeldet.append)

    warte_bis(lambda: jobs.latest(runde(stelle), jobs.NACHTLAUF).result is not None)

    assert jobs.latest(runde(stelle), jobs.NACHTLAUF).result != "None"


# --- Der Zeitplan -------------------------------------------------------------------


def test_zur_angesetzten_zeit_beginnt_die_nacht(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")

    angestossen = nightly.tick(stelle, jetzt=uhr(4))

    assert angestossen is not None
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.NACHTLAUF).fertig)


def test_vor_der_angesetzten_zeit_passiert_nichts(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    assert nightly.tick(stelle, jetzt=uhr(3, 59)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF) is None


def test_eine_verpasste_nacht_wird_nicht_nachgeholt(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    assert nightly.tick(stelle, jetzt=uhr(10)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF) is None


def test_wer_kurz_nach_der_zeit_einschaltet_bekommt_seine_nacht_noch(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    assert nightly.tick(stelle, jetzt=uhr(4, 30)) is not None


def test_in_derselben_nacht_wird_nicht_zweimal_gestartet(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    erster = nightly.tick(stelle, jetzt=uhr(4))
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.NACHTLAUF).fertig)

    assert nightly.tick(stelle, jetzt=uhr(4, 20)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF).id == erster.id


def test_in_der_naechsten_nacht_laeuft_es_wieder(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    erster = nightly.tick(stelle, jetzt=uhr(4))
    assert warte_bis(lambda: jobs.latest(runde(stelle), jobs.NACHTLAUF).fertig)

    # Die nächste Nacht **nach** dem eben gelaufenen Lauf: der trägt den echten Zeitpunkt,
    # ein fest eingetragenes Datum wäre irgendwann seine eigene Vergangenheit.
    morgen = datetime.now(BERLIN) + timedelta(days=1)
    zweiter = nightly.tick(stelle, jetzt=morgen.replace(hour=4, minute=0, second=0, microsecond=0))
    assert zweiter is not None
    assert zweiter.id != erster.id


def test_solange_ein_anderer_lauf_laeuft_beginnt_die_nacht_nicht(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    sitzung_id = mit_notiz(stelle)
    laufender_job(stelle.database_path, jobs.CHRONIK, sitzung_id)

    assert nightly.tick(stelle, jetzt=uhr(4)) is None
    assert jobs.latest(runde(stelle), jobs.NACHTLAUF) is None


def test_bei_gleicher_uhrzeit_bekommt_auch_die_zweite_runde_ihre_nacht(stelle, monkeypatch):
    """#180: dauert die Nacht der ersten Runde länger als das Fenster, fiel die zweite
    heraus — und weil die Reihenfolge an der Id hing, jede Nacht dieselbe."""
    tor = threading.Event()
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: haltend(tor))
    erste = runde(stelle)
    zweite = runden.anlegen(stelle.database_path, "Runde B", guild_id="2202")

    angestossen = nightly.tick(stelle, jetzt=uhr(4))
    assert angestossen is not None and angestossen.runde_id == erste.id

    assert nightly.tick(stelle, jetzt=uhr(4, 30)) is None
    assert nightly.tick(stelle, jetzt=uhr(5, 30)) is None

    tor.set()
    assert warte_bis(lambda: not jobs.running(erste))

    spaeter = nightly.tick(stelle, jetzt=uhr(6))
    assert spaeter is not None and spaeter.runde_id == zweite.id


def test_wer_noch_nie_eine_nacht_hatte_ist_zuerst_dran(stelle, monkeypatch):
    """Sonst gewinnt bei gleicher Uhrzeit immer die kleinere Id."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    abgelegter_lauf(stelle, [], started="2026-08-05T02:00:00+00:00")
    zweite = runden.anlegen(stelle.database_path, "Runde B", guild_id="2202")

    angestossen = nightly.tick(stelle, jetzt=uhr(4))

    assert angestossen is not None and angestossen.runde_id == zweite.id


def test_ohne_vormerkung_bleibt_eine_verpasste_nacht_verpasst(stelle, monkeypatch):
    """Die Gegenprobe zur Vormerkung: war niemand da, wird nichts nachgeholt."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    runden.anlegen(stelle.database_path, "Runde B", guild_id="2202")

    assert nightly.tick(stelle, jetzt=uhr(6)) is None


def test_die_gespeicherte_uhrzeit_bestimmt_den_zeitpunkt(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    settings.save_nightly_time(runde(stelle), "23:30")

    assert nightly.tick(stelle, jetzt=uhr(4)) is None
    assert nightly.tick(stelle, jetzt=uhr(23, 30)) is not None


def test_eine_verabschiedete_runde_bekommt_keine_nacht_mehr(stelle, monkeypatch):
    """Dieser Faden sähe den Rauswurf sonst nie — er verschriftete dreißig Tage lang
    weiter, was eine Gruppe längst widerrufen hat."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
    unsere = runden.anlegen(stelle.database_path, "Der Krumme Ast", guild_id="1101")
    lebenszyklus.sperren(stelle.database_path, "1101")

    nightly.tick(stelle, jetzt=uhr(4))

    assert jobs.latest(unsere, jobs.NACHTLAUF) is None


def test_der_faden_dreht_sich_weiter_auch_wenn_ein_blick_scheitert(stelle, monkeypatch):
    runden = []

    def platzt(_config, **_kwargs):
        runden.append(len(runden))
        raise ValueError("Datenbank kurz weg")

    monkeypatch.setattr(nightly, "tick", platzt)
    nightly.betreiben(stelle, schlafen=lambda _: None, weiter=lambda: len(runden) < 3)

    assert len(runden) == 3


def _stiller_faden(monkeypatch, stelle, durchgaenge):
    """So viele Blicke auf die Uhr, und keiner findet etwas — der Alltag des Fadens."""
    gesehen = []
    monkeypatch.setattr(nightly, "tick", lambda _config, **_k: gesehen.append(1))
    nightly.betreiben(stelle, schlafen=lambda _: None, weiter=lambda: len(gesehen) < durchgaenge)


def test_der_faden_sagt_ohne_faellige_nacht_dass_er_lebt(stelle, monkeypatch, caplog):
    """Sonst sähen ein laufender und ein toter Faden 23 von 24 Stunden gleich aus (#237)."""
    with caplog.at_level(logging.INFO, logger="chronicle.nightly"):
        _stiller_faden(monkeypatch, stelle, 1)

    assert nightly.WACH % 0 in caplog.messages


def test_das_lebenszeichen_nennt_keine_runde(stelle, monkeypatch, caplog):
    """Wer wann spielt, gehört in keine Logzeile — ein Lebenszeichen erst recht nicht."""
    runden.anlegen(stelle.database_path, "Der Krumme Ast", guild_id="2202")

    with caplog.at_level(logging.INFO, logger="chronicle.nightly"):
        _stiller_faden(monkeypatch, stelle, 1)

    assert "Der Krumme Ast" not in caplog.text


def test_das_lebenszeichen_kommt_nicht_bei_jedem_blick(stelle, monkeypatch, caplog):
    """Eine Zeile je Minute wäre bald das Einzige, was im Log des Bots noch steht."""
    with caplog.at_level(logging.INFO, logger="chronicle.nightly"):
        _stiller_faden(monkeypatch, stelle, nightly.LEBENSZEICHEN + 1)

    assert caplog.messages == [nightly.WACH % 0, nightly.WACH % nightly.LEBENSZEICHEN]


# --- Welche Uhr gemeint ist -----------------------------------------------------------


def test_die_uhrzeit_meint_die_zone_der_runde_und_nicht_die_des_prozesses(stelle, monkeypatch):
    """Der Container läuft auf der Box in UTC — 04:00 Berlin ist dort 02:00."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")

    assert nightly.tick(stelle, jetzt=datetime(2026, 8, 6, 2, 0, tzinfo=UTC)) is not None


def test_um_vier_uhr_utc_ist_die_sommernacht_laengst_vorbei(stelle, monkeypatch):
    """Genau der gemeldete Fehler: 04:00 UTC sind 06:00 in Berlin, zwei Stunden zu spät."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")

    assert nightly.tick(stelle, jetzt=datetime(2026, 8, 6, 4, 0, tzinfo=UTC)) is None


def test_ueber_die_sommerzeitgrenze_bleibt_04_00_dieselbe_wanduhrzeit():
    """Im Sommer sind 04:00 Berlin 02:00 UTC, im Winter 03:00 — die Einstellung bleibt."""
    assert nightly.faellig(datetime(2026, 8, 6, 2, tzinfo=UTC), "04:00", None, "Europe/Berlin")
    assert nightly.faellig(datetime(2026, 1, 6, 3, tzinfo=UTC), "04:00", None, "Europe/Berlin")

    assert not nightly.faellig(datetime(2026, 8, 6, 4, tzinfo=UTC), "04:00", None, "Europe/Berlin")
    assert not nightly.faellig(datetime(2026, 1, 6, 2, tzinfo=UTC), "04:00", None, "Europe/Berlin")


def test_zwei_runden_duerfen_in_verschiedenen_zonen_liegen(stelle, monkeypatch):
    """Der Grund gegen ein festes TZ im Pod: eine Instanz trägt beide."""
    monkeypatch.setattr(nightly, "lauf", lambda config, eine, **rest: "durch")
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


def test_eine_uhrzeit_wird_gespeichert(stelle):
    assert settings.save_nightly_time(runde(stelle), "22:15")
    assert settings.nightly_time(runde(stelle)) == "22:15"


@pytest.mark.parametrize("unsinn", ["", "  ", "vier Uhr", "25:00", "4", "04:60"])
def test_was_keine_uhrzeit_ist_laesst_die_bisherige_stehen(stelle, unsinn):
    settings.save_nightly_time(runde(stelle), "22:15")

    assert not settings.save_nightly_time(runde(stelle), unsinn)
    assert settings.nightly_time(runde(stelle)) == "22:15"


# --- Was der letzte Lauf hergibt -----------------------------------------------------
#
# Angezeigt wird das seit #89 nicht mehr auf der Betreiber-Seite: der Lauf gehört der
# Runde. Erzählt wird er in Discord; hier steht, was dafür bereitliegt.


def test_jeder_schritt_kommt_mit_seinem_eigenen_ergebnis_zurueck(stelle):
    abgelegter_lauf(
        stelle,
        [
            (nightly.DIKTAT, "Zwei Diktate abgelegt."),
            (nightly.TRANSKRIPT, "Eine Spur verschriftet."),
            (nightly.ABGLEICH, "Stand vom Mittwoch geholt."),
            (nightly.CHRONIK, "Sitzung vom 05.08. geschrieben."),
        ],
    )

    letzter = nightly.letzter(runde(stelle))

    assert letzter.diktat.text == "Zwei Diktate abgelegt."
    assert letzter.transkript.text == "Eine Spur verschriftet."
    assert letzter.abgleich.text == "Stand vom Mittwoch geholt."
    assert letzter.chronik.text == "Sitzung vom 05.08. geschrieben."
    assert letzter.zeitpunkt == "06.08.2026 um 06:00"


def test_ohne_gelaufene_nacht_gibt_es_keinen_lauf(stelle):
    assert nightly.letzter(runde(stelle)) is None


def test_ein_unterbrochener_lauf_steht_ehrlich_da(stelle):
    abgelegter_lauf(stelle, [], state=jobs.LAEUFT)

    letzter = nightly.letzter(runde(stelle))

    assert letzter.fehler == jobs.UNTERBROCHEN
    assert not letzter.laeuft


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


# --- Wo der Zeitplan hängt -----------------------------------------------------------


def test_der_faden_laeuft_neben_dem_bot(stelle, monkeypatch):
    monkeypatch.setattr(nightly, "betreiben", lambda config, **kwargs: None)
    faden = nightly.starten(stelle)
    faden.join(timeout=5)
    assert faden.daemon


def test_das_fenster_ist_eine_stunde():
    assert nightly.FENSTER == timedelta(hours=1)
