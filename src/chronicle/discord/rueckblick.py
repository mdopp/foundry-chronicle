"""Den Rückblick dorthin stellen, wo die Gruppe ohnehin ist.

Der Rückblick wird unmittelbar vor der nächsten Sitzung gelesen — also gehört er in den
Gruppenkanal und nicht in eine Oberfläche, die jemand extra öffnen muss. Nicht in
``#diktat``: der ist der Briefkasten des Erzählenden, hier geht es in die Runde. Welcher
Kanal es ist, sagt eine Einstellung; **leer heißt: keine Zustellung.** Es gibt keinen
Sitzungskalender, also auch keinen Zeitpunkt, auf den sich zielen ließe.

Gepostet wird der abgelegte Rückblick, unverändert — als **Embed**: er ist kurz genug
dafür, und ein Embed hebt ihn im Kanalverlauf ab, statt ihn zwischen den Gesprächen des
Abends untergehen zu lassen. Der Rückblick ist per Konstruktion aus berechtigungs­
gefiltertem Material komponiert; daran vorbei wird nichts hineingereicht.

Seine eigenen Überschriften bleiben stehen, und sie sind der Punkt: »Offene Fäden —
Deutung des Modells, keine Fakten« steht im Embed genauso da wie im abgelegten Text. Wer
ihn vor der nächsten Sitzung liest, sieht damit, was belegt ist und was gedeutet.

**Eine Sitzung, eine Zustellung.** Der Zeitpunkt steht in ``protocol.delivered_at``; ein
zweiter Lauf sieht ihn und schweigt. Eine neu komponierte Fassung wird deshalb *nicht*
noch einmal gepostet: der Kanal ist die Zeitachse der Gruppe, ein zweiter Rückblick darin
läse sich wie eine zweite Sitzung. Die jeweils gültige Fassung hängt als Chronik-Datei im
Sitzungs-Thread — dort ist der Ort für Fassungen, siehe ``chronicle.discord.ausgabe``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from chronicle import db, settings
from chronicle.compose.service import RUECKBLICK
from chronicle.config import Config
from chronicle.discord.client import DiscordClient, DiscordError
from chronicle.discord.grenzen import EMBED_TEXT, EMBED_TITEL, gekappt
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

# Discords Maße für ein Embed stehen in ``chronicle.discord.grenzen``. Ein Rückblick liegt
# weit darunter — er ist auf zehn bis fünfzehn Sätze angelegt. Wird er trotzdem länger, ist
# das ein Fehler des Rückblicks und kein Grund, ihn auf mehrere Nachrichten zu verteilen:
# gekürzt wird er, und der Hinweis sagt, wo er ganz steht.
TITEL_GRENZE = EMBED_TITEL
TEXT_GRENZE = EMBED_TEXT

NICHT_EINGERICHTET = "Kein Bot-Token — der Rückblick bleibt in der Chronik."
KEIN_ZUSTELLKANAL = "Kein Zustellkanal eingetragen — der Rückblick bleibt in der Chronik."
KEIN_KANAL = "Kein Kanal #{kanal} — der Bot sieht ihn nicht, oder er heißt anders."
KEIN_RUECKBLICK = "Sitzung {sitzung} hat keinen Rückblick."
SCHON_ZUGESTELLT = "Rückblick zur Sitzung {sitzung} war schon zugestellt."
ZUGESTELLT = "Rückblick zur Sitzung {sitzung} nach #{kanal} zugestellt."
GESCHEITERT = "Rückblick zur Sitzung {sitzung} nicht zugestellt: {grund}"

GEKUERZT = "\n\n… hier gekürzt. Die ganze Sitzung steht in der Chronik-Datei im Thread."


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _protokoll(runde: Runde, session_id: int) -> tuple[str, str | None] | None:
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT text, delivered_at FROM protocol "
            "WHERE runde_id = ? AND session_id = ? AND kind = ?",
            (scope.runde_id, session_id, RUECKBLICK),
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else (str(zeile["text"]), zeile["delivered_at"])


def _merken(runde: Runde, session_id: int, at: str) -> None:
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "UPDATE protocol SET delivered_at = ? "
                "WHERE runde_id = ? AND session_id = ? AND kind = ?",
                (at, scope.runde_id, session_id, RUECKBLICK),
            )
    finally:
        scope.close()


def embed(text: str) -> dict[str, str]:
    """Der abgelegte Rückblick als Embed: die Titelzeile als Titel, der Rest darunter.

    Umgeschrieben wird nichts. Die Überschriften des Rückblicks tragen die Trennung
    zwischen Belegtem und Gedeutetem, und Discord stellt sie genauso dar — eine zweite
    Auszeichnung wäre eine zweite Wahrheit.
    """
    zeilen = text.strip().splitlines()
    titel = ""
    if zeilen and zeilen[0].startswith("# "):
        titel = zeilen[0][2:].strip()
        zeilen = zeilen[1:]
    rumpf = "\n".join(zeilen).strip()
    if len(rumpf) > TEXT_GRENZE:
        logger.warning(
            "Rückblick ist %s Zeichen lang, ein Embed nimmt %s — gekürzt zugestellt.",
            len(rumpf),
            TEXT_GRENZE,
        )
    gebaut = {"description": gekappt(rumpf, TEXT_GRENZE, GEKUERZT)}
    if titel:
        gebaut["title"] = gekappt(titel, TITEL_GRENZE)
    return gebaut


def deliver(
    config: Config, runde: Runde, session_id: int, *, client: DiscordClient | None = None
) -> str:
    """Stellt den Rückblick dieser Sitzung zu, wenn er noch nicht zugestellt ist."""
    zugang = settings.effective(config, runde)
    if not zugang.discord_configured:
        return NICHT_EINGERICHTET
    kanalname = (zugang.discord_recap_channel or "").strip().lstrip("#")
    if not kanalname:
        return KEIN_ZUSTELLKANAL

    abgelegt = _protokoll(runde, session_id)
    if abgelegt is None:
        return KEIN_RUECKBLICK.format(sitzung=session_id)
    text, zugestellt = abgelegt
    if zugestellt is not None:
        return SCHON_ZUGESTELLT.format(sitzung=session_id)

    bot = client if client is not None else DiscordClient(zugang)
    try:
        kanal = bot.channel_id(kanalname)
        if kanal is None:
            return KEIN_KANAL.format(kanal=kanalname)
        bot.post_embed(kanal, embed(text))
    except DiscordError as fehler:
        # Der Rückblick steht bereits in der Datenbank; ein Discord, das gerade nicht
        # antwortet, macht daraus keinen fehlgeschlagenen Stapellauf. ``delivered_at``
        # bleibt leer, der nächste Lauf holt die Zustellung nach.
        return GESCHEITERT.format(sitzung=session_id, grund=fehler)
    # Erst posten, dann merken: ein fehlgeschlagener Post soll wiederholt werden. Die
    # Lücke dazwischen ist ein Prozessabbruch zwischen HTTP-200 und einem lokalen UPDATE.
    _merken(runde, session_id, _now())
    return ZUGESTELLT.format(sitzung=session_id, kanal=kanalname)
