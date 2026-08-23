"""Der Adapter gegen einen **echten** Weltabzug — anonymisiert, aber nicht erfunden.

Herkunft der Fixture ``echtwelt-2026-08-06.json``: sie ist ein Abzug des Daggerheart-
Servers dieser Runde vom 2026-08-06 (13,9 MB roh, auf der Box unter
``/mnt/data/stacks/daggerheart-aufnahmen/welt-dump-2026-08-06.json``). Eingecheckt ist
nicht der Abzug, sondern was ``scripts/anonymisiere_welt.py`` daraus macht: eine
Erlaubnisliste je Ebene wirft alles weg, was nicht ausdrücklich genannt ist — Journale,
Makros, Gegenstände, Biografien, Wortlaute —, Namen werden durch nummerierte
Kunstsilben ersetzt, und die Selbstprüfung ``pruefe`` bricht ab, statt zu schreiben,
wenn ein Name, eine Adresse, ein Rechnername, eine IP, eine Telefonnummer oder ein
Heimatpfad überlebt hat. Der Lauf meldete: 17 Konten, 154 Figuren, 59 Nachrichten,
45 Szenen, 210 Namen ersetzt, 329 Spuren geprüft, keine Beanstandung.

**Warum es diese Datei gibt.** #242 wurde ohne sie behoben: dass die Zahlen in
``rolls[]`` und nicht in ``message.system.roll`` stehen, war recherchiert (#146) und an
einer *erzeugten* Fixture geprüft. Der Commit sagte selbst, die echte Form bleibe
unbelegt. Sie ist es jetzt nicht mehr — und der Abzug hat den Fix an zwei Stellen
korrigiert (siehe ``test_ein_veralteter_block_wird_fallengelassen`` und
``test_die_benannten_wuerfel_stehen_in_den_termen``).

Die Zahlen in den Erwartungen sind an dieser Datei ausgezählt, nicht gewählt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import runde

from chronicle import db, notes
from chronicle.foundry import service, systems, world

DATEI = Path(__file__).with_name("echtwelt-2026-08-06.json")

# Ausgezählt an der Fixture. Sie stehen hier oben, weil sie der eigentliche Befund sind.
NACHRICHTEN = 59
MIT_ROLLS = 40
MIT_SYSTEM_BLOCK = 59
MIT_SYSTEM_ROLL = 0
# Drei der 40 Würfe sind unausgewertet (``total`` leer, ``formula`` leer) — ein
# Schadenswurf ohne Ergebnis ist keine Zahl, und es wird auch keine erfunden.
LESBARE_WUERFE = 37
MIT_BENANNTEN_WUERFELN = 20
MIT_TITEL = 32
SZENEN_GESAMT = 45
SZENEN_SICHTBAR = 3

# Der Abend lief am 2026-08-05 von 18:16 bis 20:26 UTC.
VOR_DEM_ABEND = "2026-08-05T17:00:00+00:00"
MITTEN_IM_ABEND = "2026-08-05T19:00:00+00:00"
WUERFE_ERSTE_SZENE = 29
WUERFE_ZWEITE_SZENE = 8


@pytest.fixture(scope="module")
def echtwelt() -> Mapping:
    return json.loads(DATEI.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schnappschuss(echtwelt):
    return world.project(echtwelt, str(echtwelt["userId"]), fetched_at="2026-08-06T00:00:00+00:00")


def _wuerfe(echtwelt) -> list:
    return [systems.read_roll(systems.DAGGERHEART, n) for n in echtwelt["messages"]]


def _serialisiert(nachricht: Mapping) -> list[Mapping]:
    return [json.loads(eintrag) for eintrag in nachricht.get("rolls") or ()]


def test_der_befund_aus_242_steht_jetzt_belegt_in_einer_fixture(echtwelt):
    """Die drei Zahlen, um die es ging — an echtem Material und nicht an einer Annahme."""
    nachrichten = echtwelt["messages"]
    assert len(nachrichten) == NACHRICHTEN
    assert sum(1 for n in nachrichten if n.get("rolls")) == MIT_ROLLS
    assert sum(1 for n in nachrichten if isinstance(n.get("system"), Mapping)) == MIT_SYSTEM_BLOCK
    assert (
        sum(1 for n in nachrichten if isinstance((n.get("system") or {}).get("roll"), Mapping))
        == MIT_SYSTEM_ROLL
    )


def test_wer_nur_den_system_block_liest_findet_keine_einzige_zahl(echtwelt):
    """Der Adapter vor c419d19, hier nachgestellt: er sah nur ``message.system.roll``.

    Das ist die Gegenprobe zu allem, was darunter steht — ohne sie könnte der zweite
    Einstieg auch überflüssig sein.
    """
    alt = [
        n.get("system", {}).get("roll")
        for n in echtwelt["messages"]
        if isinstance((n.get("system") or {}).get("roll"), Mapping)
    ]
    assert alt == []
    assert sum(1 for wurf in _wuerfe(echtwelt) if wurf is not None) == LESBARE_WUERFE


def test_was_zahlen_traegt_lohnt_den_thread(schnappschuss, echtwelt):
    """``lohnt`` hing an ``read_roll`` — und war damit gegen diesen Server durchweg falsch."""
    fuer_alle = world.fuer_die_gruppe(echtwelt)
    lohnend = [n for n in schnappschuss.messages if systems.lohnt(n)]
    assert len(lohnend) == LESBARE_WUERFE
    # Und keiner davon fällt an der engeren Sicht des Sitzungs-Threads noch heraus.
    assert sum(1 for n in lohnend if n.id in fuer_alle) == LESBARE_WUERFE


def test_die_benannten_wuerfel_stehen_in_den_termen(echtwelt):
    """Die zweite Korrektur an c419d19: ``terms[]`` **nennt** Hoffnung und Furcht selbst.

    Der Commit von damals ließ die benannten Würfel leer, wo nur die Kernfelder da waren —
    »welcher d12 die Hoffnung war, sagt allein der aufbereitete Block«. Der echte Abzug
    sagt etwas anderes: die Klasse des Terms heißt ``HopeDie`` bzw. ``FearDie``. Geraten
    wird also nichts, und der Block wird dafür nicht gebraucht.
    """
    mit_wuerfeln = [wurf for wurf in _wuerfe(echtwelt) if wurf and wurf.dice]
    assert len(mit_wuerfeln) == MIT_BENANNTEN_WUERFELN
    for wurf in mit_wuerfeln:
        assert [wuerfel.name for wuerfel in wurf.dice] == ["hope", "fear"]
        for wuerfel in wurf.dice:
            assert wuerfel.faces in {"d8", "d12"}
            assert 1 <= wuerfel.value <= int(wuerfel.faces[1:])
    # Die Klassen stehen wirklich so in der Fixture — sonst prüfte der Test seine Vorlage.
    klassen = {
        str(term.get("class"))
        for nachricht in echtwelt["messages"]
        for wurf in _serialisiert(nachricht)
        for term in wurf.get("terms") or ()
    }
    assert {"HopeDie", "FearDie"} <= klassen


def test_der_titel_steht_neben_dem_aufbereiteten_block_und_nicht_darin(echtwelt):
    """Kein einziges ``options.roll`` trägt einen ``title`` — der steht in ``options``."""
    bloecke = [
        wurf["options"]["roll"]
        for nachricht in echtwelt["messages"]
        for wurf in _serialisiert(nachricht)
        if isinstance((wurf.get("options") or {}).get("roll"), Mapping) and wurf["options"]["roll"]
    ]
    assert bloecke and not any(block.get("title") for block in bloecke)
    assert sum(1 for wurf in _wuerfe(echtwelt) if wurf and wurf.title) == MIT_TITEL


def test_ein_veralteter_block_wird_fallengelassen(echtwelt):
    """Die erste Korrektur an c419d19: ``options.roll`` gewann, und lag zweimal daneben.

    Zwei der 32 Blöcke beschreiben einen **anderen** Wurf als den gesendeten — sie tragen
    Formel, Summe und Würfel eines früheren weiter. c419d19 nahm von dort Summe und
    Formel; die Chronik hätte hier 14 statt der gewürfelten 25 getragen. Belegt wird beides
    an derselben Nachricht: was im Block steht und was herauskommt.
    """
    veraltet = [
        (nachricht, wurf)
        for nachricht in echtwelt["messages"]
        for wurf in _serialisiert(nachricht)
        if isinstance((wurf.get("options") or {}).get("roll"), Mapping)
        and wurf["options"]["roll"]
        and wurf["options"]["roll"].get("formula") != wurf.get("formula")
    ]
    assert len(veraltet) == 2

    nachricht, roh = next(n for n in veraltet if n[1].get("total") == 25)
    block = roh["options"]["roll"]
    assert (block["total"], block["formula"]) == (14, "1d12 + 1d12 + 3 + 3")
    assert (block["hope"]["value"], block["fear"]["value"]) == (2, 6)

    gelesen = systems.read_roll(systems.DAGGERHEART, nachricht)
    assert (gelesen.total, gelesen.formula) == (25, "1d12 + 1d12 + 2")
    assert [(w.name, w.value) for w in gelesen.dice] == [("hope", 11), ("fear", 12)]
    # Was nur im Block stand, fällt mit ihm weg, statt als fremde Zahl stehenzubleiben.
    assert gelesen.modifier_total is None


def test_wo_block_und_wurf_denselben_meinen_zaehlt_der_block_weiter(echtwelt):
    """Die 30 unbeanstandeten Blöcke tragen ihren Beitrag unverändert bei."""
    mit_modifikator = [
        wurf for wurf in _wuerfe(echtwelt) if wurf and wurf.modifier_total is not None
    ]
    assert len(mit_modifikator) == 30
    assert sum(1 for wurf in _wuerfe(echtwelt) if wurf and wurf.critical) == 1
    assert {wurf.kind for wurf in _wuerfe(echtwelt) if wurf} == {
        "action",
        "reaction",
        "BaseRoll",
        "DualityRoll",
    }


def test_die_szenen_kommen_gefiltert_und_aus_der_kartenleiste_benannt(echtwelt, schnappschuss):
    """45 Karten liegen in der Welt, drei durfte dieses Konto sehen — und die heißen richtig."""
    assert len(echtwelt["scenes"]) == SZENEN_GESAMT
    assert len(schnappschuss.scenes) == SZENEN_SICHTBAR
    assert all(szene.name for szene in schnappschuss.scenes)

    roh = {str(szene["_id"]): szene for szene in echtwelt["scenes"]}
    mit_leiste = [s for s in schnappschuss.scenes if roh[s.id].get("navName")]
    ohne_leiste = [s for s in schnappschuss.scenes if not roh[s.id].get("navName")]
    assert mit_leiste and ohne_leiste, "beide Fälle müssen vorkommen, sonst prüft das nichts"
    for szene in mit_leiste:
        assert szene.name == roh[szene.id]["navName"]
    for szene in ohne_leiste:
        assert szene.name == roh[szene.id]["name"]


def test_der_abend_haengt_seine_wuerfe_an_die_szenen_der_sitzung(config, echtwelt):
    """Der Weg bis in die Chronik, an echtem Material: ohne ``read_roll`` bleibt er leer.

    ``_nachtragen`` überspringt jede Nachricht, für die ``lohnt`` falsch ist. Vor c419d19
    war das jede einzelne — der Abgleich holte 59 Nachrichten und die Chronik trug keine
    Zahl. Hier landen sie an den zwei Trennlinien dieses Abends.
    """
    eine = runde(config)
    sitzung = _sitzung(eine, [("Aufbruch", VOR_DEM_ABEND), ("Keller", MITTEN_IM_ABEND)])
    schnapp = world.project(
        echtwelt, str(echtwelt["userId"]), fetched_at="2026-08-06T00:00:00+00:00"
    )

    getan = service._nachtragen(eine, sitzung, echtwelt, schnapp)

    assert getan == WUERFE_ERSTE_SZENE + WUERFE_ZWEITE_SZENE == LESBARE_WUERFE
    szenen = [szene.id for szene in notes.session(eine, sitzung).scenes]
    verknuepft = [len(_verknuepft(eine, szene)) for szene in szenen]
    assert verknuepft == [WUERFE_ERSTE_SZENE, WUERFE_ZWEITE_SZENE]


def _sitzung(eine, szenen):
    scope = db.scoped(eine)
    try:
        with scope:
            sitzung = int(
                scope.execute(
                    "INSERT INTO session (runde_id, played_on, title, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (scope.runde_id, "2026-08-05", "Der belegte Abend", szenen[0][1]),
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


def _verknuepft(eine, szene_id):
    scope = db.scoped(eine)
    try:
        return [
            zeile["message_id"]
            for zeile in scope.execute(
                "SELECT message_id FROM scene_foundry_message WHERE runde_id = ? AND scene_id = ?",
                (scope.runde_id, szene_id),
            ).fetchall()
        ]
    finally:
        scope.close()
