"""python -m chronicle.foundry — ein Abgleich, im Stapel.

Das Passwort wird gefragt, nicht gelesen: es steht in keiner Datei und in keiner
Umgebungsvariable, und als Aufrufargument stünde es in der Shell-History.
"""

from __future__ import annotations

import logging
import sys
from getpass import getpass

from chronicle import runde as runden
from chronicle.config import Config
from chronicle.foundry.service import sync

FRAGE = "Foundry-Passwort: "


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    zustand = sync(config, runden.erste(config.database_path), passwort=getpass(FRAGE))
    print(zustand.message)
    return 1 if zustand.stale else 0


if __name__ == "__main__":
    sys.exit(main())
