"""Die Eigennamen der Sitzung als Wortvorgabe — und die harte Kappung.

Whisper nimmt je Anfrage eine Wortliste entgegen und schreibt danach erfundene Namen
richtig. Sie geht als Vorspann in dasselbe Fenster, aus dem der Erkenner anschließend
den erkannten Text schreibt — wer es ausfüllt, bekommt keinen Text mehr, sondern einen
Fehler (#248). Deshalb wird hier gekappt, und zwar deutlich unter dem Fenster.

Gekappt wird nach Rangfolge, und die kommt vom Aufrufer: erst die Namen, die im
Chat-Log dieser Sitzung gesprochen haben, dann der Rest des Zwischenspeichers. Ab dem
ersten Namen, der nicht mehr ins Budget passt, ist Schluss — das ganze Kompendium
gehört ohnehin nicht hinein.

**Diese Liste bleibt unser**, auch seit die Erkennung im Nachbardienst läuft (#216): wer
in dieser Sitzung gesprochen hat und wie die Figuren der Kampagne heißen, weiß der
Dienst nie. Er zählt die Token mit dem echten Tokenizer nach und meldet, was er dabei
fallen lässt; hier steht die Rangfolge davor und eine Schätzung, die eher zu viel
verwirft als zu wenig.
"""

from __future__ import annotations

from collections.abc import Iterable

# Whispers Dekoder-Kontext: 448 Token (``max_target_positions``). Aus **demselben**
# Fenster kommen Vorspann und erkannter Text, und faster-whisper füllt es in dieser
# Reihenfolge: eine Marke, die Wortvorgabe, der vorige Text (bis zur halben Fensterlänge
# minus eins), die Startfolge. Was danach übrig ist, ist die Dekodierlänge — ist sie
# null, bricht ctranslate2 mit »The maximum decoding length must be > 0« ab, und die
# ganze Spur scheitert.
FENSTER = 448
VORIGER_TEXT = FENSTER // 2 - 1
MARKER = 4

# Was mindestens zum Schreiben übrig bleiben muss. Ein 30-Sekunden-Fenster dichter
# deutscher Rede sind ~75 Wörter; in Whispers Zerlegung liegt das unter 125 Token, auch
# mit Zeitmarken. Damit hält die Schranke im schlechtesten Fall — voller vorheriger Text
# —, und am Anfang einer Spur, wo es den noch nicht gibt, ist es reichlich.
DEKODIERLAENGE = 125

# 96. Der frühere Wert 224 war die halbe Fensterlänge und damit alles, was faster-whisper
# einer Wortvorgabe überhaupt zugesteht — zusammen mit vollem vorherigem Text blieb nichts
# zum Schreiben (#248).
MAX_TOKEN = FENSTER - VORIGER_TEXT - MARKER - DEKODIERLAENGE

# Whispers Tokenizer steckt im Modell, das hier gerade nicht geladen wird. Drei Zeichen
# je Token schätzt eher zu viele Token als zu wenige — erfundene Namen zerfallen in der
# Zerlegung in kurze Stücke, und die Grenze muss halten statt ungefähr zu stimmen.
ZEICHEN_JE_TOKEN = 3

TRENNER = ", "


def tokens(text: str) -> int:
    return -(-len(text) // ZEICHEN_JE_TOKEN)


def capped(names: Iterable[str], *, max_tokens: int = MAX_TOKEN) -> tuple[str, ...]:
    """Die Namen in Rangfolge, hart auf ``max_tokens`` geschätzte Token begrenzt."""
    rest = max_tokens
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
