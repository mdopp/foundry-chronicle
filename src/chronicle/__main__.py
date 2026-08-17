"""python -m chronicle — der Entwicklungsserver."""

from __future__ import annotations

import os

from chronicle.app import create_app


def main() -> None:
    create_app().run(
        host=os.environ.get("CHRONICLE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CHRONICLE_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
