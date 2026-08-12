"""Der wöchentliche Handgriff: Sitzung öffnen, Szene beginnen, mitschreiben.

Bedient wird das seit #157 in Discord (``/chronik start``, ``/szene``, eine Nachricht im
Thread — geprüft in ``test_chronik``). Was hier steht, ist die Schicht darunter: was
``notes`` zusagt, ganz gleich wer es anstößt.
"""

from conftest import runde

from chronicle import db, notes
from chronicle.config import Config


def gruppe(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    return runde(config)


def sitzung_anlegen(wo, **felder):
    return notes.create_session(wo, played_on="2026-08-05", **felder)


def test_neue_sitzung_bringt_sofort_eine_erste_szene(tmp_path):
    wo = gruppe(tmp_path)
    sitzung_id = sitzung_anlegen(wo, title="Der Keller unter dem Krummen Ast")

    gelesen = notes.session(wo, sitzung_id)
    assert gelesen.title == "Der Keller unter dem Krummen Ast"
    assert [szene.position for szene in gelesen.scenes] == [1]


def test_notiz_landet_bei_der_szene_und_uebersteht_das_neuladen(tmp_path):
    wo = gruppe(tmp_path)
    sitzung_id = sitzung_anlegen(wo)
    szene = notes.session(wo, sitzung_id).scenes[0]

    notes.add_note(wo, szene.id, "Brok tritt die Tür ein.")

    # Zweimal gelesen: nichts hängt an einem Aufruf, alles steht auf der Platte.
    for _ in range(2):
        gelesen = notes.session(wo, sitzung_id)
        assert [n.text for n in gelesen.scenes[0].notes] == ["Brok tritt die Tür ein."]


def test_leere_notiz_wird_nicht_gespeichert(tmp_path):
    wo = gruppe(tmp_path)
    sitzung_id = sitzung_anlegen(wo)
    szene = notes.session(wo, sitzung_id).scenes[0]

    notes.add_note(wo, szene.id, "   ")

    assert notes.session(wo, sitzung_id).note_count == 0


def test_eine_neue_szene_haengt_sich_hinten_an(tmp_path):
    wo = gruppe(tmp_path)
    sitzung_id = sitzung_anlegen(wo)

    notes.add_scene(wo, sitzung_id, title="Ankunft im Hafen")

    zweite = notes.session(wo, sitzung_id).scenes[1]
    assert zweite.position == 2
    assert zweite.title == "Ankunft im Hafen"


def test_notizen_bleiben_bei_ihrer_szene(tmp_path):
    wo = gruppe(tmp_path)
    sitzung_id = sitzung_anlegen(wo)
    erste = notes.session(wo, sitzung_id).scenes[0]
    zweite = notes.add_scene(wo, sitzung_id, title="")

    notes.add_note(wo, erste.id, "Erst der Keller.")
    notes.add_note(wo, zweite, "Dann der Hafen.")

    gelesen = notes.session(wo, sitzung_id)
    assert [n.text for n in gelesen.scenes[0].notes] == ["Erst der Keller."]
    assert [n.text for n in gelesen.scenes[1].notes] == ["Dann der Hafen."]


def test_ohne_titel_bleibt_das_datum(tmp_path):
    wo = gruppe(tmp_path)
    sitzung_anlegen(wo)
    assert notes.sessions(wo)[0].played_on == "2026-08-05"


def test_ohne_datum_zaehlt_heute(tmp_path):
    wo = gruppe(tmp_path)
    notes.create_session(wo, played_on="", title="")
    assert notes.sessions(wo)[0].played_on == notes.today()


def test_die_liste_zaehlt_szenen_und_notizen(tmp_path):
    wo = gruppe(tmp_path)
    sitzung_id = sitzung_anlegen(wo, title="Zweiter Abend")
    notes.add_note(wo, notes.session(wo, sitzung_id).scenes[0].id, "Eine Notiz.")

    eintrag = notes.sessions(wo)[0]
    assert eintrag.title == "Zweiter Abend"
    assert (eintrag.scene_count, eintrag.note_count) == (1, 1)


def test_ohne_sitzung_ist_die_liste_leer(tmp_path):
    assert notes.sessions(gruppe(tmp_path)) == ()


def test_eine_unbekannte_sitzung_gibt_es_nicht(tmp_path):
    assert notes.session(gruppe(tmp_path), 99) is None


def test_eine_szene_zu_einer_unbekannten_sitzung_entsteht_nicht(tmp_path):
    wo = gruppe(tmp_path)
    assert notes.add_scene(wo, 99, title="Nirgendwo") is None


def test_eine_unbekannte_szene_gehoert_zu_keiner_sitzung(tmp_path):
    assert notes.session_of_scene(gruppe(tmp_path), 99) is None


def test_mitschreiben_geht_ohne_foundry(tmp_path):
    # Foundry kommt beim Mitschreiben nicht vor — eine tote Foundry darf nichts blockieren.
    wo = gruppe(tmp_path)
    sitzung_id = sitzung_anlegen(wo)
    notes.add_note(wo, notes.session(wo, sitzung_id).scenes[0].id, "Ohne Foundry geht das auch.")
    assert notes.session(wo, sitzung_id).note_count == 1
