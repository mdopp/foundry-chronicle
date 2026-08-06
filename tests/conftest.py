"""Erfundene Testdaten in den Formen aus docs/foundry-zugriff.md.

Nichts hier stammt aus einem echten Weltabzug: der enthält die Klarnamen aller
Beteiligten und gehört deshalb nie ins Repo.
"""

import time

import pytest

from chronicle import jobs
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

WELT = {
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


def warte_bis(bedingung):
    ende = time.monotonic() + GRENZE
    while time.monotonic() < ende:
        if bedingung():
            return True
        time.sleep(0.01)
    return False


def laufender_job(database_path, kind, session_id=None):
    """Eine Zeile samt Faden-Vermerk — so sieht ein wirklich laufender Job aus."""
    verbindung = jobs.db.connect(database_path)
    try:
        with verbindung:
            zeiger = verbindung.execute(
                "INSERT INTO job (kind, session_id, state, started_at) VALUES (?, ?, ?, ?)",
                (kind, session_id, jobs.LAEUFT, "2026-08-06T10:00:00+00:00"),
            )
        job_id = int(zeiger.lastrowid)
    finally:
        verbindung.close()
    jobs._laufend.add(job_id)
    return job_id


@pytest.fixture(autouse=True)
def ohne_alte_laeufe():
    """Ein Lauf des einen Tests darf im nächsten nicht als »läuft noch« dastehen."""
    jobs._laufend.clear()
    yield
    jobs._laufend.clear()


@pytest.fixture
def welt():
    return WELT


@pytest.fixture
def config(tmp_path):
    return Config(
        foundry_url="https://foundry.example/",
        foundry_user="Chronist",
        foundry_password=PASSWORT,
        data_dir=tmp_path,
    )
