"""Discords Grenzen: geteilt wird, was nicht in eine Nachricht passt — verloren geht nichts.

Die eine Zusicherung, an der alles hängt: aneinandergehängt ergeben die Stücke wieder den
Text. Ein Text, der mit jedem neuen Befehl wächst, darf an Discords 2000 Zeichen nicht
hängenbleiben, und was vorne steht, muss vorne bleiben — die Vorstellung im Sprachkanal
trägt ihren Ausweg aus der Aufnahme im ersten Absatz.
"""

from __future__ import annotations

import pytest

from chronicle.bot import einrichten, erinnern
from chronicle.discord import ausgabe, grenzen


def test_discords_masse_stehen_auch_dort_richtig_wo_sie_gebraucht_werden():
    """Die Zahlen, die von außen kommen — gemeinsam geprüft, weil sie zusammengehören.

    Oberhalb dieser Werte weist Discord die Interaktion mit HTTP 400 ab: ein Menü ginge ab
    der sechsundzwanzigsten Zeile schlicht nicht mehr auf. Jeder andere Test setzt die
    Konstante ein und verschöbe sich mit ihr, deshalb steht die Zahl hier ausgeschrieben.
    """
    assert erinnern.PRO_SEITE == 5
    assert erinnern.OPTIONEN_GRENZE == 25
    assert einrichten.KANAL_GRENZE == 25
    assert erinnern.KNOPF_GRENZE == 80
    assert ausgabe.MAX_BYTES == 8 * 1024 * 1024


def test_ein_kurzer_text_bleibt_eine_nachricht():
    assert grenzen.teile("Ein Satz.") == ("Ein Satz.",)


def test_ohne_text_gibt_es_nichts_zuzustellen():
    # Discord nimmt keine leere Nachricht an — und der Aufrufer hat nichts zu sagen.
    assert grenzen.teile("") == ()


@pytest.mark.parametrize(
    "text",
    [
        "Zeile.\n" * 900,
        "Wort " * 1500,
        "x" * 6000,
        "Kopf.\n" + "y" * 5000,
    ],
)
def test_geteilt_wird_nichts_weggelassen(text):
    stuecke = grenzen.teile(text)
    assert len(stuecke) > 1
    assert all(len(stueck) <= grenzen.NACHRICHT for stueck in stuecke)
    assert all(stueck for stueck in stuecke)
    assert "".join(stuecke) == text


def test_getrennt_wird_an_der_spaetesten_fuge_davor():
    text = "eins zwei\n" + "z" * 2100
    erstes, zweites, _rest = grenzen.teile(text)
    # Der Umbruch schlägt das Leerzeichen, und beide schlagen den Schnitt mitten im Wort.
    assert erstes == "eins zwei\n"
    assert zweites.startswith("z")
    # Ohne Umbruch bleibt das Leerzeichen: getrennt wird hinter »zwei«, nicht in ihm.
    assert grenzen.teile("eins zwei" + " " + "z" * 1995)[0] == "eins zwei "


def test_ohne_fuge_wird_hart_geschnitten():
    # Ein zerschnittenes Wort ist besser als eine Nachricht, die niemand bekommt.
    erstes, zweites = grenzen.teile("w" * 2500)
    assert len(erstes) == grenzen.NACHRICHT
    assert len(zweites) == 500


def test_was_zuerst_dasteht_kommt_zuerst():
    text = "Der Ausweg.\n" + "Befehl.\n" * 500
    assert grenzen.teile(text)[0].startswith("Der Ausweg.")


def test_gekappt_kuerzt_nur_was_zu_lang_ist():
    assert grenzen.gekappt("kurz", grenzen.EMBED_FELD) == "kurz"
    lang = grenzen.gekappt("a" * 2000, grenzen.EMBED_FELD, "…")
    assert len(lang) == grenzen.EMBED_FELD
    assert lang.endswith("…")


def test_zeilenweise_laesst_keine_halbe_zeile_stehen():
    """Ein harter Schnitt landet mitten in ``[Titel](url)`` — dann steht die Zeile roh da."""
    zeilen = [
        f"[Sitzung vom 2026-06-{tag:02d}](https://discord.example/kanal/{tag}0000000)"
        for tag in range(1, 41)
    ]

    gekuerzt = grenzen.zeilenweise(zeilen, grenzen.EMBED_FELD, "\n… und weitere.")

    assert len(gekuerzt) <= grenzen.EMBED_FELD
    assert gekuerzt.endswith("\n… und weitere.")
    behalten = gekuerzt.removesuffix("\n… und weitere.").split("\n")
    # Jede behaltene Zeile steht unversehrt da, keine ist angeschnitten.
    assert behalten == zeilen[: len(behalten)]
    assert 0 < len(behalten) < len(zeilen)


def test_zeilenweise_laesst_was_hineinpasst_unangetastet():
    zeilen = ["[eins](u)", "[zwei](u)"]
    assert grenzen.zeilenweise(zeilen, grenzen.EMBED_FELD, "…") == "[eins](u)\n[zwei](u)"


def test_zeilenweise_schneidet_hart_wenn_nicht_einmal_die_erste_zeile_passt():
    # Nichts anzuzeigen wäre die schlechtere Antwort als eine angeschnittene Zeile.
    gekuerzt = grenzen.zeilenweise(["z" * 3000], grenzen.EMBED_FELD, "…")
    assert len(gekuerzt) == grenzen.EMBED_FELD
    assert gekuerzt.endswith("…")
