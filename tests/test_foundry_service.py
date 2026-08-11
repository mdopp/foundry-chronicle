import json
import logging

import pytest
from conftest import (
    GM_FIGUR,
    GM_GEFLUESTER,
    PASSWORT,
    UNBETEILIGTES_KONTO,
    UNSER_KONTO,
    VERWORFENE_ADRESSE,
    WELT_ID,
    WELT_TITEL,
    runde,
)

import chronicle.foundry.__main__ as batch
from chronicle import db, zugang
from chronicle import runde as runden
from chronicle.foundry import service, store
from chronicle.foundry.client import FoundryUnreachable
from chronicle.foundry.model import NICHT_MEHR_VORHANDEN, World


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


def andere_welt(welt, *, id="der-tiefe-schacht", titel="Der tiefe Schacht"):
    return dict(welt, world={"id": id, "title": titel})


def gebundene_welt(config):
    from chronicle import db

    scope = db.scoped(runde(config))
    try:
        return store.world(scope)
    finally:
        scope.close()


def gespeicherter_text(config):
    pfad = config.database_path
    roh = b""
    for datei in (pfad, pfad.with_suffix(".sqlite3-wal")):
        if datei.exists():
            roh += datei.read_bytes()
    return roh.decode("utf-8", errors="ignore")


def test_erfolgreicher_abgleich_meldet_den_umfang(config, welt):
    zustand = service.sync(config, runde(config), client=Abgleich(welt))
    assert not zustand.stale
    assert zustand.snapshot.system == "daggerheart"
    assert "2 Spieler" in zustand.message
    assert "3 Charaktere" in zustand.message
    assert "3 Chat-Nachrichten" in zustand.message


def test_der_zwischenspeicher_ueberlebt_den_prozess(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt))
    zustand = service.current(config, runde(config))
    assert not zustand.stale
    assert len(zustand.snapshot.messages) == 3


def test_gefiltert_wird_vor_dem_zwischenspeicher(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt))
    inhalt = gespeicherter_text(config)
    assert "Brok Eisenfaust" in inhalt
    assert GM_FIGUR not in inhalt
    assert GM_GEFLUESTER not in inhalt
    assert UNBETEILIGTES_KONTO not in inhalt
    assert VERWORFENE_ADRESSE not in inhalt
    assert "Der Keller unter dem Krummen Ast" not in inhalt


def test_ins_archiv_kommt_nur_was_den_filter_passiert_hat(config, welt):
    """Nachrichten werden gesammelt statt gespiegelt — Gefiltertes sammelt sich trotzdem nie."""
    service.sync(config, runde(config), client=Abgleich(welt))
    service.sync(config, runde(config), client=Abgleich(welt))
    zustand = service.current(config, runde(config))
    assert {n.id for n in zustand.snapshot.messages} == {
        "m-aufbruch",
        "m-wurf",
        "m-fluester-an-uns",
    }
    assert GM_GEFLUESTER not in gespeicherter_text(config)


def test_verschwundene_nachrichten_stehen_in_der_meldung(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt))
    zustand = service.sync(config, runde(config), client=Abgleich(dict(welt, messages=[])))
    assert "3 Chat-Nachrichten" in zustand.message
    assert f"davon 3 {NICHT_MEHR_VORHANDEN}" in zustand.message


def test_ohne_abgleich_gibt_es_eine_erklaerung_statt_einer_leeren_liste(config):
    zustand = service.current(config, runde(config))
    assert zustand.snapshot is None
    assert "Noch kein Abgleich" in zustand.message


def test_foundry_aus_und_noch_kein_stand_erklaert_das(config):
    zustand = service.sync(
        config, runde(config), client=Abgleich(fehler=FoundryUnreachable("keine Antwort"))
    )
    assert zustand.stale
    assert zustand.snapshot is None
    assert "nicht erreichbar" in zustand.message
    assert "keine Antwort" in zustand.message


def test_foundry_aus_liefert_den_letzten_stand_plus_meldung(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt))
    zustand = service.sync(
        config, runde(config), client=Abgleich(fehler=FoundryUnreachable("keine Antwort"))
    )
    assert zustand.stale
    assert len(zustand.snapshot.messages) == 3
    assert "nicht erreichbar" in zustand.message
    assert "Angezeigt wird der Stand" in zustand.message


def test_die_meldung_ueberlebt_bis_zum_naechsten_aufruf(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt))
    service.sync(config, runde(config), client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    assert service.current(config, runde(config)).stale


def test_ein_gelungener_abgleich_raeumt_die_meldung_weg(config, welt):
    service.sync(config, runde(config), client=Abgleich(fehler=FoundryUnreachable("keine Antwort")))
    service.sync(config, runde(config), client=Abgleich(welt))
    assert not service.current(config, runde(config)).stale


def test_ohne_konfiguration_meldet_der_abgleich_das_verstaendlich(tmp_path):
    from chronicle.config import Config

    leer = Config(data_dir=tmp_path)
    zustand = service.sync(leer, runde(leer), passwort=PASSWORT)
    assert zustand.stale
    assert "die Adresse und der Benutzer" in zustand.message
    assert "in den Einstellungen" in zustand.message
    assert "FOUNDRY_" not in zustand.message


def test_die_meldung_nennt_nur_das_wirklich_fehlende(tmp_path):
    from chronicle.config import Config

    halb = Config(data_dir=tmp_path, foundry_url="https://foundry.example")
    zustand = service.sync(halb, runde(halb), passwort=PASSWORT)
    assert "fehlt noch der Benutzer" in zustand.message
    assert "Adresse" not in zustand.message


def test_ohne_passwort_wird_gar_nicht_erst_verbunden(config):
    """Es steht nirgends — also fragt der Abgleich danach, statt es zu suchen."""
    zustand = service.sync(config, runde(config))
    assert zustand.stale
    assert "nirgends gespeichert" in zustand.message


def test_das_gemerkte_passwort_traegt_den_abgleich(config, welt, monkeypatch):
    gesehen = {}

    def durchgereicht(_config, passwort, **_kwargs):
        gesehen["passwort"] = passwort
        return Abgleich(welt)

    monkeypatch.setattr(service, "FoundryClient", durchgereicht)
    zugang.merken(runde(config), PASSWORT)
    zustand = service.sync(config, runde(config))
    assert not zustand.stale
    assert gesehen["passwort"] == PASSWORT


def test_ein_abgleich_verbraucht_das_passwort(config, welt):
    """Auch der gescheiterte: der nächste Versuch fragt neu, statt eines liegen zu lassen."""
    zugang.merken(runde(config), PASSWORT)
    service.sync(config, runde(config), client=Abgleich(welt))
    assert not zugang.ist_gemerkt(runde(config))

    zugang.merken(runde(config), PASSWORT)
    service.sync(config, runde(config), client=Abgleich(fehler=FoundryUnreachable("aus")))
    assert not zugang.ist_gemerkt(runde(config))


def test_das_passwort_landet_in_keiner_zeile_der_datenbank(config, welt):
    zugang.merken(runde(config), PASSWORT)
    service.sync(config, runde(config), client=Abgleich(welt))
    assert PASSWORT not in gespeicherter_text(config)


def test_der_erste_abgleich_bindet_die_runde_an_die_welt(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
    assert gebundene_welt(config) == World(id=WELT_ID, title=WELT_TITEL)


def test_dieselbe_welt_gleicht_weiter_ab(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
    zustand = service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
    assert not zustand.stale


def test_eine_andere_welt_wird_verweigert(config, welt):
    """Ein Server hostet eine Welt: der Wechsel zöge sonst die falsche Kampagne herein."""
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
    zustand = service.sync(
        config, runde(config), client=Abgleich(andere_welt(welt)), passwort=PASSWORT
    )
    assert zustand.stale
    assert "andere Welt" in zustand.message
    assert WELT_TITEL in zustand.message
    assert "Der tiefe Schacht" in zustand.message
    # Nichts übernommen: der alte Stand steht unverändert, die Bindung auch.
    assert zustand.snapshot.messages
    assert gebundene_welt(config).id == WELT_ID
    assert service.current(config, runde(config)).stale


def test_umhaengen_bindet_die_runde_ausdruecklich_um(config, welt):
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
    zustand = service.sync(
        config,
        runde(config),
        client=Abgleich(andere_welt(welt)),
        passwort=PASSWORT,
        umhaengen=True,
    )
    assert not zustand.stale
    assert gebundene_welt(config).id == "der-tiefe-schacht"


def test_eine_welt_ohne_kennung_haelt_den_abgleich_nicht_auf(config, welt):
    """Ältere Stände tragen keinen ``world``-Block — ohne Beleg wird nicht verweigert."""
    ohne = {name: wert for name, wert in welt.items() if name != "world"}
    service.sync(config, runde(config), client=Abgleich(ohne), passwort=PASSWORT)
    zustand = service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
    assert not zustand.stale
    assert gebundene_welt(config).id == WELT_ID


def test_kein_passwort_in_den_logzeilen_eines_abgleichs(config, welt, caplog):
    with caplog.at_level(logging.DEBUG):
        service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
        service.sync(
            config,
            runde(config),
            client=Abgleich(fehler=FoundryUnreachable("keine Antwort")),
            passwort=PASSWORT,
        )
        service.sync(config, runde(config), client=Abgleich(andere_welt(welt)), passwort=PASSWORT)
    assert caplog.records
    assert PASSWORT not in caplog.text


def test_stapellauf_fragt_nach_dem_passwort_und_meldet_ueber_den_rueckgabewert(
    config, welt, monkeypatch
):
    """Der Stapellauf liest das Passwort nirgends — er fragt danach."""
    gefragt = []
    monkeypatch.setattr(batch, "getpass", lambda frage: gefragt.append(frage) or PASSWORT)
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))

    monkeypatch.setattr(
        batch,
        "sync",
        lambda _config, _runde, passwort: service.sync(
            config, runde(config), client=Abgleich(welt), passwort=passwort
        ),
    )
    assert batch.main([]) == 0
    assert gefragt == [batch.FRAGE]

    monkeypatch.setattr(
        batch,
        "sync",
        lambda _config, _runde, passwort: service.sync(
            config, runde(config), client=Abgleich(fehler=FoundryUnreachable("aus"))
        ),
    )
    assert batch.main([]) == 1


def test_der_abzug_schreibt_die_rohe_welt_und_warnt(config, welt, tmp_path):
    """Der Abzug geht weder durch den Filter noch in die Datenbank — das ist sein Zweck."""
    ziel = tmp_path / "abzug" / "welt-dump.json"
    meldung = service.abzug(config, runde(config), ziel, passwort=PASSWORT, client=Abgleich(welt))
    assert json.loads(ziel.read_text(encoding="utf-8")) == welt
    assert GM_GEFLUESTER in ziel.read_text(encoding="utf-8")
    assert "nie ins Repo" in meldung
    assert "anonymisiere_welt.py" in meldung
    assert service.current(config, runde(config)).snapshot is None


def test_der_abzug_verbraucht_das_gemerkte_passwort(config, welt, tmp_path):
    zugang.merken(runde(config), PASSWORT)
    service.abzug(config, runde(config), tmp_path / "welt-dump.json", client=Abgleich(welt))
    assert zugang.passwort(runde(config)) is None


def test_der_stapellauf_kann_abgreifen(config, welt, tmp_path, monkeypatch):
    """Das Ziel ist nicht wählbar: der Abzug landet immer im gitignorierten Ordner.

    Ein freier Pfad war die Restlücke — ``--dump abzug.json`` hätte den Abzug neben den
    Quelltext gelegt, wo ihn weder ``welt-dump*.json`` noch ``dumps/`` gefangen hätte.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(batch, "getpass", lambda frage: PASSWORT)
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "FoundryClient", lambda _config, _passwort: Abgleich(welt))
    assert batch.main(["--dump"]) == 0
    ziel = tmp_path / service.ABZUG_ZIEL
    assert json.loads(ziel.read_text(encoding="utf-8")) == welt
    # Klarnamen liegen nicht für alle lesbar herum.
    assert ziel.stat().st_mode & 0o077 == 0

    monkeypatch.setattr(
        service,
        "FoundryClient",
        lambda _config, _passwort: Abgleich(fehler=FoundryUnreachable("aus")),
    )
    assert batch.main(["--dump"]) == 1


def test_ein_vorhandener_ordner_wird_nachgezogen(config, welt, tmp_path):
    """``mkdir(mode=…)`` greift nur beim Anlegen — ein schon offener Ordner bliebe offen."""
    ordner = tmp_path / "dumps"
    ordner.mkdir(mode=0o755)
    ordner.chmod(0o755)
    service.abzug(
        config, runde(config), ordner / "welt-dump.json", passwort=PASSWORT, client=Abgleich(welt)
    )
    assert ordner.stat().st_mode & 0o077 == 0


def test_der_stapellauf_verlangt_die_runde_sobald_es_mehrere_gibt(config, monkeypatch, capsys):
    """Sonst zöge ``--dump`` still den ungefilterten Abzug der ersten Runde."""
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))
    zweite = runden.anlegen(config.database_path, "Die Zweiten")
    assert batch.main(["--dump"]) == 1
    assert "mehrere Runden" in capsys.readouterr().err

    assert batch.main([f"--runde={zweite.id + 99}"]) == 1
    assert "Keine Runde mit der Kennung" in capsys.readouterr().err


def test_eine_unbekannte_kennung_legt_keine_runde_an(config, monkeypatch, capsys):
    """``erste`` legt bei leerer Tabelle eine an — der Fehlweg darf nichts hinterlassen."""
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))
    db.init(config.database_path)
    vorher = runden.alle(config.database_path)

    assert batch.main(["--runde=999", "--dump"]) == 1

    assert "Keine Runde mit der Kennung" in capsys.readouterr().err
    assert [runde.id for runde in runden.alle(config.database_path)] == [
        runde.id for runde in vorher
    ]


def test_der_stapellauf_sagt_welche_runde_er_nimmt(config, welt, tmp_path, monkeypatch, capsys):
    zweite = runden.anlegen(config.database_path, "Die Zweiten")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(batch, "getpass", lambda frage: PASSWORT)
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "FoundryClient", lambda _config, _passwort: Abgleich(welt))
    assert batch.main([f"--runde={zweite.id}", "--dump"]) == 0
    assert f"Runde {zweite.id}: Die Zweiten" in capsys.readouterr().out


def test_ein_freier_pfad_ist_kein_argument_mehr(config, monkeypatch):
    monkeypatch.setattr(batch.Config, "from_env", classmethod(lambda cls: config))
    with pytest.raises(SystemExit):
        batch.main(["--dump", "abzug.json"])


def test_kein_passwort_in_den_logzeilen_eines_abzugs(config, welt, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        service.abzug(
            config,
            runde(config),
            tmp_path / "welt-dump.json",
            passwort=PASSWORT,
            client=Abgleich(welt),
        )
    assert PASSWORT not in caplog.text
