from chronicle import db, notes, search
from chronicle.compose.service import KIND, RUECKBLICK, compose_session, recap_session
from chronicle.config import Config

NOTIZ_TEXT = "Wir fanden ein Schwert im Keller."


class Chronist:
    name = "chronist-test"

    def __init__(self, satz="Die Runde tastet sich voran."):
        self._satz = satz

    def write(self, *, system, prompt):
        if "Fäden" in system:
            return "- Die Wirtin wartet auf Antwort."
        return self._satz


def eine_sitzung(tmp_path, *, text=NOTIZ_TEXT):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    sitzung_id = notes.create_session(config.database_path, played_on="2026-08-05", title="Keller")
    szene = notes.session(config.database_path, sitzung_id).scenes[0]
    notes.add_note(config.database_path, szene.id, text)
    return config, sitzung_id, szene.id


def arten(ergebnis):
    return [gruppe.kind for gruppe in ergebnis.groups]


def test_eine_notiz_ist_ohne_neustart_zu_finden(tmp_path):
    config, sitzung_id, szene_id = eine_sitzung(tmp_path)
    ergebnis = search.find(config.database_path, "Schwert")
    assert arten(ergebnis) == [search.NOTIZ]
    treffer = ergebnis.groups[0].hits[0]
    assert treffer.session_id == sitzung_id
    assert treffer.scene_id == szene_id
    assert treffer.title == "Keller"


def test_die_suche_geht_ueber_wortanfaenge(tmp_path):
    config, _, _ = eine_sitzung(tmp_path)
    assert search.find(config.database_path, "schwe").groups
    assert not search.find(config.database_path, "chwert").groups


def test_alle_woerter_muessen_vorkommen(tmp_path):
    config, _, _ = eine_sitzung(tmp_path)
    assert search.find(config.database_path, "Schwert Keller").groups
    assert not search.find(config.database_path, "Schwert Hafen").groups


def test_was_ein_mensch_tippt_ist_keine_abfragesprache(tmp_path):
    config, _, _ = eine_sitzung(tmp_path)
    assert search.expression('NOT "schwert" OR *') == '"NOT"* "schwert"* "OR"*'
    assert search.find(config.database_path, '"Schwert"?').groups
    assert search.find(config.database_path, "*").groups == ()


def test_die_chronik_liegt_im_index(tmp_path):
    config, sitzung_id, _ = eine_sitzung(tmp_path)
    compose_session(config, sitzung_id, model=Chronist())
    ergebnis = search.find(config.database_path, "tastet")
    assert arten(ergebnis) == [KIND]
    treffer = ergebnis.groups[0].hits[0]
    assert treffer.session_id == sitzung_id
    assert treffer.scene_id is None


def test_der_rueckblick_ist_eine_eigene_gruppe(tmp_path):
    config, sitzung_id, _ = eine_sitzung(tmp_path)
    compose_session(config, sitzung_id, model=Chronist())
    recap_session(config, sitzung_id, model=Chronist())
    assert arten(search.find(config.database_path, "wirtin")) == [RUECKBLICK]


def test_notizen_stehen_vor_den_protokollen(tmp_path):
    config, sitzung_id, _ = eine_sitzung(tmp_path)
    compose_session(config, sitzung_id, model=Chronist())
    assert arten(search.find(config.database_path, "schwert")) == [search.NOTIZ, KIND]


def test_ein_zweiter_lauf_laesst_keine_verworfene_fassung_stehen(tmp_path):
    config, sitzung_id, _ = eine_sitzung(tmp_path)
    compose_session(config, sitzung_id, model=Chronist("Die Runde zoegert."))
    compose_session(config, sitzung_id, model=Chronist("Die Runde stuermt vor."))
    assert not search.find(config.database_path, "zoegert").groups
    assert len(search.find(config.database_path, "stuermt").groups[0].hits) == 1


def test_eine_geloeschte_szene_nimmt_ihre_notizen_aus_dem_index(tmp_path):
    config, _, szene_id = eine_sitzung(tmp_path)
    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute("DELETE FROM scene WHERE id = ?", (szene_id,))
    finally:
        connection.close()
    assert not search.find(config.database_path, "Schwert").groups


def test_eine_datenbank_ohne_index_wird_beim_start_nachgetragen(tmp_path):
    config, sitzung_id, _ = eine_sitzung(tmp_path)
    compose_session(config, sitzung_id, model=Chronist())
    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute("DELETE FROM search_index")
    finally:
        connection.close()
    assert not search.find(config.database_path, "Schwert").indexed
    db.init(config.database_path)
    assert arten(search.find(config.database_path, "schwert")) == [search.NOTIZ, KIND]


def test_ohne_inhalt_sagt_das_ergebnis_dass_der_index_leer_ist(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    ergebnis = search.find(config.database_path, "Schwert")
    assert ergebnis.groups == ()
    assert ergebnis.indexed is False


def test_eine_leere_eingabe_sucht_nicht(tmp_path):
    config, _, _ = eine_sitzung(tmp_path)
    ergebnis = search.find(config.database_path, "   ")
    assert ergebnis.query == ""
    assert ergebnis.groups == ()
    assert ergebnis.indexed is True


def test_der_ausschnitt_hebt_hervor_und_bleibt_maskiert(tmp_path):
    config, _, _ = eine_sitzung(tmp_path, text="Ein <b>Schwert</b> & eine Kerze.")
    ausschnitt = str(search.find(config.database_path, "Schwert").groups[0].hits[0].snippet)
    assert "<mark>Schwert</mark>" in ausschnitt
    assert "&lt;b&gt;" in ausschnitt
    assert "&amp;" in ausschnitt


def test_die_trefferzahl_ist_begrenzt(tmp_path):
    config, _, szene_id = eine_sitzung(tmp_path)
    for _ in range(5):
        notes.add_note(config.database_path, szene_id, NOTIZ_TEXT)
    assert len(search.find(config.database_path, "Schwert", limit=3).groups[0].hits) == 3
