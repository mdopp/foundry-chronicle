"""Was der Instanz gehört und keiner Runde.

Fast alles in diesem System gehört einer Runde. Dieser Wert nicht:

- **Die Verwaltungsgruppe.** Sie stammt aus der Benutzerverwaltung der Box und gilt für
  die Betreiber-Seite dieser Instanz — und nur für sie: Rechte über eine fremde Runde
  trägt sie nicht und bekommt sie auch nicht (#90).

**Bot-Token, Ollama-Adresse und -Modell standen hier bis #230** und stehen es nicht mehr.
Sie kommen jetzt ausschließlich aus der Umgebung (``DISCORD_BOT_TOKEN``, ``OLLAMA_URL``,
``OLLAMA_MODEL``), gesetzt von den Template-Variablen der Box. Die Begründung von damals
— sie gehören keiner Gilde und haben deshalb in Discord keinen Ort — stimmt weiterhin;
sie begründet aber nur einen Ort *außerhalb* von Discord, und die Umgebung ist einer.
Was der Weg über die SQLite dagegen kostete, war ein Bot-Token im Klartext in der Datei,
und die geht ins Backup. Der Einwand gegen die Umgebung war #33: der Installations-
Assistent würfelte für ``type: secret`` einen Zufallswert. Das ServiceBay-Rezept
*rotate-a-service-secret* beantwortet ihn — ein übergebener Wert gewinnt und wird nicht
gewürfelt.

Abgelegt wird in ``meta`` — der Schlüsselraum ohne Runde. ``settings`` daneben ist die
Tabelle **einer** Runde.
"""

from __future__ import annotations

from pathlib import Path

from chronicle import db

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


def admin_group(database_path: Path) -> str:
    return (_lesen(database_path, ADMIN_GROUP_KEY) or "").strip()


def save_admin_group(database_path: Path, value: str) -> None:
    """Ein leerer Name nimmt die Rolle zurück — dann darf wieder jeder alles."""
    _schreiben(database_path, ADMIN_GROUP_KEY, value.strip())
