#!/usr/bin/env python3
"""Die eingebaute Testwelt erzeugen — **erfunden, nicht abgegriffen**.

    python scripts/erzeuge_testwelt.py

Die Fixture ``src/chronicle/foundry/testwelt.json`` stammt aus diesem Skript und aus
nichts sonst. Kein Wert darin kommt aus einer echten Welt: keine Kennung, kein
Zeitstempel, kein Name, kein Welttitel. Was sie mit einer echten Welt teilt, ist allein
die **Form** — die Schlüssel, die Verschachtelung und die Kombinationen, die unsere
Strecke unterscheiden muss.

Warum überhaupt erzeugen und nicht anonymisieren? Eine pseudonymisierte Welt ist immer
noch die Welt einer echten, privaten Gruppe: der Konten-Graph, die
Berechtigungsverteilung und die Zeitstempel sagen, wer wann wie lange mit wem spielt.
Das gehört in kein veröffentlichtes Image. ``scripts/anonymisiere_welt.py`` bleibt für
den Fall, dass jemand seine **eigene** Welt lokal durchspielen will; eingecheckt ist die
erfundene.

**Wonach ist sie gebaut.** Ein echter Abzug wurde einmal ausgezählt — Schlüssel je Ebene,
Figurentypen, Verteilung der ``ownership``-Stufen, Rollen der Konten, welche Nachrichten
einen ``system.roll`` tragen, wie ein Daggerheart-Wurf mit ``hope``/``fear`` aussieht und
was in den eingebetteten JSON-Strings unter ``rolls[]`` steht. Diese *Auszählung* ist
hier eingeflossen, keine Zeile ihres Inhalts.

Die Formen, auf die es ankommt, und warum jede da ist:

- **Vier Rollenstufen** (Spieler, Vertraut, Assistenz, Leitung), damit ``is_gm`` an der
  Schwelle geprüft wird und nicht nur an ihren Enden.
- **Alle vier Berechtigungsstufen**, als ``default`` und je Konto: Figuren, die für das
  gewählte Konto verschwinden müssen (NONE), solche, die nur ihren Namen hergeben
  (LIMITED), und solche mit voller Sicht.
- **Drei Gruppen** an einer Instanz: was Gruppe B gehört, darf für ein Konto aus
  Gruppe A nicht auftauchen.
- **Nachrichten mit und ohne ``system.roll``**, dazu eine geflüsterte und eine blinde —
  das ist der Leitungs-Inhalt, den der Filter fallen lassen muss.
- **Würfe mit Hoffnung, mit Furcht und ein kritischer**, jeder doppelt abgelegt: einmal
  aufbereitet in ``system.roll``, einmal roh im JSON-String unter ``rolls[]``.
- **Karten mit und ohne ``navName``, genau eine aktive**, mit derselben Stufenmischung wie
  die Figuren: eine Szene, die nur die Leitung kennt, ist ein Vorgriff und muss
  verschwinden.

Das Skript ist deterministisch: derselbe Lauf schreibt dieselbe Datei. Genau das prüft
``tests/test_testwelt.py`` als Dauergate — die eingecheckte Fixture muss Zeichen für
Zeichen aus diesem Skript herausfallen, sonst ist sie irgendwo anders hergekommen.
"""

from __future__ import annotations

import json
import random
import string
from pathlib import Path

ZIEL = Path(__file__).resolve().parents[1] / "src" / "chronicle" / "foundry" / "testwelt.json"

SAATGUT = 58

# 2000-01-01 00:00:00 UTC. Ein Datum, an dem diese Gruppe nicht gespielt hat, weil es sie
# nicht gibt — die Uhrzeiten einer echten Runde sind Verhaltensdaten und stehen deshalb
# nirgends hier.
BASIS_MS = 946_684_800_000
MINUTE_MS = 60_000

# Foundry vergibt jedem Dokument eine 16-stellige Zufallskennung aus Buchstaben und
# Ziffern. Die hier sind aus demselben Alphabet gezogen, aber aus diesem Saatgut.
KENNUNGSLAENGE = 16

WELT_ID = "erfundene-testwelt"
# Die Kennung des Regelwerks ist das einzige, was der Adapter wirklich braucht: an ihr
# hängt, dass ``hope`` und ``fear`` als Würfel gelesen werden. Die Versionsnummern sind
# erfunden und zeigen bewusst auf keine echte Installation.
SYSTEM_ID = "daggerheart"
CORE_VERSION = "0.0.1"
SYSTEM_VERSION = "0.0.1"

LEITUNG, ASSISTENZ, VERTRAUT, SPIELER = 4, 3, 2, 1
ROLLEN = (LEITUNG, LEITUNG, ASSISTENZ) + (VERTRAUT, VERTRAUT) + (SPIELER,) * 14

# Wer mit wem spielt. Gruppe A ist die, aus deren Sicht die Testwelt gelesen wird; was B
# und C gehört, muss für sie unsichtbar bleiben.
GRUPPE_A = range(3, 9)
GRUPPE_B = range(9, 15)
GRUPPE_C = range(15, 19)

NONE, LIMITED, OBSERVER, OWNER = 0, 1, 2, 3

# Welche Karte gerade liegt. Es ist eine, die die Gruppe auch sehen darf und auf der die
# ersten Nachrichten fallen — eine aktive Szene hinter dem Berechtigungsfilter wäre eine
# Form, die es am Tisch nicht gibt.
AKTIVE_SZENE = 10

# Titel, Würfel und Modifikator je Wurf — von Hand gesetzt, damit alle drei Ausgänge
# vorkommen: Hoffnung überwiegt, Furcht überwiegt, und beide gleich ist ein Kritischer.
WUERFE = (
    ("Knowledge Roll", 3, 1, 3),
    ("Knowledge Roll", 9, 2, 1),
    ("Instinct Roll", 1, 8, 3),
    ("Instinct Roll", 4, 4, 1),
    ("Presence Roll", 9, 9, 2),
    ("Presence Roll", 2, 4, 1),
    ("Agility Roll", 12, 5, 1),
)

# Eine Nachricht mit Text bleibt eine mit Text — den Unterschied braucht die Strecke. Was
# hier steht, sagt zugleich, dass es nie ein gesprochener Satz war.
ERSATZTEXT = "[Erfundene Welt — hier hat nie jemand etwas gesagt.]"


def _kennungen(anzahl: int, rng: random.Random) -> list[str]:
    alphabet = string.ascii_letters + string.digits
    return ["".join(rng.choice(alphabet) for _ in range(KENNUNGSLAENGE)) for _ in range(anzahl)]


def _wuerfel(augen: int) -> dict:
    return {
        "dice": "d12",
        "total": augen,
        "formula": "1d12",
        "results": [{"result": augen, "active": True}],
        "rerolled": {"any": False, "rerolls": []},
    }


def _wurfblock(titel: str, hoffnung: int, furcht: int, modifikator: int) -> dict:
    """Der aufbereitete Wurf, so wie Foundry ihn in ``message.system.roll`` ablegt."""
    if hoffnung == furcht:
        dualitaet, etikett = 0, "Critical Success"
    elif hoffnung > furcht:
        dualitaet, etikett = 1, "Hope"
    else:
        dualitaet, etikett = -1, "Fear"
    return {
        "title": titel,
        "total": hoffnung + furcht + modifikator,
        "formula": f"1d12 + 1d12 + {modifikator}",
        "type": "action",
        "isCritical": hoffnung == furcht,
        "modifierTotal": modifikator,
        "advantage": {"type": 0},
        "dice": [_wuerfel(hoffnung), _wuerfel(furcht)],
        "hope": {"dice": "d12", "value": hoffnung, "rerolled": {"any": False, "rerolls": []}},
        "fear": {"dice": "d12", "value": furcht, "rerolled": {"any": False, "rerolls": []}},
        "result": {"duality": dualitaet, "total": hoffnung + furcht, "label": etikett},
    }


def _rolls_eintrag(block: dict, hoffnung: int, furcht: int, modifikator: int) -> str:
    """Derselbe Wurf ein zweites Mal, roh — ``rolls`` ist eine Liste von JSON-Strings."""

    def wuerfelterm(augen: int) -> dict:
        return {
            "class": "Die",
            "evaluated": True,
            "number": 1,
            "faces": 12,
            "modifiers": [],
            "results": [{"result": augen, "active": True}],
        }

    roh = {
        "class": "DualityRoll",
        "formula": block["formula"],
        "total": block["total"],
        "evaluated": True,
        "dice": [],
        "terms": [
            wuerfelterm(hoffnung),
            {"class": "OperatorTerm", "evaluated": True, "operator": "+"},
            wuerfelterm(furcht),
            {"class": "OperatorTerm", "evaluated": True, "operator": "+"},
            {"class": "NumericTerm", "evaluated": True, "number": modifikator},
        ],
        "options": {"title": block["title"], "roll": block},
    }
    return json.dumps(roh, ensure_ascii=False)


def _konten(kennungen: list[str]) -> list[dict]:
    return [
        {
            "_id": kennung,
            "role": rolle,
            "character": None,
            "name": f"Konto-{stelle + 1:02d}",
        }
        for stelle, (kennung, rolle) in enumerate(zip(kennungen, ROLLEN, strict=True))
    ]


def _figuren(konten: list[dict], figuren_ids: list[str]) -> list[dict]:
    """91 Figuren mit der Stufenmischung, an der sich der Berechtigungsfilter beweisen muss."""
    leitung, zweite_leitung = konten[0]["_id"], konten[1]["_id"]
    gruppen = [
        [konten[stelle]["_id"] for stelle in gruppe] for gruppe in (GRUPPE_A, GRUPPE_B, GRUPPE_C)
    ]

    figuren: list[dict] = []
    naechste = iter(figuren_ids)

    def anlegen(typ: str, ownership: dict) -> dict:
        figur = {
            "_id": next(naechste),
            "type": typ,
            "ownership": ownership,
            "name": f"Figur-{len(figuren) + 1:03d}",
        }
        figuren.append(figur)
        return figur

    # Spielfiguren: eine je Konto mit Gruppe, plus zwei ausgemusterte, die nur die Leitung
    # noch sieht.
    for gruppe in gruppen:
        for besitzer in gruppe:
            eigen = {mitspieler: OBSERVER for mitspieler in gruppe}
            anlegen("character", {"default": NONE, leitung: OWNER, **eigen, besitzer: OWNER})
    for _ in range(2):
        anlegen("character", {"default": NONE, leitung: OWNER})

    # Gegner: die vier Sichtbarkeiten, die es zu unterscheiden gilt.
    for _ in range(20):
        anlegen("adversary", {"default": LIMITED, leitung: OWNER})
    for _ in range(15):
        anlegen("adversary", {"default": NONE, leitung: OWNER})
    for _ in range(10):
        anlegen(
            "adversary",
            {"default": NONE, leitung: OWNER, **{konto: LIMITED for konto in gruppen[0]}},
        )
    for _ in range(10):
        anlegen(
            "adversary",
            {
                "default": NONE,
                leitung: OWNER,
                zweite_leitung: OWNER,
                **{konto: OBSERVER for konto in gruppen[1]},
            },
        )
    for _ in range(8):
        anlegen("adversary", {"default": OBSERVER, leitung: OWNER})

    # Begleiter hängen an je einer Spielfigur, das Rudel darf zusehen.
    for gruppe, anzahl in ((gruppen[0], 6), (gruppen[1], 1)):
        for besitzer in list(gruppe)[:anzahl]:
            mitspieler = {konto: LIMITED for konto in gruppe}
            anlegen("companion", {"default": NONE, leitung: OWNER, **mitspieler, besitzer: OWNER})

    # Der Gruppenbogen und die Umgebung, die alle sehen dürfen.
    for gruppe in gruppen[:2]:
        anlegen(
            "party",
            {"default": NONE, leitung: OWNER, **{konto: OBSERVER for konto in gruppe}},
        )
    anlegen("environment", {"default": OBSERVER, leitung: OWNER})
    return figuren


def _szenen(konten: list[dict], szenen_ids: list[str]) -> list[dict]:
    """32 Karten mit derselben Stufenmischung wie die Figuren — zehn kennt nur die Leitung.

    Genau eine ist ``active``: die, auf der die ersten Nachrichten fallen. Und ``navName``
    steht nur an jeder dritten, weil er im echten Abzug meistens leer ist — an dieser
    Mischung entscheidet sich, ob der Adapter den richtigen der beiden Namen nimmt.
    """
    leitung = konten[0]["_id"]
    gruppe_a = [konten[stelle]["_id"] for stelle in GRUPPE_A]
    gruppe_b = [konten[stelle]["_id"] for stelle in GRUPPE_B]
    stufen: list[dict] = []
    stufen += [{"default": NONE, leitung: OWNER}] * 10
    stufen += [{"default": NONE, leitung: OWNER, **{k: OBSERVER for k in gruppe_a}}] * 8
    stufen += [{"default": NONE, leitung: OWNER, **{k: OBSERVER for k in gruppe_b}}] * 8
    stufen += [{"default": OBSERVER, leitung: OWNER}] * 6
    return [
        {
            "_id": kennung,
            "active": stelle == AKTIVE_SZENE,
            "ownership": dict(ownership),
            "name": f"Szene-{stelle + 1:02d}",
            "navName": f"Ort-{stelle + 1:02d}" if stelle % 3 == 0 else "",
        }
        for stelle, (kennung, ownership) in enumerate(zip(szenen_ids, stufen, strict=True))
    ]


def _nachrichten(
    konten: list[dict], figuren: list[dict], szenen: list[dict], token: list[str]
) -> list[dict]:
    """Acht Nachrichten: eine geflüsterte ohne Wurf, eine blinde, sechs offene Würfe."""
    leitung = konten[0]
    spielende = [konten[stelle] for stelle in GRUPPE_A]
    # Die Spielfiguren der Gruppe A stehen am Anfang der Liste — je eine je Konto.
    eigene = figuren[: len(spielende)]
    buehne = [szenen[10], szenen[11]]

    gefluestert = {
        "_id": token[0],
        "timestamp": BASIS_MS,
        "author": leitung["_id"],
        "type": "base",
        "style": 0,
        "whisper": [leitung["_id"]],
        "blind": False,
        "content": ERSATZTEXT,
        "speaker": {"scene": None, "actor": None, "token": None, "alias": leitung["name"]},
    }

    nachrichten = [gefluestert]
    for stelle, (titel, hoffnung, furcht, modifikator) in enumerate(WUERFE):
        block = _wurfblock(titel, hoffnung, furcht, modifikator)
        # Der erste Wurf ist der verdeckte der Leitung; die übrigen wirft die Gruppe.
        blind = stelle == 0
        konto = leitung if blind else spielende[(stelle - 1) % len(spielende)]
        figur = eigene[(stelle - 1) % len(eigene)]
        szene = buehne[0] if stelle < 4 else buehne[1]
        nachrichten.append(
            {
                "_id": token[stelle + 1],
                "timestamp": BASIS_MS + (stelle + 5) * MINUTE_MS,
                "author": konto["_id"],
                "type": "dualityRoll",
                "style": 0,
                "whisper": [],
                "blind": blind,
                "content": "",
                "speaker": {
                    "scene": szene["_id"],
                    "actor": figur["_id"],
                    "token": token[stelle + 9],
                    "alias": figur["name"],
                },
                "system": {"roll": block},
                "rolls": [_rolls_eintrag(block, hoffnung, furcht, modifikator)],
            }
        )
    return nachrichten


def welt() -> dict:
    rng = random.Random(SAATGUT)
    konten_ids = _kennungen(len(ROLLEN), rng)
    figuren_ids = _kennungen(91, rng)
    szenen_ids = _kennungen(32, rng)
    token = _kennungen(24, rng)

    konten = _konten(konten_ids)
    figuren = _figuren(konten, figuren_ids)
    szenen = _szenen(konten, szenen_ids)
    # Wer eine Spielfigur hat, trägt sie auch am Konto — so wie Foundry es ablegt.
    for stelle, gruppe in enumerate((GRUPPE_A, GRUPPE_B, GRUPPE_C)):
        versatz = sum(len(vorige) for vorige in (GRUPPE_A, GRUPPE_B, GRUPPE_C)[:stelle])
        for platz, konto_stelle in enumerate(gruppe):
            konten[konto_stelle]["character"] = figuren[versatz + platz]["_id"]

    return {
        # Gelesen wird die Welt mit den Augen eines Kontos ohne Leitungsrechte — sonst
        # bewiese ein Filter, der nichts wegnimmt, gar nichts.
        "userId": konten[GRUPPE_A[0]]["_id"],
        "world": {
            "id": WELT_ID,
            "type": "world",
            "version": None,
            "coreVersion": CORE_VERSION,
            "systemVersion": SYSTEM_VERSION,
        },
        "system": {"id": SYSTEM_ID, "type": "system", "version": SYSTEM_VERSION},
        "users": konten,
        "actors": figuren,
        "messages": _nachrichten(konten, figuren, szenen, token),
        "scenes": szenen,
    }


def main() -> int:
    ZIEL.write_text(json.dumps(welt(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    erzeugt = welt()
    print(
        f"{ZIEL} geschrieben: {len(erzeugt['users'])} Konten, {len(erzeugt['actors'])} Figuren, "
        f"{len(erzeugt['messages'])} Nachrichten, {len(erzeugt['scenes'])} Szenen — "
        "erfunden, nicht abgegriffen."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
