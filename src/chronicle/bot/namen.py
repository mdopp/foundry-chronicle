"""Zu einer Discord-Kennung der Anzeigename — gezielt geholt, nirgends gespiegelt.

Discord trennt die Audiodaten je Client, die Kennung des Sprechers steht deshalb an jeder
Spur. Der **Name** dagegen kommt nur aus dem Mitglieder-Zwischenspeicher, und der füllt
sich ohne den privilegierten ``members``-Intent allein über Interaktionen: wer einen Befehl
gegeben hat, ist bekannt; wer bloß im Sprachkanal saß und sprach, nicht. Genau so wurden
zwei von vier Beteiligten eines echten Abends zu »unbekannt«, obwohl ihre Kennung die ganze
Zeit dastand (#250).

Der Ausweg ist **nicht** der Intent. Ihn zu setzen hieße, die vollständige Mitgliederliste
jeder Gilde dauerhaft in diesen Prozess zu spiegeln — mehr personenbezogene Daten für
dasselbe Ergebnis. Hier wird stattdessen **je Sprecher einmal** gefragt: ein
``fetch_user``, ein Name, keine Liste. Die sparsamere Lösung ist zugleich die genauere.

**Wann gefragt wird: beim Einreihen.** Nicht beim Verbinden — dann stünde für jeden im
Kanal ein Name da, auch für die, die den Abend über schweigen und von denen nie eine
Sekunde Ton entsteht. Und nicht erst beim Schreiben der Chronik — die läuft im Stapel,
nachts, ohne Gewähr, dass der Bot dann am Gateway hängt oder Discord antwortet; derselbe
Abend käme bei zwei Läufen verschieden heraus, und jede neue Fassung fragte erneut. Das
Einreihen ist der späteste Zeitpunkt, der noch trägt: dort *ist* aus der Stimme Ton
geworden — leere Spuren werden gelöscht und kommen nie hier vorbei —, der Prozess steht
noch an seiner Verbindung, und ein Aufruf füllt eine Zeile, die alles Weitere liest.

**Scheitert das Nachschlagen, bleibt es bei »unbekannt«** — bei dem Wort, nicht beim
Dateinamen. Ein gelöschtes Konto, eine verlassene Gilde, ein stummes Discord: keiner dieser
Fälle darf einen technischen Bezeichner in einen Text tragen, den die Gruppe Wochen später
als Gedächtnisstütze liest. Die Zeile bleibt dann leer, und ``transcribe.merge`` schreibt
das Wort.

Und es ist **ein Weg, den es weiter gibt**, kein einmaliger Handgriff: derselbe Aufruf
läuft nach jedem Mitschnitt und lässt sich über ``python -m chronicle.bot.namen`` für eine
Runde oder eine einzelne Sitzung nachholen. Was ein abgestürzter Prozess oder ein
Netzausfall liegen ließ, holt der nächste Lauf; er ist idempotent und fasst nur an, wo noch
nichts steht.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence

from chronicle import consent, db, recordings, settings
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

AUFRUF = "Usage: python -m chronicle.bot.namen <round-id> [<session-id>]"
UNBEKANNTE_RUNDE = "There is no round {gewaehlt} here."
OHNE_TOKEN = "Without a bot token nobody can be looked up."
BERICHT = "{zeilen} rows named, {offen} ids stayed without a name."

# Wer die Namen holt: zu einer Handvoll Kennungen die gefundenen Anzeigenamen. Was fehlt,
# fehlt — der Aufrufer erfindet nichts.
Nachschlag = Callable[[Sequence[str]], Awaitable[Mapping[str, str]]]


async def aufloesen(client, kennungen: Sequence[str]) -> dict[str, str]:
    """Je Kennung höchstens ein Aufruf an Discord — der Zwischenspeicher zuerst.

    ``get_user`` kostet nichts und trifft die, die dem Prozess schon begegnet sind; erst
    für den Rest geht eine Anfrage hinaus. Was Discord nicht hergibt, fehlt im Ergebnis:
    ein gelöschtes Konto hat keinen Namen mehr, und einen zu erfinden wäre schlimmer als
    die Lücke.
    """
    gefunden: dict[str, str] = {}
    for kennung in kennungen:
        try:
            nummer = int(kennung)
        except ValueError:
            continue
        wer = client.get_user(nummer)
        if wer is None:
            try:
                wer = await client.fetch_user(nummer)
            except Exception as fehler:  # noqa: BLE001
                # Ohne Kennung und ohne Namen: die eine benennt eine Person, der andere
                # erst recht. Was der Betreiber hier braucht, ist der Grund.
                logger.warning(
                    "Ein Sprechername ließ sich nicht nachschlagen (%s) — es bleibt bei »%s«.",
                    type(fehler).__name__,
                    consent.UNBEKANNT,
                )
                continue
        name = getattr(wer, "display_name", None) or getattr(wer, "name", None)
        if name:
            gefunden[kennung] = name
    return gefunden


async def nachtragen(nachschlag: Nachschlag, runde: Runde, session_id: int | None = None) -> int:
    """Holt die fehlenden Sprechernamen und schreibt sie an ihre Spuren.

    Gibt zurück, wie viele Zeilen dadurch einen Namen bekamen — null ist der Normalfall
    einer Sitzung, in der alle Sprecher ohnehin bekannt waren.
    """
    offen = recordings.namenlose_sprecher(runde, session_id)
    if not offen:
        return 0
    gefunden = await nachschlag(offen)
    zeilen = recordings.namen_eintragen(runde, gefunden)
    # Zahlen statt Namen (#194): dass nachgetragen wurde und wie viel offen blieb, ist die
    # Auskunft; wer es war, steht in der Datenbank und gehört nicht ins Log des Betreibers.
    logger.info(
        "Sitzung %s: %s von %s Sprechern nachgeschlagen, %s Zeilen benannt",
        session_id,
        len(gefunden),
        len(offen),
        zeilen,
    )
    return zeilen


async def _nachholen(config: Config, runde: Runde, session_id: int | None) -> int:
    """Meldet sich mit dem Bot-Token an, trägt nach und geht wieder — ohne Gateway.

    ``login`` genügt für ``fetch_user``: gebraucht wird die HTTP-Sitzung, nicht der
    Ereignisstrom. Damit hängt das Nachtragen an keiner Absicht, an keinem Zwischenspeicher
    und an keiner laufenden Verbindung.
    """
    from chronicle.bot.gateway import _discord

    discord = _discord()
    client = discord.Client(intents=discord.Intents.none())
    await client.login(config.discord_bot_token)
    try:
        return await nachtragen(lambda ids: aufloesen(client, ids), runde, session_id)
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = sys.argv[1:] if argv is None else argv
    if not 1 <= len(args) <= 2 or not all(teil.isdigit() for teil in args):
        print(AUFRUF)
        return 2
    config = Config.from_env()
    db.init(config.database_path)
    runde = runden.get(config.database_path, int(args[0]))
    if runde is None:
        print(UNBEKANNTE_RUNDE.format(gewaehlt=args[0]))
        return 2
    # Denselben Weg wie der Bot selbst: ein gepflegter Wert schlägt die Umgebung.
    zugang = settings.effective(config, runde)
    if not zugang.discord_configured:
        print(OHNE_TOKEN)
        return 2
    sitzung = int(args[1]) if len(args) == 2 else None
    zeilen = asyncio.run(_nachholen(zugang, runde, sitzung))
    print(BERICHT.format(zeilen=zeilen, offen=len(recordings.namenlose_sprecher(runde, sitzung))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
