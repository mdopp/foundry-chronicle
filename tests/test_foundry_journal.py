"""Der erste **schreibende** Weg nach Foundry (#327).

Was hier geprüft wird, ist die Form der Nutzlast und das Verhalten drumherum — **nicht**,
dass ein echter Foundry-Server sie annimmt. Das kann dieser Test nicht: dafür braucht es
eine laufende Welt und das Foundry-Passwort, und das speichern wir nicht (#64). Die
Aufrufform bleibt bis zu einem Lauf an einer echten Welt eine begründete Annahme.
"""

from __future__ import annotations

import pytest

from chronicle import sprache as sprachen
from chronicle.foundry.client import FoundryUnreachable
from chronicle.foundry.journal import BEOBACHTER, FORMAT_HTML, _html, dokument, eintragen
from tests.conftest import runde


def test_die_seite_traegt_die_chronik_als_html():
    d = dokument("Chronik", "# Szene 1\n\nEs regnete.\n\n- ein Wurf\n- noch einer", seitentitel="Chronik")
    inhalt = d["pages"][0]["text"]["content"]
    assert "<h1>Szene 1</h1>" in inhalt
    assert "<p>Es regnete.</p>" in inhalt
    assert "<ul>\n<li>ein Wurf</li>\n<li>noch einer</li>\n</ul>" in inhalt
    assert d["pages"][0]["text"]["format"] == FORMAT_HTML


def test_der_eintrag_gehoert_der_ganzen_runde():
    """Dieselbe Chronik steht bereits in ihrem Discord-Thread — enger wäre Zeremonie."""
    assert dokument("C", "x", seitentitel="C")["ownership"] == {"default": BEOBACHTER}


def test_tischgespraech_zerlegt_keine_journalseite():
    """In den Notizen steht wörtliches Gesprochenes; ein ``<`` daraus wird escaped."""
    inhalt = _html('Er sagte "<script>alert(1)</script>".')
    assert "<script>" not in inhalt
    assert "&lt;script&gt;" in inhalt


def test_eine_leerzeile_schliesst_die_liste():
    inhalt = _html("- eins\n\nDanach.")
    assert inhalt.index("</ul>") < inhalt.index("<p>Danach.</p>")


class Leitung:
    def __init__(self, fehler=None):
        self.dokumente = []
        self._fehler = fehler

    def journal_anlegen(self, dokument):
        if self._fehler is not None:
            raise self._fehler
        self.dokumente.append(dokument)
        return "abc123"


def test_ein_abgeschaltetes_foundry_haelt_den_abschluss_nicht_auf(config):
    """Bester Wille, wie das Sitzungsfenster: die Chronik steht dann eben nur im Thread."""
    satz = eintragen(
        config,
        runde(config),
        titel="Chronik",
        text="Es regnete.",
        passwort="geheim",
        client=Leitung(FoundryUnreachable("aus")),
    )
    assert satz == sprachen.journal(sprachen.DEFAULT).misslungen


@pytest.mark.parametrize("sprache", sorted(sprachen.JOURNAL))
def test_der_satz_folgt_der_inhaltssprache(config, sprache):
    leitung = Leitung()
    satz = eintragen(
        config,
        runde(config),
        titel="Chronik vom Abend",
        text="Es regnete.",
        passwort="geheim",
        inhaltssprache=sprache,
        client=leitung,
    )
    assert satz == sprachen.journal(sprache).angelegt.format(titel="Chronik vom Abend")
    assert leitung.dokumente[0]["pages"][0]["name"] == sprachen.journal(sprache).seitentitel
