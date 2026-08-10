"""Erfundene Testdaten in den Formen aus docs/foundry-zugriff.md.

Nichts hier stammt aus einem echten Weltabzug: der enthält die Klarnamen aller
Beteiligten und gehört deshalb nie ins Repo.
"""

import threading
import time

import pytest

from chronicle import jobs, zugang
from chronicle import runde as runden
from chronicle.config import Config

# Großzügig — gemessen wird nicht, gewartet wird nur, bis ein Faden durch ist.
GRENZE = 5.0

UNSER_KONTO = "u-chronist"
LEITUNG = "u-leitung"

PASSWORT = "passwort-nur-fuer-den-test"

VERWORFENE_ADRESSE = "verworfen@example.invalid"
UNBETEILIGTES_KONTO = "Ehemaliges Konto"
GM_FIGUR = "Der Schattenfuerst"
GM_GEFLUESTER = "Der Schattenfuerst wartet im Keller."

WELT_ID = "der-krumme-ast"
WELT_TITEL = "Der Krumme Ast"

# Nutzersprache: was der Bot antwortet und was eine Seite zeigt, sagt was zu tun ist und
# was als Nächstes passiert — nie, wie es innen gemacht wird. Aufrufe von Hand, Namen von
# Umgebungsvariablen und Kopfzeilen des Proxys sind Innenleben; niemand muss sie kennen,
# um die Chronik zu bedienen. Die Liste steht hier und nicht in einer Testdatei, weil
# derselbe Sweep über die Seiten **und** über die Antworten des Bots läuft.
SYSTEMWOERTER = (
    "python -m",
    "CHRONICLE_",
    "OLLAMA_",
    "FOUNDRY_",
    "Remote-User",
    "Remote-Groups",
    "Forward-Auth",
    "Authelia",
    "runde_id",
    "guild_id",
    "thread_id",
    "session_id",
    "SQLite",
)


def systemsprache(text):
    return [wort for wort in SYSTEMWOERTER if wort in text]


WELT = {
    "world": {"id": WELT_ID, "title": WELT_TITEL},
    "system": {"id": "daggerheart"},
    "users": [
        {"_id": UNSER_KONTO, "name": "Chronist", "role": 1, "email": VERWORFENE_ADRESSE},
        {"_id": LEITUNG, "name": "Spielleitung", "role": 4},
        {"_id": "u-mira", "name": "Mira", "role": 1},
        {"_id": "u-fort", "name": UNBETEILIGTES_KONTO, "role": 1, "email": VERWORFENE_ADRESSE},
    ],
    "actors": [
        {
            "_id": "a-aelin",
            "name": "Aelin Sturmwind",
            "type": "character",
            "ownership": {"default": 2, "u-mira": 3},
        },
        {
            "_id": "a-brok",
            "name": "Brok Eisenfaust",
            "type": "character",
            "ownership": {"default": 0, UNSER_KONTO: 3},
        },
        {
            "_id": "a-wirtin",
            "name": "Die Wirtin zum Krummen Ast",
            "type": "npc",
            "ownership": {"default": 1},
        },
        {
            "_id": "a-schatten",
            "name": GM_FIGUR,
            "type": "npc",
            "ownership": {"default": 0, LEITUNG: 3},
        },
    ],
    "messages": [
        {
            "_id": "m-aufbruch",
            "timestamp": 1000,
            "author": "u-mira",
            "content": "Wir brechen bei Sonnenaufgang auf.",
            "speaker": {"actor": "a-aelin", "alias": "Aelin Sturmwind"},
        },
        {
            "_id": "m-wurf",
            "timestamp": 2000,
            "author": UNSER_KONTO,
            "content": "",
            "speaker": {"actor": "a-brok", "alias": "Brok Eisenfaust"},
            "system": {
                "roll": {
                    "title": "Knowledge Roll",
                    "total": 7,
                    "formula": "1d12 + 1d12 + 3",
                    "type": "action",
                    "isCritical": False,
                    "modifierTotal": 3,
                    "hope": {"dice": "d12", "value": 3},
                    "fear": {"dice": "d12", "value": 1},
                }
            },
        },
        {
            "_id": "m-fluester-gm",
            "timestamp": 3000,
            "author": LEITUNG,
            "whisper": [LEITUNG],
            "content": GM_GEFLUESTER,
            "speaker": {},
        },
        {
            "_id": "m-blind",
            "timestamp": 4000,
            "author": LEITUNG,
            "blind": True,
            "content": "",
            "speaker": {},
            "system": {"roll": {"title": "Verborgener Wurf", "total": 18}},
        },
        {
            "_id": "m-fluester-an-uns",
            "timestamp": 5000,
            "author": LEITUNG,
            "whisper": [UNSER_KONTO, LEITUNG],
            "content": "Du bemerkst eine Falle.",
            "speaker": {},
        },
    ],
    "scenes": [{"_id": "s-keller", "name": "Der Keller unter dem Krummen Ast"}],
}


_gemerkt: dict = {}


def runde(quelle):
    """Die erste Runde zu einer Konfiguration oder einem Pfad.

    Die Oberfläche und die Stapelaufrufe arbeiten bis #69 stillschweigend in ihr; die
    Tests bis auf ``test_isolation`` deshalb auch. Wo zwei Runden gebraucht werden, legt
    der Test die zweite selbst an.

    Gemerkt wird sie, weil ``runden.erste`` das Schema anlegt — und damit auch den
    Suchindex neu baut. Ein Test, der den Index von Hand leert, um seinen Neuaufbau zu
    prüfen, bekäme ihn sonst schon beim Nachfragen zurück.
    """
    pfad = getattr(quelle, "database_path", quelle)
    if pfad not in _gemerkt:
        _gemerkt[pfad] = runden.erste(pfad)
    return _gemerkt[pfad]


def warte_bis(bedingung):
    ende = time.monotonic() + GRENZE
    while time.monotonic() < ende:
        if bedingung():
            return True
        time.sleep(0.01)
    return False


def warte_auf_laeufe():
    """Bis kein angestoßener Lauf mehr nebenher zu Ende geht.

    Ein Faden setzt seinen Auftrag erst am Ende auf fertig und streicht ihn aus
    ``jobs._laufend``. Wer daneben eine Vorbedingung stellt — eine Zeile auf ``laeuft``
    setzen, eine Kennung eintragen —, bekommt sie ihm sonst wieder abgeräumt: der Auftrag
    steht auf fertig, obwohl der Test ihn laufen sieht, und die Merkliste ist über alle
    Datenbanken hinweg dieselbe.
    """
    for faden in threading.enumerate():
        if faden.name.startswith(jobs.FADEN):
            faden.join(GRENZE)


def laufender_job(database_path, kind, session_id=None):
    """Eine Zeile samt Faden-Vermerk — so sieht ein wirklich laufender Job aus."""
    from chronicle import db

    scope = db.scoped(runde(database_path))
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO job (runde_id, kind, session_id, state, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope.runde_id, kind, session_id, jobs.LAEUFT, "2026-08-06T10:00:00+00:00"),
            )
        job_id = int(zeiger.lastrowid)
    finally:
        scope.close()
    jobs._laufend.add(job_id)
    return job_id


@pytest.fixture(autouse=True)
def ohne_alte_laeufe():
    """Ein Lauf des einen Tests darf im nächsten nicht als »läuft noch« dastehen."""
    jobs._laufend.clear()
    _gemerkt.clear()
    yield
    warte_auf_laeufe()
    jobs._laufend.clear()
    _gemerkt.clear()


@pytest.fixture
def welt():
    return WELT


@pytest.fixture(autouse=True)
def ohne_gemerkte_passwoerter():
    """Kein Passwort eines Tests liegt im nächsten noch im Speicher."""
    zugang.vergiss_alle()
    yield
    zugang.vergiss_alle()


@pytest.fixture
def config(tmp_path):
    return Config(
        foundry_url="https://foundry.example/",
        foundry_user="Chronist",
        data_dir=tmp_path,
    )
