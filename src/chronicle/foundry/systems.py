"""Der einzige regelwerksspezifische Fleck der Anbindung.

Bei Wurf-Nachrichten ist ``content`` serverseitig leer — den Text rendert erst der
Client. Die Zahlen stehen in ``message.system.roll``, und wie die Würfel dort heißen,
bestimmt das Regelwerk: Daggerheart legt ``hope`` und ``fear`` ab, andere Systeme etwas
anderes. Nur diese Zuordnung ist hier je System hinterlegt; heraus kommt für alle
dasselbe Modell.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    steht in ``message.system.roll``, und dessen Form bestimmt das System. Heute fällt sie
    für alle gleich aus — was Zahlen trägt, lohnt; blanker Chat nicht. Der steht ohnehin
    schon im Thread, getippt von denen, die dort sitzen, und ein Strom, der ihn spiegelt,
    ertränkt die Sitzung in ihrem eigenen Echo.
    """
    return nachricht.roll is not None


def read_roll(system: str, message: Mapping) -> Roll | None:
    system_block = message.get("system")
    block = system_block.get("roll") if isinstance(system_block, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    return Roll(
        title=_text(block.get("title")),
        total=_int(block.get("total")),
        formula=_text(block.get("formula")),
        kind=_text(block.get("type")),
        critical=bool(block.get("isCritical")),
        modifier_total=_int(block.get("modifierTotal")),
        dice=_dice(system, block),
    )
