import json
import logging
from datetime import UTC, datetime

import pytest
from conftest import (
    GM_FIGUR,
    GM_GEFLUESTER,
    LEITUNG,
    PASSWORT,
    UNBETEILIGTES_KONTO,
    UNSER_KONTO,
    VERWORFENE_ADRESSE,
    WELT_ID,
    WELT_TITEL,
    runde,
)

import chronicle.foundry.__main__ as batch
from chronicle import db, lebenszyklus, notes, settings, zugang
from chronicle import runde as runden
from chronicle.compose import service as compose_service
from chronicle.foundry import permissions, service, store
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
    assert "`/chronicle setup`" in zustand.message
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


def test_auch_ein_abgleich_ohne_server_verbraucht_das_passwort(config):
    """Testwelt und ruhende Runde brechen vor dem Handschlag ab — und ließen die Eingabe
    sonst die vollen zwölf Stunden liegen, für einen Server, den es gar nicht gab."""
    settings.save_foundry_quelle(runde(config), settings.TESTWELT)
    zugang.merken(runde(config), PASSWORT)

    service.sync(config, runde(config))

    assert not zugang.ist_gemerkt(runde(config))


def test_eine_ruhende_runde_laesst_kein_passwort_liegen(config):
    """Der Rauswurf vergisst zwar selbst — die laufende Sitzung kann danach aber noch eines
    hinterlegen, und der Abgleich bricht ab, bevor er es je vorzeigen könnte."""
    runde(config)
    unsere = runden.anlegen(config.database_path, "Ruhend", guild_id="gilde-ruht")
    lebenszyklus.sperren(config.database_path, "gilde-ruht")
    zugang.merken(unsere, PASSWORT)

    service.sync(config, unsere)

    assert not zugang.ist_gemerkt(unsere)


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


# -- Der Ausflug in die Testwelt --------------------------------------------------------


def _archiv(config):
    scope = db.scoped(runde(config))
    try:
        return scope.execute(
            "SELECT id, roll_title, roll_total, vanished_at, aus_testwelt "
            "FROM foundry_message WHERE runde_id = ? ORDER BY id",
            (scope.runde_id,),
        ).fetchall()
    finally:
        scope.close()


def _auf(config, quelle):
    settings.save_foundry_quelle(runde(config), quelle)


def test_die_testwelt_erklaert_das_archiv_der_runde_nicht_fuer_verschwunden(config, welt):
    """Sie war bei keinem Server — sie kann über dessen Chat-Log nichts wissen."""
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)

    _auf(config, settings.TESTWELT)
    service.sync(config, runde(config))

    echt = [zeile for zeile in _archiv(config) if not zeile["aus_testwelt"]]
    assert echt
    assert not [zeile for zeile in echt if zeile["vanished_at"]]


def test_die_testwelt_meldet_ihre_lieferung_und_nicht_das_archiv(config, welt):
    """Sonst zählte der Satz »eingespielt aus der Fixture« die echten Nachrichten mit."""
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)

    _auf(config, settings.TESTWELT)
    zustand = service.sync(config, runde(config))

    aus_der_fixture = [zeile for zeile in _archiv(config) if zeile["aus_testwelt"]]
    assert f"{len(aus_der_fixture)} Chat-Nachrichten" in zustand.message


def test_ein_ausflug_in_die_testwelt_laesst_keinen_wurf_im_archiv(config, welt):
    """Testwelt gespielt, zurückgeschaltet, echt abgeglichen — das Archiv ist wieder das
    der Runde.

    Bliebe ein Fixture-Wurf liegen, trüge er nach dem Rückwechsel den Vermerk »nicht mehr
    vorhanden« und wäre damit von einem echten Wurf nicht mehr zu unterscheiden, den die
    Spielleitung aus dem Chat-Log geräumt hat.
    """
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)
    vorher = {zeile["id"] for zeile in _archiv(config)}

    _auf(config, settings.TESTWELT)
    service.sync(config, runde(config))
    assert len(_archiv(config)) > len(vorher)

    _auf(config, settings.SERVER)
    zustand = service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)

    assert {zeile["id"] for zeile in _archiv(config)} == vorher
    assert not [zeile for zeile in _archiv(config) if zeile["vanished_at"]]
    assert NICHT_MEHR_VORHANDEN not in zustand.message


def test_ein_wurf_der_testwelt_bleibt_keine_belegstelle(config, welt):
    """Was ausgewählt war, geht mit — sonst zeigte eine Szene auf einen Beleg, den es
    nicht mehr gibt, und die Komposition zöge eine erfundene Zahl in die Chronik."""
    _auf(config, settings.TESTWELT)
    service.sync(config, runde(config))

    scope = db.scoped(runde(config))
    try:
        sitzung = scope.execute(
            "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
            (scope.runde_id, "2026-08-05", "Der Keller", "2026-08-05T20:00:00+00:00"),
        ).lastrowid
        szene = scope.execute(
            "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.runde_id, sitzung, 1, "Aufbruch", "2026-08-05T20:00:00+00:00"),
        ).lastrowid
        erfunden = _archiv(config)[0]["id"]
        with scope:
            scope.execute(
                "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) "
                "VALUES (?, ?, ?)",
                (scope.runde_id, szene, erfunden),
            )
        assert compose_service.material(scope, sitzung).scenes[0].facts
    finally:
        scope.close()

    _auf(config, settings.SERVER)
    service.sync(config, runde(config), client=Abgleich(welt), passwort=PASSWORT)

    scope = db.scoped(runde(config))
    try:
        assert not compose_service.material(scope, sitzung).scenes[0].facts
        assert not scope.execute(
            "SELECT 1 FROM scene_foundry_message WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
    finally:
        scope.close()


# -- Der Strom während der Sitzung ------------------------------------------------------


class NurLesen:
    """Ein Foundry, das sich nur lesen lässt.

    Jeder Griff neben ``fetch_world`` fliegt auf: **kein Rückkanal** ist eine Zusage an die
    Runde, deren Welt wir öffnen, und keine Absicht, die man im Kopf behält.
    """

    def __init__(self, welt=None, fehler=None):
        self._welt = welt
        self._fehler = fehler
        self.aufrufe = 0

    def __getattr__(self, name):
        raise AssertionError(f"Der Strom hat nach {name!r} gegriffen — es gibt keinen Rückkanal.")

    def fetch_world(self):
        self.aufrufe += 1
        if self._fehler is not None:
            raise self._fehler
        return UNSER_KONTO, self._welt


def strom(config, welt=None, *, gesehen=frozenset(), fehler=None):
    """Ein Blick des Stroms, mit hinterlegtem Passwort wie nach ``/session start``."""
    zugang.merken(runde(config), PASSWORT, wer="7001")
    return service.beobachten(config, runde(config), gesehen=gesehen, client=NurLesen(welt, fehler))


def test_der_strom_stellt_wuerfe_ein_und_sonst_nichts(config, welt):
    """Was lohnt, entscheidet der Adapter: Zahlen ja, geschriebener Chat nein."""
    ergebnis = strom(config, welt)
    assert [nachricht.id for nachricht in ergebnis.neu] == ["m-wurf"]
    assert ergebnis.grund is None and ergebnis.weiter


def test_der_strom_gibt_kein_gm_geheimnis_in_den_thread(config, welt):
    """Der Server filtert nicht — wir schon, und zwar vor dem Speicher und vor dem Thread.

    Der blinde Wurf der Spielleitung ist ein Wurf und käme ohne diese Filterung durch: im
    Thread läse ihn die ganze Gruppe.
    """
    ergebnis = strom(config, welt)
    ids = [nachricht.id for nachricht in ergebnis.neu]
    assert "m-blind" not in ids
    assert "m-fluester-gm" not in ids
    assert GM_GEFLUESTER not in " ".join(nachricht.content for nachricht in ergebnis.neu)


def test_gesehen_ist_gesehen(config, welt):
    """Jedes Ereignis genau einmal — der zweite Blick stellt nichts noch einmal ein."""
    assert strom(config, welt, gesehen=frozenset({"m-wurf"})).neu == ()


def test_der_strom_verbraucht_das_passwort_nicht(config, welt):
    """Sonst sähe er genau einmal nach. Verlängert wird die Frist dabei aber auch nicht."""
    zugang.merken(runde(config), PASSWORT, wer="7001")
    faellig = zugang._gemerkt[runde(config).id].ablauf

    service.beobachten(config, runde(config), client=NurLesen(welt))

    assert zugang.passwort(runde(config)) == PASSWORT
    assert zugang._gemerkt[runde(config).id].ablauf == faellig


def test_ohne_passwort_endet_der_strom_statt_zu_fragen(config, welt):
    zugang.vergiss(runde(config))
    client = NurLesen(welt)
    ergebnis = service.beobachten(config, runde(config), client=client)
    assert not ergebnis.weiter
    assert ergebnis.grund == service.STROM_OHNE_PASSWORT
    assert client.aufrufe == 0


def test_eine_ruhende_runde_wird_nicht_beobachtet(config, welt):
    runde(config)
    unsere = runden.anlegen(config.database_path, "Ruhend", guild_id="gilde-ruht")
    lebenszyklus.sperren(config.database_path, "gilde-ruht")
    zugang.merken(unsere, PASSWORT, wer="7001")
    client = NurLesen(welt)
    ergebnis = service.beobachten(config, unsere, client=client)
    assert not ergebnis.weiter and client.aufrufe == 0


def test_ein_ausfall_beendet_den_strom_nicht(config, welt):
    """Foundry startet neu, der Abend geht weiter — der nächste Blick lohnt sich."""
    ergebnis = strom(config, fehler=FoundryUnreachable("Verbindung abgelehnt"))
    assert ergebnis.weiter and ergebnis.neu == ()
    assert "Verbindung abgelehnt" in ergebnis.grund


def test_ein_ausfall_des_stroms_vermerkt_keinen_gescheiterten_abgleich(config, welt):
    """Der Strom sieht zu, er führt den Stand nicht — ein Vermerk je Blick wäre Rauschen."""
    service.sync(config, runde(config), passwort=PASSWORT, client=Abgleich(welt))
    strom(config, fehler=FoundryUnreachable("Verbindung abgelehnt"))
    assert not service.failed(config, runde(config))


def test_eine_andere_welt_beendet_den_strom_und_speichert_nichts(config, welt):
    service.sync(config, runde(config), passwort=PASSWORT, client=Abgleich(welt))
    ergebnis = strom(config, andere_welt(welt))
    assert not ergebnis.weiter
    assert "andere Welt" in ergebnis.grund
    assert gebundene_welt(config) == World(id=WELT_ID, title=WELT_TITEL)


def test_der_strom_legt_die_zahlen_gleich_ins_archiv(config, welt):
    """Fällt Foundry vor dem Abschluss aus, sind die Würfe des Abends trotzdem da."""
    strom(config, welt)
    zustand = service.current(config, runde(config))
    assert "m-wurf" in {nachricht.id for nachricht in zustand.snapshot.messages}


def test_kein_passwort_in_den_logzeilen_des_stroms(config, welt, caplog):
    with caplog.at_level(logging.DEBUG):
        strom(config, welt)
        strom(config, fehler=FoundryUnreachable("Verbindung abgelehnt"))
    assert PASSWORT not in caplog.text


# -- Der Nachtrag beim Abschluss (#219) -------------------------------------------------
#
# Der Strom war lange der einzige Schreiber von ``scene_foundry_message``, und nur was dort
# steht, wird in der Chronik zur Tatsache. Ein Abend ohne hinterlegtes Passwort bekam damit
# eine Chronik ohne eine einzige Zahl — obwohl der Abschluss das ganze Chat-Log holte.

SZENE_EINS = "2026-08-05T20:00:00+00:00"
SZENE_ZWEI = "2026-08-05T21:00:00+00:00"
FRUEHERER_ABEND = "2026-07-29T20:30:00+00:00"


def _ms(zeitpunkt: str) -> int:
    """Ein Zeitpunkt, wie Foundry ihn stempelt: Millisekunden seit der Epoche, UTC."""
    return int(datetime.fromisoformat(zeitpunkt).timestamp() * 1000)


def _wurf(kennung, zeitpunkt, **rest):
    return {
        "_id": kennung,
        "timestamp": _ms(zeitpunkt),
        "author": LEITUNG,
        "content": "",
        "speaker": {"actor": "a-brok", "alias": "Brok Eisenfaust"},
        "system": {"roll": {"title": "Wurf", "total": 7, "formula": "1d12"}},
        **rest,
    }


def _abendwelt(welt, nachrichten):
    """Dieselbe Welt, aber mit dem Chronisten als Spielleitung — der Fall aus #78.

    Nur mit einem GM-Zugang stehen ein Geflüster und ein blinder Wurf überhaupt im Archiv;
    ein Spielerkonto verliert sie schon vor dem Speicher, und der Nachtrag käme nie in die
    Lage, sie durchzulassen. Genau deshalb ist das hier die interessante Welt.
    """
    konten = [
        dict(konto, role=4) if konto["_id"] == UNSER_KONTO else konto for konto in welt["users"]
    ]
    return dict(welt, users=konten, messages=nachrichten)


def _sitzung(config, eine, szenen, *, played_on="2026-08-05", titel="Der Keller"):
    """Eine Sitzung mit Trennlinien zu festen Zeitpunkten."""
    scope = db.scoped(eine)
    try:
        with scope:
            sitzung = int(
                scope.execute(
                    "INSERT INTO session (runde_id, played_on, title, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (scope.runde_id, played_on, titel, szenen[0][1]),
                ).lastrowid
            )
            for position, (name, zeitpunkt) in enumerate(szenen, start=1):
                scope.execute(
                    "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (scope.runde_id, sitzung, position, name, zeitpunkt),
                )
    finally:
        scope.close()
    return sitzung


def _szenen_ids(eine, sitzung):
    return [szene.id for szene in notes.session(eine, sitzung).scenes]


def _fakten(eine, sitzung):
    """Was in der Chronik dieser Sitzung als Tatsache steht — je Szenenposition."""
    scope = db.scoped(eine)
    try:
        stoff = compose_service.material(scope, sitzung)
    finally:
        scope.close()
    return {szene.position: [fakt.id for fakt in szene.facts] for szene in stoff.scenes}


DER_ABEND = (
    _wurf("w-offen-1", "2026-08-05T20:30:00+00:00"),
    _wurf("w-fluester", "2026-08-05T20:40:00+00:00", whisper=[UNSER_KONTO]),
    _wurf("w-blind", "2026-08-05T21:10:00+00:00", blind=True),
    _wurf("w-offen-2", "2026-08-05T21:20:00+00:00"),
)


def _abschluss_abgleich(config, welt, sitzung, eine=None):
    """Der Abgleich, wie der Abschluss ihn macht — mit der Sitzung im Gepäck."""
    return service.sync(
        config,
        eine or runde(config),
        client=Abgleich(welt),
        passwort=PASSWORT,
        session_id=sitzung,
    )


def test_foundrys_zeitstempel_wird_als_millisekunde_in_utc_gelesen():
    """Die Einheit einmal festgenagelt: ``Date.now()``, nicht Sekunden, nicht Ortszeit.

    Still danebenzugreifen hieße hier, jeden Wurf in die falsche Szene zu hängen — und
    zwar ohne Fehler, weil SQLite eine Zahl und einen Text klaglos vergleicht.
    """
    assert service._augenblick(946684800000) == "2000-01-01T00:00:00+00:00"
    assert service._augenblick(_ms(SZENE_ZWEI)) == SZENE_ZWEI


def test_der_abschluss_traegt_die_zahlen_nach(config, welt):
    """Der Befund aus #219: ohne Strom trug die Chronik keine einzige Zahl."""
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])
    assert _fakten(runde(config), sitzung) == {1: [], 2: []}

    zustand = _abschluss_abgleich(config, _abendwelt(welt, DER_ABEND), sitzung)

    assert not zustand.stale and zustand.nachgetragen == 2
    assert _fakten(runde(config), sitzung) == {1: ["w-offen-1"], 2: ["w-offen-2"]}


def test_der_nachtrag_bringt_kein_gm_geheimnis_in_die_chronik(config, welt):
    """Dieselbe engere Sicht wie im Strom — sonst käme sie hier durch die Hintertür.

    Beides steht mit einem GM-Zugang sehr wohl im Archiv; die Chronik liest die ganze
    Runde, und für sie war weder das Geflüster noch der blinde Wurf bestimmt.
    """
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])
    _abschluss_abgleich(config, _abendwelt(welt, DER_ABEND), sitzung)

    verknuepft = set().union(*_fakten(runde(config), sitzung).values())
    assert "w-fluester" not in verknuepft
    assert "w-blind" not in verknuepft
    # Und das Archiv bleibt trotzdem voll — eingeengt ist der Weg in die Chronik.
    archiviert = {zeile["id"] for zeile in _archiv(config)}
    assert {"w-fluester", "w-blind"} <= archiviert


def test_die_gegenprobe_mit_der_weiteren_sicht_faellt_durch(config, welt, monkeypatch):
    """Ohne die engere Frage stünden Geflüster und blinder Wurf in der Chronik.

    Der Test darüber wäre sonst grün, ohne etwas zu halten: er soll rot werden, wenn
    jemand ``message_visible`` einsetzt — die Frage »darf **dieses Konto** das sehen«.
    """
    monkeypatch.setattr(
        service,
        "fuer_die_gruppe",
        lambda raw: frozenset(
            nachricht["_id"]
            for nachricht in raw["messages"]
            if permissions.message_visible(nachricht, UNSER_KONTO, True)
        ),
    )
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])
    _abschluss_abgleich(config, _abendwelt(welt, DER_ABEND), sitzung)

    verknuepft = set().union(*_fakten(runde(config), sitzung).values())
    assert {"w-fluester", "w-blind"} <= verknuepft


def test_der_nachtrag_haengt_nichts_ein_zweites_mal(config, welt):
    """Lief der Strom, hängen die Würfe schon — an der Szene, die lief, als sie fielen.

    Ein zweites Anhängen wäre kein Doppeleintrag, den der Schlüssel fängt, sondern
    dieselbe Zahl in zwei Szenen: der Schlüssel ist Szene **und** Nachricht.
    """
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])
    zweite = _szenen_ids(runde(config), sitzung)[1]
    # Wie der Strom es tut: an die Szene, die beim Blick gerade lief.
    notes.link_foundry_message(runde(config), zweite, "w-offen-1")

    zustand = _abschluss_abgleich(config, _abendwelt(welt, DER_ABEND), sitzung)

    assert zustand.nachgetragen == 1
    assert _fakten(runde(config), sitzung) == {1: [], 2: ["w-offen-1", "w-offen-2"]}


def test_ein_zweiter_abschluss_traegt_nichts_dazu(config, welt):
    """Zweimal auslösen ist keine zweite Zuordnung."""
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])
    _abschluss_abgleich(config, _abendwelt(welt, DER_ABEND), sitzung)
    vorher = _fakten(runde(config), sitzung)

    zustand = _abschluss_abgleich(config, _abendwelt(welt, DER_ABEND), sitzung)

    assert zustand.nachgetragen == 0
    assert _fakten(runde(config), sitzung) == vorher


def test_die_wuerfe_fremder_abende_bleiben_drau_en(config, welt):
    """Das Chat-Log trägt die ganze Kampagne — nicht nur diesen Abend.

    Eine Auffanglinie wie in ``scene_at`` zöge jeden früheren Wurf in die erste Szene
    dieser Sitzung, und der nächste Abend hinge an ihrer letzten. Beides wäre eine
    Zuordnung, die niemand je gesehen hat.
    """
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])
    _sitzung(
        config,
        runde(config),
        [("Weiter", "2026-08-12T20:00:00+00:00")],
        played_on="2026-08-12",
        titel="Der Turm",
    )
    nachrichten = (
        _wurf("w-vorher", FRUEHERER_ABEND),
        *DER_ABEND,
        _wurf("w-danach", "2026-08-12T20:30:00+00:00"),
    )

    zustand = _abschluss_abgleich(config, _abendwelt(welt, nachrichten), sitzung)

    assert zustand.nachgetragen == 2
    assert _fakten(runde(config), sitzung) == {1: ["w-offen-1"], 2: ["w-offen-2"]}


def test_strom_und_nachtrag_ordnen_denselben_wurf_derselben_szene_zu(config, welt):
    """Ein Abend **mit** Strom sieht aus wie einer ohne — geprüft an beiden Wegen.

    Gefallen sind die Würfe in der laufenden Szene; der Strom hängt sie an die Szene von
    jetzt, der Nachtrag an die ihres Zeitstempels. Kommen dabei zwei verschiedene Chroniken
    heraus, ist eine davon falsch.
    """
    laufend = datetime.now(UTC).isoformat(timespec="seconds")
    jetzige = tuple(
        _wurf(kennung, laufend, **rest)
        for kennung, rest in (
            ("w-offen-1", {}),
            ("w-fluester", {"whisper": [UNSER_KONTO]}),
            ("w-blind", {"blind": True}),
            ("w-offen-2", {}),
        )
    )
    abend = _abendwelt(welt, jetzige)
    szenen = [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)]

    mit_strom = runden.anlegen(config.database_path, "Mit Strom", guild_id="gilde-strom")
    a = _sitzung(config, mit_strom, szenen)
    zugang.merken(mit_strom, PASSWORT, wer="7001")
    ergebnis = service.beobachten(config, mit_strom, client=NurLesen(abend))
    # Was ``bot.chronik`` mit dem Ergebnis tut: an die Szene von jetzt.
    szene_jetzt = notes.scene_at(mit_strom, a, datetime.now(UTC).isoformat(timespec="seconds"))
    for nachricht in ergebnis.neu:
        notes.link_foundry_message(mit_strom, szene_jetzt, nachricht.id)
    service.sync(config, mit_strom, client=Abgleich(abend), passwort=PASSWORT, session_id=a)

    ohne_strom = runden.anlegen(config.database_path, "Ohne Strom", guild_id="gilde-ohne")
    b = _sitzung(config, ohne_strom, szenen)
    _abschluss_abgleich(config, abend, b, eine=ohne_strom)

    assert _fakten(mit_strom, a) == {1: [], 2: ["w-offen-1", "w-offen-2"]}
    assert _fakten(ohne_strom, b) == _fakten(mit_strom, a)


def test_ohne_passwort_traegt_der_abschluss_nichts_nach(config, welt):
    """Der Abgleich kommt nicht durch — dann kommt auch keine Zahl, und das ist richtig."""
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])

    # Ohne ``client`` geht der echte Weg: kein Passwort im Speicher, kein Handschlag.
    zustand = service.sync(config, runde(config), session_id=sitzung)

    assert zustand.stale and zustand.nachgetragen == 0
    assert "fehlt das Foundry-Passwort" in zustand.message
    assert _fakten(runde(config), sitzung) == {1: [], 2: []}


def test_ein_abgleich_ohne_sitzung_ordnet_nichts_zu(config, welt):
    """Ein Abgleich für sich hat keine Sitzung — die Zuordnung ist keine Nebenwirkung."""
    sitzung = _sitzung(config, runde(config), [("Aufbruch", SZENE_EINS), ("Keller", SZENE_ZWEI)])

    zustand = service.sync(
        config, runde(config), client=Abgleich(_abendwelt(welt, DER_ABEND)), passwort=PASSWORT
    )

    assert zustand.nachgetragen == 0
    assert _fakten(runde(config), sitzung) == {1: [], 2: []}
