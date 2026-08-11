"""Die Nacherzählung — die Stufe mit dem höchsten Erfindungsrisiko des ganzen Systems.

Gelesen wird sie von Leuten, die die Lücken selbst nicht mehr kennen. Fünf Sätze geben
dieser Suite ihren Sinn, und jeder hat hier seinen Test:

* **Belegtes und Verbindendes stehen sichtbar getrennt** — in eigenen Überschriften, die
  im Klartext sagen, was sie tragen.
* **Keine Zahl kommt ohne Beleg durch.** Nennt das Modell eine, die in der Chronik dieser
  Sitzung und ihren Registereinträgen nicht steht, wird der Absatz verworfen und das
  Verwerfen dazugeschrieben — ausgeschrieben und römisch ebenso wie in Ziffern.
* **Die Überschriften setzt diese Stufe, nicht das Modell.** Ein Absatz, der eine eigene
  aufmacht, wird verworfen: sonst schriebe sich das Modell sein eigenes »Belegt aus dem
  Register«.
* **Eine Lücke wird benannt, nicht gefüllt.** Eine Sitzung ohne bestätigten
  Registereintrag kommt gar nicht erst in einen Modellaufruf.
* **Rollierend statt alles auf einmal.** Je Sitzung ein Aufruf, mitgeführt wird nur der
  zuletzt angenommene Absatz.

Das Modell ist eine Attrappe — aber keine, die immer dasselbe sagt: sie baut ihre Antwort
aus dem, was in den Aufruf ging. Nur so lässt sich prüfen, dass wirklich das Register die
Auswahl trifft und nicht die halbe Kampagne mitreist.
"""

from __future__ import annotations

import re

import pytest
from conftest import runde as erste_runde
from test_discord_ausgabe import FakeDiscord

from chronicle import db, lebenszyklus, register, settings
from chronicle.compose.client import ModelUnreachable
from chronicle.compose.composer import NICHT_ERREICHBAR
from chronicle.compose.nacherzaehlung import (
    ERZAEHLT_TITEL,
    LUECKE,
    LUECKE_TITEL,
    OHNE_MODELL,
    REGISTER_TITEL,
    REGISTER_ZEILE,
    STAND_ZEILE,
    VERWORFEN,
    VERWORFEN_UEBERSCHRIFT,
    Abschnitt,
    ErzaehlStoff,
    nacherzaehlen,
)
from chronicle.compose.service import KIND, erzaehlen
from chronicle.config import Config
from chronicle.discord import ausgabe
from chronicle.discord.client import DiscordClient

TOKEN = "bot-token-nur-fuer-den-test"

WURF = "Brok Eisenfaust — Knowledge Roll: Summe 7"

MIRA = REGISTER_ZEILE.format(label="Figur", name="Mira", satz="Kundschafterin der Runde")
KELLER = REGISTER_ZEILE.format(label="Ort", name="Der Keller", satz="unter dem Krummen Ast")
FADEN = REGISTER_ZEILE.format(label="Handlungsfaden", name="Die Tür", satz="steht noch offen")


def chronik(*zeilen: str) -> str:
    return "\n".join(["# Chronik", "", "### Belegt aus Foundry", *(f"- {z}" for z in zeilen), ""])


def namen(prompt: str) -> list[str]:
    """Die Registernamen, die in einem Aufruf standen — sie stehen in Guillemets."""
    return re.findall(r"»([^«»]+)«", prompt)


class Modell:
    """Ein Modell, das prüfbar antwortet: es sagt zurück, was es gesehen hat.

    Eine feste Antwort träfe hier nichts. Geprüft werden muss, **was** in den Aufruf ging:
    die Einträge dieser einen Sitzung und der zuletzt angenommene Absatz — nicht mehr.
    Deshalb baut diese Attrappe ihre Antwort aus dem Prompt.
    """

    def __init__(self, *, name="chronist-test", antwort=None):
        self.name = name
        self._antwort = antwort
        self.aufrufe: list[str] = []

    def write(self, *, system, prompt):
        self.aufrufe.append(prompt)
        if self._antwort is None:
            return "Weiter ging es um " + ", ".join(namen(prompt)) + "."
        antwort = self._antwort(prompt) if callable(self._antwort) else self._antwort
        if isinstance(antwort, Exception):
            raise antwort
        return antwort


def abschnitt(nummer: int, *eintraege: str, text: str = "", titel: str | None = None) -> Abschnitt:
    return Abschnitt(
        session_id=nummer,
        played_on=f"2026-05-0{nummer}",
        title=titel,
        chronicle=text or chronik(WURF),
        entries=eintraege,
    )


def block(text: str, played_on: str) -> str:
    """Der Abschnitt einer Sitzung, aus dem fertigen Text herausgeschnitten."""
    teile = text.split("## Sitzung vom ")
    (gesucht,) = [teil for teil in teile if teil.startswith(played_on)]
    return gesucht


# --- Die Trennung zwischen Belegtem und Verbindungstext ---------------------------------


def test_die_trennung_steht_sichtbar_in_den_ueberschriften():
    """Wer das liest, sieht ohne Nachschlagen, welcher Satz getragen ist."""
    modell = Modell()

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA, KELLER),)), modell)

    eine = block(ergebnis.text, "2026-05-01")
    assert ERZAEHLT_TITEL in eine
    assert REGISTER_TITEL in eine
    # Das Erzählte steht über dem Belegten und trägt seine Kennzeichnung im Klartext.
    assert eine.index(ERZAEHLT_TITEL) < eine.index(REGISTER_TITEL)
    assert "nicht belegt" in ERZAEHLT_TITEL
    assert f"- {MIRA}" in eine.split(REGISTER_TITEL)[1]
    assert "Weiter ging es um Mira, Der Keller." in eine.split(ERZAEHLT_TITEL)[1]
    assert ergebnis.prose_count == 1


def test_der_kopf_sagt_wer_erzaehlt_hat_und_was_belegt_ist():
    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), Modell())

    kopf = ergebnis.text.split("## ")[0]
    assert "`chronist-test`" in kopf
    assert "Belegt ist nur" in kopf
    assert "Lücke" in kopf


# --- Keine Zahl ohne Beleg --------------------------------------------------------------


def test_eine_zahl_ohne_beleg_verwirft_den_absatz_und_sagt_es():
    modell = Modell(antwort="Sie fanden 47 Silberstücke im Keller.")

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), modell)

    assert "47" not in ergebnis.text
    assert VERWORFEN in ergebnis.text
    # Verworfen wird der Absatz, nicht die Sitzung: das Belegte steht weiter da.
    assert f"- {MIRA}" in ergebnis.text
    assert ergebnis.prose_count == 0


def test_eine_zahl_aus_der_chronik_darf_stehenbleiben():
    """Die Schranke sperrt das Erfundene, nicht das Belegte."""
    modell = Modell(antwort="Broks Wurf kam auf 7.")

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), modell)

    assert "Broks Wurf kam auf 7." in ergebnis.text
    assert VERWORFEN not in ergebnis.text


def test_ein_verworfener_absatz_wird_nicht_mitgetragen():
    """Sonst reiste die erfundene Zahl als »Stand bisher« durch den ganzen Bereich."""
    modell = Modell(
        antwort=lambda prompt: (
            "Sie fanden 47 Silberstücke." if "2026-05-01" in prompt else "Danach zogen sie weiter."
        )
    )

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA), abschnitt(2, KELLER))), modell)

    assert "47" not in ergebnis.text
    assert STAND_ZEILE not in modell.aufrufe[1]


@pytest.mark.parametrize(
    "erfunden",
    [
        "Sie fanden siebzehn Silberstücke im Keller.",
        "Dreimal traten sie an, gegen vier Wachen.",
        "Sie gingen durch Saal XVII in Kammer IV.",
        "Der Marsch dauerte 3.5 Stunden.",
        "Im Fass standen noch 5,7 Liter.",
        "Ein Dutzend Krähen saß auf dem Dach.",
    ],
)
def test_eine_zahl_kommt_auch_ausgeschrieben_nicht_durch(erfunden):
    """Die Schranke prüft den Wert, nicht die Schreibweise.

    Belegt sind hier die 7 aus Broks Wurf und sonst nichts. Ausgeschrieben, römisch oder
    aus belegten Ziffern zu einer neuen Zahl zusammengesetzt — es bleibt eine Zahl, die
    nirgends steht, und genau die soll nicht im Protokoll landen.
    """
    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), Modell(antwort=erfunden))

    assert erfunden not in ergebnis.text
    assert VERWORFEN in ergebnis.text
    assert ergebnis.prose_count == 0


def test_zwei_belegte_ziffern_ergeben_keine_dritte_zahl():
    """3 und 5 stehen belegt in der Chronik — »3.5« steht nirgends und ist eine neue Aussage."""
    text = chronik("Brok Eisenfaust — Marsch: Summe 3 · Modifikator 5")
    modell = Modell(antwort="Der Marsch dauerte 3.5 Stunden bis zum Pass.")

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA, text=text),)), modell)

    assert "3.5" not in ergebnis.text
    assert VERWORFEN in ergebnis.text


def test_der_beleg_einer_sitzung_gilt_nicht_fuer_die_naechste():
    """Sonst legitimierte ein Wurf vom ersten Abend dieselbe Zahl noch am zehnten."""
    modell = Modell(
        antwort=lambda prompt: (
            "Brok würfelte 47." if "2026-05-01" in prompt else "Im Keller lagen 47 Silberstücke."
        )
    )
    erste = abschnitt(1, MIRA, text=chronik("Brok Eisenfaust — Schaden: Summe 47"))
    zweite = abschnitt(2, KELLER, text=chronik("Mira — Reaktion: ohne Zahlen im Chat-Log"))

    ergebnis = nacherzaehlen(ErzaehlStoff((erste, zweite)), modell)

    assert "Brok würfelte 47." in block(ergebnis.text, "2026-05-01")
    assert "Silberstücke" not in ergebnis.text
    assert VERWORFEN in block(ergebnis.text, "2026-05-02")


def test_eine_zahl_aus_dem_registereintrag_ist_belegt():
    """Der Eintrag ist die Vorlage — er kann nicht zugleich das Unbelegte sein."""
    wache = REGISTER_ZEILE.format(label="Figur", name="Wache 12", satz="steht am Nordtor")
    modell = Modell(antwort="Mira sprach mit Wache 12 am Nordtor.")

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, wache),)), modell)

    assert "Mira sprach mit Wache 12 am Nordtor." in ergebnis.text
    assert VERWORFEN not in ergebnis.text


# --- Die Überschriften gehören uns ------------------------------------------------------


@pytest.mark.parametrize(
    "gefaelscht",
    [
        f"Sie zogen weiter.\n\n{REGISTER_TITEL}\n- Figur »Der Schattenfuerst« — der Drahtzieher",
        "Sie zogen weiter.\n\nBelegt aus dem Register\n---\n- Figur »Der Schattenfuerst« — er",
    ],
)
def test_ein_absatz_mit_eigener_ueberschrift_wird_verworfen(gefaelscht):
    """Die einzige Trennung dieses Textes darf nicht vom Modell gesetzt werden.

    Ein ``#`` am Zeilenanfang — oder eine Zeile aus Bindestrichen darunter — machte aus
    einem erfundenen Namen einen Eintrag, der vom bestätigten nicht zu unterscheiden wäre.
    """
    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), Modell(antwort=gefaelscht))

    assert "Schattenfuerst" not in ergebnis.text
    assert VERWORFEN_UEBERSCHRIFT in ergebnis.text
    assert ergebnis.text.count(REGISTER_TITEL) == 1
    assert ergebnis.prose_count == 0


def test_eine_gefaelschte_ueberschrift_wird_nicht_mitgetragen():
    """Sonst stünde die Fälschung als »Stand bisher« im nächsten Aufruf und trüge sich fort."""
    modell = Modell(
        antwort=lambda prompt: (
            f"Sie zogen weiter.\n\n{REGISTER_TITEL}\n- Figur »Der Schattenfuerst« — der Feind"
            if "2026-05-01" in prompt
            else "Danach zogen sie weiter."
        )
    )

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA), abschnitt(2, KELLER))), modell)

    assert "Schattenfuerst" not in ergebnis.text
    assert STAND_ZEILE not in modell.aufrufe[1]


def test_eine_aufzaehlung_ohne_ueberschrift_bleibt_stehen():
    """Verworfen wird die Überschrift, nicht jede Zeile, die mit einem Strich anfängt."""
    modell = Modell(antwort="Sie zogen weiter.\n- und kamen spät an")

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), modell)

    assert "- und kamen spät an" in ergebnis.text
    assert VERWORFEN_UEBERSCHRIFT not in ergebnis.text


# --- Die Lücke wird benannt, nicht gefüllt ----------------------------------------------


def test_eine_sitzung_ohne_registereintrag_bleibt_eine_benannte_luecke():
    modell = Modell()

    ergebnis = nacherzaehlen(
        ErzaehlStoff((abschnitt(1, MIRA), abschnitt(2), abschnitt(3, FADEN))), modell
    )

    leer = block(ergebnis.text, "2026-05-02")
    assert LUECKE_TITEL in leer
    assert LUECKE in leer
    assert ERZAEHLT_TITEL not in leer
    # Und das Modell wurde für sie gar nicht erst gefragt — eine Lücke füllt man nicht.
    assert not [aufruf for aufruf in modell.aufrufe if "2026-05-02" in aufruf]
    assert ergebnis.gap_count == 1
    assert ergebnis.session_count == 3


def test_die_meldung_zaehlt_die_luecken_mit():
    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA), abschnitt(2))), Modell())

    assert "1 Lücke benannt, nicht gefüllt." in ergebnis.message


# --- Rollierend mit mitgeführtem Zustand ------------------------------------------------


def test_je_sitzung_ein_aufruf_mit_dem_letzten_absatz_als_stand():
    modell = Modell()

    ergebnis = nacherzaehlen(
        ErzaehlStoff((abschnitt(1, MIRA), abschnitt(2, KELLER), abschnitt(3, FADEN))), modell
    )

    assert len(modell.aufrufe) == 3
    # Der erste Aufruf hat noch keinen Stand, jeder weitere genau den davor angenommenen.
    assert STAND_ZEILE not in modell.aufrufe[0]
    assert "Weiter ging es um Mira." in modell.aufrufe[1]
    assert "Weiter ging es um Der Keller." in modell.aufrufe[2]
    assert ergebnis.prose_count == 3


def test_in_einen_aufruf_geht_nur_das_register_dieser_sitzung():
    """Das Register wählt aus — sonst reiste die halbe Kampagne durch jedes Kontextfenster."""
    modell = Modell()

    nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA), abschnitt(2, KELLER))), modell)

    assert namen(modell.aufrufe[0]) == ["Mira"]
    # »Mira« steht im zweiten Aufruf nur noch als mitgeführter Absatz, nicht als Eintrag.
    assert modell.aufrufe[1].split(STAND_ZEILE)[1].count(KELLER) == 1
    assert MIRA not in modell.aufrufe[1]


# --- Wenn das Modell fehlt oder ausfällt ------------------------------------------------


def test_ohne_modell_bleibt_das_belegte_stehen():
    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), None)

    assert ergebnis.reason == OHNE_MODELL
    assert ERZAEHLT_TITEL not in ergebnis.text
    assert f"- {MIRA}" in ergebnis.text
    assert ergebnis.prose_count == 0


def test_ein_ausfallendes_modell_haelt_das_schon_erzaehlte():
    modell = Modell(
        antwort=lambda prompt: (
            "Sie brachen auf." if "2026-05-01" in prompt else ModelUnreachable("Ollama war aus")
        )
    )

    ergebnis = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA), abschnitt(2, KELLER))), modell)

    assert ergebnis.reason == NICHT_ERREICHBAR
    assert "Sie brachen auf." in ergebnis.text
    assert f"- {KELLER}" in ergebnis.text
    assert ergebnis.prose_count == 1


# --- Der Bereich und das Register in der Datenbank --------------------------------------


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(discord_bot_token=TOKEN, data_dir=tmp_path / "daten")
    db.init(gesetzt.database_path)
    return gesetzt


def sitzung(config, played_on: str, *, titel="", text=""):
    scope = db.scoped(erste_runde(config))
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
                (scope.runde_id, played_on, titel, f"{played_on}T20:00:00+00:00"),
            )
            sitzung_id = int(zeiger.lastrowid)
            scope.execute(
                "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    sitzung_id,
                    KIND,
                    text or chronik(WURF),
                    f"{played_on}T21:00:00+00:00",
                ),
            )
    finally:
        scope.close()
    return sitzung_id


def eintragen(
    config, sitzung_id, *, name, kind=register.FIGUR, satz="Kundschafterin", state, verweise=1
):
    scope = db.scoped(erste_runde(config))
    try:
        with scope:
            zeiger = scope.execute(
                "INSERT INTO register_entry "
                "(runde_id, kind, name, description, state, suggested_at) "
                "VALUES (?, ?, ?, ?, ?, '2026-05-01T21:00:00')",
                (scope.runde_id, kind, name, satz, state),
            )
            # Ein Verweis je Szene, in der der Name fiel — der Normalfall sind mehrere.
            scope.executemany(
                "INSERT INTO register_mention (runde_id, entry_id, session_id) VALUES (?, ?, ?)",
                [(scope.runde_id, int(zeiger.lastrowid), sitzung_id)] * verweise,
            )
    finally:
        scope.close()


def test_nur_bestaetigte_eintraege_kommen_in_die_nacherzaehlung(config):
    """Was das Register nicht bestätigt kennt, kommt nicht vor — auch nicht als Andeutung."""
    unsere = erste_runde(config)
    erste = sitzung(config, "2026-05-01")
    eintragen(config, erste, name="Mira", state=register.BESTAETIGT)
    eintragen(config, erste, name="Der Schattenfuerst", state=register.VORSCHLAG)
    modell = Modell()

    ergebnis = erzaehlen(config, unsere, erste, erste, register.nach_sitzung(unsere), model=modell)

    assert namen(modell.aufrufe[0]) == ["Mira"]
    assert "Schattenfuerst" not in ergebnis.text


def test_ein_eintrag_steht_je_sitzung_genau_einmal(config):
    """Die Verweise sind szenenweise — dreimal dieselbe Zeile wäre eine dreifache Gewichtung."""
    unsere = erste_runde(config)
    erste = sitzung(config, "2026-05-01")
    eintragen(config, erste, name="Mira", state=register.BESTAETIGT, verweise=3)

    erwartet = REGISTER_ZEILE.format(label="Figur", name="Mira", satz="Kundschafterin")
    assert register.nach_sitzung(unsere)[erste] == (erwartet,)


def test_der_bereich_nimmt_genau_die_sitzungen_dazwischen(config):
    unsere = erste_runde(config)
    ids = [
        sitzung(config, datum, titel=datum) for datum in ("2026-05-01", "2026-05-02", "2026-05-03")
    ]
    for sitzung_id in ids:
        eintragen(config, sitzung_id, name=f"Mira {sitzung_id}", state=register.BESTAETIGT)

    ergebnis = erzaehlen(
        config, unsere, ids[0], ids[1], register.nach_sitzung(unsere), model=Modell()
    )

    assert ergebnis.session_count == 2
    assert (ergebnis.von, ergebnis.bis) == ("2026-05-01", "2026-05-02")
    assert "2026-05-03" not in ergebnis.text


def test_vertauschte_eckpunkte_werden_geordnet_statt_abgewiesen(config):
    unsere = erste_runde(config)
    erste = sitzung(config, "2026-05-01")
    zweite = sitzung(config, "2026-05-02")
    eintragen(config, erste, name="Mira", state=register.BESTAETIGT)

    ergebnis = erzaehlen(
        config, unsere, zweite, erste, register.nach_sitzung(unsere), model=Modell()
    )

    assert (ergebnis.von, ergebnis.bis) == ("2026-05-01", "2026-05-02")


def test_eine_ruhende_runde_erzaehlt_nichts_nach(config, monkeypatch):
    """Dieselbe Sperre wie vor Chronik und Rückblick — eine ruhende Runde bekommt nichts."""
    unsere = erste_runde(config)
    erste = sitzung(config, "2026-05-01")
    eintragen(config, erste, name="Mira", state=register.BESTAETIGT)
    eintraege = register.nach_sitzung(unsere)
    monkeypatch.setattr(lebenszyklus, "ruht", lambda _runde: True)

    assert erzaehlen(config, unsere, erste, erste, eintraege) is None


def test_ein_bereich_ohne_sitzung_gibt_nichts_zurueck(config):
    unsere = erste_runde(config)

    assert erzaehlen(config, unsere, 4711, 4711, {}) is None


# --- Der Weg nach Discord ---------------------------------------------------------------


def test_die_nacherzaehlung_geht_als_markdown_datei_in_den_kanal(config):
    unsere = erste_runde(config)
    api = FakeDiscord()
    wirksam = settings.effective(config, unsere)
    text = nacherzaehlen(ErzaehlStoff((abschnitt(1, MIRA),)), Modell()).text

    meldung = ausgabe.erzaehlung_zustellen(
        config,
        unsere,
        "kanal-1",
        text,
        "2026-05-01",
        "2026-05-03",
        client=DiscordClient(wirksam, http=lambda: api),
    )

    (kanal, name, inhalt, art, begleitung) = api.angehaengt[0]
    assert meldung == ""
    assert (kanal, name, art) == (
        "kanal-1",
        "nacherzaehlung-2026-05-01-bis-2026-05-03.md",
        "text/markdown",
    )
    # Die Trennung übersteht den Weg: Markdown trägt die Überschriften unverändert.
    assert ERZAEHLT_TITEL in inhalt
    assert REGISTER_TITEL in inhalt
    assert "Belegt aus dem Register" in begleitung["payload_json"]


def test_eine_zu_grosse_nacherzaehlung_wird_gesagt_statt_abgeschnitten(config, monkeypatch):
    unsere = erste_runde(config)
    monkeypatch.setattr(ausgabe, "MAX_BYTES", 10)

    meldung = ausgabe.erzaehlung_zustellen(
        config, unsere, "kanal-1", "viel zu lang für zehn Bytes", "2026-05-01", "2026-05-03"
    )

    assert meldung.startswith("Die Nacherzählung ist größer als")
