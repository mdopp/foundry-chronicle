"""Die Eigennamen der Sitzung als Vorspann — und die harte Kappung.

Whisper nimmt einen kurzen Vorspann entgegen und schreibt danach erfundene Namen
richtig. Das Feld fasst rund 224 Token; was darüber liegt, wird **still verworfen** —
deshalb wird hier gekappt und nicht gehofft.

Gekappt wird nach Rangfolge, und die kommt vom Aufrufer: erst die Namen, die im
Chat-Log dieser Sitzung gesprochen haben, dann der Rest des Zwischenspeichers. Ab dem
ersten Namen, der nicht mehr ins Budget passt, ist Schluss — das ganze Kompendium
gehört ohnehin nicht hinein.
"""

from __future__ import annotations

from collections.abc import Iterable

MAX_TOKEN = 224

# Whispers Tokenizer steckt im Modell, das hier gerade nicht geladen wird. Drei Zeichen
# je Token schätzt eher zu viele Token als zu wenige — erfundene Namen zerfallen in der
# Zerlegung in kurze Stücke, und die Grenze muss halten statt ungefähr zu stimmen.
ZEICHEN_JE_TOKEN = 3

EINLEITUNG = "In dieser Sitzung kommen vor: "

TRENNER = ", "


def tokens(text: str) -> int:
    return -(-len(text) // ZEICHEN_JE_TOKEN)


def capped(names: Iterable[str], *, max_tokens: int = MAX_TOKEN) -> tuple[str, ...]:
    """Die Namen in Rangfolge, hart auf ``max_tokens`` geschätzte Token begrenzt."""
    rest = max_tokens - tokens(EINLEITUNG)
    gewaehlt: list[str] = []
    gesehen: set[str] = set()
    for name in names:
        sauber = " ".join(name.split())
        if not sauber or sauber in gesehen:
            continue
        kosten = tokens(sauber + TRENNER)
        if kosten > rest:
            break
        rest -= kosten
        gesehen.add(sauber)
        gewaehlt.append(sauber)
    return tuple(gewaehlt)


def prompt(names: tuple[str, ...]) -> str:
    return f"{EINLEITUNG}{TRENNER.join(names)}." if names else ""
