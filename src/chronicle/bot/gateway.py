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
from datetime import UTC, datetime
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
from chronicle.discord import grenzen
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
KENNUNG_BETRETEN = "betreten"
KENNUNG_KANAL = "kanal"
KENNUNG_QUELLE = "quelle"
KENNUNG_LOESCHEN = "loeschen"
KENNUNG_EINLESEN = "einlesen"
KENNUNG_SITZUNG = "sitzung"

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

# Der Test schneidet mit, verwirft und trennt: liefe er in einen laufenden Mitschnitt
# hinein, nähme er ihm die Verbindung weg. Also wird gesagt, dass nichts geschieht — und
# es geschieht auch nichts.
PROBE_NICHT_STOEREN = (
    "Ich schneide gerade mit — für einen Empfangstest fasse ich das nicht an; er würde die "
    "laufende Aufnahme abreißen. Der Mitschnitt läuft weiter. Nach `/aufnahme stop` gerne."
)

PROBE_LAEUFT = (
    "Ich prüfe gerade schon den Empfang, das dauert nur ein paar Sekunden — danach noch einmal."
)

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

# Nicht angehalten, sondern gesagt: der Betreiber hat entschieden, dass niemand ungefragt
# seine Spur verliert. Der Satz nennt keinen Namen und keine Kennung — er steht im Thread
# der Sitzung, und wer gemeint ist, weiß es, weil er als Einziger im Sprachkanal sitzt.
ALLEIN = (
    "Im Sprachkanal ist außer mir nur noch **eine** Person — und ich schneide weiter mit. "
    "Ich sage es, weil die Ansage einer Gruppe galt, die gerade nicht mehr da ist: wer so "
    "nicht aufgezeichnet werden möchte, gibt `/aufnahme stop` oder verlässt den Kanal. "
    "Kommen die anderen zurück, läuft dieselbe Aufnahme weiter — wer hereinkommt, hört die "
    "Ansage noch einmal."
)

LEER_GESCHEITERT = (
    "Im Sprachkanal war niemand mehr, aber das Beenden ist schiefgegangen — die Aufnahme "
    "gilt weiter als laufend, und ich bin womöglich noch im Kanal. Von selbst sehe ich "
    "erst wieder nach, wenn jemand den Kanal betritt und ihn erneut verlässt. Bitte "
    "einmal `/aufnahme stop` geben: das nimmt genau diesen Lauf und reiht die Spuren "
    "nach. Der Grund steht im Log des Bots."
)

# Verschieben ist kein Umzug der Einwilligung: die Ansage lief in **einem** Kanal, und nur
# dort hat jemand zugestimmt. Wer im neuen Kanal sitzt, hat sie nie gehört.
VERSCHOBEN = (
    "Jemand hat mich aus #{kanal} in einen anderen Sprachkanal gezogen — deshalb ist der "
    "Mitschnitt beendet. Angesagt und eingewilligt wurde in #{kanal}; im neuen Kanal hat "
    "das niemand gehört, also nehme ich dort nicht auf. Was nach dem Wechsel noch ankam, "
    "steht in keiner Spur. Die Sitzung bleibt offen: hier weiterzuschreiben geht, und "
    "`/chronik fertig` bleibt eure Entscheidung. Soll im neuen Kanal mitgeschnitten "
    "werden, gebt dort `/aufnahme start` — die Ansage läuft dann dort."
)

VERSCHOBEN_GESCHEITERT = (
    "Jemand hat mich aus #{kanal} in einen anderen Sprachkanal gezogen, aber das Beenden "
    "ist schiefgegangen — die Aufnahme gilt weiter als laufend, und ich bin womöglich noch "
    "im falschen Kanal. Geschrieben wird dort nichts. Bitte einmal `/aufnahme stop` geben: "
    "das nimmt genau diesen Lauf und reiht die Spuren nach. Der Grund steht im Log des Bots."
)

# Ein Abriss ist kein Umzug: verschoben hat den Bot jemand, getrennt kann ihn auch das Netz
# haben. Beides endet gleich, begründet sich aber verschieden — und der Satz im Thread ist
# Wochen später die einzige Auskunft darüber, warum die Spuren an dieser Stelle aufhören.
GETRENNT = (
    "Meine Verbindung zu #{kanal} ist abgerissen — deshalb ist der Mitschnitt beendet. Ob "
    "mich jemand hinausgeworfen hat oder das Netz zuckte, sehe ich von hier aus nicht. Was "
    "nach dem Abriss gesprochen wurde, steht in keiner Spur. Die Sitzung bleibt offen: hier "
    "weiterzuschreiben geht, und `/chronik fertig` bleibt eure Entscheidung. Soll weiter "
    "mitgeschnitten werden, gebt `/aufnahme start` — die Ansage läuft dann noch einmal."
)

GETRENNT_GESCHEITERT = (
    "Meine Verbindung zu #{kanal} ist abgerissen, aber das Beenden ist schiefgegangen — die "
    "Aufnahme gilt weiter als laufend. Mitgeschrieben wird nichts mehr. Bitte einmal "
    "`/aufnahme stop` geben: das nimmt genau diesen Lauf und reiht die Spuren nach. Der "
    "Grund steht im Log des Bots."
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

# Steht schon ein Anfang im Kanal und bricht die Zustellung mittendrin ab, endet er mitten
# im Satz. Ein zerrissener Text, den niemand als zerrissen erkennt, ist schlimmer als eine
# fehlende Nachricht — also sagt der Abriss sich selbst an.
ABGERISSEN = (
    "⚠️ Der Text davor ist unvollständig: nur {zugestellt} von {ganz} Teilen {kam} durch, "
    "{fehlend} {fehlt}. Was fehlt, steht nicht hier — den Grund nennt das Log des Bots."
)

BEFEHLE = (
    "• `/chronik start` — ich lege die Sitzung an und öffne ihren Thread; ab dort wird jede "
    "Nachricht eine Notiz. Das Fenster davor nimmt das Foundry-Passwort; gebt ihr eines, "
    "stelle ich die Würfe aus eurem Foundry ein, während ihr spielt.\n"
    "• `/szene <Name>` — die Trennlinie zur nächsten Szene.\n"
    "• `/aufnahme start` — ich komme in deinen Sprachkanal, spiele eine hörbare Ansage "
    "und schneide **erst danach** mit, je Stimme eine eigene Spur.\n"
    "• `/aufnahme stop` — ich höre auf und gehe wieder; die Spuren werden nachts zu Text. "
    "Bin ich allein im Kanal, höre ich von selbst auf und sage es im Thread.\n"
    f"• `/aufnahme test` — {recorder.PROBE_DAUER} Sekunden lauschen und dir allein sagen, ob "
    "der Ton hier wirklich ankommt. Angesagt wird auch dafür, und alles Mitgeschnittene "
    "wird sofort gelöscht.\n"
    "• `/chronik fertig` — Sitzung abschließen: eine laufende Aufnahme beende ich zuerst; "
    "danach Zahlen holen, verschriften, Chronik schreiben. Nach dem Passwort frage ich "
    "nur, wenn **du** beim Start keines gabst.\n"
    "• `/chronik abgleich` — nur die Zahlen holen, ohne Sitzung; das Fenster nimmt das "
    "Passwort.\n"
    "• `/chronik nacherzaehlung` — mehrere Sitzungen als Prosa; belegt und erzählt bleiben "
    "getrennt.\n"
    "• `/chronik einlesen` — ein vorhandenes Notizdokument hängt ihr hier an, ich mache "
    "Sitzungen und Szenen daraus. Vorher zeige ich, was entstünde.\n"
    "• `/suche <Wort>` — ich sehe in Notizen, Diktaten, Chroniken und Register nach; jeder "
    "Treffer führt zurück an seine Stelle.\n"
    "• `/wer <Name>` — was im Register über einen Namen steht.\n"
    "• `/register offen` — Registervorschläge bestätigen oder verwerfen.\n"
    "• `/zuordnung` — wer von euch welchen Foundry-Spieler spielt.\n"
    "• `/setup` — Foundry, Kanal, Uhrzeit, Zone und Quelle ändern; nur für die "
    "Verwaltung.\n"
    "• `/chronik sitzung-loeschen` — **eine** Sitzung samt ihren Aufnahmen löschen, nach "
    "Rückfrage; nur für die Administration.\n"
    "• `/chronik loeschen` — alles von dieser Runde löschen, nach Rückfrage; nur für die "
    "Administration.\n"
    "• `/aufnahme hilfe` — alles noch einmal in Ruhe.\n"
)

# Der eine Satz, der rechtlich trägt (§201 StGB): ohne ihn ist die Vorstellung nur noch
# eine Ankündigung. Er steht deshalb als Konstante da und nicht als Halbsatz mitten im
# Absatz — was geteilt zugestellt wird, muss ihn nachweislich im **ersten** Stück haben,
# und das prüft ein Test gegen genau diese Konstante.
#
# Daraus folgt die Reihenfolge in **jedem** Text, der ihn führt: der Ausweg steht **vor**
# der Befehlsliste. Die Liste wächst mit jedem neuen Befehl und schiebt alles hinter sich
# irgendwann in eine zweite Nachricht; was vor ihr steht, kommt zuerst und damit sicher an.
AUSWEG = (
    "Wer nicht aufgezeichnet werden möchte, verlässt den Sprachkanal — außerhalb nehme "
    "ich nichts auf."
)

HILFE = (
    "**So schneide ich eine Sitzung mit**\n"
    f"{AUSWEG} Wer später dazukommt, hört die Ansage noch einmal. "
    # Nicht in BEFEHLE und damit nicht in der Vorstellung: dort steht der Ausweg vorn,
    # und dieser Fall tritt erst mitten in der Sitzung ein. Gesagt wird er ohnehin,
    # wenn er eintritt.
    "Bleibt eine Person allein im Sprachkanal zurück, schneide ich weiter mit und sage "
    "ihr das im Thread; `/aufnahme stop` beendet es. "
    f"Die Aufnahmen werden nach {recordings.RETENTION_TAGE} Tagen gelöscht.\n"
    f"{BEFEHLE}"
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
    f"und Sprecher eine eigene Spur. Bis dahin ist Zeit: {AUSWEG} "
    f"Die Tonspuren werden nach {recordings.RETENTION_TAGE} Tagen gelöscht.\n"
    "**So bedient ihr mich:**\n"
    f"{BEFEHLE}"
    "Meine Antworten sieht immer nur, wer den Befehl gegeben hat."
)

# Steht im Kanal, **bevor** die Ansage läuft — wie die Vorstellung vor einer Aufnahme, und
# aus demselben Grund: hier wird zehn Sekunden lang wirklich aufgezeichnet. Dass alles
# gleich danach gelöscht wird, macht die Ansage nicht entbehrlich, sondern nur kurz.
PROBE_VORSTELLUNG = (
    "**Empfangstest.** Ich prüfe, ob euer Ton bei mir überhaupt lesbar ankommt — das ist "
    "von außen sonst nicht zu sehen. Gleich kommt die hörbare Ansage, danach höre ich "
    f"{recorder.PROBE_DAUER} Sekunden zu und **lösche alles wieder**: nichts davon wird "
    "verschriftet, nichts geht in die Chronik. Sprecht in der Zeit einfach ein paar Sätze. "
    f"{AUSWEG}"
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
    "⚠️ **Daraus wird nichts: ich schneide nicht mit.** Was darüber steht, ist damit "
    "hinfällig — es läuft keine Aufnahme, und in die Chronik geht davon nichts ein. "
    "Grund: {grund}"
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
    """Eine Instanz pro Gruppe — also höchstens eine Aufnahme zur Zeit."""

    def __init__(self) -> None:
        self.stimme: Sprachverbindung | None = None
        self.aufnahme: Aufnahme | None = None
        # Der Empfangstest steht bewusst **neben** der Aufnahme und nicht in ihr: er hält
        # seine Verbindung selbst und räumt sie selbst ab. Läge er in ``aufnahme``, reihte
        # ``/aufnahme stop`` seine Probespuren ein — genau das, was nie geschehen darf.
        self.probe = False
        self.frist = None
        self.abschied = None
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
        # Je offener Sitzung ein Beobachter von Foundry. Anders als der Mitschnitt gibt es
        # ihn mehrfach: der Bot bedient mehrere Gilden, und deren Abende überschneiden sich.
        self.stroeme: dict[int, object] = {}


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
        # die Spuren lägen uneingereiht, und ``/aufnahme stop`` antwortete ab jetzt immer
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
        kam="kam" if zugestellt == 1 else "kamen",
        fehlend=fehlend,
        fehlt="fehlt" if fehlend == 1 else "fehlen",
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


async def _in_den_thread(bot, aufnahme: Aufnahme, text: str) -> bool:
    """Ein Satz in den Thread der Sitzung — dort liest die Runde ohnehin mit.

    Nicht ``_sagen``: das antwortet einem, der gerade etwas angeklickt hat. Hier gibt es
    niemanden, der wartet — der Beobachter meldet sich von selbst, an die Runde.

    Zurück kommt, ob es einen Weg dorthin gab. Wo der Satz nur begleitet, ist das
    gleichgültig; wo er die Bedingung des Weitermachens ist, hängt daran die Entscheidung.
    Ein **fortgeräumter** Thread ist dabei kein Weg, sondern ein fehlender: Discords 404
    kommt beim nächsten Ereignis genauso wieder, und wer ihn wie ein Zucken behandelt,
    schneidet ewig weiter, ohne dass je etwas gesagt wurde. Alles andere fliegt weiter —
    ein zuckendes Discord ist beim nächsten Wechsel womöglich wieder da.
    """
    # Die Aufnahme hält ihre Runde seit Stunden. Ist sie inzwischen gelöscht und ihre
    # Kennung neu vergeben, führte die Frage nach dem Thread in eine fremde Kampagne.
    gemeint = lebenszyklus.dieselbe(aufnahme.runde)
    if gemeint is None:
        return False
    return await _in_den_sitzungsthread(bot, gemeint, aufnahme.session_id, text)


async def _in_den_sitzungsthread(bot, runde: Runde, session_id: int, text: str) -> bool:
    """Derselbe Weg, ohne Aufnahme: ein Befehl kennt seine Runde, aber keinen Mitschnitt."""
    thread_id = chronik.thread_der_sitzung(runde, session_id)
    if thread_id is None:
        logger.info("Sitzung %s hat keinen Thread — es bleibt ungesagt.", session_id)
        return False
    kennung = int(thread_id)
    discord = _discord()
    try:
        thread = bot.get_channel(kennung) or await bot.fetch_channel(kennung)
        await _zustellen(thread.send, text)
    except discord.NotFound:
        logger.info("Thread %s der Sitzung %s ist fort — es bleibt ungesagt.", kennung, session_id)
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
        angekommen = await _in_den_thread(bot, aufnahme, ALLEIN)
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
    ``/zuordnung`` dieselbe Person auf ein anderes Konto legt, verlöre seine Entscheidung
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
        gesagt = await _in_den_thread(bot, aufnahme, stand.vermerk)
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
            await _in_den_thread(bot, aufnahme, gescheitert)
        return
    if not meldungen:
        # Leer heißt: ein anderer war schneller. Dann gehört ihm auch der Satz dazu.
        return
    # Die Erfolgsmeldung steht außerhalb: umfasste ein ``try`` beides, machte ein zuckendes
    # ``thread.send`` aus einem gelungenen Ende einen gemeldeten Fehlschlag — und schickte
    # zu ``/aufnahme stop``, das dann »keine Aufnahme« antwortet. Bleibt sie ungesagt, ist
    # das ein fehlender Satz; die Fehlermeldung wäre ein falscher.
    #
    # Was dieser Fang **nicht** mehr verdeckt, ist die eigene Überlänge: eine Runde mit
    # dreißig Spuren reihte hier dreißig Meldungen aneinander, Discord wies die Nachricht
    # ab, und der Satz fiel still weg (#120). Verdecken kann er dafür jetzt einen halb
    # zugestellten Text — mehrere Stücke sind mehrere Aufrufe. Erkennbar bleibt das nicht
    # durch diesen Fang, sondern weil ``_zustellen`` den Abriss vorher selbst ansagt.
    try:
        await _in_den_thread(bot, aufnahme, " ".join((beendet, *meldungen)))
    except Exception:  # noqa: BLE001
        logger.exception("Das Ende des Mitschnitts blieb ungesagt")


async def _abschied_bei_leere(bot, lauf: _Lauf, aufnahme: Aufnahme) -> None:
    """Nach der Frist noch einmal nachsehen — und dann Schluss.

    Noch einmal, weil die Frist genau dafür da ist: wer die Verbindung verliert und
    zurückkommt, soll keine zerschnittene Sitzung vorfinden. Und gegen *diese* Aufnahme,
    denn in der Frist kann eine neue begonnen haben, die diese Frist nichts angeht.
    """
    await asyncio.sleep(LEER_FRIST)
    if lauf.aufnahme is not aufnahme or _menschen(lauf):
        return
    # Die Sitzung statt des Kanalnamens (#206/#211): der Name beschreibt die Struktur
    # einer fremden Gilde, die Sitzungskennung niemanden. Sie genügt trotzdem, weil an
    # ihr Thread, Spuren und Einwilligungsnachweis hängen — und im Nachweis steht der
    # Kanalname weiter, einen Schritt entfernt und dort mit Grund.
    logger.info("Sitzung %s: Sprachkanal leer — der Mitschnitt endet.", aufnahme.session_id)
    await _beenden_und_sagen(bot, lauf, aufnahme, LEER_BEENDET, LEER_GESCHEITERT)


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
                meldung = await asyncio.to_thread(chronik.ereignisse_abholen, config, strom)
                zugestellt = not meldung.text or await _in_den_sitzungsthread(
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


def _strom_abbestellen(lauf: _Lauf, session_id: int) -> None:
    """Beim Abschluss ist Schluss: der Abgleich löst das Passwort ein und holt alles ganz."""
    faden = lauf.stroeme.pop(session_id, None)
    if faden is not None:
        faden.cancel()


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


async def _widerrufen(ziel, fehler: BaseException) -> None:
    """Die öffentliche Ankündigung dort zurücknehmen, wo sie steht.

    Gesagt wird der Grund, den auch die ephemere Absage nennt — bei einem erwarteten
    Fehler sein Satz, sonst nur die Art. Was im Fehler sonst noch stecken kann, bleibt im
    Log des Betreibers: der Kanal ist der öffentlichste Ort, den dieser Bot hat.

    Scheitert der Widerruf selbst, fliegt der **ursprüngliche** Fehler weiter — der ist
    die Auskunft, auf die der Aufrufer wartet. Dass die Ankündigung ohne ihn stehenblieb,
    steht dann im Log; mehr ist von hier aus nicht zu erreichen.
    """
    grund = (
        str(fehler)
        if isinstance(fehler, BotFehler)
        else UNERWARTET.format(typ=type(fehler).__name__)
    )
    try:
        await _zustellen(ziel.send, WIDERRUF.format(grund=grund))
    except Exception:  # noqa: BLE001
        logger.exception("Die Vorstellung blieb im Kanal stehen — der Widerruf kam nicht durch")


def _melder(ziel) -> Callable[[str], None]:
    """Der Lauf trägt sich in einem eigenen Faden zu; melden darf nur die Ereignisschleife."""
    schleife = asyncio.get_running_loop()

    def melden(text: str) -> None:
        asyncio.run_coroutine_threadsafe(_zustellen(ziel.send, text), schleife)

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
) -> str:
    """Thread, Sitzung, Passwort, Beobachter — ein Satz, der jeden Ausgang unterscheidbar macht.

    Der breite Fang ist das Sicherheitsnetz, das ``@antwortet`` sonst um den Befehlsrumpf
    legt: aus dem Rückruf eines Fensters entkäme eine Ausnahme in py-cords ``on_error``,
    das die Interaktion **nie** beantwortet — der Thread stünde, die Sitzung nicht, und
    niemand erführe es. Scheitert dagegen erst die Begrüßung, stehen Thread und Sitzung
    schon; dann darf die Antwort nicht »versuch es noch einmal« sagen, sonst legt der
    zweite Anlauf beides ein zweites Mal an.
    """
    try:
        thread = await _thread_anlegen(ziel, chronik.threadname(titel))
        sitzung = chronik.sitzung_anlegen(runde, str(thread.id), titel)
        gemerkt = chronik.passwort_merken(runde, eingabe, wer)
    except BotFehler as fehler:
        return GESCHEITERT.format(grund=str(fehler))
    except Exception as fehler:  # noqa: BLE001
        logger.exception("Sitzungsstart gescheitert")
        return GESCHEITERT.format(grund=UNERWARTET.format(typ=type(fehler).__name__))
    hinweis = chronik.starthinweis(config, runde, gemerkt)
    # Nur mit hinterlegtem Passwort: ohne eines käme der Beobachter beim ersten Blick an
    # keinen Server und beendete sich sofort. Der Strom hängt damit an derselben
    # Entscheidung wie die Zahlen selbst — wer es nicht gibt, spielt ohne beides weiter.
    if gemerkt:
        _strom_stellen(config, bot, lauf, runde, sitzung)
    try:
        await _zustellen(thread.send, chronik.ANGELEGT)
    except Exception:  # noqa: BLE001
        logger.exception("Begrüßung im neuen Thread nicht zugestellt")
        return f"{chronik.STUMM_ANGELEGT.format(thread=thread.mention)} {hinweis}"
    return f"{chronik.THREAD_STEHT.format(thread=thread.mention)} {hinweis}"


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
            antwort = await _sitzung_eroeffnen(
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
    _strom_abbestellen(lauf, session_id)
    meldungen: tuple[str, ...] = ()
    try:
        meldungen = await _mitschnitt_beenden(lauf, runde)
        meldung = chronik.abschluss_starten(
            config, runde, session_id, passwort, wer=wer, merken=merken, melden=_melder(kanal)
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
    """``/setup`` ist die Schranke vor dem Foundry-Passwort.

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
    """Zwei Menüs unter dem Fenster: wohin die Chronik geht und woher die Zahlen kommen.

    Beide wirken sofort und beide gegen den Stand von jetzt: ein Kanal aus dieser Gilde, in
    die Runde einer fremden geschrieben, schickte deren Chroniken künftig hierher, und eine
    dort gesetzte Testwelt füllte deren Protokolle mit erfundenen Zahlen. Anders als ein
    Löschknopf ist beides keine einmalige Fehlhandlung, sondern eine dauerhafte.

    Nach einer Wahl bleibt die Ansicht stehen, statt zu verschwinden: es sind zwei
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

    class Einrichtungsansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(kanal)
            self.add_item(quelle)

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
                f"{meldung} {einrichten.KANAL_FRAGE} {einrichten.QUELLE_FRAGE}",
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
        # Tondateien und Zeilen einer langen Sitzung: das dauert und gehört nicht auf die
        # Ereignisschleife — solange sie rechnet, antwortet der Bot niemandem.
        meldung = await asyncio.to_thread(chronik.sitzung_geloescht, config, gemeint, marke)
        await interaction.response.edit_message(content=meldung, view=None)

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


def _einleseansicht(config: Config, runde, abende):
    """Ein Knopf vor fünfzehn Sitzungen: erst zeigen, dann auf Zuruf schreiben.

    Wie die Löschansicht entscheidet auch dieser Knopf gegen den Stand von jetzt: die
    Ansicht lebt eine Viertelstunde, in der die Runde gelöscht und ihre Kennung neu
    vergeben sein kann — der Altbestand der einen Gruppe landete sonst bei einer fremden.

    Und er wirkt genau einmal: ``stop`` nimmt die Ansicht aus Discords Zustellung, damit ein
    zweiter Klick gar nicht erst hier ankommt. Die Löschansicht braucht das nicht, weil die
    gelöschte Runde jeden weiteren Klick von selbst ins Leere laufen lässt; hier bleibt die
    Runde stehen, also muss der Knopf es selbst tun.

    Gerechnet wird auch das Recht, und nicht nur am Befehl: die Vorschau steht als Nachricht
    da, und klicken könnte jeder, der sie sieht.
    """
    discord = _discord()

    ja = discord.ui.Button(label=chronik.DOKUMENT_JA, custom_id=f"{KENNUNG_EINLESEN}:{runde.id}:ja")
    nein = discord.ui.Button(
        label=chronik.DOKUMENT_NEIN, custom_id=f"{KENNUNG_EINLESEN}:{runde.id}:nein"
    )

    @_geklickt
    async def bestaetigt(interaction) -> None:
        if not _darf_loeschen(getattr(interaction, "user", None)):
            await interaction.response.edit_message(content=einrichten.NUR_ADMIN, view=None)
            return
        gemeint = await _noch_dieselbe(config, interaction, runde)
        if gemeint is None:
            return
        ansicht.stop()
        meldung = chronik.dokument_anlegen(gemeint, abende)
        await interaction.response.edit_message(content=meldung, view=None)

    @_geklickt
    async def verworfen(interaction) -> None:
        ansicht.stop()
        await interaction.response.edit_message(content=chronik.DOKUMENT_ABGEBROCHEN, view=None)

    ja.callback = bestaetigt
    nein.callback = verworfen

    class Einleseansicht(discord.ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=erinnern.FRIST)
            self.add_item(ja)
            self.add_item(nein)

    ansicht = Einleseansicht()
    return ansicht


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
            super().__init__(timeout=erinnern.FRIST)
            for zeile, eintrag in enumerate(stand.eintraege):
                self.add_item(schild(eintrag, zeile))
                for art, schrift in erinnern.ENTSCHEIDUNGEN:
                    self.add_item(knopf(eintrag, art, schrift, zeile))

    return Registeransicht()


async def _uebernahme_sagen(bot, runde, ergebnis: erinnern.Zugeordnet) -> None:
    """Ein übernommenes Konto bekommt Tageslicht: die Runde erfährt es, die Vorbesitzerin auch.

    Das Umhängen ist der Schritt mit der größten Folge — jemand nimmt einer anderen ihr
    Konto. Von selbst ist er der stillste: die Antwort auf den Klick sieht nur, wer geklickt
    hat, und in der Zuordnung bleibt danach genau eine Zeile stehen. Dass ``/zuordnung`` die
    Runde nebeneinander zeigt, trägt das nicht — die Ansicht reicht bis ``PRO_SEITE``, und
    ab der sechsten Person steht die Vorbesitzerin weder vorher noch nachher darin.

    Zwei Wege, und der **Thread** ist der belastbare: er erreicht die Runde auch dann, wenn
    die Vorbesitzerin keine Direktnachrichten annimmt. Deren Ausbleiben verwirft die
    Übernahme deshalb nicht; es wird protokolliert, ohne Namen und ohne Kennung.

    Ob es den Thread überhaupt gab, steht im Log: eine Runde ohne Sitzung hat keinen, und
    »der Thread-Vermerk trägt sie« wäre dann eine Auskunft über etwas, das nicht geschehen
    ist.
    """
    wer, vorher, spieler = ergebnis.wer, ergebnis.vorher, ergebnis.spieler
    sitzung = chronik.letzte_sitzung(runde)
    im_thread = sitzung is not None and await _in_den_sitzungsthread(
        bot,
        runde,
        sitzung,
        erinnern.UEBERNAHME_VERMERK.format(
            name=wer.discord_name, spieler=spieler.name, vorher=vorher.discord_name
        ),
    )
    if not im_thread:
        # Nur die Tatsache, keine Vorhersage: der Brief an die Vorbesitzerin geht erst
        # danach los und kann genauso scheitern. Ob er ankam, sagt der Aufrufer.
        logger.warning("Die Übernahme steht in keinem Thread.")
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
    ohne ``uebernehmen``, anders als in ``/zuordnung``, wo umgehängt werden darf und die
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
            await _in_den_thread(
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


def _datei(discord, beschreibung: str):
    """Ein Pflicht-Anhang eines Slash-Befehls — als Vorgabewert, aus dem Grund aus ``_feld``."""
    return discord.Option(discord.Attachment, description=beschreibung, required=True)


def _notizdatei(anhang) -> chronik.Notizdatei:
    """Discords Anhang, so weit das Einlesen ihn braucht — gelesen wird erst auf Zuruf."""
    return chronik.Notizdatei(filename=anhang.filename, size=anhang.size, lesen=anhang.read)


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
            await _zustellen(ctx.respond, LAEUFT_SCHON, ephemeral=True)
            return
        # Der Test hält gerade dieselbe Sprachverbindung und trennt sie gleich wieder — ein
        # Mitschnitt, der jetzt begänne, verlöre sie mitten im Satz.
        if lauf.probe:
            await _zustellen(ctx.respond, PROBE_LAEUFT, ephemeral=True)
            return
        # Dieselbe Schranke wie vor ``/chronik start``, und vor dem Beitreten: eine Gilde
        # ohne eigene Runde nimmt nicht auf, eine ruhende erst recht nicht.
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        kanal = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if kanal is None:
            await _zustellen(ctx.respond, NICHT_IM_KANAL, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        stimme = Sprachverbindung(await kanal.connect())
        ziel = _vorstellungsziel(ctx, kanal)
        try:
            await _zustellen(ziel.send, VORSTELLUNG)
        except BaseException as fehler:
            # Die Vorstellung ist die lesbare Hälfte der Einwilligung: sie nennt den
            # Ausweg, und zwar solange noch nichts mitgeschnitten wird. Kam sie nicht
            # durch, wird nicht gestartet. Zurückzunehmen ist trotzdem etwas: sie ist
            # länger als eine Discord-Nachricht, und scheitert erst das zweite Stück,
            # steht »gleich schneide ich mit« bereits im Kanal — mit dem Abriss-Hinweis
            # daneben, der von einer Aufnahme nichts zurücknimmt. Der Widerruf tut es.
            # Kam wirklich nichts an, ist der Kanal ohnehin zu und er verfällt ins Log.
            await _widerrufen(ziel, fehler)
            await stimme.trennen()
            raise
        try:
            lauf.aufnahme = await recorder.starten(config, stimme, runde)
        except BaseException as fehler:
            # Erst der Widerruf, dann das Aufräumen: ``trennen`` geht ans Netz und kann
            # selbst stolpern — danach käme er nie, und die Ankündigung bliebe stehen.
            await _widerrufen(ziel, fehler)
            await stimme.trennen()
            raise
        lauf.stimme = stimme
        await _zustellen(ctx.respond, recorder.GESTARTET, ephemeral=True)
        # Nach der Antwort und nicht davor: die Frage geht an jede anwesende Person
        # einzeln, und wer den Befehl gab, soll nicht erst danach erfahren, dass
        # mitgeschnitten wird. Scheitern kann sie hier nicht mehr — sie fängt selbst.
        for wer in stimme.anwesende():
            await _zuordnung_klaeren(bot, lauf, lauf.aufnahme, wer)

    @gruppe.command(name="stop", description="Aufnahme beenden und die Spuren einreihen")
    @antwortet
    async def stop(ctx) -> None:
        if lauf.aufnahme is None:
            await _zustellen(ctx.respond, LAEUFT_NICHT, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        meldungen = await _mitschnitt_beenden(lauf)
        # Leer heißt: in der Zwischenzeit war ein anderer schneller — der leere Kanal etwa.
        # Das ist kein Fehlschlag, und so ausgesprochen zu werden verdient er auch nicht.
        await _zustellen(ctx.respond, " ".join(meldungen) or LAEUFT_NICHT, ephemeral=True)

    @gruppe.command(name="test", description="Kurz lauschen und sagen, ob der Ton wirklich ankommt")
    @antwortet
    async def empfangstest(ctx) -> None:
        """Die Frage »hört der Bot überhaupt?« — beantwortet in Discord statt im Log."""
        if lauf.aufnahme is not None:
            await _zustellen(ctx.respond, PROBE_NICHT_STOEREN, ephemeral=True)
            return
        if lauf.probe:
            await _zustellen(ctx.respond, PROBE_LAEUFT, ephemeral=True)
            return
        # Dieselbe Schranke wie vor ``/aufnahme start``: eine Gilde ohne eigene Runde prüft
        # hier nichts, eine ruhende erst recht nicht — es wird aufgezeichnet.
        runde = chronik.runde_verlangen(config, ctx.guild_id)
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
                # Wie bei ``/aufnahme start``: was von einer geteilten Ankündigung schon
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

    @gruppe.command(name="hilfe", description="Was der Bot tut und wie man ihn bedient")
    @antwortet
    async def hilfe(ctx) -> None:
        await _zustellen(ctx.respond, HILFE, ephemeral=True)

    @chronikgruppe.command(name="start", description="Sitzung anlegen und den Thread öffnen")
    @antwortet
    async def chronik_start(
        ctx,
        titel=_feld(discord, "Titel der Sitzung"),  # noqa: B008
    ) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        if not chronik.foundry_im_spiel(config, runde):
            # Ohne Server gäbe es nichts, wo das Passwort vorgezeigt würde — es läge nur
            # bis zur Frist herum. Ohne Fenster kann der Befehl selbst aufschieben.
            await ctx.defer(ephemeral=True)
            await _zustellen(
                ctx.respond,
                await _sitzung_eroeffnen(config, bot, lauf, ctx, runde, titel, "", _wer(ctx)),
                ephemeral=True,
            )
            return
        # Kein ``defer`` davor: ein Fenster geht nur als *erste* Antwort auf den Befehl.
        # Deshalb entsteht die Sitzung erst im Rückruf des Fensters, der selbst aufschiebt.
        await ctx.send_modal(_startfenster(config, bot, lauf, runde, titel))

    @chronikgruppe.command(
        name="fertig", description="Sitzung abschließen und die Chronik anstoßen"
    )
    @antwortet
    async def chronik_fertig(ctx) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        # Nicht der Kanal, sondern die Runde bestimmt die Sitzung — wie bei ``/aufnahme``.
        # Nach ``/aufnahme stop`` steht man im Sprachkanal, und dort abzuweisen hieß, zu
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
                    config, runde, sitzung, geheim, lauf, ctx.channel, wer, merken=False
                ),
                ephemeral=True,
            )
            return
        fremd = chronik.passwort_gehalten(runde)
        hinweis = chronik.FREMDES_HINWEIS if fremd else chronik.PASSWORT_HINWEIS
        await ctx.send_modal(_passwortfrage(config, runde, sitzung, lauf, hinweis))

    @chronikgruppe.command(
        name="abgleich", description="Die Zahlen aus Foundry holen, ohne eine Sitzung zu führen"
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
        name="nacherzaehlung", description="Einen Sitzungsbereich als Prosa nacherzählen"
    )
    @antwortet
    async def chronik_nacherzaehlung(
        ctx,
        von=_feld(discord, "Ab welcher Sitzung, als Datum"),  # noqa: B008
        bis=_feld(discord, "Bis zu welcher Sitzung, als Datum"),  # noqa: B008
    ) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        # Der Melder wird **hier** gebaut, in der Ereignisschleife: der Lauf trägt sich in
        # einem eigenen Faden zu und hätte dort keine, an die er sich hängen könnte.
        melden = _melder(ctx.channel)
        await ctx.defer(ephemeral=True)
        await ctx.respond(
            chronik.nacherzaehlung_starten(
                config, runde, von, bis, str(ctx.channel_id), melden=melden
            ),
            ephemeral=True,
        )

    @chronikgruppe.command(
        name="einlesen", description="Ein vorhandenes Notizdokument als Sitzungen anlegen"
    )
    @antwortet
    async def chronik_einlesen(
        ctx,
        datei=_datei(discord, "Das Notizdokument, ein Abschnitt je Abend"),  # noqa: B008
    ) -> None:
        """Im Kanal der Runde und nicht im Thread: ein Dokument deckt mehrere Abende ab.

        **Die Schranke ist die des Löschens** (Betreiber-Entscheidung, 2026-08-13). Es geht
        dabei nicht um die Menge — dass ein Dokument fünfzehn Abende trägt, ist der
        auffällige, aber nicht der tragende Grund. Tragend ist: wer die Chronik einer
        Kampagne rückwirkend umschreibt, greift genauso tief in sie ein wie wer sie
        fortnimmt, und rückgängig gibt es das nur einzeln, Sitzung für Sitzung. Ein
        Eingriff in die Vergangenheit einer Kampagne verdient deshalb dieselbe Schwelle
        wie ihre Zerstörung.

        Die Vorschau darunter ersetzt sie nicht: sie fängt das **Versehen** ab — ohne
        Bestätigung entsteht nichts —, aber nicht die **Absicht**. Genau diese
        Unterscheidung hat schon bei #171 die Schranke bestimmt.

        Angelegt wird hier noch nichts — der Befehl antwortet mit der Vorschau und einem
        Knopf darunter. Aufgeschoben wird davor: das Herunterladen der Datei geht ans Netz,
        und die drei Sekunden, die Discord der ersten Antwort lässt, reichen dafür nicht.
        Gerechnet wird noch einmal am Knopf.
        """
        if not _darf_loeschen(getattr(ctx, "author", None)):
            await _zustellen(ctx.respond, einrichten.NUR_ADMIN, ephemeral=True)
            return
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        await ctx.defer(ephemeral=True)
        vorschau = await chronik.dokument_vorschau(runde, _notizdatei(datei))
        ansicht = _einleseansicht(config, runde, vorschau.abende) if vorschau.abende else None
        await _zustellen(ctx.respond, vorschau.text, zuletzt={"view": ansicht}, ephemeral=True)

    @chronikgruppe.command(
        name="sitzung-loeschen", description="Eine einzelne Sitzung löschen, nach Rückfrage"
    )
    @antwortet
    async def chronik_sitzung_loeschen(ctx) -> None:
        """Der kleine Weg neben ``/chronik loeschen``: ein Abend statt der ganzen Runde.

        **Die Schranke ist die der Administration, dieselbe wie vor der ganzen Runde**
        (Operator-Entscheidung, #171/#174) — und das ist nicht die naheliegende Antwort.
        Naheliegend wäre die Verwaltung: eine Sitzung fortzunehmen sieht nach Berichtigung
        aus, der überzählige Abend aus einem eingelesenen Dokument. Sie ist aber nicht die
        harmlosere Löschung, sondern die **schlimmere**: sie ist auswählbar *und* lautlos.
        Alles hier läuft ephemer, der Thread der Sitzung bleibt unverändert stehen, und wer
        geklickt hat, steht mit Absicht in keinem Log. Ein Verwalter nähme so genau den
        einen unbequemen Abend samt seinen Aufnahmen fort, und niemand in der Gruppe
        erführe es; ``/chronik loeschen`` kann man nicht heimlich drücken. Der Maßstab von
        ``_darf_loeschen`` trägt genau das: das Sofortige bekommt die strengere Schranke,
        das Langsame die Umkehrbarkeit — und ein langsames, umkehrbares Gegenstück zu
        dieser Löschung gibt es überhaupt nicht.

        Die Alternative steht offen: ``_darf_verwalten`` wäre vertretbar, wenn dafür die
        Heimlichkeit fiele — ein sichtbarer Vermerk im Kanal der Runde, dass dieser Abend
        gelöscht wurde. Das ist nicht gebaut; wer es baut, darf die Schranke senken.

        Gerechnet wird noch einmal am Menü und am Knopf.

        Eine ruhende Runde kommt hier nicht durch: sie ist verabschiedet, und wer sie ganz
        loswerden will, hat dafür ``/chronik loeschen``.
        """
        if not _darf_loeschen(getattr(ctx, "author", None)):
            await _zustellen(ctx.respond, einrichten.NUR_ADMIN, ephemeral=True)
            return
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        wahl = chronik.sitzungswahl(runde)
        ansicht = _sitzungswahlansicht(config, runde, wahl.zeilen) if wahl.zeilen else None
        await _zustellen(ctx.respond, wahl.text, zuletzt={"view": ansicht}, ephemeral=True)

    @chronikgruppe.command(
        name="loeschen", description="Alles von dieser Runde löschen, nach Rückfrage"
    )
    @antwortet
    async def chronik_loeschen(ctx) -> None:
        # Discord kennt ``default_member_permissions`` nur für den ganzen Befehl, und
        # ``/chronik start`` soll jedes Mitglied geben dürfen. Für diesen Unterbefehl steht
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
            await _zustellen(ctx.respond, einrichten.NUR_IM_SERVER, ephemeral=True)
            return
        # Die Angabe oben blendet den Befehl bei Discord aus; sie ist eine Vorgabe, die die
        # Serververwaltung überschreiben kann. Gerechnet wird deshalb auch hier.
        if not _darf_verwalten(getattr(ctx, "author", None)):
            await _zustellen(ctx.respond, einrichten.NUR_VERWALTUNG, ephemeral=True)
            return
        await ctx.send_modal(_einrichtungsfenster(config, ctx))

    @bot.slash_command(name=BEFEHL_SUCHE, description="In allem nachsehen, was geschrieben wurde")
    @antwortet
    async def suche(
        ctx,
        begriff=_feld(discord, "Wonach ich suchen soll"),  # noqa: B008
    ) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        await _antworten(ctx, erinnern.suche(runde, begriff))

    @bot.slash_command(name=BEFEHL_WER, description="Was im Register über einen Namen steht")
    @antwortet
    async def wer(
        ctx,
        name=_feld(discord, "Der Name, zu dem ich nachsehe"),  # noqa: B008
    ) -> None:
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
        await _antworten(ctx, stand.antwort, _zuordnungsansicht(bot, config, runde, stand))

    @bot.slash_command(name=BEFEHL_SZENE, description="Die Trennlinie zur nächsten Szene ziehen")
    @antwortet
    async def szene(
        ctx,
        name=_feld(discord, "Name der neuen Szene"),  # noqa: B008
    ) -> None:
        runde = chronik.runde_verlangen(config, ctx.guild_id)
        sitzung = chronik.sitzung_verlangen(runde, str(ctx.channel_id))
        # Sichtbar für alle: die Trennlinie gehört in den Thread, nicht nur zu dem, der
        # sie gezogen hat.
        await _zustellen(ctx.respond, chronik.szene_setzen(runde, sitzung, name), ephemeral=False)

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
            await _in_den_sitzungsthread(bot, runde, wechsel.sitzung, wechsel.antwort)

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
        await _zustellen(kanal.send, zurueck.text)
        if zurueck.wartet is not None:
            einrichten.wieder_im_dienst(config, zurueck.wartet)

    @bot.event
    async def on_guild_remove(gilde) -> None:
        einrichten.verabschieden(config.database_path, str(gilde.id))

    @bot.event
    async def on_ready() -> None:
        # Vor den Fristen: eine Runde, die niemand mehr erreicht, ist der dringendere Fall.
        await _verwaiste_runde_uebernehmen(config, bot)
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
                lauf.leer = asyncio.create_task(_abschied_bei_leere(bot, lauf, aufnahme))
            elif len(verblieben) == 1:
                await _allein_melden(bot, lauf, verblieben[0])

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
