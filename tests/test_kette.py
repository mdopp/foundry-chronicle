"""Die ganze Kette an einer erfundenen Sitzung: Spuren → Verschränkung → Chronik.

Jede Stufe für sich war geprüft, die Kette als Ganzes nie — und genau dort lag der Bruch,
der beim ersten vollständigen Lauf herauskam (#140): die Verschriftung erzeugt
Transkripte, ``compose.service.material()`` liest sie nicht, und die einzige Brücke ist
eine Formularroute der Weboberfläche, die mit #62/#69 verschwindet. Ein Test, der nur
Stufen prüft, sieht so etwas nie. Dieser hier schickt eine Sitzung durch alles hindurch.

Die Sitzung ist synthetisch und bleibt es: vier Sprecher, erfundene Namen, ein erfundener
Ort. Sie ist trotzdem so gebaut, wie Menschen wirklich reden — jemand fällt ins Wort, ein
Satz bleibt halb liegen, zwei antworten gleichzeitig, einer wiederholt sich, und
dazwischen liegen Minuten, in denen niemand etwas sagt. Die Spuren liegen in der Form, die
der Aufnahme-Bot ablegt: je Sprecher eine ``recording``-Zeile mit ``discord_user_id``, dazu
``transcript`` und ``transcript_segment`` mit sitzungsabsoluten ``start_ms``.

Das Sprachmodell ist gemockt, aber nicht stumpf: es liest den Auftrag und antwortet
darauf — es nennt einen Namen aus der Vorlage und setzt eine belegte Summe aus einem
Foundry-Fakt ein. Für genau eine Szene erfindet es eine Zahl. Ein Mock mit fester Antwort
bestätigte nur sich selbst; die Zahlenschranke braucht ein Modell, das sie wirklich
verletzt.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from conftest import runde

from chronicle import consent, db, notes, people, recordings
from chronicle import runde as runden
from chronicle.compose.composer import BELEG_TITEL, NOTIZEN_TITEL, VERBINDUNG_TITEL, VERWORFEN
from chronicle.compose.service import compose_session
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.foundry.world import project
from chronicle.transcribe import merge
from chronicle.transcribe import service as transcribe

STAND = "2026-08-11T21:00:00+00:00"

# --- Die Runde, die spricht ----------------------------------------------------------

WINDHALM = consent.Member(id="8001", name="windhalm")
MORGENTAU = consent.Member(id="8002", name="morgentau")
KESSELFLICK = consent.Member(id="8003", name="kesselflick")
RANDLAEUFER = consent.Member(id="8004", name="randlaeufer")

MITGLIEDER = (WINDHALM, MORGENTAU, KESSELFLICK, RANDLAEUFER)

# Zwei Spuren sind einem Foundry-Konto zugeordnet und tragen dessen Namen, zwei nicht und
# behalten den Discord-Namen. Beide Beschriftungswege kommen in einer echten Runde vor.
MORGENTAU_IN_FOUNDRY = "u-morgentau"
KESSELFLICK_IN_FOUNDRY = "u-kesselflick"

BESCHRIFTUNG = ("windhalm", "Morgentau", "Kesselflick", "randlaeufer")

# --- Das Gespräch --------------------------------------------------------------------
#
# ``(Sprecher, start_ms, end_ms, Text)`` auf der Sitzungsuhr. Die Marken überlappen und
# greifen ineinander — genau der Fall, den eine Sortierung nach Spur zerstören würde.

HALBER_SATZ = "Der Regen hat die Grabsteine glattgewaschen, und hinter dem Tor steht"
UNTERBRECHUNG = "Warte —"
FORTSETZUNG = "— steht ein Karren ohne Zugtier."
GLEICHZEITIG_FRAGE = "Ohne Zugtier?"
GLEICHZEITIG_ANTWORT = "Ohne Zugtier."
WIEDERHOLUNG = "Ich gehe zum Karren, sagte ich."

GESPRAECH = (
    (WINDHALM, 0, 4200, HALBER_SATZ),
    (MORGENTAU, 3800, 5200, UNTERBRECHUNG),
    (WINDHALM, 4200, 7000, FORTSETZUNG),
    (KESSELFLICK, 6800, 8100, GLEICHZEITIG_FRAGE),
    (RANDLAEUFER, 7000, 8300, GLEICHZEITIG_ANTWORT),
    # Eine Viertelminute, in der niemand etwas sagt.
    (MORGENTAU, 26000, 28500, "Ich gehe zum Karren."),
    (RANDLAEUFER, 27400, 29000, "Ich gehe mit."),
    (MORGENTAU, 28500, 30000, WIEDERHOLUNG),
    (KESSELFLICK, 30500, 32000, "Dann geht ihr beide, und ich"),
    (WINDHALM, 31500, 33200, "Wer die Plane anhebt, würfelt auf Instinkt."),
    (KESSELFLICK, 33200, 34400, "— und ich bleibe am Tor."),
    # Eine halbe Minute Stille, dann geht es unter der Plane weiter.
    (MORGENTAU, 61000, 63200, "Unter der Plane liegt eine Kiste mit einem Wachssiegel."),
    (WINDHALM, 62600, 64100, "Das Siegel ist frisch."),
    (WINDHALM, 64100, 64400, "Ganz frisch."),
    (RANDLAEUFER, 64500, 66000, "Frisch heißt, jemand war vor uns hier."),
    (KESSELFLICK, 66200, 68000, "Oder jemand ist noch hier."),
)

VERSCHMOLZEN = "Das Siegel ist frisch. Ganz frisch."

# --- Die Foundry-Fakten --------------------------------------------------------------

UNSER_KONTO = "u-chronist"
LEITUNG = "u-leitung"

INSTINKT_SUMME = 14
SIEGEL_SUMME = 19

FAKT_TOR = "m-tor"
FAKT_INSTINKT = "m-instinkt"
FAKT_SIEGEL = "m-siegel"

WELT = {
    "world": {"id": "das-tor-der-grauen-gasse", "title": "Das Tor der Grauen Gasse"},
    "system": {"id": "daggerheart"},
    "users": [
        {"_id": UNSER_KONTO, "name": "Chronist", "role": 1},
        {"_id": LEITUNG, "name": "Spielleitung", "role": 4},
        {"_id": MORGENTAU_IN_FOUNDRY, "name": "Morgentau", "role": 1},
        {"_id": KESSELFLICK_IN_FOUNDRY, "name": "Kesselflick", "role": 1},
    ],
    "actors": [
        {
            "_id": "a-yrsa",
            "name": "Yrsa Grauhalm",
            "type": "character",
            "ownership": {"default": 2, MORGENTAU_IN_FOUNDRY: 3},
        },
        {
            "_id": "a-bodo",
            "name": "Bodo Kesselflick",
            "type": "character",
            "ownership": {"default": 2, KESSELFLICK_IN_FOUNDRY: 3},
        },
    ],
    "messages": [
        {
            "_id": FAKT_TOR,
            "timestamp": 1000,
            "author": MORGENTAU_IN_FOUNDRY,
            "content": "Wir standen vor dem Tor, als der Regen einsetzte.",
            "speaker": {"actor": "a-yrsa", "alias": "Yrsa Grauhalm"},
        },
        {
            "_id": FAKT_INSTINKT,
            "timestamp": 2000,
            "author": MORGENTAU_IN_FOUNDRY,
            "content": "",
            "speaker": {"actor": "a-yrsa", "alias": "Yrsa Grauhalm"},
            "system": {
                "roll": {
                    "title": "Instinkt",
                    "total": INSTINKT_SUMME,
                    "formula": "1d12 + 1d12 + 2",
                    "type": "action",
                    "isCritical": False,
                    "modifierTotal": 2,
                    "hope": {"dice": "d12", "value": 8},
                    "fear": {"dice": "d12", "value": 4},
                }
            },
        },
        {
            "_id": FAKT_SIEGEL,
            "timestamp": 3000,
            "author": KESSELFLICK_IN_FOUNDRY,
            "content": "",
            "speaker": {"actor": "a-bodo", "alias": "Bodo Kesselflick"},
            "system": {
                "roll": {
                    "title": "Wissen",
                    "total": SIEGEL_SUMME,
                    "formula": "1d12 + 1d12 + 4",
                    "type": "action",
                    "isCritical": False,
                    "modifierTotal": 4,
                    "hope": {"dice": "d12", "value": 9},
                    "fear": {"dice": "d12", "value": 6},
                }
            },
        },
    ],
}

ZWEITE_SZENE = "Der Karren ohne Zugtier"
DRITTE_SZENE = "Was unter der Plane lag"

# Wann die Trennlinien gezogen wurden, in Sekunden nach dem Aufnahmebeginn. Beide liegen
# **mitten im Gespräch**: die zweite Szene beginnt, während Kesselflick noch redet, die
# dritte in der Stille davor. Genau daran zeigt sich die Regel — eine Äußerung gehört in
# die Szene ihres Startzeitpunkts, auch wenn sie über die Linie hinausredet.
SZENE_ZWEI_AB = 30
SZENE_DREI_AB = 60

# Wer wohin gehört, wenn die Regel gilt. Von Hand aus GESPRAECH abgelesen: alles vor
# 30 s in die erste, alles ab 30 s in die zweite, alles ab 60 s in die dritte Szene.
JE_SZENE = (
    (
        HALBER_SATZ,
        UNTERBRECHUNG,
        FORTSETZUNG,
        GLEICHZEITIG_FRAGE,
        GLEICHZEITIG_ANTWORT,
        "Ich gehe zum Karren.",
        "Ich gehe mit.",
        WIEDERHOLUNG,
    ),
    (
        "Dann geht ihr beide, und ich",
        "Wer die Plane anhebt, würfelt auf Instinkt.",
        "— und ich bleibe am Tor.",
    ),
    (
        "Unter der Plane liegt eine Kiste mit einem Wachssiegel.",
        VERSCHMOLZEN,
        "Frisch heißt, jemand war vor uns hier.",
        "Oder jemand ist noch hier.",
    ),
)

# --- Die Nachbarrunde ----------------------------------------------------------------
#
# Sie spielt etwas ganz anderes und darf in der Chronik der ersten mit keinem Wort
# vorkommen — auch nicht im Auftrag ans Sprachmodell.

NACHBARIN = consent.Member(id="9001", name="pfeifenkraut")
FREMDES_WORT = "Kupferpfeife"
FREMDE_SUMME = 27
FREMDE_FIGUR = f"Traegerin der {FREMDES_WORT}"

FREMDE_WELT = {
    "world": {"id": "der-hohle-turm", "title": "Der Hohle Turm"},
    "system": {"id": "daggerheart"},
    "users": [{"_id": UNSER_KONTO, "name": "Chronist", "role": 1}],
    "actors": [
        {
            "_id": "a-fremd",
            "name": FREMDE_FIGUR,
            "type": "character",
            "ownership": {"default": 2, UNSER_KONTO: 3},
        }
    ],
    "messages": [
        {
            "_id": "m-fremd",
            "timestamp": 1000,
            "author": UNSER_KONTO,
            "content": f"Die {FREMDES_WORT} lag noch auf dem Tisch.",
            "speaker": {"actor": "a-fremd", "alias": FREMDE_FIGUR},
            "system": {
                "roll": {
                    "title": "Wissen",
                    "total": FREMDE_SUMME,
                    "formula": "1d12 + 1d12 + 5",
                    "modifierTotal": 5,
                    "hope": {"dice": "d12", "value": 11},
                    "fear": {"dice": "d12", "value": 11},
                }
            },
        }
    ],
}

# --- Das gemockte Sprachmodell -------------------------------------------------------

ERFUNDENE_ZAHL = "2317"

# Woran der Mock erkennt, was ihm vorliegt: die Aufzählungszeilen der Vorlage tragen vorn
# den Sprecher, und ein belegter Wurf nennt seine Summe.
NAME = re.compile(r"^- ([^:—\n]+?)\s*[:—]", re.MULTILINE)
SUMME = re.compile(r"Summe (\d+)")

GEORDNET = "Am Tor blieb die Gruppe beieinander; {wer} ging voran, mehr steht nicht fest."
BELEGT = "{wer} nahm die Probe auf sich, und sie ging auf Summe {summe} aus."
ERFUNDEN = "{wer} hob die Plane an, und darunter lagen {zahl} Muenzen aus dem Turm."
OHNE_VORLAGE = "Ohne Vorlage bleibt nichts zu ordnen."


class Mitschreiber:
    """Ein Sprachmodell-Ersatz, der die Vorlage wirklich liest.

    Er antwortet auf das, was im Auftrag steht: den ersten Sprecher der Vorlage und die
    Summe eines belegten Wurfs. Für die benannte Szene erfindet er stattdessen eine Zahl —
    ohne sie hätte die Zahlenschranke nichts zu fangen und der Test bewiese sie nicht.
    """

    name = "kettenmock"

    def __init__(self, *, vergiftete_szene: str) -> None:
        self._vergiftet = vergiftete_szene
        self.auftraege: list[str] = []
        self.antworten: list[str] = []

    def write(self, *, system: str, prompt: str) -> str:
        self.auftraege.append(prompt)
        gefunden = NAME.search(prompt)
        wer = gefunden.group(1) if gefunden else ""
        if not wer:
            text = OHNE_VORLAGE
        elif self._vergiftet in prompt:
            text = ERFUNDEN.format(wer=wer, zahl=ERFUNDENE_ZAHL)
        elif (summe := SUMME.search(prompt)) is not None:
            text = BELEGT.format(wer=wer, summe=summe.group(1))
        else:
            text = GEORDNET.format(wer=wer)
        self.antworten.append(text)
        return text


# --- Aufbau --------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")


def angesagt(gastgeber, *mitglieder, guild_id="g-erste"):
    consent.record(
        gastgeber,
        session_id=None,
        kind=consent.ANSAGE,
        guild_id=guild_id,
        channel_id=f"c-{guild_id}",
        channel_name="Am Tisch",
        text="Ich schneide mit.",
        members=tuple(mitglieder),
    )


def spuren(gastgeber, sitzung_id, gespraech, beginn_at):
    """Je Sprecher eine Spur mit Discord-Id und Aufnahmebeginn — so legt der Bot sie ab."""
    je_sprecher: dict[str, list[tuple[int, int, str]]] = {}
    for wer, beginn, ende, text in gespraech:
        je_sprecher.setdefault(wer.id, []).append((beginn, ende, text))
    scope = db.scoped(gastgeber)
    try:
        for wer in dict.fromkeys(eintrag[0] for eintrag in gespraech):
            recordings.enqueue(
                gastgeber,
                sitzung_id,
                f"{wer.name}.wav",
                discord_user_id=wer.id,
                started_at=beginn_at,
            )
            transcribe.store(scope, sitzung_id, wer.name, tuple(je_sprecher[wer.id]), STAND)
    finally:
        scope.close()


def sitzungsbeginn(gastgeber, sitzung_id):
    """Wann die erste Szene gezogen wurde — der Nullpunkt, auf den die Spuren gelegt werden.

    Die erste Szene entsteht mit der Sitzung und trägt die Wanduhr; die Aufnahme beginnt
    im selben Moment. Damit liegen Sitzungsuhr und Szenengrenzen auf derselben Achse,
    ohne dass irgendwo ein Zeitpunkt erfunden würde.
    """
    scope = db.scoped(gastgeber)
    try:
        return scope.execute(
            "SELECT created_at FROM scene WHERE runde_id = ? AND session_id = ? "
            "ORDER BY position LIMIT 1",
            (scope.runde_id, sitzung_id),
        ).fetchone()["created_at"]
    finally:
        scope.close()


def versetzt(zeitpunkt, sekunden):
    return (datetime.fromisoformat(zeitpunkt) + timedelta(seconds=sekunden)).isoformat(
        timespec="seconds"
    )


def welt_einspielen(gastgeber, roh):
    """Derselbe Weg wie ein Abgleich: Berechtigungsfilter, Adapter, Zwischenspeicher."""
    scope = db.scoped(gastgeber)
    try:
        store.save(scope, project(roh, UNSER_KONTO, fetched_at=STAND))
    finally:
        scope.close()


def verknuepfe(gastgeber, paare):
    """Fakt an Szene hängen — dafür gibt es bis heute keine Ansicht, nur die Tabelle."""
    scope = db.scoped(gastgeber)
    try:
        with scope:
            scope.executemany(
                "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) "
                "VALUES (?, ?, ?)",
                [(scope.runde_id, *paar) for paar in paare],
            )
    finally:
        scope.close()


@pytest.fixture
def sitzung_id(config):
    """Die synthetische Sitzung, vollständig aufgebaut: Spuren, Szenen, Foundry-Fakten."""
    gastgeber = runde(config)
    welt_einspielen(gastgeber, WELT)
    angesagt(gastgeber, *MITGLIEDER)
    people.confirm(
        gastgeber,
        {MORGENTAU.id: MORGENTAU_IN_FOUNDRY, KESSELFLICK.id: KESSELFLICK_IN_FOUNDRY},
    )
    sitzung = notes.create_session(gastgeber, played_on="2026-08-11", title="Die Graue Gasse")
    beginn = sitzungsbeginn(gastgeber, sitzung)
    spuren(gastgeber, sitzung, GESPRAECH, beginn)
    for titel, ab in ((ZWEITE_SZENE, SZENE_ZWEI_AB), (DRITTE_SZENE, SZENE_DREI_AB)):
        notes.add_scene(gastgeber, sitzung, title=titel, at=versetzt(beginn, ab))
    szenen = notes.session(gastgeber, sitzung).scenes
    verknuepfe(
        gastgeber,
        zip(
            (szene.id for szene in szenen),
            (FAKT_TOR, FAKT_INSTINKT, FAKT_SIEGEL),
            strict=True,
        ),
    )
    return sitzung


@pytest.fixture
def nachbarin(config, sitzung_id):
    """Eine zweite Runde mit eigener Sitzung, eigenen Spuren, eigenen Zahlen."""
    zweite = runden.anlegen(config.database_path, "Der Hohle Turm", guild_id="g-zweite")
    welt_einspielen(zweite, FREMDE_WELT)
    angesagt(zweite, NACHBARIN, guild_id="g-zweite")
    sitzung = notes.create_session(zweite, played_on="2026-08-11", title=FREMDES_WORT)
    spuren(
        zweite,
        sitzung,
        (
            (NACHBARIN, 0, 3000, f"Die {FREMDES_WORT} lag noch auf dem Tisch."),
            (NACHBARIN, 9000, 11000, f"Niemand ruehrte die {FREMDES_WORT} an."),
        ),
        sitzungsbeginn(zweite, sitzung),
    )
    szene = notes.session(zweite, sitzung).scenes[0]
    notes.add_note(zweite, szene.id, f"Die {FREMDES_WORT} blieb liegen.")
    verknuepfe(zweite, [(szene.id, "m-fremd")])
    return zweite, sitzung


@pytest.fixture
def modell():
    return Mitschreiber(vergiftete_szene=DRITTE_SZENE)


@pytest.fixture
def unterhaltung(config, sitzung_id):
    return merge.conversation(runde(config), sitzung_id)


@pytest.fixture
def uebernommen(config, sitzung_id):
    """Der Übergang, den ``jobs.chronik`` vor der Komposition geht — hier für sich."""
    return merge.uebernehmen(runde(config), sitzung_id)


@pytest.fixture
def chronik(config, sitzung_id, uebernommen, modell):
    return compose_session(config, runde(config), sitzung_id, model=modell)


# --- Die Verschränkung ---------------------------------------------------------------


def test_die_spuren_werden_nach_zeit_verschraenkt_und_nicht_nach_spur(unterhaltung):
    """Wer ins Wort fällt, steht auch im Ergebnis dazwischen."""
    texte = [a.text for a in unterhaltung]

    assert [a.start_ms for a in unterhaltung] == sorted(a.start_ms for a in unterhaltung)
    # Nach Spur sortiert stünde die Unterbrechung hinter dem ganzen Satz; hier steht sie
    # zwischen seinen Hälften.
    assert texte.index(HALBER_SATZ) < texte.index(UNTERBRECHUNG) < texte.index(FORTSETZUNG)
    # Zwei reden gleichzeitig: beide stehen da, in der Reihenfolge ihres Beginns.
    assert texte.index(GLEICHZEITIG_FRAGE) + 1 == texte.index(GLEICHZEITIG_ANTWORT)
    assert texte.index("Ich gehe mit.") < texte.index(WIEDERHOLUNG)


def test_die_beschriftung_kommt_aus_der_bestaetigten_zuordnung(unterhaltung):
    """Bestätigt steht der Foundry-Name da, sonst der Discord-Name — nie ein Vorschlag."""
    assert {a.speaker for a in unterhaltung} == set(BESCHRIFTUNG)
    assert [a.speaker for a in unterhaltung][:3] == ["windhalm", "Morgentau", "windhalm"]


def test_zwei_segmente_derselben_stimme_werden_ein_beitrag(unterhaltung):
    """Der Nachsatz gehört zum Satz davor, solange niemand dazwischenredet."""
    assert VERSCHMOLZEN in [a.text for a in unterhaltung]
    assert len(unterhaltung) == len(GESPRAECH) - 1


# --- Der Durchstich ------------------------------------------------------------------


def test_jeder_redebeitrag_kommt_in_der_chronik_an(unterhaltung, chronik, modell):
    """Der Bruch, den kein Stufentest sah: ein Transkript, erzeugt und fallengelassen."""
    fehlend = [a.text for a in unterhaltung if a.text not in chronik.text]

    assert fehlend == []
    # Es reicht nicht, dass die Sätze irgendwo stehen: das Modell muss sie gesehen haben,
    # sonst ordnet es an ihnen vorbei.
    gezeigt = "\n".join(modell.auftraege)
    assert [a.text for a in unterhaltung if a.text not in gezeigt] == []
    assert NOTIZEN_TITEL in chronik.text


def test_jede_aussage_faellt_in_die_szene_ihres_starts(config, sitzung_id, uebernommen):
    """Wer über die Trennlinie hinausredet, bleibt trotzdem in der Szene, in der er anfing."""
    assert uebernommen == 3

    szenen = notes.session(runde(config), sitzung_id).scenes
    assert [len(szene.notes) for szene in szenen] == [1, 1, 1]
    for szene, erwartet in zip(szenen, JE_SZENE, strict=True):
        zeilen = szene.notes[0].text.splitlines()
        assert [zeile.split(": ", 1)[1] for zeile in zeilen] == list(erwartet)


def test_ein_zweiter_abschluss_legt_das_gespraech_nicht_ein_zweites_mal(
    config, sitzung_id, uebernommen
):
    """Der Lauf ist wiederholbar — sonst stünde nach dem zweiten alles doppelt da."""
    vorher = notes.session(runde(config), sitzung_id)

    assert merge.uebernehmen(runde(config), sitzung_id) == 3

    nachher = notes.session(runde(config), sitzung_id)
    assert nachher.note_count == vorher.note_count
    assert [[notiz.text for notiz in szene.notes] for szene in nachher.scenes] == [
        [notiz.text for notiz in szene.notes] for szene in vorher.scenes
    ]


def test_eine_von_hand_geschriebene_notiz_ueberlebt_den_zweiten_lauf(
    config, sitzung_id, uebernommen
):
    """Ersetzt wird nur Abgeleitetes — was ein Mensch tippte, räumt kein Lauf fort."""
    gastgeber = runde(config)
    erste = notes.session(gastgeber, sitzung_id).scenes[0]
    notes.add_note(gastgeber, erste.id, "Am Tor roch es nach nassem Stein.")

    merge.uebernehmen(gastgeber, sitzung_id)

    texte = [notiz.text for notiz in notes.session(gastgeber, sitzung_id).scenes[0].notes]
    assert "Am Tor roch es nach nassem Stein." in texte
    assert len(texte) == 2


# --- Die Zahlenschranke --------------------------------------------------------------


def test_eine_unbelegte_zahl_wird_verworfen_und_die_verwerfung_steht_da(chronik, modell):
    """Erfinden ist der teuerste Fehler — und die Lücke bleibt sichtbar."""
    assert any(ERFUNDENE_ZAHL in antwort for antwort in modell.antworten)

    assert ERFUNDENE_ZAHL not in chronik.text
    assert VERWORFEN in chronik.text


def test_eine_belegte_zahl_bleibt_im_verbindungstext_stehen(chronik, modell):
    """Die Schranke fängt das Erfundene, nicht das Belegte — sonst bliebe nur die Liste.

    Wen der Mock nennt, ist der erste Name in seiner Vorlage — seit die Übernahme steht,
    ist das eine Stimme aus dem Transkript und nicht mehr der Sprecher des Wurfs. Genau
    das ist der Beleg, dass das Gespräch vor den Zahlen im Auftrag steht.
    """
    belegter_satz = BELEGT.format(wer="Kesselflick", summe=INSTINKT_SUMME)

    assert belegter_satz in modell.antworten
    assert belegter_satz in chronik.text
    assert f"Summe {INSTINKT_SUMME}" in chronik.text
    assert f"Summe {SIEGEL_SUMME}" in chronik.text


def test_die_chronik_trennt_belegtes_von_verbindungstext(chronik, modell):
    """Wochen später muss man Satz für Satz sehen, was belegt war."""
    assert BELEG_TITEL in chronik.text
    assert VERBINDUNG_TITEL in chronik.text
    assert "nicht belegt" in VERBINDUNG_TITEL
    assert (chronik.scene_count, chronik.fact_count, chronik.prose_count) == (3, 3, 2)
    assert chronik.model_name == modell.name


# --- Die Rundentrennung --------------------------------------------------------------


def test_die_nachbarrunde_kommt_in_der_chronik_der_ersten_nicht_vor(
    nachbarin, unterhaltung, chronik, modell
):
    """Die Trennung zwischen Runden ist die wichtigste Sicherheitseigenschaft."""
    zweite, fremde_sitzung = nachbarin

    assert FREMDES_WORT not in chronik.text
    assert FREMDES_WORT not in "\n".join(modell.auftraege)
    assert str(FREMDE_SUMME) not in chronik.text
    assert all(FREMDES_WORT not in a.text for a in unterhaltung)

    # Die Gegenprobe: die zweite Runde hat ihre eigene Unterhaltung, und in der steht
    # nichts von der ersten.
    drueben = merge.conversation(zweite, fremde_sitzung)
    eigene = [a.text for a in unterhaltung]
    assert [a.speaker for a in drueben] == [NACHBARIN.name]
    assert all(a.text not in eigene for a in drueben)
