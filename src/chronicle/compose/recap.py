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

from chronicle import sprache as sprachen
from chronicle.compose.client import ModelError, TextModel
from chronicle.compose.composer import eigene_ueberschrift, numbers, zitat

logger = logging.getLogger(__name__)

SZENE = re.compile(r"^## +(\S.*)$")
UEBERSCHRIFT = re.compile(r"^#{1,6} +\S")

MAX_FAEDEN = sprachen.MAX_FAEDEN

# Woran ``digest`` den Belegblock einer Chronik erkennt — in **jeder** Sprache. Eine Runde
# darf umstellen, und dann liegt eine deutsche Chronik unter einem englischen Rückblick;
# nur die eigene Überschrift zu kennen hieße, ihre belegten Fakten zu übersehen und den
# Rückblick still ohne sie zu schreiben.
BELEG_TITEL = tuple(texte.beleg_titel for texte in sprachen.CHRONIK.values())


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
    inhaltssprache: str = sprachen.DEFAULT

    @property
    def message(self) -> str:
        texte = sprachen.rueckblick(self.inhaltssprache)
        umfang = texte.umfang.format(szenen=self.scene_count, fakten=self.fact_count)
        if self.reason is None:
            return texte.fertig.format(umfang=umfang, faeden=self.thread_count)
        return texte.geordnet.format(umfang=umfang, grund=self.reason)


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
            im_beleg = zeile in BELEG_TITEL
            continue
        if im_beleg and zeile.startswith("- "):
            fakten.append(zeile[2:].strip())
    return tuple(szenen), tuple(fakten)


def _liste(zeilen: tuple[str, ...]) -> str:
    return "\n".join(f"- {zeile}" for zeile in zeilen)


def _prompt(material: RecapMaterial, auftrag: str, texte: sprachen.Rueckblicktexte) -> str:
    """Die Chronik zwischen den Marken, der Auftrag außerhalb.

    Die Chronik trägt unter »Notizen« wörtliches Tischgespräch — dasselbe fremde Wort,
    das die Komposition eine Stufe früher schon eingeklammert hat. Ungeklammert stünde es
    hier an derselben Stelle wie unsere eigene Auftragszeile.
    """
    teile = []
    if material.previous:
        teile.append(texte.vorige.format(texte="\n\n".join(material.previous)))
    teile.append(texte.vorlage.format(datum=material.played_on, chronik=material.chronicle.strip()))
    return zitat("\n\n".join(teile)) + f"\n\n{auftrag}"


def _faeden(text: str) -> tuple[str, ...]:
    zeilen = (zeile.strip() for zeile in text.splitlines())
    punkte = (z[2:].strip() for z in zeilen if z.startswith("- "))
    return tuple(punkt for punkt in punkte if punkt)[:MAX_FAEDEN]


def _geprueft(
    absatz: str, belegt: set[str], texte: sprachen.Rueckblicktexte, inhaltssprache: str
) -> tuple[str, str]:
    """Der Absatz — oder nichts und die Zeile, die dem Leser sagt, warum er fehlt."""
    unbelegt = numbers(absatz, inhaltssprache) - belegt
    if unbelegt:
        logger.warning("Rückblick: Absatz verworfen, unbelegte Zahlen %s", sorted(unbelegt))
        return "", texte.verworfen
    if eigene_ueberschrift(absatz):
        logger.warning("Rückblick: Absatz verworfen, er machte eine eigene Überschrift auf")
        return "", texte.verworfen_ueberschrift
    return absatz, ""


def _kopf(
    material: RecapMaterial,
    name: str | None,
    grund: str | None,
    texte: sprachen.Rueckblicktexte,
) -> str:
    titel = texte.kopf.format(datum=material.played_on)
    if material.title:
        titel += f": {material.title}"
    if grund is None:
        quelle = texte.quelle_mit_vorigen if material.previous else texte.quelle
        stand = texte.stand.format(quelle=quelle, name=name)
    else:
        stand = f"_{grund}_"
    return f"{titel}\n\n{stand}"


def recap(
    material: RecapMaterial,
    model: TextModel | None = None,
    *,
    inhaltssprache: str = sprachen.DEFAULT,
) -> Recap:
    texte = sprachen.rueckblick(inhaltssprache)
    szenen, fakten = digest(material.chronicle)
    # Auch die Zahlen der vorigen Rückblicke gelten als belegt: sie haben diese Schranke
    # bereits gegen ihre eigene Chronik passiert.
    belegt = numbers(material.chronicle, inhaltssprache) | numbers(
        "\n".join(material.previous), inhaltssprache
    )
    name = None if model is None else model.name
    grund = None if model is not None else texte.ohne_modell
    hergang = ""
    hergang_grund = ""
    faeden: tuple[str, ...] = ()
    faeden_grund = ""

    if model is not None:
        try:
            roher_hergang = model.write(
                system=texte.system_hergang,
                prompt=_prompt(material, texte.auftrag_hergang, texte),
            )
            rohe_faeden = model.write(
                system=texte.system_faeden,
                prompt=_prompt(material, texte.auftrag_faeden, texte),
            )
        except ModelError as fehler:
            grund = sprachen.chronik(inhaltssprache).nicht_erreichbar
            logger.warning("Rückblick bleibt bei der geordneten Fassung: %s", fehler)
        else:
            hergang, hergang_grund = _geprueft(roher_hergang.strip(), belegt, texte, inhaltssprache)
            geprueft, faeden_grund = _geprueft(rohe_faeden.strip(), belegt, texte, inhaltssprache)
            faeden = _faeden(geprueft)

    bloecke = [_kopf(material, name, grund, texte)]
    if hergang:
        bloecke.append(f"{texte.hergang_titel}\n{hergang}")
    elif hergang_grund:
        bloecke.append(f"{texte.hergang_titel}\n{hergang_grund}")
    if faeden:
        bloecke.append(f"{texte.faeden_titel}\n{_liste(faeden)}")
    elif faeden_grund:
        bloecke.append(f"{texte.faeden_titel}\n{faeden_grund}")
    elif grund is None:
        bloecke.append(f"{texte.faeden_titel}\n{texte.kein_faden}")

    beleg = [texte.chronik_titel]
    if szenen:
        beleg.append(f"{texte.szenen_zeile}\n{_liste(szenen)}")
    if fakten:
        beleg.append(f"{texte.fakten_zeile}\n{_liste(fakten)}")
    if not szenen and not fakten:
        beleg.append(texte.leer)
    bloecke.append("\n\n".join(beleg))

    return Recap(
        text="\n\n".join(bloecke) + "\n",
        model_name=name,
        reason=grund,
        scene_count=len(szenen),
        fact_count=len(fakten),
        thread_count=len(faeden),
        inhaltssprache=sprachen.zurechtgelegt(inhaltssprache),
    )
