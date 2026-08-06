"""Der Aufnahme-Bot: beitreten, ansagen, je Sprecher eine Spur schreiben."""

from __future__ import annotations


class BotFehler(RuntimeError):
    """Alles, was eine Aufnahme verhindert — gesagt wird es, still scheitert nichts."""
