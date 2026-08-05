"""Die Berechtigungsfilterung, die Foundry nicht macht.

Foundry schickt jedem angemeldeten Benutzer die komplette Welt und filtert erst im
Client beim Rendern. Hier ist die Stufe eines Dokuments ``max(ownership.default,
ownership[userId])``; eine Spielleitung sieht davon unabhängig alles.
"""

from __future__ import annotations

from collections.abc import Mapping

NONE = 0
LIMITED = 1
OBSERVER = 2
OWNER = 3

ASSISTANT_ROLE = 3

DEFAULT_KEY = "default"


def _level(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return NONE
    return min(max(value, NONE), OWNER)


def effective_level(ownership: object, user_id: str | None) -> int:
    if not isinstance(ownership, Mapping):
        return NONE
    stufen = [_level(ownership.get(DEFAULT_KEY))]
    if user_id is not None:
        stufen.append(_level(ownership.get(user_id)))
    return max(stufen)


def owner_ids(ownership: object) -> tuple[str, ...]:
    if not isinstance(ownership, Mapping):
        return ()
    return tuple(
        sorted(
            str(key)
            for key, value in ownership.items()
            if key != DEFAULT_KEY and _level(value) == OWNER
        )
    )


def is_gm(user: Mapping) -> bool:
    role = user.get("role")
    return isinstance(role, int) and not isinstance(role, bool) and role >= ASSISTANT_ROLE


def message_visible(message: Mapping, user_id: str, gm: bool) -> bool:
    """Geflüstertes und blinde Würfe sind GM-Inhalt, solange wir nicht selbst gemeint sind."""
    whisper = [str(eintrag) for eintrag in (message.get("whisper") or [])]
    if whisper and user_id not in whisper and str(message.get("author")) != user_id:
        return False
    return gm or not message.get("blind")
