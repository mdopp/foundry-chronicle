"""Discords Grenzen: geteilt wird, was nicht in eine Nachricht passt — verloren geht nichts.

Die eine Zusicherung, an der alles hängt: aneinandergehängt ergeben die Stücke wieder den
Text. Ein Text, der mit jedem neuen Befehl wächst, darf an Discords 2000 Zeichen nicht
hängenbleiben, und was vorne steht, muss vorne bleiben — die Vorstellung im Sprachkanal
trägt ihren Ausweg aus der Aufnahme im ersten Absatz.
"""

from __future__ import annotations

import pytest

from chronicle.discord import grenzen


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
