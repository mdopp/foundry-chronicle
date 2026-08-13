"""Ein Altbestand an Notizen wird zu Sitzungen, Szenen und Notizen.

Die Sätze, die dieser Suite ihren Sinn geben: **die Ebenen sind relativ** — dasselbe
Dokument eine Überschriftenebene höher ergibt dieselben Abende —, **kein Datum, keine
Sitzung**, **der Text wird übernommen, wie er dasteht**, und **zweimal eingelesen
verdoppelt nichts**.

Das Beispiel ist das aus #169 und bleibt erfunden: es steht in keiner echten Kampagne.
"""

from __future__ import annotations

import pytest
from conftest import runde as erste_runde

from chronicle import db, dokument, notes, search
from chronicle.compose import service as compose_service
from chronicle.config import Config
from chronicle.transcribe import merge

BEISPIEL = """\
# Die Chronik der Sturmklingen

Unsere Notizen seit dem Frühjahr — zusammengetragen aus Zetteln und Erinnerung.

## 12.03.2026 — Ankunft in Hohenfels

### Der Regen und der Wirt

Wir kamen bei strömendem Regen an. Der Wirt im »Gebeugten Ast« wollte uns
erst nicht einlassen, weil Brok den halben Türrahmen ausfüllt.

### Das Gespräch am Kamin

Aelin bekam heraus, dass seit vier Wochen niemand mehr aus dem Steinbruch
zurückkommt. Der Wirt wurde einsilbig, als der Name Hallendorn fiel.

## 26.03.2026 — Der Keller

### Die Luke hinter dem Fass

Brok fand sie, weil er sich beim Fassrollen den Fuß stieß.

### Was unten wartete

Drei Zeichen an der Wand, keines davon in einer Sprache, die jemand von uns
liest. Mira hat sie abgemalt.

## 09.04.2026

An dem Abend haben wir kaum mitgeschrieben. Nur so viel: das Tor ließ sich
nicht öffnen, und Mira hat den Schlüssel behalten.
"""

# Dasselbe Dokument, nur ohne Vorspann und eine Ebene höher: ``#`` je Abend, ``##`` je
# Szene. Es muss dieselben Sitzungen ergeben — das ist der Kern von »relativ, nicht fest«.
OHNE_VORSPANN = "\n".join(
    zeile[1:] if zeile.startswith("##") else zeile
    for zeile in BEISPIEL.splitlines()
    if not zeile.startswith("# ")
).strip()

REGEN = (
    "Wir kamen bei strömendem Regen an. Der Wirt im »Gebeugten Ast« wollte uns\n"
    "erst nicht einlassen, weil Brok den halben Türrahmen ausfüllt."
)

DRITTER_ABEND = (
    "An dem Abend haben wir kaum mitgeschrieben. Nur so viel: das Tor ließ sich\n"
    "nicht öffnen, und Mira hat den Schlüssel behalten."
)

VORSPANN = "Unsere Notizen seit dem Frühjahr"

NACHGETRAGEN = """
## 23.04.2026 — Vor dem Tor

Der Schlüssel passte doch, nur nicht in dieses Schloss.
"""

# Hinten angehängt und über der Abend-Ebene: der Fall, an dem »Datum statt Mehrfachheit«
# hängt. Die Unterüberschrift darunter muss draußen bleiben — sie gehört zu keinem Abend.
ANHANG = """
# Anhang

### Die drei Zeichen aus dem Keller

Mira hat sie abgemalt; hier stehen sie noch einmal sauber.
"""


@pytest.fixture
def config(tmp_path):
    config = Config(data_dir=tmp_path)
    db.init(config.database_path)
    return config


@pytest.fixture
def unsere(config):
    return erste_runde(config)


def eingelesen(runde, text=BEISPIEL):
    """Der ganze Weg der Datenschicht: lesen, das Neue aussuchen, anlegen."""
    aufteilung = dokument.lesen(text)
    return dokument.anlegen(runde, dokument.neu(runde, aufteilung.abende))


def gliederung(runde):
    """Sitzung für Sitzung: Datum, Titel und die Szenen mit ihren Notizen."""
    return [
        (
            sitzung.played_on,
            sitzung.title,
            [
                (szene.title, [notiz.text for notiz in szene.notes])
                for szene in notes.session(runde, sitzung.id).scenes
            ],
        )
        for sitzung in sorted(notes.sessions(runde), key=lambda s: s.played_on)
    ]


# -- Aufteilen --------------------------------------------------------------------------


def test_das_beispiel_ergibt_genau_drei_abende():
    abende = dokument.lesen(BEISPIEL).abende

    assert [abend.datum for abend in abende] == ["2026-03-12", "2026-03-26", "2026-04-09"]
    assert [abend.titel for abend in abende] == ["Ankunft in Hohenfels", "Der Keller", None]


def test_der_vorspann_wird_keine_sitzung(unsere):
    eingelesen(unsere)

    assert [datum for datum, _titel, _szenen in gliederung(unsere)] == [
        "2026-03-12",
        "2026-03-26",
        "2026-04-09",
    ]
    alles = " ".join(
        notiz
        for _d, _t, szenen in gliederung(unsere)
        for _s, notizen in szenen
        for notiz in notizen
    )
    assert VORSPANN not in alles
    assert "Die Chronik der Sturmklingen" not in alles


def test_eine_ebene_hoeher_ergibt_dieselben_abende():
    """Nicht »``##`` ist eine Sitzung«: maßgeblich ist die Ebene, auf der die Daten stehen."""
    hoch = dokument.lesen(OHNE_VORSPANN).abende
    tief = dokument.lesen(BEISPIEL).abende

    assert [(a.datum, a.titel) for a in hoch] == [(a.datum, a.titel) for a in tief]
    assert [[s.titel for s in a.szenen] for a in hoch] == [
        [s.titel for s in a.szenen] for a in tief
    ]
    # Und derselbe Abend ist auch derselbe: die Ebene geht in den Abdruck nicht ein.
    assert [a.abdruck for a in hoch] == [a.abdruck for a in tief]


def test_die_szenen_stehen_in_der_reihenfolge_des_dokuments(unsere):
    eingelesen(unsere)

    erster, zweiter, dritter = gliederung(unsere)

    assert [titel for titel, _notizen in erster[2]] == [
        "Der Regen und der Wirt",
        "Das Gespräch am Kamin",
    ]
    # Der zweite Abend ist der einzige, bei dem Sitzungs-Id und Id der ersten Szene
    # auseinanderlaufen — ein vertauschtes Argumentpaar in ``rename_scene`` fällt nur hier auf.
    assert [titel for titel, _notizen in zweiter[2]] == [
        "Die Luke hinter dem Fass",
        "Was unten wartete",
    ]
    # Der dritte Abend hat keine eigene Szene — sein Text steht in der ersten.
    assert dritter[2] == [(None, [DRITTER_ABEND])]


def test_der_text_wird_uebernommen_wie_er_dasteht(unsere):
    """Kein Modell, keine Zusammenfassung: Zeile für Zeile dasselbe wie im Dokument."""
    eingelesen(unsere)

    erster, *_rest = gliederung(unsere)

    assert erster[2][0][1] == [REGEN]


def test_ohne_datum_wird_gefragt_statt_geraten(unsere):
    text = BEISPIEL.replace("## 26.03.2026 — Der Keller", "## Der Keller")

    aufteilung = dokument.lesen(text)
    dokument.anlegen(unsere, aufteilung.abende)

    assert aufteilung.ohne_datum == ("Der Keller",)
    assert [datum for datum, _titel, _szenen in gliederung(unsere)] == ["2026-03-12", "2026-04-09"]


def test_ein_unmoegliches_datum_ist_keines():
    """Der 32. wird nicht auf den 31. zurechtgebogen — was nicht sicher ist, wird gefragt."""
    aufteilung = dokument.lesen(
        "## 12.03.2026 — Ankunft\n\nText.\n\n## 32.03.2026 — Der Keller\n\nEtwas geschah.\n"
    )

    assert [abend.datum for abend in aufteilung.abende] == ["2026-03-12"]
    assert aufteilung.ohne_datum == ("32.03.2026 — Der Keller",)


def test_iso_und_deutsche_schreibweise_werden_beide_gelesen():
    (deutsch,) = dokument.lesen("# 12.03.2026 — Ankunft\n\nText.\n").abende
    (iso,) = dokument.lesen("# 2026-03-12 — Ankunft\n\nText.\n").abende

    assert deutsch.datum == iso.datum == "2026-03-12"
    assert deutsch.titel == iso.titel == "Ankunft"


def test_ein_datum_ohne_jahr_wird_nicht_ergaenzt():
    """``12.03.`` ist nicht sicher zu lesen — das Jahr zu ergänzen wäre Erfinden."""
    aufteilung = dokument.lesen(
        "## 12.03.2026 — Ankunft\n\nText.\n\n## 26.03. — Der Keller\n\nEtwas geschah.\n"
    )

    assert [abend.datum for abend in aufteilung.abende] == ["2026-03-12"]
    assert aufteilung.ohne_datum == ("26.03. — Der Keller",)


def test_ein_abend_ganz_ohne_text_wird_uebersprungen_statt_angelegt(unsere):
    """#172: ohne Notiz gäbe es keine Herkunft — der Abend entstünde bei jedem Mal neu.

    Angelegt bliebe eine Sitzung, die nichts trägt, über die Suche nicht zu finden ist und
    einzeln auch nicht wieder loszuwerden. Sie wird deshalb benannt, nicht geschrieben.
    """
    text = "## 01.01.2026 — Leer\n\n## 02.01.2026 — Auch leer\n"

    aufteilung = dokument.lesen(text)

    assert aufteilung.abende == ()
    assert aufteilung.ohne_text == ("01.01.2026 — Leer", "02.01.2026 — Auch leer")
    assert eingelesen(unsere, text) == 0
    assert eingelesen(unsere, text) == 0
    assert notes.sessions(unsere) == ()


def test_ein_satz_unter_einer_szene_reicht_dem_abend(unsere):
    """Die Gegenprobe: übersprungen wird nur, wo **nirgends** unter dem Abend ein Satz steht."""
    text = "## 01.01.2026 — Leer\n\n## 02.01.2026 — Voll\n\n### Die Luke\n\nSie klemmte.\n"

    assert eingelesen(unsere, text) == 1
    assert dokument.lesen(text).ohne_text == ("01.01.2026 — Leer",)
    assert gliederung(unsere) == [("2026-01-02", "Voll", [("Die Luke", ["Sie klemmte."])])]
    # Und was steht, steht danach als bekannt da — der zweite Anlauf legt es nicht noch einmal.
    assert eingelesen(unsere, text) == 0


def test_ein_dokument_ganz_ohne_datum_ergibt_nichts():
    """Ohne ein einziges Datum ist nicht einmal zu sagen, welche Ebene die Abende trennt."""
    assert dokument.lesen("# Die Sturmklingen\n\n## Der Keller\n\nEtwas geschah.\n") == (
        dokument.Aufteilung()
    )


def test_ein_nachgestellter_anhang_wird_kein_abend_und_keine_szene(unsere):
    """Die Begründung für »Datum statt Mehrfachheit« — und dafür, dass oben zugemacht wird.

    Mit ``# Anhang`` kommt die oberste Ebene zweimal vor. Entschiede die Mehrfachheit, wäre
    sie damit die Abend-Ebene, und der Titel der Sammlung stünde plötzlich als Abend ohne
    Datum da. Und was **unter** dem Anhang steht, gehört zu keinem Abend — auch nicht zum
    letzten, der vor ihm zu Ende ging.
    """
    aufteilung = dokument.lesen(BEISPIEL + ANHANG)
    eingelesen(unsere, BEISPIEL + ANHANG)

    assert [abend.datum for abend in aufteilung.abende] == [
        "2026-03-12",
        "2026-03-26",
        "2026-04-09",
    ]
    assert aufteilung.ohne_datum == ()
    assert gliederung(unsere)[-1][2] == [(None, [DRITTER_ABEND])]


def test_ein_einzelner_abend_bleibt_ein_abend(unsere):
    """Die Gegenprobe zur Mehrfachheit: hier kommt nur die Szenen-Ebene doppelt vor."""
    text = "# Die Sturmklingen\n\n## 12.03.2026 — Der Keller\n\n### Eins\n\nA\n\n### Zwei\n\nB\n"

    eingelesen(unsere, text)

    assert gliederung(unsere) == [("2026-03-12", "Der Keller", [("Eins", ["A"]), ("Zwei", ["B"])])]


# -- Anlegen ----------------------------------------------------------------------------


def test_text_unter_der_abendueberschrift_steht_in_der_ersten_szene(unsere):
    text = "## 12.03.2026 — Der Keller\n\nWir gingen hinunter.\n\n### Die Luke\n\nSie klemmte.\n"

    eingelesen(unsere, text)

    assert gliederung(unsere) == [
        (
            "2026-03-12",
            "Der Keller",
            [(None, ["Wir gingen hinunter."]), ("Die Luke", ["Sie klemmte."])],
        )
    ]


def test_dasselbe_dokument_zweimal_verdoppelt_den_bestand_nicht(unsere):
    assert eingelesen(unsere) == 3

    assert eingelesen(unsere) == 0
    assert len(notes.sessions(unsere)) == 3


def test_ein_gewachsenes_dokument_bringt_nur_den_neuen_abend(unsere):
    """Der wahrscheinliche zweite Anlauf: dieselbe Datei, ein Abend länger."""
    eingelesen(unsere)

    assert eingelesen(unsere, BEISPIEL + NACHGETRAGEN) == 1
    assert [datum for datum, _titel, _szenen in gliederung(unsere)] == [
        "2026-03-12",
        "2026-03-26",
        "2026-04-09",
        "2026-04-23",
    ]


# -- Was danach damit geschieht ---------------------------------------------------------


def test_die_herkunft_ist_nicht_die_des_transkripts(unsere):
    """Sonst räumte ``merge`` das Eingelesene beim nächsten Lauf fort — unwiederbringlich."""
    eingelesen(unsere)
    sitzung = notes.sessions(unsere)[-1].id

    assert notes.drop_derived(unsere, sitzung, merge.TRANSKRIPT) == 0
    assert notes.session(unsere, sitzung).note_count == 2


def test_das_eingelesene_geht_in_die_chronik_ein(unsere):
    eingelesen(unsere)
    sitzung = [s for s in notes.sessions(unsere) if s.played_on == "2026-03-12"][0]

    scope = db.scoped(unsere)
    try:
        stoff = compose_service.material(scope, sitzung.id)
    finally:
        scope.close()

    assert [szene.title for szene in stoff.scenes] == [
        "Der Regen und der Wirt",
        "Das Gespräch am Kamin",
    ]
    assert stoff.scenes[0].notes == (REGEN,)


def test_das_eingelesene_ist_ueber_die_suche_zu_finden(unsere):
    eingelesen(unsere)

    treffer = search.find(unsere, "Steinbruch")

    gefunden = [hit.raw for gruppe in treffer.groups for hit in gruppe.hits]
    assert gefunden and any("Steinbruch" in stelle for stelle in gefunden)
