"""Konfiguration aus der Umgebung — die Vorgabe, nicht das letzte Wort.

Die Umgebung ist der Deploy-Weg und der Stand beim ersten Start; die fünf Werte für
Foundry und Ollama lassen sich in der Oberfläche überschreiben. Wer sie braucht, nimmt
deshalb ``chronicle.settings.effective`` und nicht dieses Objekt direkt.

Foundry kennt keinen API-Token: der Zugang ist Benutzer und Passwort eines echten
Kontos (siehe docs/foundry-zugriff.md). Passwort und Bot-Token verlassen dieses
Objekt nie über ``repr``/``str`` — das ist der wahrscheinlichste Weg in eine Logzeile.
Die Ollama-Adresse ist dagegen Konfiguration und kein Geheimnis; sie bleibt lesbar.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MASK = "***"

FOUNDRY_VARIABLES = ("FOUNDRY_URL", "FOUNDRY_USER", "FOUNDRY_PASSWORD")

OLLAMA_VARIABLES = ("OLLAMA_URL", "OLLAMA_MODEL")

REMOTE_USER_VARIABLE = "CHRONICLE_REQUIRE_REMOTE_USER"

DEFAULT_DATA_DIR = "data"

# Bewusst ein Geschwister von ``data`` und nicht darin: die SQLite geht ins Backup, die
# Audiospuren nie — sie sind das Einzige, was groß wird, und nach dem Lauf entbehrlich.
DEFAULT_RECORDINGS_DIR = "recordings"

DATABASE_NAME = "chronicle.sqlite3"

# Der Kompromiss aus Erkennung und Laufzeit auf CPU. Größer geht über die Umgebung;
# ein Feld in der Oberfläche braucht es dafür nicht — der Wert wird einmal gesetzt.
DEFAULT_WHISPER_MODEL = "small"


def masked(secret: str | None) -> str:
    return "None" if secret is None else f"'{MASK}'"


def _value(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, repr=False)
class Config:
    foundry_url: str | None = None
    foundry_user: str | None = None
    foundry_password: str | None = None
    discord_bot_token: str | None = None
    discord_recap_channel: str | None = None
    ollama_url: str | None = None
    ollama_model: str | None = None
    public_url: str | None = None
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    recordings_dir: Path = Path(DEFAULT_RECORDINGS_DIR)
    whisper_model: str = DEFAULT_WHISPER_MODEL
    require_remote_user: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        return cls(
            foundry_url=_value(env, "FOUNDRY_URL"),
            foundry_user=_value(env, "FOUNDRY_USER"),
            foundry_password=_value(env, "FOUNDRY_PASSWORD"),
            discord_bot_token=_value(env, "DISCORD_BOT_TOKEN"),
            discord_recap_channel=_value(env, "DISCORD_RECAP_CHANNEL"),
            ollama_url=_value(env, "OLLAMA_URL"),
            ollama_model=_value(env, "OLLAMA_MODEL"),
            public_url=_value(env, "CHRONICLE_PUBLIC_URL"),
            data_dir=Path(_value(env, "CHRONICLE_DATA_DIR") or DEFAULT_DATA_DIR),
            recordings_dir=Path(_value(env, "CHRONICLE_RECORDINGS_DIR") or DEFAULT_RECORDINGS_DIR),
            whisper_model=_value(env, "CHRONICLE_WHISPER_MODEL") or DEFAULT_WHISPER_MODEL,
            require_remote_user=_flag(env, REMOTE_USER_VARIABLE),
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
    def missing_ollama_variables(self) -> tuple[str, ...]:
        paare = zip(OLLAMA_VARIABLES, (self.ollama_url, self.ollama_model), strict=True)
        return tuple(name for name, value in paare if not value)

    @property
    def ollama_configured(self) -> bool:
        return not self.missing_ollama_variables

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
            f"discord_recap_channel={self.discord_recap_channel!r}, "
            f"ollama_url={self.ollama_url!r}, "
            f"ollama_model={self.ollama_model!r}, "
            f"public_url={self.public_url!r}, "
            f"data_dir={str(self.data_dir)!r}, "
            f"recordings_dir={str(self.recordings_dir)!r}, "
            f"whisper_model={self.whisper_model!r}, "
            f"require_remote_user={self.require_remote_user!r})"
        )
