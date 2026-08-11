"""Die Nacherzählung: mehrere Sitzungen als ein Text — entlang des Registers.

Das ist die Stufe mit dem höchsten Erfindungsrisiko des ganzen Systems. Gelesen wird sie
von Leuten, die die Lücken selbst nicht mehr kennen; ein glatter Satz über einer Lücke
fällt hier niemandem mehr auf. Vier Dinge halten das:

* **Das Register wählt aus.** In den Aufruf geht nicht die Chronik einer Sitzung, sondern
  das, was ein Mensch für sie bestätigt hat. Was das Register nicht kennt, kommt nicht
  vor — und darüber gibt es hier keine zweite Meinung.
* **Eine Lücke wird benannt, nicht überbrückt.** Eine Sitzung ohne bestätigten Eintrag
  bekommt keinen Absatz, sondern ihren eigenen Abschnitt, der sagt, dass hier nichts steht.
  Das Modell wird für sie gar nicht erst gefragt: die Lücke zeigt auf einen fehlenden
  Registereintrag und nicht auf ein Versagen der Prosa.
* **Die Zahlenschranke läuft gegen die Chroniken des Bereichs.** Nennt das Modell eine
  Ziffer, die dort nicht steht, wird der Absatz verworfen — die Sitzung bleibt dann bei
  ihren belegten Zeilen.
* **Rollierend, Sitzung für Sitzung.** Ein Bereich über Wochen passt in kein
  Kontextfenster. Jede Sitzung ist ein eigener Aufruf; mitgeführt wird nur der zuletzt
  angenommene Absatz, und der hat die Zahlenschranke schon passiert — dasselbe Muster wie
  in der Komposition.

Die Trennung steht in den Überschriften und nirgends sonst: was unter »Belegt aus dem
Register« steht, hat ein Mensch bestätigt; was unter »Nacherzählt« steht, hat das Modell
verbunden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from chronicle.compose.client import ModelError, TextModel
from chronicle.compose.composer import NICHT_ERREICHBAR, numbers

logger = logging.getLogger(__name__)

ERZAEHLT_TITEL = "### Nacherzählt — vom Sprachmodell, nicht belegt"
REGISTER_TITEL = "### Belegt aus dem Register"
LUECKE_TITEL = "### Lücke — kein bestätigter Registereintrag"

LUECKE = (
    "_Zu dieser Sitzung führt das Register nichts Bestätigtes. Die Lücke bleibt stehen: "
    "erzählt wird hier nichts, und überbrückt wird sie auch nicht._"
)

VERWORFEN = (
    "_Verworfen: der Absatz nannte eine Zahl, die in den Chroniken dieses Bereichs nicht vorkommt._"
)

OHNE_MODELL = (
    "Noch kein Modell gewählt — der Bereich wurde aufgereiht statt erzählt. "
    "Ein Modell wählst du in den Einstellungen."
)

REGISTER_ZEILE = "{label} »{name}« — {satz}"

SYSTEM = (
    "Du erzählst einer Tisch-Rollenspiel-Runde nach, was über mehrere Abende geschah. Du "
    "ordnest und verknüpfst, du erfindest nichts.\n"
    "- Höchstens fünf Sätze, zusammenhängend, in der Vergangenheitsform.\n"
    "- Nenne keine Ziffer und keine Zahl.\n"
    "- Nimm ausschließlich die genannten Einträge auf. Erfinde keine Figur, keinen Ort, "
    "keinen Faden, kein Ereignis und keinen Wurf.\n"
    "- Ist die Vorlage dünn, schreibe entsprechend wenig; eine Lücke füllst du nicht.\n"
    "- Antworte mit dem Absatz selbst, ohne Überschrift und ohne Vorrede."
)

AUFTRAG = "Erzähle diese Sitzung nach."

STAND_ZEILE = "Stand bisher:"
REGISTER_VORLAGE = "Das Register führt zu dieser Sitzung:"


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

    @property
    def message(self) -> str:
        umfang = f"Nacherzählung über {self.session_count} Sitzungen"
        satz = (
            f"{umfang}. {self.reason}"
            if self.reason is not None
            else f"{umfang} — {self.prose_count} davon erzählt."
        )
        if not self.gap_count:
            return satz
        mehr = "n" if self.gap_count > 1 else ""
        return f"{satz} {self.gap_count} Lücke{mehr} benannt, nicht gefüllt."


def _liste(zeilen: tuple[str, ...]) -> str:
    return "\n".join(f"- {zeile}" for zeile in zeilen)


def _kopfzeile(abschnitt: Abschnitt) -> str:
    titel = f"## Sitzung vom {abschnitt.played_on}"
    return f"{titel}: {abschnitt.title}" if abschnitt.title else titel


def _prompt(stand: str, abschnitt: Abschnitt) -> str:
    teile = []
    if stand:
        teile.append(f"{STAND_ZEILE}\n{stand}")
    teile.append(_kopfzeile(abschnitt).lstrip("# "))
    teile.append(f"{REGISTER_VORLAGE}\n{_liste(abschnitt.entries)}")
    teile.append(AUFTRAG)
    return "\n\n".join(teile)


def _kopf(stoff: ErzaehlStoff, name: str | None, grund: str | None) -> str:
    titel = f"# Nacherzählung — von der Sitzung vom {stoff.von} bis zur Sitzung vom {stoff.bis}"
    if grund is None:
        stand = (
            f"_Erzählt vom Sprachmodell `{name}` entlang des Registers. Belegt ist nur, was "
            "unter »Belegt aus dem Register« steht; die Absätze darüber sind gedeutet. "
            "Sitzungen ohne bestätigten Eintrag stehen als Lücke da._"
        )
    else:
        stand = f"_{grund}_"
    return f"{titel}\n\n{stand}"


def nacherzaehlen(stoff: ErzaehlStoff, model: TextModel | None = None) -> Nacherzaehlung:
    schreiber = model
    name = None if model is None else model.name
    grund = None if model is not None else OHNE_MODELL
    belegt: set[str] = set()
    stand = ""
    erzaehlt = 0
    luecken = 0
    bloecke = []

    for abschnitt in stoff.abschnitte:
        belegt |= numbers(abschnitt.chronicle)
        teile = [_kopfzeile(abschnitt)]

        if not abschnitt.entries:
            luecken += 1
            teile.append(f"{LUECKE_TITEL}\n{LUECKE}")
            bloecke.append("\n\n".join(teile))
            continue

        if schreiber is not None:
            try:
                absatz = schreiber.write(system=SYSTEM, prompt=_prompt(stand, abschnitt)).strip()
            except ModelError as fehler:
                grund = NICHT_ERREICHBAR
                logger.warning("Nacherzählung läuft ohne Modell weiter: %s", fehler)
                schreiber = None
            else:
                unbelegt = numbers(absatz) - belegt
                if unbelegt:
                    # Die unbelegten Zahlen bleiben im Log: im Text wären sie genau die
                    # erfundene Zahl, gegen die diese Stufe gebaut ist.
                    logger.warning(
                        "Sitzung %s: Absatz verworfen, unbelegte Zahlen %s",
                        abschnitt.session_id,
                        sorted(unbelegt),
                    )
                    teile.append(f"{ERZAEHLT_TITEL}\n{VERWORFEN}")
                else:
                    teile.append(f"{ERZAEHLT_TITEL}\n{absatz}")
                    stand = absatz
                    erzaehlt += 1

        teile.append(f"{REGISTER_TITEL}\n{_liste(abschnitt.entries)}")
        bloecke.append("\n\n".join(teile))

    kopf = _kopf(stoff, name, grund)
    return Nacherzaehlung(
        text="\n\n".join([kopf, *bloecke]) + "\n",
        von=stoff.von,
        bis=stoff.bis,
        model_name=name,
        reason=grund,
        session_count=len(stoff.abschnitte),
        prose_count=erzaehlt,
        gap_count=luecken,
    )
