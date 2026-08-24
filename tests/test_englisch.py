"""Das Dauergate für #268: in der Bedienoberfläche steht kein deutscher Satz mehr.

Die Entscheidung selbst steht in ``chronicle.sprache``: die **Bedienung** ist fest
englisch, der **Inhalt** folgt einer Einstellung je Runde. Ohne einen Test dieser Art
bleibt beim nächsten Feature wieder ein deutscher Satz hängen — genau so ist der Bot
überhaupt erst zweisprachig geworden.

Geprüft wird das ganze Paket ``chronicle.bot`` und nicht eine Liste von Dateien: eine
neue Datei darunter soll von selbst mitgeprüft werden, statt darauf zu warten, dass
jemand sie hier nachträgt.

**Was nicht geprüft wird, und warum:**

* **Kommentare und Docstrings.** Sie bleiben deutsch — das ist die Sprache, in der dieses
  Projekt denkt, und sie steht in keinem Chatfenster. Der AST liefert Kommentare gar nicht
  erst; Docstrings werden ausdrücklich übersprungen.
* **Logzeilen.** Sie gehen an den Betreiber dieser Box und nicht an eine Gilde.
* **``chronicle.sprache``.** Dort *gehören* die deutschen Sätze hin; das ist der Ort, den
  dieser Test überhaupt erzwingt.

Erkannt wird Deutsch an Funktionswörtern und an den Umlauten. Das ist eine Heuristik und
kein Beweis: ein englischer Satz ohne diese Wörter fällt nicht auf, ein deutscher mit
ihnen schon. Die Richtung stimmt — was durchrutscht, ist eine vergessene Übersetzung, was
anschlägt, ist mit Sicherheit deutsch.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import chronicle.bot
from chronicle import settings, sprache

# Die Bedienoberfläche, wörtlich genommen: alles, dessen Zeichenketten in Discord landen.
# Das ganze Paket ``chronicle.bot`` und daneben die Module, deren Meldungen der Bot
# weiterreicht — Laufberichte, Zustellung, Suche, Register, Verschriftung. Was hier nicht
# steht, redet mit niemandem: Schema, Foundry-Adapter, Datenschicht.
PAKET = Path(chronicle.bot.__file__).parent
DANEBEN = (
    "jobs.py",
    "kette.py",
    "lebenszyklus.py",
    "nightly.py",
    "recordings.py",
    "register.py",
    "search.py",
    "discord/ausgabe.py",
    "discord/client.py",
    "discord/rueckblick.py",
    "discord/service.py",
    "transcribe/client.py",
    "transcribe/merge.py",
    "transcribe/service.py",
)

# Funktionswörter, die es im Englischen nicht gibt. Bewusst knapp gehalten und mehrfach
# gekürzt: jedes Wort, das in beiden Sprachen vorkommt, wäre ein Fehlalarm auf einem
# englischen Satz — »was«, »hat«, »die« und »den« standen hier und mussten wieder weg.
DEUTSCHE_WOERTER = (
    "der",
    "das",
    "dem",
    "ein",
    "eine",
    "einen",
    "einem",
    "einer",
    "ist",
    "sind",
    "nicht",
    "und",
    "oder",
    "aber",
    "mit",
    "für",
    "von",
    "auf",
    "sich",
    "dass",
    "wird",
    "werden",
    "kann",
    "noch",
    "schon",
    "wenn",
    "wer",
    "wie",
    "haben",
    "ich",
    "euch",
    "eure",
    "ihr",
)

DEUTSCH = re.compile(
    r"\b(?:" + "|".join(DEUTSCHE_WOERTER) + r")\b|[äöüß]",
    re.IGNORECASE,
)


def _module() -> list[Path]:
    return sorted(PAKET.rglob("*.py")) + [PAKET.parent / name for name in DANEBEN]


def _logzeile(knoten: ast.Call) -> bool:
    """Ob dieser Aufruf ans Log geht — ``logger.warning(...)`` und Verwandte."""
    ziel = knoten.func
    return isinstance(ziel, ast.Attribute) and getattr(ziel.value, "id", "") == "logger"


# ``%s``/``%d`` kommen nur in einer Logging-Vorlage vor: was an eine Runde geht, wird mit
# ``format`` und benannten Feldern gebaut. Eine solche Konstante steht manchmal neben dem
# Aufruf statt darin — erkannt wird sie deshalb an der Form und nicht am Ort.
LOGVORLAGE = re.compile(r"%[sdrif]")


# Ein ``{von}`` in einer Vorlage ist der Name eines Arguments und kein Wort an eine Runde.
# Die Bezeichner bleiben deutsch (#268), also werden die Klammern herausgenommen, bevor
# gelesen wird — sonst schlüge der Test auf einem englischen Satz an.
PLATZHALTER = re.compile(r"\{[^{}]*\}")


def texte(quelle: str) -> list[str]:
    """Jede Zeichenkette eines Moduls, die weder Docstring noch Logzeile ist.

    Eine Konstante, die **ausschließlich** in einem ``logger``-Aufruf vorkommt, zählt
    ebenso als Logzeile. Sonst fiele jeder ausgelagerte Log-Satz durch, obwohl ihn nie
    jemand in Discord liest — und der Ausweg wäre, ihn wieder in den Aufruf zu schreiben,
    also schlechterer Code für einen Test.
    """
    baum = ast.parse(quelle)
    ausgenommen: set[int] = set()
    im_log: set[str] = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Call) and _logzeile(knoten):
            ausgenommen.update(id(teil) for teil in ast.walk(knoten))
            im_log.update(teil.id for teil in ast.walk(knoten) if isinstance(teil, ast.Name))
        if isinstance(knoten, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            erstes = knoten.body[0] if knoten.body else None
            if isinstance(erstes, ast.Expr) and isinstance(erstes.value, ast.Constant):
                ausgenommen.add(id(erstes.value))
    anderswo = {
        knoten.id
        for knoten in ast.walk(baum)
        if isinstance(knoten, ast.Name)
        and isinstance(knoten.ctx, ast.Load)
        and id(knoten) not in ausgenommen
    }
    nur_im_log = im_log - anderswo
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Assign) and any(
            isinstance(ziel, ast.Name) and ziel.id in nur_im_log for ziel in knoten.targets
        ):
            ausgenommen.update(id(teil) for teil in ast.walk(knoten.value))
    return [
        knoten.value
        for knoten in ast.walk(baum)
        if isinstance(knoten, ast.Constant)
        and isinstance(knoten.value, str)
        and id(knoten) not in ausgenommen
    ]


@pytest.mark.parametrize("pfad", _module(), ids=lambda pfad: pfad.name)
def test_bedienoberflaeche_ist_englisch(pfad: Path) -> None:
    deutsch = [
        text
        for text in texte(pfad.read_text(encoding="utf-8"))
        if LOGVORLAGE.search(text) is None
        and DEUTSCH.search(PLATZHALTER.sub(" ", text)) is not None
    ]
    assert not deutsch, (
        f"{pfad.name}: deutsche Zeichenkette in der Bedienoberfläche — "
        f"die Bedienung ist englisch (#268): {deutsch!r}"
    )


def test_der_test_findet_eine_deutsche_zeichenkette() -> None:
    """Ohne diese Probe wäre der Test oben grün, auch wenn er gar nichts prüfte."""
    eingeschmuggelt = 'MELDUNG = "Das hat nicht geklappt — versuch es noch einmal."\n'
    gefunden = [text for text in texte(eingeschmuggelt) if DEUTSCH.search(text)]
    assert not DEUTSCH.search(PLATZHALTER.sub(" ", "from {von} to {bis}"))
    assert gefunden


def test_docstring_und_logzeile_bleiben_deutsch() -> None:
    """Die beiden Ausnahmen, damit sie nicht versehentlich mitgeprüft werden."""
    quelle = (
        '"""Ein deutscher Docstring über die Sache."""\n'
        "def f():\n"
        '    """Und einer für die Funktion."""\n'
        '    logger.warning("Die Zustellung ist gescheitert: %s", 1)\n'
    )
    assert not [text for text in texte(quelle) if DEUTSCH.search(text)]


def test_inhaltssprache_traegt_beide_seiten() -> None:
    """Was der Test oben erzwingt, muss es woanders geben — sonst wäre er bloß Zensur."""
    assert sprache.DEFAULT == sprache.ENGLISCH
    for kennung in sprache.SPRACHEN:
        assert sprache.ANSAGE[kennung].strip()
        assert sprache.chronik(kennung).beleg_titel.strip()
        assert sprache.rueckblick(kennung).chronik_titel.strip()
        assert sprache.erzaehlung(kennung).register_titel.strip()
    # Die deutsche Ansage ist deutsch — die Probe in die Gegenrichtung.
    assert DEUTSCH.search(sprache.ANSAGE[sprache.DEUTSCH])
    assert not DEUTSCH.search(sprache.ANSAGE[sprache.ENGLISCH])


def test_die_einstellung_heisst_hier_wie_in_der_wanderung() -> None:
    """``db`` stempelt den Bestand ohne ``settings`` zu importieren — der Name muss halten."""
    from chronicle import db

    assert db.SPRACHE_KEY == settings.SPRACHE_KEY
    assert db.BESTANDSSPRACHE in sprache.SPRACHEN
