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
import time
import wave
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from chronicle import consent, lebenszyklus, mitlauf, nightly, recordings, settings
from chronicle import runde as runden
from chronicle.bot import (
    BotFehler,
    BotHaelt,
    ansage,
    chronik,
    einrichten,
    erinnern,
    namen,
    recorder,
)
from chronicle.bot.recorder import Aufnahme, Kanal
from chronicle.config import Config
from chronicle.discord import grenzen
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

# Zwei Gruppen und sonst nichts (#272). Die Trennung verläuft zwischen »jetzt gerade«
# und »später nachsehen«, nicht mehr zwischen Ton und Text — und weil Discord nur die
# **obersten** Namen kennt, meldet py-cords Sammelaufruf beim Verbinden die acht alten
# (``aufnahme``, ``chronik``, ``register``, ``setup``, ``suche``, ``wer``, ``zuordnung``,
# ``szene``) damit von selbst ab: was nicht im Satz steht, den er schreibt, löscht er.
# Das trägt nur, solange kein Befehl ``guild_ids`` bekommt — ein gildeneigener stünde
# neben dem globalen Satz und überlebte ihn.
GRUPPE = "session"
GRUPPE_CHRONIK = "chronicle"
BEFEHL_START = "start"
BEFEHL_PAUSE = "pause"
BEFEHL_SZENE = "scene"
BEFEHL_SUCHE = "search"
BEFEHL_WER = "who"
BEFEHL_SETUP = "setup"

# Die Kennungen, mit denen ein Knopf oder ein Menü zurückkommt. Sie stehen nur in der
# Nachricht, aus der sie stammen — entschieden wird trotzdem gegen den Stand von jetzt.
KENNUNG_SCHILD = "eintrag"
KENNUNG_ENTSCHEIDUNG = "entscheidung"
KENNUNG_ZUORDNUNG = "zuordnung"
KENNUNG_BETRETEN = "betreten"
KENNUNG_KANAL = "kanal"
KENNUNG_QUELLE = "quelle"
KENNUNG_SPRACHE = "sprache"
KENNUNG_LOESCHEN = "loeschen"
KENNUNG_SITZUNG = "sitzung"

NICHT_INSTALLIERT = (
    "py-cord is not installed — the image ships it, locally add it with: pip install '.[discord]'"
)

SPRACHE_FEHLT = (
    "The bot is missing {fehlend} — without it py-cord does not speak Discord's voice "
    "encryption: no announcement, no recording. The image ships it, locally add it with: "
    "pip install '.[discord]'"
)

# Discord weist die Anmeldung mit 4014 ab, py-cord meldet PrivilegedIntentsRequired.
# Danach **hört der Bot auf**: der Schalter liegt im Developer-Portal, kein Neustart
# bringt ihn um. Wer es trotzdem wieder versucht, verbindet sich in Minuten tausendfach —
# und Discord setzt den Token zurück. Genau so ist es am 2026-08-10 passiert.
RECHTE_FEHLEN = (
    "Discord refuses the login: the bot lacks the permission for message content. "
    "Without it every note from the channel arrives empty, which is why the bot demands it. "
    "Switch it on at https://discord.com/developers/applications → the application → Bot → "
    "Privileged Gateway Intents → Message Content Intent. "
    "I will not try again until then — please restart the service afterwards."
)

# Dasselbe von der anderen Seite: ein Token, den Discord ablehnt, wird durch Wiederholen
# nicht richtiger. Ein Anmeldeversuch im Sekundentakt ist der Weg, auf dem der nächste
# Token auch noch zurückgesetzt wird.
TOKEN_ABGELEHNT = (
    "Discord rejects the bot token. It has expired, been reset or been mistyped — a new one "
    "is at https://discord.com/developers/applications → the application → Bot → Reset "
    "Token. "
    "I will not try again until then — please restart the service afterwards."
)

NICHT_IM_KANAL = "You are in no voice channel — go into one and call me again."

# Kein Fehlschlag: die Sitzung steht, es wird nur nichts gesprochen mitgeschnitten. Genau
# das ist der Fall, den `/session start` nicht mehr zur Reihenfolge macht (#272) — wer im
# Textkanal anfängt und erst später in den Sprachkanal geht, ruft ihn einfach noch einmal.
OHNE_SPRACHKANAL = (
    "Nothing is being recorded — you are in no voice channel. Go into one and call "
    "`/session start` again; the session then carries on, only the audio joins in."
)
LAEUFT_SCHON = "I am already recording."
LAEUFT_NICHT = "No recording is running right now."

# Der Test schneidet mit, verwirft und trennt: liefe er in einen laufenden Mitschnitt
# hinein, nähme er ihm die Verbindung weg. Also wird gesagt, dass nichts geschieht — und
# es geschieht auch nichts.
PROBE_NICHT_STOEREN = (
    "I am recording right now — I will not touch that for a reception check; it would tear "
    "off the running recording. The recording carries on. After `/session pause`, gladly."
)

PROBE_LAEUFT = "I am already checking reception, that only takes a few seconds — then again."

# Wie lange der Sprachkanal leer sein darf, bevor der Mitschnitt von selbst endet.
# Anderthalb Minuten, weil ein Wiederverbinden nach Netzwechsel oder Absturz des Clients
# darunter bleibt: wer in der Frist zurückkommt, findet seine Sitzung ungeschnitten vor.
# Länger zu warten bringt nichts — wer nach anderthalb Minuten nicht da ist, kommt auch
# nach zehn nicht — und kostet genau das, wogegen die Frist gebaut ist: Spuren aus Stille
# und eine Sprachverbindung, die niemand mehr braucht. Falsch zu liegen ist nur in eine
# Richtung teuer: wer zu spät zurückkommt, ruft ``/session start`` noch einmal und hört
# die Ansage dabei — was ohnehin das Richtige ist, denn die vorige galt einer Gruppe, die
# es zu dem Zeitpunkt nicht mehr gab.
LEER_FRIST = 90

# Seit #271 endet hier der ganze Abend und nicht nur der Mitschnitt: den Abschluss zu
# vergessen war der häufigste Fehler, und wer schon gegangen ist, tippt ihn nicht mehr.
# Das Passwort dafür liegt seit dem Start im Merkzettel — niemand muss es noch eingeben.
LEER_BEENDET = (
    "Nobody was left in the voice channel — I have ended the recording and left. With that "
    "I am also closing the session. For a new evening `/session start`; for a new recording "
    "`/session start` — the announcement then runs once more."
)

# Der Satz an den Tisch, der nach dem Abschluss weitertippt (#288). Ohne ihn fiel jede
# Zeile hier stumm in nichts: keine Antwort, keine Logzeile, keine Zeile in der Datenbank.
# Getippte Notizen sind die einzige Eingabe einer Präsenzrunde, und der Moment, in dem sie
# wegfielen, war ausgerechnet das Abmoderieren — EP, Beute, »nächstes Mal«.
ABEND_IST_ZU = (
    "This evening is already closed — I closed it when the voice channel went empty. "
    "Anything written here from now on does **not** go into the chronicle; the finished "
    "version goes onto the session's thread. If this should become a new evening, "
    "use `/session start`."
)

# Nicht angehalten, sondern gesagt: der Betreiber hat entschieden, dass niemand ungefragt
# seine Spur verliert. Der Satz nennt keinen Namen und keine Kennung — er steht im Thread
# der Sitzung, und wer gemeint ist, weiß es, weil er als Einziger im Sprachkanal sitzt.
ALLEIN = (
    "There is only **one** person besides me left in the voice channel — and I am still "
    "recording. I say so because the announcement was made to a group that is not there "
    "any more: whoever does not want to be recorded like this gives `/session pause` or "
    "leaves the channel. If the others come back, the same recording carries on — whoever "
    "enters hears the announcement again."
)

LEER_GESCHEITERT = (
    "Nobody was left in the voice channel, but ending it went wrong — the recording still "
    "counts as running, and I may still be in the channel. By myself I will only look again "
    "when somebody enters the channel and leaves it again. Please give `/session pause` "
    "once: that takes exactly this run and queues the tracks. The reason is in the bot log."
)

# Verschieben ist kein Umzug der Einwilligung: die Ansage lief in **einem** Kanal, und nur
# dort hat jemand zugestimmt. Wer im neuen Kanal sitzt, hat sie nie gehört.
VERSCHOBEN = (
    "Somebody dragged me out of #{kanal} into another voice channel — that is why the "
    "recording has ended. The announcement and the consent happened in #{kanal}; in the new "
    "channel nobody heard it, so I do not record there. Whatever arrived after the move is "
    "in no track. The session stays open: writing on here works, and `/session done` "
    "remains your decision. If the new channel is to be recorded, give `/session start` "
    "there — the announcement then runs there."
)

VERSCHOBEN_GESCHEITERT = (
    "Somebody dragged me out of #{kanal} into another voice channel, but ending it went "
    "wrong — the recording still counts as running, and I may still be in the wrong "
    "channel. Nothing is being written there. Please give `/session pause` once: that takes "
    "exactly this run and queues the tracks. The reason is in the bot log."
)

# Ein Abriss ist kein Umzug: verschoben hat den Bot jemand, getrennt kann ihn auch das Netz
# haben. Beides endet gleich, begründet sich aber verschieden — und der Satz im Thread ist
# Wochen später die einzige Auskunft darüber, warum die Spuren an dieser Stelle aufhören.
GETRENNT = (
    "My connection to #{kanal} broke off — that is why the recording has ended. Whether "
    "somebody threw me out or the network twitched I cannot see from here. Whatever was "
    "spoken after the break is in no track. The session stays open: writing on here works, "
    "and `/session done` remains your decision. If recording is to carry on, give "
    "`/session start` — the announcement then runs once more."
)

GETRENNT_GESCHEITERT = (
    "My connection to #{kanal} broke off, but ending it went wrong — the recording still "
    "counts as running. Nothing more is being written. Please give `/session pause` once: "
    "that takes exactly this run and queues the tracks. The reason is in the bot log."
)

# Dasselbe Wort wie in ``consent`` und in der Chronik: was hier als Platzhalter an eine
# Spur kommt, muss beim Einreihen wieder als Platzhalter erkannt werden (#250).
UNBEKANNT = consent.UNBEKANNT

# Ein Befehl, der nicht antwortet, lässt Discord ewig »denkt nach …« anzeigen. Das ist
# der schlechteste Ausgang: niemand weiß, ob aufgenommen wird. Deshalb antwortet jeder
# Befehl, auch wenn er scheitert — der Grund in Nutzersprache, die Einzelheiten ins Log.
GESCHEITERT = (
    "That did not work: {grund} "
    "What you can do: try again — if it stays that way, the reason is in the bot log."
)

UNERWARTET = "unexpected error in the bot ({typ})."

# Der Nachtlauf meldet sich nur, wenn es etwas zu sagen gibt — eine geschriebene Chronik
# steht ohnehin im Kanal. Gesagt wird also, was **fehlt** (#287).
NACHTBERICHT = "From the nightly run:\n{zeilen}"

# Und die Frist meldet sich, wenn sie zugreift. Sieben Tage sind in der Ansage zugesagt;
# der Tag, an dem es so weit ist, darf nicht der stillste des Monats sein (#286).
FRIST_GERAEUMT = "The promised retention deadline has cleared up:\n{zeilen}"

# Steht schon ein Anfang im Kanal und bricht die Zustellung mittendrin ab, endet er mitten
# im Satz. Ein zerrissener Text, den niemand als zerrissen erkennt, ist schlimmer als eine
# fehlende Nachricht — also sagt der Abriss sich selbst an.
ABGERISSEN = (
    "⚠️ The text above is incomplete: only {zugestellt} of {ganz} parts got through, "
    "{fehlend} {fehlt} missing. What is missing is not here — the bot log names the reason."
)

# Neun Zeilen, und sie sind die ganze Bedienoberfläche (#272). Was hier fehlt, fehlt mit
# Absicht: `/session pause` und `/chronicle abgleich` sind Notnägel, `/chronicle
# sitzung-loeschen` ist der schmale Weg neben `/chronicle delete` — alle drei bleiben
# aufrufbar, aber eine Liste, die jeden Sonderfall führt, wird nicht mehr gelesen.
BEFEHLE = (
    "• `/session start` — I create the session and come into your voice channel: first an "
    "audible announcement, then one track per voice. From then on every message in this "
    "channel becomes a note. The window before it takes the Foundry password; if you give "
    "one, I post the open rolls from your Foundry while you play.\n"
    "• `/session scene <name>` — the dividing line to the next scene.\n"
    "• `/session done` — close the session: I end a running recording first; then fetch the "
    "numbers, transcribe, write the chronicle. I only ask for the password if **you** gave "
    "none at the start. If the voice channel goes empty, I do this by myself.\n"
    f"• `/session check` — listen for {recorder.PROBE_DAUER} seconds and tell you alone "
    "whether the audio really arrives here. That is announced too, and everything recorded "
    "is deleted immediately.\n"
    "• `/session help` — all of it again, at leisure.\n"
    "• `/chronicle search <word>` — I look through notes, dictations, chronicles and the "
    "register; every hit leads back to its place.\n"
    "• `/chronicle who <name>` — what the register holds about a name.\n"
    "• `/chronicle delete` — delete everything of this round, after a confirmation; for "
    "administrators only.\n"
    "• `/chronicle setup` — set up Foundry, channel, time, zone, source and the language of "
    "the content; for server managers only.\n"
)

# Der eine Satz, der rechtlich trägt (§201 StGB): ohne ihn ist die Vorstellung nur noch
# eine Ankündigung. Er steht deshalb als Konstante da und nicht als Halbsatz mitten im
# Absatz — was geteilt zugestellt wird, muss ihn nachweislich im **ersten** Stück haben,
# und das prüft ein Test gegen genau diese Konstante.
#
# Daraus folgt die Reihenfolge in **jedem** Text, der ihn führt: der Ausweg steht **vor**
# der Befehlsliste. Die Liste wächst mit jedem neuen Befehl und schiebt alles hinter sich
# irgendwann in eine zweite Nachricht; was vor ihr steht, kommt zuerst und damit sicher an.
#
# **Er steht hier auf Englisch und bleibt es** (#268): dieser Text ist die geschriebene
# Hälfte im Kanal. Die **gesprochene** Ansage folgt der Inhaltssprache der Runde und steht
# in ``chronicle.sprache`` — sie ist der Vorgang selbst, dieser Satz begleitet ihn.
AUSWEG = (
    "Whoever does not want to be recorded leaves the voice channel — outside it I record nothing."
)

HILFE = (
    "**This is how I record a session**\n"
    f"{AUSWEG} Whoever joins later hears the announcement again. "
    # Nicht in BEFEHLE und damit nicht in der Vorstellung: dort steht der Ausweg vorn,
    # und dieser Fall tritt erst mitten in der Sitzung ein. Gesagt wird er ohnehin,
    # wenn er eintritt.
    "If one person is left alone in the voice channel, I carry on recording and tell them "
    "so in the session's channel; `/session pause` ends it. "
    f"The recordings are deleted after {recordings.RETENTION_TAGE} days.\n"
    f"{BEFEHLE}"
    "Only whoever gave the command sees my replies."
)

# Diese Nachricht steht im Kanal, **bevor** die Ansage läuft: wer nicht aufgezeichnet
# werden will, soll den Ausweg lesen können, solange noch nichts mitgeschnitten wird.
# Frist und Befehlsliste stehen deshalb nicht als zweite Kopie hier, sondern kommen aus
# derselben Quelle wie `/session help` und die Ansage — eine Kopie driftet, und eine
# Zusage, die vom Verhalten abweicht, ist schlimmer als keine.
VORSTELLUNG = (
    "**I am this round's chronicle.**\n"
    "Out of what you speak, what you write and what is rolled in your Foundry I make a "
    "readable session log after the evening — numbers come exclusively from the Foundry "
    "log, I cannot invent them.\n"
    "**An audible announcement follows in a moment. Only after it do I record**, one track "
    f"per speaker. Until then there is time: {AUSWEG} "
    f"The audio tracks are deleted after {recordings.RETENTION_TAGE} days.\n"
    "**This is how you operate me:**\n"
    f"{BEFEHLE}"
    "Only whoever gave the command ever sees my replies."
)

# Steht im Kanal, **bevor** die Ansage läuft — wie die Vorstellung vor einer Aufnahme, und
# aus demselben Grund: hier wird zehn Sekunden lang wirklich aufgezeichnet. Dass alles
# gleich danach gelöscht wird, macht die Ansage nicht entbehrlich, sondern nur kurz.
PROBE_VORSTELLUNG = (
    "**Reception check.** I am checking whether your audio arrives here readably at all — "
    "from outside that cannot be seen otherwise. The audible announcement follows in a "
    f"moment, then I listen for {recorder.PROBE_DAUER} seconds and **delete everything "
    "again**: none of it is transcribed, none of it goes into the chronicle. Just speak a "
    f"few sentences during that time. {AUSWEG}"
)

# Die Vorstellung steht **öffentlich** im Kanal, die Absage kommt nur bei der Person an,
# die den Befehl gab. Bleibt die Ankündigung allein stehen, liest die Runde, dass gleich
# mitgeschnitten wird, und spielt den Abend in dem Glauben, er werde festgehalten — die
# Umkehrung dessen, wofür die Vorstellung überhaupt da ist. Also wird sie dort
# zurückgenommen, wo sie steht (#189).
#
# Der Wortlaut sagt nur, was in **jedem** dieser Fälle wirklich gilt: es läuft nichts, und
# es kommt nichts an. Über Ansage und Einwilligungsprotokoll steht hier absichtlich nichts
# — ob die Ansage schon lief, hängt daran, wo der Start stolperte, und ein Satz, der das
# pauschal verneinte, wäre genau die Sorte Zusage, die vom Verhalten abweicht.
WIDERRUF = (
    "⚠️ **Nothing will come of this: I am not recording.** What stands above is void "
    "with that — no recording is running, and none of it goes into the chronicle. "
    "Reason: {grund}"
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
    Warnzeile ins Log; scheitern würde erst ``/session start``, mitten im Befehl und für
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


def _wo_discord_uns_sieht(voice_client) -> str | None:
    """Der Kanal aus Discords eigenem Zustand des Bots — ``None``, wenn keiner da ist.

    Die zweite Quelle neben ``VoiceClient.channel``: ``state.parse_voice_state_update``
    schreibt sie in den Zwischenspeicher der Gilde, bevor es das Ereignis ausliefert, und
    unabhängig davon, in welchem Verbindungszustand der Voice-Client gerade steckt.

    ``VoiceClient.guild`` ist keine Eigenschaft des Clients, sondern eine Property über
    ``self.channel.guild`` — sie wirft, sobald py-cord den Kanal auf ``None`` gesetzt hat.
    Der Aufrufer hat ``channel`` da schon einmal gelesen, aber ``SpurSenke.write`` läuft im
    Empfangs-Thread, und dazwischen kann das Trennen liegen.
    """
    gilde = getattr(voice_client, "guild", None)
    zustand = getattr(getattr(gilde, "me", None), "voice", None)
    kanal = getattr(zustand, "channel", None)
    return None if kanal is None else str(kanal.id)


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

    async def namen(self, kennungen: Sequence[str]) -> Mapping[str, str]:
        """Zu den genannten Kennungen die Anzeigenamen — gezielt, nicht als Liste.

        Der Weg an ``members`` vorbei: ein Aufruf je Sprecher statt der ganzen
        Mitgliederliste jeder Gilde im Prozess (#250, siehe ``chronicle.bot.namen``).
        """
        return await namen.aufloesen(self._vc.client, kennungen)

    def anwesende(self) -> tuple:
        """Dieselben, aber als Discord-Mitglieder — nur an sie ist eine Frage zustellbar.

        ``mitglieder`` gibt Kennung und Name her und sonst nichts; das ist die Form, in der
        ``recorder`` rechnet, und sie kennt Discord mit Absicht nicht. Wer beim Betreten
        gefragt werden soll, wird aber angeschrieben, und dafür braucht es das Mitglied
        selbst.
        """
        return tuple(wer for wer in self._kanal.members if not wer.bot)

    def im_kanal(self) -> bool:
        """Ob der Bot noch dort sitzt, wo angesagt und eingewilligt wurde.

        Zwei Quellen, und keine allein. Verschiebt ein Administrator den Bot, trägt
        py-cord den neuen Kanal in ``voice_client.channel`` ein, bevor das Ereignis bei
        uns ankommt — aber nur, solange die Verbindung im Zustand ``connected`` steht.
        Fällt die Verschiebung mit einer Voice-Server-Migration zusammen, lässt
        ``voice/state.py`` den Kanal im Zustand ``got_voice_server_update`` unverändert,
        und der Voice-Client zeigt weiter auf den alten. Discords eigenen Zustand des Bots
        schreibt ``guild._update_voice_state`` dagegen immer, und zwar **vor** der
        Auslieferung des Ereignisses. Was danach ankommt, ist ein Rahmen, für den niemand
        zugestimmt hat.
        """
        jetzt = self._vc.channel
        if jetzt is None or str(jetzt.id) != self.kanal.id:
            return False
        gemeldet = _wo_discord_uns_sieht(self._vc)
        # Kein zwischengespeicherter Zustand ist kein Beleg für einen Umzug — dann bleibt
        # es bei dem, was der Voice-Client sagt. Erreichbar ist das nicht über ein
        # fehlendes ``guild.me`` — py-cord legt das Selbst-Mitglied bedingungslos ab —,
        # sondern über den **Zielkanal**: kennt ``guild._channels`` ihn nicht, wirft
        # ``_update_voice_state`` den Zustand ganz hinaus, statt ihn umzuschreiben. Fällt
        # das mit dem Zustand ``got_voice_server_update`` zusammen, bleibt die Lücke —
        # beides zugleich, keins allein.
        return gemeldet is None or gemeldet == self.kanal.id

    def woanders(self) -> bool:
        """Ob der Bot in einem **anderen** Sprachkanal sitzt — sonst sitzt er in keinem.

        Nur zu fragen, wenn ``im_kanal`` schon Nein gesagt hat; die Frage ist dann allein
        die nach der Begründung. Discords eigener Zustand entscheidet sie, denn py-cord
        räumt ``voice_client.channel`` beim Trennen **nicht**: ein dort stehengebliebener
        alter Kanal belegt keinen Umzug, sondern nur, dass niemand aufgeräumt hat. Erst
        wenn Discord gar keinen Kanal für uns kennt, zählt der Voice-Client — der trägt
        beim Verschieben den neuen ein, bevor das Ereignis bei uns ankommt.
        """
        gemeldet = _wo_discord_uns_sieht(self._vc)
        if gemeldet is not None:
            return gemeldet != self.kanal.id
        jetzt = getattr(self._vc, "channel", None)
        return jetzt is not None and str(jetzt.id) != self.kanal.id

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
        senke = _senke(self, aufnahme)
        # Ohne Abschluss-Rückruf: der festgenagelte Stand ruft ihn als ``after(sink, *args)``
        # und nur, solange ``args`` nicht leer ist — ``start_recording`` ohne Zusatzargumente
        # setzt genau das leere Tupel. Ein Rückruf hier liefe also nie, und selbst wenn,
        # bekäme er die Senke statt des Fehlers. Was der Empfang taugt, sagt deshalb
        # ``mitschnitt_beenden``.
        self._vc.start_recording(senke)
        # ``Sink.client`` liest ``self.vc``, und ``opus.py`` prüft es mit einem ``assert``,
        # sobald das erste Paket kommt. Die veröffentlichte 2.8.1 setzt es im Empfangspfad
        # **nie** — ``sink._client = self.client`` steht in ``voice/receive/reader.py``
        # auskommentiert —, und ohne diese Zeile fiel der Empfänger am 2026-08-11 in der
        # ersten echten Runde nach 25 Sekunden mit einem nackten ``AssertionError`` um und
        # beendete den Mitschnitt von sich aus. Der festgenagelte Stand ruft ``sink.init``
        # in ``AudioReader.__init__`` selbst; die Zeile setzt dann denselben Wert noch
        # einmal. Sie bleibt, weil ``AudioReader.set_sink`` die Verdrahtung weiterhin
        # **nicht** vornimmt und der Preis eines vergessenen ``vc`` eine ganze Sitzung ist.
        senke.init(self._vc)

    def mitschnitt_beenden(self) -> bool:
        """Beendet den Mitschnitt und sagt, ob er überhaupt noch lief.

        Das »noch« ist die Auskunft: stirbt py-cords Paket-Router, ruft er in seinem
        ``finally`` selbst ``stop_recording`` — der Mitschnitt ist dann längst aus, ohne
        dass jemand hier etwas gesehen hätte. Genau das sah aus wie eine laufende Aufnahme.

        Der zweite Anlauf nach einem gescheiterten Trennen soll das Trennen nachholen und
        nicht daran scheitern, dass der erste den Mitschnitt schon angehalten hat —
        py-cord wirft dafür »You are not recording«.
        """
        if not self._vc.is_recording():
            return False
        self._vc.stop_recording()
        return True

    async def trennen(self) -> None:
        await self._vc.disconnect()


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


def _senke(stimme: Sprachverbindung, aufnahme: Aufnahme):
    discord = _discord()

    class SpurSenke(discord.sinks.Sink):
        """Je Sprecher eine Spur — geschrieben wird auf die Platte, nicht in den Speicher.

        Die Basisklasse allein genügt py-cord 2.8 nicht mehr: der neue Empfangs-Router
        verlangt ``__sink_listeners__``, ``walk_children``, ``root`` und ``is_opus`` —
        Teile einer Senken-Schnittstelle, die in der veröffentlichten 2.8.1 **keine**
        mitgelieferte Senke erfüllt, auch ``WaveSink`` nicht. Der festgenagelte Stand legt
        sie inzwischen in die Basisklasse; von Hand steht sie hier trotzdem weiter, weil
        sie beides bedient und nichts kostet: wir hören auf keines der Senken-Ereignisse,
        also ist die Liste leer, und Kinder-Senken gibt es nicht.
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
            # Der Rahmen, nicht der Lauf, ist die Einheit der Zustimmung: was ankommt,
            # nachdem der Bot aus seinem Kanal gezogen wurde, fällt weg statt in die Spur.
            # Das Ereignis, das den Mitschnitt beendet, kommt erst danach — bis dahin
            # wäre sonst genau das aufgezeichnet, wofür niemand gefragt wurde.
            if not stimme.im_kanal():
                return
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
            await _zustellen(ctx.respond, GESCHEITERT.format(grund=str(fehler)), ephemeral=True)
        except Exception as fehler:  # noqa: BLE001
            logger.exception("Befehl %s gescheitert", befehl.__name__)
            grund = UNERWARTET.format(typ=type(fehler).__name__)
            await _zustellen(ctx.respond, GESCHEITERT.format(grund=grund), ephemeral=True)
        return None

    return gefasst


class _Lauf:
    """Was in **einer** Runde gerade läuft — höchstens eine Aufnahme zur Zeit.

    Eine Instanz trägt mehrere Runden (#62/#63), also gibt es diesen Zustand je Runde
    und nicht je Prozess: sonst spräche eine Gilde für die andere mit.
    """

    def __init__(self) -> None:
        self.stimme: Sprachverbindung | None = None
        self.aufnahme: Aufnahme | None = None
        # Der Empfangstest steht bewusst **neben** der Aufnahme und nicht in ihr: er hält
        # seine Verbindung selbst und räumt sie selbst ab. Läge er in ``aufnahme``, reihte
        # ``/session pause`` seine Probespuren ein — genau das, was nie geschehen darf.
        self.probe = False
        self.leer = None
        # Die Aufnahme und die Personen, denen für sie schon gesagt wurde, dass sie allein
        # zurückbleiben. Beides zusammen: an der Person allein hinge der Vermerk über die
        # nächste Aufnahme hinaus und verschluckte deren Satz — dieselbe Falle, in die der
        # Wächter des leeren Kanals getappt ist. An der Aufnahme allein erführe die
        # **zweite** Zurückgebliebene nichts, weil für die erste schon vermerkt wäre.
        self.allein: tuple[Aufnahme, set[str]] | None = None
        # Und ebenso, wen diese Aufnahme beim Betreten schon zugeordnet oder gefragt hat.
        # Wieder an der Aufnahme und nicht allein an der Person: an ihr hinge der Vermerk
        # über das Ende hinaus und verschluckte die Frage der nächsten Aufnahme.
        self.gefragt: tuple[Aufnahme, set[str]] | None = None
        # Je offener Sitzung dieser Runde ein Beobachter von Foundry. Anders als der
        # Mitschnitt gibt es ihn mehrfach: eine Runde kann mehrere Sitzungen offen haben.
        self.stroeme: dict[int, object] = {}
        # Wann zuletzt eine Zeile in die laufende Sitzung fiel — die Uhr, an der der
        # Abschied bei leerem Sprachkanal misst, ob der Tisch noch abmoderiert (#288).
        # Roh und ohne Aufnahme daneben, anders als ``allein``/``gefragt``: ein alter Wert
        # verfällt hier von selbst, sobald er älter als die Frist ist.
        self.getippt: float | None = None
        # Die abgeschlossene Sitzung, für die schon gesagt wurde, dass der Abend zu ist.
        # Einmal je Abend und nicht je Zeile: zehn Minuten Abmoderieren sind zwanzig
        # Zeilen, und zwanzig gleiche Antworten wären selbst der Lärm.
        self.nachgesagt: int | None = None


class _Laeufe:
    """Ein ``_Lauf`` je Runde — und die Zuordnung, welcher Gilde welcher gehört.

    Der Kern der Trennung zwischen Runden an dieser Stelle: ein Befehl bekommt nur den
    Lauf **seiner** Runde zu fassen. Vorher stand hier ein einziger Lauf je Prozess, und
    damit brach ein ``/session pause`` der einen Gilde den Abend einer anderen ab.

    Prozessweit bleibt allein, was der Box gehört und keiner Gruppe: die beiden täglichen
    Fristen.
    """

    def __init__(self) -> None:
        self._nach_runde: dict[int, _Lauf] = {}
        self._nach_gilde: dict[str, int] = {}
        self.frist = None
        self.abschied = None

    def fuer(self, runde: Runde) -> _Lauf:
        """Der Lauf dieser Runde — angelegt, sobald sie zum ersten Mal etwas tut."""
        if runde.guild_id is not None:
            self._nach_gilde[str(runde.guild_id)] = runde.id
        return self._nach_runde.setdefault(runde.id, _Lauf())

    def fuer_gilde(self, guild_id) -> _Lauf | None:
        """Der Lauf dieser Gilde — ``None``, solange sie hier keinen hat.

        Für die Ereignisse aus Discord, die keine Runde mitbringen, sondern eine Gilde:
        gefragt wird nach *dieser* und nicht »läuft irgendwo etwas«.
        """
        runde_id = self._nach_gilde.get(str(guild_id))
        return None if runde_id is None else self._nach_runde.get(runde_id)


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
    # Der Vermerk ist eine Liste von Discord-Kennungen; ohne diese Zeile hielte der Prozess
    # sie nach dem Ende der Aufnahme weiter vor, ohne dass ihn noch jemand liest.
    allein, lauf.allein = lauf.allein, None
    gefragt, lauf.gefragt = lauf.gefragt, None
    _leerlauf_absagen(lauf)
    try:
        return tuple(await recorder.stoppen(stimme, aufnahme))
    except BaseException:
        # Ohne diese Rücknahme wäre der Anspruch das Ende: der Bot säße weiter im Kanal,
        # die Spuren lägen uneingereiht, und ``/session pause`` antwortete ab jetzt immer
        # »keine Aufnahme« — zu beenden wäre das nur noch durch einen Neustart.
        # Der abbestellte Wächter kommt dabei **nicht** zurück: einen neuen zu stellen
        # hieße, bei bleibendem Fehler alle neunzig Sekunden denselben Fehlschlag in den
        # Thread zu schreiben. Also sagt ``LEER_GESCHEITERT`` es stattdessen — von selbst
        # sieht erst wieder nach, wen ``on_voice_state_update`` neu bestellt.
        lauf.aufnahme, lauf.stimme, lauf.allein, lauf.gefragt = aufnahme, stimme, allein, gefragt
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


async def _zustellen(hinaus, text: str | None, *, zuletzt: dict | None = None, **jedes) -> None:
    """Ein Text nach Discord — in so vielen Nachrichten, wie seine Länge verlangt.

    Der eine Weg für alles, was hier hinausgeht: Kanal, Thread, Antwort auf einen Befehl,
    Erwiderung auf eine Notiz. Discord nimmt 2000 Zeichen und weist eine längere Nachricht
    ganz ab — ein Text, der mit jedem neuen Befehl wächst, darf daran nicht hängen (#109).
    Geteilt wird in der Reihenfolge des Textes: was zuerst dasteht, geht zuerst hinaus.

    ``zuletzt`` hängt Embed oder Ansicht an das **letzte** Stück — ein Knopf gehört unter
    den ganzen Text und nicht mitten hinein. Ohne Text bleibt genau ein Aufruf übrig; für
    ein Embed ohne Begleitsatz ist das der Normalfall.

    Mehrere Stücke heißen mehrere Aufrufe, und der zweite kann scheitern, wo der erste
    ankam. Das Teilen tauscht damit »gar nichts« gegen »die Hälfte« — deshalb meldet
    ``_abriss_melden`` den Rest, bevor der Fehlschlag weiterfliegt.
    """
    stuecke: tuple[str | None, ...] = grenzen.teile(text or "") or (None,)
    for nummer, stueck in enumerate(stuecke, start=1):
        anhang = {**jedes, **(zuletzt or {})} if nummer == len(stuecke) else jedes
        try:
            await hinaus(stueck, **anhang)
        except Exception:
            await _abriss_melden(hinaus, jedes, nummer - 1, len(stuecke))
            raise


def _abrisssatz(zugestellt: int, ganz: int) -> str:
    """»1 von 2 Teilen kamen durch, 1 fehlen« stand hier bis #208.

    Der Satz erklärt einer Gruppe, warum ihr Text mitten im Wort endet, und ist damit der
    öffentlichste dieses Bots — oft das Erste, was jemand von ihm bewusst liest. Ein Teil
    ist der häufigste Abriss, nicht der seltene: geteilt wird erst ab zwei Stücken.
    """
    fehlend = ganz - zugestellt
    return ABGERISSEN.format(
        zugestellt=zugestellt,
        ganz=ganz,
        fehlend=fehlend,
        fehlt="is" if fehlend == 1 else "are",
    )


async def _abriss_melden(hinaus, jedes: dict, zugestellt: int, ganz: int) -> None:
    """Was schon draußen ist, als unvollständig kenntlich machen.

    Vor dem ersten Stück ist nichts angekommen — dann bleibt es beim alten Alles-oder-nichts
    und der Fehlschlag ist beim Aufrufer ehrlich aufgehoben. Danach steht ein Anfang im
    Kanal, den niemand von einem ganzen Text unterscheiden kann; das Log hält die Zahlen
    fest, und der Hinweis sagt es denen, die nur den Kanal sehen. Scheitert auch er, ist der
    Kanal offenbar ganz zu — dann trägt das Log allein.
    """
    if not zugestellt:
        return
    logger.exception(
        "Zustellung abgerissen: %d von %d Stücken zugestellt, %d fehlen.",
        zugestellt,
        ganz,
        ganz - zugestellt,
    )
    with contextlib.suppress(Exception):
        await hinaus(_abrisssatz(zugestellt, ganz), **jedes)


async def _in_den_kanal(bot, aufnahme: Aufnahme, text: str) -> bool:
    """Ein Satz in den Kanal der Sitzung — dort liest die Runde ohnehin mit.

    Nicht ``_sagen``: das antwortet einem, der gerade etwas angeklickt hat. Hier gibt es
    niemanden, der wartet — der Beobachter meldet sich von selbst, an die Runde.

    Zurück kommt, ob es einen Weg dorthin gab. Wo der Satz nur begleitet, ist das
    gleichgültig; wo er die Bedingung des Weitermachens ist, hängt daran die Entscheidung.
    Ein **fortgeräumter** Kanal ist dabei kein Weg, sondern ein fehlender: Discords 404
    kommt beim nächsten Ereignis genauso wieder, und wer ihn wie ein Zucken behandelt,
    schneidet ewig weiter, ohne dass je etwas gesagt wurde. Alles andere fliegt weiter —
    ein zuckendes Discord ist beim nächsten Wechsel womöglich wieder da.
    """
    # Die Aufnahme hält ihre Runde seit Stunden. Ist sie inzwischen gelöscht und ihre
    # Kennung neu vergeben, führte die Frage nach dem Kanal in eine fremde Kampagne.
    gemeint = lebenszyklus.dieselbe(aufnahme.runde)
    if gemeint is None:
        return False
    return await _in_den_sitzungskanal(bot, gemeint, aufnahme.session_id, text)


async def _sitzungskanal(bot, runde: Runde, session_id: int):
    """Der Discord-Kanal, in dem diese Sitzung geführt wird — oder nichts."""
    kanal_id = chronik.kanal_der_sitzung(runde, session_id)
    if kanal_id is None:
        logger.info("Sitzung %s hat keinen Kanal — es bleibt ungesagt.", session_id)
        return None
    kennung = int(kanal_id)
    discord = _discord()
    try:
        return bot.get_channel(kennung) or await bot.fetch_channel(kennung)
    except discord.NotFound:
        logger.info("Kanal %s der Sitzung %s ist fort — es bleibt ungesagt.", kennung, session_id)
        return None


async def _in_den_sitzungskanal(bot, runde: Runde, session_id: int, text: str) -> bool:
    """Derselbe Weg, ohne Aufnahme: ein Befehl kennt seine Runde, aber keinen Mitschnitt."""
    kanal = await _sitzungskanal(bot, runde, session_id)
    if kanal is None:
        return False
    discord = _discord()
    try:
        await _zustellen(kanal.send, text)
    except discord.NotFound:
        logger.info("Der Kanal der Sitzung %s ist fort — es bleibt ungesagt.", session_id)
        return False
    return True


async def _allein_melden(bot, lauf: _Lauf, zurueck: consent.Member) -> None:
    """Der Zurückgebliebenen sagen, dass sie es ist — und sonst den Mitschnitt beenden.

    Der Vermerk wird **vor** dem Sagen gesetzt und bei gescheiterter Zustellung wieder
    zurückgenommen. Beides ist nötig und keines allein reicht: py-cord stellt jedes
    Sprachereignis als eigenen Task zu und hat den Mitglieder-Zwischenspeicher schon
    vorher aktualisiert — gehen zwei im selben Gateway-Schwung, sehen **beide** Handler
    dieselbe eine Verbliebene, und ein Vermerk hinter dem ``await`` fände in beiden nichts
    vor. Der Satz stünde zweimal im Thread. Nur davor zu vermerken verbrennte ihn dafür
    beim ersten zuckenden ``thread.send``, denn nachgeholt wird er nirgends; genommen wird
    er deshalb erst, wenn er ankam.

    Und erreicht er niemanden, weil die Sitzung keinen Thread hat oder die Runde fort ist,
    endet der Mitschnitt. Zugesagt war, dass sie es **erfährt** und widersprechen kann,
    nicht dass wir es versuchen; still weiterzuschneiden wäre genau der Zustand, gegen den
    die Zusage steht. Gesagt werden kann das Ende dann ebenso wenig — der Bot verlässt den
    Sprachkanal, und das sieht sie.
    """
    aufnahme = lauf.aufnahme
    vermerkt = lauf.allein[1] if lauf.allein is not None and lauf.allein[0] is aufnahme else set()
    if zurueck.id in vermerkt:
        return
    vermerkt.add(zurueck.id)
    lauf.allein = (aufnahme, vermerkt)
    try:
        angekommen = await _in_den_kanal(bot, aufnahme, ALLEIN)
    except Exception:  # noqa: BLE001
        vermerkt.discard(zurueck.id)
        logger.exception("Der Satz ans Alleinsein kam nicht durch — beim nächsten Wechsel neu")
        return
    if angekommen:
        return
    vermerkt.discard(zurueck.id)
    logger.warning(
        "Der Satz ans Alleinsein hat in Sitzung %s keinen Weg — es wird nicht weitergeschnitten.",
        aufnahme.session_id,
    )
    try:
        meldungen = await _mitschnitt_beenden(lauf)
    except Exception:  # noqa: BLE001
        logger.exception("Der Mitschnitt ohne Thread ließ sich nicht beenden")
        return
    logger.info("Mitschnitt ohne Thread beendet: %s", " ".join(meldungen))


async def _von_selbst_zuordnen(bot, aufnahme: Aufnahme, runde, kennung: str, stand) -> None:
    """Erst schreiben, dann sagen — und kommt der Satz nicht durch, wieder zurücknehmen.

    Über zwei Systeme hinweg — SQLite hier, Discord dort — gibt es kein gemeinsames
    Zusammenschreiben; eine der beiden Reihenfolgen muss danebengehen können. Gewählt ist
    die, deren Fehlerfall **Schweigen** ist und keine Lüge. Andersherum stünde der Vermerk
    im Thread, bevor feststeht, dass es die Zuordnung gibt — und ein Satz über eine
    Verbindung, die niemand mehr nachprüft, ist schlimmer als eine Zuordnung, von der
    niemand erfährt.

    Zurückgenommen wird über ``zuruecknehmen`` und nicht über ``zuordnen(…, KEINE)``:
    zwischen dem Schreiben und hier liegt ein Gang ans Netz, und wer in diesem Fenster über
    ``/chronicle zuordnung`` dieselbe Person auf ein anderes Konto legt, verlöre seine Entscheidung
    still. Genommen wird deshalb nur das eigene Geschriebene.

    Scheitert **auch** die Rücknahme, bleibt eine wahre Zuordnung ohne Ansage stehen. Das
    ist die schwächere, ehrliche Zusage dieses Weges: selten — Discord muss zweimal
    versagen, während die Datenbank arbeitet —, der Fehlerfall ist Schweigen statt einer
    Lüge, und nachgeholt wird nichts.
    """
    try:
        entstanden = erinnern.zuordnen(runde, kennung, stand.automatisch.id).spieler
    except Exception:  # noqa: BLE001
        logger.exception("Die Zuordnung beim Betreten ließ sich nicht festschreiben")
        return
    if entstanden is None:
        logger.info("Das Konto war beim Betreten schon vergeben — es bleibt beim Discord-Namen.")
        return
    try:
        gesagt = await _in_den_kanal(bot, aufnahme, stand.vermerk)
    except Exception:  # noqa: BLE001
        logger.exception("Der Vermerk zur Zuordnung beim Betreten kam nicht durch")
        gesagt = False
    if gesagt:
        return
    try:
        geloest = erinnern.zuruecknehmen(runde, kennung, entstanden.id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Zuordnung beim Betreten steht ohne Ansage: weder der Vermerk noch seine "
            "Rücknahme gingen durch"
        )
        return
    if not geloest:
        logger.info("Dort steht nicht mehr, was eben geschrieben wurde — es bleibt, wie es ist.")
        return
    logger.info("Ohne Weg in den Thread bleibt es beim Discord-Namen — Zuordnung zurückgenommen.")


async def _zuordnen_oder_fragen(bot, aufnahme: Aufnahme, mitglied, kennung: str) -> None:
    """Der eine Weg: entweder steht die Zuordnung von selbst, oder es wird gefragt.

    Gefragt wird **die betroffene Person** und nicht die Runde — wer wer ist, entscheidet
    man über sich selbst; deshalb geht die Frage ins Zwiegespräch und nicht in den Thread.
    Der Vermerk über eine von selbst entstandene Zuordnung geht umgekehrt in den Thread:
    eine Zuordnung, die niemand sieht, ist die stillschweigend übernommene Vermutung, gegen
    die es die Bestätigung überhaupt gibt.
    """
    # Die Aufnahme hält ihre Runde seit Stunden — ist sie inzwischen gelöscht und ihre
    # Kennung neu vergeben, schriebe die Zuordnung in eine fremde Kampagne.
    gemeint = lebenszyklus.dieselbe(aufnahme.runde)
    if gemeint is None:
        return
    stand = erinnern.betreten(gemeint, kennung)
    if stand.person is None:
        return
    if stand.automatisch is not None:
        await _von_selbst_zuordnen(bot, aufnahme, gemeint, kennung, stand)
        return
    await _zustellen(
        mitglied.send,
        erinnern.BETRETEN_FRAGE.format(kanal=aufnahme.kanal.name),
        zuletzt={"view": _betretensansicht(bot, aufnahme, gemeint, stand)},
    )


async def _zuordnung_klaeren(bot, lauf: _Lauf, aufnahme: Aufnahme, mitglied) -> None:
    """Wer den Sprachkanal betritt, wird zugeordnet oder gefragt — je Aufnahme genau einmal.

    Der Vermerk steht **vor** dem ersten ``await``: py-cord stellt jedes Sprachereignis als
    eigenen Task zu, und zwei Ereignisse derselben Person im selben Schwung fänden hinter
    dem ``await`` beide nichts vor — die Frage stünde zweimal im Zwiegespräch. Zurückgenommen
    wird er anders als beim Alleinsein **nicht**: geschlossene Direktnachrichten sind kein
    Zucken, sondern ein Dauerzustand, und bei jedem Stummschalten neu anzuklopfen wäre die
    schlechtere Zumutung. Keine Antwort ist auch eine — dann bleibt die Spur unter dem
    Discord-Namen, und das Protokoll sagt es so.
    """
    kennung = str(mitglied.id)
    schon = lauf.gefragt
    vermerkt = schon[1] if schon is not None and schon[0] is aufnahme else set()
    if kennung in vermerkt:
        return
    vermerkt.add(kennung)
    lauf.gefragt = (aufnahme, vermerkt)
    try:
        await _zuordnen_oder_fragen(bot, aufnahme, mitglied, kennung)
    except Exception as fehler:  # noqa: BLE001
        # Ohne Traceback und ohne Namen: der häufigste Grund ist ein Konto, das keine
        # Direktnachrichten annimmt, und das ist kein Fehlschlag, sondern eine Antwort.
        logger.warning(
            "Die Zuordnung beim Betreten kam nicht zustande (%s) — es bleibt beim Discord-Namen.",
            type(fehler).__name__,
        )


async def _beenden_und_sagen(
    bot, lauf: _Lauf, aufnahme: Aufnahme, beendet: str, gescheitert: str
) -> None:
    """Von selbst beenden und es im Thread begründen — der Weg beider Sicherheitsnetze."""
    try:
        meldungen = await _mitschnitt_beenden(lauf)
    except Exception:  # noqa: BLE001
        # Ein Faden nebenher hat niemanden, dem er den Fehlschlag antworten könnte. Ihn
        # als unabgeholte Ausnahme verfallen zu lassen hieße: die Runde erfährt nichts,
        # obwohl offen ist, ob noch mitgeschnitten wird. Also wenigstens in den Thread.
        logger.exception("Das Beenden von selbst ist gescheitert")
        with contextlib.suppress(Exception):
            await _in_den_kanal(bot, aufnahme, gescheitert)
        return
    if not meldungen:
        # Leer heißt: ein anderer war schneller. Dann gehört ihm auch der Satz dazu.
        return
    # Die Erfolgsmeldung steht außerhalb: umfasste ein ``try`` beides, machte ein zuckendes
    # ``thread.send`` aus einem gelungenen Ende einen gemeldeten Fehlschlag — und schickte
    # zu ``/session pause``, das dann »keine Aufnahme« antwortet. Bleibt sie ungesagt, ist
    # das ein fehlender Satz; die Fehlermeldung wäre ein falscher.
    #
    # Was dieser Fang **nicht** mehr verdeckt, ist die eigene Überlänge: eine Runde mit
    # dreißig Spuren reihte hier dreißig Meldungen aneinander, Discord wies die Nachricht
    # ab, und der Satz fiel still weg (#120). Verdecken kann er dafür jetzt einen halb
    # zugestellten Text — mehrere Stücke sind mehrere Aufrufe. Erkennbar bleibt das nicht
    # durch diesen Fang, sondern weil ``_zustellen`` den Abriss vorher selbst ansagt.
    try:
        await _in_den_kanal(bot, aufnahme, " ".join((beendet, *meldungen)))
    except Exception:  # noqa: BLE001
        logger.exception("Das Ende des Mitschnitts blieb ungesagt")


async def _sagen_dass_der_abend_zu_ist(lauf: _Lauf, runde: Runde, nachricht) -> None:
    """Eine Zeile nach dem Abschluss zählt nicht mehr — aber sie verschwindet auch nicht still.

    Bis #288 endete sie hier ohne Antwort, ohne Logzeile und ohne Zeile in der Datenbank,
    und zwar ausgerechnet beim Abmoderieren. Der Bot führt (#265): den Abschluss hat er
    selbst ausgelöst, also sagt er auch, dass er ihn ausgelöst hat, statt zu schweigen und
    darauf zu warten, dass jemand den richtigen Befehl errät.

    Einmal je Abend. Wer danach weiterschreibt, hat es gelesen; jede Zeile zu beantworten
    machte aus dem Hinweis genau den Lärm, gegen den er gebaut ist.
    """
    geschlossen = chronik.abgeschlossene_sitzung_im_kanal(runde, str(nachricht.channel.id))
    if geschlossen is None or lauf.nachgesagt == geschlossen:
        return
    lauf.nachgesagt = geschlossen
    await _zustellen(nachricht.reply, ABEND_IST_ZU)


def _tippfrist(lauf: _Lauf) -> float:
    """Wie viele Sekunden noch zu warten sind, weil der Tisch gerade noch schreibt.

    ``0`` oder weniger heißt: seit der letzten Zeile ist die volle Frist vergangen. Ein
    Wert aus einem früheren Abend verfällt damit von selbst — er ist längst zu alt.
    """
    if lauf.getippt is None:
        return 0.0
    return LEER_FRIST - (time.monotonic() - lauf.getippt)


async def _abschied_bei_leere(config: Config, bot, lauf: _Lauf, aufnahme: Aufnahme) -> None:
    """Nach der Frist noch einmal nachsehen — und dann Schluss, für den ganzen Abend.

    Noch einmal, weil die Frist genau dafür da ist: wer die Verbindung verliert und
    zurückkommt, soll keine zerschnittene Sitzung vorfinden. Und gegen *diese* Aufnahme,
    denn in der Frist kann eine neue begonnen haben, die diese Frist nichts angeht.

    Seit #271 endet hier nicht nur der Mitschnitt, sondern die **Sitzung**: Zahlen holen,
    verschriften, Chronik schreiben. Der häufigste Fehler war, den Abschluss zu vergessen
    und einen Abend ohne Zahlen zu bekommen — und wer schon gegangen ist, tippt ihn nicht
    mehr. Die zweite Prüfung oben gilt damit auch für den Abschluss.

    Seit #288 fragt sie außerdem, ob im Sitzungskanal noch getippt wird. Der leere
    Sprachkanal heißt weiterhin »Abend fertig« (#264) — aber das gespielte Ende und das
    Abmoderieren sind zweierlei: alle fallen aus dem Sprachkanal und der Tisch schreibt
    noch zehn Minuten EP, Beute und »nächstes Mal«. Wer tippt, ist da; die Frist läuft
    dann von der letzten Zeile an neu. Deshalb eine Schleife und kein zweites ``if``:
    zwischen zwei Zeilen liegen keine neunzig Sekunden, und einmal nachzuwarten schlösse
    den Abend mitten im Absatz.
    """
    await asyncio.sleep(LEER_FRIST)
    while True:
        if lauf.aufnahme is not aufnahme or _menschen(lauf):
            return
        rest = _tippfrist(lauf)
        if rest <= 0:
            break
        await asyncio.sleep(rest)
    # Die Sitzung statt des Kanalnamens (#206/#211): der Name beschreibt die Struktur
    # einer fremden Gilde, die Sitzungskennung niemanden. Sie genügt trotzdem, weil an
    # ihr Kanal, Spuren und Einwilligungsnachweis hängen — und im Nachweis steht der
    # Kanalname weiter, einen Schritt entfernt und dort mit Grund.
    logger.info("Sitzung %s: Sprachkanal leer — der Abend endet.", aufnahme.session_id)
    await _beenden_und_sagen(bot, lauf, aufnahme, LEER_BEENDET, LEER_GESCHEITERT)
    await _sitzung_von_selbst_abschliessen(config, bot, lauf, aufnahme)


async def _sitzung_von_selbst_abschliessen(
    config: Config, bot, lauf: _Lauf, aufnahme: Aufnahme
) -> None:
    """Den Abschluss anstoßen, den sonst ``/session done`` anstößt — mit denselben Mitteln.

    Kein Passwort von hier: ``None`` heißt »keines gegeben«, und der Abgleich liest dann
    den Merkzettel, in dem seit dem Start das Passwort dessen liegt, der die Sitzung
    eröffnet hat. Ein eigener Weg an ein Geheimnis entsteht dadurch nicht.

    Läuft die Sitzung nicht mehr, geschieht nichts: dann hat die Runde selbst
    abgeschlossen, und ein zweiter Lauf schriebe die Chronik ein zweites Mal.
    """
    gemeint = lebenszyklus.dieselbe(aufnahme.runde)
    if gemeint is None or not chronik.sitzung_laeuft(gemeint, aufnahme.session_id):
        return
    kanal = await _sitzungskanal(bot, gemeint, aufnahme.session_id)
    try:
        antwort = await _abschliessen(config, gemeint, aufnahme.session_id, None, lauf, kanal)
    except Exception:  # noqa: BLE001
        logger.exception("Der Abschluss von selbst ist gescheitert")
        return
    await _in_den_sitzungskanal(bot, gemeint, aufnahme.session_id, antwort)


async def _abschied_beim_kanalverlust(bot, lauf: _Lauf, aufnahme: Aufnahme, woanders: bool) -> None:
    """Wer seinen Kanal verliert, hört auf — und sagt dazu, wie er ihn verlor.

    Gezogen zu werden und getrennt zu werden endet gleich: hier hat niemand mehr
    zugestimmt, also wird nicht weitergeschnitten. Die **Begründung** ist aber nicht
    dieselbe, und im Thread steht sie Wochen später als einzige Auskunft darüber, warum
    die Spuren an dieser Stelle aufhören. Einen Abriss als Verschieben zu melden schickte
    die Runde in einen Sprachkanal nebenan, in dem nie jemand war (#120).
    """
    # Beide Zeilen nennen die Sitzung statt des Kanals (#206/#211). Welcher Kanal es war,
    # steht zwei Zeilen weiter im Thread — der gehört der Gilde, die ihre eigenen Kanäle
    # ohnehin kennt. Im Log des Betreibers trägt die Kennung genug: sie unterscheidet die
    # Läufe voneinander und führt zu Thread und Spuren, ohne eine Gilde zu beschreiben.
    if woanders:
        logger.warning(
            "Sitzung %s: der Bot wurde aus dem Sprachkanal verschoben — der Mitschnitt endet.",
            aufnahme.session_id,
        )
        beendet, gescheitert = VERSCHOBEN, VERSCHOBEN_GESCHEITERT
    else:
        logger.warning(
            "Sitzung %s: die Verbindung zum Sprachkanal ist abgerissen — der Mitschnitt endet.",
            aufnahme.session_id,
        )
        beendet, gescheitert = GETRENNT, GETRENNT_GESCHEITERT
    await _beenden_und_sagen(
        bot,
        lauf,
        aufnahme,
        beendet.format(kanal=aufnahme.kanal.name),
        gescheitert.format(kanal=aufnahme.kanal.name),
    )


async def _blick(config: Config, strom: chronik.Strom) -> chronik.Meldung:
    """Ein Blick nach Foundry im Faden daneben — abbrechen lässt er sich nicht, nur abwarten.

    ``cancel`` erreicht einen Faden nicht, der schon in ``asyncio.to_thread`` steckt: er
    liefe zu Ende und schriebe seinen Stand **hinter** den des Abschlusses, der gerade alles
    noch einmal ganz holt. Ein Wurf aus diesem Zwischenraum trüge danach »nicht mehr
    vorhanden« — eine Unwahrheit über einen Beleg. Der Abbruch wird deshalb angenommen und
    erst weitergereicht, wenn dieser Blick durch ist.
    """
    laufend = asyncio.ensure_future(asyncio.to_thread(chronik.ereignisse_abholen, config, strom))
    try:
        return await asyncio.shield(laufend)
    except asyncio.CancelledError:
        await asyncio.wait({laufend})
        raise


async def _ereignisstrom(config: Config, bot, lauf: _Lauf, strom: chronik.Strom) -> None:
    """Solange die Sitzung offen ist: nachsehen, was in Foundry fällt, und es einstellen.

    Der Blick selbst läuft in einem Faden daneben — er redet über das Netz mit einem
    fremden Server und über SQLite mit unserer Platte, und beides hielte sonst den ganzen
    Bot an.

    Er endet von selbst, sobald ein nächster nichts mehr brächte: kein Passwort mehr, eine
    fremde Welt, die Runde oder der Thread fort. Ein unerwarteter Fehler beendet ihn
    ebenso — er käme sonst alle zwei Minuten wieder, und ein Log, das im Minutentakt
    dieselbe Ausnahme schreibt, verdeckt jede andere. Verloren geht dabei nichts: der
    Abschluss holt die Zahlen ohnehin noch einmal ganz.
    """
    try:
        while True:
            await asyncio.sleep(chronik.STROM_ABSTAND)
            try:
                meldung = await _blick(config, strom)
                zugestellt = not meldung.text or await _in_den_sitzungskanal(
                    bot, strom.runde, strom.session_id, meldung.text
                )
            except Exception:  # noqa: BLE001
                logger.exception("Der Blick nach Foundry ist gescheitert — der Strom endet")
                return
            if not zugestellt or not meldung.weiter:
                return
    finally:
        # Auch beim Abbestellen: der Eintrag ist die Antwort auf »läuft für diese Sitzung
        # noch einer«, und ein Eintrag ohne Faden dahinter beantwortete sie falsch.
        lauf.stroeme.pop(strom.session_id, None)


def _strom_stellen(config: Config, bot, lauf: _Lauf, runde: Runde, session_id: int) -> None:
    """Den Beobachter dieser Sitzung bestellen — einen je Sitzung, und nur mit Passwort."""
    strom = chronik.Strom(runde=runde, session_id=session_id)
    lauf.stroeme[session_id] = asyncio.create_task(_ereignisstrom(config, bot, lauf, strom))


async def _strom_abbestellen(lauf: _Lauf, session_id: int) -> None:
    """Beim Abschluss ist Schluss: der Abgleich löst das Passwort ein und holt alles ganz.

    Gewartet wird, bis der Faden wirklich steht. ``cancel`` erreicht keinen, der gerade in
    ``asyncio.to_thread`` sitzt — der Blick liefe zu Ende und schriebe seinen Stand hinter
    den des Abschlusses. Ein Wurf aus dem Zwischenraum stünde danach als »nicht mehr
    vorhanden« in der Chronik, und das wäre eine Unwahrheit über einen Beleg.
    """
    faden = lauf.stroeme.pop(session_id, None)
    if faden is None:
        return
    faden.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await faden


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
        autor_name=getattr(nachricht.author, "display_name", None),
    )


def _rohzeitpunkt(daten: dict) -> str:
    """Derselbe Zeitpunkt aus der rohen Nutzlast — Discord schickt ihn als ISO-Text.

    Er entscheidet über die Szene, auch bei einer Änderung: nachgetragener Text gehört in
    die Szene der Nachricht und nicht in die, die gerade offen ist.
    """
    roh = daten.get("timestamp")
    if not roh:
        return ""
    return datetime.fromisoformat(roh).astimezone(UTC).isoformat(timespec="seconds")


def _vom_bot(daten: dict) -> bool:
    return bool((daten.get("author") or {}).get("bot"))


def _runde_des_ereignisses(config: Config, payload):
    """Nur die Runde der meldenden Gilde — ein Ereignis von nebenan gehört nicht hierher."""
    if payload.guild_id is None:
        return None
    return chronik.runde_der_gilde(config, payload.guild_id)


def _vorstellungsziel(ctx, kanal):
    """Der Chat des Sprachkanals — und wo der keiner ist, der Ort, an dem der Befehl kam.

    Die Vorstellung darf nicht verschwinden, nur weil ein älterer oder ein Bühnenkanal
    keinen eigenen Chat hat: dann läse niemand den Ausweg vor der Ansage.
    """
    return kanal if callable(getattr(kanal, "send", None)) else ctx.channel


async def _widerrufen(ziel, fehler: BaseException) -> None:
    """Die öffentliche Ankündigung dort zurücknehmen, wo sie steht.

    Gesagt wird der Grund, den auch die ephemere Absage nennt — bei einem erwarteten
    Fehler sein Satz, sonst nur die Art. Was im Fehler sonst noch stecken kann, bleibt im
    Log des Betreibers: der Kanal ist der öffentlichste Ort, den dieser Bot hat.

    Scheitert der Widerruf selbst, fliegt der **ursprüngliche** Fehler weiter — der ist
    die Auskunft, auf die der Aufrufer wartet. Dass die Ankündigung ohne ihn stehenblieb,
    steht dann im Log; mehr ist von hier aus nicht zu erreichen.
    """
    try:
        await _zustellen(ziel.send, WIDERRUF.format(grund=_grundtext(fehler)))
    except Exception:  # noqa: BLE001
        logger.exception("Die Vorstellung blieb im Kanal stehen — der Widerruf kam nicht durch")


def _grundtext(fehler: BaseException) -> str:
    """Bei einem erwarteten Fehler sein Satz, sonst nur die Art — der Rest bleibt im Log."""
    if isinstance(fehler, BotFehler):
        return str(fehler)
    return UNERWARTET.format(typ=type(fehler).__name__)


def _melder(ziel) -> Callable[[str], None]:
    """Der Lauf trägt sich in einem eigenen Faden zu; melden darf nur die Ereignisschleife."""
    if ziel is None:
        # Der Abschluss von selbst kennt keinen Aufrufer, und der Kanal der Sitzung kann
        # fortgeräumt sein. Der Lauf ist deshalb kein Fehlschlag — er sagt nur nichts.
        return lambda text: None
    schleife = asyncio.get_running_loop()

    def melden(text: str) -> None:
        _anstossen(_zustellen(ziel.send, text), schleife)

    return melden


def _anstossen(auftrag, schleife) -> None:
    """Eine Meldung aus dem Auftragsfaden in die Ereignisschleife — oder gar nicht.

    Der Lauf überlebt den Prozess nicht, aber er überlebt die Schleife: fällt der Bot
    gerade vom Gateway, ist sie zu, wenn die Meldung ankommt. Dann wird sie verworfen und
    die Nebenläufigkeit sauber abgeräumt — ein liegengebliebener Auftrag meldete sich
    später als »coroutine was never awaited« im Log und sagte über den Lauf nichts.
    """
    try:
        asyncio.run_coroutine_threadsafe(auftrag, schleife)
    except RuntimeError:
        auftrag.close()
        logger.warning("Die Meldung des Laufs kam nicht mehr an — die Ereignisschleife ist zu.")


def _melder_mit_register(config: Config, runde, ziel) -> Callable[[str], None]:
    """Wie ``_melder``, und danach die Frage, die früher ``/register offen`` war (#272).

    Registervorschläge entstehen im Lauf und warteten bislang darauf, dass jemand einen
    Befehl kennt, den niemand kannte: am 2026-08-18 lagen zwölf davon still da. Der Bot
    fragt deshalb selbst, im Kanal des Abends, sobald der Lauf durch ist — der Befehl
    dafür ist ersatzlos fort.
    """
    if ziel is None:
        return lambda text: None
    schleife = asyncio.get_running_loop()

    async def sagen(text: str) -> None:
        await _zustellen(ziel.send, text)
        await _register_nachfragen(config, runde, ziel)

    def melden(text: str) -> None:
        _anstossen(sagen(text), schleife)

    return melden


async def _register_nachfragen(config: Config, runde, ziel) -> None:
    """Was auf ein Ja oder Nein wartet, im Kanal des Abends — oder gar nichts.

    Ohne offenen Vorschlag bleibt es still: eine Nachricht »nichts zu tun« nach jedem
    Abend ist die Sorte Rauschen, wegen der die Liste vorher niemand aufschlug.
    """
    gemeint = lebenszyklus.dieselbe(runde)
    if gemeint is None:
        return
    try:
        stand = erinnern.offen(gemeint)
        ansicht = _registeransicht(config, gemeint, stand)
        if ansicht is None:
            return
        zuletzt = {"view": ansicht}
        if stand.antwort.embed is not None:
            zuletzt["embed"] = _embed(stand.antwort.embed)
        await _zustellen(ziel.send, stand.antwort.text, zuletzt=zuletzt)
    except Exception:  # noqa: BLE001
        # Der Lauf ist durch und gemeldet; dass die Nachfrage nicht kam, macht ihn nicht
        # zum Fehlschlag — sie ist ein Angebot, kein Ergebnis.
        logger.exception("Die Nachfrage zum Register kam nicht durch")


def _zustellkanal(config: Config, bot, runde):
    """Der Kanal aus ``/chronicle setup`` — dorthin, wo auch der Rückblick landet.

    Zwei Formen, wie in ``chronicle.discord.rueckblick``: ``setup`` schreibt die Id des
    gewählten Kanals, ältere Runden und die Umgebung tragen seinen Namen. Gesucht wird in
    der Gilde **dieser** Runde; »chronik« heißt in jeder zweiten Gilde ein Kanal.
    """
    gewaehlt = (settings.effective(config, runde).discord_recap_channel or "").strip().lstrip("#")
    if not gewaehlt or not runde.guild_id:
        return None
    gilde = bot.get_guild(int(runde.guild_id))
    if gilde is None:
        return None
    for kanal in getattr(gilde, "text_channels", ()):
        if str(kanal.id) == gewaehlt or kanal.name == gewaehlt:
            return kanal
    return None


async def _nacht_zustellen(config: Config, bot, runde, bericht: tuple[str, ...]) -> None:
    kanal = _zustellkanal(config, bot, runde)
    if kanal is None:
        logger.warning(
            "Kein Zustellkanal in Runde %s — was die Nacht zu sagen hat, bleibt liegen.", runde.id
        )
        return
    if bericht:
        await _zustellen(kanal.send, NACHTBERICHT.format(zeilen="\n".join(bericht)))
    await _register_nachfragen(config, runde, kanal)


def _nachtmelder(config: Config, bot) -> nightly.Meldung:
    """Was der Nachtlauf schreibt, fragt der Bot auch nach (#281) — und was fehlt, sagt er.

    Der Nachtlauf geht dieselbe Kette wie ``/session done`` und erzeugt damit dieselben
    Registervorschläge — nur endete sein Weg in der Datenbank: die Zeile »N Vorschläge
    warten« stand im Nachtbericht und ging nirgends nach Discord. Ein reiner Notizabend
    erzeugte so Vorschläge, nach denen niemand je gefragt wurde. Gefragt wird deshalb
    hier, im Zustellkanal aus dem Setup.

    Denselben Weg nimmt seit #287 der Bericht der Nacht: eine bewusst nicht geschriebene
    Chronik und jeder Ton, der ohne Text liegen blieb. Beides stand vorher allein in
    ``job.result``, und der wird nirgends mehr gelesen.

    Der Faden des Nachtlaufs darf die Ereignisschleife nicht selbst anfassen; hängt der
    Bot gerade nicht am Gateway, bleibt es beim Eintrag in der Datenbank, und der nächste
    Anlass holt die Frage nach.
    """

    def danach(runde: Runde, bericht: tuple[str, ...] = ()) -> None:
        schleife = getattr(bot, "loop", None)
        if schleife is None or not getattr(schleife, "is_running", bool)():
            logger.warning("Ohne laufende Schleife bleibt der Bericht der Nacht liegen.")
            return
        _anstossen(_nacht_zustellen(config, bot, runde, bericht), schleife)

    return danach


def _fristmelder(config: Config, bot) -> recordings.Melder:
    """Was die Frist geholt hat, erfährt die Runde — im Kanal aus dem Setup (#286).

    Die Ansage sagt sieben Tage zu; am siebten Tag verschwindet die Stimme einer echten
    Person, und besonders die nie verschriftete Spur. ``recordings.sweep`` schreibt dazu
    seit jeher einen eigenen Satz — der fiel bis hierher in einen Rückgabewert, den
    niemand las.
    """

    def melden(runde: Runde, meldungen: tuple[str, ...]) -> None:
        schleife = getattr(bot, "loop", None)
        if schleife is None or not getattr(schleife, "is_running", bool)():
            logger.warning("Ohne laufende Schleife bleibt die Meldung der Frist liegen.")
            return
        kanal = _zustellkanal(config, bot, runde)
        if kanal is None:
            logger.warning("Kein Zustellkanal in Runde %s — die Frist räumt still auf.", runde.id)
            return
        _anstossen(
            _zustellen(kanal.send, FRIST_GERAEUMT.format(zeilen="\n".join(meldungen))), schleife
        )

    return melden


async def _vorschlaege_wieder_anschliessen(config: Config, bot) -> None:
    """Die Knöpfe von gestern hören nach einem Neustart wieder zu (#281).

    py-cord verliert mit dem Prozess jede Ansicht: die Nachricht steht weiter im Kanal,
    aber ihre Knöpfe laufen ins Leere, und einen Befehl, der die Liste zurückholte, gibt
    es seit #272 nicht mehr. Angemeldet wird die **vorderste** Seite der offenen
    Vorschläge — genau die, die auf der Nachricht steht: jede Entscheidung schreibt
    ``erinnern.offen`` neu hinein, also zeigt die lebende Nachricht immer sie.
    """
    for eine in runden.alle(config.database_path):
        if eine.gesperrt:
            continue
        try:
            ansicht = _registeransicht(config, eine, erinnern.offen(eine))
        except Exception:  # noqa: BLE001
            logger.exception("Die offenen Vorschläge der Runde %s blieben ohne Knöpfe", eine.id)
            continue
        if ansicht is not None:
            bot.add_view(ansicht)


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


def _sprachkanal(quelle):
    """Der Sprachkanal dessen, der gerade handelt — oder keiner, wenn er nirgends steht."""
    person = getattr(quelle, "user", None) or getattr(quelle, "author", None)
    return getattr(getattr(person, "voice", None), "channel", None)


def _wer(quelle) -> str:
    """Die Discord-Kennung dessen, der gerade handelt — leer, wo Discord keine nennt."""
    person = getattr(quelle, "user", None) or getattr(quelle, "author", None)
    return str(getattr(person, "id", "") or "")


async def _eines(interaction, text: str | None, **weiteres) -> None:
    """Antworten, ohne zu wissen, wie weit der Rückruf schon war.

    Nach einem ``defer`` weist Discord eine zweite *erste* Antwort ab; davor gibt es noch
    keine, die man nachreichen könnte. Wer beides fangen will, muss beides können. Ab dem
    zweiten Stück gilt ohnehin der nachgereichte Weg — die erste Antwort ist dann vergeben.
    """
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **weiteres)
        return
    await interaction.response.send_message(text, ephemeral=True, **weiteres)


async def _sagen(interaction, text: str, **zuletzt) -> None:
    """Wie ``_eines``, und lang genug für Discords Grenze geteilt."""
    await _zustellen(functools.partial(_eines, interaction), text, zuletzt=zuletzt)


def _gefenstert(rueckruf):
    """Auch ein Fenster antwortet immer — py-cords ``Modal.on_error`` tut es nicht.

    Der Fang liegt um den **ganzen** Rückruf und nicht nur um seinen Rumpf: auch der
    Vorspann — das Aufschieben, das Auflösen der Runde — geht auf Discord und auf die
    SQLite. Bleibt eine Ausnahme von dort ungefangen, sieht der Absender »This interaction
    failed« und weiß nicht, ob etwas entstanden ist.

    Ein einziges Schweigen bleibt und ist keines dieser Fälle: ein Fenster, das ohne
    Absenden geschlossen wird, ruft gar nichts auf. Und wenn schon das Antworten selbst
    nicht durchkommt, ist der Weg zu Discord zu, nicht der Fang zu eng.
    """

    @functools.wraps(rueckruf)
    async def gefasst(self, interaction) -> None:
        try:
            await rueckruf(self, interaction)
        except BotFehler as fehler:
            logger.warning("Rückruf eines Fensters abgebrochen: %s", fehler)
            await _sagen(interaction, GESCHEITERT.format(grund=str(fehler)))
        except Exception as fehler:  # noqa: BLE001
            logger.exception("Rückruf eines Fensters gescheitert")
            grund = UNERWARTET.format(typ=type(fehler).__name__)
            await _sagen(interaction, GESCHEITERT.format(grund=grund))

    return gefasst


async def _sitzung_eroeffnen(
    config: Config, bot, lauf: _Lauf, ziel, runde, titel: str, eingabe: str, wer: str
) -> tuple[bool, str]:
    """Sitzung, Passwort, Beobachter — ein Satz, der jeden Ausgang unterscheidbar macht.

    Zurück kommt zweierlei, weil ``/session start`` danach weitermacht: ob die Sitzung
    wirklich steht, und was dazu zu sagen ist. Ohne das erste finge der Mitschnitt auch
    dann an, wenn das Anlegen gescheitert ist — und schnitte in nichts hinein.

    Die Sitzung hängt seit #271 an dem Kanal, in dem der Befehl kam — im Regelfall der
    Chat des Sprachkanals, an dem die Runde ohnehin sitzt. Kein Thread mehr: damit fällt
    zugleich der Absturz weg, den ``/session start`` in einem Sprachkanal auslöste (#241).

    Der breite Fang ist das Sicherheitsnetz, das ``@antwortet`` sonst um den Befehlsrumpf
    legt: aus dem Rückruf eines Fensters entkäme eine Ausnahme in py-cords ``on_error``,
    das die Interaktion **nie** beantwortet — die Sitzung stünde und niemand erführe es.
    Scheitert dagegen erst die Ansage, steht die Sitzung schon; dann darf die Antwort
    nicht »versuch es noch einmal« sagen, sonst legt der zweite Anlauf eine zweite an.
    """
    try:
        sitzung = chronik.sitzung_anlegen(runde, str(ziel.channel.id), titel)
        gemerkt = chronik.passwort_merken(runde, eingabe, wer)
    except Exception as fehler:  # noqa: BLE001
        if not isinstance(fehler, BotFehler):
            logger.exception("Sitzungsstart gescheitert")
        return False, GESCHEITERT.format(grund=_grundtext(fehler))
    hinweis = chronik.starthinweis(config, runde, gemerkt)
    # Nur mit hinterlegtem Passwort: ohne eines käme der Beobachter beim ersten Blick an
    # keinen Server und beendete sich sofort. Der Strom hängt damit an derselben
    # Entscheidung wie die Zahlen selbst — wer es nicht gibt, spielt ohne beides weiter.
    if gemerkt:
        _strom_stellen(config, bot, lauf, runde, sitzung)
    try:
        # Öffentlich und nicht nur an den Aufrufer: ab jetzt wird jede Zeile in diesem
        # Kanal eine Notiz, und das muss lesen können, wer hier tippt.
        await _zustellen(ziel.channel.send, chronik.ANGELEGT)
    except Exception:  # noqa: BLE001
        logger.exception("Ansage zur neuen Sitzung nicht zugestellt")
        return True, f"{chronik.STUMM_ANGELEGT} {hinweis}"
    return True, f"{chronik.SITZUNG_STEHT} {hinweis}"


async def _mitschnitt_beginnen(config: Config, bot, lauf: _Lauf, ziel, runde) -> str:
    """In den Sprachkanal des Aufrufers, ansagen, mitschneiden — und nur in dieser Folge.

    Die Ansage ist die lesbare Hälfte der Einwilligung: sie nennt den Ausweg, solange noch
    nichts mitgeschnitten wird. Kam sie nicht durch, wird nicht gestartet, und was von ihr
    schon dasteht, wird widerrufen.

    Geantwortet wird mit einem Satz statt mit einer Ausnahme: seit #272 steht davor in
    demselben Befehl das Anlegen der Sitzung. Flöge der Fehler weiter, sagte die Absage
    »versuch es noch einmal« — und der zweite Anlauf legte eine zweite Sitzung an.
    """
    kanal = _sprachkanal(ziel)
    if kanal is None:
        return OHNE_SPRACHKANAL
    try:
        stimme = Sprachverbindung(await kanal.connect())
    except Exception as fehler:  # noqa: BLE001
        if not isinstance(fehler, BotFehler):
            logger.exception("Der Sprachkanal ließ sich nicht betreten")
        return GESCHEITERT.format(grund=_grundtext(fehler))
    ort = _vorstellungsziel(ziel, kanal)
    try:
        await _zustellen(ort.send, VORSTELLUNG)
        lauf.aufnahme = await recorder.starten(config, stimme, runde)
    except Exception as fehler:  # noqa: BLE001
        # Erst der Widerruf, dann das Aufräumen: ``trennen`` geht ans Netz und kann selbst
        # stolpern — danach käme er nie, und die Ankündigung bliebe stehen.
        await _widerrufen(ort, fehler)
        await stimme.trennen()
        if not isinstance(fehler, BotFehler):
            logger.exception("Der Mitschnitt begann nicht")
        return GESCHEITERT.format(grund=_grundtext(fehler))
    lauf.stimme = stimme
    return recorder.GESTARTET


async def _zuordnung_reihum(bot, lauf: _Lauf) -> None:
    """Die Frage »wer bist du in Foundry« an jede anwesende Person — **nach** der Antwort.

    Nicht davor: sie geht reihum ins Zwiegespräch, und wer den Befehl gab, soll nicht erst
    danach erfahren, dass mitgeschnitten wird. Scheitern kann sie hier nicht mehr — sie
    fängt selbst. Läuft nichts, gibt es auch niemanden zu fragen.
    """
    if lauf.aufnahme is None or lauf.stimme is None:
        return
    for wer in lauf.stimme.anwesende():
        await _zuordnung_klaeren(bot, lauf, lauf.aufnahme, wer)


async def _sitzung_starten(
    config: Config, bot, lauf: _Lauf, ziel, runde, titel: str, eingabe: str, wer: str
) -> str:
    """Ein Befehl statt einer Reihenfolge (#272): Sitzung anlegen **und** mitschneiden.

    Am 2026-08-18 lief eine echte Runde in die Reihenfolge-Falle — erst die Aufnahme, dann
    die Sitzung, und die vollständige Einwilligungs-Ansage stand schon im Kanal, als sich
    herausstellte, dass es keine Sitzung gibt. Es gibt jetzt keine Reihenfolge mehr.
    """
    steht, antwort = await _sitzung_eroeffnen(config, bot, lauf, ziel, runde, titel, eingabe, wer)
    if not steht:
        return antwort
    return f"{antwort} {await _mitschnitt_beginnen(config, bot, lauf, ziel, runde)}"


def _startfenster(config: Config, bot, lauf: _Lauf, runde, titel: str):
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

        @_gefenstert
        async def callback(self, interaction) -> None:
            # Aufgeschoben wird als Erstes: darunter liegen zwei REST-Runden, und die drei
            # Sekunden, die Discord der ersten Antwort lässt, reichen dafür nicht
            # verlässlich. Danach geht jede Antwort nachgereicht.
            await interaction.response.defer(ephemeral=True)
            # Dieselbe Prüfung wie am Passwortfenster des Abschlusses: die Runde von vorhin
            # kann eine fremde geworden sein, und ihr ginge sonst das Passwort dieser Gruppe.
            gemeint = _dieselbe(config, interaction, runde)
            if gemeint is None:
                await _sagen(interaction, chronik.VERALTET)
                return
            antwort = await _sitzung_starten(
                config,
                bot,
                lauf,
                interaction,
                gemeint,
                titel,
                self.children[0].value or "",
                _wer(interaction),
            )
            await _sagen(interaction, antwort)
            await _zuordnung_reihum(bot, lauf)

    return Startfenster()


async def _abschliessen(
    config: Config,
    runde,
    session_id: int,
    passwort: str | None,
    lauf: _Lauf,
    kanal,
    wer: str = "",
    merken: bool = True,
) -> str:
    """Erst den Mitschnitt beenden, dann den einen Lauf — die Reihenfolge steht fest.

    ``passwort`` ist ``None``, wenn beim Start eines gegeben wurde: dann wird nicht noch
    einmal gefragt und das Gemerkte auch nicht überschrieben. ``merken=False`` heißt, dass
    es schon im Merkzettel liegt und dort nicht mit neuer Frist erneuert werden darf.
    """
    # Vor allem anderen: von hier an holt der eine Lauf die Zahlen, und er verbraucht dabei
    # das Passwort. Ein Beobachter, der daneben weiterliefe, fände beim nächsten Blick
    # keines mehr vor und sagte es in einen Thread, dessen Sitzung gerade geschrieben wird.
    await _strom_abbestellen(lauf, session_id)
    meldungen: tuple[str, ...] = ()
    try:
        meldungen = await _mitschnitt_beenden(lauf, runde)
        meldung = chronik.abschluss_starten(
            config,
            runde,
            session_id,
            passwort,
            wer=wer,
            merken=merken,
            melden=_melder_mit_register(config, runde, kanal),
        )
    except BotFehler as fehler:
        meldung = GESCHEITERT.format(grund=str(fehler))
    except Exception as fehler:  # noqa: BLE001
        logger.exception("Abschluss der Sitzung gescheitert")
        meldung = GESCHEITERT.format(grund=UNERWARTET.format(typ=type(fehler).__name__))
    return " ".join((*meldungen, meldung))


def _passwortfrage(config: Config, runde, session_id: int, lauf: _Lauf, hinweis: str):
    """Das Passwort wird erfragt, verbraucht und vergessen — es steht in keinem Feld.

    Deshalb ein Modal und kein Befehls-Argument: ein Argument stünde als Klartext in der
    Befehlszeile und damit im Verlauf des Kanals.
    """
    discord = _discord()

    class Passwortfrage(discord.ui.Modal):
        def __init__(self) -> None:
            super().__init__(
                discord.ui.InputText(label=chronik.PASSWORT_FELD, placeholder=hinweis),
                title=chronik.PASSWORT_TITEL,
            )

        @_gefenstert
        async def callback(self, interaction) -> None:
            # Wie am Startfenster, und hier mit mehr Grund: darunter liegen das Beenden des
            # Mitschnitts und das Anstoßen des Laufs. Die drei Sekunden, die Discord der
            # ersten Antwort lässt, reichen dafür nicht verlässlich.
            await interaction.response.defer(ephemeral=True)
            # Das Fenster trägt die Runde von vorhin mit. Ist es nicht mehr dieselbe, ginge
            # das Passwort dieser Gruppe an das Foundry einer fremden — die Adresse dorthin
            # steht in *ihrer* Runde.
            gemeint = _dieselbe(config, interaction, runde)
            if gemeint is None:
                await _sagen(interaction, chronik.VERALTET)
                return
            antwort = await _abschliessen(
                config,
                gemeint,
                session_id,
                self.children[0].value,
                lauf,
                interaction.channel,
                _wer(interaction),
            )
            await _sagen(interaction, antwort)

    return Passwortfrage()


def _abgleichfenster(config: Config, runde, hinweis: str):
    """Dasselbe wie am Abschluss, nur ohne Sitzung dahinter — und aus demselben Grund.

    Ein Fenster und kein Befehls-Argument: ein Argument stünde als Klartext in der
    Befehlszeile und damit im Verlauf des Kanals.
    """
    discord = _discord()

    class Abgleichfenster(discord.ui.Modal):
        def __init__(self) -> None:
            super().__init__(
                discord.ui.InputText(label=chronik.PASSWORT_FELD, placeholder=hinweis),
                title=chronik.ABGLEICH_TITEL,
            )

        @_gefenstert
        async def callback(self, interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            # Wie am Abschlussfenster: ist es nicht mehr dieselbe Runde, ginge das Passwort
            # dieser Gruppe an das Foundry einer fremden — die Adresse dorthin steht in
            # *ihrer* Runde.
            gemeint = _dieselbe(config, interaction, runde)
            if gemeint is None:
                await _sagen(interaction, chronik.VERALTET)
                return
            await _sagen(
                interaction,
                chronik.abgleich_starten(
                    config,
                    gemeint,
                    self.children[0].value,
                    wer=_wer(interaction),
                    melden=_melder(interaction.channel),
                ),
            )

    return Abgleichfenster()


def _rechte(wer):
    """Was Discord diesem Mitglied auf diesem Server erlaubt — im Zwiegespräch nichts."""
    return getattr(wer, "guild_permissions", None)


def _darf_verwalten(wer) -> bool:
    """``/chronicle setup`` ist die Schranke vor dem Foundry-Passwort.

    Wer die Adresse setzt, bestimmt, welchem Server der Bot das Passwort der Spielleitung
    vorzeigt. Discords Vorgabe für einen Befehl ohne Angabe ist
    »jedes Mitglied« — deshalb steht hier eine Angabe.
    """
    rechte = _rechte(wer)
    return bool(getattr(rechte, "manage_guild", False) or getattr(rechte, "administrator", False))


def _darf_loeschen(wer) -> bool:
    """Und die zerstörerischste Handlung bekommt die strengere Schranke.

    Administration und nicht Gilden-Eigentum (#90): das Löschen ist der Weg einer Gruppe,
    ihre Daten fortzunehmen, und der darf nicht an einem einzigen Konto hängen, das
    übertragen sein oder nicht mehr vorbeikommen kann. Enger wäre es ohnehin nur auf dem
    Papier — wer den Bot aus der Gilde werfen darf, startet damit dieselbe Löschung, bloß
    mit dreißig Tagen Frist. Das Sofortige bekommt deshalb die strengere Schranke, das
    Langsame die Umkehrbarkeit.
    """
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


async def _begruessen(config: Config, kanal, guild_id: str) -> None:
    """Der erste Satz in einer Gilde — und erst danach Vermerk und Freigabe.

    Die Reihenfolge trägt zweimal dasselbe: als begrüßt zu gelten oder wieder im Dienst zu
    sein, ohne dass die Gruppe die Offenlegung je gelesen hat, ist genau der Zustand, für
    den es sie gibt.
    """
    # Eine abgelaufene Runde wird hier gelöscht, mit Dateien und Zeilen: nicht auf der
    # Ereignisschleife, sonst steht der ganze Bot währenddessen.
    zurueck = await asyncio.to_thread(einrichten.begruessung, config, guild_id)
    await _zustellen(kanal.send, zurueck.text)
    lebenszyklus.begruessung_vermerken(config.database_path, guild_id)
    if zurueck.wartet is not None:
        einrichten.wieder_im_dienst(config, zurueck.wartet)


async def _begruessung_nachholen(config: Config, bot) -> None:
    """Nachholen, was ``on_guild_join`` verpasst hat — einmal je Gilde, nicht je Neustart.

    Discord spielt den Beitritt nach einer Wiederverbindung nicht nach; fällt die
    Autorisierung in einen Neustart, steht der Bot in der Gilde und sagt nie ein Wort
    (#270). Hier und nicht in einem Befehl, weil nur hier bekannt ist, in welchen Gilden
    er steht — und weil eine Gruppe, die auf den richtigen Befehl kommen müsste, den Satz
    gerade nicht gelesen hat.

    Ein Fehlschlag hält weder die nächste Gilde noch den Start auf, und ohne Vermerk holt
    der nächste Anlauf ihn nach.
    """
    for gilde in bot.guilds:
        wer = lebenszyklus.Gilde(id=str(gilde.id), name=gilde.name)
        if not lebenszyklus.ungegruesst(config, wer):
            continue
        kanal = _begruessungskanal(gilde)
        if kanal is None:
            logger.warning("Kein Kanal zum Begrüßen in %s", wer.id)
            continue
        try:
            await _begruessen(config, kanal, wer.id)
        except Exception:  # noqa: BLE001
            logger.exception("Die nachgeholte Begrüßung blieb in %s ungesagt", wer.id)


async def _verwaiste_runde_uebernehmen(config: Config, bot) -> None:
    """Eine Runde aus der Zeit vor den Gilden zurückholen — und es der Gruppe sagen.

    Hier und nirgends sonst, weil nur hier bekannt ist, in wie vielen Gilden der Bot
    steht: ein Befehl kennt immer nur seine eigene. py-cord trägt die Gilden aus dem
    READY-Rahmen in den Zwischenspeicher und hält dieses Ereignis zurück, bis die
    GUILD_CREATEs durch sind — beim **ersten** ``on_ready`` steht die Liste also schon.

    Der Satz an die Gilde hängt hinten: kommt er nicht durch, ist die Runde trotzdem
    übernommen und die Übernahme steht im Log. Sie deswegen wieder zu lösen hieße, die
    Gruppe erneut vor eine leere Runde zu setzen — genau das, wogegen es die Übernahme
    gibt.
    """
    gilden = tuple(lebenszyklus.Gilde(id=str(gilde.id), name=gilde.name) for gilde in bot.guilds)
    if lebenszyklus.verwaiste_uebernehmen(config, gilden) is None:
        return
    kanal = _begruessungskanal(bot.guilds[0])
    if kanal is None:
        logger.warning("Kein Kanal, um die Übernahme zu sagen — sie steht nur im Log.")
        return
    try:
        await _zustellen(kanal.send, lebenszyklus.UEBERNOMMEN_GESAGT)
    except Exception:  # noqa: BLE001
        logger.exception("Die Übernahme blieb in der Gilde ungesagt")


def _gildenname(ctx) -> str:
    return getattr(getattr(ctx, "guild", None), "name", None) or einrichten.RUNDE_OHNE_NAMEN


def _textkanaele(gilde) -> tuple[tuple[str, str], ...]:
    return tuple((str(kanal.id), kanal.name) for kanal in getattr(gilde, "text_channels", ()))


def _einrichtungsansicht(config: Config, runde, gilde):
    """Drei Menüs unter dem Fenster: wohin die Chronik geht, woher die Zahlen kommen, welche
    Sprache der Inhalt hat.

    Alle drei wirken sofort und alle drei gegen den Stand von jetzt: ein Kanal aus dieser
    Gilde, in die Runde einer fremden geschrieben, schickte deren Chroniken künftig hierher,
    eine dort gesetzte Testwelt füllte deren Protokolle mit erfundenen Zahlen, und eine dort
    gesetzte Sprache läse deren Runde die Einwilligung in einer fremden vor. Anders als ein
    Löschknopf ist keines davon eine einmalige Fehlhandlung, sondern eine dauerhafte.

    Nach einer Wahl bleibt die Ansicht stehen, statt zu verschwinden: es sind drei
    Entscheidungen in einer Nachricht, und die erste darf die zweite nicht wegnehmen.
    Gebaut wird sie dabei neu — gegen ``gemeint``, damit die Häkchen zeigen, was jetzt gilt.
    """
    discord = _discord()

    def menue(kennung: str, platzhalter: str, zeilen, zeile: int):
        return discord.ui.Select(
            placeholder=platzhalter,
            row=zeile,
            custom_id=f"{kennung}:{runde.id}",
            options=[
                discord.SelectOption(label=schrift, value=wert, default=vorgewaehlt)
                for schrift, wert, vorgewaehlt in zeilen
            ],
        )

    kanal = menue(
        KENNUNG_KANAL,
        einrichten.KANAL_WAEHLEN,
        einrichten.kanalwahl(config, runde, _textkanaele(gilde)),
        0,
    )
    quelle = menue(KENNUNG_QUELLE, einrichten.QUELLE_WAEHLEN, einrichten.quellenwahl(runde), 1)
    # Ein Menü und kein Feld im Fenster, und es steht hier unten aus demselben Grund wie
    # die Quelle: an dieser Wahl hängt die hörbare Einwilligungs-Ansage, und ein
    # eingetippter Sprachname ginge als unbekannter Wert zurück. Ein Menü kennt nur die
    # Sprachen, für die es überhaupt eine Ansage gibt.
    sprache = menue(KENNUNG_SPRACHE, einrichten.SPRACHE_WAEHLEN, einrichten.sprachwahl(runde), 2)

    def entschieden(gebaut, setzen):
        @_geklickt
        async def gewaehlt(interaction) -> None:
            gemeint = await _noch_dieselbe(config, interaction, runde)
            if gemeint is None:
                return
            satz = setzen(gemeint, gebaut.values[0])
            await interaction.response.edit_message(
                content=satz, view=_einrichtungsansicht(config, gemeint, gilde)
            )

        return gewaehlt

    kanal.callback = entschieden(kanal, einrichten.kanal_setzen)
    quelle.callback = entschieden(quelle, einrichten.quelle_setzen)
    sprache.callback = entschieden(sprache, einrichten.sprache_setzen)

    class Einrichtungsansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(kanal)
            self.add_item(quelle)
            self.add_item(sprache)

    return Einrichtungsansicht()


async def _offenlegen(interaction) -> bool:
    """Die Offenlegung in den Kanal — sichtbar für die Gruppe, nicht nur für einen.

    Ob sie angekommen ist, entscheidet, ob die Runde wieder in Dienst geht; deshalb wird
    hier gefangen statt weitergereicht.
    """
    kanal = getattr(interaction, "channel", None)
    if kanal is None:
        return False
    try:
        await _zustellen(kanal.send, einrichten.OFFENLEGUNG)
    except Exception:  # noqa: BLE001
        logger.exception("Offenlegung nicht zugestellt")
        return False
    return True


def _einrichtungsfenster(config: Config, ctx):
    """Das Fenster für Adresse, Benutzer, Uhrzeit und Zone — nie für das Passwort.

    Das Modell steht hier nicht: es gehört seit #87 der Instanz und nicht der Runde.

    Die Quelle der Zahlen steht ebenfalls nicht hier, obwohl das fünfte Feld frei wäre
    (py-cord: ``You can only have up to 5 items in a modal``). Sie ist ein Schalter mit
    zwei Stellungen, kein Wert zum Eintippen — als Menü unter der Antwort kann sie nicht
    vertippt werden und trägt die Folge im Klartext neben sich.

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
                discord.ui.InputText(
                    label=einrichten.FELD_ZONE,
                    placeholder=einrichten.HINWEIS_ZONE,
                    required=False,
                ),
                title=einrichten.SETUP_TITEL,
            )

        @_gefenstert
        async def callback(self, interaction) -> None:
            # Dieses Fenster arbeitet am längsten von allen — es kann eine abgelaufene
            # Runde samt Dateien löschen. Ohne ``defer`` wäre der Token nach drei Sekunden
            # tot, und dann käme auch die Fehlermeldung nicht mehr an: niemand erführe,
            # wie es ausging.
            await interaction.response.defer(ephemeral=True)
            adresse, benutzer, uhrzeit, zone = (feld.value for feld in self.children)
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
                zone=zone,
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
            # Nachgereicht, nicht erstmalig: der Aufschub oben **war** die erste Antwort,
            # und eine zweite weist Discord ab.
            await _sagen(
                interaction,
                f"{meldung} {einrichten.KANAL_FRAGE} {einrichten.QUELLE_FRAGE} "
                f"{einrichten.SPRACHE_FRAGE}",
                view=_einrichtungsansicht(config, fertig.runde, gilde),
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
        # Erst antworten, dann arbeiten (#282). Dateien und Zeilen einer großen Runde
        # dauern länger als Discords drei Sekunden; danach ist der Token tot, und dann
        # kommt weder die Meldung noch die Fehlermeldung an — ausgerechnet bei der einen
        # Handlung ohne Rücknahme. Dass die Arbeit daneben nicht auf die Ereignisschleife
        # gehört, ist die zweite, davon unabhängige Vorsicht: solange sie rechnet,
        # antwortet der Bot niemandem.
        await interaction.response.defer()
        meldung = await asyncio.to_thread(
            einrichten.geloescht, config, gemeint, veranlasst_von=_veranlasser(wer)
        )
        await _abschliessend(interaction, meldung)

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


def _sitzungsloeschansicht(config: Config, runde, marke: str):
    """Die Rückfrage vor **einer** Sitzung — dieselben zwei Knöpfe wie vor der ganzen Runde.

    Und dieselben zwei Prüfungen beim Klick: die Runde, weil ihre Kennung inzwischen einer
    fremden Gilde gehören kann, und das Recht, weil die Frage die Administration stellt und
    klicken könnte jeder, der die Nachricht sieht. Dass die Sitzung noch da ist — und noch
    dieselbe —, prüft der Löschweg selbst an der ``marke``: er sagt es, statt ein »fort«
    über etwas zu setzen, das schon fort war oder nie gemeint war.
    """
    discord = _discord()

    ja = discord.ui.Button(
        label=chronik.SITZUNG_JA, custom_id=f"{KENNUNG_SITZUNG}:{runde.id}:{marke}:ja"
    )
    nein = discord.ui.Button(
        label=chronik.SITZUNG_NEIN, custom_id=f"{KENNUNG_SITZUNG}:{runde.id}:{marke}:nein"
    )

    @_geklickt
    async def bestaetigt(interaction) -> None:
        if not _darf_loeschen(getattr(interaction, "user", None)):
            await interaction.response.edit_message(content=einrichten.NUR_ADMIN, view=None)
            return
        gemeint = await _noch_dieselbe(config, interaction, runde)
        if gemeint is None:
            return
        # Erst antworten, dann arbeiten — dieselbe Begründung wie am Löschknopf der
        # ganzen Runde (#282): Tondateien und Zeilen einer langen Sitzung sprengen
        # Discords Drei-Sekunden-Fenster, und mit dem Token stirbt auch die Auskunft,
        # ob die Sitzung nun fort ist. Die Ereignisschleife bleibt daneben frei.
        await interaction.response.defer()
        meldung = await asyncio.to_thread(chronik.sitzung_geloescht, config, gemeint, marke)
        await _abschliessend(interaction, meldung)

    @_geklickt
    async def verworfen(interaction) -> None:
        await interaction.response.edit_message(content=chronik.SITZUNG_ABGEBROCHEN, view=None)

    ja.callback = bestaetigt
    nein.callback = verworfen

    class Sitzungsloeschansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(ja)
            self.add_item(nein)

    return Sitzungsloeschansicht()


def _sitzungswahlansicht(config: Config, runde, zeilen):
    """Ein Menü der Sitzungen — die Wahl **zeigt**, was verschwände, und löscht nichts.

    Zwei Schritte, weil es keinen dritten Versuch gibt: die Wahl benennt die Sitzung, die
    Rückfrage danach benennt, was an ihr hängt — bis hin zu den Tondateien. Ein Menü, das
    beim Loslassen löschte, wäre ein Vertipper vom Verlust entfernt.

    Weitergereicht wird die Marke der Sitzung und nicht ihre Nummer: was aus einer
    Interaktion zurückkommt, ist ein Vorschlag, und die Nummer allein trägt nicht, dass
    darunter noch derselbe Abend steht.
    """
    discord = _discord()

    menue = discord.ui.Select(
        placeholder=chronik.SITZUNG_WAEHLEN,
        custom_id=f"{KENNUNG_SITZUNG}:{runde.id}",
        options=[
            discord.SelectOption(
                label=erinnern.gekuerzt(schrift, erinnern.KNOPF_GRENZE), value=wert
            )
            for schrift, wert in zeilen
        ],
    )

    @_geklickt
    async def gewaehlt(interaction) -> None:
        if not _darf_loeschen(getattr(interaction, "user", None)):
            await interaction.response.edit_message(content=einrichten.NUR_ADMIN, view=None)
            return
        gemeint = await _noch_dieselbe(config, interaction, runde)
        if gemeint is None:
            return
        marke = str(menue.values[0])
        frage = chronik.sitzungsfrage(config, gemeint, marke)
        if frage is None:
            await interaction.response.edit_message(content=chronik.SITZUNG_SCHON_FORT, view=None)
            return
        await interaction.response.edit_message(
            content=frage, view=_sitzungsloeschansicht(config, gemeint, marke)
        )

    menue.callback = gewaehlt

    class Sitzungswahlansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(menue)

    return Sitzungswahlansicht()


def _embed(gebaut: dict | None):
    return None if gebaut is None else _discord().Embed.from_dict(gebaut)


async def _antworten(ctx, antwort: erinnern.Antwort, view=None) -> None:
    """Antworten sieht nur, wer gefragt hat: eine Suche ist die Frage eines Einzelnen."""
    weiteres = {}
    if antwort.embed is not None:
        weiteres["embed"] = _embed(antwort.embed)
    if view is not None:
        weiteres["view"] = view
    await _zustellen(ctx.respond, antwort.text, zuletzt=weiteres, ephemeral=True)


async def _ersetzen(interaction, antwort: erinnern.Antwort, view) -> None:
    """Der Knopf ändert die Nachricht, in der er steckt — die Antwort steht mit darin.

    Nicht zusätzlich: eine zweite Nachricht je Klick wäre nach fünf Entscheidungen ein
    Stapel, und die Liste daneben zeigte weiter, was es nicht mehr gibt. Genau deshalb
    wird hier gekürzt statt geteilt: es gibt nur diese eine Nachricht.
    """
    await interaction.response.edit_message(
        content=grenzen.gekappt(antwort.text, grenzen.NACHRICHT) or None,
        embed=_embed(antwort.embed),
        view=view,
    )


async def _abschliessend(interaction, text: str) -> None:
    """Die Nachricht, in der der Knopf steckt — vor wie nach einem ``defer``.

    Nach dem Aufschub ist die erste Antwort vergeben, und Discord weist ``edit_message``
    dann ab. Geändert wird dieselbe Nachricht, nur über den anderen Weg; die Ansicht
    verschwindet in beiden Fällen, weil die Handlung getan ist.
    """
    if interaction.response.is_done():
        await interaction.edit_original_response(content=text, view=None)
        return
    await interaction.response.edit_message(content=text, view=None)


def _geklickt(arbeit):
    """Auch ein Knopf antwortet immer — sonst bleibt »denkt nach …« stehen."""

    async def gefasst(interaction) -> None:
        try:
            await arbeit(interaction)
        except Exception as fehler:  # noqa: BLE001
            logger.exception("Klick in einer Ansicht gescheitert")
            grund = UNERWARTET.format(typ=type(fehler).__name__)
            await _sagen(interaction, GESCHEITERT.format(grund=grund))

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
            # Ohne Frist, und das ist der Punkt (#281). Eine Ansicht mit Frist hört nach
            # einer Viertelstunde auf zuzuhören, während ihre Knöpfe sichtbar und
            # anklickbar stehenbleiben — und der Befehl, der die Liste zurückholte, ist
            # mit #272 entfallen. Jeder Knopf trägt eine feste ``custom_id``; damit ist
            # die Ansicht *persistent* und ``_vorschlaege_wieder_anschliessen`` kann sie
            # nach einem Neustart wieder an dieselbe Nachricht anschließen.
            super().__init__(timeout=None)
            for zeile, eintrag in enumerate(stand.eintraege):
                self.add_item(schild(eintrag, zeile))
                for art, schrift in erinnern.ENTSCHEIDUNGEN:
                    self.add_item(knopf(eintrag, art, schrift, zeile))

    return Registeransicht()


async def _uebernahme_sagen(bot, runde, ergebnis: erinnern.Zugeordnet) -> None:
    """Ein übernommenes Konto bekommt Tageslicht: die Runde erfährt es, die Vorbesitzerin auch.

    Das Umhängen ist der Schritt mit der größten Folge — jemand nimmt einer anderen ihr
    Konto. Von selbst ist er der stillste: die Antwort auf den Klick sieht nur, wer geklickt
    hat, und in der Zuordnung bleibt danach genau eine Zeile stehen. Dass
    ``/chronicle zuordnung`` die Runde nebeneinander zeigt, trägt das nicht — die Ansicht
    reicht bis ``PRO_SEITE``, und ab der sechsten Person steht die Vorbesitzerin weder
    vorher noch nachher darin.

    Zwei Wege, und der **Kanal der Sitzung** ist der belastbare: er erreicht die Runde auch
    dann, wenn die Vorbesitzerin keine Direktnachrichten annimmt. Deren Ausbleiben verwirft die
    Übernahme deshalb nicht; es wird protokolliert, ohne Namen und ohne Kennung.

    Ob es den Weg überhaupt gab, steht im Log: eine Runde ohne Sitzung hat keinen, und
    »der Vermerk im Kanal trägt sie« wäre dann eine Auskunft über etwas, das nicht
    geschehen ist.
    """
    wer, vorher, spieler = ergebnis.wer, ergebnis.vorher, ergebnis.spieler
    sitzung = chronik.letzte_sitzung(runde)
    im_kanal = sitzung is not None and await _in_den_sitzungskanal(
        bot,
        runde,
        sitzung,
        erinnern.UEBERNAHME_VERMERK.format(
            name=wer.discord_name, spieler=spieler.name, vorher=vorher.discord_name
        ),
    )
    if not im_kanal:
        # Nur die Tatsache, keine Vorhersage: der Brief an die Vorbesitzerin geht erst
        # danach los und kann genauso scheitern. Ob er ankam, sagt der Aufrufer.
        logger.warning("Die Übernahme steht in keinem Kanal.")
    kennung = int(vorher.discord_user_id)
    ziel = bot.get_user(kennung) or await bot.fetch_user(kennung)
    await _zustellen(
        ziel.send,
        erinnern.UEBERNAHME_ANGESAGT.format(
            runde=runde.name, name=wer.discord_name, spieler=spieler.name
        ),
    )


def _zuordnungsansicht(bot, config: Config, runde, stand: erinnern.Zuordnung):
    """Je aufgenommener Person ein Menü mit den Foundry-Spielern dieser Runde.

    Hier steht **jedes** Konto zur Wahl, auch ein vergebenes, und hier darf es auch
    umgehängt werden (``uebernehmen``). Das ist die Stelle, an der eine falsche Zuordnung
    wieder weggeht; das Menü im Zwiegespräch kann das nicht und darf es deshalb nicht.
    Gesagt wird die Übernahme danach — siehe ``_uebernahme_sagen``.

    Wer hier handeln darf, prüft diese Ansicht **nicht**: es gibt keinen ``_wer``-Abgleich,
    jedes Mitglied bedient jede Zeile. Das ist so gewollt (#62) — wer den Befehl überhaupt
    sieht, entscheiden Discords Kanal- und Rollenrechte, und ein zweites Rollenmodell
    daneben gibt es für Spielinhalte nicht. Beim Betreten liegt es anders: dort steht die
    Frage im Zwiegespräch, sie gilt einer Person, und nur die beantwortet sie.
    """
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
            ergebnis = erinnern.zuordnen(
                gemeint, person.discord_user_id, gebaut.values[0], uebernehmen=True
            )
            naechste = erinnern.zuordnung(gemeint, meldung=ergebnis.satz)
            await _ersetzen(
                interaction, naechste.antwort, _zuordnungsansicht(bot, config, gemeint, naechste)
            )
            if ergebnis.vorher is None:
                return
            # Gefangen, weil die Antwort oben schon steht: eine Ausnahme von hier machte
            # daraus über ``_geklickt`` ein »hat nicht geklappt«, obwohl umgehängt ist. Und
            # eine geschlossene Direktnachricht ist kein Grund, die Übernahme zu verwerfen.
            # Was von den beiden Wegen ankam, sagt ``_uebernahme_sagen`` selbst — ohne
            # Namen und ohne Kennung im Log.
            try:
                await _uebernahme_sagen(bot, gemeint, ergebnis)
            except Exception as fehler:  # noqa: BLE001
                logger.warning(
                    "Die Ansage zur Übernahme ging nicht durch (%s) — umgehängt ist sie "
                    "trotzdem. Wie weit sie kam, steht in der Zeile davor.",
                    type(fehler).__name__,
                )

        gebaut.callback = gewaehlt
        return gebaut

    class Zuordnungsansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            for zeile, person in enumerate(stand.personen):
                self.add_item(menue(person, zeile))

    return Zuordnungsansicht()


def _betretensansicht(bot, aufnahme: Aufnahme, runde, stand: erinnern.Betreten):
    """Ein Menü für genau eine Person, im Zwiegespräch: wer bist du in dieser Runde?

    Zur Wahl stehen die **freien** Konten und nur sie (``erinnern.betreten``). Ein Menü, das
    ein bereits vergebenes anbietet, ist eine Einladung, sich privat und unbeaufsichtigt die
    Identität einer Mitspielerin zu nehmen; ``erinnern.zuordnen`` weist ein vergebenes
    Konto deshalb auch dann ab, wenn es doch einmal in einer alten Ansicht steht — hier
    ohne ``uebernehmen``, anders als in ``/chronicle zuordnung``, wo umgehängt werden darf und die
    Übernahme danach im Thread steht.

    Und was hier gewählt wird, steht danach im Thread. Das ist der Weg **ohne** Beleg —
    jemand klickt sich ein Konto —, und je schwächer der Beleg, desto mehr Tageslicht
    (Betreiber-Entscheidung vom 2026-08-12). Der Satz sagt deshalb nichts über Namen: ins
    Menü führt auch die **Mehrdeutigkeit**, und dort ist der Name gerade derselbe, nur eben
    nicht nur bei einer. Anders als beim Vermerk der Namensgleichheit ist er hier keine
    **Bedingung**: dort entscheidet niemand, hier hat die Person selbst geantwortet, und
    ihre Antwort wegzuwerfen, weil Discord den Thread gerade nicht hergibt, wäre die
    schlechtere Zumutung.

    Die einzige Ansicht, die nicht in einer Gilde steht — Discord nennt im Zwiegespräch
    keine. ``_dieselbe`` liefe deshalb hier immer ins Leere; geprüft wird stattdessen die
    Runde gegen ihren eigenen Stand, wie es der Lauf tut, der seine Runde schon hält.

    Und geantwortet wird nur über sich selbst: das Zwiegespräch trägt die Frage zwar
    ohnehin nur an eine Person, aber woran die Zuordnung hängt, steht in der Kennung der
    Ansicht — nicht darin, wer die Nachricht gerade offen hat.
    """
    discord = _discord()
    person = stand.person

    gebaut = discord.ui.Select(
        placeholder=erinnern.gekuerzt(
            erinnern.ZUORDNUNG_WAEHLEN.format(name=person.discord_name),
            erinnern.PLATZHALTER_GRENZE,
        ),
        custom_id=f"{KENNUNG_BETRETEN}:{person.discord_user_id}",
        options=[
            discord.SelectOption(label=schrift, value=wert, default=vorgewaehlt)
            for schrift, wert, vorgewaehlt in erinnern.wahlmoeglichkeiten(person, stand.spieler)
        ],
    )

    @_geklickt
    async def gewaehlt(interaction) -> None:
        if _wer(interaction) != person.discord_user_id:
            await interaction.response.edit_message(content=erinnern.NUR_SELBST, view=None)
            return
        gemeint = lebenszyklus.dieselbe(runde)
        if gemeint is None:
            await interaction.response.edit_message(content=chronik.VERALTET, view=None)
            return
        ergebnis = erinnern.zuordnen(gemeint, person.discord_user_id, gebaut.values[0])
        await interaction.response.edit_message(content=ergebnis.satz, view=None)
        if ergebnis.spieler is None:
            return
        # Gefangen, weil die Antwort oben schon steht: eine Ausnahme von hier machte daraus
        # über ``_geklickt`` ein »hat nicht geklappt«, obwohl die Zuordnung entstanden ist.
        try:
            await _in_den_kanal(
                bot,
                aufnahme,
                erinnern.MENUE_VERMERK.format(
                    name=person.discord_name, spieler=ergebnis.spieler.name
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Die selbst gewählte Zuordnung blieb im Thread ungesagt")

    gebaut.callback = gewaehlt

    class Betretensansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(gebaut)

    return Betretensansicht()


def _feld(discord, beschreibung: str):
    """Ein freiwilliges Textfeld eines Slash-Befehls — als Vorgabewert, nicht als Annotation.

    Diese Datei hat ``from __future__ import annotations``, und damit ist jede Annotation
    zur Laufzeit eine **Zeichenkette**. py-cord liest den Typ eines Feldes aus der
    Annotation und bekäme dann ``"str"`` statt ``str``; beim ersten Aufruf stirbt es an
    ``issubclass() arg 1 must be a class``, und Discord zeigt »Die Anwendung reagiert
    nicht«. Steht im Vorgabewert ein fertiges ``Option``, nimmt py-cord dessen Typ und
    sieht die Annotation gar nicht erst an.
    """
    return discord.Option(str, description=beschreibung, default="", required=False)


def baue(config: Config):
    """Der Bot mit seinen Befehlen und der Sitzung, die im Kanal läuft — ohne Verbindung."""
    discord = _discord()
    _sprache_pruefen(discord)
    absichten = discord.Intents.none()
    absichten.guilds = True
    absichten.voice_states = True
    # Ohne diese beiden ist die Sitzung ein leerer Behälter: Discord meldete weder die
    # Nachrichten noch ihren Inhalt, und jede Notiz käme leer an.
    absichten.messages = True
    absichten.message_content = True
    # ``members`` bleibt aus, und das ist eine Entscheidung, kein Versehen (#250). Der
    # Intent ist privilegiert und spiegelte die vollständige Mitgliederliste **jeder**
    # Gilde dauerhaft in diesen Prozess — für die eine Auskunft, die hier fehlt: wie der
    # Sprecher einer Spur heißt. Die holt ``chronicle.bot.namen`` mit einem ``fetch_user``
    # je Sprecher. Weniger Daten, dieselbe Antwort.
    bot = discord.Bot(intents=absichten)
    laeufe = _Laeufe()
    # Zwei Gruppen: »jetzt gerade« und »später nachsehen«. Das ist die Unterscheidung, die
    # Spielende im Kopf haben — die alte zwischen Ton und Text war unsere (#272).
    gruppe = bot.create_group(GRUPPE, "The evening while it runs")
    chronikgruppe = bot.create_group(GRUPPE_CHRONIK, "Look things up later — and set up")

    @gruppe.command(
        name=BEFEHL_START, description="Begin a session, announce it, record one track per speaker"
    )
    @antwortet
    async def start(
        ctx,
        titel=_feld(discord, "Title of the session"),  # noqa: B008
    ) -> None:
        """Ein Befehl statt einer Reihenfolge — anlegen und mitschneiden in einem (#272).

        **Warum das ein Befehl bleibt und kein selbst erkannter Moment.** Der Bot sieht,
        wer im Sprachkanal steht; daraus zu schließen, dass hier gespielt und
        mitgeschnitten werden soll, hieße Einwilligung zu unterstellen. §201 StGB verlangt
        einen bewussten Akt, keine Vermutung. Das Gegenstück — das **Ende** — erkennt er
        sehr wohl selbst (#271): aufzuhören darf er annehmen, anzufangen nicht.
        """
        # Zuerst die Runde: eine Gilde ohne eigene nimmt nicht auf, eine ruhende erst
        # recht nicht. Sie steht vor den Fragen darunter, weil erst sie sagt, wessen Lauf
        # gemeint ist — »Ich schneide schon mit« galt sonst einer fremden Gruppe.
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        lauf = laeufe.fuer(runde)
        if lauf.aufnahme is not None:
            await _zustellen(ctx.respond, LAEUFT_SCHON, ephemeral=True)
            return
        # Der Test hält gerade dieselbe Sprachverbindung und trennt sie gleich wieder — ein
        # Mitschnitt, der jetzt begänne, verlöre sie mitten im Satz.
        if lauf.probe:
            await _zustellen(ctx.respond, PROBE_LAEUFT, ephemeral=True)
            return
        # Läuft schon eine Sitzung, ist dies kein zweiter Anfang, sondern das Fortsetzen
        # des Mitschnitts nach ``/session pause``: angelegt wird nichts, gefragt auch nicht.
        if chronik.offene_sitzung(runde) is not None:
            await ctx.defer(ephemeral=True)
            await _zustellen(
                ctx.respond,
                await _mitschnitt_beginnen(config, bot, lauf, ctx, runde),
                ephemeral=True,
            )
            await _zuordnung_reihum(bot, lauf)
            return
        if not chronik.foundry_im_spiel(config, runde):
            # Ohne Server gäbe es nichts, wo das Passwort vorgezeigt würde — es läge nur
            # bis zur Frist herum. Ohne Fenster kann der Befehl selbst aufschieben.
            await ctx.defer(ephemeral=True)
            await _zustellen(
                ctx.respond,
                await _sitzung_starten(config, bot, lauf, ctx, runde, titel, "", _wer(ctx)),
                ephemeral=True,
            )
            await _zuordnung_reihum(bot, lauf)
            return
        # Kein ``defer`` davor: ein Fenster geht nur als *erste* Antwort auf den Befehl.
        # Deshalb entsteht die Sitzung erst im Rückruf des Fensters, der selbst aufschiebt.
        await ctx.send_modal(_startfenster(config, bot, lauf, runde, titel))

    # Fehlt bewusst in ``BEFEHLE``: ohne ihn würde die Pause mitgeschnitten, mit ihm in der
    # Liste stünde neben »so fängt der Abend an« gleich »so hört er auf« (#272).
    @gruppe.command(name=BEFEHL_PAUSE, description="End the recording and queue the tracks")
    @antwortet
    async def stop(ctx) -> None:
        # Auch hier zuerst die Runde: sie sagt, welcher Mitschnitt gemeint ist. Ohne sie
        # beendete dieser Befehl den Abend irgendeiner Gruppe — der zuletzt begonnenen.
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        lauf = laeufe.fuer(runde)
        if lauf.aufnahme is None:
            await _zustellen(ctx.respond, LAEUFT_NICHT, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        meldungen = await _mitschnitt_beenden(lauf, runde)
        # Leer heißt: in der Zwischenzeit war ein anderer schneller — der leere Kanal etwa.
        # Das ist kein Fehlschlag, und so ausgesprochen zu werden verdient er auch nicht.
        await _zustellen(ctx.respond, " ".join(meldungen) or LAEUFT_NICHT, ephemeral=True)

    @gruppe.command(
        name="check", description="Listen briefly and say whether the audio really arrives"
    )
    @antwortet
    async def empfangstest(ctx) -> None:
        """Die Frage »hört der Bot überhaupt?« — beantwortet in Discord statt im Log."""
        # Dieselbe Schranke wie vor ``/session start``: eine Gilde ohne eigene Runde prüft
        # hier nichts, eine ruhende erst recht nicht — es wird aufgezeichnet. Und wieder
        # zuerst, weil die Aufnahme, die nicht gestört werden darf, die dieser Runde ist.
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        lauf = laeufe.fuer(runde)
        if lauf.aufnahme is not None:
            await _zustellen(ctx.respond, PROBE_NICHT_STOEREN, ephemeral=True)
            return
        if lauf.probe:
            await _zustellen(ctx.respond, PROBE_LAEUFT, ephemeral=True)
            return
        # Dieselbe Prüfung vor derselben Ankündigung: genau hier lief sie am 2026-08-18 zu
        # spät, und die Gruppe las die Ansage, die die nächste Zeile zurücknahm (#270).
        if chronik.letzte_sitzung(runde) is None:
            await _zustellen(ctx.respond, recorder.OHNE_SITZUNG, ephemeral=True)
            return
        kanal = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if kanal is None:
            await _zustellen(ctx.respond, NICHT_IM_KANAL, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        lauf.probe = True
        try:
            stimme = Sprachverbindung(await kanal.connect())
            ziel = _vorstellungsziel(ctx, kanal)
            try:
                await _zustellen(ziel.send, PROBE_VORSTELLUNG)
            except BaseException as fehler:
                # Wie bei ``/session start``: was von einer geteilten Ankündigung schon
                # dasteht, wird zurückgenommen. Heute passt dieser Text in eine Nachricht,
                # aber er zieht Ausweg und Frist aus denselben Quellen wie die Vorstellung
                # und wächst mit ihnen.
                await _widerrufen(ziel, fehler)
                await stimme.trennen()
                raise
            try:
                ergebnis = await recorder.pruefen(config, stimme, runde)
            except BaseException as fehler:
                # ``pruefen`` trennt selbst, sobald es mitschneidet; das hier fängt den
                # Abbruch davor — ohne es säße der Bot nach einer fehlenden Sitzung im Kanal.
                # Und die Ankündigung steht öffentlich wie die vor einer Aufnahme, also
                # wird sie auch hier zurückgenommen statt nur dem Aufrufer abgesagt — und
                # zwar vor dem Trennen, das selbst stolpern und ihn mitnehmen kann.
                await _widerrufen(ziel, fehler)
                await stimme.trennen()
                raise
        finally:
            lauf.probe = False
        await _zustellen(ctx.respond, recorder.bericht(ergebnis), ephemeral=True)

    @gruppe.command(name="help", description="What the bot does and how to operate it")
    @antwortet
    async def hilfe(ctx) -> None:
        await _zustellen(ctx.respond, HILFE, ephemeral=True)

    @gruppe.command(name="done", description="Close the session and start the chronicle")
    @antwortet
    async def chronik_fertig(ctx) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        # Nicht der Kanal, sondern die Runde bestimmt die Sitzung — wie bei ``/session``.
        # Nach ``/session pause`` steht man im Sprachkanal, und dort abzuweisen hieß, zu
        # einer zweiten Sitzung zu raten (#156). Welche gemeint ist, sagt die Antwort.
        sitzung = chronik.laufende_sitzung(runde)
        wer = _wer(ctx)
        # Ohne Fenster nur zweierlei: wer selbst hinterlegt hat, und wo es gar keinen
        # Server gibt. Eine fremde Eingabe wird **nicht** stillschweigend übernommen.
        # Gelesen wird hier und nicht erst im Auftragsfaden: dazwischen liegen ein
        # ``defer`` und das Beenden des Mitschnitts, und in dieser Lücke kann ein zweites
        # Fenster den Merkzettel überschreiben — geprüft wäre dann das eine, vorgezeigt
        # das andere.
        geheim = chronik.passwort_fuer(runde, wer)
        if geheim is not None or not chronik.foundry_im_spiel(config, runde):
            await ctx.defer(ephemeral=True)
            await _zustellen(
                ctx.respond,
                # ``merken=False``: ``geheim`` kam gerade aus dem Merkzettel. Es dort
                # erneut abzulegen stellte die Frist aus #64 zurück — und bei belegter
                # Maschine verbraucht es niemand, sodass jeder Versuch sie weiterschöbe.
                await _abschliessen(
                    config,
                    runde,
                    sitzung,
                    geheim,
                    laeufe.fuer(runde),
                    ctx.channel,
                    wer,
                    merken=False,
                ),
                ephemeral=True,
            )
            return
        fremd = chronik.passwort_gehalten(runde)
        hinweis = chronik.FREMDES_HINWEIS if fremd else chronik.PASSWORT_HINWEIS
        await ctx.send_modal(_passwortfrage(config, runde, sitzung, laeufe.fuer(runde), hinweis))

    # Notnagel, und deshalb nicht in ``BEFEHLE``: die Zahlen holt sonst der Abschluss oder
    # der nächtliche Lauf. Aufrufbar bleibt er, weil es Abende gibt, an denen beides fehlt.
    @chronikgruppe.command(
        name="abgleich", description="Fetch the numbers from Foundry without running a session"
    )
    @antwortet
    async def chronik_abgleich(ctx) -> None:
        # Dieselbe Schranke wie vor jedem anderen Befehl der Gruppe: eine Gilde ohne Runde
        # bekommt nichts, eine ruhende erst recht nicht — auch nicht mit einem Passwort,
        # das noch im Speicher liegt.
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        wer = _wer(ctx)
        # Gelesen wird hier und nicht erst im Auftragsfaden — wie beim Abschluss: dazwischen
        # liegt ein ``defer``, und in dieser Lücke kann ein zweites Fenster den Merkzettel
        # überschreiben.
        geheim = chronik.passwort_fuer(runde, wer)
        if geheim is not None or not chronik.foundry_im_spiel(config, runde):
            await ctx.defer(ephemeral=True)
            await _zustellen(
                ctx.respond,
                # ``merken=False``: ``geheim`` kam gerade aus dem Merkzettel und darf dort
                # keine neue Frist bekommen.
                chronik.abgleich_starten(
                    config, runde, geheim, wer=wer, merken=False, melden=_melder(ctx.channel)
                ),
                ephemeral=True,
            )
            return
        fremd = chronik.passwort_gehalten(runde)
        hinweis = chronik.FREMDES_HINWEIS if fremd else chronik.PASSWORT_HINWEIS
        await ctx.send_modal(_abgleichfenster(config, runde, hinweis))

    @chronikgruppe.command(
        name="sitzung-loeschen", description="Delete a single session, after a confirmation"
    )
    @antwortet
    async def chronik_sitzung_loeschen(ctx) -> None:
        """Der kleine Weg neben ``/chronicle delete``: ein Abend statt der ganzen Runde.

        **Die Schranke ist die der Administration, dieselbe wie vor der ganzen Runde**
        (Operator-Entscheidung, #171/#174) — und das ist nicht die naheliegende Antwort.
        Naheliegend wäre die Verwaltung: eine Sitzung fortzunehmen sieht nach Berichtigung
        aus, der überzählige Abend aus einem eingelesenen Dokument. Sie ist aber nicht die
        harmlosere Löschung, sondern die **schlimmere**: sie ist auswählbar *und* lautlos.
        Alles hier läuft ephemer, der Thread der Sitzung bleibt unverändert stehen, und wer
        geklickt hat, steht mit Absicht in keinem Log. Ein Verwalter nähme so genau den
        einen unbequemen Abend samt seinen Aufnahmen fort, und niemand in der Gruppe
        erführe es; ``/chronicle delete`` kann man nicht heimlich drücken. Der Maßstab von
        ``_darf_loeschen`` trägt genau das: das Sofortige bekommt die strengere Schranke,
        das Langsame die Umkehrbarkeit — und ein langsames, umkehrbares Gegenstück zu
        dieser Löschung gibt es überhaupt nicht.

        Die Alternative steht offen: ``_darf_verwalten`` wäre vertretbar, wenn dafür die
        Heimlichkeit fiele — ein sichtbarer Vermerk im Kanal der Runde, dass dieser Abend
        gelöscht wurde. Das ist nicht gebaut; wer es baut, darf die Schranke senken.

        Gerechnet wird noch einmal am Menü und am Knopf.

        Eine ruhende Runde kommt hier nicht durch: sie ist verabschiedet, und wer sie ganz
        loswerden will, hat dafür ``/chronicle delete``.
        """
        if not _darf_loeschen(getattr(ctx, "author", None)):
            await _zustellen(ctx.respond, einrichten.NUR_ADMIN, ephemeral=True)
            return
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        wahl = chronik.sitzungswahl(runde)
        ansicht = _sitzungswahlansicht(config, runde, wahl.zeilen) if wahl.zeilen else None
        await _zustellen(ctx.respond, wahl.text, zuletzt={"view": ansicht}, ephemeral=True)

    @chronikgruppe.command(
        name="delete", description="Delete everything of this round, after a confirmation"
    )
    @antwortet
    async def chronik_loeschen(ctx) -> None:
        # Discord kennt ``default_member_permissions`` nur für den ganzen Befehl, und
        # ``/session start`` soll jedes Mitglied geben dürfen. Für diesen Unterbefehl steht
        # die Schranke deshalb hier — und noch einmal am Knopf, der wirklich löscht.
        if not _darf_loeschen(getattr(ctx, "author", None)):
            await _zustellen(ctx.respond, einrichten.NUR_ADMIN, ephemeral=True)
            return
        runde = chronik.runde_zum_loeschen(config, ctx.guild_id)
        await _zustellen(
            ctx.respond,
            einrichten.loeschfrage(),
            zuletzt={"view": _loeschansicht(config, runde)},
            ephemeral=True,
        )

    @chronikgruppe.command(
        name=BEFEHL_SETUP,
        description="Set up Foundry, delivery channel, nightly run and content language",
    )
    @antwortet
    async def setup(ctx) -> None:
        """Kein ``default_member_permissions`` mehr — und das ist der Punkt (#272/#264).

        Die Angabe blendete den Befehl bei allen ohne »Server verwalten« **vollständig**
        aus. Am 2026-08-18 fand ihn deshalb nicht einmal der Betreiber wieder: der eine
        Mensch, der ihn hätte aufrufen dürfen, sah stattdessen den gleichnamigen Befehl
        eines fremden Bots im selben Server. Unter ``/chronicle`` ist der Name eindeutig
        und der Befehl sichtbar; wer ihn nicht geben darf, bekommt es gesagt statt
        verschwiegen. Gerechnet wird unverändert hier — sichtbar ist nicht erlaubt.
        """
        # Ohne Gilde gibt es keine Runde zu beanspruchen — eine im Zwiegespräch angelegte
        # gehörte niemandem und stünde für immer da.
        if ctx.guild_id is None:
            await _zustellen(ctx.respond, einrichten.NUR_IM_SERVER, ephemeral=True)
            return
        if not _darf_verwalten(getattr(ctx, "author", None)):
            await _zustellen(ctx.respond, einrichten.NUR_VERWALTUNG, ephemeral=True)
            return
        await ctx.send_modal(_einrichtungsfenster(config, ctx))

    @chronikgruppe.command(
        name=BEFEHL_SUCHE, description="Look through everything that has been written"
    )
    @antwortet
    async def suche(
        ctx,
        begriff=_feld(discord, "What I should search for"),  # noqa: B008
    ) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        await _antworten(ctx, erinnern.suche(runde, begriff))

    @chronikgruppe.command(name=BEFEHL_WER, description="What the register holds about a name")
    @antwortet
    async def wer(
        ctx,
        name=_feld(discord, "The name I look up"),  # noqa: B008
    ) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        await _antworten(ctx, erinnern.wer(runde, name))

    # Nicht in ``BEFEHLE``, aber aufrufbar: die Frage »wer bist du in Foundry« stellt der
    # Bot seit #76 von selbst, sobald jemand den Sprachkanal betritt (#272 macht das zum
    # einzigen Weg **hinein**). Sie kommt aber nur einmal — wer schon zugeordnet ist, wird
    # nicht noch einmal gefragt. Ohne diesen Befehl gäbe es damit keinen Weg mehr, eine
    # falsche Zuschreibung zu berichtigen, und das ist die eine Auskunft, die eine Person
    # über sich selbst geben können muss.
    @chronikgruppe.command(name="zuordnung", description="Record who plays which Foundry player")
    @antwortet
    async def zuordnung(ctx) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        stand = erinnern.zuordnung(runde)
        await _antworten(ctx, stand.antwort, _zuordnungsansicht(bot, config, runde, stand))

    @gruppe.command(name=BEFEHL_SZENE, description="Draw the dividing line to the next scene")
    @antwortet
    async def szene(
        ctx,
        name=_feld(discord, "Name of the new scene"),  # noqa: B008
    ) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        sitzung = chronik.sitzung_verlangen(runde, str(ctx.channel_id))
        # Sichtbar für alle: die Trennlinie gehört in den Kanal, nicht nur zu dem, der
        # sie gezogen hat.
        await _zustellen(ctx.respond, chronik.szene_setzen(runde, sitzung, name), ephemeral=False)

    @bot.event
    async def on_message(nachricht) -> None:
        if nachricht.author.bot or nachricht.guild is None:
            return
        runde = chronik.runde_der_gilde(config, nachricht.guild.id)
        if runde is None:
            return
        # Die Grenze ist die Zeit und nicht mehr der Ort (#271): derselbe Kanal, aber nur
        # zwischen Start und Abschluss. Davor und danach ist eine Zeile hier Gerede.
        sitzung = chronik.sitzung_im_kanal(runde, str(nachricht.channel.id))
        if sitzung is None:
            await _sagen_dass_der_abend_zu_ist(laeufe.fuer(runde), runde, nachricht)
            return
        # Der Tisch ist noch da, auch wenn der Sprachkanal leer ist: der Abschied von
        # selbst wartet daran ab, statt mitten ins Abmoderieren zu schließen (#288).
        laeufe.fuer(runde).getippt = time.monotonic()
        try:
            meldungen = await chronik.aufnehmen(config, runde, sitzung, _nachricht(nachricht))
        except Exception as fehler:  # noqa: BLE001
            logger.exception("Nachricht der laufenden Sitzung nicht abgelegt")
            grund = UNERWARTET.format(typ=type(fehler).__name__)
            await _zustellen(nachricht.reply, chronik.NICHT_ABGELEGT.format(grund=grund))
            return
        for meldung in meldungen:
            await _zustellen(nachricht.reply, meldung)

    @bot.event
    async def on_raw_message_edit(payload) -> None:
        # Roh und nicht ``on_message_edit``: das gäbe es nur für Nachrichten, die der Bot
        # seit seinem Start gesehen hat — eine Woche alte Notiz gehört auch dazu.
        daten = payload.data or {}
        text = daten.get("content")
        runde = _runde_des_ereignisses(config, payload)
        # Was der Bot selbst geschrieben hat, ist keine Notiz — beim Ablegen nicht und beim
        # Ändern erst recht nicht: sonst legte eine bearbeitete Begrüßung eine an.
        if text is None or runde is None or _vom_bot(daten):
            return
        wechsel = chronik.notiz_aendern(
            runde,
            str(payload.channel_id),
            chronik.Nachricht(
                id=str(payload.message_id), text=text, zeitpunkt=_rohzeitpunkt(daten)
            ),
        )
        if wechsel.antwort is not None:
            await _in_den_sitzungskanal(bot, runde, wechsel.sitzung, wechsel.antwort)

    @bot.event
    async def on_raw_message_delete(payload) -> None:
        runde = _runde_des_ereignisses(config, payload)
        if runde is not None:
            chronik.notiz_entfernen(runde, str(payload.message_id))

    @bot.event
    async def on_guild_join(gilde) -> None:
        # Erst der Kanal, dann der Satz, und die Freigabe zuletzt: wieder im Dienst zu
        # sein, ohne dass die Gruppe die Offenlegung je gelesen hat, ist genau der Zustand,
        # für den es sie gibt. Ohne Kanal bleibt die Runde still — ``/chronicle setup`` bringt sie
        # zurück und sagt die Offenlegung dabei.
        kanal = _begruessungskanal(gilde)
        if kanal is None:
            logger.warning("Kein Kanal zum Begrüßen in %s", gilde.id)
            return
        await _begruessen(config, kanal, str(gilde.id))

    @bot.event
    async def on_guild_remove(gilde) -> None:
        einrichten.verabschieden(config.database_path, str(gilde.id))

    @bot.event
    async def on_ready() -> None:
        # Vor den Fristen: eine Runde, die niemand mehr erreicht, ist der dringendere Fall.
        await _verwaiste_runde_uebernehmen(config, bot)
        # Und danach, nicht davor: eine übernommene Runde ist keine Gilde ohne Runde mehr,
        # und die Übernahme sagt sich selbst — zwei Sätze zum selben Anlass wären einer zu
        # viel.
        await _begruessung_nachholen(config, bot)
        # Und die Knöpfe, die vor dem Neustart im Kanal standen, hören wieder zu: sie
        # überleben die Nachricht, nicht aber den Prozess.
        await _vorschlaege_wieder_anschliessen(config, bot)
        # Der Prozess läuft ohnehin durch — er ist damit der zuverlässigste Ort, die in
        # der Ansage zugesagte Frist einzuhalten, auch wenn der nächtliche Stapel steht.
        # Ein beendeter Faden ist nicht ``None``: ohne ``_erledigt`` bliebe eine Zusage
        # nach dem ersten Fehlschlag für immer liegen, und ``on_ready`` kommt bei jeder
        # Wiederverbindung noch einmal vorbei.
        if _erledigt(laeufe.frist):
            laeufe.frist = asyncio.create_task(
                recordings.taeglich(config, melden=_fristmelder(config, bot))
            )
        # Zwei Fristen, zwei Fäden: die eine gilt jeder Audiospur auf dieser Box, die
        # andere einer verabschiedeten Runde. Beide gehören dem Prozess und keiner Runde.
        if _erledigt(laeufe.abschied):
            laeufe.abschied = asyncio.create_task(lebenszyklus.taeglich(config))

    @bot.event
    async def on_voice_state_update(member, before, after) -> None:
        # Discord meldet hier keine Runde, sondern einen Kanal — und der sagt die Gilde.
        # Ohne diesen Umweg beantwortete das Ereignis der einen Gilde die Frage »läuft
        # etwas?« mit dem Mitschnitt einer anderen.
        wo = after.channel or before.channel
        if wo is None:
            return
        lauf = laeufe.fuer_gilde(wo.guild.id)
        if lauf is None:
            return
        aufnahme = lauf.aufnahme
        if aufnahme is None:
            return
        # Vor allem anderen und noch vor dem Blick auf das Mitglied: sitzt der Bot
        # überhaupt noch in dem Kanal, dem Ansage und Einwilligung gehören? Wurde er
        # gezogen, endet die Aufnahme — und der Nachzügler unten bekäme sonst einen
        # Protokolleintrag, dessen Ansage in einem Kanal lief, den er nie betreten hat.
        # Sein eigenes Verschieben meldet Discord dem Bot als Ereignis wie jedes andere.
        if lauf.stimme is not None and not lauf.stimme.im_kanal():
            # Die Frage nach dem Wohin **vor** dem ersten ``await``: danach hat der
            # Beender ``lauf.stimme`` schon geleert, und die Begründung wäre keine mehr.
            await _abschied_beim_kanalverlust(bot, lauf, aufnahme, lauf.stimme.woanders())
            return
        if member.bot:
            return
        unserer = aufnahme.kanal.id
        gekommen = after.channel is not None and str(after.channel.id) == unserer
        gegangen = before.channel is not None and str(before.channel.id) == unserer
        # Beides zugleich heißt: derselbe Kanal, nur stummgeschaltet oder verschoben.
        if gekommen and not gegangen:
            _leerlauf_absagen(lauf)
            protokolliert = await recorder.nachzuegler(
                config,
                lauf.stimme,
                aufnahme,
                consent.Member(id=str(member.id), name=member.display_name),
            )
            # Nur wenn die Ansage wirklich gehört wurde: ohne Eintrag steht diese Person
            # in keinem Einwilligungsprotokoll, und ohne das gibt es nichts zuzuordnen.
            if protokolliert is not None:
                await _zuordnung_klaeren(bot, lauf, aufnahme, member)
        elif gegangen and not gekommen:
            verblieben = _menschen(lauf)
            if not verblieben:
                # Immer neu stellen, nicht nur wenn keiner läuft: sonst zählt die Frist ab
                # dem ersten Gehen, und wer bei T=89 zurückkommt und bei T=89,5 wieder
                # geht, hat eine halbe Sekunde Karenz statt der zugesagten neunzig.
                _leerlauf_absagen(lauf)
                lauf.leer = asyncio.create_task(_abschied_bei_leere(config, bot, lauf, aufnahme))
            elif len(verblieben) == 1:
                await _allein_melden(bot, lauf, verblieben[0])

    return bot


def run(config: Config) -> None:
    logger.info("Aufnahme-Bot: verbinde mit dem Discord-Gateway")
    discord = _discord()
    bot = baue(config)
    # Der nächtliche Lauf hängt an diesem Prozess (#229) — aber neben der Schleife, nicht
    # in ihr: ``nightly.starten`` gibt einen eigenen Faden, und der Lauf selbst bekommt in
    # ``jobs.start`` noch einen. Auf der Ereignisschleife bliebe während einer Verschriftung
    # der Herzschlag zu Discord aus, und der Bot fiele mitten in der Nacht vom Gateway.
    nightly.starten(config, danach=_nachtmelder(config, bot))
    # Dasselbe Muster, dieselbe Begründung, nur tagsüber: der Mitlauf verschriftet die
    # Häppchen, während die Sitzung noch läuft (#269). Mitten in der Sitzung wöge der
    # Abfall vom Gateway schwerer als nachts — dort schneidet dieser Prozess gerade mit.
    mitlauf.starten(config)
    try:
        bot.run(config.discord_bot_token)
    except discord.errors.PrivilegedIntentsRequired as fehler:
        raise BotHaelt(RECHTE_FEHLEN) from fehler
    except discord.errors.LoginFailure as fehler:
        raise BotHaelt(TOKEN_ABGELEHNT) from fehler
