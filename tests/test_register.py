"""Das Register: Vorschlag, Bestätigung, Verweise — und was nie von allein passiert."""

from conftest import UNSER_KONTO

from chronicle import db, notes, register, search
from chronicle.compose.client import ModelUnreachable
from chronicle.compose.service import compose_session
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.foundry.world import project

WIRTIN = "Die Wirtin zum Krummen Ast"

ANTWORT = (
    f"figur | {WIRTIN} | Schenkt im Krummen Ast aus und warnt vor dem Keller.\n"
    "ort | Der Keller | Unter dem Wirtshaus, feucht und verschlossen.\n"
    "faden | Der verschlossene Keller | Die Runde kam nicht hinein."
)


class Chronist:
    """Ein Modell, das für die Komposition und fürs Register verschiedenes sagt."""

    name = "chronist-test"

    def __init__(self, register_antwort=ANTWORT, satz="Die Runde tastet sich voran."):
        self._register = register_antwort
        self._satz = satz

    def write(self, *, system, prompt):
        if system.startswith("Du führst das Register"):
            return self._register
        if "Fäden" in system:
            return "- Die Wirtin wartet auf Antwort."
        return self._satz


class Stumm:
    name = "stumm"

    def write(self, *, system, prompt):
        raise ModelUnreachable("nicht erreichbar")


def eine_sitzung(tmp_path, *, text=f"{WIRTIN} warnt vor dem Keller."):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    sitzung_id = notes.create_session(config.database_path, played_on="2026-08-05", title="Keller")
    szene = notes.session(config.database_path, sitzung_id).scenes[0]
    notes.add_note(config.database_path, szene.id, text)
    return config, sitzung_id, szene.id


def mit_chronik(tmp_path, **kwargs):
    config, sitzung_id, szene_id = eine_sitzung(tmp_path, **kwargs)
    compose_session(config, sitzung_id, model=Chronist())
    return config, sitzung_id, szene_id


def namen(eintraege):
    return [eintrag.name for eintrag in eintraege]


# --- Vorschläge lesen und verwerfen ---------------------------------------------------


def test_die_zeilen_des_modells_werden_zu_kandidaten():
    kandidaten = register.parse(ANTWORT)
    assert [k.kind for k in kandidaten] == [register.FIGUR, register.ORT, register.FADEN]
    assert kandidaten[0].name == WIRTIN
    assert kandidaten[1].description == "Unter dem Wirtshaus, feucht und verschlossen."


def test_unfoermige_zeilen_fallen_still_weg():
    kandidaten = register.parse(
        "Hier kommen die Einträge:\n"
        "figur | Ohne Satz\n"
        "gegenstand | Das Schwert | Liegt im Keller.\n"
        "figur |  | Ein Satz ohne Namen.\n"
        "- ort | Der Hafen | Weit im Süden.\n"
    )
    assert [(k.kind, k.name) for k in kandidaten] == [(register.ORT, "Der Hafen")]


def test_derselbe_name_kommt_nur_einmal_vor():
    kandidaten = register.parse(
        "figur | Mira | Die Späherin.\nfigur | mira | Noch einmal dieselbe.",
    )
    assert len(kandidaten) == 1


def test_mehr_als_die_obergrenze_wird_abgeschnitten():
    zeilen = "\n".join(f"ort | Ort {n} | Ein Satz." for n in range(register.MAX_VORSCHLAEGE + 5))
    assert len(register.parse(zeilen)) == register.MAX_VORSCHLAEGE


def test_ein_vorschlag_mit_unbelegter_zahl_wird_verworfen(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    modell = Chronist(register_antwort="ort | Der Keller | Dort lagen 47 Fässer.")
    assert register.suggest(config, sitzung_id, model=modell).count == 0
    assert register.pending(config.database_path) == ()


def test_eine_zahl_aus_der_chronik_bleibt_stehen(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path, text="Im Keller stehen 3 Fässer.")
    modell = Chronist(register_antwort="ort | Der Keller | Dort stehen 3 Fässer.")
    assert register.suggest(config, sitzung_id, model=modell).count == 1
    assert register.pending(config.database_path)[0].description == "Dort stehen 3 Fässer."


# --- Der ehrliche Rückfall ------------------------------------------------------------


def test_ohne_modell_gibt_es_keine_vorschlaege_sondern_einen_satz(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    ergebnis = register.suggest(config, sitzung_id)
    assert ergebnis.count == 0
    assert ergebnis.reason == register.OHNE_MODELL
    assert ergebnis.message == register.OHNE_MODELL
    assert register.pending(config.database_path) == ()


def test_ein_stummes_modell_laesst_das_register_wie_es_war(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    ergebnis = register.suggest(config, sitzung_id, model=Stumm())
    assert ergebnis.reason == register.NICHT_ERREICHBAR
    assert register.pending(config.database_path) == ()


def test_ohne_chronik_gibt_es_nichts_vorzuschlagen(tmp_path):
    config, sitzung_id, _ = eine_sitzung(tmp_path)
    ergebnis = register.suggest(config, sitzung_id, model=Chronist())
    assert ergebnis.reason == register.OHNE_CHRONIK
    assert register.pending(config.database_path) == ()


# --- Bestätigt wird von Hand ----------------------------------------------------------


def test_ein_vorschlag_steht_nicht_von_allein_im_register(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    assert register.suggest(config, sitzung_id, model=Chronist()).count == 3
    assert len(register.pending(config.database_path)) == 3
    assert register.overview(config.database_path) == ()


def test_ein_ja_nimmt_auf_ein_nein_verwirft(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    offen = {eintrag.name: eintrag.id for eintrag in register.pending(config.database_path)}
    register.decide(
        config.database_path,
        {
            offen[WIRTIN]: register.Entscheidung(ja=True),
            offen["Der Keller"]: register.Entscheidung(ja=False),
        },
    )
    assert namen(register.pending(config.database_path)) == ["Der verschlossene Keller"]
    gruppen = register.overview(config.database_path)
    assert [gruppe.kind for gruppe in gruppen] == [register.FIGUR]
    assert namen(gruppen[0].entries) == [WIRTIN]


def test_wer_nichts_entscheidet_laesst_die_zeile_stehen(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    register.decide(config.database_path, {})
    assert len(register.pending(config.database_path)) == 3


def test_ein_ja_uebernimmt_die_richtigstellung(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    register.decide(
        config.database_path,
        {eintrag.id: register.Entscheidung(ja=True, name="Die Wirtin", description="Schenkt aus.")},
    )
    bestaetigt = register.overview(config.database_path)[0].entries[0]
    assert (bestaetigt.name, bestaetigt.description) == ("Die Wirtin", "Schenkt aus.")


def test_ein_zweiter_lauf_ueberschreibt_keinen_bestaetigten_satz(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    register.decide(config.database_path, {eintrag.id: register.Entscheidung(ja=True)})

    anders = Chronist(register_antwort=f"figur | {WIRTIN} | Ganz etwas anderes.")
    ergebnis = register.suggest(config, sitzung_id, model=anders)

    assert ergebnis.count == 0
    bestaetigt = register.overview(config.database_path)[0].entries[0]
    assert bestaetigt.description.startswith("Schenkt im Krummen Ast aus")
    assert bestaetigt.state == register.BESTAETIGT


# --- Verweise -------------------------------------------------------------------------


def test_der_eintrag_verweist_auf_szene_und_sitzung(tmp_path):
    config, sitzung_id, szene_id = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    assert [(v.session_id, v.scene_id) for v in eintrag.mentions] == [(sitzung_id, szene_id)]
    assert eintrag.mentions[0].scene_position == 1


def test_ein_gedeuteter_faden_landet_bei_seiner_sitzung(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    faden = next(
        e for e in register.pending(config.database_path) if e.name == "Der verschlossene Keller"
    )
    assert [(v.session_id, v.scene_id) for v in faden.mentions] == [(sitzung_id, None)]


def test_ein_zweiter_lauf_verdoppelt_die_verweise_nicht(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    register.suggest(config, sitzung_id, model=Chronist())
    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    assert len(eintrag.mentions) == 1


def test_eine_figur_wird_mit_dem_foundry_aktor_verknuepft(tmp_path, welt):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    connection = db.connect(config.database_path)
    try:
        store.save(connection, project(welt, UNSER_KONTO, fetched_at="2026-08-05T20:00:00+00:00"))
    finally:
        connection.close()
    register.suggest(config, sitzung_id, model=Chronist())
    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    assert eintrag.actor_name == WIRTIN


def test_ohne_foundry_stand_bleibt_der_verweis_leer(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    assert all(e.actor_name is None for e in register.pending(config.database_path))


# --- Suche ----------------------------------------------------------------------------


def test_ein_bestaetigter_eintrag_ist_zu_finden_ein_vorschlag_nicht(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    assert search.REGISTER not in [
        g.kind for g in search.find(config.database_path, "Wirtin").groups
    ]

    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    register.decide(config.database_path, {eintrag.id: register.Entscheidung(ja=True)})

    gruppen = search.find(config.database_path, "Wirtin").groups
    assert gruppen[0].kind == search.REGISTER
    assert gruppen[0].hits[0].ref_id == eintrag.id
    assert gruppen[0].hits[0].session_id == sitzung_id


def test_ein_geloeschter_eintrag_verschwindet_aus_der_suche(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    register.decide(config.database_path, {eintrag.id: register.Entscheidung(ja=True)})
    assert search.find(config.database_path, "Krummen").groups

    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute("DELETE FROM register_entry WHERE id = ?", (eintrag.id,))
    finally:
        connection.close()
    assert search.REGISTER not in [
        g.kind for g in search.find(config.database_path, "Krummen").groups
    ]


def test_der_index_wird_beim_start_nachgetragen(tmp_path):
    config, sitzung_id, _ = mit_chronik(tmp_path)
    register.suggest(config, sitzung_id, model=Chronist())
    eintrag = next(e for e in register.pending(config.database_path) if e.name == WIRTIN)
    register.decide(config.database_path, {eintrag.id: register.Entscheidung(ja=True)})

    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute("DELETE FROM search_index")
    finally:
        connection.close()
    db.init(config.database_path)

    assert search.find(config.database_path, "Wirtin").groups[0].kind == search.REGISTER
