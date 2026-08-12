"""Vorschlagen, bestätigen, beschriften.

Der Vorschlag darf nirgends wie eine Zuordnung aussehen — das ist hier die eigentliche
Prüfung. Alle Namen sind erfunden, wie überall in diesen Tests.
"""

import pytest
from conftest import UNSER_KONTO, runde

from chronicle import consent, db, notes, people, recordings
from chronicle.bot import erinnern
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
    scope = db.scoped(runde(config))
    try:
        store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    finally:
        scope.close()
    return config


def angesagt(config, *mitglieder, session_id=None):
    consent.record(
        runde(config),
        session_id=session_id,
        kind=consent.ANSAGE,
        guild_id="g-1",
        channel_id="c-1",
        channel_name="Am Tisch",
        text="Ich schneide mit.",
        members=tuple(mitglieder),
    )


def gespeichert(config):
    scope = db.scoped(runde(config))
    try:
        return {
            z["discord_user_id"]: z["foundry_user_id"]
            for z in scope.execute(
                "SELECT * FROM person_mapping WHERE runde_id = ?", (scope.runde_id,)
            )
        }
    finally:
        scope.close()


def spieler(*namen):
    return [people.Spieler(id=f"u-{name.lower()}", name=name) for name in namen]


def test_ein_gleicher_name_wird_vorgeschlagen():
    vorschlag = people.suggest("Mira", spieler("Mira", "Chronist"))

    assert vorschlag is not None
    assert vorschlag.name == "Mira"


def test_zwei_aehnlich_nahe_namen_ergeben_keinen_vorschlag():
    assert people.suggest("Miral", spieler("Mira", "Mirah")) is None


def test_wer_wie_seine_figur_heisst_wird_ebenso_vorgeschlagen():
    """Die einen heißen in Discord wie ihr Foundry-Konto, die anderen wie ihre Figur."""
    mit_figur = people.Spieler(id="u-mira", name="Mira", characters=("Aelin Sturmwind",))

    vorschlag = people.suggest("Aelin Sturmwind", [mit_figur, *spieler("Chronist")])

    assert vorschlag is mit_figur


def test_eine_figur_hebt_die_schwelle_nicht_auf():
    """Gegenprobe: die Figur zählt mit, sie wird aber nicht großzügiger behandelt."""
    mit_figur = people.Spieler(id="u-mira", name="Mira", characters=("Aelin Sturmwind",))

    assert people.suggest("Davey", [mit_figur]) is None


def test_ein_fremder_name_ergibt_keinen_vorschlag():
    assert people.suggest("Davey", spieler("Mira", "Chronist")) is None


def test_ohne_kandidaten_gibt_es_nichts_vorzuschlagen():
    assert people.suggest("Mira", []) is None


def test_ein_vorschlag_wird_nicht_gespeichert(eingerichtet):
    angesagt(eingerichtet, MIRA)

    (person,) = people.overview(runde(eingerichtet)).personen

    assert person.suggestion is not None
    assert person.suggestion.id == MIRA_IN_FOUNDRY
    assert person.confirmed is None
    assert gespeichert(eingerichtet) == {}


def test_ein_bereits_vergebener_spieler_wird_kein_zweites_mal_vorgeschlagen(eingerichtet):
    zweite_mira = consent.Member(id="4003", name="Mira")
    angesagt(eingerichtet, MIRA, zweite_mira)
    people.confirm(runde(eingerichtet), {MIRA.id: MIRA_IN_FOUNDRY})

    nach_id = {p.discord_user_id: p for p in people.overview(runde(eingerichtet)).personen}

    assert nach_id[MIRA.id].confirmed.id == MIRA_IN_FOUNDRY
    assert nach_id[zweite_mira.id].suggestion is None


def test_bestaetigt_stehen_der_spielername_und_seine_figuren(eingerichtet):
    angesagt(eingerichtet, MIRA)

    people.confirm(runde(eingerichtet), {MIRA.id: MIRA_IN_FOUNDRY})

    (person,) = people.overview(runde(eingerichtet)).personen
    assert person.confirmed.name == "Mira"
    assert person.confirmed.characters == ("Aelin Sturmwind",)
    assert person.suggestion is None


def test_keine_zuordnung_nimmt_eine_bestaetigung_zurueck(eingerichtet):
    angesagt(eingerichtet, MIRA)
    people.confirm(runde(eingerichtet), {MIRA.id: MIRA_IN_FOUNDRY})

    people.confirm(runde(eingerichtet), {MIRA.id: ""})

    assert gespeichert(eingerichtet) == {}


def test_der_zuletzt_protokollierte_anzeigename_gewinnt(eingerichtet):
    angesagt(eingerichtet, MIRA)
    angesagt(eingerichtet, consent.Member(id=MIRA.id, name="Mira am Handy"))

    (person,) = people.overview(runde(eingerichtet)).personen

    assert person.discord_name == "Mira am Handy"


def test_die_zuordnung_ueberlebt_einen_neuen_foundry_abgleich(eingerichtet, welt):
    angesagt(eingerichtet, MIRA)
    people.confirm(runde(eingerichtet), {MIRA.id: MIRA_IN_FOUNDRY})

    scope = db.scoped(runde(eingerichtet))
    try:
        store.save(scope, project(welt, UNSER_KONTO, fetched_at=SPAETER))
    finally:
        scope.close()

    (person,) = people.overview(runde(eingerichtet)).personen
    assert person.confirmed.name == "Mira"


def test_die_zuordnung_speichert_keinen_namen(config):
    db.init(config.database_path)
    verbindung = db.connect(config.database_path)
    try:
        spalten = {z["name"] for z in verbindung.execute("PRAGMA table_info(person_mapping)")}
    finally:
        verbindung.close()

    assert spalten == {"runde_id", "discord_user_id", "foundry_user_id", "confirmed_at"}


def test_der_vorschlag_wird_erst_mit_der_bestaetigung_zur_zuordnung(eingerichtet):
    angesagt(eingerichtet, MIRA)

    (person,) = people.overview(runde(eingerichtet)).personen
    assert person.suggestion.id == MIRA_IN_FOUNDRY
    assert gespeichert(eingerichtet) == {}

    people.confirm(runde(eingerichtet), {person.discord_user_id: person.suggestion.id})

    assert gespeichert(eingerichtet) == {MIRA.id: MIRA_IN_FOUNDRY}


def test_ohne_eindeutigen_vorschlag_steht_die_person_trotzdem_zur_wahl(eingerichtet):
    angesagt(eingerichtet, DAVEY)

    (person,) = people.overview(runde(eingerichtet)).personen

    assert person.discord_name == "Davey"
    assert person.suggestion is None


def test_ein_wert_ausserhalb_der_liste_wird_nicht_gespeichert(eingerichtet):
    """Die Spielleitung fällt schon im Berechtigungsfilter heraus — wählbar ist sie nie."""
    angesagt(eingerichtet, MIRA)

    uebersicht = people.overview(runde(eingerichtet))

    assert LEITUNG_IN_FOUNDRY not in {s.id for s in uebersicht.spieler}
    assert erinnern.zuordnen(runde(eingerichtet), MIRA.id, LEITUNG_IN_FOUNDRY) == (
        erinnern.SPIELER_WEG
    )
    assert gespeichert(eingerichtet) == {}


def test_ohne_foundry_stand_gibt_es_niemanden_zu_waehlen(config):
    db.init(config.database_path)
    angesagt(config, MIRA)

    uebersicht = people.overview(runde(config))

    assert uebersicht.spieler == ()
    assert [person.discord_name for person in uebersicht.personen] == ["Mira"]
    assert uebersicht.personen[0].suggestion is None


def test_ohne_aufnahme_ist_niemand_zuzuordnen(eingerichtet):
    assert people.overview(runde(eingerichtet)).personen == ()


def test_die_spur_traegt_den_foundry_namen_sobald_bestaetigt(eingerichtet):
    sitzung_id = notes.create_session(
        runde(eingerichtet), played_on="2026-08-06", title="Der Keller"
    )
    angesagt(eingerichtet, MIRA, session_id=sitzung_id)
    recordings.enqueue(
        runde(eingerichtet), sitzung_id, "sitzung1-Mira.wav", discord_user_id=MIRA.id
    )
    people.confirm(runde(eingerichtet), {MIRA.id: MIRA_IN_FOUNDRY})

    (aufnahme,) = recordings.for_session(runde(eingerichtet), sitzung_id)
    person = people.speakers(runde(eingerichtet))[aufnahme.discord_user_id]

    assert person.confirmed.name == "Mira"
    assert person.confirmed.characters == ("Aelin Sturmwind",)


def test_eine_unzugeordnete_spur_traegt_den_discord_namen(eingerichtet):
    sitzung_id = notes.create_session(
        runde(eingerichtet), played_on="2026-08-06", title="Der Keller"
    )
    angesagt(eingerichtet, DAVEY, session_id=sitzung_id)
    recordings.enqueue(
        runde(eingerichtet), sitzung_id, "sitzung1-Davey.wav", discord_user_id=DAVEY.id
    )

    (aufnahme,) = recordings.for_session(runde(eingerichtet), sitzung_id)
    person = people.speakers(runde(eingerichtet))[aufnahme.discord_user_id]

    assert person.discord_name == "Davey"
    assert person.confirmed is None


def test_ein_diktat_ohne_sprecher_bekommt_keine_beschriftung(eingerichtet):
    sitzung_id = notes.create_session(
        runde(eingerichtet), played_on="2026-08-06", title="Der Keller"
    )
    recordings.enqueue(runde(eingerichtet), sitzung_id, "sitzung1-heimweg.m4a")

    (aufnahme,) = recordings.for_session(runde(eingerichtet), sitzung_id)

    assert aufnahme.discord_user_id is None
    assert people.speakers(runde(eingerichtet)).get(aufnahme.discord_user_id) is None
