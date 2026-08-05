"""Konfiguration — ausschließlich aus der Umgebung.

Foundry kennt keinen API-Token: der Zugang ist Benutzer und Passwort eines echten
Kontos (siehe docs/foundry-zugriff.md). Passwort und Bot-Token verlassen dieses
Objekt nie über ``repr``/``str`` — das ist der wahrscheinlichste Weg in eine Logzeile.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MASK = "***"

FOUNDRY_VARIABLES = ("FOUNDRY_URL", "FOUNDRY_USER", "FOUNDRY_PASSWORD")

DEFAULT_DATA_DIR = "data"

DATABASE_NAME = "chronicle.sqlite3"


def masked(secret: str | None) -> str:
    return "None" if secret is None else f"'{MASK}'"


def _value(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


@dataclass(frozen=True, repr=False)
class Config:
    foundry_url: str | None = None
    foundry_user: str | None = None
    foundry_password: str | None = None
    discord_bot_token: str | None = None
    data_dir: Path = Path(DEFAULT_DATA_DIR)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        return cls(
            foundry_url=_value(env, "FOUNDRY_URL"),
            foundry_user=_value(env, "FOUNDRY_USER"),
            foundry_password=_value(env, "FOUNDRY_PASSWORD"),
            discord_bot_token=_value(env, "DISCORD_BOT_TOKEN"),
            data_dir=Path(_value(env, "CHRONICLE_DATA_DIR") or DEFAULT_DATA_DIR),
        )

    @property
    def missing_foundry_variables(self) -> tuple[str, ...]:
        values = (self.foundry_url, self.foundry_user, self.foundry_password)
        paare = zip(FOUNDRY_VARIABLES, values, strict=True)
        return tuple(name for name, value in paare if not value)

    @property
    def foundry_configured(self) -> bool:
        return not self.missing_foundry_variables

    @property
    def discord_configured(self) -> bool:
        return self.discord_bot_token is not None

    @property
    def database_path(self) -> Path:
        return self.data_dir / DATABASE_NAME

    def __repr__(self) -> str:
        return (
            "Config("
            f"foundry_url={self.foundry_url!r}, "
            f"foundry_user={self.foundry_user!r}, "
            f"foundry_password={masked(self.foundry_password)}, "
            f"discord_bot_token={masked(self.discord_bot_token)}, "
            f"data_dir={str(self.data_dir)!r})"
        )
