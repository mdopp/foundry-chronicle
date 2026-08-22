"""Wo der Adapter die Zahlen eines Wurfs sucht — und wo sie wirklich liegen.

Der Befund aus #242: gegen einen echten Daggerheart-Server trug **keine** von 76
archivierten Nachrichten einen ``system.roll``. Die Chronik blieb ohne jede Zahl, und
kein Test schlug an — weil die Fixtures den Block trugen, den der Adapter erwartete.

Diese Datei prüft deshalb den zweiten Einstieg, und sie tut es an Material, das nicht für
sie geschrieben wurde: die **mitgelieferte Testwelt**, aus der nur der ``system``-Block
entfernt ist. Ihre ``rolls[]``-Einträge stammen aus ``scripts/erzeuge_testwelt.py``, und
das Skript hat die Form an einem echten Abzug ausgezählt.
"""

from __future__ import annotations

import json

from chronicle.foundry import systems, testwelt

# Der Wurf aus #242, so wie ihn die Karte auf dem echten Server zeigte: ein Reaktionswurf
# »Wurf: Wissen« mit Ergebnis 8 aus 1W12 + 1W12 + 3. Die *Form* darum herum ist die
# recherchierte aus #146 — ``class``, ``formula``, ``total``, ``terms[]``, ``options`` —
# und **nicht** an diesem Server gemessen: was er wirklich sendet, sagt erst ein
# Mitschnitt von der Box. Der Fall steht hier trotzdem, weil er der ungünstigste ist:
# ``rolls[]`` ohne aufbereiteten Block.
NUR_KERN = {
    "class": "DualityRoll",
    "formula": "1d12 + 1d12 + 3",
    "total": 8,
    "evaluated": True,
    "terms": [
        {"class": "Die", "number": 1, "faces": 12, "results": [{"result": 3, "active": True}]},
        {"class": "OperatorTerm", "operator": "+"},
        {"class": "Die", "number": 1, "faces": 12, "results": [{"result": 2, "active": True}]},
        {"class": "OperatorTerm", "operator": "+"},
        {"class": "NumericTerm", "number": 3},
    ],
    "options": {"title": "Wurf: Wissen"},
}


def _ohne_system(nachricht: dict) -> dict:
    return {feld: wert for feld, wert in nachricht.items() if feld != "system"}


def _mit_wurf() -> list[dict]:
    return [n for n in testwelt.welt()["messages"] if n.get("rolls")]


def test_ein_wurf_wird_auch_ohne_system_block_gelesen():
    """Der Fehler aus #242, an der Testwelt nachgestellt: ohne den Block war alles None."""
    nachrichten = _mit_wurf()
    assert nachrichten, "die Testwelt trägt keinen Wurf mehr — dann prüft das hier nichts"
    for nachricht in nachrichten:
        wurf = systems.read_roll(systems.DAGGERHEART, _ohne_system(nachricht))
        assert wurf is not None
        assert wurf.total is not None
        assert wurf.formula


def test_ohne_system_block_kommt_dieselbe_zahl_heraus():
    """Beide Ablagen desselben Wurfs müssen dasselbe sagen — sonst zählte die Reihenfolge."""
    for nachricht in _mit_wurf():
        assert systems.read_roll(systems.DAGGERHEART, _ohne_system(nachricht)) == systems.read_roll(
            systems.DAGGERHEART, nachricht
        )


def test_der_aufbereitete_block_bleibt_der_erste_einstieg():
    """``system.roll`` gewinnt, wo es ihn gibt — er trägt die benannten Würfel."""
    nachricht = {
        "system": {"roll": {"title": "Aus dem System", "total": 7, "formula": "1d12"}},
        "rolls": [json.dumps(NUR_KERN)],
    }
    wurf = systems.read_roll(systems.DAGGERHEART, nachricht)
    assert (wurf.title, wurf.total) == ("Aus dem System", 7)


def test_ein_leerer_system_block_haelt_den_wurf_nicht_auf():
    nachricht = {"system": {"roll": {}}, "rolls": [json.dumps(NUR_KERN)]}
    assert systems.read_roll(systems.DAGGERHEART, nachricht).total == 8


def test_der_wurf_steht_als_json_string_und_nicht_als_objekt():
    """#146: ``rolls`` trägt JSON-**Strings**. Wer sie als Objekte liest, greift ins Leere."""
    wurf = systems.read_roll(systems.DAGGERHEART, {"rolls": [json.dumps(NUR_KERN)]})
    assert (wurf.title, wurf.total, wurf.formula) == ("Wurf: Wissen", 8, "1d12 + 1d12 + 3")
    assert wurf.kind == "DualityRoll"


def test_ohne_aufbereiteten_block_wird_keine_hoffnung_erfunden():
    """Welcher d12 die Hoffnung war, sagt nur der aufbereitete Block — geraten wird nicht."""
    assert systems.read_roll(systems.DAGGERHEART, {"rolls": [json.dumps(NUR_KERN)]}).dice == ()


def test_ein_halber_block_wird_aus_dem_wurf_darum_ergaenzt():
    """``options.roll`` trägt nicht überall Formel und Titel — die stehen dann eine Ebene höher."""
    halb = dict(
        NUR_KERN,
        options={
            "title": "Wurf: Wissen",
            "roll": {"total": 8, "hope": {"dice": "d12", "value": 3}},
        },
    )
    wurf = systems.read_roll(systems.DAGGERHEART, {"rolls": [json.dumps(halb)]})
    assert (wurf.title, wurf.total, wurf.formula) == ("Wurf: Wissen", 8, "1d12 + 1d12 + 3")
    assert wurf.dice[0].value == 3


def test_kaputte_und_leere_eintraege_sind_kein_wurf():
    assert systems.read_roll(systems.DAGGERHEART, {"rolls": ["kein json"]}) is None
    ohne_zahlen = {"rolls": [json.dumps({"class": "Roll"})]}
    assert systems.read_roll(systems.DAGGERHEART, ohne_zahlen) is None
    assert systems.read_roll(systems.DAGGERHEART, {"content": "nur Text"}) is None


def test_der_erste_brauchbare_eintrag_zaehlt():
    nachricht = {"rolls": ["kein json", json.dumps(NUR_KERN)]}
    assert systems.read_roll(systems.DAGGERHEART, nachricht).total == 8


def test_was_zahlen_traegt_lohnt_den_thread():
    from chronicle.foundry.model import ChatMessage

    wurf = systems.read_roll(systems.DAGGERHEART, {"rolls": [json.dumps(NUR_KERN)]})
    assert systems.lohnt(ChatMessage(id="m", timestamp=0, roll=wurf))
    assert not systems.lohnt(ChatMessage(id="m", timestamp=0, content="nur geredet"))
