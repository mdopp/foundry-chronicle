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
import contextlib
import functools
import logging
import wave
from collections.abc import Callable
from datetime import UTC
from pathlib import Path

from chronicle import consent, lebenszyklus, recordings
from chronicle.bot import (
    BotFehler,
    BotHaelt,
    ansage,
    chronik,
    einrichten,
    erinnern,
    recorder,
)
from chronicle.bot.recorder import Aufnahme, Kanal
from chronicle.config import Config
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

GRUPPE = "aufnahme"
GRUPPE_CHRONIK = "chronik"
GRUPPE_REGISTER = "register"
BEFEHL_SZENE = "szene"
BEFEHL_SUCHE = "suche"
BEFEHL_WER = "wer"
BEFEHL_ZUORDNUNG = "zuordnung"
BEFEHL_SETUP = "setup"

# Die Kennungen, mit denen ein Knopf oder ein Menü zurückkommt. Sie stehen nur in der
# Nachricht, aus der sie stammen — entschieden wird trotzdem gegen den Stand von jetzt.
KENNUNG_SCHILD = "eintrag"
KENNUNG_ENTSCHEIDUNG = "entscheidung"
KENNUNG_ZUORDNUNG = "zuordnung"
KENNUNG_KANAL = "kanal"
KENNUNG_LOESCHEN = "loeschen"

NICHT_INSTALLIERT = (
    "py-cord ist nicht installiert — im Image ist es dabei, "
    "lokal nachrüsten mit: pip install '.[discord]'"
)

SPRACHE_FEHLT = (
    "Dem Bot fehlt {fehlend} — ohne das spricht py-cord Discords Sprach-Verschlüsselung "
    "nicht: keine Ansage, keine Aufnahme. Im Image ist es dabei, lokal nachrüsten mit: "
    "pip install '.[discord]'"
)

# Discord weist die Anmeldung mit 4014 ab, py-cord meldet PrivilegedIntentsRequired.
# Danach **hört der Bot auf**: der Schalter liegt im Developer-Portal, kein Neustart
# bringt ihn um. Wer es trotzdem wieder versucht, verbindet sich in Minuten tausendfach —
# und Discord setzt den Token zurück. Genau so ist es am 2026-08-10 passiert.
RECHTE_FEHLEN = (
    "Discord verweigert die Anmeldung: dem Bot fehlt die Freigabe für den Nachrichten-Inhalt. "
    "Ohne sie kommt jede Notiz aus dem Thread leer an, deshalb fordert der Bot sie an. "
    "Einschalten unter https://discord.com/developers/applications → die Anwendung → Bot → "
    "Privileged Gateway Intents → Message Content Intent. "
    "Ich versuche es bis dahin nicht wieder — bitte danach den Dienst neu starten."
)

# Dasselbe von der anderen Seite: ein Token, den Discord ablehnt, wird durch Wiederholen
# nicht richtiger. Ein Anmeldeversuch im Sekundentakt ist der Weg, auf dem der nächste
# Token auch noch zurückgesetzt wird.
TOKEN_ABGELEHNT = (
    "Discord lehnt den Bot-Token ab. Er ist abgelaufen, zurückgesetzt oder falsch "
    "abgetippt — ein neuer steht unter https://discord.com/developers/applications → "
    "die Anwendung → Bot → Reset Token. "
    "Ich versuche es bis dahin nicht wieder — bitte danach den Dienst neu starten."
)

NICHT_IM_KANAL = "Du bist in keinem Sprachkanal — geh hinein und ruf mich noch einmal."
LAEUFT_SCHON = "Ich schneide schon mit."
LAEUFT_NICHT = "Es läuft gerade keine Aufnahme."

# Wie lange der Sprachkanal leer sein darf, bevor der Mitschnitt von selbst endet.
# Anderthalb Minuten, weil ein Wiederverbinden nach Netzwechsel oder Absturz des Clients
# darunter bleibt: wer in der Frist zurückkommt, findet seine Sitzung ungeschnitten vor.
# Länger zu warten bringt nichts — wer nach anderthalb Minuten nicht da ist, kommt auch
# nach zehn nicht — und kostet genau das, wogegen die Frist gebaut ist: Spuren aus Stille
# und eine Sprachverbindung, die niemand mehr braucht. Falsch zu liegen ist nur in eine
# Richtung teuer: wer zu spät zurückkommt, ruft ``/aufnahme start`` noch einmal und hört
# die Ansage dabei — was ohnehin das Richtige ist, denn die vorige galt einer Gruppe, die
# es zu dem Zeitpunkt nicht mehr gab.
LEER_FRIST = 90

# Beendet wird der **Mitschnitt**, nicht die Sitzung: der Thread bleibt offen, Notizen
# gehen weiter, und ``/chronik fertig`` bleibt eine Entscheidung der Runde. Sie hier
# mitzunehmen hieße, den ganzen Lauf ohne jemanden anzustoßen, der ihn wollte — und er
# verlangt ohnehin ein Passwort, das niemand eingibt, der schon gegangen ist.
LEER_BEENDET = (
    "Im Sprachkanal war niemand mehr — ich habe den Mitschnitt beendet und bin gegangen. "
    "Die Sitzung bleibt offen: hier weiterzuschreiben geht, und `/chronik fertig` bleibt "
    "eure Entscheidung. Für einen neuen Mitschnitt `/aufnahme start` — die Ansage läuft "
    "dann noch einmal."
)

LEER_GESCHEITERT = (
    "Im Sprachkanal war niemand mehr, aber das Beenden ist schiefgegangen — die Aufnahme "
    "gilt weiter als laufend, und ich bin womöglich noch im Kanal. Von selbst sehe ich "
    "erst wieder nach, wenn jemand den Kanal betritt und ihn erneut verlässt. Bitte "
    "einmal `/aufnahme stop` geben: das nimmt genau diesen Lauf und reiht die Spuren "
    "nach. Der Grund steht im Log des Bots."
)

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

BEFEHLE = (
    "• `/chronik start` — ich lege die Sitzung an und öffne den Thread dazu; ab dort wird "
    "jede Nachricht eine Notiz. Im Fenster davor kannst du das Foundry-Passwort geben — "
    "freiwillig.\n"
    "• `/szene <Name>` — die Trennlinie zur nächsten Szene.\n"
    "• `/aufnahme start` — ich komme in deinen Sprachkanal, spiele eine hörbare Ansage "
    "und schneide **erst danach** mit, je Sprecherin und Sprecher eine eigene Spur.\n"
    "• `/aufnahme stop` — ich höre auf und gehe wieder; die Spuren wandern in den "
    "nächtlichen Lauf und werden zu Text. Ist niemand außer mir mehr im Sprachkanal, "
    "höre ich nach einer kurzen Frist von selbst auf und sage es im Thread.\n"
    "• `/chronik fertig` — Sitzung abschließen: läuft noch eine Aufnahme, beende ich sie "
    "zuerst und reihe die Spuren ein; danach Zahlen holen, verschriften, Chronik "
    "schreiben. Nach dem Passwort frage ich nur, wenn beim Start keines kam.\n"
    "• `/suche <Wort>` — ich sehe in Notizen, Diktaten, Chroniken und im Register nach; "
    "jeder Treffer führt dorthin zurück, wo er steht.\n"
    "• `/wer <Name>` — was im Register über einen Namen steht.\n"
    "• `/register offen` — Vorschläge fürs Register bestätigen oder verwerfen.\n"
    "• `/zuordnung` — festhalten, wer von euch welchen Foundry-Spieler spielt.\n"
    "• `/setup` — Foundry-Adresse, Benutzer, Zustellkanal und Uhrzeit ändern; "
    "dafür braucht es das Recht, diesen Server zu verwalten.\n"
    "• `/chronik loeschen` — alles von dieser Runde löschen, nach Rückfrage; nur für die "
    "Administration dieses Servers.\n"
    "• `/aufnahme hilfe` — alles noch einmal in Ruhe.\n"
)

HILFE = (
    "**So schneide ich eine Sitzung mit**\n"
    f"{BEFEHLE}"
    "Wer nicht aufgezeichnet werden möchte, verlässt den Sprachkanal — außerhalb nehme "
    "ich nichts auf. Wer später dazukommt, hört die Ansage noch einmal. "
    f"Die Aufnahmen werden nach {recordings.RETENTION_TAGE} Tagen gelöscht.\n"
    "Meine Antworten sieht nur, wer den Befehl gegeben hat."
)

# Diese Nachricht steht im Kanal, **bevor** die Ansage läuft: wer nicht aufgezeichnet
# werden will, soll den Ausweg lesen können, solange noch nichts mitgeschnitten wird.
# Frist und Befehlsliste stehen deshalb nicht als zweite Kopie hier, sondern kommen aus
# derselben Quelle wie `/aufnahme hilfe` und die Ansage — eine Kopie driftet, und eine
# Zusage, die vom Verhalten abweicht, ist schlimmer als keine.
VORSTELLUNG = (
    "**Ich bin die Chronik dieser Runde.**\n"
    "Aus dem, was ihr sprecht, was ihr schreibt und was in eurem Foundry gewürfelt wird, "
    "mache ich nach dem Abend ein lesbares Sitzungsprotokoll — Zahlen kommen dabei "
    "ausschließlich aus dem Foundry-Log, erfinden kann ich sie nicht.\n"
    "**Gleich kommt eine hörbare Ansage. Erst danach schneide ich mit**, je Sprecherin "
    "und Sprecher eine eigene Spur. Wer nicht aufgezeichnet werden möchte, verlässt jetzt "
    "den Sprachkanal — außerhalb nehme ich nichts auf. "
    f"Die Tonspuren werden nach {recordings.RETENTION_TAGE} Tagen gelöscht.\n"
    "**So bedient ihr mich:**\n"
    f"{BEFEHLE}"
    "Meine Antworten sieht immer nur, wer den Befehl gegeben hat."
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
        # ``voice_client.channel`` folgt dem Bot, wenn ihn jemand verschiebt. Gemeint ist
        # aber immer der Kanal, dem die Aufnahme gehört: dort lief die Ansage, dort wurde
        # eingewilligt, und gegen dessen Kennung entscheidet auch ``on_voice_state_update``,
        # wer gekommen und wer gegangen ist. Würden »leer« und »gegangen« verschiedene
        # Kanäle meinen, griffe das Netz je nach Ziel des Verschiebens nie oder zu früh.
        self._kanal = voice_client.channel
        self.kanal = Kanal(
            guild_id=str(self._kanal.guild.id), id=str(self._kanal.id), name=self._kanal.name
        )

    def mitglieder(self) -> tuple[consent.Member, ...]:
        return tuple(
            consent.Member(id=str(wer.id), name=wer.display_name)
            for wer in self._kanal.members
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
        # Der zweite Anlauf nach einem gescheiterten Trennen soll das Trennen nachholen und
        # nicht daran scheitern, dass der erste den Mitschnitt schon angehalten hat —
        # py-cord wirft dafür »You are not recording«.
        if self._vc.is_recording():
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
        self.abschied = None
        self.leer = None


async def _mitschnitt_beenden(lauf: _Lauf, runde: Runde | None = None) -> tuple[str, ...]:
    """Mitschnitt beenden, Spuren einreihen, den Lauf leeren — leer, wenn nichts läuft.

    Mit ``runde`` nur, wenn die laufende Aufnahme dieser Runde gehört: ein Abschluss in
    der einen Gilde darf den Mitschnitt einer anderen nicht abreißen.
    """
    aufnahme, stimme = lauf.aufnahme, lauf.stimme
    if aufnahme is None:
        return ()
    if runde is not None and aufnahme.runde.id != runde.id:
        return ()
    # Erst beanspruchen, dann anhalten: ``recorder.stoppen`` gibt beim Trennen ab, und ein
    # zweiter Beender in dieser Lücke bekäme von py-cord »You are not recording«.
    lauf.aufnahme = None
    lauf.stimme = None
    _leerlauf_absagen(lauf)
    try:
        return tuple(await recorder.stoppen(stimme, aufnahme))
    except BaseException:
        # Ohne diese Rücknahme wäre der Anspruch das Ende: der Bot säße weiter im Kanal,
        # die Spuren lägen uneingereiht, und ``/aufnahme stop`` antwortete ab jetzt immer
        # »keine Aufnahme« — zu beenden wäre das nur noch durch einen Neustart.
        # Der abbestellte Wächter kommt dabei **nicht** zurück: einen neuen zu stellen
        # hieße, bei bleibendem Fehler alle neunzig Sekunden denselben Fehlschlag in den
        # Thread zu schreiben. Also sagt ``LEER_GESCHEITERT`` es stattdessen — von selbst
        # sieht erst wieder nach, wen ``on_voice_state_update`` neu bestellt.
        lauf.aufnahme, lauf.stimme = aufnahme, stimme
        raise


def _leerlauf_absagen(lauf: _Lauf) -> None:
    """Den wartenden Wächter abbestellen — seine Aufnahme gibt es so nicht mehr.

    Bliebe er liegen, unterdrückte er den Wächter der **nächsten** Aufnahme: die begänne
    innerhalb der Frist, alle gingen, und niemand sähe je nach. Sich selbst bestellt der
    Wächter dabei nicht ab — er beendet den Mitschnitt ja gerade.
    """
    faden, lauf.leer = lauf.leer, None
    if faden is not None and faden is not asyncio.current_task():
        faden.cancel()


def _menschen(lauf: _Lauf) -> tuple[consent.Member, ...]:
    """Wer außer dem Bot noch im Sprachkanal steht — ``mitglieder`` zählt ihn nicht mit."""
    return () if lauf.stimme is None else lauf.stimme.mitglieder()


async def _sagen(bot, aufnahme: Aufnahme, text: str) -> None:
    """Ein Satz in den Thread der Sitzung — dort liest die Runde ohnehin mit."""
    # Die Aufnahme hält ihre Runde seit Stunden. Ist sie inzwischen gelöscht und ihre
    # Kennung neu vergeben, führte die Frage nach dem Thread in eine fremde Kampagne.
    gemeint = lebenszyklus.dieselbe(aufnahme.runde)
    if gemeint is None:
        return
    thread_id = chronik.thread_der_sitzung(gemeint, aufnahme.session_id)
    if thread_id is None:
        logger.info("Sitzung %s hat keinen Thread — es bleibt ungesagt.", aufnahme.session_id)
        return
    kennung = int(thread_id)
    thread = bot.get_channel(kennung) or await bot.fetch_channel(kennung)
    await thread.send(text)


async def _abschied_bei_leere(bot, lauf: _Lauf, aufnahme: Aufnahme) -> None:
    """Nach der Frist noch einmal nachsehen — und dann Schluss.

    Noch einmal, weil die Frist genau dafür da ist: wer die Verbindung verliert und
    zurückkommt, soll keine zerschnittene Sitzung vorfinden. Und gegen *diese* Aufnahme,
    denn in der Frist kann eine neue begonnen haben, die diese Frist nichts angeht.
    """
    await asyncio.sleep(LEER_FRIST)
    if lauf.aufnahme is not aufnahme or _menschen(lauf):
        return
    logger.info("Sprachkanal #%s leer — der Mitschnitt endet.", aufnahme.kanal.name)
    try:
        meldungen = await _mitschnitt_beenden(lauf)
    except Exception:  # noqa: BLE001
        # Ein Faden nebenher hat niemanden, dem er den Fehlschlag antworten könnte. Ihn
        # als unabgeholte Ausnahme verfallen zu lassen hieße: die Runde erfährt nichts,
        # obwohl offen ist, ob noch mitgeschnitten wird. Also wenigstens in den Thread.
        logger.exception("Abschied bei leerem Sprachkanal gescheitert")
        with contextlib.suppress(Exception):
            await _sagen(bot, aufnahme, LEER_GESCHEITERT)
        return
    # Die Erfolgsmeldung steht außerhalb: umfasste ein ``try`` beides, machte ein zuckendes
    # ``thread.send`` aus einem gelungenen Ende einen gemeldeten Fehlschlag — und schickte
    # zu ``/aufnahme stop``, das dann »keine Aufnahme« antwortet. Bleibt sie ungesagt, ist
    # das ein fehlender Satz; ``LEER_GESCHEITERT`` wäre ein falscher.
    try:
        await _sagen(bot, aufnahme, " ".join((LEER_BEENDET, *meldungen)))
    except Exception:  # noqa: BLE001
        logger.exception("Der Abschied bei leerem Sprachkanal blieb ungesagt")


def _erledigt(faden) -> bool:
    """Ob dieser dauerhafte Faden neu gestartet gehört — nie gelaufen zählt auch."""
    return faden is None or faden.done()


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


def _vorstellungsziel(ctx, kanal):
    """Der Chat des Sprachkanals — und wo der keiner ist, der Ort, an dem der Befehl kam.

    Die Vorstellung darf nicht verschwinden, nur weil ein älterer oder ein Bühnenkanal
    keinen eigenen Chat hat: dann läse niemand den Ausweg vor der Ansage.
    """
    return kanal if callable(getattr(kanal, "send", None)) else ctx.channel


def _melder(ziel) -> Callable[[str], None]:
    """Der Lauf trägt sich in einem eigenen Faden zu; melden darf nur die Ereignisschleife."""
    schleife = asyncio.get_running_loop()

    def melden(text: str) -> None:
        asyncio.run_coroutine_threadsafe(ziel.send(text), schleife)

    return melden


def _dieselbe(config: Config, interaction, runde):
    """Die Runde, gegen die diese Ansicht gebaut wurde — sofern sie es noch ist.

    Jede Ansicht lebt eine Viertelstunde und schließt ihre ``Runde`` ein; die Kennung
    darunter kann in der Zeit gelöscht und an eine fremde Gilde neu vergeben sein.
    Entschieden wird deshalb gegen den Stand von jetzt, in **jedem** Rückruf.
    """
    return chronik.dieselbe_runde(config, getattr(interaction, "guild_id", None), runde)


async def _noch_dieselbe(config: Config, interaction, runde):
    """Wie ``_dieselbe``, und ein Klick, der nichts tut, sagt es auch."""
    gemeint = _dieselbe(config, interaction, runde)
    if gemeint is None:
        await interaction.response.edit_message(content=chronik.VERALTET, view=None)
    return gemeint


def _startfenster(config: Config, runde, titel: str):
    """Das Passwort wird beim Start erfragt — freiwillig, damit Foundry den Abend über offen ist.

    Ein Modal und kein Befehls-Argument: ein Argument stünde als Klartext in der
    Befehlszeile und damit im Verlauf des Kanals. Angelegt wird die Sitzung **hier**, nach
    dem Absenden — auch ohne Passwort, denn daran darf keine Sitzung scheitern.
    """
    discord = _discord()

    class Startfenster(discord.ui.Modal):
        def __init__(self) -> None:
            super().__init__(
                discord.ui.InputText(
                    label=chronik.START_FELD,
                    placeholder=chronik.START_HINWEIS,
                    required=False,
                ),
                title=chronik.START_TITEL,
            )

        async def callback(self, interaction) -> None:
            # Dieselbe Prüfung wie am Passwortfenster des Abschlusses: die Runde von vorhin
            # kann eine fremde geworden sein, und ihr ginge sonst das Passwort dieser Gruppe.
            gemeint = _dieselbe(config, interaction, runde)
            if gemeint is None:
                await interaction.response.send_message(chronik.VERALTET, ephemeral=True)
                return
            try:
                thread = await _thread_anlegen(interaction, chronik.threadname(titel))
                chronik.sitzung_anlegen(gemeint, str(thread.id), titel)
                gemerkt = chronik.passwort_merken(gemeint, self.children[0].value or "")
            except BotFehler as fehler:
                await interaction.response.send_message(
                    GESCHEITERT.format(grund=str(fehler)), ephemeral=True
                )
                return
            await thread.send(chronik.ANGELEGT)
            steht = chronik.THREAD_STEHT.format(thread=thread.mention)
            hinweis = chronik.MIT_FOUNDRY if gemerkt else chronik.OHNE_FOUNDRY
            await interaction.response.send_message(f"{steht} {hinweis}", ephemeral=True)

    return Startfenster()


async def _abschliessen(
    config: Config, runde, session_id: int, passwort: str | None, lauf: _Lauf, kanal
) -> str:
    """Erst den Mitschnitt beenden, dann den einen Lauf — die Reihenfolge steht fest.

    ``passwort`` ist ``None``, wenn beim Start eines gegeben wurde: dann wird nicht noch
    einmal gefragt und das Gemerkte auch nicht überschrieben.
    """
    meldungen: tuple[str, ...] = ()
    try:
        meldungen = await _mitschnitt_beenden(lauf, runde)
        meldung = chronik.abschluss_starten(
            config, runde, session_id, passwort, melden=_melder(kanal)
        )
    except BotFehler as fehler:
        meldung = GESCHEITERT.format(grund=str(fehler))
    except Exception as fehler:  # noqa: BLE001
        logger.exception("Abschluss der Sitzung gescheitert")
        meldung = GESCHEITERT.format(grund=UNERWARTET.format(typ=type(fehler).__name__))
    return " ".join((*meldungen, meldung))


def _passwortfrage(config: Config, runde, session_id: int, lauf: _Lauf):
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
            # Das Fenster trägt die Runde von vorhin mit. Ist es nicht mehr dieselbe, ginge
            # das Passwort dieser Gruppe an das Foundry einer fremden — die Adresse dorthin
            # steht in *ihrer* Runde.
            gemeint = _dieselbe(config, interaction, runde)
            if gemeint is None:
                await interaction.response.send_message(chronik.VERALTET, ephemeral=True)
                return
            antwort = await _abschliessen(
                config, gemeint, session_id, self.children[0].value, lauf, interaction.channel
            )
            await interaction.response.send_message(antwort, ephemeral=True)

    return Passwortfrage()


def _rechte(wer):
    """Was Discord diesem Mitglied auf diesem Server erlaubt — im Zwiegespräch nichts."""
    return getattr(wer, "guild_permissions", None)


def _darf_verwalten(wer) -> bool:
    """``/setup`` ist die Schranke vor dem Foundry-Passwort.

    Wer die Adresse setzt, bestimmt, welchem Server der Bot das Passwort der Spielleitung
    vorzeigt. Discords Vorgabe für einen Befehl ohne Angabe ist
    »jedes Mitglied« — deshalb steht hier eine Angabe.
    """
    rechte = _rechte(wer)
    return bool(getattr(rechte, "manage_guild", False) or getattr(rechte, "administrator", False))


def _darf_loeschen(wer) -> bool:
    """Und die zerstörerischste Handlung bekommt die strengere Schranke."""
    return bool(getattr(_rechte(wer), "administrator", False))


def _veranlasser(wer) -> str:
    """Wer eine Löschung ausgelöst hat — danach steht es nirgends mehr, die Runde ist fort."""
    if wer is None:
        return UNBEKANNT
    return f"{getattr(wer, 'display_name', None) or UNBEKANNT} [{getattr(wer, 'id', UNBEKANNT)}]"


def _begruessungskanal(gilde):
    """Wo die Gruppe den ersten Satz liest: der Systemkanal, sonst der erste beschreibbare.

    Discord garantiert keinen: der Systemkanal lässt sich abschalten, und in einem Kanal
    ohne Schreibrecht bliebe die Nachricht ein Fehlschlag im Log. Deshalb wird gesucht,
    statt geraten.
    """
    kandidaten = [
        kanal
        for kanal in (getattr(gilde, "system_channel", None), *getattr(gilde, "text_channels", ()))
        if kanal is not None
    ]
    for kanal in kandidaten:
        rechte = kanal.permissions_for(gilde.me)
        if getattr(rechte, "send_messages", False):
            return kanal
    return None


def _gildenname(ctx) -> str:
    return getattr(getattr(ctx, "guild", None), "name", None) or einrichten.RUNDE_OHNE_NAMEN


def _textkanaele(gilde) -> tuple[tuple[str, str], ...]:
    return tuple((str(kanal.id), kanal.name) for kanal in getattr(gilde, "text_channels", ()))


def _kanalansicht(config: Config, runde, gilde):
    """Ein Menü mit den Textkanälen dieser Gilde — die Wahl wirkt sofort.

    Und sie wirkt gegen den Stand von jetzt: ein Kanal aus dieser Gilde, in die Runde einer
    fremden geschrieben, schickte deren Chroniken künftig hierher. Anders als ein
    Löschknopf ist das keine einmalige Fehlhandlung, sondern eine dauerhafte.
    """
    discord = _discord()

    gebaut = discord.ui.Select(
        placeholder=einrichten.KANAL_WAEHLEN,
        custom_id=f"{KENNUNG_KANAL}:{runde.id}",
        options=[
            discord.SelectOption(label=schrift, value=wert, default=vorgewaehlt)
            for schrift, wert, vorgewaehlt in einrichten.kanalwahl(
                config, runde, _textkanaele(gilde)
            )
        ],
    )

    @_geklickt
    async def gewaehlt(interaction) -> None:
        gemeint = await _noch_dieselbe(config, interaction, runde)
        if gemeint is None:
            return
        satz = einrichten.kanal_setzen(gemeint, gebaut.values[0])
        await interaction.response.edit_message(content=satz, view=None)

    gebaut.callback = gewaehlt

    class Kanalansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(gebaut)

    return Kanalansicht()


async def _offenlegen(interaction) -> bool:
    """Die Offenlegung in den Kanal — sichtbar für die Gruppe, nicht nur für einen.

    Ob sie angekommen ist, entscheidet, ob die Runde wieder in Dienst geht; deshalb wird
    hier gefangen statt weitergereicht.
    """
    kanal = getattr(interaction, "channel", None)
    if kanal is None:
        return False
    try:
        await kanal.send(einrichten.OFFENLEGUNG)
    except Exception:  # noqa: BLE001
        logger.exception("Offenlegung nicht zugestellt")
        return False
    return True


def _einrichtungsfenster(config: Config, ctx):
    """Das Fenster für Adresse, Benutzer und Uhrzeit — nie für das Passwort.

    Das Modell steht hier nicht: es gehört seit #87 der Instanz und nicht der Runde.

    Das Passwort fehlt hier mit Absicht und nicht aus Vergesslichkeit: es wird beim
    Abschluss der Sitzung erfragt, verbraucht und vergessen. Ein Feld dafür gäbe es nur,
    wenn wir es behalten wollten.
    """
    discord = _discord()
    guild_id = ctx.guild_id
    gildenname = _gildenname(ctx)
    gilde = getattr(ctx, "guild", None)

    class Einrichtungsfenster(discord.ui.Modal):
        def __init__(self) -> None:
            super().__init__(
                discord.ui.InputText(
                    label=einrichten.FELD_ADRESSE,
                    placeholder=einrichten.HINWEIS_ADRESSE,
                    required=False,
                ),
                discord.ui.InputText(
                    label=einrichten.FELD_BENUTZER,
                    placeholder=einrichten.HINWEIS_BENUTZER,
                    required=False,
                ),
                discord.ui.InputText(
                    label=einrichten.FELD_UHRZEIT,
                    placeholder=einrichten.HINWEIS_UHRZEIT,
                    required=False,
                ),
                title=einrichten.SETUP_TITEL,
            )

        async def callback(self, interaction) -> None:
            adresse, benutzer, uhrzeit = (feld.value for feld in self.children)
            # Eine abgelaufene Runde wird hier gelöscht, mit Dateien und Zeilen — derselbe
            # Weg wie beim Wiedersehen und am Löschknopf, und deshalb nicht auf der
            # Ereignisschleife: solange sie rechnet, antwortet der Bot niemandem.
            fertig = await asyncio.to_thread(
                einrichten.einrichten,
                config,
                guild_id,
                gildenname,
                adresse=adresse,
                benutzer=benutzer,
                uhrzeit=uhrzeit,
            )
            meldung = fertig.meldung
            # Hier geht eine ruhende Runde wieder in Dienst — der eine Weg zurück, auf dem
            # keine Begrüßung steht. Die Offenlegung gehört deshalb hierher, und zwar in
            # den Kanal: sie ist eine Aussage an die Gruppe, nicht an den einen, der
            # eingerichtet hat. Freigegeben wird erst, wenn sie angekommen ist.
            if fertig.ruhte:
                if await _offenlegen(interaction):
                    einrichten.wieder_im_dienst(config, fertig.runde)
                else:
                    meldung = f"{meldung} {einrichten.STILL_GEBLIEBEN}"
            await interaction.response.send_message(
                f"{meldung} {einrichten.KANAL_FRAGE}",
                view=_kanalansicht(config, fertig.runde, gilde),
                ephemeral=True,
            )

    return Einrichtungsfenster()


def _loeschansicht(config: Config, runde):
    """Zwei Knöpfe und kein Befehl: eine Kampagne verschwindet nicht durch einen Vertipper.

    Und keiner der beiden entscheidet gegen den Stand von vorhin: die Ansicht lebt eine
    Viertelstunde, in der die Runde gelöscht und ihre Kennung neu vergeben sein kann.
    Geprüft wird deshalb beim Klick — die Runde, das Recht und die Gilde.
    """
    discord = _discord()

    ja = discord.ui.Button(
        label=einrichten.LOESCHEN_JA, custom_id=f"{KENNUNG_LOESCHEN}:{runde.id}:ja"
    )
    nein = discord.ui.Button(
        label=einrichten.LOESCHEN_NEIN, custom_id=f"{KENNUNG_LOESCHEN}:{runde.id}:nein"
    )

    @_geklickt
    async def bestaetigt(interaction) -> None:
        wer = getattr(interaction, "user", None)
        if not _darf_loeschen(wer):
            await interaction.response.edit_message(content=einrichten.NUR_ADMIN, view=None)
            return
        gemeint = _dieselbe(config, interaction, runde)
        if gemeint is None:
            await interaction.response.edit_message(content=einrichten.LOESCHEN_VERALTET, view=None)
            return
        # Dateien und Zeilen einer großen Runde: das dauert und gehört nicht auf die
        # Ereignisschleife — solange sie rechnet, antwortet der Bot niemandem.
        meldung = await asyncio.to_thread(
            einrichten.geloescht, config, gemeint, veranlasst_von=_veranlasser(wer)
        )
        await interaction.response.edit_message(content=meldung, view=None)

    @_geklickt
    async def verworfen(interaction) -> None:
        await interaction.response.edit_message(content=einrichten.LOESCHEN_ABGEBROCHEN, view=None)

    ja.callback = bestaetigt
    nein.callback = verworfen

    class Loeschansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(ja)
            self.add_item(nein)

    return Loeschansicht()


def _embed(gebaut: dict | None):
    return None if gebaut is None else _discord().Embed.from_dict(gebaut)


async def _antworten(ctx, antwort: erinnern.Antwort, view=None) -> None:
    """Antworten sieht nur, wer gefragt hat: eine Suche ist die Frage eines Einzelnen."""
    weiteres = {}
    if antwort.embed is not None:
        weiteres["embed"] = _embed(antwort.embed)
    if view is not None:
        weiteres["view"] = view
    await ctx.respond(antwort.text or None, ephemeral=True, **weiteres)


async def _ersetzen(interaction, antwort: erinnern.Antwort, view) -> None:
    """Der Knopf ändert die Nachricht, in der er steckt — die Antwort steht mit darin.

    Nicht zusätzlich: eine zweite Nachricht je Klick wäre nach fünf Entscheidungen ein
    Stapel, und die Liste daneben zeigte weiter, was es nicht mehr gibt.
    """
    await interaction.response.edit_message(
        content=antwort.text or None, embed=_embed(antwort.embed), view=view
    )


def _geklickt(arbeit):
    """Auch ein Knopf antwortet immer — sonst bleibt »denkt nach …« stehen."""

    async def gefasst(interaction) -> None:
        try:
            await arbeit(interaction)
        except Exception as fehler:  # noqa: BLE001
            logger.exception("Klick in einer Ansicht gescheitert")
            grund = UNERWARTET.format(typ=type(fehler).__name__)
            await interaction.response.send_message(GESCHEITERT.format(grund=grund), ephemeral=True)

    return gefasst


def _registeransicht(config: Config, runde, stand: erinnern.Offen):
    """Je Vorschlag eine Reihe: sein Name, die drei Arten, ein Nein."""
    if not stand.eintraege:
        return None
    discord = _discord()

    def schild(eintrag, zeile: int):
        return discord.ui.Button(
            label=erinnern.gekuerzt(eintrag.name, erinnern.KNOPF_GRENZE),
            row=zeile,
            disabled=True,
            custom_id=f"{KENNUNG_SCHILD}:{eintrag.id}",
        )

    def knopf(eintrag, art: str, schrift: str, zeile: int):
        gebaut = discord.ui.Button(
            label=schrift,
            row=zeile,
            custom_id=f"{KENNUNG_ENTSCHEIDUNG}:{eintrag.id}:{art or 'nein'}",
        )

        @_geklickt
        async def entschieden(interaction) -> None:
            gemeint = await _noch_dieselbe(config, interaction, runde)
            if gemeint is None:
                return
            satz = erinnern.entscheiden(gemeint, eintrag.id, art)
            naechste = erinnern.offen(gemeint, meldung=satz)
            await _ersetzen(
                interaction, naechste.antwort, _registeransicht(config, gemeint, naechste)
            )

        gebaut.callback = entschieden
        return gebaut

    class Registeransicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            for zeile, eintrag in enumerate(stand.eintraege):
                self.add_item(schild(eintrag, zeile))
                for art, schrift in erinnern.ENTSCHEIDUNGEN:
                    self.add_item(knopf(eintrag, art, schrift, zeile))

    return Registeransicht()


def _zuordnungsansicht(config: Config, runde, stand: erinnern.Zuordnung):
    """Je aufgenommener Person ein Menü mit den Foundry-Spielern dieser Runde."""
    if not stand.personen:
        return None
    discord = _discord()

    def menue(person, zeile: int):
        gebaut = discord.ui.Select(
            placeholder=erinnern.gekuerzt(
                erinnern.ZUORDNUNG_WAEHLEN.format(name=person.discord_name),
                erinnern.PLATZHALTER_GRENZE,
            ),
            row=zeile,
            custom_id=f"{KENNUNG_ZUORDNUNG}:{person.discord_user_id}",
            options=[
                discord.SelectOption(label=schrift, value=wert, default=vorgewaehlt)
                for schrift, wert, vorgewaehlt in erinnern.wahlmoeglichkeiten(person, stand.spieler)
            ],
        )

        @_geklickt
        async def gewaehlt(interaction) -> None:
            gemeint = await _noch_dieselbe(config, interaction, runde)
            if gemeint is None:
                return
            satz = erinnern.zuordnen(gemeint, person.discord_user_id, gebaut.values[0])
            naechste = erinnern.zuordnung(gemeint, meldung=satz)
            await _ersetzen(
                interaction, naechste.antwort, _zuordnungsansicht(config, gemeint, naechste)
            )

        gebaut.callback = gewaehlt
        return gebaut

    class Zuordnungsansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            for zeile, person in enumerate(stand.personen):
                self.add_item(menue(person, zeile))

    return Zuordnungsansicht()


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
    registergruppe = bot.create_group(GRUPPE_REGISTER, "Das Register führen")

    @gruppe.command(name="start", description="Beitreten, ansagen, je Sprecher mitschneiden")
    @antwortet
    async def start(ctx) -> None:
        if lauf.aufnahme is not None:
            await ctx.respond(LAEUFT_SCHON, ephemeral=True)
            return
        # Dieselbe Schranke wie vor ``/chronik start``, und vor dem Beitreten: eine Gilde
        # ohne eigene Runde nimmt nicht auf, eine ruhende erst recht nicht.
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        kanal = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if kanal is None:
            await ctx.respond(NICHT_IM_KANAL, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        stimme = Sprachverbindung(await kanal.connect())
        try:
            await _vorstellungsziel(ctx, kanal).send(VORSTELLUNG)
            lauf.aufnahme = await recorder.starten(config, stimme, runde)
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
        meldungen = await _mitschnitt_beenden(lauf)
        # Leer heißt: in der Zwischenzeit war ein anderer schneller — der leere Kanal etwa.
        # Das ist kein Fehlschlag, und so ausgesprochen zu werden verdient er auch nicht.
        await ctx.respond(" ".join(meldungen) or LAEUFT_NICHT, ephemeral=True)

    @gruppe.command(name="hilfe", description="Was der Bot tut und wie man ihn bedient")
    @antwortet
    async def hilfe(ctx) -> None:
        await ctx.respond(HILFE, ephemeral=True)

    @chronikgruppe.command(name="start", description="Sitzung anlegen und den Thread öffnen")
    @antwortet
    async def chronik_start(ctx, titel: str = "") -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        # Kein ``defer`` davor: ein Fenster geht nur als *erste* Antwort auf den Befehl.
        # Deshalb entsteht die Sitzung erst im Rückruf des Fensters.
        await ctx.send_modal(_startfenster(config, runde, titel))

    @chronikgruppe.command(
        name="fertig", description="Sitzung abschließen und die Chronik anstoßen"
    )
    @antwortet
    async def chronik_fertig(ctx) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        sitzung = chronik.sitzung_verlangen(runde, str(ctx.channel_id))
        if chronik.passwort_bereit(runde):
            await ctx.defer(ephemeral=True)
            await ctx.respond(
                await _abschliessen(config, runde, sitzung, None, lauf, ctx.channel),
                ephemeral=True,
            )
            return
        await ctx.send_modal(_passwortfrage(config, runde, sitzung, lauf))

    @chronikgruppe.command(
        name="loeschen", description="Alles von dieser Runde löschen, nach Rückfrage"
    )
    @antwortet
    async def chronik_loeschen(ctx) -> None:
        # Discord kennt ``default_member_permissions`` nur für den ganzen Befehl, und
        # ``/chronik start`` soll jedes Mitglied geben dürfen. Für diesen Unterbefehl steht
        # die Schranke deshalb hier — und noch einmal am Knopf, der wirklich löscht.
        if not _darf_loeschen(getattr(ctx, "author", None)):
            await ctx.respond(einrichten.NUR_ADMIN, ephemeral=True)
            return
        runde = chronik.runde_zum_loeschen(config, ctx.guild_id)
        await ctx.respond(
            einrichten.loeschfrage(), view=_loeschansicht(config, runde), ephemeral=True
        )

    @bot.slash_command(
        name=BEFEHL_SETUP,
        description="Foundry, Zustellkanal und nächtlichen Lauf einrichten",
        default_member_permissions=discord.Permissions(manage_guild=True),
    )
    @antwortet
    async def setup(ctx) -> None:
        # Ohne Gilde gibt es keine Runde zu beanspruchen — eine im Zwiegespräch angelegte
        # gehörte niemandem und stünde für immer da.
        if ctx.guild_id is None:
            await ctx.respond(einrichten.NUR_IM_SERVER, ephemeral=True)
            return
        # Die Angabe oben blendet den Befehl bei Discord aus; sie ist eine Vorgabe, die die
        # Serververwaltung überschreiben kann. Gerechnet wird deshalb auch hier.
        if not _darf_verwalten(getattr(ctx, "author", None)):
            await ctx.respond(einrichten.NUR_VERWALTUNG, ephemeral=True)
            return
        await ctx.send_modal(_einrichtungsfenster(config, ctx))

    @bot.slash_command(name=BEFEHL_SUCHE, description="In allem nachsehen, was geschrieben wurde")
    @antwortet
    async def suche(ctx, begriff: str = "") -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        await _antworten(ctx, erinnern.suche(runde, begriff))

    @bot.slash_command(name=BEFEHL_WER, description="Was im Register über einen Namen steht")
    @antwortet
    async def wer(ctx, name: str = "") -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        await _antworten(ctx, erinnern.wer(runde, name))

    @registergruppe.command(
        name="offen", description="Vorschläge fürs Register bestätigen oder verwerfen"
    )
    @antwortet
    async def register_offen(ctx) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        stand = erinnern.offen(runde)
        await _antworten(ctx, stand.antwort, _registeransicht(config, runde, stand))

    @bot.slash_command(
        name=BEFEHL_ZUORDNUNG, description="Festhalten, wer welchen Foundry-Spieler spielt"
    )
    @antwortet
    async def zuordnung(ctx) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        stand = erinnern.zuordnung(runde)
        await _antworten(ctx, stand.antwort, _zuordnungsansicht(config, runde, stand))

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
    async def on_guild_join(gilde) -> None:
        # Erst der Kanal, dann der Satz, und die Freigabe zuletzt: wieder im Dienst zu
        # sein, ohne dass die Gruppe die Offenlegung je gelesen hat, ist genau der Zustand,
        # für den es sie gibt. Ohne Kanal bleibt die Runde still — ``/setup`` bringt sie
        # zurück und sagt die Offenlegung dabei.
        kanal = _begruessungskanal(gilde)
        if kanal is None:
            logger.warning("Kein Kanal zum Begrüßen in %s", gilde.id)
            return
        # Eine abgelaufene Runde wird hier gelöscht, mit Dateien und Zeilen: nicht auf der
        # Ereignisschleife, sonst steht der ganze Bot währenddessen.
        zurueck = await asyncio.to_thread(einrichten.begruessung, config, str(gilde.id))
        await kanal.send(zurueck.text)
        if zurueck.wartet is not None:
            einrichten.wieder_im_dienst(config, zurueck.wartet)

    @bot.event
    async def on_guild_remove(gilde) -> None:
        einrichten.verabschieden(config.database_path, str(gilde.id))

    @bot.event
    async def on_ready() -> None:
        # Der Prozess läuft ohnehin durch — er ist damit der zuverlässigste Ort, die in
        # der Ansage zugesagte Frist einzuhalten, auch wenn der nächtliche Stapel steht.
        # Ein beendeter Faden ist nicht ``None``: ohne ``_erledigt`` bliebe eine Zusage
        # nach dem ersten Fehlschlag für immer liegen, und ``on_ready`` kommt bei jeder
        # Wiederverbindung noch einmal vorbei.
        if _erledigt(lauf.frist):
            lauf.frist = asyncio.create_task(recordings.taeglich(config))
        # Zwei Fristen, zwei Läufe: die eine gilt jeder Audiospur auf dieser Box, die
        # andere einer verabschiedeten Runde.
        if _erledigt(lauf.abschied):
            lauf.abschied = asyncio.create_task(lebenszyklus.taeglich(config))

    @bot.event
    async def on_voice_state_update(member, before, after) -> None:
        if lauf.aufnahme is None or member.bot:
            return
        unserer = lauf.aufnahme.kanal.id
        gekommen = after.channel is not None and str(after.channel.id) == unserer
        gegangen = before.channel is not None and str(before.channel.id) == unserer
        # Beides zugleich heißt: derselbe Kanal, nur stummgeschaltet oder verschoben.
        if gekommen and not gegangen:
            _leerlauf_absagen(lauf)
            await recorder.nachzuegler(
                config,
                lauf.stimme,
                lauf.aufnahme,
                consent.Member(id=str(member.id), name=member.display_name),
            )
        elif gegangen and not gekommen and not _menschen(lauf):
            # Immer neu stellen, nicht nur wenn keiner läuft: sonst zählt die Frist ab dem
            # ersten Gehen, und wer bei T=89 zurückkommt und bei T=89,5 wieder geht, hat
            # eine halbe Sekunde Karenz statt der zugesagten neunzig Sekunden.
            _leerlauf_absagen(lauf)
            lauf.leer = asyncio.create_task(_abschied_bei_leere(bot, lauf, lauf.aufnahme))

    return bot


def run(config: Config) -> None:
    logger.info("Aufnahme-Bot: verbinde mit dem Discord-Gateway")
    discord = _discord()
    try:
        baue(config).run(config.discord_bot_token)
    except discord.errors.PrivilegedIntentsRequired as fehler:
        raise BotHaelt(RECHTE_FEHLEN) from fehler
    except discord.errors.LoginFailure as fehler:
        raise BotHaelt(TOKEN_ABGELEHNT) from fehler
