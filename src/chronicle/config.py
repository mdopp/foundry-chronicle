"""Konfiguration aus der Umgebung — die Vorgabe, nicht das letzte Wort.

Die Umgebung ist der Deploy-Weg und der Stand beim ersten Start; die Werte für Foundry
lassen sich in Discord unter ``/chronicle setup`` überschreiben. Wer sie braucht, nimmt deshalb
``chronicle.settings.effective`` und nicht dieses Objekt direkt.

**Das Foundry-Passwort steht hier nicht.** Es gibt keine Umgebungsvariable dafür und kein
Feld: seit #64 lebt es allein im Arbeitsspeicher (``chronicle.zugang``). Eine Variable
wäre ein zweiter, dauerhafter Ort — sie steht in der Dienstbeschreibung der Box —, und in
einer Instanz mit mehreren Runden ginge das Passwort des Betreibers an *jeden*
Foundry-Server, den irgendeine Runde einträgt. Adresse und Benutzer bleiben Konfiguration.

Der Bot-Token und die beiden Schlüssel (Modell, Erkenner) verlassen dieses Objekt nie über
``repr``/``str`` — das ist der wahrscheinlichste Weg in eine Logzeile. Die Adressen sind
dagegen Konfiguration und kein Geheimnis; sie bleiben lesbar.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MASK = "***"

FOUNDRY_VARIABLES = ("FOUNDRY_URL", "FOUNDRY_USER")

# Dieselben Werte, wie sie in ``/chronicle setup`` heißen — mit Artikel, weil daraus ein Satz
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

# **Der Modelldienst kommt von Laib** (#362). Adresse, Schlüssel und Lease sind dort
# eingebaute Werte, die jeder Dienst in seinen Units bekommt — ``${MODEL_URL}``,
# ``${MODEL_KEY}``, ``${MODEL_LEASE}`` (laib ``internal/state/load.go``) — und die Vorlage
# reicht sie unter denselben Namen herein. Eine Vorgabe gibt es hier deshalb nicht mehr:
# die Box kennt ihren Modelldienst, dieser Prozess nicht. Ohne Adresse schreibt niemand,
# die Chronik entsteht geordnet statt formuliert.
#
# **Bis #362 standen hier zwei Adressen mit ``127.0.0.1``**: ``llama-server`` auf 11435
# und der Nachbar ``solaris`` auf 8787, der das Sitzungsfenster vergab. Beide setzten
# das Host-Netz voraus (#165); auf Laib läuft dieser Dienst in seinem eigenen
# Netz-Namensraum, und die Box nennt die Adresse, unter der er den Modelldienst erreicht.
MODEL_URL_VARIABLE = "MODEL_URL"
MODEL_KEY_VARIABLE = "MODEL_KEY"
MODEL_LEASE_VARIABLE = "MODEL_LEASE"

# **Welches Modell, nicht welches Fenster** (#362, laib ADR 0011, Ergänzung 2026-10-14).
# Ein Preset wie ``gemma-4-12b``; das engste Fenster dafür wählt die Lease selbst. Es
# steht in einer Template-Variablen und nirgends als Konstante: ein Fenstername im Code
# wäre genau die Kopplung an den Nachbarn, die der Vertrag abschafft (``foundry`` war
# einer, und Laib weist ihn ab).
MODEL_PRESET_VARIABLE = "MODEL_PRESET"

# Der Sprachdienst spricht die Einwilligungs-Ansage: **Piper aus Laibs Stimme** (#362),
# über das Wyoming-Protokoll. Die Vorlage nennt die Adresse; ohne sie gilt die Schleife
# dieser Maschine. Dorthin fließt kein Wort der Runde, sondern allein unser eigener,
# feststehender Ansagetext, und wer ihn nicht erreicht, bekommt espeak-ng statt einer
# Fehlermeldung.
DEFAULT_TTS_URL = "tcp://127.0.0.1:10200"

# Der Spracherkenner für die Spuren (#216): seit #362 der eigene Container dieses Dienstes
# (``chronik-whisper``), nicht mehr ``solaris-whisper-batch``. Es gibt nichts zu wählen —
# welches Modell rechnet, entscheidet seine Unit; die Vorlage nennt die Adresse.
DEFAULT_WHISPER_URL = "http://127.0.0.1:10301"

# Der Schlüssel, den der Erkenner verlangt (#362). Er ist ein Geheimnis dieses Dienstes,
# das Laib beim ersten Apply erzeugt und beiden Containern als podman-Secret gibt: der
# Erkenner liest die Spuren der Runden, und ein Port, den jeder Pod der Box erreicht,
# braucht dann eine Tür.
WHISPER_KEY_VARIABLE = "CHRONICLE_WHISPER_KEY"

# Ob das Sitzungsfenster überhaupt angemeldet wird (#299). An, weil der Vertrag mit der
# Lease der Box der Normalfall ist. Der Schalter steht daneben, damit der Betreiber ihn
# ohne Neubau verlassen kann — aus heißt: kein Aufruf geht hinaus, und die Box behält ihr
# Haushaltsmodell auf der Karte.
GPU_LEASE_VARIABLE = "CHRONICLE_GPU_LEASE"

# **``CHRONICLE_LLM_BACKEND`` stand hier bis #329.** Mit #316 wählte es zwischen Ollamas
# ``/api/chat`` und dem OpenAI-förmigen ``/v1/chat/completions`` des Ablösers, damit der
# Umzug eine Einstellung sein konnte und kein Neubau; seit #335 stand die Vorgabe auf
# ``openai``. Die Box hat Ollama abgeschaltet (``mdopp/solarisbay#1332``, gemessen am
# 2026-09-06), und damit ist der zweite Wert weggefallen. Ein Schalter mit genau einem
# gültigen Wert bleibt nicht als Höflichkeit stehen: wer ``ollama`` in das Textfeld tippt,
# bekäme still ``openai`` — dieselbe falsche Zusage, wegen der oben
# ``CHRONICLE_REQUIRE_REMOTE_USER`` gefallen ist.

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


def _schalter(env: Mapping[str, str], name: str, *, vorgabe: bool) -> bool:
    """Wie ``_flag``, nur für einen Schalter, dessen Vorgabe **an** ist.

    Leer heißt hier nicht »aus«, sondern »unverändert« — sonst schaltete jede Box, die die
    Variable nicht kennt, den Vertrag stillschweigend ab.
    """
    roh = (env.get(name) or "").strip().lower()
    return vorgabe if not roh else roh in ("1", "true", "yes", "on", "an")


@dataclass(frozen=True, repr=False)
class Config:
    foundry_url: str | None = None
    foundry_user: str | None = None
    discord_bot_token: str | None = None
    discord_recap_channel: str | None = None
    model_url: str | None = None
    model_preset: str | None = None
    model_key: str | None = None
    model_lease: str | None = None
    tts_url: str | None = None
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    recordings_dir: Path = Path(DEFAULT_RECORDINGS_DIR)
    whisper_url: str | None = None
    whisper_key: str | None = None
    health_port: int | None = None
    foundry_mitschnitt: bool = False
    gpu_lease: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        return cls(
            foundry_url=_value(env, "FOUNDRY_URL"),
            foundry_user=_value(env, "FOUNDRY_USER"),
            discord_bot_token=_value(env, "DISCORD_BOT_TOKEN"),
            discord_recap_channel=_value(env, "DISCORD_RECAP_CHANNEL"),
            model_url=_value(env, MODEL_URL_VARIABLE),
            model_preset=_value(env, MODEL_PRESET_VARIABLE),
            model_key=_value(env, MODEL_KEY_VARIABLE),
            model_lease=_value(env, MODEL_LEASE_VARIABLE),
            tts_url=_value(env, "TTS_URL"),
            data_dir=Path(_value(env, "CHRONICLE_DATA_DIR") or DEFAULT_DATA_DIR),
            recordings_dir=Path(_value(env, "CHRONICLE_RECORDINGS_DIR") or DEFAULT_RECORDINGS_DIR),
            whisper_url=_value(env, "CHRONICLE_WHISPER_URL"),
            whisper_key=_value(env, WHISPER_KEY_VARIABLE),
            health_port=_port(env, HEALTH_PORT_VARIABLE),
            foundry_mitschnitt=_flag(env, MITSCHNITT_VARIABLE),
            gpu_lease=_schalter(env, GPU_LEASE_VARIABLE, vorgabe=True),
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
    def model_configured(self) -> bool:
        # Beides nennt die Box: wo der Modelldienst steht und welches Preset wir wollen.
        # Fehlt eines, schreibt niemand — eine geratene Adresse wäre eine Behauptung über
        # eine Box, die dieser Prozess nicht kennt.
        return bool(self.model_url and self.model_preset)

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
            f"model_url={self.model_url!r}, "
            f"model_preset={self.model_preset!r}, "
            f"model_key={masked(self.model_key)}, "
            f"model_lease={self.model_lease!r}, "
            f"tts_url={self.tts_url!r}, "
            f"data_dir={str(self.data_dir)!r}, "
            f"recordings_dir={str(self.recordings_dir)!r}, "
            f"whisper_url={self.whisper_url!r}, "
            f"whisper_key={masked(self.whisper_key)}, "
            f"health_port={self.health_port!r}, "
            f"foundry_mitschnitt={self.foundry_mitschnitt!r}, "
            f"gpu_lease={self.gpu_lease!r})"
        )
