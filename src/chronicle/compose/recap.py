"""Der Rückblick: zweiter Ausgang derselben Komposition, kein eigener Stapel.

Gelesen wird er unmittelbar vor der nächsten Sitzung — er, nicht die Chronik; die ist
das Archiv dahinter. Eingabe ist deshalb die fertige Chronik dieser Sitzung plus die
letzten Rückblicke, nicht noch einmal das Rohmaterial.

Es gilt dieselbe Regel wie in der Chronik, nur verschärft: Verdichtung erhöht das
Erfindungsrisiko, weil die glatte Überleitung umso verführerischer wird, je kürzer der
Text ist. Drei Dinge sichern das ab:

* **Die Zahlenschranke läuft gegen die Chronik.** Nennt das Modell eine Ziffer, die dort
  nicht steht, wird der Absatz verworfen; stehen bleibt die geordnete Fassung.
* **Die Überschriften gehören uns, nicht dem Modell.** Ein Absatz, der eine eigene
  Überschrift aufmacht, wird ebenso verworfen — sonst setzte sich das Modell sein eigenes
  »Belegt aus der Chronik« über den echten Block, und der Satz »Aus dem Foundry-Chat-Log,
  unverändert« stünde über Zeilen, die nie im Chat-Log standen.
* **Offene Fäden sind Deutungen, keine Fakten.** Sie stehen unter einer Überschrift, die
  das sagt — auch dann noch, wenn der Text Wochen später als Gedächtnisstütze dient.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from chronicle.compose.client import ModelError, TextModel
from chronicle.compose.composer import (
    BELEG_TITEL,
    NICHT_ERREICHBAR,
    ZITAT_REGEL,
    eigene_ueberschrift,
    numbers,
    zitat,
)

logger = logging.getLogger(__name__)

SZENE = re.compile(r"^## +(\S.*)$")
UEBERSCHRIFT = re.compile(r"^#{1,6} +\S")

HERGANG_TITEL = "### Was bisher geschah — vom Sprachmodell, nicht belegt"
FAEDEN_TITEL = "### Offene Fäden — Deutung des Modells, keine Fakten"
CHRONIK_TITEL = "### Belegt aus der Chronik"

SZENEN_ZEILE = "Die Szenen dieser Sitzung:"
FAKTEN_ZEILE = "Aus dem Foundry-Chat-Log, unverändert:"

OHNE_MODELL = (
    "Noch kein Modell gewählt — der Rückblick wurde geordnet statt erzählt. "
    "Ein Modell hinterlegt der Betreiber dieser Box."
)

VERWORFEN = "_Verworfen: der Absatz nannte eine Zahl, die in der Chronik nicht vorkommt._"
VERWORFEN_UEBERSCHRIFT = (
    "_Verworfen: der Absatz machte eine eigene Überschrift auf. Welche Zeile belegt ist und "
    "welche gedeutet, sagen hier die Überschriften — die setzt niemand außer dieser Stufe._"
)
KEIN_FADEN = "_Das Modell hat keinen offenen Faden benannt._"
LEER = "_Die Chronik nennt weder Szene noch Foundry-Fakt._"

MAX_FAEDEN = 5

SYSTEM_HERGANG = (
    "Du fasst für eine Tisch-Rollenspiel-Runde zusammen, was zuletzt geschah; gelesen "
    "wird das unmittelbar vor der nächsten Sitzung. Du ordnest und verknüpfst, du "
    "erfindest nichts.\n"
    f"{ZITAT_REGEL}\n"
    "- Zehn bis fünfzehn Sätze, zusammenhängend, in der Vergangenheitsform.\n"
    "- Nenne keine Ziffer und keine Zahl. Die Zahlen stehen belegt unter dem Rückblick.\n"
    "- Erfinde keine Ereignisse, Namen, Orte, Würfe oder Ergebnisse.\n"
    "- Ist die Vorlage dünn, schreibe entsprechend wenig.\n"
    "- Antworte mit dem Text selbst, ohne Überschrift und ohne Vorrede."
)

SYSTEM_FAEDEN = (
    "Du benennst die offenen Fäden einer Tisch-Rollenspiel-Runde: was begonnen und nicht "
    "zu Ende gebracht wurde. Du deutest nur, was in der Vorlage steht.\n"
    f"{ZITAT_REGEL}\n"
    f"- Höchstens {MAX_FAEDEN} Punkte, je einer pro Zeile, jede Zeile beginnt mit '- '.\n"
    "- Nenne keine Ziffer und keine Zahl.\n"
    "- Erfinde keinen Faden. Gibt die Vorlage keinen her, antworte mit: keine\n"
    "- Antworte mit den Punkten selbst, ohne Überschrift und ohne Vorrede."
)

AUFTRAG_HERGANG = "Schreibe den Rückblick auf diese Sitzung."
AUFTRAG_FAEDEN = "Nenne die offenen Fäden dieser Sitzung."


@dataclass(frozen=True)
class RecapMaterial:
    session_id: int
    played_on: str
    title: str | None = None
    chronicle: str = ""
    previous: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recap:
    text: str
    model_name: str | None = None
    reason: str | None = None
    scene_count: int = 0
    fact_count: int = 0
    thread_count: int = 0

    @property
    def message(self) -> str:
        umfang = f"{self.scene_count} Szenen, {self.fact_count} Foundry-Fakten"
        if self.reason is None:
            return f"Rückblick aus {umfang} — {self.thread_count} Fäden als Deutung markiert."
        return f"Rückblick aus {umfang}. {self.reason}"


def digest(chronik: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Szenenfolge und belegte Fakten aus der eigenen Chronik zurücklesen."""
    szenen: list[str] = []
    fakten: list[str] = []
    im_beleg = False
    for rohzeile in chronik.splitlines():
        zeile = rohzeile.strip()
        kopf = SZENE.match(zeile)
        if kopf is not None:
            szenen.append(kopf.group(1))
        if UEBERSCHRIFT.match(zeile):
            im_beleg = zeile == BELEG_TITEL
            continue
        if im_beleg and zeile.startswith("- "):
            fakten.append(zeile[2:].strip())
    return tuple(szenen), tuple(fakten)


def _liste(zeilen: tuple[str, ...]) -> str:
    return "\n".join(f"- {zeile}" for zeile in zeilen)


def _prompt(material: RecapMaterial, auftrag: str) -> str:
    """Die Chronik zwischen den Marken, der Auftrag außerhalb.

    Die Chronik trägt unter »Notizen« wörtliches Tischgespräch — dasselbe fremde Wort,
    das die Komposition eine Stufe früher schon eingeklammert hat. Ungeklammert stünde es
    hier an derselben Stelle wie unsere eigene Auftragszeile.
    """
    teile = []
    if material.previous:
        teile.append(
            "Rückblicke der vorigen Sitzungen, jüngster zuerst:\n\n"
            + "\n\n".join(material.previous)
        )
    teile.append(f"Chronik der Sitzung vom {material.played_on}:\n\n{material.chronicle.strip()}")
    return zitat("\n\n".join(teile)) + f"\n\n{auftrag}"


def _faeden(text: str) -> tuple[str, ...]:
    zeilen = (zeile.strip() for zeile in text.splitlines())
    punkte = (z[2:].strip() for z in zeilen if z.startswith("- "))
    return tuple(punkt for punkt in punkte if punkt)[:MAX_FAEDEN]


def _geprueft(absatz: str, belegt: set[str]) -> tuple[str, str]:
    """Der Absatz — oder nichts und die Zeile, die dem Leser sagt, warum er fehlt."""
    unbelegt = numbers(absatz) - belegt
    if unbelegt:
        logger.warning("Rückblick: Absatz verworfen, unbelegte Zahlen %s", sorted(unbelegt))
        return "", VERWORFEN
    if eigene_ueberschrift(absatz):
        logger.warning("Rückblick: Absatz verworfen, er machte eine eigene Überschrift auf")
        return "", VERWORFEN_UEBERSCHRIFT
    return absatz, ""


def _kopf(material: RecapMaterial, name: str | None, grund: str | None) -> str:
    titel = f"# Rückblick — Sitzung vom {material.played_on}"
    if material.title:
        titel += f": {material.title}"
    if grund is None:
        quelle = "der Chronik dieser Sitzung"
        if material.previous:
            quelle += " und den vorigen Rückblicken"
        stand = (
            f"_Verdichtet aus {quelle} vom Sprachmodell `{name}`. Erzähltes ist gedeutet; "
            "belegt ist nur, was unter »Belegt aus der Chronik« steht._"
        )
    else:
        stand = f"_{grund}_"
    return f"{titel}\n\n{stand}"


def recap(material: RecapMaterial, model: TextModel | None = None) -> Recap:
    szenen, fakten = digest(material.chronicle)
    # Auch die Zahlen der vorigen Rückblicke gelten als belegt: sie haben diese Schranke
    # bereits gegen ihre eigene Chronik passiert.
    belegt = numbers(material.chronicle) | numbers("\n".join(material.previous))
    name = None if model is None else model.name
    grund = None if model is not None else OHNE_MODELL
    hergang = ""
    hergang_grund = ""
    faeden: tuple[str, ...] = ()
    faeden_grund = ""

    if model is not None:
        try:
            roher_hergang = model.write(
                system=SYSTEM_HERGANG, prompt=_prompt(material, AUFTRAG_HERGANG)
            )
            rohe_faeden = model.write(
                system=SYSTEM_FAEDEN, prompt=_prompt(material, AUFTRAG_FAEDEN)
            )
        except ModelError as fehler:
            grund = NICHT_ERREICHBAR
            logger.warning("Rückblick bleibt bei der geordneten Fassung: %s", fehler)
        else:
            hergang, hergang_grund = _geprueft(roher_hergang.strip(), belegt)
            geprueft, faeden_grund = _geprueft(rohe_faeden.strip(), belegt)
            faeden = _faeden(geprueft)

    bloecke = [_kopf(material, name, grund)]
    if hergang:
        bloecke.append(f"{HERGANG_TITEL}\n{hergang}")
    elif hergang_grund:
        bloecke.append(f"{HERGANG_TITEL}\n{hergang_grund}")
    if faeden:
        bloecke.append(f"{FAEDEN_TITEL}\n{_liste(faeden)}")
    elif faeden_grund:
        bloecke.append(f"{FAEDEN_TITEL}\n{faeden_grund}")
    elif grund is None:
        bloecke.append(f"{FAEDEN_TITEL}\n{KEIN_FADEN}")

    beleg = [CHRONIK_TITEL]
    if szenen:
        beleg.append(f"{SZENEN_ZEILE}\n{_liste(szenen)}")
    if fakten:
        beleg.append(f"{FAKTEN_ZEILE}\n{_liste(fakten)}")
    if not szenen and not fakten:
        beleg.append(LEER)
    bloecke.append("\n\n".join(beleg))

    return Recap(
        text="\n\n".join(bloecke) + "\n",
        model_name=name,
        reason=grund,
        scene_count=len(szenen),
        fact_count=len(fakten),
        thread_count=len(faeden),
    )
