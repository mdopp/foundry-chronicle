"""Das gemeinsame Modell hinter dem Adapter.

Ab hier weiß nichts mehr, welches Regelwerk gespielt wurde: ein Wurf ist ein Titel, eine
Summe, eine Formel und eine Handvoll benannter Würfel.
"""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    type: str | None = None
    owner_ids: tuple[str, ...] = ()
    limited: bool = False


@dataclass(frozen=True)
class ChatMessage:
    id: str
    timestamp: int
    speaker_actor: str | None = None
    speaker_alias: str | None = None
    content: str = ""
    roll: Roll | None = None


@dataclass(frozen=True)
class WorldSnapshot:
    system: str
    fetched_at: str
    players: tuple[Player, ...] = ()
    characters: tuple[Character, ...] = ()
    messages: tuple[ChatMessage, ...] = ()


@dataclass(frozen=True)
class SyncState:
    message: str
    stale: bool = False
    snapshot: WorldSnapshot | None = None
