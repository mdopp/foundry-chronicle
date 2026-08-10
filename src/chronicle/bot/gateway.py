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
import functools
import logging
import wave
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

from chronicle import consent, recordings
from chronicle.bot import BotFehler, ansage, chronik, recorder
from chronicle.bot.recorder import Aufnahme, Kanal
from chronicle.config import Config

logger = logging.getLogger(__name__)

GRUPPE = "aufnahme"
GRUPPE_CHRONIK = "chronik"
BEFEHL_SZENE = "szene"

NICHT_INSTALLIERT = (
    "py-cord ist nicht installiert — im Image ist es dabei, "
    "lokal nachrüsten mit: pip install '.[discord]'"
)

SPRACHE_FEHLT = (
    "Dem Bot fehlt {fehlend} — ohne das spricht py-cord Discords Sprach-Verschlüsselung "
    "nicht: keine Ansage, keine Aufnahme. Im Image ist es dabei, lokal nachrüsten mit: "
    "pip install '.[discord]'"
)

# Discord weist die Anmeldung mit 4014 ab, py-cord meldet PrivilegedIntentsRequired. Der
# Bot bleibt trotzdem beim Fehlschlag stehen und läuft in den Neustart: sobald der Schalter
# gesetzt ist, kommt er von allein wieder hoch. Was fehlte, ist ein Satz statt eines
# Stapelauszugs — daran lag es zuletzt eine Nacht lang.
RECHTE_FEHLEN = (
    "Discord verweigert die Anmeldung: dem Bot fehlt die Freigabe für den Nachrichten-Inhalt. "
    "Ohne sie kommt jede Notiz aus dem Thread leer an, deshalb fordert der Bot sie an. "
    "Einschalten unter https://discord.com/developers/applications → die Anwendung → Bot → "
    "Privileged Gateway Intents → Message Content Intent. Danach startet der Bot von selbst neu."
)

NICHT_IM_KANAL = "Du bist in keinem Sprachkanal — geh hinein und ruf mich noch einmal."
LAEUFT_SCHON = "Ich schneide schon mit."
LAEUFT_NICHT = "Es läuft gerade keine Aufnahme."

UNBEKANNT = "unbekannt"

# Ein Befehl, der nicht antwortet, lässt Discord ewig »denkt nach …« anzeigen. Das ist
# der schlechteste Ausgang: niemand weiß, ob aufgenommen wird. Deshalb antwortet jeder
# Befehl, auch wenn er scheitert — der Grund in Nutzersprache, die Einzelheiten ins Log.
GESCHEITERT = (
    "Das hat nicht geklappt: {grund} "
    "Was du tun kannst: es noch einmal versuchen — bleibt es dabei, steht der Grund im "
    "Log des Bots."
)

UNERWARTET = "unerwarteter Fehler im Bot ({typ})."

HILFE = (
    "**So schneide ich eine Sitzung mit**\n"
    "• `/chronik start` — ich lege die Sitzung an und öffne den Thread dazu; ab dort wird "
    "jede Nachricht eine Notiz.\n"
    "• `/aufnahme start` — ich komme in deinen Sprachkanal, spiele eine hörbare Ansage "
    "und schneide **erst danach** mit, je Sprecherin und Sprecher eine eigene Spur.\n"
    "• `/aufnahme stop` — ich höre auf und gehe wieder; die Spuren wandern in den "
    "nächtlichen Lauf und werden zu Text.\n"
    "• `/chronik fertig` — Sitzung abschließen: Zahlen holen, verschriften, Chronik "
    "schreiben.\n"
    "• `/aufnahme hilfe` — dieser Text.\n"
    "Wer nicht aufgezeichnet werden möchte, verlässt den Sprachkanal — außerhalb nehme "
    "ich nichts auf. Wer später dazukommt, hört die Ansage noch einmal. "
    f"Die Aufnahmen werden nach {recordings.RETENTION_TAGE} Tagen gelöscht.\n"
    "Meine Antworten sieht nur, wer den Befehl gegeben hat."
)

RAHMEN = ansage.KANAELE * ansage.BREITE


def _discord():
    # Lokal importiert wie der Spracherkenner: die Sprach-Abhängigkeiten liegen im Image,
    # aber nicht in jeder Dev-Installation — ohne sie bleibt der Rest startbar.
    try:
        import discord
    except ImportError as fehler:
        raise BotFehler(NICHT_INSTALLIERT) from fehler
    return discord


def _sprache_pruefen(discord) -> None:
    """Beim Start prüfen, was sonst erst im Sprachkanal auffällt.

    Fehlt PyNaCl oder davey, verbindet sich py-cord anstandslos und schreibt eine einzige
    Warnzeile ins Log; scheitern würde erst ``/aufnahme start``, mitten im Befehl und für
    den Aufrufer unsichtbar. Ein Bot, der nichts hören kann, soll das beim Start sagen.
    """
    fehlend = discord.utils.get_missing_voice_dependencies()
    if fehlend:
        raise BotFehler(SPRACHE_FEHLT.format(fehlend=", ".join(fehlend)))


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
        self._vc.start_recording(_senke(aufnahme), _abgeschlossen)

    def mitschnitt_beenden(self) -> None:
        self._vc.stop_recording()

    async def trennen(self) -> None:
        await self._vc.disconnect()


async def _abgeschlossen(senke, *args) -> None:
    """py-cord verlangt einen Rückruf; die Spuren schließt ``Aufnahme.beenden``."""


def _mitglied(quelle) -> consent.Member:
    """Aus py-cords Sprecher wird unser Mitglied — ohne Namen bleibt die Spur namenlos.

    ``VoiceData.source`` darf ``None`` sein, solange py-cord die SSRC noch keinem Konto
    zuordnen konnte. Geraten wird dann nichts: die Sekunden landen in einer Spur, die
    ehrlich »unbekannt« heißt, statt jemandem in den Mund gelegt zu werden.
    """
    if quelle is None:
        return consent.Member(id=UNBEKANNT, name=UNBEKANNT)
    return consent.Member(
        id=str(quelle.id), name=getattr(quelle, "display_name", None) or UNBEKANNT
    )


def _senke(aufnahme: Aufnahme):
    discord = _discord()

    class SpurSenke(discord.sinks.Sink):
        """Je Sprecher eine Spur — geschrieben wird auf die Platte, nicht in den Speicher.

        Die Basisklasse allein genügt py-cord 2.8 nicht mehr: der neue Empfangs-Router
        verlangt ``__sink_listeners__``, ``walk_children``, ``root`` und ``is_opus`` —
        Teile einer Senken-Schnittstelle, die in 2.8.1 noch **keine** mitgelieferte Senke
        erfüllt, auch ``WaveSink`` nicht (siehe Test und PR). Wir erfüllen sie hier von
        Hand: wir hören auf keines der Senken-Ereignisse, also ist die Liste leer, und
        Kinder-Senken gibt es nicht.
        """

        __sink_listeners__: tuple[tuple[str, str], ...] = ()

        @property
        def root(self):
            return self

        def walk_children(self):
            return ()

        def is_opus(self) -> bool:
            # Nein: wir wollen dekodiertes PCM, damit die Spur ohne weiteres Werkzeug
            # abspielbar und für die Transkription lesbar ist.
            return False

        def write(self, data, source) -> None:
            aufnahme.schreiben(_mitglied(source), data.pcm)

        def cleanup(self) -> None:
            self.finished = True

    return SpurSenke()


def antwortet(befehl):
    """Kein Befehl geht ohne Antwort aus — auch der, der stolpert.

    Ein Fehlschlag, den Discord als »denkt nach …« stehen lässt, ist schlimmer als eine
    Absage: mitten in der Runde weiß niemand, ob gerade aufgenommen wird oder nicht.
    """

    @functools.wraps(befehl)
    async def gefasst(ctx, *args, **kwargs):
        try:
            return await befehl(ctx, *args, **kwargs)
        except BotFehler as fehler:
            logger.warning("Befehl %s abgebrochen: %s", befehl.__name__, fehler)
            await ctx.respond(GESCHEITERT.format(grund=str(fehler)), ephemeral=True)
        except Exception as fehler:  # noqa: BLE001
            logger.exception("Befehl %s gescheitert", befehl.__name__)
            grund = UNERWARTET.format(typ=type(fehler).__name__)
            await ctx.respond(GESCHEITERT.format(grund=grund), ephemeral=True)
        return None

    return gefasst


class _Lauf:
    """Eine Instanz pro Gruppe — also höchstens eine Aufnahme zur Zeit."""

    def __init__(self) -> None:
        self.stimme: Sprachverbindung | None = None
        self.aufnahme: Aufnahme | None = None
        self.frist = None


def _zeitpunkt(nachricht) -> str:
    """Der Zeitpunkt der Nachricht in der Form, in der die Szenen ihre Trennlinien tragen.

    Er und nicht die Ankunft entscheidet über die Szene — sonst rutschte eine Woche später
    nachgetragene Nachricht ans Ende der Sitzung.
    """
    gestellt = getattr(nachricht, "created_at", None)
    if gestellt is None:
        return ""
    return gestellt.astimezone(UTC).isoformat(timespec="seconds")


def _nachricht(nachricht) -> chronik.Nachricht:
    return chronik.Nachricht(
        id=str(nachricht.id),
        text=nachricht.content or "",
        zeitpunkt=_zeitpunkt(nachricht),
        anhaenge=tuple(
            chronik.Anhang(filename=anhang.filename, size=anhang.size, speichern=anhang.save)
            for anhang in nachricht.attachments
        ),
        autor_id=str(nachricht.author.id),
    )


def _runde_des_ereignisses(config: Config, payload):
    """Nur die Runde der meldenden Gilde — ein Ereignis von nebenan gehört nicht hierher."""
    if payload.guild_id is None:
        return None
    return chronik.runde_der_gilde(config, payload.guild_id)


async def _thread_anlegen(ctx, name: str):
    """Der Thread ist die Sitzung — ohne ihn wird auch keine angelegt."""
    discord = _discord()
    try:
        return await ctx.channel.create_thread(name=name)
    except discord.HTTPException as fehler:
        raise chronik.ChronikFehler(chronik.KEIN_THREAD) from fehler


def _melder(ziel) -> Callable[[str], None]:
    """Der Lauf trägt sich in einem eigenen Faden zu; melden darf nur die Ereignisschleife."""
    schleife = asyncio.get_running_loop()

    def melden(text: str) -> None:
        asyncio.run_coroutine_threadsafe(ziel.send(text), schleife)

    return melden


def _passwortfrage(config: Config, runde, session_id: int):
    """Das Passwort wird erfragt, verbraucht und vergessen — es steht in keinem Feld.

    Deshalb ein Modal und kein Befehls-Argument: ein Argument stünde als Klartext in der
    Befehlszeile und damit im Verlauf des Kanals.
    """
    discord = _discord()

    class Passwortfrage(discord.ui.Modal):
        def __init__(self) -> None:
            super().__init__(
                discord.ui.InputText(
                    label=chronik.PASSWORT_FELD, placeholder=chronik.PASSWORT_HINWEIS
                ),
                title=chronik.PASSWORT_TITEL,
            )

        async def callback(self, interaction) -> None:
            try:
                meldung = chronik.abschluss_starten(
                    config,
                    runde,
                    session_id,
                    self.children[0].value,
                    melden=_melder(interaction.channel),
                )
            except BotFehler as fehler:
                meldung = GESCHEITERT.format(grund=str(fehler))
            except Exception as fehler:  # noqa: BLE001
                logger.exception("Abschluss der Sitzung gescheitert")
                meldung = GESCHEITERT.format(grund=UNERWARTET.format(typ=type(fehler).__name__))
            await interaction.response.send_message(meldung, ephemeral=True)

    return Passwortfrage()


def baue(config: Config):
    """Der Bot mit seinen Befehlen und dem Thread, der die Sitzung ist — ohne Verbindung."""
    discord = _discord()
    _sprache_pruefen(discord)
    absichten = discord.Intents.none()
    absichten.guilds = True
    absichten.voice_states = True
    # Ohne diese beiden ist der Thread ein leerer Behälter: Discord meldete weder die
    # Nachrichten noch ihren Inhalt, und jede Notiz käme leer an.
    absichten.messages = True
    absichten.message_content = True
    bot = discord.Bot(intents=absichten)
    lauf = _Lauf()
    gruppe = bot.create_group(GRUPPE, "Die Sitzung mitschneiden")
    chronikgruppe = bot.create_group(GRUPPE_CHRONIK, "Die Sitzung schreiben")

    @gruppe.command(name="start", description="Beitreten, ansagen, je Sprecher mitschneiden")
    @antwortet
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
        except BaseException:
            await stimme.trennen()
            raise
        lauf.stimme = stimme
        await ctx.respond(recorder.GESTARTET, ephemeral=True)

    @gruppe.command(name="stop", description="Aufnahme beenden und die Spuren einreihen")
    @antwortet
    async def stop(ctx) -> None:
        if lauf.aufnahme is None:
            await ctx.respond(LAEUFT_NICHT, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        meldungen = await recorder.stoppen(lauf.stimme, lauf.aufnahme)
        lauf.stimme = None
        lauf.aufnahme = None
        await ctx.respond(" ".join(meldungen), ephemeral=True)

    @gruppe.command(name="hilfe", description="Was der Bot tut und wie man ihn bedient")
    @antwortet
    async def hilfe(ctx) -> None:
        await ctx.respond(HILFE, ephemeral=True)

    @chronikgruppe.command(name="start", description="Sitzung anlegen und den Thread öffnen")
    @antwortet
    async def chronik_start(ctx, titel: str = "") -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        await ctx.defer(ephemeral=True)
        thread = await _thread_anlegen(ctx, chronik.threadname(titel))
        chronik.sitzung_anlegen(runde, str(thread.id), titel)
        await thread.send(chronik.ANGELEGT)
        await ctx.respond(chronik.THREAD_STEHT.format(thread=thread.mention), ephemeral=True)

    @chronikgruppe.command(
        name="fertig", description="Sitzung abschließen und die Chronik anstoßen"
    )
    @antwortet
    async def chronik_fertig(ctx) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        sitzung = chronik.sitzung_verlangen(runde, str(ctx.channel_id))
        await ctx.send_modal(_passwortfrage(config, runde, sitzung))

    @bot.slash_command(name=BEFEHL_SZENE, description="Die Trennlinie zur nächsten Szene ziehen")
    @antwortet
    async def szene(ctx, name: str = "") -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        sitzung = chronik.sitzung_verlangen(runde, str(ctx.channel_id))
        # Sichtbar für alle: die Trennlinie gehört in den Thread, nicht nur zu dem, der
        # sie gezogen hat.
        await ctx.respond(chronik.szene_setzen(runde, sitzung, name), ephemeral=False)

    @bot.event
    async def on_message(nachricht) -> None:
        if nachricht.author.bot or nachricht.guild is None:
            return
        runde = chronik.runde_der_gilde(config, nachricht.guild.id)
        if runde is None:
            return
        sitzung = chronik.sitzung_des_threads(runde, str(nachricht.channel.id))
        if sitzung is None:
            return
        try:
            meldungen = await chronik.aufnehmen(config, runde, sitzung, _nachricht(nachricht))
        except Exception as fehler:  # noqa: BLE001
            logger.exception("Nachricht im Sitzungs-Thread nicht abgelegt")
            grund = UNERWARTET.format(typ=type(fehler).__name__)
            await nachricht.reply(chronik.NICHT_ABGELEGT.format(grund=grund))
            return
        for meldung in meldungen:
            await nachricht.reply(meldung)

    @bot.event
    async def on_raw_message_edit(payload) -> None:
        # Roh und nicht ``on_message_edit``: das gäbe es nur für Nachrichten, die der Bot
        # seit seinem Start gesehen hat — eine Woche alte Notiz gehört auch dazu.
        text = (payload.data or {}).get("content")
        runde = _runde_des_ereignisses(config, payload)
        if text is not None and runde is not None:
            chronik.notiz_aendern(runde, str(payload.message_id), text)

    @bot.event
    async def on_raw_message_delete(payload) -> None:
        runde = _runde_des_ereignisses(config, payload)
        if runde is not None:
            chronik.notiz_entfernen(runde, str(payload.message_id))

    @bot.event
    async def on_ready() -> None:
        # Der Prozess läuft ohnehin durch — er ist damit der zuverlässigste Ort, die in
        # der Ansage zugesagte Frist einzuhalten, auch wenn der nächtliche Stapel steht.
        if lauf.frist is None:
            lauf.frist = asyncio.create_task(recordings.taeglich(config))

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
    discord = _discord()
    try:
        baue(config).run(config.discord_bot_token)
    except discord.errors.PrivilegedIntentsRequired as fehler:
        raise BotFehler(RECHTE_FEHLEN) from fehler
