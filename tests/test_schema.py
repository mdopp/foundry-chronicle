import sqlite3
from pathlib import Path

import pytest
from conftest import UNSER_KONTO, runde

from chronicle import db
from chronicle.foundry import store
from chronicle.foundry.world import project

STAND = "2026-08-05T20:00:00+00:00"
SPAETER = "2026-08-12T20:00:00+00:00"


@pytest.fixture
def scope(tmp_path):
    zugang = db.scoped(runde(tmp_path / "chronicle.sqlite3"))
    yield zugang
    zugang.close()


def sitzung(scope, *, played_on="2026-08-05", title="Der Keller"):
    zeiger = scope.execute(
        "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
        (scope.runde_id, played_on, title, STAND),
    )
    return zeiger.lastrowid


def szene(scope, sitzung_id, *, position=1, title="Aufbruch"):
    zeiger = scope.execute(
        "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, sitzung_id, position, title, STAND),
    )
    return zeiger.lastrowid


def notiz(scope, szene_id, text):
    scope.execute(
        "INSERT INTO note (runde_id, scene_id, text, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, szene_id, text, STAND, STAND),
    )


def test_eine_sitzung_hat_szenen_und_eine_szene_hat_notizen(scope):
    sitzung_id = sitzung(scope)
    erste = szene(scope, sitzung_id, position=1, title="Aufbruch")
    zweite = szene(scope, sitzung_id, position=2, title="Der Keller")
    notiz(scope, erste, "Wir brechen bei Sonnenaufgang auf.")
    notiz(scope, zweite, "Die Wirtin warnt vor dem Keller.")

    zeilen = scope.execute(
        "SELECT scene.title AS szene, note.text AS text FROM note "
        "JOIN scene ON scene.id = note.scene_id "
        "WHERE scene.runde_id = ? AND scene.session_id = ? ORDER BY scene.position, note.id",
        (scope.runde_id, sitzung_id),
    ).fetchall()
    assert [(z["szene"], z["text"]) for z in zeilen] == [
        ("Aufbruch", "Wir brechen bei Sonnenaufgang auf."),
        ("Der Keller", "Die Wirtin warnt vor dem Keller."),
    ]


def test_szenenreihenfolge_ist_je_sitzung_eindeutig(scope):
    sitzung_id = sitzung(scope)
    szene(scope, sitzung_id, position=1)
    with pytest.raises(sqlite3.IntegrityError):
        szene(scope, sitzung_id, position=1, title="Noch einmal die erste")


def test_dieselbe_position_in_einer_anderen_sitzung_ist_erlaubt(scope):
    erste = sitzung(scope, played_on="2026-08-05")
    zweite = sitzung(scope, played_on="2026-08-12")
    szene(scope, erste, position=1)
    szene(scope, zweite, position=1)


def test_notiz_ohne_szene_wird_abgewiesen(scope):
    with pytest.raises(sqlite3.IntegrityError):
        notiz(scope, 999, "Notiz ins Leere")


def test_loeschen_einer_sitzung_raeumt_szenen_notizen_und_protokolle_ab(scope):
    sitzung_id = sitzung(scope)
    szene_id = szene(scope, sitzung_id)
    notiz(scope, szene_id, "Wir brechen auf.")
    scope.execute(
        "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, sitzung_id, "chronik", "Es war einmal.", STAND),
    )
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) VALUES (?, ?, ?)",
        (scope.runde_id, szene_id, "m-wurf"),
    )

    scope.execute("DELETE FROM session WHERE runde_id = ? AND id = ?", (scope.runde_id, sitzung_id))

    for tabelle in ("scene", "note", "protocol", "scene_foundry_message"):
        anzahl = scope.execute(
            f"SELECT COUNT(*) FROM {tabelle} WHERE runde_id = ?", (scope.runde_id,)
        ).fetchone()[0]
        assert anzahl == 0


def test_je_sitzung_gibt_es_chronik_und_rueckblick_genau_einmal(scope):
    sitzung_id = sitzung(scope)
    for art in ("chronik", "rueckblick"):
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.runde_id, sitzung_id, art, "Text", STAND),
        )
    with pytest.raises(sqlite3.IntegrityError):
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.runde_id, sitzung_id, "chronik", "Zweite Chronik", SPAETER),
        )


def test_eine_unbekannte_protokollart_wird_abgewiesen(scope):
    sitzung_id = sitzung(scope)
    with pytest.raises(sqlite3.IntegrityError):
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (scope.runde_id, sitzung_id, "zusammenfassung", "Text", STAND),
        )


def test_foundry_fakt_haengt_ueber_die_szene_an_der_notiz(scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(scope)
    szene_id = szene(scope, sitzung_id)
    notiz(scope, szene_id, "Brok prüft, was er über den Keller weiß.")
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) VALUES (?, ?, ?)",
        (scope.runde_id, szene_id, "m-wurf"),
    )

    zeile = scope.execute(
        "SELECT foundry_message.speaker_actor AS figur, foundry_message.roll_total AS summe "
        "FROM scene_foundry_message "
        "JOIN foundry_message ON foundry_message.id = scene_foundry_message.message_id "
        "AND foundry_message.runde_id = scene_foundry_message.runde_id "
        "WHERE scene_foundry_message.runde_id = ? AND scene_foundry_message.scene_id = ?",
        (scope.runde_id, szene_id),
    ).fetchone()
    assert (zeile["figur"], zeile["summe"]) == ("a-brok", 7)


def test_zuordnung_ueberlebt_einen_neuen_foundry_abgleich(scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    szene_id = szene(scope, sitzung(scope))
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) VALUES (?, ?, ?)",
        (scope.runde_id, szene_id, "m-wurf"),
    )

    store.save(scope, project(welt, UNSER_KONTO, fetched_at=SPAETER))

    zeile = scope.execute(
        "SELECT foundry_message.roll_total AS summe FROM scene_foundry_message "
        "JOIN foundry_message ON foundry_message.id = scene_foundry_message.message_id "
        "AND foundry_message.runde_id = scene_foundry_message.runde_id "
        "WHERE scene_foundry_message.runde_id = ? AND scene_foundry_message.scene_id = ?",
        (scope.runde_id, szene_id),
    ).fetchone()
    assert zeile["summe"] == 7


def test_ein_protokoll_aus_schema_11_bekommt_die_zustellspalte_nachgetragen(tmp_path):
    # ``CREATE TABLE IF NOT EXISTS`` erreicht eine bestehende Tabelle nicht — hier steht
    # deshalb die Fassung vor Schema 12, samt einem Rückblick, der noch nie zugestellt war.
    pfad = tmp_path / "alt.sqlite3"
    verbindung = db.connect(pfad)
    try:
        with verbindung:
            verbindung.execute(
                "CREATE TABLE protocol (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, "
                "kind TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, "
                "UNIQUE (session_id, kind))"
            )
            verbindung.execute(
                "INSERT INTO protocol (session_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
                (1, "rueckblick", "Die Runde tastete sich voran.", STAND),
            )
    finally:
        verbindung.close()

    db.init(pfad)

    verbindung = db.connect(pfad)
    try:
        zeile = verbindung.execute("SELECT text, delivered_at FROM protocol").fetchone()
    finally:
        verbindung.close()
    assert zeile["delivered_at"] is None
    assert zeile["text"] == "Die Runde tastete sich voran."
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_ein_archiv_von_vor_der_testweltspalte_gilt_als_echt(tmp_path):
    """Nachgetragen wird mit ``NOT NULL DEFAULT 0`` — beides zusammen oder gar nicht.

    Ohne die Vorgabe wiese SQLite das ``ALTER TABLE`` ab, und ohne das ``NOT NULL`` stünde
    in den Bestandszeilen ``NULL``: der Bedingung ``aus_testwelt = 0`` genügte dann keine
    einzige, und der nächste Abgleich erklärte ein gewachsenes Archiv für unberührbar.
    """
    pfad = tmp_path / "alt.sqlite3"
    verbindung = db.connect(pfad)
    try:
        with verbindung:
            verbindung.execute(
                "CREATE TABLE foundry_message (runde_id INTEGER NOT NULL, id TEXT NOT NULL, "
                "timestamp INTEGER NOT NULL, content TEXT NOT NULL, vanished_at TEXT, "
                "PRIMARY KEY (runde_id, id))"
            )
            verbindung.execute(
                "INSERT INTO foundry_message (runde_id, id, timestamp, content) "
                "VALUES (1, 'm-alt', 1000, 'Ein Wurf von damals.')"
            )
    finally:
        verbindung.close()

    db.init(pfad)

    verbindung = db.connect(pfad)
    try:
        zeile = verbindung.execute(
            "SELECT content, aus_testwelt FROM foundry_message WHERE aus_testwelt = 0"
        ).fetchone()
    finally:
        verbindung.close()
    assert zeile["content"] == "Ein Wurf von damals."
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_eine_sitzung_von_vor_der_kennung_bekommt_die_spalte_und_ihren_wert(tmp_path):
    """``CREATE TABLE IF NOT EXISTS`` erreicht eine bestehende Tabelle nicht — ohne den
    Eintrag in ``NACHGETRAGEN`` stünde die laufende Instanz nach dem Aufspielen ohne die
    Spalte da, und schon ``notes.sessions`` fiele über sie. Der Wert muss mitkommen: eine
    Sitzung ohne Kennung ist über ``/chronik sitzung-loeschen`` nicht mehr erreichbar.
    """
    pfad = tmp_path / "alt.sqlite3"
    verbindung = db.connect(pfad)
    try:
        with verbindung:
            verbindung.execute(
                "CREATE TABLE session (id INTEGER PRIMARY KEY, runde_id INTEGER NOT NULL, "
                "played_on TEXT NOT NULL, title TEXT, created_at TEXT NOT NULL, "
                "thread_id TEXT, UNIQUE (id, runde_id))"
            )
            verbindung.executemany(
                "INSERT INTO session (id, runde_id, played_on, created_at) VALUES (?, 1, ?, ?)",
                ((1, "2026-05-01", STAND), (2, "2026-05-08", SPAETER)),
            )
    finally:
        verbindung.close()

    db.init(pfad)

    verbindung = db.connect(pfad)
    try:
        kennungen = [
            zeile["token"] for zeile in verbindung.execute("SELECT token FROM session ORDER BY id")
        ]
    finally:
        verbindung.close()
    assert all(kennungen)
    assert len(set(kennungen)) == 2
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


BESTAND = Path(__file__).with_name("bestand.sql")


def tabellenform(pfad):
    """Jede Tabelle mit ihren Spalten, wie SQLite sie erklärt — ohne Reihenfolge."""
    verbindung = db.connect(pfad)
    try:
        namen = [
            zeile["name"]
            for zeile in verbindung.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            name: {
                zeile["name"]: (zeile["type"], zeile["notnull"], zeile["dflt_value"], zeile["pk"])
                for zeile in verbindung.execute(f"PRAGMA table_info({name})")
            }
            for name in namen
        }
    finally:
        verbindung.close()


def test_eine_gewachsene_datenbank_kommt_beim_start_auf_die_form_einer_frischen(tmp_path):
    """Der Fall, für den ``NACHGETRAGEN`` überhaupt existiert — und der sonst nirgends vorkommt.

    Jeder andere Test beginnt bei einer frischen Datenbank, in der ``schema.sql`` alle
    Spalten ohnehin anlegt; ein vergessener Nachtrag fällt dort nie auf. Hier startet der
    Lauf stattdessen auf ``bestand.sql`` — einer eingefrorenen Momentaufnahme dessen, was
    auf der laufenden Instanz steht. Dass ihr die Spalten wirklich fehlen, liegt nicht an
    diesem Test: ``schema.sql`` legt seine Tabellen mit ``IF NOT EXISTS`` an und rührt eine
    bestehende nicht mehr an, eine Spalte kann also nur über einen Nachtrag kommen.

    Entscheidend ist, dass die Momentaufnahme **unabhängig** von ``NACHGETRAGEN`` ist. Eine
    Vorlage, die sich ihre fehlenden Spalten aus der Liste selbst ableitet, prüft nichts:
    ein gestrichener Eintrag fehlte dann auch der Vorlage, die Spalte wäre nie fort und der
    Test bliebe grün. Weil ``bestand.sql`` daneben steht und sich nicht bewegt, macht das
    Streichen eines Eintrags ihn rot — und eine Spalte, die nächste Woche zu einer
    bestehenden Tabelle kommt, ist ohne weiteres Zutun mitgeprüft.

    Gewandert wird über ``db.init``, damit die Reihenfolge der Schritte die der laufenden
    Instanz ist und keine hier gewählte. Verglichen wird die Form, nicht der Inhalt: was
    eine nachgetragene Spalte in den Bestandszeilen tragen muss, hängt am einzelnen Eintrag
    und steht bei ihm — die beiden Tests darüber sind die Fälle, die es bisher gibt.

    Eine Tabelle deckt dieser Test nicht: ``runde`` wird auf dieser Momentaufnahme ganz neu
    gebaut, weil ihr die Kennung fehlt, und bekommt ihre Spalten dabei aus ``schema.sql``
    statt aus ``NACHGETRAGEN``. Der Test darunter setzt genau dort an.
    """
    gewachsen = tmp_path / "bestand.sqlite3"
    verbindung = db.connect(gewachsen)
    try:
        verbindung.executescript(BESTAND.read_text(encoding="utf-8"))
        verbindung.commit()
    finally:
        verbindung.close()
    frisch = tmp_path / "frisch.sqlite3"
    db.init(frisch)
    # Wäre die Momentaufnahme irgendwann an das heutige Schema angeglichen worden, fehlte
    # ihr keine Spalte mehr und der Vergleich unten ginge auf, ohne je etwas zu wandern.
    vorher, vorbild = tabellenform(gewachsen), tabellenform(frisch)
    assert {
        name: sorted(set(vorbild[name]) - set(spalten))
        for name, spalten in vorher.items()
        if name in vorbild and set(vorbild[name]) - set(spalten)
    }

    db.init(gewachsen)

    nachher = tabellenform(gewachsen)
    for name in sorted(set(nachher) | set(vorbild)):
        assert nachher.get(name) == vorbild.get(name), name
    assert db.current_schema_version(gewachsen) == db.SCHEMA_VERSION


def test_eine_runde_die_ihre_kennung_schon_hat_bekommt_neue_spalten_einzeln(tmp_path):
    """``runde`` ist die einzige Tabelle, deren Nachtrag zweimal aussieht — je nach Alter.

    Fehlt die Kennung, baut ``_kennung_nachtragen`` die Tabelle ganz neu und füllt sie aus
    ``schema.sql``; jede fehlende Spalte kommt dabei mit, ob sie in ``NACHGETRAGEN`` steht
    oder nicht. Diesseits dieses Umbaus — und dort steht jede Instanz, sobald sie einmal
    gestartet ist — fällt dieser Weg weg, und eine neue Spalte an ``runde`` kann nur noch
    über die Liste kommen. Genau dieser Stand wird hier gebaut: die Momentaufnahme, einmal
    gewandert, dann wieder auf ihre damaligen Spalten zurückgesetzt — die Kennung
    ausgenommen, denn sie ist der Schalter, der über den Umbau entscheidet. Welche Spalten
    »damals« waren, sagt ``bestand.sql`` und nicht ``NACHGETRAGEN``; sonst prüfte der Test
    die Liste an sich selbst und ein gestrichener Eintrag fiele wieder niemandem auf.
    """
    pfad = tmp_path / "bestand.sqlite3"
    verbindung = db.connect(pfad)
    try:
        verbindung.executescript(BESTAND.read_text(encoding="utf-8"))
        verbindung.commit()
    finally:
        verbindung.close()
    damals = set(tabellenform(pfad)["runde"])
    frisch = tmp_path / "frisch.sqlite3"
    db.init(frisch)
    db.init(pfad)

    seither = sorted(set(tabellenform(frisch)["runde"]) - damals - {"token"})
    assert seither
    verbindung = db.connect(pfad)
    try:
        for spalte in seither:
            verbindung.execute(f"ALTER TABLE runde DROP COLUMN {spalte}")
        verbindung.commit()
    finally:
        verbindung.close()

    db.init(pfad)

    assert tabellenform(pfad)["runde"] == tabellenform(frisch)["runde"]


def test_eine_datenbank_ohne_die_neue_laufart_bekommt_sie_nachgetragen(tmp_path):
    """``CHECK (kind IN …)`` steht im ``CREATE TABLE`` und wandert mit keinem ``ALTER`` mit.

    Ohne diese Wanderung wiese eine bestehende Instanz die Nacherzählung beim Anlegen des
    Laufs ab — und zwar erst im Befehl, nicht beim Start.
    """
    pfad = tmp_path / "chronicle.sqlite3"
    zugang = db.scoped(runde(pfad))
    with zugang:
        zugang.execute(
            "INSERT INTO job (runde_id, kind, state, started_at) VALUES (?, 'abgleich', ?, ?)",
            (zugang.runde_id, "fertig", STAND),
        )
        # Die Fassung von vorher: dieselbe Tabelle, nur ohne die neue Art.
        zugang.execute("PRAGMA legacy_alter_table = ON")
        zugang.execute("ALTER TABLE job RENAME TO job__vorher")
        zugang.execute(
            "CREATE TABLE job (id INTEGER PRIMARY KEY, runde_id INTEGER NOT NULL "
            "REFERENCES runde (id) ON DELETE CASCADE, kind TEXT NOT NULL "
            "CHECK (kind IN ('abgleich', 'chronik', 'nachtlauf')), session_id INTEGER, "
            "state TEXT NOT NULL CHECK (state IN ('laeuft', 'fertig', 'gescheitert')), "
            "started_at TEXT NOT NULL, finished_at TEXT, result TEXT, error TEXT)"
        )
        zugang.execute(
            "INSERT INTO job (id, runde_id, kind, session_id, state, started_at, finished_at, "
            "result, error) SELECT id, runde_id, kind, session_id, state, started_at, "
            "finished_at, result, error FROM job__vorher WHERE runde_id = ?",
            (zugang.runde_id,),
        )
        zugang.execute("DROP TABLE job__vorher")
    runde_id = zugang.runde_id
    zugang.close()

    db.init(pfad)

    verbindung = db.connect(pfad)
    try:
        with verbindung:
            verbindung.execute(
                "INSERT INTO job (runde_id, kind, state, started_at) "
                "VALUES (?, 'nacherzaehlung', 'laeuft', ?)",
                (runde_id, STAND),
            )
        arten = [zeile["kind"] for zeile in verbindung.execute("SELECT kind FROM job ORDER BY id")]
    finally:
        verbindung.close()
    assert arten == ["abgleich", "nacherzaehlung"]
    assert db.current_schema_version(pfad) == db.SCHEMA_VERSION


def test_ein_zweiter_lauf_des_schemas_laesst_die_daten_stehen(tmp_path):
    pfad = tmp_path / "chronicle.sqlite3"
    zugang = db.scoped(runde(pfad))
    with zugang:
        sitzung_id = sitzung(zugang, title="Der Krumme Ast")
        notiz(zugang, szene(zugang, sitzung_id), "Wir brechen auf.")
    zugang.close()

    db.init(pfad)

    zugang = db.scoped(runde(pfad))
    try:
        assert db.current_schema_version(pfad) == db.SCHEMA_VERSION
        titel = zugang.execute(
            "SELECT title FROM session WHERE runde_id = ?", (zugang.runde_id,)
        ).fetchone()["title"]
        assert titel == "Der Krumme Ast"
        anzahl = zugang.execute(
            "SELECT COUNT(*) FROM note WHERE runde_id = ?", (zugang.runde_id,)
        ).fetchone()[0]
        assert anzahl == 1
    finally:
        zugang.close()
