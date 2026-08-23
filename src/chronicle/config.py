"""Konfiguration aus der Umgebung — die Vorgabe, nicht das letzte Wort.

Die Umgebung ist der Deploy-Weg und der Stand beim ersten Start; die Werte für Foundry
lassen sich in Discord unter ``/setup`` überschreiben. Wer sie braucht, nimmt deshalb
``chronicle.settings.effective`` und nicht dieses Objekt direkt.

**Das Foundry-Passwort steht hier nicht.** Es gibt keine Umgebungsvariable dafür und kein
Feld: seit #64 lebt es allein im Arbeitsspeicher (``chronicle.zugang``). Eine Variable
wäre ein zweiter, dauerhafter Ort — sie steht in der Dienstbeschreibung der Box —, und in
einer Instanz mit mehreren Runden ginge das Passwort des Betreibers an *jeden*
Foundry-Server, den irgendeine Runde einträgt. Adresse und Benutzer bleiben Konfiguration.

Der Bot-Token verlässt dieses Objekt nie über ``repr``/``str`` — das ist der
wahrscheinlichste Weg in eine Logzeile. Die Ollama-Adresse ist dagegen Konfiguration und
kein Geheimnis; sie bleibt lesbar.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MASK = "***"

FOUNDRY_VARIABLES = ("FOUNDRY_URL", "FOUNDRY_USER")

# Dieselben Werte, wie sie in ``/setup`` heißen — mit Artikel, weil daraus ein Satz
# für jemanden gebaut wird, der nie eine Umgebungsvariable gesehen hat.
FOUNDRY_FELDER = ("die Adresse", "der Benutzer")

# **``CHRONICLE_REQUIRE_REMOTE_USER`` und ``CHRONICLE_TRUSTED_PROXIES`` standen hier bis
# #231.** Sie gehörten zum Türsteher der Betreiber-Seite (#190, ``chronicle.herkunft``):
# ``Remote-User`` und ``Remote-Groups`` waren nur zu glauben, wenn der Aufruf von dieser
# Maschine kam, denn der Port lag im Host-Netz und jeder im LAN konnte sich die Kopfzeile
# selbst schreiben. Die Seite ist fort, und mit ihr jede Kopfzeile, die zu prüfen wäre:
# der einzige Horcher dieses Dienstes ist ``/healthz`` auf der Schleife, und es liest
# keinen Header und beantwortet keine Frage nach Rechten. Ein Schalter, der nichts mehr
# schützt, ist keine Vorsicht, sondern eine falsche Zusage.

# Ob jeder Blick nach Foundry sein Rohmaterial mitschreibt (#242). Aus, und das ist keine
# Bequemlichkeit: ein Mitschnitt ist der Weltabzug in Serie — Klarnamen, Geflüster,
# GM-Inhalte — und was er anlegt, legt er dauerhaft an. Eingeschaltet wird er, wenn ein
# Fehler gegen einen echten Server untersucht wird; danach wieder aus.
MITSCHNITT_VARIABLE = "CHRONICLE_FOUNDRY_MITSCHNITT"

# Der Port, auf dem der Bot-Prozess das Install-Gate der Box bedient (#228). Ohne ihn
# bindet er nichts — gesetzt wird er von der Vorlage. Eine Adresse gibt es dazu nicht:
# gebunden wird die Schleife und sonst nichts (``chronicle.bot.healthz``). Seit #231 ist
# das der **einzige** Horcher dieses Dienstes.
HEALTH_PORT_VARIABLE = "CHRONICLE_HEALTH_PORT"

# Der Box-Standard: unser Pod läuft im Host-Netz, Ollama hört daneben auf 11434. Er steht
# hier und nirgends sonst — Einrichtung und Komposition fragen denselben Wert, sonst
# verspräche der Bot eine Adresse, gegen die der Lauf nicht redet.
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Derselbe Fall eine Tür weiter: der Sprachdienst der Box spricht die Einwilligungs-Ansage,
# im Host-Netz auf 8881. Anders als Ollama hat er keine eigene Template-Variable: dorthin
# fließt kein Wort der Runde, sondern allein unser eigener, feststehender Ansagetext, und
# wer die Vorgabe nicht erreicht, bekommt espeak-ng statt einer Fehlermeldung. Eine Frage
# nach einem Wert, dessen falsche Belegung nur die Stimme ändert, wäre eine zu viel.
DEFAULT_TTS_URL = "http://127.0.0.1:8881"

# Und noch eine: ``solaris-whisper-batch`` verschriftet die Spuren, im Host-Netz auf
# 10301 (#216, ``mdopp/solarisbay#1161``). Auch er bekommt keine eigene Variable — es gibt
# nichts zu wählen: welches Modell rechnet, entscheidet seine eigene Unit.
DEFAULT_WHISPER_URL = "http://127.0.0.1:10301"

DEFAULT_DATA_DIR = "data"

# Bewusst ein Geschwister von ``data`` und nicht darin: die SQLite geht ins Backup, die
# Audiospuren nie — sie sind das Einzige, was groß wird, und nach dem Lauf entbehrlich.
DEFAULT_RECORDINGS_DIR = "recordings"

DATABASE_NAME = "chronicle.sqlite3"


def masked(secret: str | None) -> str:
    return "None" if secret is None else f"'{MASK}'"


def _value(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def _port(env: Mapping[str, str], name: str) -> int | None:
    roh = _value(env, name)
    return None if roh is None else int(roh)


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, repr=False)
class Config:
    foundry_url: str | None = None
    foundry_user: str | None = None
    discord_bot_token: str | None = None
    discord_recap_channel: str | None = None
    ollama_url: str | None = None
    ollama_model: str | None = None
    tts_url: str | None = None
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    recordings_dir: Path = Path(DEFAULT_RECORDINGS_DIR)
    whisper_url: str | None = None
    health_port: int | None = None
    foundry_mitschnitt: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        return cls(
            foundry_url=_value(env, "FOUNDRY_URL"),
            foundry_user=_value(env, "FOUNDRY_USER"),
            discord_bot_token=_value(env, "DISCORD_BOT_TOKEN"),
            discord_recap_channel=_value(env, "DISCORD_RECAP_CHANNEL"),
            ollama_url=_value(env, "OLLAMA_URL"),
            ollama_model=_value(env, "OLLAMA_MODEL"),
            tts_url=_value(env, "TTS_URL"),
            data_dir=Path(_value(env, "CHRONICLE_DATA_DIR") or DEFAULT_DATA_DIR),
            recordings_dir=Path(_value(env, "CHRONICLE_RECORDINGS_DIR") or DEFAULT_RECORDINGS_DIR),
            whisper_url=_value(env, "CHRONICLE_WHISPER_URL"),
            health_port=_port(env, HEALTH_PORT_VARIABLE),
            foundry_mitschnitt=_flag(env, MITSCHNITT_VARIABLE),
        )

    @property
    def _foundry_values(self) -> tuple[str | None, ...]:
        return (self.foundry_url, self.foundry_user)

    @property
    def missing_foundry_variables(self) -> tuple[str, ...]:
        paare = zip(FOUNDRY_VARIABLES, self._foundry_values, strict=True)
        return tuple(name for name, value in paare if not value)

    @property
    def missing_foundry_fields(self) -> tuple[str, ...]:
        paare = zip(FOUNDRY_FELDER, self._foundry_values, strict=True)
        return tuple(name for name, value in paare if not value)

    @property
    def foundry_configured(self) -> bool:
        return not self.missing_foundry_variables

    @property
    def discord_configured(self) -> bool:
        return self.discord_bot_token is not None

    @property
    def ollama_configured(self) -> bool:
        # Die Adresse löst immer auf — ohne eigene ist es das Ollama dieser Box. Offen
        # bleibt allein die Modellwahl.
        return bool(self.ollama_model)

    @property
    def database_path(self) -> Path:
        return self.data_dir / DATABASE_NAME

    def __repr__(self) -> str:
        return (
            "Config("
            f"foundry_url={self.foundry_url!r}, "
            f"foundry_user={self.foundry_user!r}, "
            f"discord_bot_token={masked(self.discord_bot_token)}, "
            f"discord_recap_channel={self.discord_recap_channel!r}, "
            f"ollama_url={self.ollama_url!r}, "
            f"ollama_model={self.ollama_model!r}, "
            f"tts_url={self.tts_url!r}, "
            f"data_dir={str(self.data_dir)!r}, "
            f"recordings_dir={str(self.recordings_dir)!r}, "
            f"whisper_url={self.whisper_url!r}, "
            f"health_port={self.health_port!r}, "
            f"foundry_mitschnitt={self.foundry_mitschnitt!r})"
        )
