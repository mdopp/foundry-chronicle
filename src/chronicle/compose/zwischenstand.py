"""Der Zwischenstand: die eben geschlossene Szene, verdichtet, während weitergespielt wird.

Das ist kein zweiter Weg zur Chronik, sondern ein **Vorgriff neben ihr** (#294). Er läuft
am Szenenschnitt statt am Sitzungsende, und das ist die einzige Verschiebung: niemand
wartet auf ihn, die nächste Szene läuft bereits. Der Stapel bleibt der Stapel.

Vier Dinge machen ihn ungefährlich — und das dritte ist der eigentliche Punkt:

* **Er ist Deutung, nie Beleg**, und er sagt das über sich selbst. Sein Kopf steht unter
  ``Zwischenstand``, und die Zeile darunter nennt das Modell und schreibt hin, dass nichts
  davon belegt ist. Einen Belegblock hat er nicht: was belegt ist, steht in der Chronik,
  und zwei Belegblöcke, von denen einer keiner ist, wären genau die Verwechslung, gegen
  die dieses System gebaut ist.
* **Er fließt nirgends zurück.** Er wird nicht abgelegt — keine Notiz, keine Zeile in
  ``protocol``, nichts. Die Chronik am Ende liest Notizen und Foundry-Fakten und findet
  ihn deshalb strukturell nicht; er kann gar nicht als Fakt zurückgelesen werden. Wie bei
  der Nacherzählung ist eine Kopie in der Datenbank nicht bloß überflüssig, sie wäre die
  Gefahr.
* **Die Zahlenschranke läuft gegen diese eine Szene.** Belegt ist, was in *ihren* Notizen
  und *ihren* Foundry-Fakten steht — dieselbe Rechnung wie in der Komposition, und aus
  demselben Grund je Szene und nicht aufsummiert.
* **Die Überschriften gehören uns, nicht dem Modell.** Ein Absatz, der eine eigene
  Überschrift aufmacht, wird verworfen; sonst schriebe sich das Modell sein eigenes
  »Belegt aus Foundry« über einen Text, in dem nichts belegt ist.

Und wo keine Karte steht, fällt er **ersatzlos** aus: ohne Modell oder mit einem, das
nicht antwortet, kommt ``None`` zurück und im Thread erscheint nichts. Eine geordnete
Fassung wäre hier keine Hilfe — die Notizen der Szene stehen als Nachrichten ohnehin im
Thread, und sie zurückzuspiegeln wäre Rauschen. Die Chronik am Ende entsteht wie bisher.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from chronicle import sprache as sprachen
from chronicle.compose.client import ModelError, TextModel
from chronicle.compose.composer import (
    SceneMaterial,
    eigene_ueberschrift,
    numbers,
    szenenfakten,
    szenennotizen,
    zitat,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Zwischenstand:
    text: str
    model_name: str
    position: int = 0
    inhaltssprache: str = sprachen.DEFAULT


def _liste(zeilen: tuple[str, ...]) -> str:
    return "\n".join(f"- {zeile}" for zeile in zeilen)


def _kopf(scene: SceneMaterial, name: str, texte: sprachen.Zwischenstandtexte) -> str:
    titel = texte.kopf.format(position=scene.position)
    if scene.title:
        titel += f" — {scene.title}"
    return f"{titel}\n\n{texte.hinweis.format(name=name)}"


def _prompt(
    scene: SceneMaterial,
    notizen: tuple[str, ...],
    verschriftet: tuple[str, ...],
    fakten: tuple[str, ...],
    texte: sprachen.Zwischenstandtexte,
    chroniktexte: sprachen.Chroniktexte,
) -> str:
    """Dieselben Vorlagenwörter wie in der Komposition — und dieselben Marken darum.

    Die Beschriftungen kommen aus ``Chroniktexte``: es ist dasselbe Material aus derselben
    Szene, und zwei Fassungen davon liefen beim nächsten Umformulieren auseinander. Eigen
    ist nur der Auftrag, und der steht außerhalb der Marken.
    """
    teile = [chroniktexte.szene.format(position=scene.position).lstrip("# ")]
    if scene.title:
        teile[0] += f" — {scene.title}"
    if notizen:
        teile.append(chroniktexte.notizen.format(liste=_liste(notizen)))
    if verschriftet:
        teile.append(chroniktexte.verschriftet.format(liste=_liste(verschriftet)))
    if fakten:
        teile.append(chroniktexte.fakten.format(liste=_liste(fakten)))
    return zitat("\n\n".join(teile)) + f"\n\n{texte.auftrag}"


def zwischenstand(
    scene: SceneMaterial,
    model: TextModel | None,
    *,
    inhaltssprache: str = sprachen.DEFAULT,
) -> Zwischenstand | None:
    """Die geschlossene Szene in wenigen Sätzen — oder ``None``, und dann bleibt es still.

    ``None`` heißt in allen drei Fällen dasselbe: es erscheint nichts, und der Abend läuft
    unverändert weiter. Kein Modell (die Karte fehlt), ein Modell, das nicht antwortet,
    oder eine Szene, in der nichts steht — für alle drei ist Schweigen die ehrliche
    Antwort.
    """
    if model is None:
        return None
    sprache = sprachen.zurechtgelegt(inhaltssprache)
    texte = sprachen.zwischenstand(sprache)
    chroniktexte = sprachen.chronik(sprache)
    notizen, verschriftet = szenennotizen(scene)
    fakten = szenenfakten(scene, sprache)
    if not notizen and not verschriftet and not fakten:
        return None

    try:
        absatz = model.write(
            system=texte.system,
            prompt=_prompt(scene, notizen, verschriftet, fakten, texte, chroniktexte),
        ).strip()
    except ModelError as fehler:
        logger.warning("Der Zwischenstand fällt aus: %s", fehler)
        return None

    belegt = numbers("\n".join(notizen + verschriftet + fakten), sprache)
    unbelegt = numbers(absatz, sprache) - belegt
    if unbelegt:
        # Die unbelegten Zahlen bleiben im Log: im Text wären sie die erfundene Zahl, gegen
        # die auch diese Stufe steht — gedeutet heißt nicht ausgedacht.
        logger.warning(
            "Zwischenstand zu Szene %s verworfen, unbelegte Zahlen %s",
            scene.position,
            sorted(unbelegt),
        )
        absatz = texte.verworfen
    elif eigene_ueberschrift(absatz):
        logger.warning(
            "Zwischenstand zu Szene %s verworfen, er machte eine eigene Überschrift auf",
            scene.position,
        )
        absatz = texte.verworfen_ueberschrift

    return Zwischenstand(
        text=f"{_kopf(scene, model.name, texte)}\n\n{absatz}\n",
        model_name=model.name,
        position=scene.position,
        inhaltssprache=sprache,
    )
