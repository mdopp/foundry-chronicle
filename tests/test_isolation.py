"""Das Dauergate: Runde A sieht nichts von Runde B.

Bis hierher gab es nichts zu trennen — eine Instanz, eine Gruppe. Ab dem Runden-Modell ist
die Trennung die wichtigste Sicherheitseigenschaft des Systems: ein vergessenes
``WHERE runde_id = ?`` ist kein Fehler, sondern ein Datenleck zwischen fremden Kampagnen.
Diese Datei ist deshalb kein Test *einer* Funktion, sondern eine Schranke vor der ganzen
Datenschicht, und sie ist so gebaut, dass eine **neue** Funktion ohne Runden-Bezug durch
sie hindurchfällt:

1. ``test_jede_gescopte_funktion_wird_geprueft`` zählt die öffentlichen Aufrufe der
   Schicht auf und verlangt für jeden einen Eintrag in ``ABFRAGEN`` oder ``SCHREIBER``.
   Wer eine Funktion hinzufügt und nicht prüft, bekommt hier rot.
2. ``test_signatur_verlangt_die_runde`` verlangt von jedem Aufruf, der Konfiguration oder
   Datenbank anfasst, die Runde als Argument — oder das Kennzeichen ``instanzweit``.
3. Der Rest legt **zwei** Runden mit gleichnamigen Sitzungen, Szenen, Notizen und
   Registereinträgen an und ruft jede lesende Funktion für A auf. Taucht irgendwo die
   Marke von B auf, ist das Gate rot.

Gleichnamig ist Absicht: eine Trennung, die nur über verschiedene Texte hält, hält nicht.
"""

from __future__ import annotations

import inspect
import sqlite3

import pytest
from conftest import UNSER_KONTO, WELT, warte_auf_laeufe

from chronicle import (
    consent,
    db,
    dokument,
    instanz,
    jobs,
    lebenszyklus,
    nightly,
    notes,
    people,
    protocol,
    recordings,
    register,
    search,
    settings,
    zugang,
)
from chronicle import runde as runden
from chronicle.bot import chronik as bot_chronik
from chronicle.bot import einrichten as bot_einrichten
from chronicle.bot import erinnern
from chronicle.compose import service as compose_service
from chronicle.config import Config
from chronicle.discord import ausgabe as discord_ausgabe
from chronicle.discord import rueckblick as discord_rueckblick
from chronicle.discord import service as discord_service
from chronicle.discord.client import Message
from chronicle.foundry import service as foundry_service
from chronicle.foundry import store as foundry_store
from chronicle.foundry.model import Character, ChatMessage, Player, Scene, WorldSnapshot
from chronicle.transcribe import merge
from chronicle.transcribe import service as transcribe_service

# Die Datenschicht. Was hier drinsteht, unterliegt dem Gate; ``chronicle.db`` und
# ``chronicle.runde`` sind ihr Fundament und ``chronicle.instanz`` gehört ausdrücklich der
# Instanz — die drei stehen deshalb nicht in der Liste, sondern haben eigene Tests.
#
# ``chronicle.zugang`` steht mit drin, obwohl es nichts speichert: es hält je Runde etwas
# vor, und ein Merkzettel, den die falsche Runde einlöst, wäre dasselbe Leck eine Etage
# höher.
SCHICHT = (
    notes,
    dokument,
    protocol,
    recordings,
    search,
    register,
    people,
    consent,
    jobs,
    nightly,
    settings,
    foundry_service,
    foundry_store,
    compose_service,
    transcribe_service,
    merge,
    discord_service,
    discord_rueckblick,
    discord_ausgabe,
    zugang,
    # Was der Bot auf einen Befehl hin antwortet, ist eine Abfrage wie jede andere: die
    # Suche und das Register gehen hier durch die Runde der fragenden Gilde.
    erinnern,
    # Und das Löschen erst recht: es ist der eine Aufruf, bei dem eine verwechselte Runde
    # nicht bloß etwas zeigt, was sie nicht zeigen darf, sondern etwas fortnimmt.
    lebenszyklus,
)

# Woran ein Aufruf erkannt wird, der Daten anfasst: er nimmt eine Konfiguration, einen
# Datenbankpfad, eine rohe Verbindung oder einen Scope entgegen.
DATENARGUMENTE = ("config", "database_path", "connection", "scope")

MARKE = {1: "Alpharunde", 2: "Betarunde"}


def _oeffentlich(modul) -> dict[str, object]:
    return {
        name: wert
        for name, wert in vars(modul).items()
        if not name.startswith("_")
        and (inspect.isfunction(wert) or inspect.iscoroutinefunction(wert))
        and wert.__module__ == modul.__name__
    }


def _gescopt(funktion) -> bool:
    namen = inspect.signature(funktion).parameters
    return "runde" in namen or "scope" in namen


def _schluessel(modul, name: str) -> str:
    return f"{modul.__name__.rsplit('.', 1)[-1]}.{name}"


def fuellen(config: Config, runde, marke: str) -> dict[str, int]:
    """Eine Runde mit einem vollständigen Satz Daten — überall dieselbe Marke."""
    sitzung = notes.create_session(
        runde, played_on="2026-05-01", title=f"Sitzung {marke}", thread_id=f"thread-{marke}"
    )
    szene = notes.session(runde, sitzung).scenes[0]
    notes.add_note(runde, szene.id, f"Im Keller stand {marke}.", message_id=f"nachricht-{marke}")

    scope = db.scoped(runde)
    try:
        foundry_store.save(
            scope,
            WorldSnapshot(
                system="daggerheart",
                fetched_at="2026-05-01T20:00:00+00:00",
                players=(Player(id="u-1", name=f"Spielerin {marke}", role=1, is_gm=False),),
                characters=(
                    Character(
                        id="a-1", name=f"Figur {marke}", type="character", owner_ids=("u-1",)
                    ),
                ),
                messages=(
                    ChatMessage(
                        id="m-1",
                        timestamp=1000,
                        speaker_actor="a-1",
                        speaker_alias=f"Figur {marke}",
                        content=f"Der Wurf von {marke}.",
                    ),
                ),
                scenes=(Scene(id="k-1", name=f"Karte {marke}", active=True),),
            ),
        )
        with scope:
            scope.execute(
                "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) "
                "VALUES (?, ?, 'm-1')",
                (scope.runde_id, szene.id),
            )
        compose_service.save(scope, sitzung, f"Chronik {marke}", "2026-05-01T21:00:00+00:00")
        compose_service.save(
            scope,
            sitzung,
            f"Rueckblick {marke}",
            "2026-05-01T21:00:00+00:00",
            kind=compose_service.RUECKBLICK,
        )
        transkript = transcribe_service.store(
            scope,
            sitzung,
            f"{marke}-spur",
            ((0, 1000, f"Gesprochen von {marke}."),),
            "2026-05-01T21:00:00",
        )
        transcribe_service.store(
            scope,
            sitzung,
            f"{marke}-memo",
            ((0, 1000, f"Diktiert von {marke}."),),
            "2026-05-01T21:00:00",
        )
        with scope:
            scope.execute(
                "INSERT INTO register_entry (runde_id, kind, name, description, state, "
                "suggested_at) VALUES (?, 'figur', 'Mira', ?, 'bestaetigt', '2026-05-01T21:00:00')",
                (scope.runde_id, f"Kundschafterin aus {marke}"),
            )
            eintrag = scope.execute(
                "SELECT id FROM register_entry WHERE runde_id = ? AND name = 'Mira'",
                (scope.runde_id,),
            ).fetchone()["id"]
            scope.execute(
                "INSERT INTO register_mention (runde_id, entry_id, session_id, scene_id) "
                "VALUES (?, ?, ?, ?)",
                (scope.runde_id, eintrag, sitzung, szene.id),
            )
            scope.execute(
                "INSERT INTO job (runde_id, kind, session_id, state, started_at, result) "
                "VALUES (?, 'nachtlauf', ?, 'fertig', '2026-05-01T04:00:00+00:00', ?)",
                (
                    scope.runde_id,
                    sitzung,
                    f'[{{"name": "diktat", "text": "{marke}", "gelungen": true}}]',
                ),
            )
            scope.execute(
                "INSERT INTO discord_intake (runde_id, message_id, status, handled_at) "
                "VALUES (?, ?, 'abgelegt', '2026-05-01T21:00:00')",
                (scope.runde_id, f"nachricht-{marke}"),
            )
    finally:
        scope.close()

    # Mit festgehaltenem Aufnahmebeginn: erst damit hat die Spur eine Sitzungsuhr, und
    # erst dann geht der Weg »Transkript → Szene« überhaupt los, den es zu trennen gilt.
    recordings.enqueue(
        runde,
        sitzung,
        f"{marke}-spur.wav",
        discord_user_id="d-1",
        started_at="2026-05-01T20:00:00+00:00",
    )
    aufnahme = recordings.for_session(runde, sitzung)[0]
    # Und ein Diktat daneben: keine Sitzungsuhr, dafür der Zeitpunkt seiner Nachricht. Das
    # ist der zweite Weg, auf dem gesprochenes Wort in Szenen geschrieben wird (#160), und
    # er gehört durch dieselbe Schranke wie der erste.
    recordings.enqueue(
        runde,
        sitzung,
        f"{marke}-memo.m4a",
        discord_user_id="d-1",
        message_at="2026-05-01T21:30:00+00:00",
    )
    consent.record(
        runde,
        session_id=sitzung,
        kind=consent.ANSAGE,
        guild_id=f"gilde-{marke}",
        channel_id="kanal",
        channel_name=f"Runde {marke}",
        text=f"Ansage {marke}",
        members=(
            consent.Member(id="d-1", name=f"Mensch {marke}"),
            # Und eine, die in Discord **genau** so heißt wie ihr Foundry-Konto: das ist der
            # Weg aus #76, auf dem ohne Klick geschrieben wird. Ohne sie liefe der Auto-Zweig
            # in diesem Gate nie, und ein ``raise`` mittendrin bliebe grün.
            consent.Member(id="d-2", name=f"Spielerin {marke}"),
        ),
    )
    # Ein zweiter Abend, damit der Bereich der Nacherzählung wirklich einer ist: über eine
    # einzige Sitzung könnte er eine fremde strukturell nie erreichen — und ein vergessenes
    # ``WHERE runde_id = ?`` in der Bereichsabfrage bliebe unbemerkt.
    spaeter = notes.create_session(
        runde, played_on="2026-05-08", title=f"Zweiter Abend {marke}", thread_id=f"spaet-{marke}"
    )
    scope = db.scoped(runde)
    try:
        compose_service.save(
            scope, spaeter, f"Chronik vom zweiten Abend {marke}", "2026-05-08T21:00:00+00:00"
        )
    finally:
        scope.close()

    people.confirm(runde, {"d-1": "u-1"})
    # Ollama trägt hier bewusst keine Marke: es gehört seit #87 der Instanz und ist damit
    # kein Kanarienvogel für ein Leck zwischen Runden, sondern ein geteilter Wert.
    settings.save(runde, {"foundry_url": f"https://{marke}.example"})
    zugang.merken(runde, f"passwort-{marke}", wer=f"wer-{marke}")
    return {
        "sitzung": sitzung,
        "spaeter": spaeter,
        "szene": szene.id,
        "aufnahme": aufnahme.id,
        "transkript": transkript,
        "eintrag": eintrag,
        "thread": f"thread-{marke}",
        "nachricht": f"nachricht-{marke}",
    }


@pytest.fixture
def zwei_runden(tmp_path):
    config = Config(data_dir=tmp_path, recordings_dir=tmp_path / "aufnahmen")
    db.init(config.database_path)
    a = runden.erste(config.database_path)
    b = runden.anlegen(config.database_path, "Zweite", guild_id="gilde-b")
    ids = {1: fuellen(config, a, MARKE[1]), 2: fuellen(config, b, MARKE[2])}
    return config, a, b, ids


def _mit_scope(runde, arbeit):
    scope = db.scoped(runde)
    try:
        return arbeit(scope)
    finally:
        scope.close()


def _marke(runde, ids) -> str:
    """Die Sitzungsmarke dieser Runde — was das Menü mitgibt, statt einer nackten Nummer."""
    return notes.sitzungsmarke(notes.session(runde, ids["sitzung"]))


# Jede lesende Funktion der Schicht, aufgerufen für **eine** Runde. Die Prüfung ist immer
# dieselbe: in der Antwort darf die Marke der anderen Runde nicht vorkommen.
ABFRAGEN = {
    "notes.sessions": lambda c, r, i: notes.sessions(r),
    "notes.session": lambda c, r, i: notes.session(r, i["sitzung"]),
    "notes.latest_session": lambda c, r, i: notes.latest_session(r),
    "notes.session_of_scene": lambda c, r, i: notes.session_of_scene(r, i["szene"]),
    "notes.session_of_thread": lambda c, r, i: notes.session_of_thread(r, i["thread"]),
    "notes.thread_of_session": lambda c, r, i: notes.thread_of_session(r, i["sitzung"]),
    "dokument.neu": lambda c, r, i: dokument.neu(r, ()),
    "notes.session_contents": lambda c, r, i: notes.session_contents(c, r, _marke(r, i)),
    "notes.sitzungsmarke": lambda c, r, i: notes.sitzungsmarke(notes.session(r, i["sitzung"])),
    "notes.gemeinte_sitzung": lambda c, r, i: notes.gemeinte_sitzung(r, _marke(r, i)),
    "notes.scene_at": lambda c, r, i: notes.scene_at(r, i["sitzung"], "2026-05-01T20:00:00+00:00"),
    "notes.today": lambda c, r, i: notes.today(),
    "protocol.stored": lambda c, r, i: protocol.stored(r, i["sitzung"]),
    "protocol.entries": lambda c, r, i: protocol.entries(r),
    "recordings.for_session": lambda c, r, i: recordings.for_session(r, i["sitzung"]),
    "recordings.get": lambda c, r, i: recordings.get(r, i["aufnahme"]),
    "recordings.pending": lambda c, r, i: recordings.pending(r),
    "recordings.expired": lambda c, r, i: recordings.expired(r, tage=0),
    "recordings.target_path": lambda c, r, i: recordings.target_path(c.recordings_dir, 1, "a.wav"),
    "search.find": lambda c, r, i: search.find(r, "Keller"),
    "search.expression": lambda c, r, i: search.expression("Keller"),
    "register.overview": lambda c, r, i: register.overview(r),
    "register.pending": lambda c, r, i: register.pending(r),
    "register.parse": lambda c, r, i: register.parse("figur | Mira | Eine Kundschafterin"),
    "register.nach_sitzung": lambda c, r, i: register.nach_sitzung(r),
    "people.overview": lambda c, r, i: people.overview(r),
    "people.speakers": lambda c, r, i: people.speakers(r),
    "people.suggest": lambda c, r, i: people.suggest("Mira", ()),
    "people.genau": lambda c, r, i: people.genau("Mira", ()),
    "consent.for_session": lambda c, r, i: consent.for_session(r, i["sitzung"]),
    "jobs.latest": lambda c, r, i: jobs.latest(r, jobs.NACHTLAUF),
    "jobs.running": lambda c, r, i: jobs.running(r),
    "jobs.belegt": lambda c, r, i: jobs.belegt(r),
    "jobs.mehrzahl": lambda c, r, i: jobs.mehrzahl(2),
    "nightly.offen": lambda c, r, i: nightly.offen(r),
    "nightly.letzter": lambda c, r, i: nightly.letzter(r),
    "nightly.faellig": lambda c, r, i: nightly.faellig(
        __import__("datetime").datetime.now().astimezone(), "04:00", None, "Europe/Berlin"
    ),
    "settings.stored": lambda c, r, i: settings.stored(r),
    "settings.effective": lambda c, r, i: settings.effective(c, r),
    "settings.sources": lambda c, r, i: settings.sources(c, r),
    "settings.is_set": lambda c, r, i: settings.is_set(c, r, "foundry_url"),
    "settings.nightly_time": lambda c, r, i: settings.nightly_time(r),
    "settings.nightly_zone": lambda c, r, i: settings.nightly_zone(r),
    "settings.foundry_quelle": lambda c, r, i: settings.foundry_quelle(r),
    "settings.nightly_at": lambda c, r, i: settings.nightly_at("04:00"),
    "service.current": lambda c, r, i: foundry_service.current(c, r),
    "service.failed": lambda c, r, i: foundry_service.failed(c, r),
    "zugang.passwort": lambda c, r, i: zugang.passwort(r),
    "zugang.ist_gemerkt": lambda c, r, i: zugang.ist_gemerkt(r),
    "zugang.gemerkt_von": lambda c, r, i: zugang.gemerkt_von(r),
    "zugang.passwort_von": lambda c, r, i: zugang.passwort_von(r, f"wer-{MARKE[1]}"),
    "store.load": lambda c, r, i: _mit_scope(r, foundry_store.load),
    "store.world": lambda c, r, i: _mit_scope(r, foundry_store.world),
    "store.last_failure": lambda c, r, i: _mit_scope(r, foundry_store.last_failure),
    "store.message": lambda c, r, i: _mit_scope(
        r,
        lambda s: foundry_store.message(
            s.execute("SELECT * FROM foundry_message WHERE runde_id = ?", (s.runde_id,)).fetchone()
        ),
    ),
    "service.material": lambda c, r, i: _mit_scope(
        r, lambda s: compose_service.material(s, i["sitzung"])
    ),
    "service.recap_material": lambda c, r, i: _mit_scope(
        r, lambda s: compose_service.recap_material(s, i["sitzung"])
    ),
    # Die Nacherzählung läuft über einen Sitzungsbereich und das Register — zwei Wege in
    # eine fremde Kampagne, wenn einer davon die Runde vergisst. Der Bereich geht deshalb
    # über **beide** Abende: eine Abfrage von einer Sitzung zu sich selbst kann eine fremde
    # gar nicht erst einsammeln und prüfte die Schranke nie.
    "service.erzaehl_material": lambda c, r, i: _mit_scope(
        r,
        lambda s: compose_service.erzaehl_material(
            s, i["sitzung"], i["spaeter"], register.nach_sitzung(r)
        ),
    ),
    "service.erzaehlen": lambda c, r, i: compose_service.erzaehlen(
        c, r, i["sitzung"], i["spaeter"], register.nach_sitzung(r)
    ),
    "service.names": lambda c, r, i: _mit_scope(
        r, lambda s: transcribe_service.names(s, i["sitzung"])
    ),
    "service.zeitmarke": lambda c, r, i: transcribe_service.zeitmarke(61),
    "service.segment_rows": lambda c, r, i: transcribe_service.segment_rows(()),
    "merge.conversation": lambda c, r, i: merge.conversation(r, i["sitzung"]),
    "merge.marke_ms": lambda c, r, i: merge.marke_ms("1:02"),
    "merge.span": lambda c, r, i: merge.span(()),
    "merge.note_text": lambda c, r, i: merge.note_text(()),
    "service.cursor": lambda c, r, i: discord_service.cursor(r),
    "rueckblick.embed": lambda c, r, i: discord_rueckblick.embed("# Titel\n\nKurz."),
    "ausgabe.datei": lambda c, r, i: discord_ausgabe.datei(
        "# Chronik", "2026-05-01T21:00:00+00:00"
    ),
    "ausgabe.dateiname": lambda c, r, i: discord_ausgabe.dateiname("2026-05-01"),
    "ausgabe.begleitung": lambda c, r, i: discord_ausgabe.begleitung(None),
    "erinnern.suche": lambda c, r, i: erinnern.suche(r, "Keller"),
    "erinnern.wer": lambda c, r, i: erinnern.wer(r, "Mira"),
    "erinnern.offen": lambda c, r, i: erinnern.offen(r),
    "erinnern.zuordnung": lambda c, r, i: erinnern.zuordnung(r),
    "erinnern.wahlmoeglichkeiten": lambda c, r, i: erinnern.wahlmoeglichkeiten(
        people.overview(r).personen[0], people.overview(r).spieler
    ),
    # Die Entscheidung beim Betreten liest nur — geschrieben wird sie von ``zuordnen``,
    # nachdem der Vermerk im Thread steht. Gefragt wird hier mit **d-2**: die trifft ihr
    # Konto 1:1 und läuft damit durch den Zweig, der ohne Klick auskommt.
    "erinnern.betreten": lambda c, r, i: erinnern.betreten(r, "d-2"),
    "lebenszyklus.frist_datum": lambda c, r, i: lebenszyklus.frist_datum(r),
    "lebenszyklus.ruht": lambda c, r, i: lebenszyklus.ruht(r),
    "lebenszyklus.dieselbe": lambda c, r, i: lebenszyklus.dieselbe(r),
}

# Schreibende Aufrufe. Sie werden nicht auf eine Antwort geprüft, sondern darauf, dass sie
# eine fremde Zeile nicht erreichen — jeweils ein eigener Test weiter unten.
SCHREIBER = frozenset(
    {
        "notes.create_session",
        "notes.add_scene",
        "notes.add_note",
        "notes.rename_scene",
        "dokument.anlegen",
        "notes.update_note",
        "notes.remove_note",
        "notes.drop_derived",
        # Die schärfste Löschung unterhalb der Runde: sie nimmt Tondateien von der Platte,
        # und der Weg dorthin führt am Dateinamen entlang und nicht an der Runde. Geprüft
        # weiter unten mit einer fremden Sitzungskennung — dieselbe Nummer, andere Runde.
        "notes.delete_session",
        "merge.uebernehmen",
        "notes.link_foundry_message",
        "recordings.enqueue",
        "recordings.mark",
        "recordings.sweep",
        # Beide fassen die ``laeuft``-Zeilen **einer** Runde an: das Zurückstellen einer
        # verwaisten Spur ist ein Schreiber wie jeder andere, und der Herzschlag hinter
        # ``in_arbeit`` schreibt in dieselbe Zeile.
        "recordings.zurueckstellen",
        "recordings.in_arbeit",
        "register.suggest",
        "register.decide",
        "people.confirm",
        "consent.record",
        "jobs.start",
        "jobs.abgleich",
        "jobs.abschluss",
        "jobs.chronik",
        "jobs.nacherzaehlung",
        "nightly.lauf",
        "settings.save",
        "settings.save_nightly_time",
        "settings.save_nightly_zone",
        "settings.save_foundry_quelle",
        "service.sync",
        "service.beobachten",
        "service.abzug",
        "zugang.merken",
        "zugang.vergiss",
        "store.save",
        "store.bind_world",
        "store.record_failure",
        # Räumt die Fixture aus dem Archiv — ein Löscher, und damit der Aufruf, bei dem
        # eine verwechselte Runde nicht bloß etwas Fremdes zeigte, sondern es fortnähme.
        "store.testwelt_raeumen",
        "service.compose_session",
        "service.recap_session",
        "service.save",
        "service.store",
        "service.transcribe_session",
        "service.run_queue",
        "service.run",
        "rueckblick.deliver",
        "ausgabe.anhaengen",
        "ausgabe.erzaehlung_zustellen",
        "erinnern.entscheiden",
        # Der eine Schreiber der Personen-Zuordnung — auch für den Weg beim Betreten, seit
        # ``betreten`` nur noch entscheidet (#76). Geprüft weiter unten, einmal am Klick und
        # einmal am ganzen Weg ohne Klick.
        "erinnern.zuordnen",
        # Und die Rücknahme dazu: sie löscht eine Zeile, wenn dort genau ein bestimmtes
        # Konto steht — beide Runden tragen ``d-1`` auf ``u-1``, also wäre eine verwechselte
        # Runde hier ein Löschen von nebenan.
        "erinnern.zuruecknehmen",
        "lebenszyklus.loeschen",
        "lebenszyklus.freigeben",
    }
)


def test_jede_gescopte_funktion_wird_geprueft():
    """Eine neue Funktion der Schicht fällt durch, bis sie hier steht."""
    ungeprueft = []
    for modul in SCHICHT:
        for name, funktion in _oeffentlich(modul).items():
            if not _gescopt(funktion):
                continue
            schluessel = _schluessel(modul, name)
            if schluessel not in ABFRAGEN and schluessel not in SCHREIBER:
                ungeprueft.append(f"{modul.__name__}.{name}")
    assert not ungeprueft, f"ohne Isolationsprüfung: {ungeprueft}"


def test_signatur_verlangt_die_runde():
    """Wer Konfiguration oder Datenbank anfasst, nennt die Runde — oder ist instanzweit."""
    ohne = []
    for modul in SCHICHT:
        for name, funktion in _oeffentlich(modul).items():
            namen = inspect.signature(funktion).parameters
            if not any(argument in namen for argument in DATENARGUMENTE):
                continue
            if getattr(funktion, "instanzweit", False) or _gescopt(funktion):
                continue
            ohne.append(f"{modul.__name__}.{name}")
    assert not ohne, f"ohne Runden-Argument: {ohne}"


def test_keine_rohe_verbindung_in_der_schicht():
    """``connection`` wäre der Weg an der Schranke vorbei — den gibt es nicht mehr."""
    roh = [
        f"{modul.__name__}.{name}"
        for modul in SCHICHT
        for name, funktion in _oeffentlich(modul).items()
        if "connection" in inspect.signature(funktion).parameters
    ]
    assert not roh, f"nimmt eine rohe Verbindung: {roh}"


def test_alle_runden_tabellen_tragen_die_spalte(tmp_path):
    """Jede Tabelle gehört entweder einer Runde — mit Spalte — oder der Instanz."""
    pfad = tmp_path / "chronicle.sqlite3"
    db.init(pfad)
    connection = db.connect(pfad)
    try:
        tabellen = {
            zeile["name"]
            for zeile in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'search_index_%'"
            )
        }
        eigen = tabellen - {"meta", "runde"}
        assert eigen == set(db.GESCOPTE_TABELLEN)
        ohne = [
            name
            for name in sorted(eigen)
            if "runde_id"
            not in {z["name"] for z in connection.execute(f"PRAGMA table_info({name})")}
        ]
    finally:
        connection.close()
    assert not ohne, f"ohne runde_id: {ohne}"


def test_scope_weist_eine_abfrage_ohne_runde_ab(zwei_runden):
    _config, a, _b, _ids = zwei_runden
    scope = db.scoped(a)
    try:
        with pytest.raises(db.UngescopteAbfrage):
            scope.execute("SELECT * FROM note")
    finally:
        scope.close()


def test_lesende_schicht_sieht_die_fremde_runde_nicht(zwei_runden):
    config, a, _b, ids = zwei_runden
    verraten = []
    for name, abfrage in ABFRAGEN.items():
        antwort = repr(abfrage(config, a, ids[1]))
        if MARKE[2] in antwort:
            verraten.append(name)
    assert not verraten, f"zeigt Daten der fremden Runde: {verraten}"


def test_suche_findet_nur_die_eigene_runde(zwei_runden):
    config, a, b, _ids = zwei_runden
    treffer_a = " ".join(str(h.snippet) for g in search.find(a, "Keller").groups for h in g.hits)
    treffer_b = " ".join(str(h.snippet) for g in search.find(b, "Keller").groups for h in g.hits)
    assert MARKE[1] in treffer_a and MARKE[2] not in treffer_a
    assert MARKE[2] in treffer_b and MARKE[1] not in treffer_b


def test_gleichnamiger_registereintrag_steht_zweimal(zwei_runden):
    config, a, b, _ids = zwei_runden
    beide = [g.entries[0] for g in (register.overview(a) + register.overview(b))]
    assert [e.name for e in beide] == ["Mira", "Mira"]
    assert beide[0].description != beide[1].description


def test_notiz_landet_nicht_in_der_fremden_szene(zwei_runden):
    config, a, b, ids = zwei_runden
    assert notes.add_note(a, ids[2]["szene"], "Fremde Szene") is None
    assert notes.add_scene(a, ids[2]["sitzung"]) is None
    assert notes.rename_scene(a, ids[2]["sitzung"], ids[2]["szene"], "Umbenannt") is False
    assert notes.session(b, ids[2]["sitzung"]).scenes[0].title is None
    assert notes.session(a, ids[2]["sitzung"]) is None
    assert notes.session_of_scene(a, ids[2]["szene"]) is None
    # Und der Ereignisstrom hängt seine Würfe nicht in die Szene von nebenan: dort stünden
    # sie in einer fremden Chronik, ohne dass jemand sie dort je gesehen hätte.
    assert notes.link_foundry_message(a, ids[2]["szene"], "m-neu") is False
    assert notes.link_foundry_message(a, ids[1]["szene"], "m-neu") is True


def test_der_ereignisstrom_sieht_und_schreibt_nur_die_eigene_runde(zwei_runden):
    """Ein Blick nach Foundry ist ein Schreiber wie der Abgleich — und ebenso eingezäunt."""

    class NurUnsereWelt:
        def fetch_world(self):
            return UNSER_KONTO, WELT

    config, a, b, _ids = zwei_runden
    ergebnis = foundry_service.beobachten(config, a, client=NurUnsereWelt())

    assert [nachricht.id for nachricht in ergebnis.neu] == ["m-wurf"]
    eigene = {nachricht.id for nachricht in _mit_scope(a, foundry_store.load).messages}
    fremde = _mit_scope(b, foundry_store.load).messages
    assert "m-wurf" in eigene
    assert [nachricht.id for nachricht in fremde] == ["m-1"]
    assert all(nachricht.vanished_at is None for nachricht in fremde)


def test_der_thread_der_fremden_runde_ist_keine_sitzung(zwei_runden):
    """Der Thread ist die Sitzung — und ein Thread von nebenan ist keine Sitzung von hier.

    Discord meldet Nachricht, Änderung und Löschung mit ihrer Kennung, sonst nichts. Ohne
    diese Schranke schriebe ein Thread aus einem fremden Server in eine fremde Chronik.
    """
    config, a, b, ids = zwei_runden
    assert notes.session_of_thread(a, ids[2]["thread"]) is None
    assert notes.session_of_thread(a, ids[1]["thread"]) == ids[1]["sitzung"]

    # Und die Gegenrichtung ebenso: der Bot sagt einer Sitzung in ihren Thread, dass er
    # den Mitschnitt beendet hat — ein Thread von nebenan wäre der falsche Server.
    assert notes.thread_of_session(a, ids[2]["sitzung"]) is None
    assert notes.thread_of_session(a, ids[1]["sitzung"]) == ids[1]["thread"]

    assert notes.update_note(a, ids[2]["nachricht"], "umgeschrieben") is False
    assert notes.remove_note(a, ids[2]["nachricht"]) is False
    fremde = notes.session(b, ids[2]["sitzung"]).scenes[0].notes
    assert [notiz.text for notiz in fremde] == [f"Im Keller stand {MARKE[2]}."]


def test_die_fremde_sitzung_wird_nicht_geloescht(zwei_runden):
    """Der teuerste Griff daneben: gelöscht wird endgültig, und Tondateien gehen mit.

    Die Sitzungskennung ist über alle Runden hinweg eindeutig, der Dateiname im
    Aufnahmeverzeichnis trägt sie — wer die Datei über den Namen sucht, hat die Runde
    dabei aus der Hand gegeben. Genau deshalb steht die Schranke **vor** dem Griff ins
    Verzeichnis: eine Sitzung, die dieser Runde nicht gehört, gibt es hier nicht, und
    dann wird auch nichts gesucht.
    """
    config, a, b, ids = zwei_runden
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    fremd = config.recordings_dir / f"sitzung{ids[2]['sitzung']}-20260501T200000-Mira.wav"
    fremd.write_bytes(b"ton")
    fremde_marke = _marke(b, ids[2])

    assert notes.delete_session(config, a, fremde_marke) is None

    assert fremd.exists()
    assert notes.session(b, ids[2]["sitzung"]) is not None
    assert notes.session_contents(config, a, fremde_marke) is None
    # Und die eigene geht sehr wohl — sonst bliebe der Test auch grün, wenn gar nichts löschte.
    assert notes.delete_session(config, a, _marke(a, ids[1])) is not None


def _notiztexte(runde, session_id) -> str:
    daten = notes.session(runde, session_id)
    return " ".join(notiz.text for szene in daten.scenes for notiz in szene.notes)


def test_das_transkript_wandert_nur_in_die_eigenen_szenen(zwei_runden):
    """Der neue Weg aus #140 geht durch dieselbe Schranke wie alles andere.

    Er ist der schärfste Fall seit Langem: er **schreibt** gesprochenes Wort in Szenen,
    und eine verwechselte Runde legte die Stimmen der einen Gruppe in die Chronik der
    anderen. Zweitens verwirft er dabei — ein zweiter Lauf hier darf drüben nichts
    abräumen.

    Zwei Notizen je Runde, weil es zwei Wege sind: die Bot-Spur über die Sitzungsuhr und
    das Diktat über den Zeitpunkt seiner Nachricht (#160). Beide müssen durch die Schranke.
    """
    _config, a, b, ids = zwei_runden
    assert merge.uebernehmen(a, ids[1]["sitzung"]) == 2
    assert merge.uebernehmen(a, ids[2]["sitzung"]) == 0
    assert merge.uebernehmen(b, ids[2]["sitzung"]) == 2

    assert f"Diktiert von {MARKE[1]}." in _notiztexte(a, ids[1]["sitzung"])
    assert MARKE[1] in _notiztexte(a, ids[1]["sitzung"])
    assert MARKE[2] not in _notiztexte(a, ids[1]["sitzung"])
    assert MARKE[2] in _notiztexte(b, ids[2]["sitzung"])
    assert MARKE[1] not in _notiztexte(b, ids[2]["sitzung"])

    assert notes.drop_derived(a, ids[2]["sitzung"], merge.TRANSKRIPT) == 0
    assert MARKE[2] in _notiztexte(b, ids[2]["sitzung"])


DOKUMENT = "## 12.03.2026 — Der Keller\n\n### Die Luke\n\nDahinter lag {marke}.\n"


def _mit_titel(runde, titel):
    return [sitzung for sitzung in notes.sessions(runde) if sitzung.title == titel]


def test_ein_eingelesenes_dokument_bleibt_in_seiner_runde(zwei_runden):
    """Der Altbestand einer Gruppe geht in **ihre** Chronik — und in keine daneben.

    Und die Wiedererkennung trennt mit: derselbe Abschnitt, in A schon eingelesen, gilt in
    B trotzdem als neu. Ohne diese Schranke unterdrückte die eine Runde das Einlesen der
    anderen — eine Gruppe bekäme ihre eigenen Notizen nicht angelegt, weil eine fremde
    dieselbe Datei schon hatte.
    """
    _config, a, b, _ids = zwei_runden
    abende = dokument.lesen(DOKUMENT.format(marke=MARKE[1])).abende

    assert dokument.anlegen(a, abende) == 1

    assert dokument.neu(a, abende) == ()
    assert dokument.neu(b, abende) == abende

    (angelegt,) = _mit_titel(a, "Der Keller")
    assert MARKE[1] in _notiztexte(a, angelegt.id)
    assert _mit_titel(b, "Der Keller") == []


def test_aufnahme_der_fremden_runde_bleibt_unberuehrt(zwei_runden):
    config, a, b, ids = zwei_runden
    assert recordings.get(a, ids[2]["aufnahme"]) is None
    recordings.mark(a, ids[2]["aufnahme"], recordings.GESCHEITERT, "von nebenan")
    assert recordings.get(b, ids[2]["aufnahme"]).status == recordings.WARTET


def test_zuordnung_und_register_bleiben_getrennt(zwei_runden):
    config, a, b, ids = zwei_runden
    people.confirm(a, {"d-1": ""})
    assert people.overview(a).personen[0].confirmed is None
    assert people.overview(b).personen[0].confirmed.name == f"Spielerin {MARKE[2]}"

    register.decide(a, {ids[2]["eintrag"]: register.Entscheidung(ja=False)})
    assert [g.entries[0].id for g in register.overview(b)] == [ids[2]["eintrag"]]


def test_eine_ansicht_von_vorhin_meint_nicht_die_frische_runde(zwei_runden):
    """Der Griff, an dem jede Ansicht ihre Runde wiedererkennt — Knopf, Menü und Fenster.

    Eine Ansicht lebt eine Viertelstunde und schließt ihre ``Runde`` ein. ``runde.id`` ist
    ein ``INTEGER PRIMARY KEY`` ohne ``AUTOINCREMENT``: SQLite vergibt sie nach einer
    Löschung wieder, und dann gehört sie einer fremden Gilde. Ohne diesen Vergleich
    schriebe das Kanalmenü von hier den Zustellkanal in die Runde von dort.
    """
    config, _a, b, _ids = zwei_runden
    lebenszyklus.loeschen(config, b)
    frisch = runden.anlegen(config.database_path, "Dritte", guild_id="gilde-c")
    assert frisch.id == b.id

    assert bot_chronik.dieselbe_runde(config, "gilde-c", b) is None
    assert bot_chronik.dieselbe_runde(config, "gilde-c", frisch) == frisch
    assert bot_chronik.dieselbe_runde(config, None, frisch) is None


def test_keine_runde_kommt_ohne_kennung_in_die_datenbank(zwei_runden):
    """Und der Griff hält nur, solange jede Runde eine Kennung *hat*.

    Zwei Runden ohne Kennung und eine wiederverwendete Id vergleichen sich als dieselbe —
    die Schranke fiele damit still auf Id-Gleichheit zurück, ohne dass etwas rot würde.
    Deshalb steht die Zusicherung im Schema und nicht in der Absicht des Aufrufers.
    """
    config, a, b, _ids = zwei_runden
    assert a.token and b.token and a.token != b.token
    assert all(runde.token for runde in runden.alle(config.database_path))

    connection = db.connect(config.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runde (name, guild_id, created_at) VALUES (?, ?, ?)",
                ("Ohne Kennung", "gilde-x", "2026-08-11T12:00:00+00:00"),
            )
    finally:
        connection.close()


def test_die_runde_ohne_gilde_faellt_nicht_der_naechstbesten_zu(zwei_runden):
    """Die Übernahme aus #121 greift nur, wo es keine zweite Deutung gibt.

    Runde A trägt keine Gilde — so sieht eine Runde aus der Zeit vor dem Runden-Modell aus.
    Neben ihr steht B. Sie jetzt der Gilde zuzuschlagen, die den Bot gerade eingeladen hat,
    gäbe fremde Klarnamen, Einwilligungsprotokolle und Foundry-Daten an eine beliebige
    Gruppe — und zwar dauerhaft, denn danach ist es *ihre* Runde.
    """
    config, a, _b, _ids = zwei_runden
    assert a.guild_id is None

    fremde = (lebenszyklus.Gilde(id="gilde-c", name="Fremde"),)
    assert lebenszyklus.verwaiste_uebernehmen(config, fremde) is None
    assert runden.get(config.database_path, a.id).guild_id is None


def test_knopf_und_menue_wirken_nur_in_der_eigenen_runde(zwei_runden):
    """Ein Klick trägt die Kennung seiner Ansicht — entschieden wird trotzdem in *seiner* Runde."""
    config, a, b, ids = zwei_runden
    assert erinnern.entscheiden(a, ids[2]["eintrag"], register.FIGUR) == erinnern.SCHON_ENTSCHIEDEN
    assert [g.entries[0].id for g in register.overview(b)] == [ids[2]["eintrag"]]

    erinnern.zuordnen(a, "d-1", erinnern.KEINE)
    assert people.overview(a).personen[0].confirmed is None
    assert people.overview(b).personen[0].confirmed.name == f"Spielerin {MARKE[2]}"


def _nach_kennung(runde) -> dict[str, object]:
    return {person.discord_user_id: person for person in people.overview(runde).personen}


def test_die_zuordnung_beim_betreten_bleibt_in_ihrer_runde(zwei_runden):
    """Der Weg aus #76 verknüpft eine Discord-Person mit einem Foundry-Konto — ohne Klick.

    Damit ist er der schärfste der drei Zuordnungswege: hier entscheidet niemand von Hand,
    und eine verwechselte Runde legte die Person der einen Gruppe auf ein Konto der anderen.
    Gegangen wird deshalb der **ganze** Weg — entscheiden und schreiben —, und zwar in A;
    B trägt dieselben Discord-Kennungen und dieselbe Namensgleichheit und bleibt trotzdem,
    wie es war.
    """
    _config, a, b, _ids = zwei_runden
    # Erst freigeben: solange das Konto vergeben ist, gibt es nichts von selbst zu setzen.
    people.confirm(a, {"d-1": ""})
    people.confirm(b, {"d-1": ""})

    stand = erinnern.betreten(a, "d-2")

    assert stand.automatisch is not None
    assert stand.automatisch.name == f"Spielerin {MARKE[1]}"
    # Entschieden, nicht geschrieben: das tut erst ``zuordnen``, nach dem Vermerk.
    assert _nach_kennung(a)["d-2"].confirmed is None

    erinnern.zuordnen(a, "d-2", stand.automatisch.id)

    assert _nach_kennung(a)["d-2"].confirmed.name == f"Spielerin {MARKE[1]}"
    assert _nach_kennung(b)["d-2"].confirmed is None
    assert _nach_kennung(b)["d-1"].confirmed is None


def test_die_ruecknahme_loest_nur_in_der_eigenen_runde(zwei_runden):
    """Sie nimmt zurück, was **hier** geschrieben wurde — nicht die gleichlautende Zeile drüben.

    Beide Runden tragen ``d-1`` auf ``u-1``. Eine verwechselte Runde löschte hier die
    Zuordnung der Nachbarn, und niemand hätte sie zurückgenommen.
    """
    _config, a, b, _ids = zwei_runden

    assert erinnern.zuruecknehmen(a, "d-1", "u-1") is True

    assert _nach_kennung(a)["d-1"].confirmed is None
    assert _nach_kennung(b)["d-1"].confirmed.name == f"Spielerin {MARKE[2]}"


def test_das_umhaengen_nimmt_nur_in_der_eigenen_runde_etwas_fort(zwei_runden):
    """``uebernehmen`` ist der einzige Aufruf, der einer **anderen** Person etwas wegnimmt.

    Beide Runden tragen dieselben Discord-Kennungen und dasselbe Konto ``u-1``; eine
    verwechselte Runde risse hier nicht bloß eine fremde Zeile auf, sondern löschte sie.
    Gepinnt, weil die Eigenschaft sonst nur zufällig hält.
    """
    _config, a, b, _ids = zwei_runden

    ergebnis = erinnern.zuordnen(a, "d-2", "u-1", uebernehmen=True)

    assert ergebnis.spieler.id == "u-1"
    assert ergebnis.vorher.discord_user_id == "d-1"
    assert _nach_kennung(a)["d-2"].confirmed.name == f"Spielerin {MARKE[1]}"
    assert _nach_kennung(a)["d-1"].confirmed is None
    # Und drüben steht alles, wie es war — dieselbe Kennung, dasselbe Konto.
    assert _nach_kennung(b)["d-1"].confirmed.name == f"Spielerin {MARKE[2]}"
    assert _nach_kennung(b)["d-2"].confirmed is None


def test_das_raeumen_der_testwelt_greift_nicht_ins_nachbararchiv(zwei_runden):
    """Ein Löscher auf ``foundry_message`` — hier wäre eine verwechselte Runde ein Verlust.

    Beide Runden tragen dieselbe Nachrichten-Id; geräumt wird nur, was aus der Fixture
    stammt, und nur in der Runde, deren Scope gefragt wurde.
    """
    _config, a, b, _ids = zwei_runden
    for gruppe in (a, b):
        _mit_scope(
            gruppe,
            lambda scope: foundry_store.save(
                scope,
                WorldSnapshot(
                    system="daggerheart",
                    fetched_at="2026-05-02T20:00:00+00:00",
                    messages=(ChatMessage(id="m-2", timestamp=2000, content="Erfunden."),),
                ),
                testwelt=True,
            ),
        )

    assert _mit_scope(a, foundry_store.testwelt_raeumen) == 1

    def _ids_von(scope):
        return {
            zeile["id"]
            for zeile in scope.execute(
                "SELECT id FROM foundry_message WHERE runde_id = ?", (scope.runde_id,)
            )
        }

    assert _mit_scope(a, _ids_von) == {"m-1"}
    assert _mit_scope(b, _ids_von) == {"m-1", "m-2"}


def test_das_menue_beim_betreten_zeigt_nur_konten_der_eigenen_runde(zwei_runden):
    """Wo gefragt wird, steht zur Wahl, was **dieser** Runde gehört — und nur das Freie."""
    _config, a, b, _ids = zwei_runden
    people.confirm(a, {"d-1": ""})

    stand = erinnern.betreten(a, "d-1")

    assert stand.automatisch is None
    assert [wer.name for wer in stand.spieler] == [f"Spielerin {MARKE[1]}"]
    # Und wer drüben zugeordnet ist, wird von hier aus nicht angefasst — auch nicht unter
    # derselben Discord-Kennung, die es in beiden Runden gibt.
    assert erinnern.betreten(b, "d-1") == erinnern.Betreten()
    assert _nach_kennung(b)["d-1"].confirmed.name == f"Spielerin {MARKE[2]}"
    assert _nach_kennung(a)["d-1"].confirmed is None


def test_einstellungen_gehoeren_der_runde(zwei_runden):
    config, a, b, _ids = zwei_runden
    assert settings.effective(config, a).foundry_url == f"https://{MARKE[1]}.example"
    assert settings.effective(config, b).foundry_url == f"https://{MARKE[2]}.example"
    settings.save(a, {"foundry_url": ""})
    assert settings.effective(config, a).foundry_url is None
    assert settings.effective(config, b).foundry_url == f"https://{MARKE[2]}.example"


TOKEN = "token-nur-in-diesem-test"


def test_der_bot_token_gehoert_der_instanz_und_keiner_runde(zwei_runden):
    """Unser Token: keine Gruppe liest ihn aus ihrer Runde, keine schreibt ihn dort um."""
    config, a, b, _ids = zwei_runden
    instanz.save(config.database_path, {"discord_bot_token": TOKEN})

    # Erst schreiben, dann nachsehen — vor dem Schreiben sagt die Prüfung nichts. ``/setup``
    # und die Kanalwahl sind die Wege, die einer Gilde offenstehen; der Schreibversuch mit
    # der Runde als Absender ist der, den ein Formular ihr eröffnen könnte.
    eingerichtet = bot_einrichten.einrichten(
        config, "gilde-b", "Zweite", adresse="https://b.example", benutzer="chronist"
    )
    kanal = bot_einrichten.kanal_setzen(b, "4711")
    settings.save(b, {"discord_bot_token": TOKEN})

    # Er steht in ``meta`` und in keiner runden-eigenen Zeile — sonst läge er im Zugriff
    # einer Gruppe, sobald sie ihre eigenen Einstellungen liest.
    for gruppe in (a, b):
        assert "discord_bot_token" not in _gepflegte_zeilen(gruppe)

    assert TOKEN not in eingerichtet.meldung + kanal
    assert instanz.stored(config.database_path)["discord_bot_token"] == TOKEN


def _gepflegte_zeilen(runde) -> set[str]:
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT key FROM settings WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
    finally:
        scope.close()
    return {zeile["key"] for zeile in zeilen}


def test_ollama_gehoert_der_instanz_und_nicht_der_runde(zwei_runden):
    """Wohin gesprochenes Wort fließt, entscheidet nicht eine fremde Gruppe (#87)."""
    config, a, b, _ids = zwei_runden
    settings.save(a, {"ollama_model": "gemma4:e4b"})
    settings.save(b, {"ollama_model": "gemma4:12b", "ollama_url": "http://box.example:11434"})
    assert settings.effective(config, a).ollama_model == "gemma4:12b"
    assert settings.effective(config, a).ollama_url == "http://box.example:11434"
    connection = db.connect(config.database_path)
    try:
        gepflegt = {zeile["key"] for zeile in connection.execute("SELECT key FROM settings")}
    finally:
        connection.close()
    assert "ollama_model" not in gepflegt
    assert "ollama_url" not in gepflegt


def test_die_quelle_der_spieldaten_gehoert_der_runde(zwei_runden):
    """Die eine Gruppe probiert die Testwelt aus, die andere spielt weiter auf ihrem Server."""
    _config, a, b, _ids = zwei_runden
    settings.save_foundry_quelle(a, settings.TESTWELT)
    assert settings.foundry_quelle(a) == settings.TESTWELT
    assert settings.foundry_quelle(b) == settings.SERVER


def test_die_bedienstellen_in_discord_treffen_nur_ihre_eigene_runde(zwei_runden):
    """Die Wege aus #110 und #116 gehen durch dieselbe Schranke wie alles andere.

    Sie stehen nicht in ``SCHICHT`` — ``chronicle.bot.einrichten`` nimmt eine Gilde-Kennung
    statt einer Runde und fiele durch ``test_signatur_verlangt_die_runde``. Geprüft werden
    sie deshalb hier von Hand: eine Zone, eine Quelle und ein angestoßener Abgleich sind
    drei Griffe, mit denen eine verwechselte Runde einer fremden Gruppe die Nacht
    verschöbe, ihre Chronik mit erfundenen Zahlen füllte oder ihr Foundry befragte.
    """
    config, a, b, _ids = zwei_runden
    bot_einrichten.einrichten(config, "gilde-b", MARKE[2], zone="Pacific/Auckland")
    bot_einrichten.quelle_setzen(b, settings.TESTWELT)

    assert settings.nightly_zone(a) == settings.DEFAULT_NIGHTLY_ZONE
    assert settings.nightly_zone(b) == "Pacific/Auckland"
    assert settings.foundry_quelle(a) == settings.SERVER
    assert settings.foundry_quelle(b) == settings.TESTWELT
    assert [schrift for schrift, _wert, gewaehlt in bot_einrichten.quellenwahl(a) if gewaehlt] == [
        bot_einrichten.QUELLE_SERVER
    ]

    # Der Abgleich reicht das Passwort **dieser** Runde weiter und verbraucht es dort —
    # ginge er an die falsche, zeigte er es dem Foundry-Server einer fremden Gruppe vor.
    bot_chronik.abgleich_starten(config, b, None, melden=lambda text: None)
    warte_auf_laeufe()
    assert not zugang.ist_gemerkt(b)
    assert zugang.passwort(a) == f"passwort-{MARKE[1]}"
    assert jobs.latest(a, jobs.ABGLEICH) is None
    assert jobs.latest(b, jobs.ABGLEICH) is not None


def test_bot_token_gehoert_der_instanz_und_nicht_der_runde(zwei_runden):
    """Unser Token, nicht der einer Gruppe: er liegt nicht in der Tabelle einer Runde."""
    config, a, b, _ids = zwei_runden
    settings.save(a, {"discord_bot_token": "platzhalter-token"})
    assert settings.effective(config, b).discord_bot_token == "platzhalter-token"
    assert instanz.stored(config.database_path)["discord_bot_token"] == "platzhalter-token"
    connection = db.connect(config.database_path)
    try:
        gepflegt = {zeile["key"] for zeile in connection.execute("SELECT key FROM settings")}
    finally:
        connection.close()
    assert "discord_bot_token" not in gepflegt


def test_das_gemerkte_passwort_gehoert_genau_einer_runde(zwei_runden):
    """Der Merkzettel ist keine Ausnahme von der Trennung, nur weil er nichts speichert."""
    _config, a, b, _ids = zwei_runden
    assert zugang.passwort(a) == f"passwort-{MARKE[1]}"
    assert zugang.passwort(b) == f"passwort-{MARKE[2]}"
    zugang.vergiss(a)
    assert zugang.passwort(a) is None
    assert zugang.passwort(b) == f"passwort-{MARKE[2]}"


def test_auftraege_serialisieren_ueber_alle_runden(zwei_runden, monkeypatch):
    """Eine CPU, ein Ollama: läuft in A einer, beginnt in B keiner."""
    config, a, b, _ids = zwei_runden
    haltet = []
    laeuft = jobs.start(config, a, jobs.ABGLEICH, lambda: haltet.append(1) or "fertig")
    assert laeuft is not None
    warte_auf_laeufe()
    scope = db.scoped(a)
    try:
        with scope:
            scope.execute(
                "UPDATE job SET state = ? WHERE runde_id = ? AND id = ?",
                (jobs.LAEUFT, scope.runde_id, laeuft.id),
            )
    finally:
        scope.close()
    jobs._laufend.add(laeuft.id)
    assert jobs.start(config, b, jobs.CHRONIK, lambda: "nie") is None
    assert jobs.latest(b, jobs.CHRONIK) is None


def test_die_geloeschte_runde_nimmt_die_nachbarin_nicht_mit(zwei_runden):
    """Der schärfste Fall der Trennung: hier verschwindet etwas, statt bloß sichtbar zu sein."""
    config, a, b, ids = zwei_runden
    lebenszyklus.loeschen(config, a)

    assert runden.get(config.database_path, a.id) is None
    assert runden.get(config.database_path, b.id) is not None
    assert notes.session(b, ids[2]["sitzung"]).title == f"Sitzung {MARKE[2]}"
    treffer = " ".join(str(h.snippet) for g in search.find(b, "Keller").groups for h in g.hits)
    assert MARKE[2] in treffer and MARKE[1] not in treffer

    connection = db.connect(config.database_path)
    try:
        uebrig = {
            tabelle: connection.execute(
                f"SELECT COUNT(*) AS anzahl FROM {tabelle} WHERE runde_id = ?", (a.id,)
            ).fetchone()["anzahl"]
            for tabelle in sorted(db.GESCOPTE_TABELLEN)
        }
    finally:
        connection.close()
    assert not [tabelle for tabelle, anzahl in uebrig.items() if anzahl]


def test_nachbarrunde_wird_nicht_ueber_fremdschluessel_erreicht(zwei_runden):
    """Ein Verweis auf eine fremde Sitzung ist kein Schleichweg, sondern ein Fehler."""
    config, a, _b, ids = zwei_runden
    scope = db.scoped(a)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with scope:
                scope.execute(
                    "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
                    "VALUES (?, ?, 'chronik', 'geschmuggelt', '2026-05-02T00:00:00')",
                    (scope.runde_id, ids[2]["sitzung"]),
                )
    finally:
        scope.close()
    assert protocol.stored(a, ids[2]["sitzung"]) is None


def test_der_rueckblick_erreicht_keinen_kanal_einer_fremden_gilde(zwei_runden):
    """Der Kanal ist der Ort, an dem die Gruppe ihren Rückblick liest (#182).

    Beide Gilden haben einen Kanal namens »chronik« — gleichnamig ist Absicht, wie überall
    hier. Eine Suche über alle Gilden des Bots fände die erstbeste, und eine Id lässt sich
    ohnehin von Hand in die Einstellung schreiben. Beides muss an der Gilde der Runde
    enden, sonst läse eine fremde Gruppe die Sitzung dieser hier.
    """
    config, _a, b, ids = zwei_runden
    settings.save(b, {"discord_bot_token": "platzhalter-token"})

    class ZweiGilden:
        """Zwei Gilden, in beiden ein Kanal »chronik«."""

        kanaele = {"gilde-a": "kanal-der-fremden", "gilde-b": "kanal-von-b"}

        def __init__(self):
            self.gepostet = []

        def guild_channel_id(self, guild_id, kanal):
            kennung = self.kanaele.get(guild_id)
            if kennung is None:
                return None
            return kennung if kanal in (kennung, "chronik") else None

        def post_embed(self, kanal, eingebettet):
            self.gepostet.append(kanal)

    settings.save(b, {"discord_recap_channel": "kanal-der-fremden"})
    bot = ZweiGilden()
    fremd = discord_rueckblick.deliver(config, b, ids[2]["sitzung"], client=bot)

    assert bot.gepostet == []
    assert fremd.gescheitert

    settings.save(b, {"discord_recap_channel": "chronik"})
    eigen = discord_rueckblick.deliver(config, b, ids[2]["sitzung"], client=bot)

    assert bot.gepostet == ["kanal-von-b"]
    assert not eigen.gescheitert


def test_der_briefkasten_wird_nicht_in_einer_fremden_gilde_geleert(zwei_runden):
    """Die Gegenrichtung dazu, und die schwerere (#192).

    Beide Gilden haben einen Kanal namens »diktat« — gleichnamig ist Absicht, wie überall
    hier. Eine Suche über alle Gilden fände die erstbeste, und was darin liegt, bliebe
    nicht bloß am falschen Ort liegen: es würde verschriftet und stünde danach als Notiz
    in der Chronik einer Gruppe, die es nie gesprochen hat.

    Und die Runde ohne Gilde — die aus der Zeit der Weboberfläche — bekommt keinen
    Briefkasten geliehen: geraten wäre hier genau derselbe Griff in eine fremde Gilde.
    """
    config, a, b, _ids = zwei_runden
    settings.save(b, {"discord_bot_token": "platzhalter-token"})
    fremd = "Die Nachbarn sprachen über ihren eigenen Keller."

    class ZweiBriefkaesten:
        """Zwei Gilden, in beiden ein »diktat«; nur einer gehört dieser Runde."""

        kanaele = {"gilde-a": "briefkasten-der-fremden", "gilde-b": "briefkasten-von-b"}
        einwuerfe = {
            "briefkasten-der-fremden": (Message(id="200", content=fremd, from_bot=False),),
            "briefkasten-von-b": (
                Message(id="100", content="Der Keller war eine Falle.", from_bot=False),
            ),
        }

        def __init__(self):
            self.gelesen = []

        def guild_channel_id(self, guild_id, kanal):
            kennung = self.kanaele.get(guild_id)
            if kennung is None:
                return None
            return kennung if kanal in (kennung, discord_service.KANAL) else None

        def messages(self, kanal, *, after=None, limit=100):
            self.gelesen.append(kanal)
            return self.einwuerfe.get(kanal, ())

        def react(self, kanal, message_id, emoji):
            return None

        def reply(self, kanal, message_id, text):
            return None

    bot = ZweiBriefkaesten()
    assert discord_service.run(config, a, client=bot) == (discord_service.OHNE_GILDE,)
    assert bot.gelesen == []

    discord_service.run(config, b, client=bot)

    assert bot.gelesen == ["briefkasten-von-b"]
    assert discord_service.cursor(b) == "100"
    texte = [
        eintrag.text
        for sitzung in notes.sessions(b)
        for szene in notes.session(b, sitzung.id).scenes
        for eintrag in szene.notes
    ]
    assert "Der Keller war eine Falle." in texte
    assert fremd not in texte
