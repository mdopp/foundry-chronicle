"""Die Personen-Zuordnung Discord ↔ Foundry.

Discord-Ids stehen nirgends in Foundry. Über die Namen lässt sich ein Vorschlag machen —
**bestätigt wird er von Hand**, genau einmal, danach steht die Zuordnung und die Frage
stellt sich nie wieder. Die Bestätigung ist der Punkt, nicht die Erkennung: ein
stillschweigend übernommener Vorschlag ordnete irgendwann Aussagen der falschen Person
zu, und das fiele im fertigen Protokoll niemandem mehr auf.

Gespeichert wird deshalb nur Bestätigtes. Vorschläge entstehen bei jedem Aufruf neu.

Die Namen kommen aus dem, was ohnehin liegt: die Discord-Anzeigenamen aus dem
Einwilligungsprotokoll, die Foundry-Spieler und ihre Figuren aus dem Zwischenspeicher.
Hier steht nur, welche Id zu welcher gehört.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher

from chronicle import db
from chronicle.runde import Runde

# Ab hier ist eine Namensähnlichkeit einen Vorschlag wert.
SCHWELLE = 0.8

# Liegen zwei Foundry-Spieler ähnlich nah, wird nichts vorgeschlagen: dann ist die Frage
# echt, und eine echte Frage gehört dem Menschen und nicht der Vorauswahl.
ABSTAND = 0.1


@dataclass(frozen=True)
class Spieler:
    id: str
    name: str
    characters: tuple[str, ...] = ()


@dataclass(frozen=True)
class Person:
    discord_user_id: str
    discord_name: str
    confirmed: Spieler | None = None
    suggestion: Spieler | None = None


@dataclass(frozen=True)
class Uebersicht:
    personen: tuple[Person, ...] = ()
    spieler: tuple[Spieler, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _spieler(scope: db.Scope) -> tuple[Spieler, ...]:
    figuren: dict[str, list[str]] = {}
    for zeile in scope.execute(
        "SELECT name, owner_ids FROM foundry_character WHERE runde_id = ? ORDER BY name",
        (scope.runde_id,),
    ):
        for besitzer in json.loads(zeile["owner_ids"]):
            figuren.setdefault(besitzer, []).append(zeile["name"])
    return tuple(
        Spieler(id=z["id"], name=z["name"], characters=tuple(figuren.get(z["id"], ())))
        for z in scope.execute(
            "SELECT id, name FROM foundry_player WHERE runde_id = ? ORDER BY name",
            (scope.runde_id,),
        )
    )


def _mitglieder(scope: db.Scope) -> tuple[tuple[str, str], ...]:
    # Der zuletzt protokollierte Anzeigename gewinnt — Discord-Namen ändern sich.
    zeilen = scope.execute(
        "SELECT user_id, name FROM consent_member WHERE runde_id = ? ORDER BY event_id",
        (scope.runde_id,),
    ).fetchall()
    neueste = {z["user_id"]: z["name"] for z in zeilen}
    return tuple(sorted(neueste.items(), key=lambda paar: paar[1].casefold()))


def _bestaetigt(scope: db.Scope) -> dict[str, str]:
    return {
        z["discord_user_id"]: z["foundry_user_id"]
        for z in scope.execute(
            "SELECT discord_user_id, foundry_user_id FROM person_mapping WHERE runde_id = ?",
            (scope.runde_id,),
        )
    }


def _stand(runde: Runde) -> tuple[tuple[Spieler, ...], dict[str, str], tuple]:
    scope = db.scoped(runde)
    try:
        return _spieler(scope), _bestaetigt(scope), _mitglieder(scope)
    finally:
        scope.close()


def _aehnlich(links: str, rechts: str) -> float:
    return SequenceMatcher(None, links.casefold().strip(), rechts.casefold().strip()).ratio()


def suggest(name: str, kandidaten: Sequence[Spieler]) -> Spieler | None:
    """Der eine naheliegende Foundry-Spieler zu einem Discord-Namen — oder keiner."""
    bewertet = sorted(
        ((_aehnlich(name, k.name), k) for k in kandidaten),
        key=lambda paar: paar[0],
        reverse=True,
    )
    if not bewertet or bewertet[0][0] < SCHWELLE:
        return None
    if len(bewertet) > 1 and bewertet[0][0] - bewertet[1][0] < ABSTAND:
        return None
    return bewertet[0][1]


def overview(runde: Runde) -> Uebersicht:
    """Wer aufgenommen wurde, wem er zugeordnet ist und was vorzuschlagen wäre."""
    spieler, bestaetigt, mitglieder = _stand(runde)
    nach_id = {s.id: s for s in spieler}
    vergeben = set(bestaetigt.values())
    frei = [s for s in spieler if s.id not in vergeben]
    personen = []
    for user_id, name in mitglieder:
        zugeordnet = nach_id.get(bestaetigt.get(user_id, ""))
        personen.append(
            Person(
                discord_user_id=user_id,
                discord_name=name,
                confirmed=zugeordnet,
                suggestion=None if zugeordnet else suggest(name, frei),
            )
        )
    return Uebersicht(personen=tuple(personen), spieler=spieler)


def speakers(runde: Runde) -> dict[str, Person]:
    """Je Discord-Id, wie eine Spur zu beschriften ist — ohne Vorschläge.

    Ein Vorschlag darf hier nicht auftauchen: an einer Spur stünde er wie eine Tatsache.
    """
    spieler, bestaetigt, mitglieder = _stand(runde)
    nach_id = {s.id: s for s in spieler}
    return {
        user_id: Person(
            discord_user_id=user_id,
            discord_name=name,
            confirmed=nach_id.get(bestaetigt.get(user_id, "")),
        )
        for user_id, name in mitglieder
    }


def confirm(runde: Runde, auswahl: Mapping[str, str]) -> None:
    """Schreibt fest, was ein Mensch ausgewählt hat; ein leerer Wert nimmt zurück."""
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        with scope:
            for user_id, foundry_user_id in auswahl.items():
                if not foundry_user_id:
                    scope.execute(
                        "DELETE FROM person_mapping WHERE runde_id = ? AND discord_user_id = ?",
                        (scope.runde_id, user_id),
                    )
                    continue
                scope.execute(
                    "INSERT INTO person_mapping "
                    "(runde_id, discord_user_id, foundry_user_id, confirmed_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (runde_id, discord_user_id) DO UPDATE SET "
                    "foundry_user_id = excluded.foundry_user_id, "
                    "confirmed_at = excluded.confirmed_at",
                    (scope.runde_id, user_id, foundry_user_id, zeitpunkt),
                )
    finally:
        scope.close()
