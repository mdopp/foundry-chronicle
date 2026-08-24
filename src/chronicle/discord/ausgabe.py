"""Die Chronik als Markdown-Datei in den Sitzungs-Thread.

Der Rückblick ist kurz und geht als Embed in den Gruppenkanal; die Chronik ist es nicht.
Sie in Nachrichten zu zerlegen hieße, eine Sitzung in Häppchen zu lesen — eine Datei
kennt kein Zeichenlimit und landet dort, wo die Sitzung ohnehin steht: in ihrem Thread.

**Die Trennung trägt die Datei in ihren Überschriften.** Der abgelegte Text hat die
Konvention schon: ``### Belegt aus Foundry`` und ``### Verbindungstext — vom Sprachmodell,
nicht belegt`` sagen im Klartext, was belegt ist und was nicht. Markdown gibt beides
unverändert weiter, deshalb wird hier nichts umgeschrieben und keine zweite Auszeichnung
erfunden — die HTML-Ansicht leitet ihre Klassen aus **denselben** Überschriften ab, und
eine zweite Konvention liefe beim nächsten Umformulieren von der ersten weg. Angefügt
wird genau eine Zeile: welche Fassung das ist und welche sie ablöst.

**Der Thread ist ein Protokoll, keine Tafel.** Eine neu komponierte Chronik wird als neue
Datei angehängt und benennt die Fassung, die sie ersetzt; die alte bleibt stehen. Woran
erkannt wird, ob eine Fassung schon hängt: ``protocol.delivered_at`` steht für die Chronik
auf dem Zeitpunkt ihres letzten Anhängens — ist der Text seither nicht neu geschrieben
worden, passiert nichts. Für den Rückblick gilt dieselbe Spalte strenger: dort heißt sie
»genau einmal«, siehe ``chronicle.discord.rueckblick``.

Und einmal deutlich: was hier hinausgeht, liegt danach bei Discord. Das ist der Preis
dieses Kanals, und die erste Datei einer Sitzung sagt es.

Zurück kommt nur, **was gesagt werden muss** — ein leerer Text heißt: die Datei hängt im
Thread und spricht für sich. Gemeldet wird, was schiefging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from chronicle import db, settings
from chronicle.compose.service import KIND
from chronicle.config import Config
from chronicle.discord.client import DiscordClient, DiscordError
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

DATEINAME = "chronik-{datum}.md"

# Die kleinste Grenze, die Discord einem Server ohne Boost setzt. Eine Chronik erreicht
# sie nie — sie besteht aus Sätzen, nicht aus Ton. Gefragt wird trotzdem vorher: die
# Alternative wäre ein Abbruch mitten im Lauf statt eines Satzes, den jemand lesen kann.
MAX_BYTES = 8 * 1024 * 1024

ERSTE = (
    "This session's chronicle — as a file, so that no character limit cuts it short. "
    "From now on it lies with Discord, like everything in this thread."
)
NEUE = (
    "Newly written chronicle. The version of {vorher} stays further up in the thread — "
    "nothing is overwritten here."
)

FASSUNG = "_Version of {stand}._"
FASSUNG_ERSETZT = "_Version of {stand}. It replaces the version of {vorher}._"

ZU_GROSS = (
    "The chronicle is larger than {grenze} MB and does not fit here as an attachment. It "
    "is written and stays written — split the session in two, then it fits."
)
GESCHEITERT = "I could not attach the chronicle here: {grund} I will try again on the next run."

# Die Nacherzählung geht denselben Weg und aus demselben Grund: sie ist lang, und ihre
# Überschriften tragen die Trennung zwischen Belegtem und Verbundenem. Nur der Ort ist ein
# anderer — sie gehört keiner Sitzung, also liegt sie dort, wo jemand nach ihr gefragt hat.
ERZAEHLUNG_DATEI = "nacherzaehlung-{von}-bis-{bis}.md"

ERZAEHLUNG_BEGLEITUNG = (
    "The retelling of this range of sessions — as a file, so that no character limit cuts "
    "it short. Under the “traced from the register” heading stands what is confirmed; the "
    "paragraphs above it were connected by the language model, and gaps are named as gaps."
)

ERZAEHLUNG_GESCHEITERT = "I could not attach the retelling here: {grund} Call the command again."

ERZAEHLUNG_ZU_GROSS = (
    "The retelling is larger than {grenze} MB and does not fit here as an attachment. "
    "Take a shorter range, then it fits."
)


@dataclass(frozen=True)
class Stand:
    """Die abgelegte Chronik einer Sitzung mit allem, was das Anhängen braucht."""

    text: str
    created_at: str
    delivered_at: str | None
    played_on: str
    kanal_id: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _lesbar(zeitpunkt: str) -> str:
    return datetime.fromisoformat(zeitpunkt).astimezone().strftime("%d.%m.%Y um %H:%M")


def _stand(runde: Runde, session_id: int) -> Stand | None:
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT p.text, p.created_at, p.delivered_at, s.played_on, s.kanal_id "
            "FROM protocol p JOIN session s ON s.id = p.session_id AND s.runde_id = p.runde_id "
            "WHERE p.runde_id = ? AND p.session_id = ? AND p.kind = ?",
            (scope.runde_id, session_id, KIND),
        ).fetchone()
    finally:
        scope.close()
    if zeile is None:
        return None
    return Stand(
        text=str(zeile["text"]),
        created_at=str(zeile["created_at"]),
        delivered_at=zeile["delivered_at"],
        played_on=str(zeile["played_on"]),
        kanal_id=zeile["kanal_id"],
    )


def _merken(runde: Runde, session_id: int, at: str) -> None:
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "UPDATE protocol SET delivered_at = ? "
                "WHERE runde_id = ? AND session_id = ? AND kind = ?",
                (at, scope.runde_id, session_id, KIND),
            )
    finally:
        scope.close()


def dateiname(played_on: str) -> str:
    return DATEINAME.format(datum=played_on)


def datei(text: str, stand: str, vorher: str | None = None) -> str:
    """Der abgelegte Text, unangetastet, mit einer Zeile davor: welche Fassung das ist."""
    kopf = (
        FASSUNG.format(stand=_lesbar(stand))
        if vorher is None
        else FASSUNG_ERSETZT.format(stand=_lesbar(stand), vorher=_lesbar(vorher))
    )
    return f"{kopf}\n\n{text.strip()}\n"


def begleitung(vorher: str | None) -> str:
    return ERSTE if vorher is None else NEUE.format(vorher=_lesbar(vorher))


def erzaehlung_zustellen(
    config: Config,
    runde: Runde,
    kanal_id: str,
    text: str,
    von: str,
    bis: str,
    *,
    client: DiscordClient | None = None,
) -> str:
    """Die Nacherzählung als Markdown-Datei dorthin, wo der Befehl kam.

    Gemerkt wird nichts: sie hängt an keiner Sitzung, und ein zweiter Aufruf ist ein
    zweiter Wunsch und keine zweite Fassung. Zurück kommt nur, was gesagt werden muss.
    """
    zugang = settings.effective(config, runde)
    if not zugang.discord_configured:
        return ""
    inhalt = f"{text.strip()}\n".encode()
    if len(inhalt) > MAX_BYTES:
        logger.warning("Nacherzählung ist %s Bytes groß.", len(inhalt))
        return ERZAEHLUNG_ZU_GROSS.format(grenze=MAX_BYTES // (1024 * 1024))
    bot = client if client is not None else DiscordClient(zugang)
    try:
        bot.post_file(
            kanal_id,
            ERZAEHLUNG_DATEI.format(von=von, bis=bis),
            inhalt,
            ERZAEHLUNG_BEGLEITUNG,
        )
    except DiscordError as fehler:
        return ERZAEHLUNG_GESCHEITERT.format(grund=fehler)
    return ""


def anhaengen(
    config: Config, runde: Runde, session_id: int, *, client: DiscordClient | None = None
) -> str:
    """Hängt die Chronik dieser Sitzung in ihren Kanal, wenn diese Fassung noch fehlt."""
    zugang = settings.effective(config, runde)
    if not zugang.discord_configured:
        return ""

    abgelegt = _stand(runde, session_id)
    if abgelegt is None:
        logger.warning("Sitzung %s hat keine Chronik zum Anhängen.", session_id)
        return ""
    if not abgelegt.kanal_id:
        # Sitzungen aus der Zeit vor Discord haben keinen. Die Chronik steht trotzdem,
        # und ein Satz darüber hätte hier keinen Ort, an dem ihn jemand läse.
        logger.info("Sitzung %s hat keinen Kanal — die Chronik bleibt abgelegt.", session_id)
        return ""
    if abgelegt.delivered_at is not None and abgelegt.created_at <= abgelegt.delivered_at:
        return ""

    jetzt = _now()
    inhalt = datei(abgelegt.text, jetzt, abgelegt.delivered_at).encode("utf-8")
    if len(inhalt) > MAX_BYTES:
        logger.warning("Chronik der Sitzung %s ist %s Bytes groß.", session_id, len(inhalt))
        return ZU_GROSS.format(grenze=MAX_BYTES // (1024 * 1024))

    bot = client if client is not None else DiscordClient(zugang)
    try:
        bot.post_file(
            abgelegt.kanal_id,
            dateiname(abgelegt.played_on),
            inhalt,
            begleitung(abgelegt.delivered_at),
        )
    except DiscordError as fehler:
        # Die Chronik steht in der Datenbank; ein Discord, das gerade nicht antwortet,
        # macht daraus keinen gescheiterten Lauf. ``delivered_at`` bleibt, wie es war, und
        # der nächste Lauf hängt sie nach.
        return GESCHEITERT.format(grund=fehler)
    # Erst hängen, dann merken — wie beim Rückblick: ein misslungener Anhang soll
    # wiederholt werden.
    _merken(runde, session_id, jetzt)
    return ""
