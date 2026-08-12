"""Was abgelegt wurde, kommt zurück — Chronik und Rückblick unter eigener Art."""

import pytest
from conftest import runde

from chronicle import db, protocol
from chronicle.compose.composer import BELEG_TITEL, NOTIZEN_TITEL, VERBINDUNG_TITEL
from chronicle.compose.recap import CHRONIK_TITEL, FAEDEN_TITEL, HERGANG_TITEL
from chronicle.compose.service import RUECKBLICK, save

STAND = "2026-08-05T20:00:00+00:00"

CHRONIK = "\n".join(
    [
        "# Chronik — Sitzung vom 2026-08-05: Der Keller",
        "",
        "_Verbindungstexte stammen vom Sprachmodell `chronist-test`._",
        "",
        "## Szene 1 — Aufbruch",
        "",
        NOTIZEN_TITEL,
        "- Wir brechen bei Sonnenaufgang auf.",
        "- Brok packt seine Axt ein.",
        "",
        BELEG_TITEL,
        "- Brok — Knowledge Roll: Summe 7 · Formel 1d12 + 1d12 + 3",
        "",
        VERBINDUNG_TITEL,
        "Die Runde tastet sich voran.",
        "",
    ]
)


RUECKBLICK_TEXT = "\n".join(
    [
        "# Rückblick — Sitzung vom 2026-08-05: Der Keller",
        "",
        HERGANG_TITEL,
        "Die Runde stieg in den Keller.",
        "",
        FAEDEN_TITEL,
        "- Die Wirtin wartet auf Antwort.",
        "",
        CHRONIK_TITEL,
        "- Brok — Knowledge Roll: Summe 7",
        "",
    ]
)


@pytest.fixture
def scope(config):
    zugang = db.scoped(runde(config))
    yield zugang
    zugang.close()


def sitzung(scope, *, played_on="2026-08-05", title="Der Keller"):
    zeiger = scope.execute(
        "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
        (scope.runde_id, played_on, title, STAND),
    )
    scope.commit()
    return zeiger.lastrowid


def test_ohne_lauf_gibt_es_kein_protokoll(config, scope):
    assert protocol.stored(runde(config), sitzung(scope)) is None


def test_das_abgelegte_protokoll_kommt_zurueck(config, scope):
    sitzung_id = sitzung(scope)
    save(scope, sitzung_id, CHRONIK, STAND)

    abgelegt = protocol.stored(runde(config), sitzung_id)

    assert abgelegt.text == CHRONIK
    assert abgelegt.created_at == STAND


def test_die_liste_zeigt_jede_sitzung_mit_und_ohne_chronik(config, scope):
    mit = sitzung(scope, played_on="2026-08-05", title="Der Keller")
    ohne = sitzung(scope, played_on="2026-07-01", title="Der Hafen")
    save(scope, mit, CHRONIK, STAND)

    eintraege = protocol.entries(runde(config))

    assert [e.session_id for e in eintraege] == [mit, ohne]
    assert eintraege[0].created_at == STAND
    assert eintraege[1].created_at is None


def test_ohne_sitzung_ist_die_liste_leer(config, scope):
    assert protocol.entries(runde(config)) == ()


def test_chronik_und_rueckblick_liegen_unter_eigener_art(config, scope):
    sitzung_id = sitzung(scope)
    save(scope, sitzung_id, CHRONIK, STAND)
    save(scope, sitzung_id, RUECKBLICK_TEXT, STAND, kind=RUECKBLICK)

    assert protocol.stored(runde(config), sitzung_id).text == CHRONIK
    abgelegt = protocol.stored(runde(config), sitzung_id, RUECKBLICK)
    assert abgelegt.text == RUECKBLICK_TEXT


def test_ohne_rueckblick_kommt_nichts_zurueck(config, scope):
    sitzung_id = sitzung(scope)
    save(scope, sitzung_id, CHRONIK, STAND)

    assert protocol.stored(runde(config), sitzung_id, RUECKBLICK) is None
