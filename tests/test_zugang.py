"""Der Merkzettel für das Foundry-Passwort — was er hält, wie lange, und was er zeigt.

Kein Wert hier ist ein echtes Geheimnis; sie stehen alle nur in diesem Test.
"""

import pytest
from conftest import runde

from chronicle import db, zugang
from chronicle.config import Config

PASSWORT = "passwort-nur-in-diesem-test"


@pytest.fixture
def eine_runde(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    return runde(config)


def test_was_gemerkt_wurde_kommt_zurueck(eine_runde):
    zugang.merken(eine_runde, PASSWORT)
    assert zugang.passwort(eine_runde) == PASSWORT
    assert zugang.ist_gemerkt(eine_runde)


def test_ohne_merken_liegt_nichts_da(eine_runde):
    assert zugang.passwort(eine_runde) is None
    assert not zugang.ist_gemerkt(eine_runde)


def test_vergessen_ist_sofort(eine_runde):
    zugang.merken(eine_runde, PASSWORT)
    zugang.vergiss(eine_runde)
    assert zugang.passwort(eine_runde) is None


def test_ein_leeres_passwort_merkt_nichts_und_raeumt_auf(eine_runde):
    """Ein leer abgesendetes Feld setzt nichts fort — es räumt weg."""
    zugang.merken(eine_runde, PASSWORT)
    zugang.merken(eine_runde, "   ")
    assert zugang.passwort(eine_runde) is None


def test_die_harte_frist_laesst_nichts_liegen(eine_runde):
    """Was eingegeben, aber nie verbraucht wurde, verfällt von allein."""
    zugang.merken(eine_runde, PASSWORT, frist=-1.0)
    assert zugang.passwort(eine_runde) is None
    assert not zugang.ist_gemerkt(eine_runde)
    # Abgelaufen heißt weg und nicht nur unsichtbar.
    assert eine_runde.id not in zugang._gemerkt


def test_die_frist_ist_zwoelf_stunden():
    """Lang genug für eine Sitzung bis in die Nacht, kurz genug für »am nächsten Abend weg«."""
    assert zugang.FRIST == 12 * 60 * 60


def test_das_passwort_steht_in_keinem_repr(eine_runde):
    zugang.merken(eine_runde, PASSWORT)
    assert PASSWORT not in repr(zugang._gemerkt)
    assert PASSWORT not in repr(zugang._gemerkt[eine_runde.id])
    assert zugang.MASKE in repr(zugang._gemerkt[eine_runde.id])


def test_vergiss_alle_raeumt_die_ganze_instanz(eine_runde, tmp_path):
    zweite = runde(Config(data_dir=tmp_path / "zweite"))
    zugang.merken(eine_runde, PASSWORT)
    zugang.merken(zweite, PASSWORT)
    zugang.vergiss_alle()
    assert zugang.passwort(eine_runde) is None
    assert zugang.passwort(zweite) is None
