"""Den Rückblick dorthin stellen, wo die Gruppe ohnehin ist.

Der Rückblick wird unmittelbar vor der nächsten Sitzung gelesen — also gehört er in den
Gruppenkanal und nicht in eine Oberfläche, die jemand extra öffnen muss. Nicht in
``#diktat``: der ist der Briefkasten des Erzählenden, hier geht es in die Runde. Welcher
Kanal es ist, sagt eine Einstellung; **leer heißt: keine Zustellung.** Es gibt keinen
Sitzungskalender, also auch keinen Zeitpunkt, auf den sich zielen ließe.

Gepostet wird der abgelegte Rückblick, unverändert. Er ist per Konstruktion aus
berechtigungsgefiltertem Material komponiert — daran vorbei wird nichts hineingereicht.

**Eine Sitzung, eine Zustellung.** Der Zeitpunkt steht in ``protocol.delivered_at``; ein
zweiter Lauf sieht ihn und schweigt. Eine neu komponierte Fassung wird deshalb *nicht*
noch einmal gepostet: der Kanal ist die Zeitachse der Gruppe, ein zweiter Rückblick darin
läse sich wie eine zweite Sitzung. Die jeweils gültige Fassung steht in der Chronik, und
darauf zeigt der Link.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from chronicle import db, settings
from chronicle.compose.service import RUECKBLICK
from chronicle.config import Config
from chronicle.discord.client import DiscordClient, DiscordError
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

# Discord kappt eine Nachricht bei 2000 Zeichen. Ein Rückblick, der darüber liegt, ist ein
# Fehler des Rückblicks und kein Grund zum Aufteilen — er wird gekürzt und verlinkt.
GRENZE = 2000

NICHT_EINGERICHTET = "Kein Bot-Token — der Rückblick bleibt in der Chronik."
KEIN_ZUSTELLKANAL = "Kein Zustellkanal eingetragen — der Rückblick bleibt in der Chronik."
KEIN_KANAL = "Kein Kanal #{kanal} — der Bot sieht ihn nicht, oder er heißt anders."
KEIN_RUECKBLICK = "Sitzung {sitzung} hat keinen Rückblick."
SCHON_ZUGESTELLT = "Rückblick zur Sitzung {sitzung} war schon zugestellt."
ZUGESTELLT = "Rückblick zur Sitzung {sitzung} nach #{kanal} zugestellt."
GESCHEITERT = "Rückblick zur Sitzung {sitzung} nicht zugestellt: {grund}"

GEKUERZT = "… gekürzt. Vollständig: {adresse}"
GEKUERZT_OHNE_ADRESSE = "… gekürzt — vollständig steht er in der Chronik."


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


def _adresse(config: Config, session_id: int) -> str | None:
    if not config.public_url:
        return None
    return f"{config.public_url.rstrip('/')}/sitzungen/{session_id}/protokoll"


def nachricht(text: str, adresse: str | None, session_id: int) -> str:
    if len(text) <= GRENZE:
        return text
    logger.warning(
        "Rückblick der Sitzung %s ist %s Zeichen lang, Discord nimmt %s — gekürzt zugestellt.",
        session_id,
        len(text),
        GRENZE,
    )
    zusatz = "\n\n" + (GEKUERZT.format(adresse=adresse) if adresse else GEKUERZT_OHNE_ADRESSE)
    return text[: GRENZE - len(zusatz)] + zusatz


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
        bot.post(kanal, nachricht(text, _adresse(zugang, session_id), session_id))
    except DiscordError as fehler:
        # Der Rückblick steht bereits in der Datenbank; ein Discord, das gerade nicht
        # antwortet, macht daraus keinen fehlgeschlagenen Stapellauf. ``delivered_at``
        # bleibt leer, der nächste Lauf holt die Zustellung nach.
        return GESCHEITERT.format(sitzung=session_id, grund=fehler)
    # Erst posten, dann merken: ein fehlgeschlagener Post soll wiederholt werden. Die
    # Lücke dazwischen ist ein Prozessabbruch zwischen HTTP-200 und einem lokalen UPDATE.
    _merken(runde, session_id, _now())
    return ZUGESTELLT.format(sitzung=session_id, kanal=kanalname)
