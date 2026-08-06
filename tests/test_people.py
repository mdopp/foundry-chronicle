"""Vorschlagen, bestätigen, beschriften.

Der Vorschlag darf nirgends wie eine Zuordnung aussehen — das ist hier die eigentliche
Prüfung. Alle Namen sind erfunden, wie überall in diesen Tests.
"""

import pytest
from conftest import UNSER_KONTO

from chronicle import consent, db, notes, people, recordings
from chronicle.app import create_app
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.foundry.world import project

STAND = "2026-08-06T20:00:00+00:00"
SPAETER = "2026-08-13T20:00:00+00:00"

MIRA = consent.Member(id="4001", name="Mira")
DAVEY = consent.Member(id="4002", name="Davey")

# Die beiden Foundry-Konten aus dem Weltabzug, die der Berechtigungsfilter übrig lässt.
MIRA_IN_FOUNDRY = "u-mira"
LEITUNG_IN_FOUNDRY = "u-leitung"


@pytest.fixture
def eingerichtet(config, welt):
    db.init(config.database_path)
    verbindung = db.connect(config.database_path)
    try:
        store.save(verbindung, project(welt, UNSER_KONTO, fetched_at=STAND))
    finally:
        verbindung.close()
    return config


def angesagt(config, *mitglieder, session_id=None):
    consent.record(
        config.database_path,
        session_id=session_id,
        kind=consent.ANSAGE,
        guild_id="g-1",
        channel_id="c-1",
        channel_name="Am Tisch",
        text="Ich schneide mit.",
        members=tuple(mitglieder),
    )


def gespeichert(config):
    verbindung = db.connect(config.database_path)
    try:
        return {
            z["discord_user_id"]: z["foundry_user_id"]
            for z in verbindung.execute("SELECT * FROM person_mapping")
        }
    finally:
        verbindung.close()


def spieler(*namen):
    return [people.Spieler(id=f"u-{name.lower()}", name=name) for name in namen]


def test_ein_gleicher_name_wird_vorgeschlagen():
    vorschlag = people.suggest("Mira", spieler("Mira", "Chronist"))

    assert vorschlag is not None
    assert vorschlag.name == "Mira"


def test_zwei_aehnlich_nahe_namen_ergeben_keinen_vorschlag():
    assert people.suggest("Miral", spieler("Mira", "Mirah")) is None


def test_ein_fremder_name_ergibt_keinen_vorschlag():
    assert people.suggest("Davey", spieler("Mira", "Chronist")) is None


def test_ohne_kandidaten_gibt_es_nichts_vorzuschlagen():
    assert people.suggest("Mira", []) is None


def test_ein_vorschlag_wird_nicht_gespeichert(eingerichtet):
    angesagt(eingerichtet, MIRA)

    (person,) = people.overview(eingerichtet.database_path).personen

    assert person.suggestion is not None
    assert person.suggestion.id == MIRA_IN_FOUNDRY
    assert person.confirmed is None
    assert gespeichert(eingerichtet) == {}


def test_ein_bereits_vergebener_spieler_wird_kein_zweites_mal_vorgeschlagen(eingerichtet):
    zweite_mira = consent.Member(id="4003", name="Mira")
    angesagt(eingerichtet, MIRA, zweite_mira)
    people.confirm(eingerichtet.database_path, {MIRA.id: MIRA_IN_FOUNDRY})

    nach_id = {p.discord_user_id: p for p in people.overview(eingerichtet.database_path).personen}

    assert nach_id[MIRA.id].confirmed.id == MIRA_IN_FOUNDRY
    assert nach_id[zweite_mira.id].suggestion is None


def test_bestaetigt_stehen_der_spielername_und_seine_figuren(eingerichtet):
    angesagt(eingerichtet, MIRA)

    people.confirm(eingerichtet.database_path, {MIRA.id: MIRA_IN_FOUNDRY})

    (person,) = people.overview(eingerichtet.database_path).personen
    assert person.confirmed.name == "Mira"
    assert person.confirmed.characters == ("Aelin Sturmwind",)
    assert person.suggestion is None


def test_keine_zuordnung_nimmt_eine_bestaetigung_zurueck(eingerichtet):
    angesagt(eingerichtet, MIRA)
    people.confirm(eingerichtet.database_path, {MIRA.id: MIRA_IN_FOUNDRY})

    people.confirm(eingerichtet.database_path, {MIRA.id: ""})

    assert gespeichert(eingerichtet) == {}


def test_der_zuletzt_protokollierte_anzeigename_gewinnt(eingerichtet):
    angesagt(eingerichtet, MIRA)
    angesagt(eingerichtet, consent.Member(id=MIRA.id, name="Mira am Handy"))

    (person,) = people.overview(eingerichtet.database_path).personen

    assert person.discord_name == "Mira am Handy"


def test_die_zuordnung_ueberlebt_einen_neuen_foundry_abgleich(eingerichtet, welt):
    angesagt(eingerichtet, MIRA)
    people.confirm(eingerichtet.database_path, {MIRA.id: MIRA_IN_FOUNDRY})

    verbindung = db.connect(eingerichtet.database_path)
    try:
        store.save(verbindung, project(welt, UNSER_KONTO, fetched_at=SPAETER))
    finally:
        verbindung.close()

    (person,) = people.overview(eingerichtet.database_path).personen
    assert person.confirmed.name == "Mira"


def test_die_zuordnung_speichert_keinen_namen(config):
    db.init(config.database_path)
    verbindung = db.connect(config.database_path)
    try:
        spalten = {z["name"] for z in verbindung.execute("PRAGMA table_info(person_mapping)")}
    finally:
        verbindung.close()

    assert spalten == {"discord_user_id", "foundry_user_id", "confirmed_at"}


def test_die_seite_uebernimmt_den_vorschlag_erst_nach_dem_absenden(eingerichtet):
    angesagt(eingerichtet, MIRA)
    client = create_app(eingerichtet).test_client()

    html = client.get("/zuordnung").get_data(as_text=True)
    assert "Vorschlag" in html
    assert gespeichert(eingerichtet) == {}

    client.post("/zuordnung", data={people.FELD + MIRA.id: MIRA_IN_FOUNDRY})

    assert gespeichert(eingerichtet) == {MIRA.id: MIRA_IN_FOUNDRY}


def test_ohne_eindeutigen_vorschlag_sagt_die_seite_das_auch(eingerichtet):
    angesagt(eingerichtet, DAVEY)

    html = create_app(eingerichtet).test_client().get("/zuordnung").get_data(as_text=True)

    assert "Kein eindeutiger Vorschlag" in html
    assert "Davey" in html


def test_ein_wert_ausserhalb_der_liste_wird_nicht_gespeichert(eingerichtet):
    angesagt(eingerichtet, MIRA)
    client = create_app(eingerichtet).test_client()

    client.post("/zuordnung", data={people.FELD + MIRA.id: LEITUNG_IN_FOUNDRY})

    assert gespeichert(eingerichtet) == {}


def test_ohne_foundry_stand_erklaert_die_seite_was_fehlt(config):
    db.init(config.database_path)
    angesagt(config, MIRA)

    html = create_app(config).test_client().get("/zuordnung").get_data(as_text=True)

    assert "Kein Foundry-Stand" in html


def test_ohne_aufnahme_steht_da_wovon_die_liste_lebt(eingerichtet):
    html = create_app(eingerichtet).test_client().get("/zuordnung").get_data(as_text=True)

    assert "Noch niemand aufgenommen" in html


def test_die_spur_traegt_den_foundry_namen_sobald_bestaetigt(eingerichtet):
    sitzung_id = notes.create_session(
        eingerichtet.database_path, played_on="2026-08-06", title="Der Keller"
    )
    angesagt(eingerichtet, MIRA, session_id=sitzung_id)
    recordings.enqueue(
        eingerichtet.database_path, sitzung_id, "sitzung1-Mira.wav", discord_user_id=MIRA.id
    )
    people.confirm(eingerichtet.database_path, {MIRA.id: MIRA_IN_FOUNDRY})

    seite = create_app(eingerichtet).test_client().get(f"/sitzungen/{sitzung_id}")

    html = seite.get_data(as_text=True)
    assert "Gesprochen von <strong>Mira</strong>" in html
    assert "Aelin Sturmwind" in html


def test_eine_unzugeordnete_spur_zeigt_den_discord_namen_und_den_weg_dorthin(eingerichtet):
    sitzung_id = notes.create_session(
        eingerichtet.database_path, played_on="2026-08-06", title="Der Keller"
    )
    angesagt(eingerichtet, DAVEY, session_id=sitzung_id)
    recordings.enqueue(
        eingerichtet.database_path, sitzung_id, "sitzung1-Davey.wav", discord_user_id=DAVEY.id
    )

    html = (
        create_app(eingerichtet)
        .test_client()
        .get(f"/sitzungen/{sitzung_id}")
        .get_data(as_text=True)
    )

    assert "Gesprochen von Davey" in html
    assert "noch nicht zugeordnet" in html
    assert "/zuordnung" in html


def test_ein_diktat_ohne_sprecher_bekommt_keine_beschriftung(eingerichtet):
    sitzung_id = notes.create_session(
        eingerichtet.database_path, played_on="2026-08-06", title="Der Keller"
    )
    recordings.enqueue(eingerichtet.database_path, sitzung_id, "sitzung1-heimweg.m4a")

    html = (
        create_app(eingerichtet)
        .test_client()
        .get(f"/sitzungen/{sitzung_id}")
        .get_data(as_text=True)
    )

    assert "Gesprochen von" not in html


def test_ohne_remote_user_bleibt_die_zuordnung_zu(tmp_path):
    client = create_app(Config(data_dir=tmp_path, require_remote_user=True)).test_client()

    assert client.get("/zuordnung").status_code == 403
    assert client.post("/zuordnung", data={}).status_code == 403
