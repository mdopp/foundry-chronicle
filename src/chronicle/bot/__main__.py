"""python -m chronicle.bot — der Aufnahme-Bot, ein eigener, dauerhafter Prozess.

Anders als ``python -m chronicle.discord`` ist das kein Stapellauf: der Bot hält eine
Gateway-Verbindung, weil Sprache nur mitgeschnitten werden kann, während sie gesprochen
wird. Er läuft deshalb neben dem Webdienst, nicht in ihm.

Ohne Token startet er nicht und sagt das in einem Satz — der Token wird unter
``/einstellungen`` gepflegt und ein dort gesetzter Wert schlägt die Umgebung.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from chronicle import db, settings
from chronicle.bot import BotFehler
from chronicle.config import Config

KEIN_TOKEN = (
    "Kein Discord-Bot-Token — der Aufnahme-Bot startet nicht. "
    "Token unter /einstellungen setzen, dann den Bot neu starten."
)


def _gateway() -> Callable[[Config], None]:
    from chronicle.bot.gateway import run

    return run


def main(argv: list[str] | None = None, *, gateway: Callable[[], Callable] = _gateway) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    db.init(config.database_path)
    zugang = settings.effective(config)
    if not zugang.discord_configured:
        print(KEIN_TOKEN)
        return 0
    try:
        gateway()(zugang)
    except BotFehler as fehler:
        print(str(fehler))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
