from dataclasses import fields

from conftest import (
    GM_FIGUR,
    GM_SZENE,
    KARTENLEISTE,
    LEITUNG,
    OHNE_KARTENLEISTE,
    UNBETEILIGTES_KONTO,
    UNSER_KONTO,
)

from chronicle.foundry import permissions
from chronicle.foundry.model import Scene
from chronicle.foundry.systems import read_roll
from chronicle.foundry.world import project

STAND = "2026-08-05T20:00:00+00:00"


def abzug(welt, user_id=UNSER_KONTO):
    return project(welt, user_id, fetched_at=STAND)


def namen(eintraege):
    return [eintrag.name for eintrag in eintraege]


def test_wirksame_stufe_ist_das_maximum_aus_default_und_benutzer():
    ownership = {"default": 1, "u-a": 3}
    assert permissions.effective_level(ownership, "u-a") == permissions.OWNER
    assert permissions.effective_level(ownership, "u-b") == permissions.LIMITED
    assert permissions.effective_level(None, "u-a") == permissions.NONE


def test_journalseite_erbt_das_maximum_aus_dokument_und_seite():
    dokument = {"default": 0, "u-a": 2}
    seite = {"default": 0}
    assert (
        max(
            permissions.effective_level(dokument, "u-a"),
            permissions.effective_level(seite, "u-a"),
        )
        == permissions.OBSERVER
    )


def test_gm_only_figur_landet_nicht_im_abzug(welt):
    assert GM_FIGUR not in namen(abzug(welt).characters)


def test_spielleitung_sieht_dieselbe_welt_vollstaendig(welt):
    assert GM_FIGUR in namen(abzug(welt, LEITUNG).characters)


def test_limited_gibt_nur_den_namen_her(welt):
    wirtin = next(f for f in abzug(welt).characters if f.id == "a-wirtin")
    assert wirtin.limited
    assert wirtin.type is None
    assert wirtin.owner_ids == ()


def test_observer_und_owner_geben_die_zuordnung_her(welt):
    figuren = {f.id: f for f in abzug(welt).characters}
    assert figuren["a-aelin"].owner_ids == ("u-mira",)
    assert figuren["a-brok"].owner_ids == (UNSER_KONTO,)
    assert figuren["a-brok"].type == "character"


def test_nur_konten_mit_sichtbarer_figur_werden_uebernommen(welt):
    gespeichert = namen(abzug(welt).players)
    assert set(gespeichert) == {"Chronist", "Mira"}
    assert UNBETEILIGTES_KONTO not in gespeichert


def test_gefluestertes_an_die_leitung_wird_verworfen(welt):
    assert "m-fluester-gm" not in [n.id for n in abzug(welt).messages]


def test_gefluestertes_an_uns_bleibt(welt):
    assert "m-fluester-an-uns" in [n.id for n in abzug(welt).messages]


def test_blinder_wurf_ist_nur_fuer_die_leitung(welt):
    assert "m-blind" not in [n.id for n in abzug(welt).messages]
    assert "m-blind" in [n.id for n in abzug(welt, LEITUNG).messages]


def test_die_zahlen_kommen_aus_system_roll_nicht_aus_content(welt):
    wurf = next(n for n in abzug(welt).messages if n.id == "m-wurf")
    assert wurf.content == ""
    assert wurf.roll.total == 7
    assert wurf.roll.formula == "1d12 + 1d12 + 3"
    assert wurf.roll.modifier_total == 3
    assert [(w.name, w.faces, w.value) for w in wurf.roll.dice] == [
        ("hope", "d12", 3),
        ("fear", "d12", 1),
    ]


def test_die_figur_kommt_aus_speaker_actor_nicht_aus_author(welt):
    wurf = next(n for n in abzug(welt).messages if n.id == "m-wurf")
    assert wurf.speaker_actor == "a-brok"
    assert wurf.speaker_alias == "Brok Eisenfaust"


def test_nachricht_ohne_wurf_hat_keinen_wurf(welt):
    text = next(n for n in abzug(welt).messages if n.id == "m-aufbruch")
    assert text.roll is None
    assert text.content == "Wir brechen bei Sonnenaufgang auf."


def test_fremdes_regelwerk_liefert_dieselben_zahlen_ohne_benannte_wuerfel(welt):
    nachricht = welt["messages"][1]
    wurf = read_roll("dnd5e", nachricht)
    assert wurf.total == 7
    assert wurf.dice == ()


def test_regelwerk_aus_dem_abzug(welt):
    assert abzug(welt).system == "daggerheart"
    assert project({"system": "pf2e"}, UNSER_KONTO, fetched_at=STAND).system == "pf2e"
    assert project({}, UNSER_KONTO, fetched_at=STAND).system == "unbekannt"


def test_beifang_wird_nicht_uebernommen(welt):
    abgezogen = abzug(welt)
    assert abgezogen.fetched_at == STAND
    # Vom Karten-Innenleben — Wänden, Lichtern, Token — kommt nichts mit: die Chronik
    # trägt es nicht, und was nicht gespeichert wird, kann nicht auslaufen.
    assert {feld.name for feld in fields(Scene)} == {"id", "name", "active"}


def test_die_szene_die_nur_die_leitung_kennt_ist_ein_vorgriff(welt):
    """Der Spoiler-Fall: ein ungespielter Ortsname im Protokoll verriete das Geheimnis."""
    assert GM_SZENE not in namen(abzug(welt).scenes)
    assert GM_SZENE in namen(abzug(welt, LEITUNG).scenes)


def test_der_ortsname_kommt_aus_navname_und_faellt_auf_name_zurueck(welt):
    """``navName`` ist, was in der Kartenleiste steht — im echten Abzug oft leer."""
    orte = {szene.id: szene.name for szene in abzug(welt).scenes}
    assert orte["s-keller"] == KARTENLEISTE
    assert orte["s-markt"] == OHNE_KARTENLEISTE


def test_foundry_sagt_welche_karte_gerade_liegt(welt):
    aktiv = [szene.id for szene in abzug(welt).scenes if szene.active]
    assert aktiv == ["s-keller"]


def test_eine_welt_ohne_szenen_liefert_keine(welt):
    assert abzug({}).scenes == ()
    assert abzug({"scenes": []}).scenes == ()
