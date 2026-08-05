import pytest
from conftest import UNSER_KONTO

from chronicle import db
from chronicle.foundry import store
from chronicle.foundry.model import Character, ChatMessage, Die, Player, Roll, WorldSnapshot
from chronicle.foundry.world import project

STAND = "2026-08-05T20:00:00+00:00"


@pytest.fixture
def connection(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    verbindung = db.connect(pfad)
    yield verbindung
    verbindung.close()


def test_schemastand_steigt_auch_in_einer_bestehenden_datei(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    verbindung = db.connect(pfad)
    verbindung.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    verbindung.commit()
    verbindung.close()
    db.init(pfad)
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_ohne_abgleich_gibt_es_keinen_stand(connection):
    assert store.load(connection) is None
    assert store.last_failure(connection) == (None, None)


def test_abzug_ueberlebt_den_umweg_durch_sqlite(connection, welt):
    abzug = project(welt, UNSER_KONTO, fetched_at=STAND)
    store.save(connection, abzug)
    geladen = store.load(connection)
    assert geladen.system == abzug.system
    assert geladen.fetched_at == STAND
    assert set(geladen.players) == set(abzug.players)
    assert set(geladen.characters) == set(abzug.characters)
    assert {n.id: n for n in geladen.messages} == {n.id: n for n in abzug.messages}


def test_wurfzahlen_bleiben_zahlen(connection, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    zeile = connection.execute(
        "SELECT roll_total, roll_formula FROM foundry_message WHERE id = 'm-wurf'"
    ).fetchone()
    assert zeile["roll_total"] == 7
    assert zeile["roll_formula"] == "1d12 + 1d12 + 3"


def test_ein_abgleich_ersetzt_den_vorigen(connection, welt):
    store.save(connection, project(welt, UNSER_KONTO, fetched_at=STAND))
    leer = WorldSnapshot(system="daggerheart", fetched_at="2026-08-06T20:00:00+00:00")
    store.save(connection, leer)
    geladen = store.load(connection)
    assert geladen.players == ()
    assert geladen.characters == ()
    assert geladen.messages == ()
    assert geladen.fetched_at == "2026-08-06T20:00:00+00:00"


def test_ein_fehler_wird_gemerkt_und_von_einem_erfolg_geloescht(connection):
    store.record_failure(connection, "Foundry antwortet nicht", STAND)
    assert store.last_failure(connection) == ("Foundry antwortet nicht", STAND)
    store.save(connection, WorldSnapshot(system="daggerheart", fetched_at=STAND))
    assert store.last_failure(connection) == (None, None)


def test_alle_felder_eines_wurfs_kommen_zurueck(connection):
    nachricht = ChatMessage(
        id="m-1",
        timestamp=1,
        roll=Roll(
            title="Duality",
            total=12,
            formula="2d12",
            kind="dualityRoll",
            critical=True,
            modifier_total=0,
            dice=(Die(name="hope", faces="d12", value=6),),
        ),
    )
    store.save(
        connection,
        WorldSnapshot(
            system="daggerheart",
            fetched_at=STAND,
            players=(Player(id="u-1", name="Mira", role=1, is_gm=False),),
            characters=(Character(id="a-1", name="Aelin", type="character", owner_ids=("u-1",)),),
            messages=(nachricht,),
        ),
    )
    assert store.load(connection).messages[0] == nachricht
