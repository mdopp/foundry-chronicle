"""Das gemeinsame Modell hinter dem Adapter.

Ab hier weiß nichts mehr, welches Regelwerk gespielt wurde: ein Wurf ist ein Titel, eine
Summe, eine Formel und eine Handvoll benannter Würfel.
"""

from __future__ import annotations

from dataclasses import dataclass

# Der Herkunftsvermerk einer archivierten Nachricht. Er steht hier und nicht dort, wo er
# angezeigt wird: Speicher, Abgleichsmeldung und Chronik sagen denselben Satz.
NICHT_MEHR_VORHANDEN = "nicht mehr in Foundry vorhanden"


@dataclass(frozen=True)
class Die:
    name: str
    faces: str
    value: int


@dataclass(frozen=True)
class Roll:
    title: str | None = None
    total: int | None = None
    formula: str | None = None
    kind: str | None = None
    critical: bool = False
    modifier_total: int | None = None
    dice: tuple[Die, ...] = ()


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    role: int
    is_gm: bool


# Der Aktor-Typ einer **Spielfigur**. Foundry führt in derselben Liste auch alles, was die
# Spielleitung besitzt — Gegner, Wirtinnen, Wachen —, und deren Namen sind keine Namen von
# Mitspielenden. Der Wert ist Foundry-Kern und nicht Regelwerk: 5e, PF2e und Daggerheart
# schreiben ihn gleich, unterschiedlich sind nur die Typen daneben.
SPIELFIGUR = "character"


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    type: str | None = None
    owner_ids: tuple[str, ...] = ()
    limited: bool = False


@dataclass(frozen=True)
class ChatMessage:
    """Ein Ereignis, kein Zustand — es wird archiviert, nicht gespiegelt.

    ``vanished_at`` trägt den Zeitpunkt des Abgleichs, der diese Nachricht in Foundry
    nicht mehr fand; leer heißt vorhanden. Frisch aus dem Rohdump projiziert ist er immer
    leer, den Vermerk kennt nur der Speicher.
    """

    id: str
    timestamp: int
    speaker_actor: str | None = None
    speaker_alias: str | None = None
    content: str = ""
    roll: Roll | None = None
    vanished_at: str | None = None


@dataclass(frozen=True)
class Scene:
    """Eine Karte der Welt — für die Chronik ist sie ein Ortsname, sonst nichts.

    Das Karten-Innenleben (Wände, Lichter, Kacheln, Token) bleibt in Foundry: die Chronik
    trägt es nicht, und was nicht gespeichert wird, kann auch nicht auslaufen. ``name`` ist
    ``navName``, wenn die Spielleitung einen vergeben hat, sonst ``name`` — die Leiste über
    der Karte ist das, was die Gruppe am Tisch gesehen hat.
    """

    id: str
    name: str
    active: bool = False


@dataclass(frozen=True)
class World:
    """Welche Welt der Server gerade zeigt.

    Ein Foundry-Server hostet immer nur **eine** Welt. Wer mehrere Runden auf einem Server
    fährt, wechselt sie — und ein Abgleich zöge sonst das Chat-Log der falschen Kampagne in
    die falsche Chronik. Die Kennung steht deshalb bei der Runde und wird bei jedem
    Abgleich verglichen.
    """

    id: str
    title: str


@dataclass(frozen=True)
class WorldSnapshot:
    system: str
    fetched_at: str
    players: tuple[Player, ...] = ()
    characters: tuple[Character, ...] = ()
    messages: tuple[ChatMessage, ...] = ()
    scenes: tuple[Scene, ...] = ()


@dataclass(frozen=True)
class SyncState:
    message: str
    stale: bool = False
    snapshot: WorldSnapshot | None = None
