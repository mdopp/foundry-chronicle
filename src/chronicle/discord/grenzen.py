"""Discords Maße an einer Stelle — und wie ein zu langer Text sie überlebt.

Eine Nachricht fasst 2000 Zeichen. **py-cord prüft das nicht**: eine längere geht hinaus
und kommt als HTTP 400 zurück, die Nachricht kommt gar nicht erst an. Für einen Text, der
mit jedem neuen Befehl wächst, ist das kein Randfall, sondern der Normalfall von
übermorgen — und für die Vorstellung im Sprachkanal heißt »kommt nicht an«, dass niemand
den Ausweg aus der Aufnahme liest (#109).

Deshalb wird hier **geteilt statt gekürzt**: nichts fällt weg, und was im Text zuerst
steht, steht in der ersten Nachricht. Die Vorstellung hängt genau daran — ihr Ausweg
steht vor der Befehlsliste, also kommt er auch dann an, wenn die Liste dahinter noch zwei
Nachrichten füllt.

Gekürzt wird nur, wo Discord kein zweites Stück zulässt: ein Embed ist **ein** Feld und
keine Folge von Nachrichten. Dort sagt der Hinweis, wo der Text ganz steht.

Was hier nicht steht, ist anderswo schon begrenzt: Knopfbeschriftung, Platzhalter und die
Zahl der Menüzeilen in ``chronicle.bot.erinnern``, die Kanalliste in
``chronicle.bot.einrichten``, die Größe eines Anhangs in ``chronicle.discord.ausgabe``.
"""

from __future__ import annotations

# Discords Grenze je Nachricht. Nitro hebt sie auf 4000 — für einen Bot gilt sie nicht.
NACHRICHT = 2000

# Ein Embed: Titel, Beschreibung, und je Feld ein Wert. Überschreitet **eines** davon sein
# Maß, weist Discord die ganze Nachricht ab, nicht nur das Feld.
EMBED_TITEL = 256
EMBED_TEXT = 4096
EMBED_FELD = 1024


def _fuge(text: str, grenze: int) -> int:
    """Wo geschnitten wird: hinter dem letzten Umbruch, sonst hinter dem letzten Leerzeichen.

    Und wo es beides nicht gibt, mitten im Wort — ein zerschnittenes Wort ist besser als
    eine Nachricht, die niemand bekommt.
    """
    for zeichen in ("\n", " "):
        stelle = text.rfind(zeichen, 1, grenze)
        if stelle > 0:
            return stelle + 1
    return grenze


def teile(text: str, grenze: int = NACHRICHT) -> tuple[str, ...]:
    """Der Text in Stücke, die Discord annimmt — in der Reihenfolge, in der er dasteht.

    Aneinandergehängt ergeben die Stücke wieder genau den Text: hier geht nichts verloren,
    auch kein Trennzeichen. Ein leerer Text ergibt kein Stück — Discord nimmt keine leere
    Nachricht an, und der Aufrufer hat dann nichts zu sagen.
    """
    if not text:
        return ()
    stuecke = []
    rest = text
    while len(rest) > grenze:
        schnitt = _fuge(rest, grenze)
        stuecke.append(rest[:schnitt])
        rest = rest[schnitt:]
    stuecke.append(rest)
    return tuple(stuecke)


def gekappt(text: str, grenze: int, hinweis: str = "") -> str:
    """Für die Stellen, an denen es kein zweites Stück gibt — der Hinweis zählt mit."""
    if len(text) <= grenze:
        return text
    return text[: grenze - len(hinweis)].rstrip() + hinweis
