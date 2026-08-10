"""Der Aufnahme-Bot: beitreten, ansagen, je Sprecher eine Spur schreiben."""

from __future__ import annotations


class BotFehler(RuntimeError):
    """Alles, was eine Aufnahme verhindert — gesagt wird es, still scheitert nichts."""


class BotHaelt(BotFehler):
    """Was kein Neustart heilt: der Bot sagt es einmal und bleibt dann aus.

    Ein fehlender Intent und ein abgelehnter Token sind keine Störungen, die vergehen.
    Bis ein Mensch etwas umlegt, endet **jeder** Versuch gleich — und ein Prozess, der
    es unter einem Neustart-Schalter trotzdem weiter versucht, verbindet sich in Minuten
    tausendfach. Am 2026-08-10 hat Discord genau deshalb unseren Token zurückgesetzt.

    Deshalb endet der Bot hier mit **0**: erfolgreich beendet, nicht abgestürzt. Nur so
    lässt ``Restart=on-failure`` ihn liegen, statt ihn in dieselbe Wand zu schicken.
    """
