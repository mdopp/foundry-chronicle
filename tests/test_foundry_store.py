from dataclasses import replace

import pytest
from conftest import GM_SZENE, UNSER_KONTO, runde

from chronicle import db
from chronicle.foundry import store
from chronicle.foundry.model import Character, ChatMessage, Die, Player, Roll, WorldSnapshot
from chronicle.foundry.world import project

STAND = "2026-08-05T20:00:00+00:00"
SPAETER = "2026-08-06T20:00:00+00:00"
NOCH_SPAETER = "2026-08-07T20:00:00+00:00"


def leer(fetched_at):
    return WorldSnapshot(system="daggerheart", fetched_at=fetched_at)


def mit(fetched_at, *nachrichten):
    return WorldSnapshot(system="daggerheart", fetched_at=fetched_at, messages=nachrichten)


@pytest.fixture
def scope(tmp_path):
    """Der gescopte Zugang einer Runde — den rohen gibt es für die Datenschicht nicht."""
    zugang = db.scoped(runde(tmp_path / "chronicle.sqlite3"))
    yield zugang
    zugang.close()


def test_schemastand_steigt_auch_in_einer_bestehenden_datei(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    verbindung = db.connect(pfad)
    verbindung.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    verbindung.commit()
    verbindung.close()
    db.init(pfad)
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_ohne_abgleich_gibt_es_keinen_stand(scope):
    assert store.load(scope) is None
    assert store.last_failure(scope) == (None, None)


def test_abzug_ueberlebt_den_umweg_durch_sqlite(scope, welt):
    abzug = project(welt, UNSER_KONTO, fetched_at=STAND)
    store.save(scope, abzug)
    geladen = store.load(scope)
    assert geladen.system == abzug.system
    assert geladen.fetched_at == STAND
    assert set(geladen.players) == set(abzug.players)
    assert set(geladen.characters) == set(abzug.characters)
    assert {n.id: n for n in geladen.messages} == {n.id: n for n in abzug.messages}
    assert set(geladen.scenes) == set(abzug.scenes)


def test_wurfzahlen_bleiben_zahlen(scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    zeile = scope.execute(
        "SELECT roll_total, roll_formula FROM foundry_message WHERE runde_id = ? AND id = 'm-wurf'",
        (scope.runde_id,),
    ).fetchone()
    assert zeile["roll_total"] == 7
    assert zeile["roll_formula"] == "1d12 + 1d12 + 3"


def test_ein_abgleich_ersetzt_die_spiegel(scope, welt):
    """Konten, Figuren und Karten sind Spiegel: ihr aktueller Stand steht in Foundry."""
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    store.save(scope, leer(SPAETER))
    geladen = store.load(scope)
    assert geladen.players == ()
    assert geladen.characters == ()
    assert geladen.scenes == ()
    assert geladen.fetched_at == SPAETER


def test_die_aktive_karte_bleibt_die_aktive(scope, welt):
    """Nur *eine* Karte liegt — und nach dem Umweg durch SQLite ist es dieselbe."""
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    geladen = store.load(scope)
    assert [szene.id for szene in geladen.scenes if szene.active] == ["s-keller"]
    assert GM_SZENE not in [szene.name for szene in geladen.scenes]


def test_ein_geleertes_chat_log_nimmt_die_belege_nicht_mit(scope, welt):
    """Der Kern von #61: die Spielleitung leert das Log, der Wurf bleibt trotzdem belegt."""
    abzug = project(welt, UNSER_KONTO, fetched_at=STAND)
    store.save(scope, abzug)
    store.save(scope, leer(SPAETER))
    geladen = store.load(scope)
    assert [n.id for n in geladen.messages] == ["m-aufbruch", "m-wurf", "m-fluester-an-uns"]
    assert {n.id: n for n in geladen.messages}["m-wurf"].roll.total == 7
    assert all(n.vanished_at == SPAETER for n in geladen.messages)


def test_ein_zweiter_abgleich_ergaenzt_die_nachrichten(scope):
    erste = ChatMessage(id="m-1", timestamp=1000, content="Wir brechen auf.")
    zweite = ChatMessage(id="m-2", timestamp=2000, content="Der Keller steht offen.")
    store.save(scope, mit(STAND, erste))
    store.save(scope, mit(SPAETER, zweite))
    geladen = store.load(scope)
    assert [n.id for n in geladen.messages] == ["m-1", "m-2"]
    assert geladen.messages[0].vanished_at == SPAETER
    assert geladen.messages[1].vanished_at is None


def test_der_vermerk_nennt_den_ersten_abgleich_ohne_die_nachricht(scope):
    store.save(scope, mit(STAND, ChatMessage(id="m-1", timestamp=1000, content="Wir brechen auf.")))
    store.save(scope, leer(SPAETER))
    store.save(scope, leer(NOCH_SPAETER))
    assert store.load(scope).messages[0].vanished_at == SPAETER


def test_eine_wieder_aufgetauchte_nachricht_verliert_den_vermerk(scope):
    """Der Vermerk beschreibt die Gegenwart — er bliebe sonst als Unwahrheit stehen."""
    nachricht = ChatMessage(id="m-1", timestamp=1000, content="Wir brechen auf.")
    store.save(scope, mit(STAND, nachricht))
    store.save(scope, leer(SPAETER))
    store.save(scope, mit(NOCH_SPAETER, nachricht))
    assert store.load(scope).messages[0].vanished_at is None


def test_solange_es_die_nachricht_gibt_gewinnt_foundry(scope):
    """Die Id ist stabil, also ist es dasselbe Ereignis: eine Korrektur dort gilt auch hier."""
    store.save(scope, mit(STAND, ChatMessage(id="m-1", timestamp=1000, content="Wir brechen auf.")))
    store.save(scope, mit(SPAETER, ChatMessage(id="m-1", timestamp=1000, content="Wir bleiben.")))
    assert [n.content for n in store.load(scope).messages] == ["Wir bleiben."]


def test_ein_erneuter_abgleich_aendert_nichts(scope, welt):
    abzug = project(welt, UNSER_KONTO, fetched_at=STAND)
    store.save(scope, abzug)
    erst = store.load(scope)
    store.save(scope, replace(abzug, fetched_at=SPAETER))
    assert store.load(scope) == replace(erst, fetched_at=SPAETER)


def test_ein_fehler_wird_gemerkt_und_von_einem_erfolg_geloescht(scope):
    store.record_failure(scope, "Foundry antwortet nicht", STAND)
    assert store.last_failure(scope) == ("Foundry antwortet nicht", STAND)
    store.save(scope, leer(STAND))
    assert store.last_failure(scope) == (None, None)


def test_alle_felder_eines_wurfs_kommen_zurueck(scope):
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
        scope,
        WorldSnapshot(
            system="daggerheart",
            fetched_at=STAND,
            players=(Player(id="u-1", name="Mira", role=1, is_gm=False),),
            characters=(Character(id="a-1", name="Aelin", type="character", owner_ids=("u-1",)),),
            messages=(nachricht,),
        ),
    )
    assert store.load(scope).messages[0] == nachricht
