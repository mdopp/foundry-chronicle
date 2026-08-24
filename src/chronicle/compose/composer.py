"""Ordnen, nicht ausschmücken.

Die Zahlen kommen aus dem Foundry-Zwischenspeicher und werden unverändert eingesetzt;
das Modell schreibt nur die Sätze dazwischen. Damit man Wochen später noch sieht, was
davon belegt ist, steht beides in getrennten Abschnitten — Notizen und Foundry-Fakten
wörtlich, Verbindungstext unter einer Überschrift, die ihn als unbelegt ausweist.

Fünf Dinge sind hier die eigentliche Arbeit:

* **Die Zahlenschranke.** Nach jedem Modellaufruf wird geprüft, ob der Text eine Zahl
  nennt, die nicht in den Notizen oder Fakten **dieser Szene** steht — in Ziffern,
  ausgeschrieben oder römisch. Wenn ja, wird der Absatz verworfen; die Szene bleibt dann
  bei ihrer geordneten Fassung. Eine Lücke ist besser als ein erfundener Satz, dem man das
  nicht ansieht. Wie weit sie reicht und wo sie aufhört, steht bei ``numbers``.
* **Getippt und verschriftet stehen getrennt.** Was aus der Aufnahme kommt, trägt eine
  eigene Überschrift: eine verhörte Zahl ist dort alltäglich, und ohne die Trennung
  stünde sie Wochen später so belegt da wie eine abgelesene.
* **Die Überschriften gehören uns, nicht dem Modell.** Ein Absatz, der eine eigene
  Überschrift aufmacht, wird ebenso verworfen. Die sichtbare Trennung ist das Einzige,
  woran ein Leser Belegtes von Gedeutetem unterscheidet — dürfte das Modell sie selbst
  setzen, schriebe es sich sein eigenes »Belegt aus Foundry«, das von unserem nicht zu
  unterscheiden wäre und das der Rückblick anschließend als Foundry-Fakt zurückliest.
  Eine Bitte im System-Prompt trägt das nicht.
* **Szene für Szene.** Ein Sitzungstranskript passt nicht in ein Kontextfenster. Jede
  Szene ist ein eigener Aufruf; mitgeführt wird nur der zuletzt angenommene Absatz, und
  der hat die Zahlenschranke bereits passiert.
* **Das Material ist Zitat, keine Anweisung.** Notizen und Fakten sind gesprochenes und
  geschriebenes Wort aus einer fremden Gilde und stehen im Aufruf zwischen Marken; die
  Anweisungen dieser Stufe stehen außerhalb. An den Zahlen ändert das nichts — die kommen
  aus dem Chat-Log und werden eingesetzt. Es schützt die **Prosa dazwischen**, also genau
  das, was das Modell frei formuliert. Wie weit das trägt, steht bei ``zitat``; dieselben
  Marken setzen ``recap`` und ``register``, die dieses Wort eine Stufe später weiterlesen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from chronicle import sprache as sprachen
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

# Ausgeschrieben ist eine Zahl immer noch eine Zahl.
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

# Drei dieser Wörter heißen im Deutschen öfter etwas anderes als eine Zahl und zählen als
# Grundwort deshalb nicht mit: »ein« ist der unbestimmte Artikel, »elf« ist in einer
# Fantasy-Runde das Volk, und »acht« steht in »gib acht« für kein Ergebnis. Als Vorsilbe
# bleibt »acht« gültig — »achtundzwanzig« ist eindeutig. Näheres bei ``numbers``.
MEHRDEUTIG = {"ein", "elf", "acht"}

GROESSER = {
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

ZAHLWERTE = {name: wert for name, wert in (EINER | GROESSER).items() if name not in MEHRDEUTIG}

# Die Endungen sind die des Zählens und Vervielfachens. »e« und »en« stehen mit Absicht
# nicht dabei: mit ihnen wären »Elfe«, »Elfen«, »achten« und »nullen« Zahlen — siehe
# ``numbers``.
ZAHLWORT = re.compile(
    r"\b(?:(" + "|".join(sorted(EINER, key=len, reverse=True)) + r")(?:und)?)?"
    r"(" + "|".join(sorted(ZAHLWERTE, key=len, reverse=True)) + r")"
    r"(?:mal|fach|erlei|er)?\b",
    re.IGNORECASE,
)

# Und dasselbe für Englisch (#268). Ohne das fiele die Zahlenschranke für die neue
# Vorgabesprache auf Ziffern und römische Zahlen zurück: »seventeen guards« käme durch,
# »siebzehn Wachen« nicht — und die Schranke ist die eine Stelle, an der dieses System
# gegen Erfundenes steht. Die Verbindung schreibt sich hier mit Bindestrich (»twenty-one«)
# statt mit »und«; alles andere ist dieselbe Rechnung.
EINER_EN = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}

# »one« ist im Englischen öfter Pronomen als Zahl (»the one who«, »one of them«) und zählt
# als Grundwort deshalb nicht mit — dieselbe Überlegung wie bei »ein«. Als Vorsilbe bleibt
# es gültig: »one hundred« ist eindeutig.
MEHRDEUTIG_EN = {"one"}

GROESSER_EN = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "dozen": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1000,
}

ZAHLWERTE_EN = {
    name: wert for name, wert in (EINER_EN | GROESSER_EN).items() if name not in MEHRDEUTIG_EN
}

# Drei Gruppen, weil Englisch die Verbindung andersherum schreibt: »one hundred« stellt
# die Vorsilbe voran, »twenty-one« hängt sie hinten an. Deutsch kennt nur die eine
# Reihenfolge (»einundzwanzig«) und kommt mit zwei aus.
ZAHLWORT_EN = re.compile(
    r"\b(?:(" + "|".join(sorted(EINER_EN, key=len, reverse=True)) + r")[- ])?"
    r"(" + "|".join(sorted(ZAHLWERTE_EN, key=len, reverse=True)) + r")"
    r"(?:[- ](" + "|".join(sorted(EINER_EN, key=len, reverse=True)) + r"))?"
    r"(?:fold|s)?\b",
    re.IGNORECASE,
)


def _wortwert(grund: int, einer: int | None, *, voran: bool) -> int:
    """Grundwort und Einerstelle zu einem Wert. Voran heißt mal, dahinter heißt plus.

    »zweihundert« und »two hundred« vervielfachen, »einundzwanzig« und »twenty-one«
    addieren. Die Unterscheidung hängt an der Stellung und nicht an der Sprache — im
    Deutschen fällt beides auf dieselbe Seite, im Englischen nicht.
    """
    if einer is None:
        return grund
    if voran:
        return einer * grund if grund >= 100 else einer + grund
    return grund + einer


def _zahlwoerter_de(text: str) -> set[str]:
    return {
        str(
            _wortwert(
                ZAHLWERTE[grundwort.lower()],
                EINER[vorsilbe.lower()] if vorsilbe else None,
                voran=True,
            )
        )
        for vorsilbe, grundwort in ZAHLWORT.findall(text)
    }


def _zahlwoerter_en(text: str) -> set[str]:
    gefunden = set()
    for vorsilbe, grundwort, nachsilbe in ZAHLWORT_EN.findall(text):
        grund = ZAHLWERTE_EN[grundwort.lower()]
        if vorsilbe:
            gefunden.add(str(_wortwert(grund, EINER_EN[vorsilbe.lower()], voran=True)))
        elif nachsilbe:
            gefunden.add(str(_wortwert(grund, EINER_EN[nachsilbe.lower()], voran=False)))
        else:
            gefunden.add(str(grund))
    return gefunden


# Je Sprache der Weg von einem Text zu den ausgeschriebenen Zahlen darin.
ZAHLWOERTER = {sprachen.DEUTSCH: _zahlwoerter_de, sprachen.ENGLISCH: _zahlwoerter_en}

# Die Marken, zwischen denen das fremde Wort steht — sprachneutral und deshalb in
# ``chronicle.sprache`` nur einmal.
ZITAT_AUF = sprachen.ZITAT_AUF
ZITAT_ZU = sprachen.ZITAT_ZU


@dataclass(frozen=True)
class Notiz:
    """Eine Notiz und die eine Sache, die die Chronik über ihre Herkunft wissen muss.

    ``verschriftet`` heißt: ein Spracherkenner hat den Satz aus dem Ton geholt, ein Mensch
    hat ihn nie gelesen. Das ist nicht dasselbe wie »abgeleitet« — ein eingelesenes
    Notizdokument ist ebenfalls nicht hier getippt, aber jemand hat es geschrieben. Welche
    Herkunft aus ``note.origin`` was bedeutet, entscheidet die Stufe, die die Spalte liest;
    hier steht nur noch das Ergebnis.
    """

    text: str
    verschriftet: bool = False


@dataclass(frozen=True)
class SceneMaterial:
    position: int
    title: str | None = None
    # Eine blanke Zeichenkette ist die getippte Notiz — der Normalfall, und deshalb ohne
    # Umweg über ``Notiz`` schreibbar.
    notes: tuple[Notiz | str, ...] = ()
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
    # Die Sprache, in der dieser Text entstanden ist. Sie steht an der Komposition und
    # wird nicht nachträglich erfragt: die Meldung an die Runde gehört zum Text und muss
    # ihn in derselben Sprache begleiten, auch wenn jemand die Einstellung inzwischen
    # umgestellt hat.
    inhaltssprache: str = sprachen.DEFAULT

    @property
    def message(self) -> str:
        texte = sprachen.chronik(self.inhaltssprache)
        umfang = texte.umfang.format(szenen=self.scene_count, fakten=self.fact_count)
        if self.reason is None:
            return texte.fertig.format(umfang=umfang, prosa=self.prose_count)
        return texte.geordnet.format(umfang=umfang, grund=self.reason)


def _roemischer_wert(wort: str) -> int:
    wert = 0
    groesstes = 0
    for zeichen in reversed(wort):
        einzeln = ROEMISCHE_WERTE[zeichen]
        wert += -einzeln if einzeln < groesstes else einzeln
        groesstes = max(groesstes, einzeln)
    return wert


def numbers(text: str, inhaltssprache: str = sprachen.DEFAULT) -> set[str]:
    """Die Zahlen eines Textes — Ziffern, römisch Geschriebenes und ausgeschriebene Zahlwörter.

    Vergleichbar gemacht wird über den Wert: »XVII« und »siebzehn« zählen als ``17``.
    Ziffernfolgen bleiben dagegen so stehen, wie sie geschrieben sind, damit ``3.5`` nicht
    als belegt gilt, bloß weil ``3`` und ``5`` irgendwo einzeln vorkommen.

    **Die Zahlwörter sind die der Inhaltssprache** (#268). Nur eine Sprache zu kennen wäre
    die gefährliche Richtung: die Schranke fiele für die andere auf Ziffern und römische
    Zahlen zurück, und ein erfundenes »seventeen guards« ginge durch, während »siebzehn
    Wachen« hängen bliebe. Gefragt wird immer nur eine Sprache — die des Textes; beide
    zugleich zu nehmen brächte fremde Fehltreffer und machte die Schranke damit weiter.

    **Die Grenze:** vollständig ist das nicht und kann es nicht sein. Eine einzelne belegte
    Ziffer lässt sich weiter in eine neue Aussage setzen — steht ``5`` in der Vorlage, kommt
    »5 Prozent« durch. Ordnungszahlwörter fehlen mit Absicht: »erst« und »zweit« stehen im
    Deutschen zu oft für etwas anderes, und ein Absatz, der an »erst spät« scheitert, wäre
    ein Fehlalarm bei jedem zweiten Satz.

    Aus demselben Grund zählt ein Zahlwort nur, wo es selbst dasteht, und nur, wenn es
    zuverlässig eine Zahl meint. Die Endungen enden bei ``mal``/``fach``/``erlei``/``er``,
    und ``MEHRDEUTIG`` nimmt »ein«, »elf« und »acht« als Grundwort ganz heraus. Sonst wären
    »Elf«, »Elfe«, »Elfen«, »achten«, »gib acht« und »nullen« Zahlen — und **das ist die
    gefährliche Richtung**: ein solches Wort steht meist in der *Quelle*, und dort macht ein
    Fehltreffer die Schranke weiter statt enger. Stünde »Elfen« in den Notizen, gälte die
    ``11`` als belegt und ein erfundenes »11 Wachen« käme durch. Beide Zahlen bleiben als
    Ziffer und römisch erkannt; verloren geht nur die ausgeschriebene Form.

    Die Schranke fängt das plump Erfundene, nicht jede denkbare Umschreibung.
    """
    gefunden = set(ZIFFERN.findall(text))
    gefunden |= {str(_roemischer_wert(wort)) for wort in ROEMISCH.findall(text) if wort}
    gefunden |= ZAHLWOERTER[sprachen.zurechtgelegt(inhaltssprache)](text)
    return gefunden


def eigene_ueberschrift(absatz: str) -> bool:
    """Ob der Absatz eine Zeile enthält, die Markdown als Überschrift läse.

    ``#`` am Zeilenanfang, oder eine Zeile aus lauter ``=``/``-``, die die Zeile darüber
    zur Überschrift macht. Beides kostet das Modell ein einziges Zeichen und fälschte
    damit die einzige Trennung, die diese Texte haben.
    """
    for zeile in absatz.splitlines():
        blank = zeile.strip()
        if not blank:
            continue
        if blank.startswith("#") or set(blank) <= {"="} or set(blank) <= {"-"}:
            return True
    return False


def _einzeilig(text: str) -> str:
    return " ".join(text.split())


def _liste(zeilen: tuple[str, ...]) -> str:
    return "\n".join(f"- {zeile}" for zeile in zeilen)


def _wurf(roll: Roll, texte: sprachen.Chroniktexte) -> str:
    teile = []
    if roll.total is not None:
        teile.append(texte.summe.format(wert=roll.total))
    if roll.formula:
        teile.append(texte.formel.format(wert=roll.formula))
    if roll.modifier_total is not None:
        teile.append(texte.modifikator.format(wert=roll.modifier_total))
    teile.extend(f"{w.name} {w.faces} = {w.value}" for w in roll.dice)
    if roll.critical:
        teile.append(texte.kritisch)
    return " · ".join(teile) or texte.ohne_zahlen


def fact_line(message: ChatMessage, inhaltssprache: str = sprachen.DEFAULT) -> str:
    texte = sprachen.chronik(inhaltssprache)
    sprecher = message.speaker_alias or message.speaker_actor or texte.ohne_sprecher
    if message.roll is None:
        zeile = f"{sprecher}: {_einzeilig(message.content)}"
    else:
        titel = message.roll.title or message.roll.kind or texte.wurf
        zeile = f"{sprecher} — {titel}: {_wurf(message.roll, texte)}"
    if not message.vanished_at:
        return zeile
    # Der Fakt bleibt belegt — er stand im Chat-Log, als wir ihn holten; nachschlagen kann
    # ihn dort nur niemand mehr. Ohne den Zeitpunkt: die Zeile geht als belegter Fakt in
    # die Zahlenschranke ein, und die Ziffern eines Zeitstempels gälten danach als belegt.
    return f"{zeile} [{NICHT_MEHR_VORHANDEN}]"


def _fakten(scene: SceneMaterial, inhaltssprache: str) -> tuple[str, ...]:
    return tuple(
        fact_line(m, inhaltssprache) for m in scene.facts if m.roll is not None or m.content.strip()
    )


def _notizen(scene: SceneMaterial) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Getipptes und Verschriftetes getrennt, in der Reihenfolge der Vorlage."""
    getippt: list[str] = []
    verschriftet: list[str] = []
    for note in scene.notes:
        eintrag = note if isinstance(note, Notiz) else Notiz(note)
        zeile = _einzeilig(eintrag.text)
        if not zeile:
            continue
        (verschriftet if eintrag.verschriftet else getippt).append(zeile)
    return tuple(getippt), tuple(verschriftet)


def _kopfzeile(scene: SceneMaterial, texte: sprachen.Chroniktexte) -> str:
    return texte.szene.format(position=scene.position) + (
        f" — {scene.title}" if scene.title else ""
    )


def zitat(material: str) -> str:
    """Das Material zwischen Marken, die es selbst nicht setzen kann.

    Szenentitel, Notizen und Fakten sind fremdes Wort — in einer Runde sitzen Leute, die
    weder uns noch den Betreiber der Box kennen. Die Marken trennen dieses Wort von den
    Anweisungen dieser Stufe, und die Marken werden aus dem Material entfernt, bevor es
    hineingeht: sonst schriebe sich ein Satz das Ende der Mitschrift selbst und stünde
    danach da, wo nur unsere Anweisungen stehen.

    **Die Grenze:** das ist eine Abgrenzung, keine Schranke. Ein Modell, das die Marken
    missachtet, hält sich auch an den Satz darüber nicht. Belegt bleibt trotzdem, was
    belegt war — die Zahlen kommen aus dem Chat-Log und werden eingesetzt, kein Zuruf
    kann sie ändern. Was hier gewonnen wird, ist die Prosa dazwischen.
    """
    for marke in (ZITAT_AUF, ZITAT_ZU):
        material = re.sub(re.escape(marke), "…", material, flags=re.IGNORECASE)
    return f"{ZITAT_AUF}\n{material}\n{ZITAT_ZU}"


def _prompt(
    stand: str,
    scene: SceneMaterial,
    notizen: tuple,
    verschriftet: tuple,
    fakten: tuple,
    texte: sprachen.Chroniktexte,
) -> str:
    teile = []
    if stand:
        teile.append(texte.stand_bisher.format(stand=stand))
    teile.append(_kopfzeile(scene, texte).lstrip("# "))
    if notizen:
        teile.append(texte.notizen.format(liste=_liste(notizen)))
    if verschriftet:
        teile.append(texte.verschriftet.format(liste=_liste(verschriftet)))
    if fakten:
        teile.append(texte.fakten.format(liste=_liste(fakten)))
    return zitat("\n\n".join(teile)) + f"\n\n{texte.auftrag}"


def _kopf(
    material: SessionMaterial,
    name: str | None,
    reason: str | None,
    prosa: int,
    fakten: int,
    texte: sprachen.Chroniktexte,
) -> str:
    titel = texte.kopf.format(datum=material.played_on)
    if material.title:
        titel += f": {material.title}"
    if reason is None:
        # Ohne einen einzigen Foundry-Fakt stammt jede Zahl aus einer Notiz — im Regelfall
        # aus einem Whisper-Transkript, wo eine verhörte Zahl alltäglich ist. Wer das
        # Protokoll Wochen später liest, muss das wissen; das Chat-Log hat hier nichts belegt.
        herkunft = texte.herkunft_mit_fakten if fakten else texte.herkunft_ohne_fakten
        stand = texte.stand.format(name=name, herkunft=herkunft)
    elif prosa:
        stand = f"_{texte.teilweise.format(grund=reason)}_"
    else:
        stand = f"_{reason}_"
    return f"{titel}\n\n{stand}"


def compose(
    material: SessionMaterial,
    model: TextModel | None = None,
    *,
    inhaltssprache: str = sprachen.DEFAULT,
) -> Composition:
    texte = sprachen.chronik(inhaltssprache)
    schreiber = model
    name = None if model is None else model.name
    grund = None if model is not None else texte.ohne_modell
    stand = ""
    prosa = 0
    fakten_gesamt = 0
    bloecke = []

    for scene in material.scenes:
        notizen, verschriftet = _notizen(scene)
        fakten = _fakten(scene, inhaltssprache)
        fakten_gesamt += len(fakten)
        # Je Szene neu, wie in ``nacherzaehlung``: eine Zahl aus Szene 1 belegt nichts in
        # Szene 8. Aufsummiert deckte eine verhörte Achtzig vom Anfang des Abends einen
        # selbstsicheren Satz am Ende, in dem sie nirgends steht.
        belegt = numbers("\n".join(notizen + verschriftet + fakten), inhaltssprache)

        teile = [_kopfzeile(scene, texte)]
        if notizen:
            teile.append(f"{texte.notizen_titel}\n{_liste(notizen)}")
        if verschriftet:
            teile.append(f"{texte.transkript_titel}\n{_liste(verschriftet)}")
        if fakten:
            teile.append(f"{texte.beleg_titel}\n{_liste(fakten)}")
        if not notizen and not verschriftet and not fakten:
            teile.append(texte.leer)

        if schreiber is not None and (notizen or verschriftet or fakten):
            try:
                absatz = schreiber.write(
                    system=texte.system,
                    prompt=_prompt(stand, scene, notizen, verschriftet, fakten, texte),
                ).strip()
            except ModelError as fehler:
                # Was genau scheiterte, steht im Log; ins Protokoll gehört der Satz, den
                # der Leser braucht, nicht die Adresse und der Ausnahmename.
                grund = texte.nicht_erreichbar
                logger.warning("Komposition läuft ohne Modell weiter: %s", fehler)
                schreiber = None
            else:
                unbelegt = numbers(absatz, inhaltssprache) - belegt
                if unbelegt:
                    # Die unbelegten Zahlen bleiben im Log und dürfen nicht ins Protokoll:
                    # dort wäre genau das die erfundene Zahl, die hier verhindert wird.
                    logger.warning(
                        "Szene %s: Verbindungstext verworfen, unbelegte Zahlen %s",
                        scene.position,
                        sorted(unbelegt),
                    )
                    teile.append(texte.verworfen)
                elif eigene_ueberschrift(absatz):
                    logger.warning(
                        "Szene %s: Verbindungstext verworfen, er machte eine eigene "
                        "Überschrift auf",
                        scene.position,
                    )
                    teile.append(texte.verworfen_ueberschrift)
                else:
                    teile.append(f"{texte.verbindung_titel}\n{absatz}")
                    stand = absatz
                    prosa += 1

        bloecke.append("\n\n".join(teile))

    kopf = _kopf(material, name, grund, prosa, fakten_gesamt, texte)
    return Composition(
        text="\n\n".join([kopf, *bloecke]) + "\n",
        model_name=name,
        reason=grund,
        scene_count=len(material.scenes),
        fact_count=fakten_gesamt,
        prose_count=prosa,
        inhaltssprache=sprachen.zurechtgelegt(inhaltssprache),
    )
