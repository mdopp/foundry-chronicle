"""Die eingebaute Testwelt: dieselbe Strecke, kein Netz — und unübersehbar gekennzeichnet.

Zwei Dinge werden hier geprüft, und das zweite ist das wichtigere. Erstens: die Fixture
läuft wirklich durch Berechtigungsfilter, System-Adapter und Zwischenspeicher, nicht an
ihnen vorbei. Zweitens: solange sie aktiv ist, sagt die Oberfläche das auf jeder Seite —
eine Verwechslung von erfundenen mit echten Kampagnendaten ist das Hauptrisiko dieses
Schalters, und dagegen hilft nur, es überall hinzuschreiben.

Dazu kommen zwei Dauergates auf die eingecheckte Datei selbst. Sie muss Zeichen für
Zeichen aus ``scripts/erzeuge_testwelt.py`` herausfallen — das ist der Beleg für die
Zusage »erzeugt, nie abgegriffen«, und ohne ihn wäre sie nur eine Behauptung im
Docstring. Und sie darf nur die Felder tragen, die der Anonymisierer durchlässt; ein Feld
mehr wäre ein Feld, das niemand geprüft hat.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import PASSWORT, UNSER_KONTO, runde

from chronicle import db, settings
from chronicle.app import create_app
from chronicle.bot import chronik, einrichten
from chronicle.config import Config
from chronicle.foundry import permissions, service, store, testwelt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import anonymisiere_welt as anonym  # noqa: E402
import erzeuge_testwelt  # noqa: E402

KOPF = {"Remote-User": "erzaehlerin"}

# Die erlaubten Felder werden nicht abgeschrieben, sondern aus den Bauplänen des
# Anonymisierers gelesen: sonst wüchsen die beiden Listen auseinander, und die Fixture
# dürfte etwas tragen, was das Werkzeug längst wegwirft.
FIXTURE_PLAN = {
    "userId": anonym.WERT,
    "world": {feld: anonym.WERT for feld in anonym.WELT_FELDER},
    "system": {feld: anonym.WERT for feld in anonym.SYSTEM_FELDER},
    "users": [{feld: anonym.WERT for feld in (*anonym.BENUTZER_FELDER, "name")}],
    "actors": [anonym.FIGUR_PLAN | {"name": anonym.WERT}],
    "scenes": [anonym.SZENEN_PLAN | {"name": anonym.WERT, "navName": anonym.WERT}],
    "messages": [
        anonym.NACHRICHT_PLAN
        | {
            "content": anonym.WERT,
            "speaker": anonym.SPRECHER_PLAN | {"alias": anonym.WERT},
            "system": {"roll": anonym.WURF_PLAN},
            "rolls": [anonym.WERT],
        }
    ],
}


def nach_plan(wert, plan, pfad=""):
    """Kein Schlüssel, den der Bauplan nicht nennt — auf jeder Ebene, nicht nur oben."""
    if plan is anonym.WERT or plan is anonym.STUFEN:
        return
    if isinstance(plan, list):
        for stelle, eintrag in enumerate(wert):
            nach_plan(eintrag, plan[0], f"{pfad}[{stelle}]")
        return
    assert set(wert) <= set(plan), f"{pfad}: {set(wert) - set(plan)}"
    for feld, inhalt in wert.items():
        nach_plan(inhalt, plan[feld], f"{pfad}.{feld}")


class NiemalsAnrufen:
    def fetch_world(self):
        raise AssertionError("Die Testwelt darf keinen Handschlag machen.")


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(data_dir=tmp_path, recordings_dir=tmp_path / "spuren")
    db.init(gesetzt.database_path)
    return gesetzt


@pytest.fixture
def auf_testwelt(config):
    settings.save_foundry_quelle(runde(config), settings.TESTWELT)
    return config


def geladen(config):
    scope = db.scoped(runde(config))
    try:
        return store.load(scope)
    finally:
        scope.close()


def test_die_fixture_ist_erzeugt_und_nicht_abgegriffen(tmp_path, monkeypatch):
    """Das Gate hinter der Zusage: die Datei fällt Zeichen für Zeichen aus dem Skript.

    Ohne diesen Test wäre »erzeugt, nie abgegriffen« eine Behauptung im Docstring. Fällt
    er, ist entweder das Skript geändert worden, ohne die Fixture neu zu schreiben — oder
    die Fixture kommt woandersher, und das ist der Fall, für den es dieses Gate gibt.
    """
    nachgebaut = tmp_path / "testwelt.json"
    monkeypatch.setattr(erzeuge_testwelt, "ZIEL", nachgebaut)
    assert erzeuge_testwelt.main() == 0
    assert nachgebaut.read_text(encoding="utf-8") == testwelt.DATEI.read_text(encoding="utf-8")


def test_die_fixture_traegt_nur_gepruefte_felder():
    """Was hier liegt, ist genau das, was der Anonymisierer durchlässt — nichts sonst."""
    raw = testwelt.welt()
    nach_plan(raw, FIXTURE_PLAN)
    for nachricht in raw["messages"]:
        for stelle, wurf in enumerate(nachricht.get("rolls") or []):
            nach_plan(json.loads(wurf), anonym.WURF_JSON_PLAN, f"rolls[{stelle}]")


def test_in_der_fixture_steht_kein_personenbezug():
    """Dieselbe Prüfung, die der Anonymisierer fährt — hier gegen die eingecheckte Datei."""
    assert anonym.pruefe(testwelt.welt(), ()) == []


def test_in_der_fixture_steht_kein_gesprochener_satz():
    """Nachrichtentexte sind erfunden oder leer — der Wortlaut echter Menschen bleibt draußen."""
    for nachricht in testwelt.welt()["messages"]:
        assert nachricht["content"] in ("", erzeuge_testwelt.ERSATZTEXT)


def test_die_fixture_hat_den_umfang_einer_echten_welt():
    raw = testwelt.welt()
    assert len(raw["users"]) >= 5
    assert len(raw["actors"]) >= 20
    # Acht Nachrichten sind wenig, und mehr wären hier auch nur mehr vom Gleichen: sieben
    # davon tragen einen Wurf, und darum geht es — dass der Adapter etwas zu lesen hat.
    assert len(raw["messages"]) >= 5
    stufen = {permissions.effective_level(figur.get("ownership"), None) for figur in raw["actors"]}
    # Ohne die harten Fälle prüfte die Testwelt den Filter nicht: NONE muss verschwinden,
    # LIMITED darf nur den Namen hergeben.
    assert permissions.NONE in stufen
    assert stufen & {permissions.LIMITED, permissions.OBSERVER}


def test_die_fixture_traegt_karten_die_nur_die_leitung_kennt():
    """Ohne sie bewiese die Filterung an den Szenen nichts — und der Vorgriff bliebe offen."""
    raw = testwelt.welt()
    konto = testwelt.konto(raw)
    stufen = [permissions.effective_level(szene.get("ownership"), konto) for szene in raw["scenes"]]
    assert stufen.count(permissions.NONE) == 18
    assert [szene["active"] for szene in raw["scenes"]].count(True) == 1
    # Beide Formen des Ortsnamens kommen vor: mit Kartenleiste und ohne.
    assert any(szene["navName"] for szene in raw["scenes"])
    assert any(not szene["navName"] for szene in raw["scenes"])


def test_die_fixture_traegt_leitungs_inhalt_den_der_filter_wegnehmen_muss():
    """Eine geflüsterte und eine blinde Nachricht — sonst bewiese der Filter hier nichts."""
    raw = testwelt.welt()
    konto = testwelt.konto(raw)
    assert any(n["whisper"] and konto not in n["whisper"] for n in raw["messages"])
    assert any(n["blind"] for n in raw["messages"])
    sichtbar = [n for n in raw["messages"] if permissions.message_visible(n, konto, gm=False)]
    # Festgenagelt statt „weniger als alle": ein Filter, der nichts wegnimmt, käme durch
    # eine Ungleichung durch — und genau das soll dieser Test verhindern.
    assert len(raw["messages"]) == 8
    assert len(sichtbar) == 6


def test_die_wuerfe_stehen_da_wo_die_doku_sie_nennt():
    """`docs/foundry-zugriff.md` nennt ``message.system.roll`` als Ort der Zahlen.

    Eine frühere Fassung dieser Fixture trug den Block nicht — daraus entstand der
    Verdacht, der Adapter lese aus einer echten Welt gar nichts. Die Auszählung eines
    echten Abzugs hat ihn widerlegt: dort tragen sieben von acht Nachrichten den Block,
    und die erzeugte Fixture bildet das nach. Fällt der Test, hat sich entweder die
    Fixture oder Foundry verändert — beides gehört nachgelesen, bevor der Adapter
    angefasst wird.
    """
    raw = testwelt.welt()
    mit_wurf = [n for n in raw["messages"] if n.get("rolls")]
    assert mit_wurf, "ohne Würfe prüfte diese Welt den Adapter gar nicht"
    mit_block = [
        n for n in raw["messages"] if isinstance(n.get("system"), dict) and n["system"].get("roll")
    ]
    assert len(mit_block) == len(raw["messages"]) - 1
    rechnung = mit_block[0]["system"]["roll"]
    assert rechnung.get("total") is not None
    assert rechnung.get("formula")
    assert rechnung["hope"]["value"] and rechnung["fear"]["value"]


def test_das_konto_ist_kein_leitungskonto():
    raw = testwelt.welt()
    gewaehlt = testwelt.konto(raw)
    benutzer = {str(eintrag["_id"]): eintrag for eintrag in raw["users"]}
    assert gewaehlt in benutzer
    assert not permissions.is_gm(benutzer[gewaehlt])
    assert testwelt.konto(raw) == gewaehlt


def test_der_abgleich_laeuft_ohne_netz_durch_dieselbe_strecke(auf_testwelt):
    config = auf_testwelt
    zustand = service.sync(config, runde(config), client=NiemalsAnrufen())
    assert not zustand.stale
    assert testwelt.HINWEIS in zustand.message
    assert "kein Server im Spiel" in zustand.message

    raw = testwelt.welt()
    stand = geladen(config)
    assert stand.system == str(raw["system"]["id"])
    # Der Filter hat wirklich gefiltert: unsichtbare Figuren fehlen, limitierte tragen
    # nur ihren Namen. Die Zahlen stehen ausgeschrieben da, weil eine Ungleichung auch
    # ein Filter erfüllt, der gar nichts wegnimmt.
    assert (len(raw["actors"]), len(raw["messages"])) == (91, 8)
    assert len(stand.characters) == 52
    limitiert = [figur for figur in stand.characters if figur.limited]
    assert len(limitiert) == 35
    assert all(figur.type is None and figur.owner_ids == () for figur in limitiert)
    assert len(stand.messages) == 6
    assert stand.players
    # Und dieselbe Rechnung für die Karten: von 32 bleiben die 8 der eigenen Gruppe und die
    # 6 offenen; die 10 der Leitung und die 8 der Nachbargruppe sind Vorgriffe und fallen.
    assert len(raw["scenes"]) == 32
    assert len(stand.scenes) == 14
    assert len([szene for szene in stand.scenes if szene.active]) == 1


def test_die_testwelt_braucht_kein_passwort(auf_testwelt):
    """Kein gemerktes Passwort, kein Handschlag — und trotzdem ein Stand."""
    config = auf_testwelt
    zustand = service.sync(config, runde(config))
    assert not zustand.stale
    assert zustand.snapshot is not None


def test_umschalten_ersetzt_den_zwischenspeicher(config, welt):
    """Erst der echte Server, dann die Testwelt — und wieder zurück."""

    class Server:
        def fetch_world(self):
            return "u-chronist", welt

    echt = service.sync(config, runde(config), passwort=PASSWORT, client=Server())
    assert not echt.stale
    echte_figuren = {figur.name for figur in echt.snapshot.characters}

    settings.save_foundry_quelle(runde(config), settings.TESTWELT)
    testlauf = service.sync(config, runde(config), client=NiemalsAnrufen())
    assert not echte_figuren & {figur.name for figur in testlauf.snapshot.characters}

    # Zurück: die Runde wurde nie an die Testwelt gebunden, der Abgleich verweigert also
    # nicht wegen einer »anderen Welt«.
    settings.save_foundry_quelle(runde(config), settings.SERVER)
    zurueck = service.sync(config, runde(config), passwort=PASSWORT, client=Server())
    assert not zurueck.stale, zurueck.message
    assert {figur.name for figur in zurueck.snapshot.characters} == echte_figuren


def test_die_runde_wird_nicht_an_die_testwelt_gebunden(auf_testwelt):
    config = auf_testwelt
    service.sync(config, runde(config), client=NiemalsAnrufen())
    scope = db.scoped(runde(config))
    try:
        assert store.world(scope) is None
    finally:
        scope.close()


def test_die_quelle_gehoert_der_runde_und_faellt_auf_den_server_zurueck(config):
    gruppe = runde(config)
    assert settings.foundry_quelle(gruppe) == settings.SERVER
    assert settings.save_foundry_quelle(gruppe, settings.TESTWELT) is True
    assert settings.foundry_quelle(gruppe) == settings.TESTWELT
    assert settings.save_foundry_quelle(gruppe, "erfunden") is False
    assert settings.foundry_quelle(gruppe) == settings.TESTWELT


def test_jeder_abgleich_sagt_dass_die_welt_erfunden_ist(auf_testwelt):
    """Das Dauerband der Oberfläche ist mit #157 fort — der Satz steht jetzt an der Meldung.

    Sie geht bei jedem ``/chronik fertig`` und bei jedem nächtlichen Lauf in den Kanal.
    Wer erfundene Zahlen für echte hält, ist der teuerste Fehler dieses Schalters; dagegen
    hilft nur, es an jeder Stelle zu sagen, an der Zahlen entstehen.
    """
    stand = service.sync(auf_testwelt, runde(auf_testwelt))
    assert not stand.stale, stand.message
    assert testwelt.HINWEIS in stand.message


def test_die_wahl_selbst_sagt_es_auch(auf_testwelt):
    """``/setup`` in Discord antwortet auf die Umstellung mit derselben Warnung."""
    assert "erfunden" in einrichten.QUELLE_GESETZT_TESTWELT
    assert einrichten.QUELLE_TESTWELT.endswith("erfundene Zahlen")


class NurDieWelt:
    """Ein Foundry, das genau diesen Rohdump zeigt — mehr braucht der Abgleich nicht."""

    def __init__(self, welt):
        self._welt = welt

    def fetch_world(self):
        return UNSER_KONTO, self._welt


def test_ohne_testwelt_steht_der_hinweis_nirgends(config, welt):
    stand = service.sync(config, runde(config), client=NurDieWelt(welt))
    assert testwelt.HINWEIS not in stand.message


def test_die_betreiberseite_stellt_die_quelle_nicht_mehr_um(config):
    """Sie gehört der Runde — ein Speichern dort lässt sie, wie sie ist (#89)."""
    client = create_app(config).test_client()
    settings.save_foundry_quelle(runde(config), settings.TESTWELT)

    antwort = client.post(
        "/einstellungen", data={settings.QUELLE_KEY: settings.SERVER}, headers=KOPF
    )

    assert antwort.status_code == 302
    assert settings.foundry_quelle(runde(config)) == settings.TESTWELT
    seite = client.get("/einstellungen", headers=KOPF).get_data(as_text=True)
    assert f'name="{settings.QUELLE_KEY}"' not in seite


def test_mit_testwelt_fragt_der_sitzungsstart_kein_passwort(auf_testwelt):
    """Wer die Testwelt gewählt hat, hat Foundry entschieden — auch ohne Adresse."""
    assert not chronik.foundry_im_spiel(auf_testwelt, runde(auf_testwelt))


def test_die_fixture_ist_gueltiges_json_und_wird_nur_einmal_gelesen():
    testwelt.welt.cache_clear()
    erste = testwelt.welt()
    assert json.dumps(erste)[:1] == "{"
    assert testwelt.welt() is erste
