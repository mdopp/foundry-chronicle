"""Die dauerhafte Gateway-Verbindung — der einzige Ort, der py-cord kennt.

**Warum py-cord.** Audio zu *empfangen* ist von Discord nicht offiziell unterstützt;
``discord.py`` kann es nicht. Von den beiden Nachrüstungen bringt py-cord die Senken-API
als Teil der Bibliothek mit, wird regelmäßig veröffentlicht und füllt beim Empfang die
Sprechpausen anhand der RTP-Zeitstempel mit Stille auf — genau das hält die Spuren aller
Sprecher auf **einer** Zeitachse, ohne die die spätere Zusammenführung raten müsste.
``discord-ext-voice-recv`` leistet dasselbe als Erweiterung zu discord.py, ist aber
erklärtermaßen im Aufbau. Das bleibt die eine bekannte Bruchstelle des Systems, deshalb
steckt sie in dieser einen Datei: darunter weiß nichts mehr, dass es Discord gibt.

Die Befehle werden beim Start per REST registriert — py-cord schreibt sie beim Verbinden
mit einem einzigen Aufruf über die Anwendungs-Befehle des Bots.

Der Token geht in genau einen Aufruf und in keine Logzeile.
"""

from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path

from chronicle import consent
from chronicle.bot import BotFehler, ansage, recorder
from chronicle.bot.recorder import Aufnahme, Kanal
from chronicle.config import Config

logger = logging.getLogger(__name__)

GRUPPE = "aufnahme"

NICHT_INSTALLIERT = (
    "py-cord ist nicht installiert — im Image ist es dabei, "
    "lokal nachrüsten mit: pip install '.[discord]'"
)

NICHT_IM_KANAL = "Du bist in keinem Sprachkanal — geh hinein und ruf mich noch einmal."
LAEUFT_SCHON = "Ich schneide schon mit."
LAEUFT_NICHT = "Es läuft gerade keine Aufnahme."

RAHMEN = ansage.KANAELE * ansage.BREITE


def _discord():
    # Lokal importiert wie der Spracherkenner: die Sprach-Abhängigkeiten liegen im Image,
    # aber nicht in jeder Dev-Installation — ohne sie bleibt der Rest startbar.
    try:
        import discord
    except ImportError as fehler:
        raise BotFehler(NICHT_INSTALLIERT) from fehler
    return discord


class _WavStrom:
    """``PCMAudio`` liest rohe Rahmen; die Ansage liegt als WAV auf der Platte."""

    def __init__(self, pfad: Path) -> None:
        self._wave = wave.open(str(pfad), "rb")

    def read(self, anzahl: int) -> bytes:
        return self._wave.readframes(anzahl // RAHMEN)


class Sprachverbindung:
    """Die py-cord-Seite der ``Stimme`` aus ``recorder``."""

    def __init__(self, voice_client) -> None:
        self._vc = voice_client
        kanal = voice_client.channel
        self.kanal = Kanal(guild_id=str(kanal.guild.id), id=str(kanal.id), name=kanal.name)

    def mitglieder(self) -> tuple[consent.Member, ...]:
        return tuple(
            consent.Member(id=str(wer.id), name=wer.display_name)
            for wer in self._vc.channel.members
            if not wer.bot
        )

    async def ansagen(self, datei: Path) -> None:
        """Spielt die Ansage und kehrt erst zurück, wenn sie zu Ende ist."""
        discord = _discord()
        schleife = asyncio.get_running_loop()
        fertig = asyncio.Event()
        # ``after`` läuft im Abspiel-Thread — der Ereignisschleife darf man von dort nur
        # über call_soon_threadsafe nahekommen.
        self._vc.play(
            discord.PCMAudio(_WavStrom(datei)),
            after=lambda _fehler: schleife.call_soon_threadsafe(fertig.set),
        )
        await fertig.wait()

    def mitschneiden(self, aufnahme: Aufnahme) -> None:
        self._vc.start_recording(_senke(aufnahme, self._vc), _abgeschlossen)

    def mitschnitt_beenden(self) -> None:
        self._vc.stop_recording()

    async def trennen(self) -> None:
        await self._vc.disconnect()


async def _abgeschlossen(senke, *args) -> None:
    """py-cord verlangt einen Rückruf; die Spuren schließt ``Aufnahme.beenden``."""


def _mitglied(voice_client, user_id: int) -> consent.Member:
    wer = voice_client.guild.get_member(user_id)
    return consent.Member(
        id=str(user_id), name=wer.display_name if wer is not None else str(user_id)
    )


def _senke(aufnahme: Aufnahme, voice_client):
    discord = _discord()

    class SpurSenke(discord.sinks.Sink):
        """Je Sprecher eine Spur — geschrieben wird auf die Platte, nicht in den Speicher."""

        def write(self, data: bytes, user: int) -> None:
            aufnahme.schreiben(_mitglied(voice_client, user), data)

        def cleanup(self) -> None:
            self.finished = True

    return SpurSenke()


class _Lauf:
    """Eine Instanz pro Gruppe — also höchstens eine Aufnahme zur Zeit."""

    def __init__(self) -> None:
        self.stimme: Sprachverbindung | None = None
        self.aufnahme: Aufnahme | None = None


def baue(config: Config):
    """Der Bot mit seinen beiden Befehlen — noch ohne Verbindung."""
    discord = _discord()
    absichten = discord.Intents.none()
    absichten.guilds = True
    absichten.voice_states = True
    bot = discord.Bot(intents=absichten)
    lauf = _Lauf()
    gruppe = bot.create_group(GRUPPE, "Die Sitzung mitschneiden")

    @gruppe.command(name="start", description="Beitreten, ansagen, je Sprecher mitschneiden")
    async def start(ctx) -> None:
        if lauf.aufnahme is not None:
            await ctx.respond(LAEUFT_SCHON, ephemeral=True)
            return
        kanal = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if kanal is None:
            await ctx.respond(NICHT_IM_KANAL, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        stimme = Sprachverbindung(await kanal.connect())
        try:
            lauf.aufnahme = await recorder.starten(config, stimme)
        except BotFehler as fehler:
            await stimme.trennen()
            await ctx.respond(str(fehler), ephemeral=True)
            return
        lauf.stimme = stimme
        await ctx.respond(recorder.GESTARTET, ephemeral=True)

    @gruppe.command(name="stop", description="Aufnahme beenden und die Spuren einreihen")
    async def stop(ctx) -> None:
        if lauf.aufnahme is None:
            await ctx.respond(LAEUFT_NICHT, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        meldungen = await recorder.stoppen(lauf.stimme, lauf.aufnahme)
        lauf.stimme = None
        lauf.aufnahme = None
        await ctx.respond(" ".join(meldungen), ephemeral=True)

    @bot.event
    async def on_voice_state_update(member, before, after) -> None:
        if lauf.aufnahme is None or member.bot or after.channel is None:
            return
        if str(after.channel.id) != lauf.aufnahme.kanal.id:
            return
        if before.channel is not None and before.channel.id == after.channel.id:
            return
        await recorder.nachzuegler(
            config,
            lauf.stimme,
            lauf.aufnahme,
            consent.Member(id=str(member.id), name=member.display_name),
        )

    return bot


def run(config: Config) -> None:
    logger.info("Aufnahme-Bot: verbinde mit dem Discord-Gateway")
    baue(config).run(config.discord_bot_token)
