"""Ordnen, nicht ausschmücken.

Die Zahlen kommen aus dem Foundry-Zwischenspeicher und werden unverändert eingesetzt;
das Modell schreibt nur die Sätze dazwischen. Damit man Wochen später noch sieht, was
davon belegt ist, steht beides in getrennten Abschnitten — Notizen und Foundry-Fakten
wörtlich, Verbindungstext unter einer Überschrift, die ihn als unbelegt ausweist.

Zwei Dinge sind hier die eigentliche Arbeit:

* **Die Zahlenschranke.** Nach jedem Modellaufruf wird geprüft, ob der Text eine Zahl
  nennt, die nicht in den Notizen oder Fakten dieser Sitzung steht — in Ziffern,
  ausgeschrieben oder römisch. Wenn ja, wird der Absatz verworfen; die Szene bleibt dann
  bei ihrer geordneten Fassung. Eine Lücke ist besser als ein erfundener Satz, dem man das
  nicht ansieht. Wie weit sie reicht und wo sie aufhört, steht bei ``numbers``.
* **Szene für Szene.** Ein Sitzungstranskript passt nicht in ein Kontextfenster. Jede
  Szene ist ein eigener Aufruf; mitgeführt wird nur der zuletzt angenommene Absatz, und
  der hat die Zahlenschranke bereits passiert.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from chronicle.compose.client import ModelError, TextModel
from chronicle.foundry.model import NICHT_MEHR_VORHANDEN, ChatMessage, Roll

logger = logging.getLogger(__name__)

# Eine Zahl bleibt zusammen: »3.5« ist ein Wert und nicht die belegte 3 neben der belegten
# 5. Ohne das Trennzeichen im Muster ließe sich aus zwei belegten Ziffern eine dritte Zahl
# zusammensetzen, die nirgends steht.
ZIFFERN = re.compile(r"\d+(?:[.,:]\d+)*")

# Römisch geschrieben ist dieselbe Zahl — geprüft wird der Wert, nicht die Schreibweise.
# Ohne Kleinschreibung, sonst verschluckte das Muster halbe deutsche Wörter.
ROEMISCH = re.compile(
    r"\b(?=[MDCLXVI])M{0,3}(?:C[MD]|D?C{0,3})(?:X[CL]|L?X{0,3})(?:I[XV]|V?I{0,3})\b"
)

ROEMISCHE_WERTE = {"M": 1000, "D": 500, "C": 100, "L": 50, "X": 10, "V": 5, "I": 1}

# Ausgeschrieben ist eine Zahl immer noch eine Zahl. »ein« fehlt mit Absicht: als
# unbestimmter Artikel stünde es in fast jedem Satz und verwürfe jeden zweiten Absatz.
EINER = {
    "null": 0,
    "eins": 1,
    "ein": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "fuenf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
}

ZAHLWERTE = {name: wert for name, wert in EINER.items() if name != "ein"} | {
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "zwoelf": 12,
    "dutzend": 12,
    "dreizehn": 13,
    "vierzehn": 14,
    "fünfzehn": 15,
    "fuenfzehn": 15,
    "sechzehn": 16,
    "siebzehn": 17,
    "achtzehn": 18,
    "neunzehn": 19,
    "zwanzig": 20,
    "dreißig": 30,
    "dreissig": 30,
    "vierzig": 40,
    "fünfzig": 50,
    "fuenfzig": 50,
    "sechzig": 60,
    "siebzig": 70,
    "achtzig": 80,
    "neunzig": 90,
    "hundert": 100,
    "tausend": 1000,
}

ZAHLWORT = re.compile(
    r"\b(?:(" + "|".join(sorted(EINER, key=len, reverse=True)) + r")(?:und)?)?"
    r"(" + "|".join(sorted(ZAHLWERTE, key=len, reverse=True)) + r")"
    r"(?:mal|fach|erlei|er|en|e)?\b",
    re.IGNORECASE,
)

NOTIZEN_TITEL = "### Notizen"
BELEG_TITEL = "### Belegt aus Foundry"
VERBINDUNG_TITEL = "### Verbindungstext — vom Sprachmodell, nicht belegt"
VERWORFEN = "_Der Verbindungstext wurde verworfen: er nannte eine Zahl ohne Beleg._"
LEER = "_Weder Notizen noch Foundry-Fakten._"

# Diese beiden Sätze landen im abgelegten Protokoll und werden Wochen später gelesen —
# von Leuten, die nie eine Umgebungsvariable gesehen haben. Sie sagen deshalb, was ist
# und was als Nächstes zu tun ist, und nennen keinen Namen aus dem Maschinenraum.
OHNE_MODELL = (
    "Noch kein Modell gewählt — die Chronik wurde geordnet, nicht erzählt. "
    "Ein Modell wählst du in den Einstellungen."
)
NICHT_ERREICHBAR = (
    "Das Sprachmodell war nicht erreichbar — geordnet statt erzählt; beim nächsten Lauf "
    "wird es erneut versucht."
)

SYSTEM = (
    "Du bist Chronist für eine Tisch-Rollenspiel-Runde. Du ordnest und verknüpfst, "
    "du erfindest nichts.\n"
    "- Höchstens drei Sätze.\n"
    "- Nenne keine Ziffer und keine Zahl. Die Zahlen stehen bereits belegt im Protokoll.\n"
    "- Erfinde keine Ereignisse, Namen, Orte, Würfe oder Ergebnisse.\n"
    "- Ist die Vorlage dünn, schreibe entsprechend wenig.\n"
    "- Antworte mit dem Absatz selbst, ohne Überschrift und ohne Vorrede."
)


@dataclass(frozen=True)
class SceneMaterial:
    position: int
    title: str | None = None
    notes: tuple[str, ...] = ()
    facts: tuple[ChatMessage, ...] = ()


@dataclass(frozen=True)
class SessionMaterial:
    session_id: int
    played_on: str
    title: str | None = None
    scenes: tuple[SceneMaterial, ...] = ()


@dataclass(frozen=True)
class Composition:
    text: str
    model_name: str | None = None
    reason: str | None = None
    scene_count: int = 0
    fact_count: int = 0
    prose_count: int = 0

    @property
    def message(self) -> str:
        umfang = f"{self.scene_count} Szenen, {self.fact_count} Foundry-Fakten"
        if self.reason is None:
            return f"Chronik aus {umfang} — {self.prose_count} Verbindungstexte vom Modell."
        return f"Chronik aus {umfang}. {self.reason}"


def _roemischer_wert(wort: str) -> int:
    wert = 0
    groesstes = 0
    for zeichen in reversed(wort):
        einzeln = ROEMISCHE_WERTE[zeichen]
        wert += -einzeln if einzeln < groesstes else einzeln
        groesstes = max(groesstes, einzeln)
    return wert


def _wortwert(vorsilbe: str, grundwort: str) -> int:
    grund = ZAHLWERTE[grundwort.lower()]
    if not vorsilbe:
        return grund
    vorne = EINER[vorsilbe.lower()]
    return vorne * grund if grund >= 100 else vorne + grund


def numbers(text: str) -> set[str]:
    """Die Zahlen eines Textes — Ziffern, römisch Geschriebenes und ausgeschriebene Zahlwörter.

    Vergleichbar gemacht wird über den Wert: »XVII« und »siebzehn« zählen als ``17``.
    Ziffernfolgen bleiben dagegen so stehen, wie sie geschrieben sind, damit ``3.5`` nicht
    als belegt gilt, bloß weil ``3`` und ``5`` irgendwo einzeln vorkommen.

    **Die Grenze:** vollständig ist das nicht und kann es nicht sein. Eine einzelne belegte
    Ziffer lässt sich weiter in eine neue Aussage setzen — steht ``5`` in der Vorlage, kommt
    »5 Prozent« durch. Ordnungszahlwörter fehlen mit Absicht: »erst« und »zweit« stehen im
    Deutschen zu oft für etwas anderes, und ein Absatz, der an »erst spät« scheitert, wäre
    ein Fehlalarm bei jedem zweiten Satz. Die Schranke fängt das plump Erfundene, nicht
    jede denkbare Umschreibung.
    """
    gefunden = set(ZIFFERN.findall(text))
    gefunden |= {str(_roemischer_wert(wort)) for wort in ROEMISCH.findall(text) if wort}
    gefunden |= {str(_wortwert(*treffer)) for treffer in ZAHLWORT.findall(text)}
    return gefunden


def _einzeilig(text: str) -> str:
    return " ".join(text.split())


def _liste(zeilen: tuple[str, ...]) -> str:
    return "\n".join(f"- {zeile}" for zeile in zeilen)


def _wurf(roll: Roll) -> str:
    teile = []
    if roll.total is not None:
        teile.append(f"Summe {roll.total}")
    if roll.formula:
        teile.append(f"Formel {roll.formula}")
    if roll.modifier_total is not None:
        teile.append(f"Modifikator {roll.modifier_total}")
    teile.extend(f"{w.name} {w.faces} = {w.value}" for w in roll.dice)
    if roll.critical:
        teile.append("kritisch")
    return " · ".join(teile) or "ohne Zahlen im Chat-Log"


def fact_line(message: ChatMessage) -> str:
    sprecher = message.speaker_alias or message.speaker_actor or "Ohne Sprecher"
    if message.roll is None:
        zeile = f"{sprecher}: {_einzeilig(message.content)}"
    else:
        titel = message.roll.title or message.roll.kind or "Wurf"
        zeile = f"{sprecher} — {titel}: {_wurf(message.roll)}"
    if not message.vanished_at:
        return zeile
    # Der Fakt bleibt belegt — er stand im Chat-Log, als wir ihn holten; nachschlagen kann
    # ihn dort nur niemand mehr. Ohne den Zeitpunkt: die Zeile geht als belegter Fakt in
    # die Zahlenschranke ein, und die Ziffern eines Zeitstempels gälten danach als belegt.
    return f"{zeile} [{NICHT_MEHR_VORHANDEN}]"


def _fakten(scene: SceneMaterial) -> tuple[str, ...]:
    return tuple(fact_line(m) for m in scene.facts if m.roll is not None or m.content.strip())


def _kopfzeile(scene: SceneMaterial) -> str:
    return f"## Szene {scene.position}" + (f" — {scene.title}" if scene.title else "")


def _prompt(stand: str, scene: SceneMaterial, notizen: tuple, fakten: tuple) -> str:
    teile = []
    if stand:
        teile.append(f"Stand bisher:\n{stand}")
    teile.append(_kopfzeile(scene).lstrip("# "))
    if notizen:
        teile.append(f"Notizen:\n{_liste(notizen)}")
    if fakten:
        teile.append(f"Belegte Fakten aus Foundry:\n{_liste(fakten)}")
    teile.append("Schreibe den Verbindungstext für diese Szene.")
    return "\n\n".join(teile)


def _kopf(material: SessionMaterial, name: str | None, reason: str | None, prosa: int) -> str:
    titel = f"# Chronik — Sitzung vom {material.played_on}"
    if material.title:
        titel += f": {material.title}"
    if reason is None:
        stand = (
            f"_Verbindungstexte stammen vom Sprachmodell `{name}`. Alle Zahlen stehen "
            "unverändert so im Foundry-Chat-Log._"
        )
    elif prosa:
        stand = f"_{reason} Die Szenen bis dahin sind erzählt._"
    else:
        stand = f"_{reason}_"
    return f"{titel}\n\n{stand}"


def compose(material: SessionMaterial, model: TextModel | None = None) -> Composition:
    schreiber = model
    name = None if model is None else model.name
    grund = None if model is not None else OHNE_MODELL
    belegt: set[str] = set()
    stand = ""
    prosa = 0
    fakten_gesamt = 0
    bloecke = []

    for scene in material.scenes:
        notizen = tuple(_einzeilig(n) for n in scene.notes if n.strip())
        fakten = _fakten(scene)
        fakten_gesamt += len(fakten)
        belegt |= numbers("\n".join(notizen + fakten))

        teile = [_kopfzeile(scene)]
        if notizen:
            teile.append(f"{NOTIZEN_TITEL}\n{_liste(notizen)}")
        if fakten:
            teile.append(f"{BELEG_TITEL}\n{_liste(fakten)}")
        if not notizen and not fakten:
            teile.append(LEER)

        if schreiber is not None and (notizen or fakten):
            try:
                absatz = schreiber.write(
                    system=SYSTEM, prompt=_prompt(stand, scene, notizen, fakten)
                ).strip()
            except ModelError as fehler:
                # Was genau scheiterte, steht im Log; ins Protokoll gehört der Satz, den
                # der Leser braucht, nicht die Adresse und der Ausnahmename.
                grund = NICHT_ERREICHBAR
                logger.warning("Komposition läuft ohne Modell weiter: %s", fehler)
                schreiber = None
            else:
                unbelegt = numbers(absatz) - belegt
                if unbelegt:
                    # Die unbelegten Zahlen bleiben im Log und dürfen nicht ins Protokoll:
                    # dort wäre genau das die erfundene Zahl, die hier verhindert wird.
                    logger.warning(
                        "Szene %s: Verbindungstext verworfen, unbelegte Zahlen %s",
                        scene.position,
                        sorted(unbelegt),
                    )
                    teile.append(VERWORFEN)
                else:
                    teile.append(f"{VERBINDUNG_TITEL}\n{absatz}")
                    stand = absatz
                    prosa += 1

        bloecke.append("\n\n".join(teile))

    kopf = _kopf(material, name, grund, prosa)
    return Composition(
        text="\n\n".join([kopf, *bloecke]) + "\n",
        model_name=name,
        reason=grund,
        scene_count=len(material.scenes),
        fact_count=fakten_gesamt,
        prose_count=prosa,
    )
