"""Die Nacherzählung: mehrere Sitzungen als ein Text — entlang des Registers.

Das ist die Stufe mit dem höchsten Erfindungsrisiko des ganzen Systems. Gelesen wird sie
von Leuten, die die Lücken selbst nicht mehr kennen; ein glatter Satz über einer Lücke
fällt hier niemandem mehr auf. Fünf Dinge halten das:

* **Das Register wählt aus.** In den Aufruf geht nicht die Chronik einer Sitzung, sondern
  das, was ein Mensch für sie bestätigt hat. Was das Register nicht kennt, kommt nicht
  vor — und darüber gibt es hier keine zweite Meinung.
* **Eine Lücke wird benannt, nicht überbrückt.** Eine Sitzung ohne bestätigten Eintrag
  bekommt keinen Absatz, sondern ihren eigenen Abschnitt, der sagt, dass hier nichts steht.
  Das Modell wird für sie gar nicht erst gefragt: die Lücke zeigt auf einen fehlenden
  Registereintrag und nicht auf ein Versagen der Prosa.
* **Die Zahlenschranke läuft je Sitzung.** Belegt ist, was in *ihrer* Chronik und in
  *ihren* Registereinträgen steht — nicht, was irgendwann im Bereich einmal fiel. Sonst
  legitimierte ein Wurf vom ersten Abend dieselbe Zahl noch am zehnten, und aus »Schaden
  47« würde Wochen später »47 Silberstücke«. Was nicht belegt ist, verwirft den Absatz;
  die Sitzung bleibt dann bei ihren belegten Zeilen.
* **Die Überschriften gehören uns, nicht dem Modell.** Ein Absatz, der eine eigene
  Überschrift aufmacht, wird verworfen. Die sichtbare Trennung ist das Einzige, woran ein
  Leser Belegtes von Gedeutetem unterscheidet — dürfte das Modell sie selbst setzen,
  schriebe es sich ein »Belegt aus dem Register«, das von unserem nicht zu unterscheiden
  wäre. Eine Bitte im System-Prompt trägt das nicht.
* **Rollierend, Sitzung für Sitzung.** Ein Bereich über Wochen passt in kein
  Kontextfenster. Jede Sitzung ist ein eigener Aufruf; mitgeführt wird nur der zuletzt
  angenommene Absatz, und der hat beide Schranken schon passiert — dasselbe Muster wie
  in der Komposition.

Die Trennung steht in den Überschriften und nirgends sonst: was unter »Belegt aus dem
Register« steht, hat ein Mensch bestätigt; was unter »Nacherzählt« steht, hat das Modell
verbunden. Was die Zahlenschranke **nicht** kann, steht bei ``composer.numbers``: sie
vergleicht Werte, keine Bedeutungen, und eine einzeln belegte Ziffer lässt sich weiter in
eine neue Aussage setzen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from chronicle import sprache as sprachen
from chronicle.compose.client import ModelError, TextModel
from chronicle.compose.composer import eigene_ueberschrift, numbers

logger = logging.getLogger(__name__)

REGISTER_ZEILE = "{label} »{name}« — {satz}"


@dataclass(frozen=True)
class Abschnitt:
    """Eine Sitzung, so weit das Register sie kennt — plus ihre Chronik für die Schranke."""

    session_id: int
    played_on: str
    title: str | None = None
    chronicle: str = ""
    entries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ErzaehlStoff:
    abschnitte: tuple[Abschnitt, ...] = ()

    @property
    def von(self) -> str:
        return self.abschnitte[0].played_on if self.abschnitte else ""

    @property
    def bis(self) -> str:
        return self.abschnitte[-1].played_on if self.abschnitte else ""


@dataclass(frozen=True)
class Nacherzaehlung:
    text: str
    von: str = ""
    bis: str = ""
    model_name: str | None = None
    reason: str | None = None
    session_count: int = 0
    prose_count: int = 0
    gap_count: int = 0
    inhaltssprache: str = sprachen.DEFAULT

    @property
    def message(self) -> str:
        texte = sprachen.erzaehlung(self.inhaltssprache)
        umfang = texte.umfang.format(sitzungen=self.session_count)
        satz = (
            texte.geordnet.format(umfang=umfang, grund=self.reason)
            if self.reason is not None
            else texte.fertig.format(umfang=umfang, prosa=self.prose_count)
        )
        if not self.gap_count:
            return satz
        vorlage = texte.luecken if self.gap_count == 1 else texte.luecken_mehrere
        return f"{satz} {vorlage.format(anzahl=self.gap_count)}"


def _liste(zeilen: tuple[str, ...]) -> str:
    return "\n".join(f"- {zeile}" for zeile in zeilen)


def _kopfzeile(abschnitt: Abschnitt, texte: sprachen.Erzaehltexte) -> str:
    titel = texte.sitzung.format(datum=abschnitt.played_on)
    return f"{titel}: {abschnitt.title}" if abschnitt.title else titel


def _prompt(stand: str, abschnitt: Abschnitt, texte: sprachen.Erzaehltexte) -> str:
    teile = []
    if stand:
        teile.append(f"{texte.stand_zeile}\n{stand}")
    teile.append(_kopfzeile(abschnitt, texte).lstrip("# "))
    teile.append(f"{texte.register_vorlage}\n{_liste(abschnitt.entries)}")
    teile.append(texte.auftrag)
    return "\n\n".join(teile)


def _kopf(
    stoff: ErzaehlStoff, name: str | None, grund: str | None, texte: sprachen.Erzaehltexte
) -> str:
    titel = texte.kopf.format(von=stoff.von, bis=stoff.bis)
    vorlage = texte.stand if name else texte.stand_ohne_namen
    stand = vorlage.format(name=name) if grund is None else f"_{grund}_"
    return f"{titel}\n\n{stand}"


def nacherzaehlen(
    stoff: ErzaehlStoff,
    model: TextModel | None = None,
    *,
    inhaltssprache: str = sprachen.DEFAULT,
) -> Nacherzaehlung:
    texte = sprachen.erzaehlung(inhaltssprache)
    schreiber = model
    grund = None if model is not None else texte.ohne_modell
    stand = ""
    erzaehlt = 0
    luecken = 0
    bloecke = []

    for abschnitt in stoff.abschnitte:
        # Je Sitzung neu: eine Zahl vom ersten Abend belegt nichts am zehnten. Und das
        # Register zählt mit — es ist die Vorlage, aus der das Modell schöpfen soll.
        belegt = numbers("\n".join((abschnitt.chronicle, *abschnitt.entries)), inhaltssprache)
        teile = [_kopfzeile(abschnitt, texte)]

        if not abschnitt.entries:
            luecken += 1
            teile.append(f"{texte.luecke_titel}\n{texte.luecke}")
            bloecke.append("\n\n".join(teile))
            continue

        if schreiber is not None:
            try:
                absatz = schreiber.write(
                    system=texte.system, prompt=_prompt(stand, abschnitt, texte)
                ).strip()
            except ModelError as fehler:
                grund = texte.nicht_erreichbar
                logger.warning("Nacherzählung läuft ohne Modell weiter: %s", fehler)
                schreiber = None
            else:
                unbelegt = numbers(absatz, inhaltssprache) - belegt
                if unbelegt:
                    # Die unbelegten Zahlen bleiben im Log: im Text wären sie genau die
                    # erfundene Zahl, gegen die diese Stufe gebaut ist.
                    logger.warning(
                        "Sitzung %s: Absatz verworfen, unbelegte Zahlen %s",
                        abschnitt.session_id,
                        sorted(unbelegt),
                    )
                    teile.append(f"{texte.erzaehlt_titel}\n{texte.verworfen}")
                elif eigene_ueberschrift(absatz):
                    logger.warning(
                        "Sitzung %s: Absatz verworfen, er machte eine eigene Überschrift auf",
                        abschnitt.session_id,
                    )
                    teile.append(f"{texte.erzaehlt_titel}\n{texte.verworfen_ueberschrift}")
                else:
                    teile.append(f"{texte.erzaehlt_titel}\n{absatz}")
                    stand = absatz
                    erzaehlt += 1

        teile.append(f"{texte.register_titel}\n{_liste(abschnitt.entries)}")
        bloecke.append("\n\n".join(teile))

    # Erst jetzt, aus der Antwort statt aus der Einstellung (#320).
    name = None if model is None else model.name
    kopf = _kopf(stoff, name, grund, texte)
    return Nacherzaehlung(
        text="\n\n".join([kopf, *bloecke]) + "\n",
        von=stoff.von,
        bis=stoff.bis,
        model_name=name,
        reason=grund,
        session_count=len(stoff.abschnitte),
        prose_count=erzaehlt,
        gap_count=luecken,
        inhaltssprache=sprachen.zurechtgelegt(inhaltssprache),
    )
