"""Der Zwischenstand am Szenenschnitt: Deutung, nie Beleg — und er fließt nirgends zurück.

Vier Zusagen hält diese Datei fest (#294, Carve-out aus PR #296):

* Ein Schnitt stößt **genau einen** Lauf für die **geschlossene** Szene an und antwortet
  sofort; ein zweiter Lauf beginnt nicht, solange einer läuft.
* Der Text weist sich als Deutung aus und steht in **keiner** Tabelle — die Endchronik
  und der Rückblick können ihn deshalb gar nicht als Fakt zurücklesen.
* Der Schnitt geht **nicht** nach Foundry und verbraucht **nicht** das gemerkte Passwort
  (#64) — sonst stünde der Abschluss am Abendende ohne eines da.
* Ohne Modell oder mit einem, das nicht antwortet, verhält sich der Schnitt wie vor #294.
"""

import pytest
import requests
from conftest import PASSWORT, deutsche_runde, runde, warte_auf_laeufe

from chronicle import db, jobs, notes, settings, zugang
from chronicle import sprache as sprachen
from chronicle.bot import chronik
from chronicle.compose import client as modellklient
from chronicle.compose import service as compose_service
from chronicle.compose.client import ModelUnreachable
from chronicle.compose.composer import SceneMaterial, compose
from chronicle.compose.recap import recap
from chronicle.compose.service import zwischenstand_der_szene
from chronicle.compose.zwischenstand import zwischenstand
from chronicle.config import Config
from chronicle.foundry import service as foundry

STAND = "2026-08-05T20:00:00+00:00"

_TEXTE = sprachen.zwischenstand(sprachen.DEUTSCH)
_CHRONIK = sprachen.chronik(sprachen.DEUTSCH)

GEDEUTET = "Die Runde stieg in den Keller und fand die Tür verschlossen."


class Modell:
    def __init__(self, antwort=GEDEUTET, name="chronist-test"):
        self.name = name
        self.antwort = antwort
        self.prompts = []

    def write(self, *, system, prompt):
        self.prompts.append(prompt)
        if isinstance(self.antwort, Exception):
            raise self.antwort
        return self.antwort


def stueck(*notizen, position=2, title="Im Keller"):
    return SceneMaterial(position=position, title=title, notes=tuple(notizen))


def deutsch(scene, model):
    return zwischenstand(scene, model, inhaltssprache=sprachen.DEUTSCH)


# -- Die Stufe für sich ------------------------------------------------------------------


def test_der_zwischenstand_weist_sich_als_deutung_aus():
    ergebnis = deutsch(stueck("Wir steigen in den Keller."), Modell())

    assert ergebnis is not None
    assert ergebnis.text.startswith("## Zwischenstand — Szene 2 — Im Keller")
    assert _TEXTE.hinweis.format(name="chronist-test") in ergebnis.text
    assert GEDEUTET in ergebnis.text
    # Kein Belegblock: was belegt ist, steht in der Chronik. Zwei Belegblöcke, von denen
    # einer keiner ist, wären genau die Verwechslung, gegen die diese Stufe steht.
    assert _CHRONIK.beleg_titel not in ergebnis.text


def test_eine_unbelegte_zahl_verwirft_den_absatz():
    ergebnis = deutsch(stueck("Wir steigen in den Keller."), Modell("Dort standen 12 Wachen."))

    assert ergebnis is not None
    assert "12" not in ergebnis.text
    assert _TEXTE.verworfen in ergebnis.text


def test_eine_belegte_zahl_bleibt_stehen():
    ergebnis = deutsch(stueck("Im Keller standen 12 Wachen."), Modell("Es waren 12 Wachen."))

    assert ergebnis is not None
    assert "Es waren 12 Wachen." in ergebnis.text


def test_eine_eigene_ueberschrift_verwirft_den_absatz():
    ergebnis = deutsch(stueck("Wir steigen in den Keller."), Modell("### Belegt\nSo war es."))

    assert ergebnis is not None
    assert "### Belegt" not in ergebnis.text
    assert _TEXTE.verworfen_ueberschrift in ergebnis.text


def test_das_material_steht_zwischen_marken():
    modell = Modell()
    deutsch(stueck("Wir steigen in den Keller."), modell)

    vorlage = modell.prompts[0]
    assert sprachen.ZITAT_AUF in vorlage and sprachen.ZITAT_ZU in vorlage
    assert vorlage.rstrip().endswith(_TEXTE.auftrag), "der Auftrag steht außerhalb der Marken"


@pytest.mark.parametrize(
    "modell",
    [None, Modell(ModelUnreachable("Ollama schweigt"))],
    ids=["ohne-modell", "modell-nicht-erreichbar"],
)
def test_ohne_karte_faellt_er_ersatzlos_aus(modell):
    """Der Carve-out wörtlich: wo keine Karte steht, entsteht kein Zwischenstand."""
    assert deutsch(stueck("Wir steigen in den Keller."), modell) is None


def test_eine_leere_szene_ergibt_nichts():
    assert deutsch(stueck(), Modell()) is None


# -- Der Weg durch die Datenbank ---------------------------------------------------------


@pytest.fixture
def stelle(tmp_path):
    config = Config(data_dir=tmp_path, ollama_model="chronist-test")
    db.init(config.database_path)
    return config


def sitzung_mit_szenen(config):
    gewaehlt = deutsche_runde(config)
    sitzung = notes.create_session(
        gewaehlt, played_on="2026-08-05", title="Der Keller", kanal_id="kanal-1", laeuft=True
    )
    erste = notes.session(gewaehlt, sitzung).scenes[0]
    notes.add_note(gewaehlt, erste.id, "Wir steigen in den Keller.", message_id="n-1")
    return gewaehlt, sitzung, erste.id


def test_der_stoff_ist_genau_diese_eine_szene(stelle):
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    zweite = notes.add_scene(gewaehlt, sitzung, title="Auf dem Dach")
    notes.add_note(gewaehlt, zweite, "Oben pfeift der Wind.", message_id="n-2")

    scope = db.scoped(gewaehlt)
    try:
        stoff = compose_service.szenenstoff(scope, sitzung, erste)
    finally:
        scope.close()

    assert stoff is not None
    assert [notiz.text for notiz in stoff.notes] == ["Wir steigen in den Keller."]


def test_eine_szene_aus_einer_fremden_sitzung_gibt_es_nicht(stelle):
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    andere = notes.create_session(gewaehlt, played_on="2026-08-12", kanal_id="kanal-2")

    scope = db.scoped(gewaehlt)
    try:
        assert compose_service.szenenstoff(scope, andere, erste) is None
    finally:
        scope.close()


def test_der_zwischenstand_wird_nirgends_abgelegt(stelle):
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)

    ergebnis = zwischenstand_der_szene(stelle, gewaehlt, sitzung, erste, model=Modell())

    assert ergebnis is not None and GEDEUTET in ergebnis.text
    scope = db.scoped(gewaehlt)
    try:
        protokolle = scope.execute(
            "SELECT COUNT(*) AS n FROM protocol WHERE runde_id = ?", (scope.runde_id,)
        ).fetchone()["n"]
        notizen = scope.execute(
            "SELECT text FROM note WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
    finally:
        scope.close()
    assert protokolle == 0
    assert [zeile["text"] for zeile in notizen] == ["Wir steigen in den Keller."]


def test_die_endchronik_liest_ihn_nicht_als_fakt_zurueck(stelle):
    """Die Zusage aus dem Carve-out, am ganzen Weg: Chronik und Rückblick kennen ihn nicht."""
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    zwischenstand_der_szene(stelle, gewaehlt, sitzung, erste, model=Modell())

    scope = db.scoped(gewaehlt)
    try:
        stoff = compose_service.material(scope, sitzung)
    finally:
        scope.close()
    chronikstand = compose(stoff, Modell("Sie stiegen hinab."), inhaltssprache=sprachen.DEUTSCH)

    assert GEDEUTET not in chronikstand.text
    rueckstand = recap(
        compose_service.RecapMaterial(
            session_id=sitzung, played_on="2026-08-05", chronicle=chronikstand.text
        ),
        Modell("Zuletzt stiegen sie hinab."),
        inhaltssprache=sprachen.DEUTSCH,
    )
    assert GEDEUTET not in rueckstand.text


# -- Der Schnitt selbst ------------------------------------------------------------------


def test_der_schnitt_nennt_die_geschlossene_szene(stelle):
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)

    wechsel = chronik.szene_setzen(gewaehlt, sitzung, "Auf dem Dach")

    assert wechsel.geschlossen == erste, "geschlossen ist die vorige, nicht die neue"
    assert wechsel.antwort == chronik.SZENE.format(name="Auf dem Dach")
    assert notes.session(gewaehlt, sitzung).scene_count == 2


def test_der_schnitt_stoesst_genau_einen_lauf_an(stelle, monkeypatch):
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    monkeypatch.setattr(
        compose_service.client,
        "from_config",
        lambda konfiguration, *, timeout=None: Modell(),
        raising=True,
    )
    gesagt = []

    chronik.zwischenstand_starten(stelle, gewaehlt, sitzung, erste, melden=gesagt.append)
    warte_auf_laeufe()

    lauf = jobs.latest(gewaehlt, jobs.ZWISCHENSTAND)
    assert lauf is not None and lauf.fertig
    assert lauf.session_id == sitzung
    assert len(gesagt) == 1 and GEDEUTET in gesagt[0]


def test_ohne_modell_geschieht_am_schnitt_nichts(tmp_path):
    """Ohne hinterlegtes Modell bleibt es beim Verhalten von vor #294 — kein Lauf, kein Wort."""
    ohne = Config(data_dir=tmp_path)
    db.init(ohne.database_path)
    gewaehlt, sitzung, erste = sitzung_mit_szenen(ohne)
    gesagt = []

    chronik.zwischenstand_starten(ohne, gewaehlt, sitzung, erste, melden=gesagt.append)
    warte_auf_laeufe()

    assert jobs.latest(gewaehlt, jobs.ZWISCHENSTAND) is None
    assert gesagt == []


def test_kein_zweiter_lauf_solange_einer_laeuft(stelle, monkeypatch):
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    monkeypatch.setattr(
        compose_service.client,
        "from_config",
        lambda konfiguration, *, timeout=None: Modell(),
        raising=True,
    )
    scope = db.scoped(gewaehlt)
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO job (runde_id, kind, session_id, state, started_at, besitzer, "
                "herzschlag) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope.runde_id, jobs.CHRONIK, sitzung, jobs.LAEUFT, STAND, jobs._ICH, STAND),
            )
        laufend = int(zeiger.lastrowid)
    finally:
        scope.close()
    jobs._laufend.add(laufend)
    try:
        gesagt = []
        chronik.zwischenstand_starten(stelle, gewaehlt, sitzung, erste, melden=gesagt.append)
        warte_auf_laeufe()
        assert jobs.latest(gewaehlt, jobs.ZWISCHENSTAND) is None
        assert gesagt == []
    finally:
        jobs._laufend.discard(laufend)


def test_ein_stiller_lauf_meldet_nichts_in_den_thread(stelle, monkeypatch):
    """Kein Modellwort heißt kein Wort im Thread — ein Vorgriff, der ausfällt, schweigt."""
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    monkeypatch.setattr(
        compose_service.client,
        "from_config",
        lambda konfiguration, *, timeout=None: Modell(ModelUnreachable("Ollama schweigt")),
        raising=True,
    )
    gesagt = []

    chronik.zwischenstand_starten(stelle, gewaehlt, sitzung, erste, melden=gesagt.append)
    warte_auf_laeufe()

    lauf = jobs.latest(gewaehlt, jobs.ZWISCHENSTAND)
    assert lauf is not None and lauf.fertig and lauf.result == ""
    assert gesagt == []


def test_der_schnitt_geht_nicht_nach_foundry_und_verbraucht_kein_passwort(stelle, monkeypatch):
    """Der Merkzettel aus #64 gehört dem Abschluss — ein Schnitt fasst ihn nicht an."""
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    settings.save(gewaehlt, {"foundry_url": "https://foundry.example", "foundry_user": "Chronist"})
    zugang.merken(gewaehlt, PASSWORT, wer="d-1")
    monkeypatch.setattr(
        compose_service.client,
        "from_config",
        lambda konfiguration, *, timeout=None: Modell(),
        raising=True,
    )

    def nie(*args, **kwargs):
        raise AssertionError("ein Schnitt gleicht nicht mit Foundry ab")

    monkeypatch.setattr(foundry, "sync", nie, raising=True)
    monkeypatch.setattr(jobs, "sync", nie, raising=True)

    chronik.zwischenstand_starten(stelle, gewaehlt, sitzung, erste, melden=lambda text: None)
    warte_auf_laeufe()

    assert jobs.latest(gewaehlt, jobs.ZWISCHENSTAND).fertig
    assert zugang.ist_gemerkt(gewaehlt), "das gemerkte Passwort liegt für den Abschluss bereit"
    assert zugang.passwort_von(gewaehlt, "d-1") == PASSWORT


def test_die_zuletzt_gezogene_szene_ist_die_laufende(stelle):
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)

    assert notes.latest_scene(gewaehlt, sitzung) == erste
    zweite = notes.add_scene(gewaehlt, sitzung, title="Auf dem Dach")
    assert notes.latest_scene(gewaehlt, sitzung) == zweite
    assert notes.latest_scene(runde(stelle), 999) is None


# -- Die Karte und die Uhr ---------------------------------------------------------------


class Draht:
    """Eine Ollama-Gegenstelle auf dem Draht — der Beleg steht in der Anfrage, nicht in einer
    Merkliste."""

    def __init__(self, fehler=None):
        self.anfragen = []
        self._fehler = fehler

    def post(self, url, **kwargs):
        self.anfragen.append(kwargs)
        if self._fehler is not None:
            raise self._fehler
        return self

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": GEDEUTET}}]}


@pytest.fixture
def am_draht(monkeypatch):
    def gegenstelle(fehler=None):
        eines = Draht(fehler)
        # An ``requests.Session`` und nicht an ``_http_session``: dessen Funktionsobjekt
        # steckt als Vorgabewert in den Signaturen und lässt sich dort nicht austauschen.
        monkeypatch.setattr(modellklient.requests, "Session", lambda: eines)
        return eines

    return gegenstelle


def test_ein_schnitt_traegt_keine_haltefrist_mehr(stelle, am_draht):
    """#303/#329: dieser Weg läuft nicht durch ``kette.schreiben`` und wird von dessen
    Freigabe deshalb nicht erfasst.

    Ohne ausdrückliche Frist erbte er die Vorgabe des Ollama-Dienstes dieser Box — 24
    Stunden nach *jedem* Szenenschnitt, weshalb hier eine knappe mitreiste. Der Ablöser
    kennt kein ``keep_alive``, und ein erfundenes Gegenstück wäre eine Zusage über die
    Karte, die niemand einlöst. Der Beleg steht auf dem Draht.
    """
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    draht = am_draht()

    ergebnis = zwischenstand_der_szene(stelle, gewaehlt, sitzung, erste)

    assert ergebnis is not None and GEDEUTET in ergebnis.text
    assert "keep_alive" not in draht.anfragen[-1]["json"]


def test_der_schnitt_laeuft_gegen_seine_eigene_knappe_zeitgrenze(stelle, am_draht):
    """#302: die großzügige Grenze des Aufschriebs besetzte den Job-Platz eine halbe Stunde."""
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    draht = am_draht()

    zwischenstand_der_szene(stelle, gewaehlt, sitzung, erste)

    assert draht.anfragen[-1]["timeout"] == modellklient.ZWISCHENSTAND_TIMEOUT
    assert modellklient.ZWISCHENSTAND_TIMEOUT < modellklient.DEFAULT_TIMEOUT


def test_eine_gerissene_zeitgrenze_bleibt_still(stelle, am_draht):
    """Derselbe Fall wie ein Ollama, das gar nicht antwortet — kein Fehler im Thread."""
    gewaehlt, sitzung, erste = sitzung_mit_szenen(stelle)
    am_draht(requests.Timeout("zu lange"))

    assert zwischenstand_der_szene(stelle, gewaehlt, sitzung, erste) is None


def test_der_aufschrieb_behaelt_seine_grosszuegige_grenze(stelle, am_draht):
    """Die Gegenprobe zu #301: für den Aufschrieb bleiben die 1800 Sekunden richtig."""
    gewaehlt, sitzung, _ = sitzung_mit_szenen(stelle)
    draht = am_draht()

    compose_service.compose_session(stelle, gewaehlt, sitzung)

    assert draht.anfragen[-1]["timeout"] == modellklient.DEFAULT_TIMEOUT
    assert modellklient.DEFAULT_TIMEOUT == 1800.0


def test_ohne_namen_in_der_antwort_bleibt_der_hinweis_ohne_namen():
    """#320: auch der Zwischenstand behauptet kein Modell, das nichts gesagt hat."""
    ergebnis = deutsch(stueck("Wir steigen in den Keller."), Modell(name=None))

    assert ergebnis is not None
    assert _TEXTE.hinweis_ohne_namen in ergebnis.text
    assert "None" not in ergebnis.text
    assert ergebnis.model_name is None
