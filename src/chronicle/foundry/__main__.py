"""python -m chronicle.foundry — ein Abgleich, im Stapel."""

from __future__ import annotations

import logging
import sys

from chronicle.config import Config
from chronicle.foundry.service import sync


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    zustand = sync(Config.from_env())
    print(zustand.message)
    return 1 if zustand.stale else 0


if __name__ == "__main__":
    sys.exit(main())
