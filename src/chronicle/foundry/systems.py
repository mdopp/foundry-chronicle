"""Der einzige regelwerksspezifische Fleck der Anbindung.

Bei Wurf-Nachrichten ist ``content`` serverseitig leer — den Text rendert erst der
Client. Die Zahlen liegen an **zwei** Stellen, und welche belegt ist, entscheidet die
Fassung des Regelwerks:

* ``message.system.roll`` — der aufbereitete Block. Dort stehen Titel, Summe, Formel und
  die benannten Würfel; wie die heißen, bestimmt das Regelwerk (Daggerheart legt ``hope``
  und ``fear`` ab, andere Systeme etwas anderes).
* ``message.rolls[]`` — Foundrys eigene Ablage, eine Liste von JSON-**Strings** (#146).
  Darin trägt ``options.roll`` denselben aufbereiteten Block noch einmal; fehlt auch der,
  bleiben die Kernfelder ``total``, ``formula`` und ``class``.

Der zweite Einstieg ist kein Vorrat für alle Fälle, sondern gemessen: gegen einen echten
Daggerheart-Server trug **keine einzige** von 76 archivierten Nachrichten einen
``system.roll`` (#242), und die Chronik blieb deshalb ohne jede Zahl. Der Systemquelltext
erklärt es — dort ist ``roll`` ein Getter über ``rolls[]``, und Getter werden nicht
serialisiert. Wer nur den ersten Einstieg liest, liest gegen manche Welten ins Leere.

Nur diese Zuordnung ist hier je System hinterlegt; heraus kommt für alle dasselbe Modell.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from chronicle.foundry.model import ChatMessage, Die, Roll

DAGGERHEART = "daggerheart"

NAMED_DICE = {DAGGERHEART: ("hope", "fear")}


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


def _aus_kern(wurf: Mapping) -> Roll | None:
    """Was ein serialisierter Wurf ohne aufbereiteten Block noch hergibt.

    Die **benannten** Würfel bleiben hier leer. Welcher der beiden d12 die Hoffnung und
    welcher die Furcht war, sagt erst der aufbereitete Block; aus ``terms[]`` wäre es
    geraten, und eine geratene Zahl im Protokoll ist teurer als eine fehlende.
    """
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
    )


def _ergaenzt(block: Roll, kern: Roll | None) -> Roll:
    """Zwei Beschreibungen **desselben** Wurfs — was die eine offenlässt, sagt oft die andere.

    Der Block unter ``options.roll`` ist nicht überall vollständig: er trägt die benannten
    Würfel, aber nicht immer Formel oder Titel. Die stehen dann eine Ebene höher, im
    serialisierten Wurf selbst. Zusammengelegt wird nur, wo eine Lücke ist — überschrieben
    wird nichts.
    """
    if kern is None:
        return block
    return replace(
        block,
        title=block.title or kern.title,
        total=block.total if block.total is not None else kern.total,
        formula=block.formula or kern.formula,
        kind=block.kind or kern.kind,
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
        kern = _aus_kern(wurf)
        if isinstance(block, Mapping) and block:
            return _ergaenzt(_aus_block(system, block), kern)
        if kern is not None:
            return kern
    return None
