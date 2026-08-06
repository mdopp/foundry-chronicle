import logging

from conftest import (
    GM_FIGUR,
    GM_GEFLUESTER,
    PASSWORT,
    UNBETEILIGTES_KONTO,
    UNSER_KONTO,
    VERWORFENE_ADRESSE,
)

import chronicle.foundry.__main__ as batch
from chronicle.foundry import service
from chronicle.foundry.client import FoundryUnreachable


class Abgleich:
    def __init__(self, welt=None, fehler=None):
        self._welt = welt
        self._fehler = fehler
        self.aufrufe = 0

    def fetch_world(self):
        self.aufrufe += 1
        if self._fehler is not None:
            raise self._fehler
        return UNSER_KONTO, self._welt


def gespeicherter_text(config):
    pfad = config.database_path
    roh = b""
    for datei in (pfad, pfad.with_suffix(".sqlite3-wal")):
        if datei.exists():
            roh += datei.read_bytes()
    return roh.decode("utf-8", errors="ignore")


def test_erfolgreicher_abgleich_meldet_den_umfang(config, welt):
    zustand = service.sync(config, client=Abgleich(welt))
    assert not zustand.stale
    assert zustand.snapshot.system == "daggerheart"
    assert "2 Spieler" in zustand.message
    assert "3 Charaktere" in zustand.message
    assert "3 Chat-Nachrichten" in zustand.message


def test_der_zwischenspeicher_ueberlebt_den_prozess(config, welt):
    service.sync(config, client=Abgleich(welt))
    zustand = service.current(config)
    assert not zustand.stale
    assert len(zustand.snapshot.messages) == 3


def test_gefiltert_wird_vor_dem_zwischenspeicher(config, welt):
    service.sync(config, client=Abgleich(welt))
    inhalt = gespeicherter_text(config)
    assert "Brok Eisenfaust" in inhalt
    assert GM_FIGUR not in inhalt
    assert GM_GEFLUESTER not in inhalt
    assert UNBETEILIGTES_KONTO not in inhalt
    assert VERWORFENE_ADRESSE not in inhalt
    assert "Der Keller unter dem Krummen Ast" not in inhalt


def test_ohne_abgleich_gibt_es_eine_erklaerung_statt_einer_leeren_liste(config):
    zustand = service.current(config)
    assert zustand.snapshot is None
    assert "Noch kein Abgleich" in zustand.message


def test_foundry_aus_und_noch_kein_stand_erklaert_das(config):
    zustand = service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    assert zustand.stale
    assert zustand.snapshot is None
    assert "nicht erreichbar" in zustand.message
    assert "keine Antwort" in zustand.message


def test_foundry_aus_liefert_den_letzten_stand_plus_meldung(config, welt):
    service.sync(config, client=Abgleich(welt))
    zustand = service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    assert zustand.stale
    assert len(zustand.snapshot.messages) == 3
    assert "nicht erreichbar" in zustand.message
    assert "Angezeigt wird der Stand" in zustand.message


def test_die_meldung_ueberlebt_bis_zum_naechsten_aufruf(config, welt):
    service.sync(config, client=Abgleich(welt))
    service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    assert service.current(config).stale


def test_ein_gelungener_abgleich_raeumt_die_meldung_weg(config, welt):
    service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    service.sync(config, client=Abgleich(welt))
    assert not service.current(config).stale


def test_ohne_konfiguration_meldet_der_abgleich_das_verstaendlich(tmp_path):
    from chronicle.config import Config

    zustand = service.sync(Config(data_dir=tmp_path))
    assert zustand.stale
    assert "die Adresse, der Benutzer und das Passwort" in zustand.message
    assert "in den Einstellungen" in zustand.message
    assert "FOUNDRY_" not in zustand.message


def test_die_meldung_nennt_nur_das_wirklich_fehlende(tmp_path):
    from chronicle.config import Config

    halb = Config(data_dir=tmp_path, foundry_url="https://foundry.example", foundry_user="chronist")
    zustand = service.sync(halb)
    assert "fehlt noch das Passwort" in zustand.message
    assert "Adresse" not in zustand.message


def test_kein_passwort_in_den_logzeilen_eines_abgleichs(config, welt, caplog):
    with caplog.at_level(logging.DEBUG):
        service.sync(config, client=Abgleich(welt))
        service.sync(config, client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    assert caplog.records
    assert PASSWORT not in caplog.text


def test_stapellauf_meldet_erfolg_und_misserfolg_ueber_den_rueckgabewert(config, welt, monkeypatch):
    monkeypatch.setattr(batch, "sync", lambda _config: service.sync(config, client=Abgleich(welt)))
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))
    assert batch.main() == 0

    monkeypatch.setattr(
        batch,
        "sync",
        lambda _config: service.sync(config, client=Abgleich(fehler=FoundryUnreachable("aus"))),
    )
    assert batch.main() == 1
