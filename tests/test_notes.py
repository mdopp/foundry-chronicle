"""Der wöchentliche Handgriff: Sitzung öffnen, Szene beginnen, mitschreiben."""

from conftest import runde

from chronicle import instanz, notes
from chronicle.app import create_app
from chronicle.config import Config


def klient(tmp_path):
    # Diese Tests wollen die Sitzungsliste sehen; ohne Foundry führte "/" sonst
    # jedes Mal in die Einrichtung.
    client = create_app(Config(data_dir=tmp_path)).test_client()
    instanz.finish_onboarding(pfad(tmp_path))
    return client


def pfad(tmp_path):
    return Config(data_dir=tmp_path).database_path


def sitzung_anlegen(client, **felder):
    antwort = client.post("/", data={"played_on": "2026-08-05", **felder})
    assert antwort.status_code == 302
    return antwort.headers["Location"]


def test_neue_sitzung_bringt_sofort_eine_erste_szene(tmp_path):
    client = klient(tmp_path)
    ort = sitzung_anlegen(client, title="Der Keller unter dem Krummen Ast")
    html = client.get(ort).get_data(as_text=True)
    assert "Szene 1" in html
    assert "Der Keller unter dem Krummen Ast" in html


def test_notiz_landet_bei_der_szene_und_uebersteht_das_neuladen(tmp_path):
    client = klient(tmp_path)
    ort = sitzung_anlegen(client)
    szene = notes.session(runde(pfad(tmp_path)), 1).scenes[0]

    antwort = client.post(f"/szenen/{szene.id}/notizen", data={"text": "Brok tritt die Tür ein."})
    assert antwort.status_code == 302

    html = client.get(ort).get_data(as_text=True)
    assert "Brok tritt die Tür ein." in html
    # Neuladen ist ein zweites GET: nichts liegt im Browser, alles auf der Platte.
    assert "Brok tritt die Tür ein." in client.get(ort).get_data(as_text=True)


def test_leere_notiz_wird_nicht_gespeichert(tmp_path):
    client = klient(tmp_path)
    ort = sitzung_anlegen(client)
    client.post("/szenen/1/notizen", data={"text": "   "})
    assert notes.session(runde(pfad(tmp_path)), 1).note_count == 0
    assert client.get(ort).status_code == 200


def test_neue_szene_braucht_keinen_modaldialog(tmp_path):
    client = klient(tmp_path)
    ort = sitzung_anlegen(client)

    antwort = client.post("/sitzungen/1/szenen", data={"title": "Ankunft im Hafen"})
    assert antwort.status_code == 302

    html = client.get(ort).get_data(as_text=True)
    assert "Szene 2" in html
    assert "Ankunft im Hafen" in html


def test_notizen_bleiben_bei_ihrer_szene(tmp_path):
    client = klient(tmp_path)
    sitzung_anlegen(client)
    client.post("/sitzungen/1/szenen", data={})
    client.post("/szenen/1/notizen", data={"text": "Erst der Keller."})
    client.post("/szenen/2/notizen", data={"text": "Dann der Hafen."})

    gelesen = notes.session(runde(pfad(tmp_path)), 1)
    assert [n.text for n in gelesen.scenes[0].notes] == ["Erst der Keller."]
    assert [n.text for n in gelesen.scenes[1].notes] == ["Dann der Hafen."]


def test_ohne_titel_steht_das_datum_da(tmp_path):
    client = klient(tmp_path)
    sitzung_anlegen(client)
    assert "2026-08-05" in client.get("/").get_data(as_text=True)


def test_ohne_datum_zaehlt_heute(tmp_path):
    client = klient(tmp_path)
    client.post("/", data={"played_on": "", "title": ""})
    assert notes.sessions(runde(pfad(tmp_path)))[0].played_on == notes.today()


def test_liste_zaehlt_szenen_und_notizen(tmp_path):
    client = klient(tmp_path)
    sitzung_anlegen(client, title="Zweiter Abend")
    client.post("/szenen/1/notizen", data={"text": "Eine Notiz."})
    html = client.get("/").get_data(as_text=True)
    assert "Zweiter Abend" in html
    assert "1 Szene, 1 Notiz" in html


def test_leere_liste_erklaert_sich(tmp_path):
    assert "Noch keine Sitzung" in klient(tmp_path).get("/").get_data(as_text=True)


def test_unbekannte_sitzung_ist_ein_404(tmp_path):
    assert klient(tmp_path).get("/sitzungen/99").status_code == 404


def test_szene_zu_unbekannter_sitzung_ist_ein_404(tmp_path):
    assert klient(tmp_path).post("/sitzungen/99/szenen", data={}).status_code == 404


def test_notiz_zu_unbekannter_szene_ist_ein_404(tmp_path):
    assert klient(tmp_path).post("/szenen/99/notizen", data={"text": "x"}).status_code == 404


def test_mitschreiben_geht_ohne_foundry(tmp_path):
    # Foundry kommt beim Mitschreiben nicht vor — eine tote Foundry darf nichts blockieren.
    client = klient(tmp_path)
    ort = sitzung_anlegen(client)
    client.post("/szenen/1/notizen", data={"text": "Ohne Foundry geht das auch."})
    assert "Ohne Foundry geht das auch." in client.get(ort).get_data(as_text=True)


def test_es_gibt_keinen_eigenen_diktierknopf(tmp_path):
    # Diktieren ist Tastatursache; ein eigener Aufnahmeknopf wäre der falsche Weg.
    client = klient(tmp_path)
    html = client.get(sitzung_anlegen(client)).get_data(as_text=True)
    assert "<textarea" in html
    assert "getUserMedia" not in html
    assert "MediaRecorder" not in html
