"""Aus fünf Monologen eine Unterhaltung — und aus der eine Notiz.

Geprüft wird vor allem das, was still schiefgehen würde: nach Spur statt nach Zeit zu
sortieren, einen Sprechernamen zu raten, oder einer Präsenzrunde eine Zeitachse
anzudichten, die es dort nicht gibt.
"""

from __future__ import annotations

import pytest
from conftest import runde

from chronicle import consent, db, notes, people, recordings
from chronicle.compose.composer import TRANSKRIPT_TITEL, Notiz, compose
from chronicle.compose.service import material
from chronicle.config import Config
from chronicle.transcribe import merge, service

STAND = "2026-08-06T20:00:00+00:00"

MIRA = consent.Member(id="4001", name="Mira")
DAVEY = consent.Member(id="4002", name="Davey")

MIRA_IN_FOUNDRY = "u-mira"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")


@pytest.fixture
def sitzung_id(config):
    db.init(config.database_path)
    return notes.create_session(runde(config), played_on="2026-08-06", title="Der Keller")


def angesagt(config, *mitglieder):
    consent.record(
        runde(config),
        session_id=None,
        kind=consent.ANSAGE,
        guild_id="g-1",
        channel_id="c-1",
        channel_name="Am Tisch",
        text="Ich schneide mit.",
        members=tuple(mitglieder),
    )


def spur(
    config,
    sitzung_id,
    wer,
    *segmente,
    discord_user_id="egal",
    started_at=None,
    discord_name=None,
):
    """Eine aufgenommene Spur samt Transkript — ``segmente`` sind (ms, ms, Text)."""
    kennung = None if discord_user_id is None else (wer.id if wer else discord_user_id)
    name = (wer.name if wer else discord_user_id).lower()
    recordings.enqueue(
        runde(config),
        sitzung_id,
        f"{name}.wav",
        discord_user_id=kennung,
        discord_name=discord_name,
        started_at=started_at,
    )
    scope = db.scoped(runde(config))
    try:
        service.store(scope, sitzung_id, name, tuple(segmente), STAND)
    finally:
        scope.close()


def diktat(config, sitzung_id, *segmente, discord_user_id=None, message_at=None, name="memo"):
    recordings.enqueue(
        runde(config),
        sitzung_id,
        f"{name}.m4a",
        discord_user_id=discord_user_id,
        message_at=message_at,
    )
    scope = db.scoped(runde(config))
    try:
        service.store(scope, sitzung_id, name, tuple(segmente), STAND)
    finally:
        scope.close()


def foundry_spieler(config, kennung, name):
    scope = db.scoped(runde(config))
    try:
        with scope:
            scope.execute(
                "INSERT INTO foundry_player (runde_id, id, name, role, is_gm) "
                "VALUES (?, ?, ?, 1, 0)",
                (scope.runde_id, kennung, name),
            )
    finally:
        scope.close()


def zwei_stimmen(config, sitzung_id):
    angesagt(config, MIRA, DAVEY)
    spur(
        config,
        sitzung_id,
        MIRA,
        (0, 2000, "Wir brechen bei Sonnenaufgang auf."),
        (10000, 12000, "Da liegt etwas vor der Tür."),
    )
    spur(
        config,
        sitzung_id,
        DAVEY,
        (5000, 7000, "Warte, ich sehe nach."),
        (15000, 17000, "Es ist verriegelt."),
    )


# --- Die Zeitachse ------------------------------------------------------------------


def test_verschraenkt_wird_nach_zeit_und_nicht_nach_spur(config, sitzung_id):
    zwei_stimmen(config, sitzung_id)

    unterhaltung = merge.conversation(runde(config), sitzung_id)

    assert [a.speaker for a in unterhaltung] == ["Mira", "Davey", "Mira", "Davey"]
    assert [a.start_ms for a in unterhaltung] == [0, 5000, 10000, 15000]


def test_eine_luecke_in_einer_spur_verschiebt_die_andere_nicht(config, sitzung_id):
    angesagt(config, MIRA, DAVEY)
    # Mira schweigt eine Viertelstunde; die Marke danach bleibt trotzdem die des Bandes.
    spur(config, sitzung_id, MIRA, (0, 2000, "Anfang."), (900000, 902000, "Und weiter."))
    spur(config, sitzung_id, DAVEY, (60000, 62000, "Dazwischen."))

    unterhaltung = merge.conversation(runde(config), sitzung_id)

    assert [a.marke for a in unterhaltung] == ["0:00:00", "0:01:00", "0:15:00"]
    assert [a.speaker for a in unterhaltung] == ["Mira", "Davey", "Mira"]


def test_zwei_saetze_derselben_stimme_werden_ein_beitrag(config, sitzung_id):
    angesagt(config, MIRA, DAVEY)
    spur(config, sitzung_id, MIRA, (0, 2000, "Wir gehen."), (2000, 4000, "Und zwar jetzt."))
    spur(config, sitzung_id, DAVEY, (6000, 7000, "Meinetwegen."))

    unterhaltung = merge.conversation(runde(config), sitzung_id)

    assert len(unterhaltung) == 2
    assert unterhaltung[0].text == "Wir gehen. Und zwar jetzt."
    assert unterhaltung[0].end_ms == 4000


# --- Die Beschriftung ---------------------------------------------------------------


def test_eine_bestaetigte_zuordnung_setzt_den_foundry_namen(config, sitzung_id):
    zwei_stimmen(config, sitzung_id)
    foundry_spieler(config, MIRA_IN_FOUNDRY, "Mira Hartleib")
    people.confirm(runde(config), {MIRA.id: MIRA_IN_FOUNDRY})

    unterhaltung = merge.conversation(runde(config), sitzung_id)

    assert unterhaltung[0].speaker == "Mira Hartleib"
    assert unterhaltung[0].zugeordnet is True


def test_ohne_bestaetigung_steht_der_discord_name_da_und_kein_vorschlag(config, sitzung_id):
    zwei_stimmen(config, sitzung_id)
    # Der Name wäre auf Anhieb vorzuschlagen — an einer Spur sähe ein Vorschlag aber aus
    # wie eine Tatsache, und genau das darf hier nicht passieren.
    foundry_spieler(config, MIRA_IN_FOUNDRY, "Mira")

    unterhaltung = merge.conversation(runde(config), sitzung_id)

    assert unterhaltung[0].speaker == "Mira"
    assert unterhaltung[0].zugeordnet is False


def test_eine_spur_ohne_einwilligungseintrag_nimmt_den_nachgetragenen_namen(config, sitzung_id):
    """Wer in keinem Einwilligungsprotokoll steht, wird trotzdem beim Namen genannt.

    Ohne den ``members``-Intent kennt der Bot nur, wer ihm einmal begegnet ist; der Name
    kommt dann aus dem gezielten Nachschlagen an der Spur (#250).
    """
    spur(
        config,
        sitzung_id,
        None,
        (0, 2000, "Wer war das?"),
        discord_user_id="4009",
        discord_name="Samuel",
    )

    unterhaltung = merge.conversation(runde(config), sitzung_id)

    assert unterhaltung[0].speaker == "Samuel"
    assert unterhaltung[0].zugeordnet is False


def test_eine_spur_ohne_jeden_namen_heisst_unbekannt_und_nie_nach_ihrer_datei(config, sitzung_id):
    """Der Dateiname ist kein Sprecher.

    Bis #250 stand in der fertigen Chronik ``sitzung4-20260818T201032-unbekannt`` als
    Sprecherangabe — ein technischer Bezeichner in einem Text, den die Gruppe Wochen
    später als Gedächtnisstütze liest. Schlägt das Nachtragen fehl, bleibt es beim
    **Wort**.
    """
    spur(config, sitzung_id, None, (0, 2000, "Wer war das?"), discord_user_id="4009")

    unterhaltung = merge.conversation(runde(config), sitzung_id)

    assert unterhaltung[0].speaker == consent.UNBEKANNT
    assert unterhaltung[0].zugeordnet is False


# --- Der Präsenzweg bleibt, wie er war ----------------------------------------------


def test_ein_diktat_ohne_discord_id_kommt_nicht_auf_die_zeitachse(config, sitzung_id):
    diktat(config, sitzung_id, (0, 2000, "Auf dem Heimweg fällt mir ein:"))

    assert merge.conversation(runde(config), sitzung_id) == ()


# --- Die selbsttätige Übernahme (#140) ----------------------------------------------

BEGINN = "2026-08-06T20:00:00+00:00"


def mitschnitt(config, sitzung_id, *, beginn=BEGINN):
    """Zwei Spuren mit festgehaltenem Aufnahmebeginn — so kommt der Bot-Mitschnitt an."""
    angesagt(config, MIRA, DAVEY)
    spur(config, sitzung_id, MIRA, (0, 2000, "Wir gehen hinunter."), started_at=beginn)
    spur(config, sitzung_id, DAVEY, (40000, 42000, "Hier riecht es nach Rauch."), started_at=beginn)


def notiztexte(config, sitzung_id):
    daten = notes.session(runde(config), sitzung_id)
    return [notiz.text for szene in daten.scenes for notiz in szene.notes]


def test_die_aussage_faellt_in_die_szene_ihres_startzeitpunkts(config, sitzung_id):
    mitschnitt(config, sitzung_id)
    notes.add_scene(runde(config), sitzung_id, title="Unten", at="2026-08-06T20:00:30+00:00")

    assert merge.uebernehmen(runde(config), sitzung_id) == 2

    szenen = notes.session(runde(config), sitzung_id).scenes
    assert szenen[0].notes[0].text == "Mira: Wir gehen hinunter."
    assert szenen[1].notes[0].text == "Davey: Hier riecht es nach Rauch."


def test_ohne_festgehaltenen_beginn_wird_nichts_zugeordnet(config, sitzung_id):
    """Keine Sitzungsuhr, keine Zuordnung — der Nullpunkt wird nicht geschätzt."""
    angesagt(config, MIRA)
    spur(config, sitzung_id, MIRA, (0, 2000, "Wir gehen hinunter."))

    assert merge.uebernehmen(runde(config), sitzung_id) == 0
    assert notiztexte(config, sitzung_id) == []


def test_ein_diktat_im_thread_wird_nicht_auf_die_sitzungsuhr_gelegt(config, sitzung_id):
    """Es trägt die Discord-Id seines Absenders und trotzdem keinen Aufnahmebeginn."""
    mitschnitt(config, sitzung_id)
    diktat(config, sitzung_id, (0, 2000, "Auf dem Heimweg faellt mir ein:"), discord_user_id="4009")

    merge.uebernehmen(runde(config), sitzung_id)

    assert "Auf dem Heimweg faellt mir ein:" not in " ".join(notiztexte(config, sitzung_id))


def test_eine_sitzung_ohne_szene_kommt_ohne_notiz_davon(config, sitzung_id):
    """Die erste Szene entsteht mit der Sitzung; ist sie fort, gibt es kein Ziel."""
    mitschnitt(config, sitzung_id)
    scope = db.scoped(runde(config))
    try:
        with scope:
            scope.execute(
                "DELETE FROM scene WHERE runde_id = ? AND session_id = ?",
                (scope.runde_id, sitzung_id),
            )
    finally:
        scope.close()

    assert merge.uebernehmen(runde(config), sitzung_id) == 0


def test_eine_ruhende_runde_bekommt_kein_gesprochenes_wort_mehr(config, sitzung_id, monkeypatch):
    mitschnitt(config, sitzung_id)
    monkeypatch.setattr(merge.lebenszyklus, "ruht", lambda _runde: True)

    assert merge.uebernehmen(runde(config), sitzung_id) == 0
    assert notiztexte(config, sitzung_id) == []


# --- Das Diktat findet seine Szene (#160) -------------------------------------------

GESTERN = "2026-08-06T20:00:00+00:00"
HEUTE = "2026-08-07T20:00:00+00:00"

GESAGT = "Der Wirt hat gelogen."


def zwei_abende(config, sitzung_id):
    """Zwei Trennlinien mit echten Zeitpunkten — dazwischen liegt ein Tag."""
    angesagt(config, MIRA)
    notes.add_scene(runde(config), sitzung_id, title="Gestern", at=GESTERN)
    notes.add_scene(runde(config), sitzung_id, title="Heute", at=HEUTE)


def szene_mit(config, sitzung_id, text):
    """Der Titel der Szene, in der dieser Text steht — sonst ``None``."""
    for szene in notes.session(runde(config), sitzung_id).scenes:
        if any(text in notiz.text for notiz in szene.notes):
            return szene.title
    return None


def test_ein_diktat_landet_in_der_szene_seines_nachrichtenzeitpunkts(config, sitzung_id):
    zwei_abende(config, sitzung_id)
    diktat(
        config,
        sitzung_id,
        (0, 2000, GESAGT),
        discord_user_id=MIRA.id,
        message_at="2026-08-07T21:30:00+00:00",
    )

    assert merge.uebernehmen(runde(config), sitzung_id) == 1
    assert szene_mit(config, sitzung_id, GESAGT) == "Heute"
    assert notiztexte(config, sitzung_id) == [f"Mira: {GESAGT}"]


def test_ein_diktat_von_gestern_landet_nicht_in_der_aktuellen_szene(config, sitzung_id):
    """Die Gegenprobe: entschieden hat der Zeitpunkt der Nachricht, nicht der des Ablegens."""
    zwei_abende(config, sitzung_id)
    diktat(
        config,
        sitzung_id,
        (0, 2000, GESAGT),
        discord_user_id=MIRA.id,
        message_at="2026-08-06T22:00:00+00:00",
    )

    merge.uebernehmen(runde(config), sitzung_id)

    assert szene_mit(config, sitzung_id, GESAGT) == "Gestern"


def test_ein_diktat_ohne_nachrichtenzeitpunkt_bekommt_keinen_geraten(config, sitzung_id):
    """Ein Bestand von vor der Spalte: lieber nichts als eine Szene aus ``uploaded_at``."""
    zwei_abende(config, sitzung_id)
    diktat(config, sitzung_id, (0, 2000, GESAGT), discord_user_id=MIRA.id)

    assert merge.uebernehmen(runde(config), sitzung_id) == 0
    assert notiztexte(config, sitzung_id) == []


def test_zweimal_abschliessen_schreibt_das_diktat_nicht_doppelt(config, sitzung_id):
    """Dieselbe Herkunft wie das Transkript — der zweite Lauf ersetzt, statt zu verdoppeln."""
    zwei_abende(config, sitzung_id)
    diktat(
        config,
        sitzung_id,
        (0, 2000, GESAGT),
        discord_user_id=MIRA.id,
        message_at="2026-08-07T21:30:00+00:00",
    )

    assert merge.uebernehmen(runde(config), sitzung_id) == 1
    assert merge.uebernehmen(runde(config), sitzung_id) == 1
    assert notiztexte(config, sitzung_id) == [f"Mira: {GESAGT}"]


def test_das_diktat_laesst_die_verschraenkten_bot_spuren_unberuehrt(config, sitzung_id):
    """Es kommt hinzu — es wandert nicht auf die Sitzungsuhr und verschiebt dort nichts."""
    mitschnitt(config, sitzung_id)
    notes.add_scene(runde(config), sitzung_id, title="Unten", at="2026-08-06T20:00:30+00:00")
    diktat(
        config,
        sitzung_id,
        (0, 2000, GESAGT),
        discord_user_id=MIRA.id,
        message_at="2026-08-06T20:00:10+00:00",
    )

    assert merge.uebernehmen(runde(config), sitzung_id) == 3

    szenen = notes.session(runde(config), sitzung_id).scenes
    assert szenen[0].notes[0].text == "Mira: Wir gehen hinunter."
    assert szenen[1].notes[0].text == "Davey: Hier riecht es nach Rauch."
    assert [a.text for a in merge.conversation(runde(config), sitzung_id, mit_uhr=True)] == [
        "Wir gehen hinunter.",
        "Hier riecht es nach Rauch.",
    ]


def test_die_komposition_bekommt_das_diktat_wie_eine_notiz(config, sitzung_id):
    zwei_abende(config, sitzung_id)
    diktat(
        config,
        sitzung_id,
        (0, 2000, GESAGT),
        discord_user_id=MIRA.id,
        message_at="2026-08-07T21:30:00+00:00",
    )
    merge.uebernehmen(runde(config), sitzung_id)

    scope = db.scoped(runde(config))
    try:
        stoff = material(scope, sitzung_id)
    finally:
        scope.close()

    assert Notiz(f"Mira: {GESAGT}", verschriftet=True) in [
        note for szene in stoff.scenes for note in szene.notes
    ]


def test_eine_ruhende_runde_bekommt_auch_kein_diktat_mehr(config, sitzung_id, monkeypatch):
    zwei_abende(config, sitzung_id)
    diktat(
        config,
        sitzung_id,
        (0, 2000, GESAGT),
        discord_user_id=MIRA.id,
        message_at="2026-08-07T21:30:00+00:00",
    )
    monkeypatch.setattr(merge.lebenszyklus, "ruht", lambda _runde: True)

    assert merge.uebernehmen(runde(config), sitzung_id) == 0
    assert notiztexte(config, sitzung_id) == []


# --- Marken und Abschnitte ----------------------------------------------------------


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [("", None), ("17", 17000), ("02:03", 123000), ("1:02:03", 3723000)],
)
def test_eine_zeitmarke_wird_gelesen(eingabe, erwartet):
    assert merge.marke_ms(eingabe) == erwartet


@pytest.mark.parametrize("eingabe", ["gleich", "1:2:3:4", "12,5"])
def test_eine_unlesbare_marke_wird_gesagt_und_nicht_geraten(eingabe):
    with pytest.raises(ValueError, match="keine Zeitmarke"):
        merge.marke_ms(eingabe)


def test_ein_abschnitt_wird_nach_marken_geschnitten(config, sitzung_id):
    zwei_stimmen(config, sitzung_id)
    unterhaltung = merge.conversation(runde(config), sitzung_id)

    abschnitt = merge.span(unterhaltung, von=5000, bis=10000)

    assert [a.speaker for a in abschnitt] == ["Davey", "Mira"]
    assert merge.span(unterhaltung) == unterhaltung


def test_die_notiz_traegt_den_sprecher_aber_keine_zeitmarke(config, sitzung_id):
    zwei_stimmen(config, sitzung_id)

    text = merge.note_text(merge.conversation(runde(config), sitzung_id))

    assert text.splitlines()[0] == "Mira: Wir brechen bei Sonnenaufgang auf."
    # Keine Ziffer aus der Uhr: die Zahlenschranke der Komposition liest die Notizen als
    # belegt, und eine Uhrzeit steht in keinem Foundry-Chat-Log.
    assert "0:00:00" not in text


# --- Der Weg in die Komposition -----------------------------------------------------


def test_der_abschnitt_wird_zur_notiz_in_der_gewaehlten_szene(config, sitzung_id):
    zwei_stimmen(config, sitzung_id)
    szene = notes.session(runde(config), sitzung_id).scenes[0]

    abschnitt = merge.span(
        merge.conversation(runde(config), sitzung_id),
        von=merge.marke_ms("0:00:05"),
        bis=merge.marke_ms("0:00:10"),
    )
    notes.add_note(runde(config), szene.id, merge.note_text(abschnitt))

    notiz = notes.session(runde(config), sitzung_id).scenes[0].notes[0]
    assert notiz.text == "Davey: Warte, ich sehe nach.\nMira: Da liegt etwas vor der Tür."


def test_ein_leerer_abschnitt_wird_zu_keiner_leeren_notiz(config, sitzung_id):
    zwei_stimmen(config, sitzung_id)
    szene = notes.session(runde(config), sitzung_id).scenes[0]

    abschnitt = merge.span(
        merge.conversation(runde(config), sitzung_id), von=merge.marke_ms("1:00:00")
    )

    assert abschnitt == ()
    assert notes.add_note(runde(config), szene.id, merge.note_text(abschnitt)) is None
    assert notes.session(runde(config), sitzung_id).note_count == 0


def test_ein_abschnitt_geht_nicht_in_eine_fremde_szene(config, sitzung_id):
    """Die Szene sagt selbst, zu welcher Sitzung sie gehört — daran hängt die Prüfung."""
    fremde = notes.session(
        runde(config),
        notes.create_session(runde(config), played_on="2026-08-13"),
    ).scenes[0]

    assert notes.session_of_scene(runde(config), fremde.id) != sitzung_id
    assert notes.session_of_scene(runde(config), 0) is None


def test_die_komposition_nimmt_das_transkript_wie_eine_notiz(config, sitzung_id):
    """Der Schluss der Architektur: dieselbe Materialform, derselbe Composer."""
    zwei_stimmen(config, sitzung_id)
    szene = notes.session(runde(config), sitzung_id).scenes[0]
    notes.add_note(
        runde(config),
        szene.id,
        merge.note_text(merge.conversation(runde(config), sitzung_id)),
        origin=merge.TRANSKRIPT,
    )

    scope = db.scoped(runde(config))
    try:
        stoff = material(scope, sitzung_id)
    finally:
        scope.close()

    assert stoff.scenes[0].notes == (
        Notiz(
            "Mira: Wir brechen bei Sonnenaufgang auf.\nDavey: Warte, ich sehe nach.\n"
            "Mira: Da liegt etwas vor der Tür.\nDavey: Es ist verriegelt.",
            verschriftet=True,
        ),
    )
    assert stoff.scenes[0].facts == ()

    chronik = compose(stoff).text
    assert TRANSKRIPT_TITEL in chronik
    assert "Mira: Wir brechen bei Sonnenaufgang auf." in chronik
