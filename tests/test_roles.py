"""Die Kopfzeile des Proxys ist Rohtext — hier steht, was daraus wird."""

from chronicle import db, roles, settings
from chronicle.config import Config

GRUPPE = "chronik-verwaltung"


def test_kommagetrennt_mit_leerzeichen():
    assert roles.gruppen(" spieler , chronik-verwaltung ,gaeste") == frozenset(
        {"spieler", "chronik-verwaltung", "gaeste"}
    )


def test_gross_und_klein_ist_egal():
    assert roles.traegt("Chronik-Verwaltung", GRUPPE)
    assert roles.traegt("chronik-verwaltung", "CHRONIK-VERWALTUNG")


def test_leere_und_fehlende_kopfzeile_ergeben_keine_gruppe():
    assert roles.gruppen(None) == frozenset()
    assert roles.gruppen("") == frozenset()
    assert roles.gruppen(" , ,") == frozenset()


def test_ohne_gesetzte_gruppe_traegt_sie_jeder():
    assert roles.traegt(None, "")
    assert roles.traegt("spieler", "   ")


def test_wer_nicht_drinsteht_traegt_sie_nicht():
    assert not roles.traegt("spieler,gaeste", GRUPPE)
    assert not roles.traegt(None, GRUPPE)


def test_teiltreffer_zaehlt_nicht():
    assert not roles.traegt("chronik-verwaltung-vertretung", GRUPPE)


class Anfrage:
    def __init__(self, kopfzeile=None):
        self.headers = {} if kopfzeile is None else {roles.GRUPPEN_HEADER: kopfzeile}


def test_ist_verwalter_liest_die_gespeicherte_gruppe(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    assert roles.ist_verwalter(Anfrage(), config.database_path)

    settings.save_admin_group(config.database_path, GRUPPE)
    assert not roles.ist_verwalter(Anfrage(), config.database_path)
    assert roles.ist_verwalter(Anfrage(f"spieler, {GRUPPE}"), config.database_path)
