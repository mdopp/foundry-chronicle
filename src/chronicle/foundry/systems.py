"""Der einzige regelwerksspezifische Fleck der Anbindung.

Bei Wurf-Nachrichten ist ``content`` serverseitig leer — den Text rendert erst der
Client. Die Zahlen liegen an **zwei** Stellen, und welche belegt ist, entscheidet die
Fassung des Regelwerks:

* ``message.system.roll`` — der aufbereitete Block. Dort stehen Titel, Summe, Formel und
  die benannten Würfel; wie die heißen, bestimmt das Regelwerk (Daggerheart legt ``hope``
  und ``fear`` ab, andere Systeme etwas anderes).
* ``message.rolls[]`` — Foundrys eigene Ablage, eine Liste von JSON-**Strings** (#146).
  Darin steht der ausgewertete Wurf: ``total``, ``formula``, ``class`` und ``terms[]``.
  ``options.roll`` trägt daneben einen aufbereiteten Block mit der Wurfart, dem kritischen
  Erfolg und der Summe der Modifikatoren.

Das ist nicht mehr recherchiert, sondern **gemessen** — an einem echten Weltabzug vom
2026-08-06, anonymisiert eingecheckt als ``tests/echtwelt-2026-08-06.json``:

* 59 Nachrichten, davon **40 mit ``rolls[]``** und **0 mit ``system.roll``**. Einen
  ``system``-Block tragen alle 59; ein ``roll`` steht in keinem. Der Systemquelltext
  erklärt es — dort ist ``roll`` ein Getter über ``rolls[]``, und Getter werden nicht
  serialisiert. Wer nur den ersten Einstieg liest, liest gegen diese Welt ins Leere.
* 32 der 40 Würfe tragen ein ``options.roll``. **Keiner davon hat einen ``title``** — der
  steht eine Ebene höher in ``options.title``.
* **Der ausgewertete Wurf führt, nicht der Block.** Bei 2 der 32 beschrieb ``options.roll``
  einen anderen Wurf als den gesendeten (14 aus ``1d12 + 1d12 + 3 + 3`` gegen tatsächlich
  gewürfelte 25 aus ``1d12 + 1d12 + 2``). ``terms[]`` rechnet dagegen in **allen 37**
  auswertbaren Fällen genau die gesendete Summe.
* Die benannten Würfel stehen im Kern: ``terms[].class`` heißt ``HopeDie`` bzw.
  ``FearDie``. Wo Block und Kern denselben Wurf beschreiben, sagen sie dasselbe (30 von
  30); wo nicht, ist der Block der falsche.

Nur diese Zuordnung ist hier je System hinterlegt; heraus kommt für alle dasselbe Modell.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from chronicle.foundry.model import ChatMessage, Die, Roll

DAGGERHEART = "daggerheart"

NAMED_DICE = {DAGGERHEART: ("hope", "fear")}

# Wie Foundrys Kern die benannten Würfel selbst benennt: als Klasse des Terms. Das ist
# nicht geraten, sondern am echten Abzug abgelesen — alle 20 Duality-Würfe tragen dort
# ``HopeDie`` und ``FearDie``. Ein blankes ``Die`` bleibt namenlos.
TERM_DICE = {DAGGERHEART: {"HopeDie": "hope", "FearDie": "fear"}}


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _dice(system: str, block: Mapping) -> tuple[Die, ...]:
    gewuerfelt = []
    for name in NAMED_DICE.get(system, ()):
        eintrag = block.get(name)
        if not isinstance(eintrag, Mapping):
            continue
        wert = _int(eintrag.get("value"))
        if wert is None:
            continue
        gewuerfelt.append(Die(name=name, faces=str(eintrag.get("dice") or ""), value=wert))
    return tuple(gewuerfelt)


def lohnt(nachricht: ChatMessage) -> bool:
    """Ob dieses Ereignis den Weg in den laufenden Sitzungs-Thread lohnt.

    Die Frage gehört hierher, weil ihre Antwort am Regelwerk hängt: was ein Wurf ist,
    entscheidet ``read_roll`` an den Ablagen des Systems. Heute fällt sie für alle gleich
    aus — was Zahlen trägt, lohnt; blanker Chat nicht. Der steht ohnehin schon im Thread,
    getippt von denen, die dort sitzen, und ein Strom, der ihn spiegelt, ertränkt die
    Sitzung in ihrem eigenen Echo.
    """
    return nachricht.roll is not None


def _aufbereitet(message: Mapping) -> Mapping | None:
    """Der dokumentierte Einstieg. Ein leerer Block ist keiner — dann zählt ``rolls[]``."""
    system_block = message.get("system")
    block = system_block.get("roll") if isinstance(system_block, Mapping) else None
    return block if isinstance(block, Mapping) and block else None


def _serialisiert(eintrag: object) -> Mapping | None:
    """``rolls[]`` trägt JSON-**Strings**, keine Objekte — der Server sendet die rohe Form."""
    if isinstance(eintrag, Mapping):
        return eintrag
    if not isinstance(eintrag, str):
        return None
    try:
        geladen = json.loads(eintrag)
    except ValueError:
        return None
    return geladen if isinstance(geladen, Mapping) else None


def _aus_block(system: str, block: Mapping) -> Roll:
    return Roll(
        title=_text(block.get("title")),
        total=_int(block.get("total")),
        formula=_text(block.get("formula")),
        kind=_text(block.get("type")),
        critical=bool(block.get("isCritical")),
        modifier_total=_int(block.get("modifierTotal")),
        dice=_dice(system, block),
    )


def _aus_termen(system: str, terme: object) -> tuple[Die, ...]:
    """Die benannten Würfel aus Foundrys eigener Term-Liste.

    Geraten wird dabei nichts: der Term **sagt**, was er ist (``HopeDie``, ``FearDie``).
    Gezählt werden nur die aktiven Ergebnisse — ein verworfener Wurf ist keine Zahl des
    Abends. Ein Term ohne bekannte Klasse bleibt namenlos und fällt weg.
    """
    namen = TERM_DICE.get(system, {})
    gefunden: dict[str, Die] = {}
    for term in terme or ():
        if not isinstance(term, Mapping):
            continue
        name = namen.get(str(term.get("class")))
        if name is None or name in gefunden:
            continue
        werte = [
            _int(ergebnis.get("result"))
            for ergebnis in term.get("results") or ()
            if isinstance(ergebnis, Mapping) and ergebnis.get("active")
        ]
        werte = [wert for wert in werte if wert is not None]
        if not werte:
            continue
        seiten = _int(term.get("faces"))
        gefunden[name] = Die(name=name, faces=f"d{seiten}" if seiten else "", value=sum(werte))
    return tuple(gefunden[name] for name in NAMED_DICE.get(system, ()) if name in gefunden)


def _aus_kern(system: str, wurf: Mapping) -> Roll | None:
    """Der ausgewertete Wurf selbst — die Rechnung, die Foundry wirklich gemacht hat."""
    optionen = wurf.get("options")
    optionen = optionen if isinstance(optionen, Mapping) else {}
    total = _int(wurf.get("total"))
    formula = _text(wurf.get("formula"))
    if total is None and formula is None:
        return None
    return Roll(
        title=_text(optionen.get("title")),
        total=total,
        formula=formula,
        # Der aufbereitete Block nennt hier die Wurfart (``action``), die rohe Ablage die
        # Wurfklasse (``DualityRoll``). Beides beantwortet »was für ein Wurf war das«.
        kind=_text(wurf.get("class")),
        dice=_aus_termen(system, wurf.get("terms")),
    )


def _beschreibt_denselben(block: Mapping, wurf: Mapping) -> bool:
    """Ob der aufbereitete Block überhaupt von **diesem** Wurf redet.

    Am echten Abzug taten zwei von 32 Blöcken das nicht: ``options.roll`` trug Formel,
    Summe und Würfel eines *früheren* Wurfs weiter — einmal 14 aus ``1d12 + 1d12 + 3 + 3``,
    während gewürfelt und gesendet ``25`` aus ``1d12 + 1d12 + 2`` wurde. Die Formel ist
    der Beleg: wo sie übereinstimmt, stimmen auch Summe und Würfel (30 von 30 gemessen);
    wo nicht, gehört der Block nicht hierher und wird ganz fallengelassen.
    """
    formel = _text(block.get("formula"))
    return formel is None or formel == _text(wurf.get("formula"))


def _ergaenzt(kern: Roll, block: Roll) -> Roll:
    """Zwei Beschreibungen **desselben** Wurfs — der ausgewertete führt.

    Was allein der aufbereitete Block weiß, kommt von dort: die Wurfart, der kritische
    Erfolg, die Summe der Modifikatoren. Zahl, Formel und Würfel stehen im ausgewerteten
    Wurf und werden von ihm nicht überschrieben.
    """
    return replace(
        kern,
        title=kern.title or block.title,
        total=kern.total if kern.total is not None else block.total,
        formula=kern.formula or block.formula,
        kind=block.kind or kern.kind,
        critical=block.critical,
        modifier_total=block.modifier_total,
        dice=kern.dice or block.dice,
    )


def read_roll(system: str, message: Mapping) -> Roll | None:
    block = _aufbereitet(message)
    if block is not None:
        return _aus_block(system, block)
    for eintrag in message.get("rolls") or ():
        wurf = _serialisiert(eintrag)
        if wurf is None:
            continue
        optionen = wurf.get("options")
        block = optionen.get("roll") if isinstance(optionen, Mapping) else None
        kern = _aus_kern(system, wurf)
        if isinstance(block, Mapping) and block:
            if kern is None:
                return _aus_block(system, block)
            if _beschreibt_denselben(block, wurf):
                return _ergaenzt(kern, _aus_block(system, block))
        if kern is not None:
            return kern
    return None
