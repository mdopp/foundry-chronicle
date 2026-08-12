"""Was der Instanz gehört und keiner Runde.

Fast alles in diesem System gehört einer Runde. Diese Werte nicht:

- **Der Discord-Bot-Token.** Das ist *unser* Token, mit dem *unser* Bot in fremde Gilden
  eingeladen wird — kein Geheimnis einer Gruppe. Er käme sonst pro Runde erneut zur
  Pflege, und eine Gruppe könnte den Bot der übrigen abmelden.
- **Ollama-Adresse und Modell** (#87). Wäre die Adresse runden-eigen, bestimmte eine
  fremde Gruppe, an welchen Rechner die Stimmen ihrer Mitspielenden gehen — das ist keine
  Einstellung, das ist eine Weitergabe. Und das Modell muss ohnehin auf der Box liegen;
  ein Name, den das Ollama dort nicht kennt, ist keine Wahl, sondern ein Lauf, der
  scheitert.
- **Die Verwaltungsgruppe.** Sie stammt aus der Benutzerverwaltung der Box und gilt für
  die Betreiber-Seite dieser Instanz — und nur für sie: Rechte über eine fremde Runde
  trägt sie nicht und bekommt sie auch nicht (#90).

Genau diese Werte sind der Grund, warum **eine kleine Betreiber-Seite bestehen bleibt**,
wenn #69 die übrige Oberfläche abräumt (Owner-Entscheidung 2026-08-11, #89): sie gehören
keiner Gilde und haben deshalb in Discord keinen Ort. ``/einstellungen`` ist auf sie
eingedampft; der Merker des abgeschlossenen Erststarts fiel mit dem Wizard, der ihn setzte.

Abgelegt wird in ``meta`` — der Schlüsselraum ohne Runde. ``settings`` daneben ist die
Tabelle **einer** Runde, und dass diese Werte dort nicht liegen, ist keine
Kleinigkeit: eine Gruppe soll den Token weder lesen noch überschreiben können.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from chronicle import db

# Die Konfigurationswerte, die der Instanz gehören — dieselben Namen wie in ``Config``.
KEYS = ("discord_bot_token", "ollama_url", "ollama_model")

ADMIN_GROUP_KEY = "admin_group"


def _lesen(database_path: Path, key: str) -> str | None:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        connection.close()
    return None if zeile is None else str(zeile["value"])


def _schreiben(database_path: Path, key: str, value: str | None) -> None:
    connection = db.connect(database_path)
    try:
        with connection:
            if value:
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            else:
                connection.execute("DELETE FROM meta WHERE key = ?", (key,))
    finally:
        connection.close()


def stored(database_path: Path) -> dict[str, str]:
    werte = {name: _lesen(database_path, name) for name in KEYS}
    return {name: wert for name, wert in werte.items() if wert and wert.strip()}


def save(database_path: Path, values: Mapping[str, str | None]) -> None:
    """Leerer Wert heißt: Eintrag weg, die Umgebung gilt wieder."""
    for name, wert in values.items():
        if name in KEYS:
            _schreiben(database_path, name, (wert or "").strip())


def admin_group(database_path: Path) -> str:
    return (_lesen(database_path, ADMIN_GROUP_KEY) or "").strip()


def save_admin_group(database_path: Path, value: str) -> None:
    """Ein leerer Name nimmt die Rolle zurück — dann darf wieder jeder alles."""
    _schreiben(database_path, ADMIN_GROUP_KEY, value.strip())
