"""Den Briefkasten leeren: Audio wird Spur, Text wird Notiz, beides quittiert.

Genau **ein** Kanal, nach Namenskonvention ``#diktat``. Autorisierung ist Discords
eigenes Rechtemodell: wer dort schreiben darf, darf diktieren. Geantwortet wird nur auf
Nachrichten in diesem Kanal und dort auf jede höchstens einmal.

Ein zweiter Lauf darf nichts verdoppeln. Dafür stehen zwei Dinge in der Datenbank: der
**Zeiger** auf die zuletzt abgeholte Nachricht — er spart das erneute Holen — und die
**Kennung jeder erledigten Nachricht**. Die Kennung ist die Garantie: geht der Zeiger
verloren oder wird er zurückgesetzt, wird trotzdem nichts ein zweites Mal abgelegt.

Wohin ein Diktat gehört, wird nicht geraten: es landet in der **zuletzt angelegten**
Sitzung. Gibt es keine, wartet es sichtbar — Warnzeichen und ein Satz — statt sich eine
Sitzung zu erfinden. Der Zeiger rückt dann nicht vor, das Wartende kommt im nächsten
Lauf wieder.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db, notes, recordings, settings
from chronicle.config import Config
from chronicle.discord.client import Attachment, DiscordClient, Message

logger = logging.getLogger(__name__)

KANAL = "diktat"

HAKEN = "✅"
WARNUNG = "⚠️"

CURSOR = "discord_cursor"

ABGELEGT = "abgelegt"
WARTET = "wartet"
UEBERSPRUNGEN = "uebersprungen"

QUITTUNG = "Landet im nächsten Stapel — Ergebnis morgen früh."
NOTIZ_QUITTUNG = "Als Notiz zur Sitzung vom {datum} abgelegt."
OHNE_SITZUNG = "Noch keine Sitzung angelegt — das Diktat wartet, bis es eine gibt."
ZU_GROSS = "»{name}« ist größer als {grenze} MB und bleibt liegen."

NICHT_EINGERICHTET = "Kein Bot-Token — der Diktat-Kanal ist nicht eingerichtet."
KEIN_KANAL = f"Kein Kanal #{KANAL} — der Bot sieht ihn nicht, oder er heißt anders."
LEER = "Nichts im Briefkasten."


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stand(database_path: Path, message_id: str) -> str | None:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute(
            "SELECT status FROM discord_intake WHERE message_id = ?", (message_id,)
        ).fetchone()
    finally:
        connection.close()
    return None if zeile is None else str(zeile["status"])


def _merken(database_path: Path, message_id: str, status: str) -> None:
    connection = db.connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO discord_intake (message_id, status, handled_at) VALUES (?, ?, ?) "
                "ON CONFLICT (message_id) DO UPDATE SET status = excluded.status, "
                "handled_at = excluded.handled_at",
                (message_id, status, _now()),
            )
    finally:
        connection.close()


def cursor(database_path: Path) -> str | None:
    connection = db.connect(database_path)
    try:
        zeile = connection.execute("SELECT value FROM meta WHERE key = ?", (CURSOR,)).fetchone()
    finally:
        connection.close()
    return None if zeile is None else str(zeile["value"])


def _cursor_setzen(database_path: Path, message_id: str) -> None:
    connection = db.connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (CURSOR, message_id),
            )
    finally:
        connection.close()


def _ist_audio(anhang: Attachment) -> bool:
    return Path(anhang.filename).suffix.lower() in recordings.SUFFIXES


def _spur(config: Config, bot: DiscordClient, sitzung_id: int, anhang: Attachment) -> str:
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings.target_path(config.recordings_dir, sitzung_id, anhang.filename)
    bot.download(anhang, ziel)
    recordings.enqueue(config.database_path, sitzung_id, ziel.name)
    return ziel.name


def _eine_nachricht(
    config: Config,
    bot: DiscordClient,
    kanal: str,
    nachricht: Message,
    *,
    erstmals: bool,
) -> tuple[str, str]:
    """Legt eine Nachricht ab und quittiert sie; liefert Meldung und Stand.

    Die Quittung des Abgelegten geht immer heraus — danach ist die Nachricht erledigt
    und kommt kein zweites Mal hierher. Das Warnzeichen dagegen nur beim ersten Mal:
    ein wartendes Diktat wird jeden Lauf noch einmal angesehen und soll deshalb nicht
    jede Nacht dieselbe Absage bekommen.
    """
    if nachricht.from_bot:
        return "", UEBERSPRUNGEN

    spuren = tuple(anhang for anhang in nachricht.attachments if _ist_audio(anhang))
    text = nachricht.content.strip()
    if not spuren and not text:
        if erstmals:
            bot.react(kanal, nachricht.id, WARNUNG)
        return f"Nachricht {nachricht.id}: weder Audio noch Text — übersprungen.", UEBERSPRUNGEN

    zu_gross = next((anhang for anhang in spuren if anhang.size > recordings.MAX_BYTES), None)
    if zu_gross is not None:
        grenze = ZU_GROSS.format(
            name=zu_gross.filename, grenze=recordings.MAX_BYTES // (1024 * 1024)
        )
        if erstmals:
            bot.react(kanal, nachricht.id, WARNUNG)
            bot.reply(kanal, nachricht.id, grenze)
        return grenze, UEBERSPRUNGEN

    sitzung = notes.latest_session(config.database_path)
    if sitzung is None:
        if erstmals:
            bot.react(kanal, nachricht.id, WARNUNG)
            bot.reply(kanal, nachricht.id, OHNE_SITZUNG)
        return OHNE_SITZUNG, WARTET

    meldungen = [
        f"Diktat »{_spur(config, bot, sitzung.id, anhang)}« → Sitzung {sitzung.id}, "
        "wartet auf den Stapel."
        for anhang in spuren
    ]
    if text:
        szene = sitzung.scenes[-1]
        notes.add_note(config.database_path, szene.id, text)
        meldungen.append(f"Notiz → Sitzung {sitzung.id}, Szene {szene.position}.")

    bot.react(kanal, nachricht.id, HAKEN)
    bot.reply(
        kanal,
        nachricht.id,
        QUITTUNG if spuren else NOTIZ_QUITTUNG.format(datum=sitzung.played_on),
    )
    return " ".join(meldungen), ABGELEGT


def run(config: Config, *, client: DiscordClient | None = None) -> tuple[str, ...]:
    """Holt, was seit dem letzten Lauf im Kanal liegt, und legt es ab."""
    db.init(config.database_path)
    zugang = settings.effective(config)
    if not zugang.discord_configured:
        return (NICHT_EINGERICHTET,)

    bot = client if client is not None else DiscordClient(zugang)
    kanal = bot.channel_id(KANAL)
    if kanal is None:
        return (KEIN_KANAL,)

    abgeholt = cursor(config.database_path)
    nachrichten = bot.messages(kanal, after=abgeholt)
    logger.info("Discord: %s Nachrichten in #%s seit %s", len(nachrichten), KANAL, abgeholt)

    meldungen: list[str] = []
    zeiger = abgeholt
    haelt = False
    for nachricht in nachrichten:
        stand = _stand(config.database_path, nachricht.id)
        if stand not in (ABGELEGT, UEBERSPRUNGEN):
            meldung, stand = _eine_nachricht(config, bot, kanal, nachricht, erstmals=stand is None)
            _merken(config.database_path, nachricht.id, stand)
            if meldung:
                meldungen.append(meldung)
        haelt = haelt or stand == WARTET
        if not haelt:
            zeiger = nachricht.id

    if zeiger is not None and zeiger != abgeholt:
        _cursor_setzen(config.database_path, zeiger)
    return tuple(meldungen)
