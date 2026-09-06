"""Die fertige Chronik als Journaleintrag zurück in die Welt — der erste Schreibweg.

Bis hierher war der Foundry-Zugang **rein lesend**: ``fetch_world`` fragt einmal und legt
auf. Ein Journaleintrag dreht die Richtung um, und das ist der ganze Grund, warum dieses
Modul für sich steht statt in ``service`` mitzulaufen — wer es öffnet, soll sofort sehen,
dass hier etwas in eine fremde Welt geschrieben wird.

**Warum am Abschluss und nicht am Szenenschnitt.** Der Lauf am Szenenschnitt fasst
bewusst kein Passwort an (#294): der Merkzettel aus #64 wird genau einmal eingelöst, und
wer ihn dort verbrauchte, ließe die Chronik am Abendende ohne ihre Zahlen dastehen. Der
Abschluss hat das Passwort ohnehin in der Hand — er hat eben damit abgeglichen.

**Bester Wille, wie das Sitzungsfenster.** Ein Foundry, das aus ist, hält den Abschluss
nicht auf; die Chronik steht dann im Discord-Thread und sonst nirgends, und der Satz sagt
das. Andersherum wäre es falsch: das Geschriebene zu verlieren, weil ein Server aus war.
"""

from __future__ import annotations

import logging

from chronicle import settings
from chronicle import sprache as sprachen
from chronicle.config import Config
from chronicle.foundry.client import FoundryClient, FoundryError
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

# Foundrys Rechtestufe für »darf lesen«. Der Eintrag gehört der ganzen Runde: sie hat den
# Abend gespielt, und dieselbe Chronik steht bereits in ihrem Discord-Thread. Eine engere
# Stufe hieße, sie vor ihrem eigenen Protokoll zu schützen.
BEOBACHTER = 2

# Foundrys Textformat-Kennung für HTML (1 = HTML, 2 = Markdown). Wir schicken HTML, weil
# das Markdown-Format je nach Version anders gerendert wird.
FORMAT_HTML = 1


def _html(text: str) -> str:
    """Markdown der Chronik in das schmale HTML, das eine Journalseite trägt.

    Absichtlich keine Markdown-Bibliothek: die Chronik erzeugen **wir**, ihre Form ist
    bekannt und besteht aus Überschriften, Absätzen und Strichlisten. Eine Bibliothek
    dafür wäre eine Abhängigkeit für einen Fall, den wir selbst in der Hand haben.

    Escaped wird trotzdem alles: in den Notizen steht wörtliches Tischgespräch, und ein
    ``<`` daraus soll eine Journalseite nicht zerlegen.
    """
    from markupsafe import escape

    zeilen: list[str] = []
    liste_offen = False
    for roh in text.splitlines():
        zeile = roh.strip()
        if not zeile:
            if liste_offen:
                zeilen.append("</ul>")
                liste_offen = False
            continue
        if zeile.startswith("- "):
            if not liste_offen:
                zeilen.append("<ul>")
                liste_offen = True
            zeilen.append(f"<li>{escape(zeile[2:].strip())}</li>")
            continue
        if liste_offen:
            zeilen.append("</ul>")
            liste_offen = False
        if zeile.startswith("#"):
            grad = min(len(zeile) - len(zeile.lstrip("#")), 6)
            zeilen.append(f"<h{grad}>{escape(zeile.lstrip('#').strip())}</h{grad}>")
            continue
        zeilen.append(f"<p>{escape(zeile)}</p>")
    if liste_offen:
        zeilen.append("</ul>")
    return "\n".join(zeilen)


def dokument(titel: str, text: str, *, seitentitel: str) -> dict:
    """Die Nutzlast eines JournalEntry mit genau einer Textseite."""
    return {
        "name": titel,
        "pages": [
            {
                "name": seitentitel,
                "type": "text",
                "title": {"show": False, "level": 1},
                "text": {"content": _html(text), "format": FORMAT_HTML},
            }
        ],
        "ownership": {"default": BEOBACHTER},
    }


def eintragen(
    config: Config,
    runde: Runde,
    *,
    titel: str,
    text: str,
    passwort: str,
    inhaltssprache: str = sprachen.DEFAULT,
    client: FoundryClient | None = None,
) -> str:
    """Die Chronik als Journaleintrag anlegen. Der Rückgabewert ist ein Satz für die Runde.

    Scheitert es, steht das im Satz und nicht im Abbruch — siehe den Kopf dieser Datei.
    """
    texte = sprachen.journal(inhaltssprache)
    wirksam = settings.effective(config, runde)
    try:
        leitung = client or FoundryClient(wirksam, passwort)
        leitung.journal_anlegen(dokument(titel, text, seitentitel=texte.seitentitel))
    except FoundryError as fehler:
        logger.warning("Journaleintrag nicht angelegt: %s", fehler)
        return texte.misslungen
    return texte.angelegt.format(titel=titel)
