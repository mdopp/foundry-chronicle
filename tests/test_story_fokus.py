"""Das Dauergate für #325: was das Rohmaterial liest, erzählt nur das Spiel.

Eine echte Aufnahme besteht zu erheblichen Teilen aus Gespräch **neben** dem Spiel —
Aufnahmetechnik, Werkzeuge, Regelfragen, Termine. Ohne die Regel erzählt das Modell es
gehorsam mit; gemessen am 2026-09-06 begann die Nacherzählung des ersten Spielabends mit
»Die Spielgruppe besprach technische Details zur Aufzeichnung und Synchronisation der
Spielkanäle«. Wahr, und trotzdem falsch: gelesen wird der Text Wochen später als
Gedächtnisstütze an einen *Abend*, und der Abend war das Spiel.

Geprüft werden die Stufen, die **Rohmaterial** sehen, und zwar in jeder Inhaltssprache.
Die Nacherzählung steht bewusst nicht darunter: sie liest allein das Register und nimmt
»ausschließlich die genannten Einträge« auf — ihr Schutz liegt eine Stufe früher.
"""

from __future__ import annotations

import pytest

from chronicle import sprache as sprachen


def stufen(sprache: str) -> dict[str, str]:
    """Jede Stufe, die Transkript oder Notizen im Prompt trägt."""
    return {
        "Chronik-Verbindungstext": sprachen.chronik(sprache).system,
        "Zwischenstand am Szenenschnitt": sprachen.zwischenstand(sprache).system,
        "Rückblick-Hergang": sprachen.rueckblick(sprache).system_hergang,
    }


@pytest.mark.parametrize("sprache", sorted(sprachen.CHRONIK))
def test_jede_rohmaterial_stufe_verbietet_das_tischgespraech(sprache):
    for name, text in stufen(sprache).items():
        assert sprachen._STORY_DE in text or sprachen._STORY_EN in text, name


@pytest.mark.parametrize("sprache", sorted(sprachen.CHRONIK))
def test_die_regel_steht_in_der_sprache_der_stufe(sprache):
    """Eine deutsche Regel in einem englischen Auftrag wäre ein Sprachbruch im Prompt."""
    erwartet = sprachen._STORY_DE if sprache == sprachen.DEUTSCH else sprachen._STORY_EN
    for name, text in stufen(sprache).items():
        assert erwartet in text, name


@pytest.mark.parametrize("regel", [sprachen._STORY_DE, sprachen._STORY_EN])
def test_der_ausweg_verbietet_die_erfindung_und_verlangt_trotzdem_eine_antwort(regel):
    """Der zweite Satz trägt **zwei** Zusagen, und die zweite kam erst am 2026-09-08 dazu.

    Zuerst nur: wo kein Spiel ist, wird keines erfunden. Am ersten echten Abend danach
    lieferte das genau einen Satz — »Die Vorlage gibt kein Spielgeschehen her« — und sonst
    nichts, bei fünfundvierzig Minuten Material und sechs Sprechern. Ehrlich, und für die
    Runde wertlos: an einem Abend, an dem Regeln geklärt und Figuren besprochen werden,
    fallen Entscheidungen, die der nächste braucht.

    Der Ausweg darf also nicht in Schweigen führen. Beides muss dastehen, sonst kippt die
    Regel wieder in die eine oder die andere Richtung.
    """
    zeilen = [z for z in regel.splitlines() if z.strip()]
    assert len(zeilen) == 2
    ausweg = zeilen[1]
    assert "keine Handlung" in ausweg or "invent no action" in ausweg
    assert "schweige auch nicht" in ausweg or "do not fall silent" in ausweg
    assert "besprochen wurde" in ausweg or "discussed instead" in ausweg
