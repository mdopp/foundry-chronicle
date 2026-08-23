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

Und hier steht der Rückweg: ``registerpapagei`` erkennt, wenn genau diese Liste als
Transkript **zurückkommt** (#262).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

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


# Woran ein Glied endet. Bindestriche stehen nicht dabei: sie halten Namen zusammen
# (»Sturm-Wind«), und ein Gedankenstrich mitten in einem Satz macht das Glied ohnehin zu
# lang, um noch als Name durchzugehen.
_GLIEDER = re.compile(r"[,;:.!?…\n\r\t]+")

# Wo die Grenze zwischen Papagei und Rede liegt (#262).
#
# Ein einzeln stehender Name ist Rede: »Erlok.« kann die Antwort auf »wer war das?« sein,
# und den zu verwerfen hieße, echte Sprache zu löschen, um erfundene zu verhindern. Zwei
# nebeneinander sind es auch noch — zwei Angesprochene, zwei Ziele. **Ab dem dritten**
# kippt es: drei bloße Namen in einem Segment, ohne ein Verb, einen Artikel oder eine
# Partikel dazwischen, sind eine Liste, und gesprochen wird nicht in Listen. Was dabei im
# schlimmsten Fall verlorengeht, ist die Auskunft, dass drei Namen fielen — die trägt keine
# Chronik. Was ohne die Grenze bleibt, ist erfundener Text in einem Protokoll, das Wochen
# später niemand mehr nachprüft, und das ist der teurere der beiden Fehler.
MINDEST_GLIEDER = 3
MAX_WOERTER = 2


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()


def _namensfoermig(glied: str) -> bool:
    """Ein bis zwei Wörter, keins davon kleingeschrieben — so sieht ein Name aus.

    Kleinschreibung ist der Verräter der Rede: Verben, Artikel, Partikeln, »und«, »nein«
    tragen sie, Eigennamen nicht. Nicht-Buchstaben (Ziffern, fremde Schriftzeichen aus
    einer verstümmelten Ausgabe) gelten als groß — sie sprechen weder für noch gegen Rede.
    """
    woerter = glied.split()
    return 1 <= len(woerter) <= MAX_WOERTER and not any(wort[0].islower() for wort in woerter)


def registerpapagei(text: str, register: Sequence[str]) -> bool:
    """Ist dieses Segment nur das Register, das wir vorgespannt haben — zurückgegeben?

    Whisper füllt stille Abschnitte mit dem, worauf es vorgespannt wurde, und liefert die
    Namen dieser Runde als Transkript zurück (#262). Nichts davon ist gesprochen worden.

    Verlangt werden drei Dinge zugleich, und die beiden ersten sind die Bremse: **jedes**
    Glied muss namensförmig sein — ein einziges »wer ist noch da?« daneben rettet das
    Segment —, es müssen ``MINDEST_GLIEDER`` sein, und die **Mehrheit** muss wörtlich aus
    dem Register stammen. Keine reine Mehrheit, sondern eine unter den beiden anderen
    Bedingungen: der Erkenner gibt das Register verstümmelt zurück (aus einem Namen wird
    »Zie近pe«), eine Forderung »alle« liefe deshalb an genau dem Fall vorbei, der gemeldet
    wurde. Umgekehrt reicht Mehrheit allein nicht — »Arion, Hamed, Gwendol — wer ist noch
    da?« ist Rede und bleibt.

    Ohne Register gibt es nichts zurückzugeben; dann ist das hier nicht zuständig.
    """
    if not register:
        return False
    glieder = [teil for teil in (" ".join(t.split()) for t in _GLIEDER.split(text)) if teil]
    if len(glieder) < MINDEST_GLIEDER or not all(_namensfoermig(teil) for teil in glieder):
        return False
    bekannt = {_normal(name) for name in register}
    treffer = sum(1 for teil in glieder if _normal(teil) in bekannt)
    return treffer * 2 > len(glieder)
