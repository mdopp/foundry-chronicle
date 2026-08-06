"""Die Werte, die in der Oberfläche gepflegt werden.

Die Umgebung ist die Vorgabe beim ersten Start und bleibt der Deploy-Weg; **ein in der
Oberfläche gesetzter Wert gewinnt.** Damit das keine zwei Wahrheiten werden, liest kein
Aufrufer diese Werte mehr selbst aus der Umgebung — er nimmt ``effective``.

Foundry-Passwort und Discord-Bot-Token liegen dadurch im Klartext in der SQLite. Aus
diesem Modul kommen sie nur dahin zurück, wo sie hingehören: in den Anmelde-Aufruf und
in den Authorization-Header. Für die Anzeige gibt es ``is_set`` — *ob* etwas gesetzt
ist, nie *was*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import time
from pathlib import Path

from chronicle import db
from chronicle.config import DEFAULT_OLLAMA_URL, Config

KEYS = (
    "foundry_url",
    "foundry_user",
    "foundry_password",
    "discord_bot_token",
    "discord_recap_channel",
    "ollama_url",
    "ollama_model",
)

SECRET_KEYS = ("foundry_password", "discord_bot_token")

# Steht bewusst nicht in KEYS: kein Konfigurationswert, sondern die Merkzeile des
# Erststart-Wizards — er soll nie ein zweites Mal aufgehen.
ONBOARDING_KEY = "onboarding_done"

# Ebenfalls nicht in KEYS: die Uhrzeit des nächtlichen Laufs gibt es nur hier. Sie über
# die Umgebung vorzugeben hieße, sie beim Deploy zu entscheiden — sie gehört aber der
# Gruppe, die weiß, wann ihr Server ungestört ist.
NIGHTLY_KEY = "nightly_time"
DEFAULT_NIGHTLY_TIME = "04:00"

# Auch nicht in KEYS: der Name der Gruppe, die verwalten darf. Er stammt aus der
# Benutzerverwaltung der Box und ist kein Deploy-Wert — leer heißt: alle dürfen alles.
ADMIN_GROUP_KEY = "admin_group"

FRONTEND = "Frontend"
UMGEBUNG = "Umgebung"
STANDARD = "Standard dieser Box"
UNGESETZT = "nicht gesetzt"


def stored(database_path: Path) -> dict[str, str]:
    connection = db.connect(database_path)
    try:
        zeilen = connection.execute("SELECT key, value FROM settings").fetchall()
    finally:
        connection.close()
    return {z["key"]: z["value"] for z in zeilen if z["key"] in KEYS and z["value"].strip()}


def effective(config: Config) -> Config:
    """Gespeichertes schlägt die Umgebung, die Umgebung schlägt den Box-Standard.

    Der Standard ist hier Verhalten und nicht bloß Formulartext: wer keine Adresse
    einträgt, bekommt das Ollama dieser Box — sonst versprächen die Einstellungen etwas,
    das der Lauf dann nicht tut.
    """
    zusammen = replace(config, **stored(config.database_path))
    if zusammen.ollama_url:
        return zusammen
    return replace(zusammen, ollama_url=DEFAULT_OLLAMA_URL)


def sources(config: Config) -> dict[str, str]:
    wirksam = effective(config)
    gespeichert = stored(config.database_path)
    return {
        name: FRONTEND
        if name in gespeichert
        else UMGEBUNG
        if getattr(config, name)
        else (STANDARD if getattr(wirksam, name) else UNGESETZT)
        for name in KEYS
    }


def is_set(config: Config, name: str) -> bool:
    return bool(getattr(effective(config), name))


def onboarding_done(database_path: Path) -> bool:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (ONBOARDING_KEY,)
        ).fetchone()
    finally:
        connection.close()
    return zeile is not None and zeile["value"] == "1"


def finish_onboarding(database_path: Path) -> None:
    connection = db.connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, '1') "
                "ON CONFLICT (key) DO UPDATE SET value = '1'",
                (ONBOARDING_KEY,),
            )
    finally:
        connection.close()


def admin_group(database_path: Path) -> str:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (ADMIN_GROUP_KEY,)
        ).fetchone()
    finally:
        connection.close()
    return "" if zeile is None else str(zeile["value"]).strip()


def save_admin_group(database_path: Path, value: str) -> None:
    """Ein leerer Name nimmt die Rolle zurück — dann darf wieder jeder alles."""
    sauber = value.strip()
    connection = db.connect(database_path)
    try:
        with connection:
            if sauber:
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (ADMIN_GROUP_KEY, sauber),
                )
            else:
                connection.execute("DELETE FROM settings WHERE key = ?", (ADMIN_GROUP_KEY,))
    finally:
        connection.close()


def nightly_at(value: str) -> time:
    """Die Uhrzeit hinter dem Wert — was sich nicht lesen lässt, ist die Vorgabe."""
    try:
        return time.fromisoformat(value)
    except ValueError:
        return time.fromisoformat(DEFAULT_NIGHTLY_TIME)


def nightly_time(database_path: Path) -> str:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (NIGHTLY_KEY,)
        ).fetchone()
    finally:
        connection.close()
    if zeile is None:
        return DEFAULT_NIGHTLY_TIME
    return nightly_at(str(zeile["value"])).strftime("%H:%M")


def save_nightly_time(database_path: Path, value: str) -> bool:
    """Speichert eine Uhrzeit; eine unlesbare lässt die bisherige stehen."""
    try:
        gewaehlt = time.fromisoformat(value.strip())
    except ValueError:
        return False
    connection = db.connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (NIGHTLY_KEY, gewaehlt.strftime("%H:%M")),
            )
    finally:
        connection.close()
    return True


def save(database_path: Path, values: Mapping[str, str | None]) -> None:
    """Leerer Wert heißt: Eintrag weg, die Umgebung gilt wieder.

    Ein Geheimnis, das unverändert bleiben soll, gehört deshalb gar nicht erst in
    ``values`` — sonst löscht ein leer abgesendetes Formularfeld es.
    """
    connection = db.connect(database_path)
    try:
        with connection:
            for name, wert in values.items():
                if name not in KEYS:
                    continue
                sauber = (wert or "").strip()
                if sauber:
                    connection.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (name, sauber),
                    )
                else:
                    connection.execute("DELETE FROM settings WHERE key = ?", (name,))
    finally:
        connection.close()
