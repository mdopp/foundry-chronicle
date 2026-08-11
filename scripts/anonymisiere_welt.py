#!/usr/bin/env python3
"""Aus einem rohen Weltabzug eine eincheckbare Fixture machen.

Der rohe Abzug aus ``python -m chronicle.foundry --dump`` ist personenbezogen: er trägt
die Klarnamen aller Konten der Welt, dazu Journale, Makros und Charakterbiografien voller
Freitext. Er gehört nie ins Repo. Dieses Skript macht daraus eine Welt, die eingecheckt
werden darf — und zwar nach zwei Regeln, die beide nötig sind:

1. **Behalten wird nur, was hier ausdrücklich aufgezählt ist — bis ganz nach unten.** Ein
   Feld, das dieses Skript nicht kennt, könnte einen Namen tragen; eine Biografie, eine
   Journalseite oder ein Makro tut es fast sicher. Wegwerfen ist die einzige Zusage, die
   hält — eine Ausschlussliste wäre nach dem nächsten Foundry-Update unvollständig, ohne
   dass es jemandem auffällt. Die Erlaubnisliste endete früher auf den obersten zwei
   Ebenen; darunter lief ganze Teilbäume ungefiltert durch (``world.title``,
   ``messages[].system.roll.*``, ``rolls[].terms[].options.*``, ``rolls[].options.roll.*``).
   Jetzt beschreibt je ein Bauplan jede erhaltene Ebene, auch die im eingebetteten
   JSON von ``rolls[]``.
2. **Was bleibt, wird nachgeprüft — auf Personendaten, nicht nur auf Namen.** Nach dem
   Umschreiben läuft die Ausgabe noch einmal Zeichenkette für Zeichenkette durch, Werte
   **und** Schlüssel — die Schlüssel lange nicht, und ``ownership`` mit einer
   E-Mail-Adresse darin kam so mit Exit 0 und Erfolgsmeldung durch. Findet
   sie einen Namen aus der Eingabe, eine E-Mail-Adresse, eine Adresse oder einen
   Rechnernamen, eine IP, eine telefonnummernförmige Ziffernfolge oder einen
   Heimatverzeichnis-Pfad, bricht der Lauf ab und schreibt **nichts**. Ein Werkzeug, das
   „keine Personendaten verlassen dieses Haus" verspricht, muss geschlossen scheitern —
   vorher lief alles Nichtnamentliche mit Exit 0 und Erfolgsmeldung durch.

Erhalten bleiben dabei die Strukturen, an denen unsere Strecke hängt: Ids, ``ownership``,
Rollen, die Kopfblöcke ``world`` und ``system`` und die Zahlen eines Wurfs. Umgeschrieben
werden Namen und Aliase — konsistent, dieselbe Person bekommt überall dasselbe Pseudonym.

    python scripts/anonymisiere_welt.py welt-dump.json src/chronicle/foundry/testwelt.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path

# Was an einer Nachricht stand, ist nie eincheckbar: dort steht, was echte Menschen an
# einem echten Abend gesagt haben. Der Platzhalter erhält die Unterscheidung, auf die es
# uns ankommt — eine Nachricht mit Text bleibt eine mit Text.
ERSATZTEXT = "[Wortlaut beim Anonymisieren entfernt]"

# Der Pseudonymraum ist bewusst **kein** Namensvorrat, sondern erfundene Silben mit einer
# laufenden Nummer. Der erste Anlauf nahm hübsche Fantasienamen — und die Selbstprüfung
# hat ihn überführt: hieß eine Figur „Stein", brachte das Pseudonym „Steinmoos" sie durch
# die Hintertür zurück, und der Lauf brach an einem Namen ab, den er selbst erfunden hatte.
# Ein Pseudonymraum, der Bruchstücke der Eingabe enthalten *kann*, ist untauglich; dieser
# hier kann es praktisch nicht, und die Nummer macht jedes Pseudonym für sich eindeutig.
SILBEN = tuple(mitlaut + selbstlaut for mitlaut in "bdfgkmnprstvz" for selbstlaut in "aeiouy")

# Aus welchen Dokumenten Namen gelesen werden. Journale, Ordner und Makros stehen hier
# nicht: sie fallen komplett weg, und ihre Namen als Prüfliste mitzuführen brächte nur
# Zufallstreffer gegen Feldwerte, die gar keine Namen sind.
PERSONEN_LISTEN = ("users", "actors")
ORT_LISTEN = ("scenes",)

# Ab dieser Länge wird ein Namensteil für sich ersetzt und geprüft: „Hendrik" aus
# „Hendrik Grauhand" ist als Alias üblich, „von" wäre eine Falle.
MINDESTLAENGE = 4

# Ein ganzer Name darunter ist keine Spur, sondern ein Kürzel — und als Suchmuster so
# unspezifisch, dass er jedes Pseudonym mit sich reißen würde.
KURZ = 3

# ``world.title`` und ``system.title`` sind Freitext, den die Spielleitung vergibt — im
# Prüfabzug stand dort ein Klarname samt Foundry-Adresse. Die Kennung reicht: unsere
# Strecke bindet die Runde an ``world.id``, und ``world.title`` fällt in
# ``foundry.world.identity`` ohnehin auf die Kennung zurück.
WELT_FELDER = ("id", "type", "version", "coreVersion", "systemVersion")
SYSTEM_FELDER = ("id", "type", "version")
BENUTZER_FELDER = ("_id", "role", "character")
FIGUR_FELDER = ("_id", "type")
SZENEN_FELDER = ("_id",)
NACHRICHT_FELDER = ("_id", "timestamp", "author", "type", "style", "blind")
SPRECHER_FELDER = ("scene", "actor", "token")

# Der Bauplan für eine erhaltene Ebene. ``WERT`` heißt Skalar: Zahlen, Wahrheitswerte und
# ``null`` bleiben stehen, eine Zeichenkette läuft durch die Namensersetzung, alles andere
# fällt weg. ``STUFEN`` war die einzige Stelle mit freien Schlüsseln — ``ownership`` bildet
# Konto-Ids auf Berechtigungsstufen ab. Die Werte waren geprüft, die Schlüssel nicht: ein
# Abzug mit ``{"bob@example.org": 2}`` kam mit Exit 0 durch. Jetzt überlebt als Schlüssel
# nur ``default`` oder eine Konto-Id aus ``users[]`` — alles andere fällt weg.
VORGABE_STUFE = "default"
WERT = "wert"
STUFEN = "stufen"

REROLLED_PLAN = {"any": WERT, "rerolls": [WERT]}
ERGEBNIS_PLAN = {"result": WERT, "active": WERT, "discarded": WERT}
WUERFEL_PLAN = {
    "dice": WERT,
    "total": WERT,
    "formula": WERT,
    "results": [ERGEBNIS_PLAN],
    "rerolled": REROLLED_PLAN,
}
BENANNTER_WUERFEL_PLAN = {"dice": WERT, "value": WERT, "rerolled": REROLLED_PLAN}

# Der aufbereitete Wurf — derselbe Bauplan an beiden Stellen, an denen Foundry ihn ablegt:
# ``message.system.roll`` und ``rolls[].options.roll``. Was hier nicht steht, ist Herkunft,
# Wirkung oder Beschreibung und damit Freitext.
WURF_PLAN = {
    "title": WERT,
    "total": WERT,
    "formula": WERT,
    "type": WERT,
    "isCritical": WERT,
    "modifierTotal": WERT,
    "advantage": {"type": WERT},
    "dice": [WUERFEL_PLAN],
    "hope": BENANNTER_WUERFEL_PLAN,
    "fear": BENANNTER_WUERFEL_PLAN,
    "result": {"duality": WERT, "total": WERT, "label": WERT},
}

# Ein Wurf, wie ihn Foundry im Kern ablegt: ``rolls`` ist eine Liste von JSON-**Strings**.
# ``terms[].options`` trägt die Darstellungsdaten eines Würfelmoduls und darin ein freies
# ``flavor`` — im Prüfabzug eine E-Mail-Adresse und ein Rechnername. Es fällt ganz weg.
TERM_PLAN = {
    "class": WERT,
    "evaluated": WERT,
    "number": WERT,
    "faces": WERT,
    "modifiers": [WERT],
    "results": [ERGEBNIS_PLAN],
    "operator": WERT,
}
WURF_JSON_PLAN = {
    "class": WERT,
    "formula": WERT,
    "total": WERT,
    "evaluated": WERT,
    "dice": [WUERFEL_PLAN],
    "terms": [TERM_PLAN],
    "options": {"title": WERT, "roll": WURF_PLAN},
}

FIGUR_PLAN = {feld: WERT for feld in FIGUR_FELDER} | {"ownership": STUFEN}
SZENEN_PLAN = {feld: WERT for feld in SZENEN_FELDER} | {"ownership": STUFEN}
NACHRICHT_PLAN = {feld: WERT for feld in NACHRICHT_FELDER} | {"whisper": [WERT]}
SPRECHER_PLAN = {feld: WERT for feld in SPRECHER_FELDER}


class Anonymisierung(RuntimeError):
    """Der Lauf trägt nicht — es wird nichts geschrieben."""


def _pseudonym(nummer: int) -> str:
    erste = SILBEN[nummer % len(SILBEN)]
    zweite = SILBEN[(nummer // len(SILBEN)) % len(SILBEN)]
    return f"{(erste + zweite).capitalize()}-{nummer + 1:03d}"


def _teile(name: str) -> list[str]:
    return [teil for teil in re.split(r"[^0-9A-Za-zÄÖÜäöüß]+", name) if len(teil) >= MINDESTLAENGE]


def _namen(raw: Mapping, schluessel: str) -> list[str]:
    gefunden = []
    for eintrag in raw.get(schluessel) or []:
        if not isinstance(eintrag, Mapping):
            continue
        for feld in ("name", "navName", "alias"):
            wert = str(eintrag.get(feld) or "").strip()
            if wert:
                gefunden.append(wert)
    return gefunden


def _aliase(raw: Mapping) -> list[str]:
    gefunden = []
    for nachricht in raw.get("messages") or []:
        sprecher = nachricht.get("speaker") if isinstance(nachricht, Mapping) else None
        if isinstance(sprecher, Mapping):
            wert = str(sprecher.get("alias") or "").strip()
            if wert:
                gefunden.append(wert)
    return gefunden


def _eindeutig(namen: Iterable[str]) -> list[str]:
    gesehen: dict[str, None] = {}
    for name in namen:
        gesehen.setdefault(name, None)
    return list(gesehen)


def _gefahren(namen: Iterable[str]) -> tuple[str, ...]:
    """Was in der Ausgabe nicht vorkommen darf: der ganze Name und seine tragenden Teile."""
    spuren: list[str] = []
    for name in namen:
        if len(name) >= KURZ:
            spuren.append(name)
        spuren.extend(_teile(name))
    return tuple(_eindeutig(spuren))


def _kandidaten(form: Callable[[int], str], gefahren: frozenset[str]) -> Iterator[str]:
    """Pseudonyme, die selbst keine Spur enthalten.

    Ohne diese Schranke wäre die Prüfung wertlos: heißt eine Figur »Stein«, brächte das
    Pseudonym »Steinmoos« sie durch die Hintertür zurück — und der Lauf bräche an einem
    Namen ab, den er selbst erfunden hat.
    """
    nummer = 0
    while True:
        kandidat = form(nummer)
        nummer += 1
        gesenkt = kandidat.casefold()
        if not any(spur in gesenkt for spur in gefahren):
            yield kandidat


class Ersatz:
    """Original → Pseudonym, stabil über den ganzen Lauf.

    Ersetzt wird nicht nur der ganze Name: ein Alias nennt oft nur den Vornamen, und
    „Hendrik" allein wäre sonst durchgerutscht. Geprüft wird gegen dieselbe Liste, gegen
    die ersetzt wird — sonst verspräche die Prüfung etwas anderes, als das Skript tut.
    """

    def __init__(self, raw: Mapping) -> None:
        self.konten = frozenset(
            [VORGABE_STUFE]
            + [str(konto["_id"]) for konto in _dokumente(raw, "users") if konto.get("_id")]
        )
        personen = _eindeutig([name for liste in PERSONEN_LISTEN for name in _namen(raw, liste)])
        aliase = [name for name in _eindeutig(_aliase(raw)) if name not in personen]
        orte = [
            name
            for name in _eindeutig([n for liste in ORT_LISTEN for n in _namen(raw, liste)])
            if name not in personen and name not in aliase
        ]
        self.gefahren = _gefahren(personen + aliase + orte)
        gesenkt = frozenset(spur.casefold() for spur in self.gefahren)

        pseudonyme = _kandidaten(_pseudonym, gesenkt)
        self.zuordnung = {name: next(pseudonyme) for name in personen}
        self._stelle_um(gesenkt)
        # Ein Alias ist meist der Vorname der Figur. Wer schon über die Namensteile ersetzt
        # wird, bekommt kein zweites Pseudonym — sonst spräche in der Fixture plötzlich
        # jemand anderes.
        for alias in aliase:
            if self.text(alias) == alias:
                self.zuordnung[alias] = next(pseudonyme)
        self.zuordnung |= {name: next(pseudonyme) for name in orte}
        self._stelle_um(gesenkt)

    def _stelle_um(self, gesenkt: frozenset[str]) -> None:
        tabelle: dict[str, str] = {}
        for original, pseudo in self.zuordnung.items():
            for gefahr, ersatz in self._paare(original, pseudo):
                tabelle.setdefault(gefahr.casefold(), ersatz)
        # Ersetzt wird genau, was auch geprüft wird — sonst verspräche die Prüfung etwas
        # anderes als den Lauf.
        self._tabelle = {spur: ersatz for spur, ersatz in tabelle.items() if spur in gesenkt}
        self._muster = re.compile(
            "|".join(re.escape(spur) for spur in sorted(self._tabelle, key=len, reverse=True)),
            re.IGNORECASE,
        )

    @staticmethod
    def _paare(original: str, pseudo: str) -> list[tuple[str, str]]:
        # Auch ein einzelner Namensteil wird zum ganzen Pseudonym: der Alias „Hendrik"
        # meint dieselbe Figur wie „Hendrik Grauhand" und soll in der Fixture auch
        # dieselbe bleiben.
        return [(original, pseudo)] + [(teil, pseudo) for teil in _teile(original)]

    def name(self, original: object) -> str:
        text = str(original or "")
        return self.zuordnung.get(text, self.text(text))

    def text(self, original: object) -> str:
        text = str(original or "")
        if not text or not self._tabelle:
            return text
        return self._muster.sub(lambda treffer: self._tabelle[treffer.group(0).casefold()], text)


FEHLT = object()


def _nach_plan(wert: object, plan: object, ersatz: Ersatz) -> object:
    """Ein Wert, auf seinen Bauplan zugeschnitten — was der Plan nicht nennt, fällt weg."""
    if plan is WERT:
        if isinstance(wert, str):
            return ersatz.text(wert)
        return wert if wert is None or isinstance(wert, bool | int | float) else FEHLT
    if plan is STUFEN:
        if not isinstance(wert, Mapping):
            return FEHLT
        return {
            str(schluessel): stufe
            for schluessel, stufe in wert.items()
            if str(schluessel) in ersatz.konten
            and isinstance(stufe, int)
            and not isinstance(stufe, bool)
        }
    if isinstance(plan, list):
        if not isinstance(wert, list):
            return FEHLT
        zugeschnitten = (_nach_plan(eintrag, plan[0], ersatz) for eintrag in wert)
        return [eintrag for eintrag in zugeschnitten if eintrag is not FEHLT]
    if not isinstance(wert, Mapping):
        return FEHLT
    gebaut = {}
    for feld, unterplan in plan.items():
        if feld not in wert:
            continue
        inhalt = _nach_plan(wert[feld], unterplan, ersatz)
        if inhalt is not FEHLT:
            gebaut[feld] = inhalt
    return gebaut


def _felder(quelle: Mapping, felder: Iterable[str], ersatz: Ersatz) -> dict:
    return _nach_plan(quelle, {feld: WERT for feld in felder}, ersatz)


def _kopf(raw: Mapping, schluessel: str, felder: Iterable[str], ersatz: Ersatz) -> dict:
    block = raw.get(schluessel)
    return _felder(block if isinstance(block, Mapping) else {}, felder, ersatz)


def _dokumente(raw: Mapping, schluessel: str) -> list[Mapping]:
    return [eintrag for eintrag in (raw.get(schluessel) or []) if isinstance(eintrag, Mapping)]


def _wurf(roh: object, ersatz: Ersatz) -> str | None:
    """Ein ``rolls``-Eintrag, auf die Rechnung eingedampft — er ist ein JSON-String."""
    try:
        geladen = json.loads(roh) if isinstance(roh, str) else roh
    except ValueError:
        return None
    if not isinstance(geladen, Mapping):
        return None
    return json.dumps(_nach_plan(geladen, WURF_JSON_PLAN, ersatz), ensure_ascii=False)


def _nachricht(nachricht: Mapping, ersatz: Ersatz) -> dict:
    behalten = _nach_plan(nachricht, NACHRICHT_PLAN, ersatz)
    inhalt = str(nachricht.get("content") or "")
    behalten["content"] = ERSATZTEXT if inhalt.strip() else ""
    sprecher = nachricht.get("speaker")
    if isinstance(sprecher, Mapping):
        gekuerzt = _nach_plan(sprecher, SPRECHER_PLAN, ersatz)
        if sprecher.get("alias"):
            gekuerzt["alias"] = ersatz.name(sprecher.get("alias"))
        behalten["speaker"] = gekuerzt
    system = nachricht.get("system")
    # Der dokumentierte Ort der Zahlen. Fehlt er, wird hier keiner erfunden.
    if isinstance(system, Mapping) and isinstance(system.get("roll"), Mapping):
        behalten["system"] = {"roll": _nach_plan(system["roll"], WURF_PLAN, ersatz)}
    wuerfe = [_wurf(eintrag, ersatz) for eintrag in nachricht.get("rolls") or []]
    if any(wuerfe):
        behalten["rolls"] = [wurf for wurf in wuerfe if wurf]
    return behalten


def anonymisiere(raw: Mapping, ersatz: Ersatz | None = None) -> dict:
    """Der Rohabzug, auf das Eincheckbare reduziert und umbenannt."""
    ersatz = Ersatz(raw) if ersatz is None else ersatz
    return {
        "userId": _felder(raw, ("userId",), ersatz).get("userId"),
        "world": _kopf(raw, "world", WELT_FELDER, ersatz),
        "system": _kopf(raw, "system", SYSTEM_FELDER, ersatz),
        "users": [
            _felder(benutzer, BENUTZER_FELDER, ersatz) | {"name": ersatz.name(benutzer.get("name"))}
            for benutzer in _dokumente(raw, "users")
        ],
        "actors": [
            _nach_plan(figur, FIGUR_PLAN, ersatz) | {"name": ersatz.name(figur.get("name"))}
            for figur in _dokumente(raw, "actors")
        ],
        "messages": [_nachricht(nachricht, ersatz) for nachricht in _dokumente(raw, "messages")],
        "scenes": [
            _nach_plan(szene, SZENEN_PLAN, ersatz) | {"name": ersatz.name(szene.get("name"))}
            for szene in _dokumente(raw, "scenes")
        ],
    }


def _als_json(text: str) -> object | None:
    """Ein eingebetteter JSON-Block, oder None — ein blanker Name ist kein JSON."""
    if not text.startswith(("{", "[")):
        return None
    try:
        geladen = json.loads(text)
    except ValueError:
        return None
    return geladen if isinstance(geladen, Mapping | list) else None


def _zeichenketten(wert: object, pfad: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(wert, str):
        # Ein Wurf steht als JSON-String in der Welt. Aufgetrennt statt als ein Text
        # geprüft, damit ein Fund die Stelle im Wurf nennt und nicht nur »irgendwo in
        # rolls[0]«.
        geladen = _als_json(wert)
        if geladen is None:
            yield pfad, wert
        else:
            yield from _zeichenketten(geladen, pfad)
    elif isinstance(wert, Mapping):
        for schluessel, inhalt in wert.items():
            # Auch der Schlüssel wird geprüft. Er ist hier fast überall ein Feldname aus
            # einem Bauplan — aber ``ownership`` bildet auf Konto-Ids ab, und die kamen
            # als E-Mail-Adresse oder Heimatpfad ungeprüft durch. Der Pfad nennt die
            # Stelle, nie den Schlüssel selbst: der Schlüssel *ist* hier der Wert.
            yield f"{pfad}.<Schlüssel>", str(schluessel)
            yield from _zeichenketten(inhalt, f"{pfad}.{schluessel}")
    elif isinstance(wert, list):
        for stelle, inhalt in enumerate(wert):
            yield from _zeichenketten(inhalt, f"{pfad}[{stelle}]")


def _telefonfoermig(text: str) -> bool:
    """Eine Ziffernfolge mit Trennern und mindestens sieben Ziffern.

    Das ``+`` steht nur als Vorwahlzeichen am Anfang, nicht zwischen den Ziffern — sonst
    läse diese Prüfung eine Würfelformel wie ``1d12 + 1d12 + 3`` als Telefonnummer.
    """
    return any(
        sum(zeichen.isdigit() for zeichen in treffer.group(0)) >= 7
        for treffer in ZIFFERNFOLGE.finditer(text)
    )


ZIFFERNFOLGE = re.compile(r"\+?\d[\d\s/.()-]{5,}\d")

# Was außer einem Namen noch eine Person verrät. Die Liste ist die Zusage des Skripts:
# findet sie etwas, wird nichts geschrieben. Ein Fehlalarm kostet einen Blick, ein
# übersehener Fund kostet die Zusage.
PERSONENSPUREN: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("E-Mail", re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}").search),
    ("Adresse", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://").search),
    (
        "Rechnername",
        re.compile(r"(?:[0-9A-Za-z](?:[0-9A-Za-z-]*[0-9A-Za-z])?\.)+[A-Za-z]{2,}").search,
    ),
    (
        "IP-Adresse",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b").search,
    ),
    ("IP-Adresse", re.compile(r"(?:[0-9A-Fa-f]{1,4}:){2,}[0-9A-Fa-f]{0,4}").search),
    (
        "Heimatverzeichnis",
        re.compile(r"[A-Za-z]:[\\/]|\\\\[0-9A-Za-z._-]+\\|/home/|/Users/|/root/|~/").search,
    ),
    ("Telefonnummer", _telefonfoermig),
)


def pruefe(ausgabe: Mapping, gefahren: Iterable[str]) -> list[str]:
    """Keine Personendaten überleben — was hier zurückkommt, ist ein Fund zu viel.

    Geprüft wird gegen die Namen aus der Eingabe **und** gegen die Formen, die eine
    Person auch ohne Namen verraten, über Werte **und** Schlüssel. Der Fund nennt den
    JSON-Pfad und die Art, nie den Wert: diese Zeile landet leicht in einem Log. Auch
    nicht die Anfangszeichen und nicht die Länge des getroffenen Namens — beides ist
    zusammen mit dem Pfad schon eine Spur.
    """
    verdaechtig = [gefahr.casefold() for gefahr in gefahren if gefahr]
    funde = []
    for pfad, text in _zeichenketten(ausgabe):
        gesenkt = text.casefold()
        if any(gefahr in gesenkt for gefahr in verdaechtig):
            funde.append(f"{pfad}: Name")
        funde.extend(f"{pfad}: {art}" for art, trifft in PERSONENSPUREN if trifft(text))
    return funde


def lauf(eingabe: Path, ausgabe: Path) -> str:
    raw = json.loads(eingabe.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise Anonymisierung(f"{eingabe} enthält keinen Weltabzug.")
    ersatz = Ersatz(raw)
    gesaeubert = anonymisiere(raw, ersatz)
    funde = pruefe(gesaeubert, ersatz.gefahren)
    if funde:
        # Der Wert selbst wird nicht ausgeschrieben: diese Zeile landet leicht in einem Log.
        raise Anonymisierung(
            f"{len(funde)} Personendatum/-daten haben überlebt — nichts geschrieben:\n  "
            + "\n  ".join(funde[:20])
        )
    ausgabe.parent.mkdir(parents=True, exist_ok=True)
    ausgabe.write_text(
        json.dumps(gesaeubert, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return (
        f"{ausgabe} geschrieben: {len(gesaeubert['users'])} Konten, "
        f"{len(gesaeubert['actors'])} Figuren, {len(gesaeubert['messages'])} Nachrichten, "
        f"{len(gesaeubert['scenes'])} Szenen. "
        f"{len(ersatz.zuordnung)} Namen ersetzt, {len(ersatz.gefahren)} geprüft, "
        f"{len(PERSONENSPUREN)} Formen von Personendaten ausgeschlossen. "
        "Journale, Ordner, Makros, Gegenstände, Einstellungen und Module sind weggefallen."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("eingabe", type=Path, help="roher Weltabzug (personenbezogen)")
    parser.add_argument("ausgabe", type=Path, help="Ziel der eincheckbaren Fixture")
    args = parser.parse_args(argv)
    try:
        print(lauf(args.eingabe, args.ausgabe))
    except Anonymisierung as fehler:
        print(fehler, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
