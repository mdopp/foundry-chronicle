"""Den Briefkasten leeren: Audio wird Spur, Text wird Notiz, beides quittiert.

Genau **ein** Kanal, nach Namenskonvention ``#diktat`` — und zwar der **in der Gilde
dieser Runde**. Autorisierung ist Discords eigenes Rechtemodell: wer dort schreiben darf,
darf diktieren. Geantwortet wird nur auf Nachrichten in diesem Kanal und dort auf jede
höchstens einmal.

Dass die Gilde dazugehört, ist der Punkt und keine Genauigkeit nebenbei (#192): »diktat«
heißt in jeder zweiten Gilde ein Kanal, und eine Suche über alle Gilden des Bots fand die
erstbeste. Bei mehreren Runden auf einer Instanz las damit **jede** denselben Briefkasten
— fremdes gesprochenes und geschriebenes Wort landete in einer Chronik, in die es nicht
gehört, und der Zeiger der lesenden Runde rückte über Nachrichten, die ihr nicht gehören.
Eine Runde ohne Gilde hat keinen Briefkasten; dort wird nicht geraten, sondern gesagt.

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
from chronicle.runde import Runde

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
# Die Gilde steht bewusst nicht drin: sie sagt der Gruppe nichts, und die Kennung einer
# fremden wäre das eine, was hier nicht hinausgehen soll. Ins Log gehört sie, dorthin
# sieht der Betreiber.
KEIN_KANAL = f"Kein Kanal #{KANAL} in eurer Gilde — der Bot sieht ihn nicht, oder er heißt anders."
OHNE_GILDE = (
    f"Diese Runde hängt an keiner Discord-Gilde — einen Briefkasten #{KANAL} hat sie damit "
    "nirgends. Richte sie mit `/chronicle setup` in eurer Gilde ein."
)
LEER = "Nichts im Briefkasten."


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stand(runde: Runde, message_id: str) -> str | None:
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT status FROM discord_intake WHERE runde_id = ? AND message_id = ?",
            (scope.runde_id, message_id),
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else str(zeile["status"])


def _merken(runde: Runde, message_id: str, status: str) -> None:
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "INSERT INTO discord_intake (runde_id, message_id, status, handled_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (runde_id, message_id) DO UPDATE SET status = excluded.status, "
                "handled_at = excluded.handled_at",
                (scope.runde_id, message_id, status, _now()),
            )
    finally:
        scope.close()


def cursor(runde: Runde) -> str | None:
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT value FROM runde_meta WHERE runde_id = ? AND key = ?",
            (scope.runde_id, CURSOR),
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else str(zeile["value"])


def _cursor_setzen(runde: Runde, message_id: str) -> None:
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "INSERT INTO runde_meta (runde_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (runde_id, key) DO UPDATE SET value = excluded.value",
                (scope.runde_id, CURSOR, message_id),
            )
    finally:
        scope.close()


def _ist_audio(anhang: Attachment) -> bool:
    return Path(anhang.filename).suffix.lower() in recordings.SUFFIXES


def _spur(
    config: Config,
    runde: Runde,
    bot: DiscordClient,
    sitzung_id: int,
    anhang: Attachment,
    nachricht: Message,
) -> str:
    """Ein Anhang wird eine wartende Spur — mit dem Zeitpunkt der **Nachricht** daran.

    Der Anker ist der Einwurf und nicht das Abholen (#222): der Briefkasten wird nachts
    geleert, und mit der Abholzeit käme ein Diktat vom Heimweg in der Szene an, die gerade
    zufällig die letzte ist. Ohne ihn erreicht es überhaupt keine — ``merge.DIKTATE``
    verlangt ihn, weil eine Spur ohne Sitzungsuhr sonst keine Achse hat. Der Weg über den
    Sitzungs-Thread reicht ihn seit #160 durch; hier fehlte er.

    **Nachgetragen wird nichts.** Eine Spur aus der Zeit davor hat keine Nachricht mehr,
    an der ein Anker abzulesen wäre — ``uploaded_at`` ist der Moment des Abholens und
    stünde für einen Einwurf, den es so nie gab. Wer sie in der Chronik haben will, wirft
    sie neu ein; einen geratenen Zeitpunkt bekommt sie nicht.
    """
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings.target_path(config.recordings_dir, sitzung_id, anhang.filename)
    bot.download(anhang, ziel)
    recordings.enqueue(runde, sitzung_id, ziel.name, message_at=nachricht.timestamp.strip() or None)
    return ziel.name


def _eine_nachricht(
    config: Config,
    runde: Runde,
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

    sitzung = notes.latest_session(runde)
    if sitzung is None:
        if erstmals:
            bot.react(kanal, nachricht.id, WARNUNG)
            bot.reply(kanal, nachricht.id, OHNE_SITZUNG)
        return OHNE_SITZUNG, WARTET

    meldungen = [
        f"Diktat »{_spur(config, runde, bot, sitzung.id, anhang, nachricht)}« → "
        f"Sitzung {sitzung.id}, "
        "wartet auf den Stapel."
        for anhang in spuren
    ]
    if text:
        szene = sitzung.scenes[-1]
        notes.add_note(runde, szene.id, text)
        meldungen.append(f"Notiz → Sitzung {sitzung.id}, Szene {szene.position}.")

    bot.react(kanal, nachricht.id, HAKEN)
    bot.reply(
        kanal,
        nachricht.id,
        QUITTUNG if spuren else NOTIZ_QUITTUNG.format(datum=sitzung.played_on),
    )
    return " ".join(meldungen), ABGELEGT


def run(config: Config, runde: Runde, *, client: DiscordClient | None = None) -> tuple[str, ...]:
    """Holt, was seit dem letzten Lauf im Kanal liegt, und legt es ab."""
    db.init(config.database_path)
    zugang = settings.effective(config, runde)
    if not zugang.discord_configured:
        return (NICHT_EINGERICHTET,)

    if not runde.guild_id:
        logger.warning(
            "Runde %s hängt an keiner Gilde — der Briefkasten wird nicht geleert.", runde.id
        )
        return (OHNE_GILDE,)

    bot = client if client is not None else DiscordClient(zugang)
    kanal = bot.guild_channel_id(runde.guild_id, KANAL)
    if kanal is None:
        logger.warning(
            "Kein Kanal #%s in Gilde %s — der Briefkasten der Runde %s bleibt zu.",
            KANAL,
            runde.guild_id,
            runde.id,
        )
        return (KEIN_KANAL,)

    abgeholt = cursor(runde)
    nachrichten = bot.messages(kanal, after=abgeholt)
    logger.info("Discord: %s Nachrichten in #%s seit %s", len(nachrichten), KANAL, abgeholt)

    meldungen: list[str] = []
    zeiger = abgeholt
    haelt = False
    for nachricht in nachrichten:
        stand = _stand(runde, nachricht.id)
        if stand not in (ABGELEGT, UEBERSPRUNGEN):
            meldung, stand = _eine_nachricht(
                config, runde, bot, kanal, nachricht, erstmals=stand is None
            )
            _merken(runde, nachricht.id, stand)
            if meldung:
                meldungen.append(meldung)
        haelt = haelt or stand == WARTET
        if not haelt:
            zeiger = nachricht.id

    if zeiger is not None and zeiger != abgeholt:
        _cursor_setzen(runde, zeiger)
    return tuple(meldungen)
