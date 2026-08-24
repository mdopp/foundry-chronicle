"""Die Sitzung ist eine Spanne Zeit in einem Kanal.

``/session start`` beginnt sie im Kanal, in dem der Befehl kam — seit #271 im Regelfall
der Chat des Sprachkanals, an dem die Runde ohnehin sitzt, und kein eigener Thread mehr.
Danach braucht es kein Formular — **jede Nachricht dort ist eine Notiz**, ``/session scene`` zieht
die Trennlinie zur nächsten, eine Sprachnachricht wird ein Diktat, und ``/session done``
stößt den einen Lauf an, der aus alldem die Chronik macht. Wird der Sprachkanal leer,
erkennt der Bot den Abschluss selbst.

Die Grenze ist damit die **Zeit** und nicht mehr der Ort: was vor dem Start oder nach dem
Abschluss im selben Kanal steht, ist Gerede. Und höchstens eine Sitzung je Runde ist
offen — das verhinderte vorher der Thread von selbst.

Diese Datei kennt Discord nicht. Sie bekommt Kennungen, Text und Zeitpunkte und gibt Sätze
zurück; wer sie damit füttert, entscheidet ``gateway.py``.

Drei Regeln stehen über allem:

* **Die Gilde bestimmt die Runde.** Es gibt hier keinen Rückfall auf »die erste« — ein
  Kanal aus einem fremden Server, der in eine fremde Chronik schriebe, wäre genau das
  Leck, gegen das das Runden-Modell gebaut ist. Ohne Runde passiert nichts, und das wird
  gesagt statt verschluckt.
* **Die Szene entscheidet der Zeitpunkt der Nachricht**, nicht der des Ablegens. Deshalb
  darf eine Nachricht Tage später kommen und landet trotzdem in der Szene, in die sie
  gehört.
* **Es antwortet immer jemand.** Was nicht abgelegt werden konnte, bekommt eine Antwort;
  wer schweigt, lässt die Runde weiterschreiben ins Leere.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from chronicle import (
    dokument,
    jobs,
    lebenszyklus,
    notes,
    protocol,
    recordings,
    settings,
    zugang,
)
from chronicle import runde as runden
from chronicle import sprache as sprachen
from chronicle.bot import BotFehler
from chronicle.config import Config
from chronicle.foundry import service as foundry
from chronicle.foundry.model import ChatMessage
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

# Genannt wird das Recht und nicht nur der Befehl: ``/chronicle setup`` trägt
# ``default_member_permissions(manage_guild=True)``, und Discord blendet ihn damit für
# jeden ohne »Server verwalten« vollständig aus (#270). Wer ihn nicht sieht, sucht sonst
# nach einem Befehl, den es für ihn gar nicht gibt — ein Rat, den der Empfänger nicht
# befolgen kann, ist schlimmer als keiner.
KEINE_RUNDE = (
    "No round is set up for this server yet — I file nothing here and I invent nothing "
    "either. **Somebody with “Manage Server”** has to call `/chronicle setup` once; after "
    "that everything else works."
)

# Die Runde ist noch da, sie schweigt nur. Das zu sagen ist wichtiger, als es zu erklären:
# wer den Bot zurückholt, bekommt alles zurück — bis zum genannten Tag.
GESPERRT = (
    "This round is resting since I was removed from this server. Everything is still there "
    "until {datum}: invite me back and it carries on where you left off. After that it is "
    "deleted."
)

# Was ein Klick sagt, dessen Ansicht eine Runde meint, die es so nicht mehr gibt. Getan
# wird dann nichts: die Kennung darunter kann inzwischen einer fremden Gilde gehören.
VERALTET = (
    "This view is from earlier, and something has changed here since. I have changed "
    "nothing — call the command again."
)

NUR_IN_DER_SITZUNG = (
    "This only works while a session is running here. `/session start` begins one — after "
    "that everything typed in this channel counts."
)

# Die Grenze, die vorher der Thread von selbst zog: zwei Sitzungen nebeneinander hätten
# zwei Ziele für dieselbe getippte Zeile, und der Mitschnitt hinge an der falschen (#271).
SITZUNG_LAEUFT_SCHON = (
    "A session is already running here — there are no two side by side, otherwise nobody "
    "would know which one the typing belongs to. `/session done` closes the running one, "
    "then `/session start` begins the next."
)

# Was der Abschluss sagt, wenn wirklich keine Sitzung offen ist. Nur dann ist der Rat zu
# `/session start` richtig: steht eine, legte er eine zweite an, und die Aufnahmen der
# ersten blieben liegen (#156).
KEINE_SITZUNG = (
    "No session is running here right now, so I have nothing to close. `/session start` begins one."
)

ANGELEGT = (
    "The session is running. Write here from now on: **every message becomes a note**, a "
    "voice message or an audio file becomes a dictation. `/session scene <name>` begins the "
    "next scene, `/session done` closes the session — and when the voice channel goes "
    "empty, I do that by myself. Whoever edits or deletes a message edits or deletes the "
    "note with it."
)

SITZUNG_STEHT = "The session stands — from now on everything typed here counts."

# Die Sitzung steht, nur die Ansage im Kanal kam nicht durch. Das muss anders klingen als
# ein Fehlschlag: »noch einmal versuchen« legte hier eine zweite an — und niemand außer
# dem Aufrufer weiß, dass jede Zeile jetzt eine Notiz wird.
STUMM_ANGELEGT = (
    "The session stands — but my announcement in this channel did not arrive. Do **not** "
    "call `/session start` again; please tell the group yourself that every message here "
    "becomes a note from now on."
)

SZENE = "New scene “{name}”. Whatever is written from now on belongs to it."
SZENE_OHNE_NAMEN = "New scene. Whatever is written from now on belongs to it."

# Seit #269 nimmt der Mitlauf sie meist binnen einer Minute; spätestens der Abschluss holt
# sie. Zugesagt wird trotzdem nur das Spätestens: was der Mitlauf gerade schafft, hängt an
# einem Nachbardienst, und ein Versprechen auf die Minute wäre eines, das wir hier nicht
# halten können.
DIKTAT = (
    "Recording received — it is transcribed in the background, at the latest when you give "
    "`/session done`."
)

ZU_GROSS = "“{name}” is larger than {grenze} MB and stays where it is."

# Kein Fehlschlag der Sitzung, sondern ein misslungener Anhang: die Meldung sagt beides,
# sonst sucht die Runde am nächsten Tag nach einer Aufnahme, die es nie gab.
LEER = (
    "“{name}” arrived without audio — zero bytes. I am not queueing it; just send it again. "
    "Nothing else is missing from the session."
)

# Eine Markdown-Datei im Thread ist weder Notiz noch Diktat. Sie fiel bis #169 still
# durch — und Stille ist hier das Schlechteste: die Runde hält den Altbestand für abgelegt.
DOKUMENT_IM_THREAD = (
    "“{name}” looks like notes and not like a recording — I make nothing out of it. "
    "Whatever is meant for the chronicle you type in here while the session runs."
)

DOKUMENT_KEINE_DATEI = (
    "I do not read “{name}”: I take notes as a Markdown or text file ({endungen})."
)

DOKUMENT_ZU_GROSS = "“{name}” is larger than {grenze} MB — I do not read in that much text."

DOKUMENT_UNLESBAR = "“{name}” is not text I can read — UTF-8 is expected. I have created nothing."

# Kein Fehlschlag, sondern die häufigste Ursache: die Abende stehen ohne Datum da. Der
# Satz nennt die beiden Schreibweisen, die sicher gelesen werden — geraten wird keine.
DOKUMENT_OHNE_ABENDE = (
    "I find no evening in “{name}”. I recognise a session by a heading with a date — "
    "`## 12.03.2026` or `## 2026-03-12`. I have created nothing."
)

DOKUMENT_VORSCHAU = "**Out of “{name}” I would create {sitzungen}:**"

DOKUMENT_ABEND = "• **{datum}**{titel} — {szenen}"

DOKUMENT_OHNE_DATUM = (
    "Without a date in the heading and therefore skipped: {liste}. Add one there and send "
    "the file again — I do not guess it."
)

# Ein Abend ohne einen einzigen Satz wird nicht angelegt (#172). Der Satz sagt beides: was
# ausgelassen wurde und wie es hereinkäme — sonst hielte die Runde den Abschnitt für abgelegt.
DOKUMENT_OHNE_TEXT = (
    "Without a single sentence below it and therefore skipped: {liste}. I do not create a "
    "session that carries nothing — write something below it and send the file again."
)

# Der Nachsatz allein trüge die Antwort nicht: »eine Sitzung erkenne ich an einem Datum«
# wäre hier falsch, das Datum steht ja da.
DOKUMENT_NUR_OHNE_TEXT = (
    "In “{name}” there is no sentence under any evening. I have created nothing."
)

DOKUMENT_SCHON_DA = "This is already there and stays as it is: {liste}."

DOKUMENT_FRAGE = "Shall I create it like this? Until you confirm, nothing is written."

DOKUMENT_JA = "Yes, create"
DOKUMENT_NEIN = "Cancel"

DOKUMENT_ANGELEGT = (
    "{sitzungen} created. They are in the chronicle from now on and `/chronicle search` "
    "finds what appears in them."
)

DOKUMENT_ABGEBROCHEN = "Fine, I have created nothing."

# Der zweite Klick auf denselben Knopf, oder die zweite Vorschau desselben Dokuments: die
# Abende stehen schon, und ein »angelegt« darunter wäre die Zusage, dass sie zweimal da sind.
DOKUMENT_NICHTS_NEU = (
    "These evenings are already in the chronicle — I have created nothing. Nothing is there "
    "twice because of it."
)

# -- Eine einzelne Sitzung wieder loswerden ----------------------------------------------
#
# Bis #171 gab es darunter nichts: `/chronicle delete` nahm die ganze Runde, und ein
# Fehlgriff beim Einlesen war unwiderruflich. Das hier ist der kleine Weg daneben — und er
# ist genauso endgültig, weil an einer Sitzung Tondateien hängen. Deshalb dieselbe Bauform:
# erst zeigen, was verschwände, dann fragen, dann löschen.

# Discord nimmt 25 Zeilen je Menü. Eine Runde hat schnell mehr Sitzungen; zur Wahl stehen
# dann die jüngsten, und das wird gesagt, statt die älteren stillschweigend wegzulassen.
SITZUNG_ZUR_WAHL = 25

SITZUNG_KEINE = (
    "No session is written here yet — there is nothing to delete. `/session start` begins "
    "the first one."
)

SITZUNG_WAHL = (
    "**Delete a single session.** Choose below which one; I then show what hangs on it and "
    "ask once more. Until then nothing is deleted.{rest}"
)

SITZUNG_WAHL_GEKUERZT = " The {anzahl} most recent are on offer — Discord shows no more in a menu."

SITZUNG_WAEHLEN = "Which session should go?"

SITZUNG_FRAGE = (
    "**“{sitzung}” disappears, finally and immediately:**\n"
    "{liste}\n"
    "**This stays:** the other sessions of this round and the register — and the record "
    "that an announcement was made in the voice channel. That records something about "
    "people, and this round continues to exist; whoever no longer wants it deletes the "
    "whole round. What I have already delivered to you lies in Discord anyway, my deleting "
    "does not reach there.\n"
    "There is no backup I can restore this from. Download beforehand whatever you want to "
    "keep."
)

SITZUNG_ZEILE_NOTIZEN = "• {szenen} with {notizen}"
SITZUNG_ZEILE_TON = "• {spuren} — still here of them: {dateien}, those go too"
SITZUNG_ZEILE_OHNE_TON = "• {spuren} — no audio files are left for them"
# Eine Datei ohne Zeile: der Mitschnitt läuft noch oder ist abgestürzt, eingereiht wird
# erst am Ende. Ohne diese Zeile nennte die Frage weniger, als danach geschieht.
SITZUNG_ZEILE_NUR_TON = "• Not yet queued: {dateien} — those go too"
SITZUNG_ZEILE_VERSCHRIFTET = "• {transkripte} from these tracks"
SITZUNG_ZEILE_GESCHRIEBEN = "• {protokolle} written from them: chronicle and recap"

SITZUNG_JA = "Yes, delete this session"
SITZUNG_NEIN = "Cancel"

SITZUNG_FERTIG = "“{sitzung}” is gone, with everything that hung on it.{ton}"
SITZUNG_FERTIG_TON = " Deleted from disk: {dateien}."
SITZUNG_FERTIG_REST = (
    " Not deleted: {dateien}. What remained is still here — the reason is in the bot log."
)
SITZUNG_ABGEBROCHEN = "Nothing deleted. Everything stays as it was."

# Der Knopf lebt eine Viertelstunde. In der Zeit kann dieselbe Sitzung schon gelöscht sein
# — von einer zweiten Ansicht oder von jemand anderem.
SITZUNG_SCHON_FORT = (
    "This session no longer exists; I have just deleted nothing. "
    "`/chronicle sitzung-loeschen` shows what is still there."
)

NICHT_ABGELEGT = (
    "I could not file this: {grund} Write it again — if it stays that way, the reason is in "
    "the bot log."
)

# Eine Bildunterschrift zu löschen ist ein Rücknehmen — und die Nachricht steht danach
# weiter im Thread, mit ihrem Anhang. Ohne diesen Satz sähe niemand, dass darunter etwas
# fortgenommen wurde (#184).
NOTIZ_FORT = (
    "The text is out of the message, so the note is gone too. The attachment stays where it is."
)

# Nachgetragener Text an einer Nachricht, die nie eine Notiz hatte. Die Szene ist die
# ihres Zeitpunkts und nicht die von gerade eben — das zu sagen erspart die Suche danach.
NOTIZ_NACHGETRAGEN = (
    "The added text is a note now — in the scene the message belongs to, not in the last one."
)

# Die Chronik ist ein Abzug, kein Spiegel: sie steht, wie sie geschrieben wurde. Eine
# Notiz zu ändern, ohne das zu sagen, ließe beide auseinanderlaufen, ohne dass es jemand
# sieht — genau das Gedächtnis, gegen das die Zusage im Thread steht.
CHRONIK_STEHT_SCHON = (
    "This session's chronicle already stands, though, with the old state. `/session done` "
    "writes it anew."
)

# Dieselbe Lage an einer älteren Sitzung: ``/session done`` meint immer die zuletzt
# angelegte, wäre hier also kein Ausweg, sondern ein falscher.
CHRONIK_STEHT_SCHON_ALT = (
    "This session's chronicle already stands, though, with the old state — and it is not "
    "rewritten because of this."
)

FERTIG = (
    "Fetching the numbers from Foundry, transcribing the recordings, writing the chronicle "
    "— that takes its time; I will report here when it stands."
)

# Welche Sitzung gemeint ist und wo ihre Chronik erscheint, gehört seit #156 in die
# Antwort: abgeschlossen wird auch aus dem Sprachkanal, und dort sieht man beides nicht.
SCHLIESST = "I am closing **{sitzung}** — the chronicle goes to {kanal}."

# Sitzungen aus der Weboberfläche haben keinen Kanal. Das ist kein Fehlschlag: die
# Chronik entsteht trotzdem, sie wird nur nirgends angehängt.
SCHLIESST_OHNE_KANAL = (
    "I am closing **{sitzung}** — it has no channel, so the chronicle is only filed."
)

LAEUFT_SCHON = "I am already at it — I will report here when the chronicle stands."

# Der Bereich wird über **Sitzungen** benannt, nicht über einen Faden und nicht über eine
# Figur: von welchem Abend bis zu welchem. Ein Datum genügt, ein Anfang davon auch.
OHNE_SITZUNGEN = (
    "No session is written here yet — there is nothing to retell. `/session start` begins "
    "the first one."
)

BEREICH_UNBEKANNT = (
    "“{wert}” matches no session. I know some from {erste} to {letzte} — take a date from "
    "that, or leave the field empty and I take the edge."
)

ERZAEHLT = (
    "I am retelling the sessions from {von} to {bis} — session by session, along the "
    "register. That takes its time; the file comes here into the channel."
)

ERZAEHLT_SCHON = "I am already retelling — the file comes here into the channel."

ABGLEICH_TITEL = "Fetch the numbers from Foundry"

ABGLEICH = (
    "I am fetching the numbers from your Foundry. That takes a moment — I will report here "
    "when I am through."
)

ABGLEICH_LAEUFT_SCHON = "I am already fetching — I will report here when I am through."

PASSWORT_TITEL = "Close the session"
PASSWORT_FELD = "Password for Foundry"
PASSWORT_HINWEIS = "For this one sync only — it is stored nowhere."

START_TITEL = "Begin the session"
START_FELD = "Password for Foundry — optional"
START_HINWEIS = "Leaving it empty is fine: then without the numbers."

MIT_FOUNDRY = (
    "The password stands ready until the session is closed — it is stored nowhere, and "
    "`/session done` will not ask again. From now on I post here whatever is rolled "
    "**openly** in your Foundry while you play. Not whispers and not hidden rolls: the "
    "whole group reads along here."
)

OHNE_FOUNDRY = (
    "Without a Foundry password: the session runs, only the numbers are missing. The rolls "
    "then join in at closing time, and `/session done` will ask for it once more."
)

KEIN_FOUNDRY = (
    "I am not asking for the Foundry password: there is no Foundry server in play for this "
    "round right now. `/chronicle setup` enters address and account if one does join later."
)

# Der Platzhalter des Abschlussfensters, wenn ein anderes Mitglied etwas hinterlegt hat.
# Discord kürzt Platzhalter bei 100 Zeichen — deshalb der kurze Satz.
FREMDES_HINWEIS = "Somebody else's is ready — yours is the one that counts."

# Wie oft der Strom nach Foundry sieht. Zwei Minuten sind eine Betriebsentscheidung und
# keine technische: jeder Blick ist ein vollständiger Handschlag — anmelden, Welt holen,
# abmelden —, und die Runde fragt damit ein **fremdes** Foundry an, das ihr gehört und
# nicht uns. Zwei Minuten halten das bei rund dreißig Anfragen die Stunde und bündeln
# zugleich, was am Tisch in einem Zug passiert: der Schlagabtausch einer Kampfrunde landet
# als **eine** Nachricht im Thread statt als acht. Wer beides anders gewichtet, ändert
# diese Zahl — dass sie hier steht und nicht in einem Feld, ist Absicht: sie betrifft den
# Betrieb der Box, nicht die Gruppe.
STROM_ABSTAND = 120.0

# Der Ursprung, sichtbar in einer Zeile: was hier steht, kommt aus Foundry und nicht vom
# Tisch. Der Rest der Zeile ist Abschrift — kein Wort davon ist ergänzt.
EREIGNIS = "🎲 {zeile}"

AUSFALL = (
    "I cannot reach your Foundry right now: {grund} I keep trying quietly and will only say "
    "something again when it is back — whatever falls in the meantime I fetch afterwards."
)

WIEDER_DA = "Your Foundry is back."

STROM_ENDET = "From here on I stop looking at Foundry: {grund}"


class ChronikFehler(BotFehler):
    """Was ein Befehl im Thread nicht tun kann — gesagt wird es, still scheitert nichts."""


@dataclass(frozen=True)
class Anhang:
    """Ein Anhang, so weit diese Stufe ihn braucht: Name, Größe, und wie man ihn holt."""

    filename: str
    size: int
    speichern: Callable[[Path], Awaitable[None]]


@dataclass(frozen=True)
class Notizdatei:
    """Ein angehängtes Notizdokument: Name, Größe, und wie man seinen Text bekommt.

    Wie ``Anhang``, nur andersherum gelesen: ein Diktat wird auf die Platte gelegt und
    später verschriftet, ein Dokument wird sofort gelesen und danach nicht mehr gebraucht.
    """

    filename: str
    size: int
    lesen: Callable[[], Awaitable[bytes]]


@dataclass(frozen=True)
class Vorschau:
    """Was aus einem Dokument entstünde — und was dafür noch zu bestätigen ist.

    Ohne ``abende`` gibt es nichts zu bestätigen: dann sagt ``text``, warum.
    """

    text: str
    abende: tuple[dokument.Abend, ...] = ()


@dataclass(frozen=True)
class Sitzungswahl:
    """Die Sitzungen zur Auswahl — und der Satz darüber. Ohne ``zeilen`` sagt ``text``, warum.

    Wie ``Vorschau``: die Ansicht entsteht nur, wenn es etwas zu wählen gibt.
    """

    text: str
    zeilen: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Nachricht:
    """Eine Nachricht im Sitzungs-Thread, ohne alles, was Discord sonst noch mitschickt."""

    id: str
    text: str = ""
    zeitpunkt: str = ""
    anhaenge: tuple[Anhang, ...] = ()
    autor_id: str | None = None
    # Der Anzeigename des Absenders. Er kommt mit der Nachricht und muss nicht
    # nachgeschlagen werden — ohne ihn stünde über einem Diktat der Dateiname (#250).
    autor_name: str | None = None


@dataclass
class Strom:
    """Was der Beobachter einer laufenden Sitzung zwischen zwei Blicken behält.

    Alles davon lebt im Arbeitsspeicher und nur so lange wie die Sitzung: ein Neustart
    beendet den Strom, und das ist richtig so — das Passwort ist dann ohnehin fort.

    **Er endet dabei stumm, und das ist die bekannte Grenze dieses Weges.** Wird der Bot
    mitten in einer Sitzung neu gestartet, sagt niemand im Thread, dass ab jetzt nichts
    mehr kommt; die Runde spielt in dem Glauben weiter, die Würfe kämen noch. Verloren
    geht nichts — der Abschluss fragt das Passwort neu und holt alles ganz —, aber das
    »beobachtbar« aus #93 gilt bis zum Neustart und nicht darüber hinaus. Es hier zu
    schreiben ist der ehrlichere Zustand, als es offenzulassen; wer die Lücke schließt,
    tut das beim Wiederanlauf und sagt es in den Thread.
    """

    runde: Runde
    session_id: int
    gesehen: set[str] = field(default_factory=set)
    # Der erste Blick zählt nur, was schon dastand. Ohne ihn schriebe der Strom beim
    # Anfang das ganze Chat-Log der Welt in den Thread — bei einer Runde, die seit Wochen
    # nicht abgeglichen hat, sind das hunderte Würfe von Abenden, die längst geschrieben
    # sind.
    grundlinie: bool = True
    # Ein Ausfall wird einmal gesagt, nicht einmal je Durchlauf.
    gemeldet: bool = False


@dataclass(frozen=True)
class Meldung:
    """Was der Strom in den Thread stellt — und ob es einen nächsten Blick lohnt."""

    text: str = ""
    weiter: bool = True


def runde_der_gilde(config: Config, guild_id: str) -> Runde | None:
    """Die Runde dieser Gilde, sofern sie noch spricht.

    Eine gesperrte gibt es hier nicht: sie ist verabschiedet und wartet nur noch auf ihre
    Frist. Wer sie trotzdem braucht — die Wiedereinladung, der Löschlauf —, geht über
    ``chronicle.lebenszyklus``.
    """
    gefunden = runden.fuer_gilde(config.database_path, str(guild_id))
    return None if gefunden is None or gefunden.gesperrt else gefunden


def runde_verlangen(config: Config, guild_id: str) -> Runde:
    gefunden = runden.fuer_gilde(config.database_path, str(guild_id))
    if gefunden is None:
        raise ChronikFehler(KEINE_RUNDE)
    if gefunden.gesperrt:
        raise ChronikFehler(GESPERRT.format(datum=lebenszyklus.frist_datum(gefunden)))
    return gefunden


def runde_zum_loeschen(config: Config, guild_id: str) -> Runde:
    """Wie ``runde_verlangen`` — nur dass eine ruhende Runde hier zugelassen ist.

    Vergessen darf keine Rückkehr verlangen: wer den Bot hinausgeworfen hat und nicht
    dreißig Tage warten will, soll löschen können, ohne ihn dafür erst wieder einzuladen.
    """
    gefunden = runden.fuer_gilde(config.database_path, str(guild_id))
    if gefunden is None:
        raise ChronikFehler(KEINE_RUNDE)
    return gefunden


def dieselbe_runde(config: Config, guild_id: str | None, runde: Runde) -> Runde | None:
    """Die Runde von vorhin — aber nur, wenn sie es noch ist.

    Ein Knopf, ein Menü, ein Fenster lebt eine Viertelstunde und trägt die Runde mit, gegen
    die gefragt wurde. Die Kennung allein trägt diese Zusage nicht: ``runde.id`` ist ein
    ``INTEGER PRIMARY KEY`` ohne ``AUTOINCREMENT``, SQLite vergibt sie nach einer Löschung
    also wieder. ``created_at`` reicht auch nicht — es steht auf die Sekunde genau, und
    zwei Runden in derselben Sekunde sind eine Sekunde. Verglichen wird deshalb der
    Zufallswert, den jede Runde beim Anlegen bekommt.

    Ohne diesen Vergleich schriebe ein Menü aus Gilde A in die frische Runde von Gilde B.
    """
    if guild_id is None:
        return None
    gefunden = runden.fuer_gilde(config.database_path, str(guild_id))
    if gefunden is None or (gefunden.id, gefunden.token) != (runde.id, runde.token):
        return None
    return gefunden


def sitzung_im_kanal(runde: Runde, kanal_id: str) -> int | None:
    """Die Sitzung, in die eine Zeile aus diesem Kanal gehört — oder keine.

    Die Grenze ist seit #271 die **Zeit** und nicht mehr der Ort: es genügt nicht, dass
    der Kanal derselbe ist, die Sitzung muss auch laufen. Was vor dem Start oder nach dem
    Abschluss im Chat des Sprachkanals steht, ist Gerede und keine Notiz.
    """
    laufend = notes.running_session(runde)
    if laufend is None or laufend.kanal_id != str(kanal_id):
        return None
    return laufend.id


def abgeschlossene_sitzung_im_kanal(runde: Runde, kanal_id: str) -> int | None:
    """Der Abend, der in diesem Kanal lief und schon zu ist — oder keiner (#288)."""
    return notes.closed_session_in_channel(runde, kanal_id)


def kanal_der_sitzung(runde: Runde, session_id: int) -> str | None:
    return notes.channel_of_session(runde, session_id)


def sitzung_laeuft(runde: Runde, session_id: int) -> bool:
    """Ob genau diese Sitzung noch offen ist — die Schranke vor dem Abschluss von selbst."""
    laufend = notes.running_session(runde)
    return laufend is not None and laufend.id == session_id


def sitzung_schliessen(runde: Runde, session_id: int) -> bool:
    return notes.close_session(runde, session_id)


def sitzung_verlangen(runde: Runde, kanal_id: str) -> int:
    sitzung = sitzung_im_kanal(runde, kanal_id)
    if sitzung is None:
        raise ChronikFehler(NUR_IN_DER_SITZUNG)
    return sitzung


def laufende_sitzung(runde: Runde) -> int:
    """Die Sitzung, die der Abschluss meint — dieselbe Auskunft, die die Aufnahme nimmt.

    Nicht über den Thread: nach ``/session pause`` steht man im Sprachkanal, und ein
    Abschluss, der dort abwiese, riete zu einer zweiten Sitzung — die Aufnahmen der ersten
    blieben liegen und die Chronik entstünde für die falsche (#156). Je Runde ist genau
    eine offen, also ist die Auskunft eindeutig.
    """
    sitzung = notes.latest_session(runde)
    if sitzung is None:
        raise ChronikFehler(KEINE_SITZUNG)
    return sitzung.id


def offene_sitzung(runde: Runde) -> int | None:
    """Die Sitzung, die gerade läuft — oder keine.

    Anders als ``letzte_sitzung``: die gibt auch eine längst abgeschlossene zurück.
    ``/session start`` unterscheidet daran, ob es anzulegen hat oder nur noch den
    Mitschnitt nachzuholen (#272).
    """
    laufend = notes.running_session(runde)
    return None if laufend is None else laufend.id


def letzte_sitzung(runde: Runde) -> int | None:
    """Dieselbe Auskunft wie ``laufende_sitzung``, nur ohne Wurf — für alles, was bloß sagt.

    Ein Befehl, der eine Auskunft gibt, darf an einer fehlenden Sitzung nicht scheitern:
    dann gibt es eben keinen Kanal, in dem etwas stehen könnte.
    """
    sitzung = notes.latest_session(runde)
    return None if sitzung is None else sitzung.id


def sitzungsname(sitzung: notes.Session) -> str:
    """Wie eine Sitzung in einer Antwort heißt — ihr Titel, sonst ihr Abend."""
    return (sitzung.title or "").strip() or f"Session of {sitzung.played_on}"


def abschlussmeldung(runde: Runde, session_id: int) -> str:
    """Was der Abschluss antwortet: welche Sitzung, wohin die Chronik, wie lange es dauert."""
    sitzung = notes.session(runde, session_id)
    if sitzung is None:
        return FERTIG
    name = sitzungsname(sitzung)
    kanal = kanal_der_sitzung(runde, session_id)
    kopf = (
        SCHLIESST.format(sitzung=name, kanal=f"<#{kanal}>")
        if kanal
        else SCHLIESST_OHNE_KANAL.format(sitzung=name)
    )
    return f"{kopf} {FERTIG}"


def sitzung_anlegen(runde: Runde, kanal_id: str, titel: str = "") -> int:
    """Die Sitzung in diesem Kanal — höchstens eine je Runde zur selben Zeit.

    Gefragt und geschrieben wird ohne ``await`` dazwischen: der Bot bedient seine Befehle
    in einer Ereignisschleife, damit ist zwischen Prüfen und Anlegen kein zweiter Start.
    """
    if notes.running_session(runde) is not None:
        raise ChronikFehler(SITZUNG_LAEUFT_SCHON)
    return notes.create_session(runde, title=titel, kanal_id=kanal_id, laeuft=True)


def passwort_merken(runde: Runde, eingabe: str, wer: str) -> bool:
    """Das Passwort vom Sitzungsbeginn — gemerkt wird nur, was auch dasteht.

    Ein leeres Feld heißt hier **überspringen**, nicht vergessen: wer beim Start nichts
    eingibt, soll damit nicht das Passwort einer schon laufenden Sitzung wegwerfen. Das
    ``merken`` selbst deutet leer als vergessen — deshalb steht die Unterscheidung hier.
    """
    if not eingabe.strip():
        return False
    zugang.merken(runde, eingabe, wer=wer)
    return True


def passwort_fuer(runde: Runde, wer: str) -> str | None:
    """Das bereitliegende Passwort **dieser Person** — sonst ``None``.

    Nicht bloß »es liegt eines da«: ``/session start`` steht jedem Mitglied offen, und
    eine fremde Eingabe würde sonst ohne Rückfrage dem Foundry-Konto dieser Runde
    vorgezeigt, ausgelöst von jemandem, der sie nie gesehen hat. Wer nicht selbst
    hinterlegt hat, bekommt darum das Fenster — und damit den Weg zum eigenen Passwort,
    ohne die zwölf Stunden abzuwarten.

    Zurück kommt der **Wert** und nicht nur ein Ja: der Abschluss reicht ihn bis zum
    Abgleich durch, statt ihn dort noch einmal aus dem Merkzettel zu holen. Zwischen
    Prüfen und Benutzen liegt sonst eine Fadengrenze, über die ein zweites Fenster genau
    die Zeichenkette schieben kann, die hier gerade abgelehnt wurde.
    """
    return zugang.passwort_von(runde, wer)


def passwort_gehalten(runde: Runde) -> bool:
    """Ob überhaupt eines bereitliegt, gleich von wem — *ob*, nie *was*."""
    return zugang.ist_gemerkt(runde)


def foundry_im_spiel(config: Config, runde: Runde) -> bool:
    """Ob ein eingegebenes Passwort überhaupt irgendwo vorgezeigt würde.

    Auf der Testwelt und ohne eingetragenen Zugang redet der Abgleich mit keinem Server.
    Danach zu fragen hieße, ein Geheimnis für nichts einzusammeln — und es läge dann bis
    zur harten Frist herum.

    Gefragt wird nach ``foundry_configured`` und nicht nach der Adresse allein: dieselbe
    Bedingung prüft ``FoundryClient``, bevor er überhaupt einen Handschlag versucht. Eine
    Adresse ohne Konto ergäbe sonst ein Fenster, dessen Eingabe ein ``FoundryError``
    verbraucht, ohne sie je irgendwem vorgezeigt zu haben.
    """
    if settings.foundry_quelle(runde) == settings.TESTWELT:
        return False
    return settings.effective(config, runde).foundry_configured


def starthinweis(config: Config, runde: Runde, gemerkt: bool) -> str:
    """Was der Start über das Passwort sagt — drei Lagen, drei Sätze."""
    if not foundry_im_spiel(config, runde):
        return KEIN_FOUNDRY
    return MIT_FOUNDRY if gemerkt else OHNE_FOUNDRY


def szene_setzen(runde: Runde, session_id: int, name: str, *, zeitpunkt: str = "") -> str:
    sauber = name.strip()
    notes.add_scene(runde, session_id, title=sauber, at=zeitpunkt)
    return SZENE.format(name=sauber) if sauber else SZENE_OHNE_NAMEN


def ist_diktat(dateiname: str) -> bool:
    return Path(dateiname).suffix.lower() in recordings.SUFFIXES


async def _diktat(
    config: Config, runde: Runde, session_id: int, anhang: Anhang, nachricht: Nachricht
) -> str:
    """Ein Anhang wird eine wartende Spur — mit dem Zeitpunkt der Nachricht daran.

    Der Zeitpunkt ist kein Beiwerk: eine Sitzungsuhr hat ein Diktat nicht, also ist er
    das Einzige, woran es später seine Szene findet. Ohne ihn bliebe es verschriftet in
    der Datenbank liegen und stünde in keiner Chronik.

    **Hier steht die Byte-Schranke** — »ist überhaupt etwas angekommen«. Das ist die Frage
    der Annahme, und sie wird an der Datei beantwortet, nicht an Discords Größenangabe:
    eine Spur ohne ein einziges Byte belegt sonst einen Platz in der Warteschlange und
    kostet einen Modelllauf. Die andere Frage — »steckt eine Äußerung darin« — beantwortet
    ``transcribe.service`` an der Dauer (``MINDESTDAUER``, #142) und bleibt dort: dorthin
    laufen alle Spuren, auch die des Aufnahme-Bots, die hier nie vorbeikommen.
    """
    if anhang.size > recordings.MAX_BYTES:
        return ZU_GROSS.format(name=anhang.filename, grenze=recordings.MAX_BYTES // (1024 * 1024))
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings.target_path(config.recordings_dir, session_id, anhang.filename)
    await anhang.speichern(ziel)
    if ziel.stat().st_size == 0:
        ziel.unlink()
        # Ohne den Namen: er kommt vom Hochladenden und kann alles enthalten. Discords
        # Sprachnachricht heißt »voice-message.ogg«, eine selbst benannte Datei nicht.
        # Wer sie sucht, findet sie an der Antwort im Thread, nicht im Log des Betreibers.
        logger.info("Leeres Diktat aus dem Thread abgewiesen.")
        return LEER.format(name=anhang.filename)
    recordings.enqueue(
        runde,
        session_id,
        ziel.name,
        discord_user_id=nachricht.autor_id,
        discord_name=nachricht.autor_name,
        message_at=nachricht.zeitpunkt.strip() or None,
    )
    logger.info("Diktat aus dem Thread: %s → Sitzung %s", ziel.name, session_id)
    return DIKTAT


def ist_notizdokument(dateiname: str) -> bool:
    return Path(dateiname).suffix.lower() in dokument.SUFFIXES


async def aufnehmen(
    config: Config, runde: Runde, session_id: int, nachricht: Nachricht
) -> tuple[str, ...]:
    """Text wird Notiz, Audio wird Diktat — beides an derselben Nachricht.

    Zurück kommt nur, was gesagt werden muss. Eine abgelegte Notiz sagt nichts: die
    Nachricht steht im Thread und **ist** die Notiz; eine Quittung darunter wäre die zweite
    Hälfte jedes Satzes eines ganzen Abends.

    Ein **Dokument** ist hier keines von beidem und wird deshalb nur beantwortet: es deckt
    mehrere Abende ab und gehört in keinen einzelnen Thread (#169).
    """
    meldungen = [
        await _diktat(config, runde, session_id, anhang, nachricht)
        for anhang in nachricht.anhaenge
        if ist_diktat(anhang.filename)
    ]
    meldungen += [
        DOKUMENT_IM_THREAD.format(name=anhang.filename)
        for anhang in nachricht.anhaenge
        if ist_notizdokument(anhang.filename)
    ]
    if nachricht.text.strip():
        szene = notes.scene_at(runde, session_id, nachricht.zeitpunkt)
        notes.add_note(runde, szene, nachricht.text, message_id=nachricht.id)
    return tuple(meldungen)


def _anzahl(wieviele: int, einzahl: str, mehrzahl: str) -> str:
    """Ein gezähltes Ding. Englisch hängt meist ein ``s`` an — aber nicht immer."""
    return f"{wieviele} {einzahl if wieviele == 1 else mehrzahl}"


def _szenenzahl(abend: dokument.Abend) -> str:
    # Auch ein Abend ohne eigene Überschriften bekommt eine Szene — die, die mit der
    # Sitzung entsteht und seinen Text trägt.
    return _anzahl(max(len(abend.szenen), 1), "scene", "scenes")


def _abendzeile(abend: dokument.Abend) -> str:
    return DOKUMENT_ABEND.format(
        datum=abend.datum,
        titel=f" — {abend.titel}" if abend.titel else "",
        szenen=_szenenzahl(abend),
    )


def _abendliste(abende: Sequence[dokument.Abend]) -> str:
    return ", ".join(abend.datum for abend in abende)


async def dokument_vorschau(runde: Runde, datei: Notizdatei) -> Vorschau:
    """Was aus dem Dokument entstünde — **ohne** dass etwas entsteht.

    Ein Dokument kann fünfzehn Abende tragen; ein Fehlgriff beim Aufteilen legte sonst
    fünfzehn falsche Sitzungen an, die einzeln wieder wegzuräumen wären. Geschrieben wird
    deshalb erst, was ``dokument_anlegen`` bestätigt bekommt.
    """
    if not ist_notizdokument(datei.filename):
        return Vorschau(
            DOKUMENT_KEINE_DATEI.format(name=datei.filename, endungen=", ".join(dokument.SUFFIXES))
        )
    if datei.size > dokument.MAX_BYTES:
        return Vorschau(
            DOKUMENT_ZU_GROSS.format(
                name=datei.filename, grenze=dokument.MAX_BYTES // (1024 * 1024)
            )
        )
    roh = await datei.lesen()
    try:
        text = roh.decode("utf-8")
    except UnicodeDecodeError:
        return Vorschau(DOKUMENT_UNLESBAR.format(name=datei.filename))
    aufteilung = dokument.lesen(text)
    frisch = dokument.neu(runde, aufteilung.abende)
    schon = tuple(abend for abend in aufteilung.abende if abend not in frisch)
    nachsatz = []
    if aufteilung.ohne_datum:
        nachsatz.append(
            DOKUMENT_OHNE_DATUM.format(
                liste=", ".join(f"“{kopf}”" for kopf in aufteilung.ohne_datum)
            )
        )
    if aufteilung.ohne_text:
        nachsatz.append(
            DOKUMENT_OHNE_TEXT.format(liste=", ".join(f"“{kopf}”" for kopf in aufteilung.ohne_text))
        )
    if schon:
        nachsatz.append(DOKUMENT_SCHON_DA.format(liste=_abendliste(schon)))
    if not frisch:
        leer = aufteilung.ohne_text and not (aufteilung.abende or aufteilung.ohne_datum)
        anfang = DOKUMENT_NUR_OHNE_TEXT if leer else DOKUMENT_OHNE_ABENDE
        return Vorschau(" ".join((anfang.format(name=datei.filename), *nachsatz)))
    kopf = DOKUMENT_VORSCHAU.format(
        name=datei.filename, sitzungen=_anzahl(len(frisch), "session", "sessions")
    )
    zeilen = "\n".join(_abendzeile(abend) for abend in frisch)
    return Vorschau("\n".join((kopf, zeilen, " ".join((*nachsatz, DOKUMENT_FRAGE)))), frisch)


def dokument_anlegen(runde: Runde, abende: Sequence[dokument.Abend]) -> str:
    """Das Bestätigte anlegen — und im Log steht keine Zeile aus dem Dokument.

    Weder der Dateiname noch eine Überschrift: beides kommt von der Gruppe und kann jeden
    Klarnamen enthalten. Wie viele Abende entstanden sind, sagt genug.

    Gegen den Bestand geprüft wird **hier** und nicht nur in der Vorschau: die Vorschau
    friert ihre Abende ein und ist ephemer, also ruft auf, wer unsicher ist, ob sie durchkam
    — zwei offene Vorschauen desselben Dokuments legten den Abend sonst zweimal an. Einen
    Abend zu viel wird man seit #171 auch wieder los: ``/chronicle sitzung-loeschen`` nimmt
    genau ihn, statt der ganzen Runde.
    """
    angelegt = dokument.anlegen(runde, dokument.neu(runde, abende))
    logger.info("Notizdokument eingelesen: %s Sitzungen angelegt.", angelegt)
    if not angelegt:
        return DOKUMENT_NICHTS_NEU
    return DOKUMENT_ANGELEGT.format(sitzungen=_anzahl(angelegt, "session", "sessions"))


def _wahlschrift(sitzung: notes.Session) -> str:
    """Wie eine Sitzung im Menü steht: ihr Abend, und ihr Titel, wenn sie einen hat."""
    titel = (sitzung.title or "").strip()
    return f"{sitzung.played_on} — {titel}" if titel else f"Session of {sitzung.played_on}"


def sitzungswahl(runde: Runde) -> Sitzungswahl:
    """Die Sitzungen dieser Runde zur Wahl — die jüngsten zuerst.

    Über den Thread ginge es nicht: eine eingelesene Sitzung hat keinen, und genau die ist
    der Fall, aus dem dieser Befehl entstanden ist (#169/#171). Über ein Datum ginge es
    auch nicht eindeutig — zwei Abende an einem Tag sind selten, aber möglich, und was
    endgültig löscht, darf nicht raten.

    Was das Menü mitgibt, ist nicht die Nummer, sondern die **Marke** der Sitzung: die
    Nummer allein trüge die Zusage nicht, dass eine Viertelstunde später noch derselbe Abend
    darunter steht (``notes.sitzungsmarke``).
    """
    alle = notes.sessions(runde)
    if not alle:
        return Sitzungswahl(text=SITZUNG_KEINE)
    zeilen = tuple(
        (_wahlschrift(sitzung), notes.sitzungsmarke(sitzung)) for sitzung in alle[:SITZUNG_ZUR_WAHL]
    )
    rest = SITZUNG_WAHL_GEKUERZT.format(anzahl=len(zeilen)) if len(alle) > len(zeilen) else ""
    return Sitzungswahl(text=SITZUNG_WAHL.format(rest=rest), zeilen=zeilen)


def _sitzungszeilen(umfang: notes.Contents) -> str:
    """Was an dieser Sitzung hängt, Zeile für Zeile — die Tondateien ausdrücklich.

    Sie sind der Grund, warum diese Frage überhaupt gestellt wird: an ihnen hängen die
    Stimmen echter Menschen, und ob welche da sind, sieht man einer Sitzung von außen nicht
    an. Was es nicht gibt, steht nicht da — eine Zeile »0 Aufnahmen« sagt nichts.

    Gefragt wird nach **Dateien**, nicht nach Zeilen: eine Spur wird die ganze Sitzung über
    geschrieben und erst am Ende eingereiht, ein Absturz mittendrin hinterlässt also Ton
    ohne Zeile. Genau den nimmt das Löschen mit, und was es mitnimmt, muss vorher dastehen —
    sonst bestätigt die Rückfrage etwas anderes, als geschieht.
    """
    zeilen = [
        SITZUNG_ZEILE_NOTIZEN.format(
            szenen=_anzahl(umfang.session.scene_count, "scene", "scenes"),
            notizen=_anzahl(umfang.session.note_count, "note", "notes"),
        )
    ]
    dateien = _anzahl(umfang.audio, "audio file", "audio files")
    if umfang.recordings:
        spuren = _anzahl(umfang.recordings, "recording", "recordings")
        zeilen.append(
            SITZUNG_ZEILE_TON.format(spuren=spuren, dateien=dateien)
            if umfang.audio
            else SITZUNG_ZEILE_OHNE_TON.format(spuren=spuren)
        )
    elif umfang.audio:
        zeilen.append(SITZUNG_ZEILE_NUR_TON.format(dateien=dateien))
    if umfang.transcripts:
        zeilen.append(
            SITZUNG_ZEILE_VERSCHRIFTET.format(
                transkripte=_anzahl(umfang.transcripts, "transcript", "transcripts")
            )
        )
    if umfang.protocols:
        zeilen.append(
            SITZUNG_ZEILE_GESCHRIEBEN.format(
                protokolle=_anzahl(umfang.protocols, "written text", "written texts")
            )
        )
    return "\n".join(zeilen)


def sitzungsfrage(config: Config, runde: Runde, marke: str) -> str | None:
    """Was diese Sitzung kostet — gefragt, bevor irgendetwas geschieht. ``None``: schon fort."""
    umfang = notes.session_contents(config, runde, marke)
    if umfang is None:
        return None
    return SITZUNG_FRAGE.format(sitzung=sitzungsname(umfang.session), liste=_sitzungszeilen(umfang))


def sitzung_geloescht(config: Config, runde: Runde, marke: str) -> str:
    """Die Sitzung fortnehmen und sagen, was fort ist.

    Im Log stehen **Zahlen und sonst nichts** — kein Titel, keine Kennung, kein Name. Der
    Titel kommt von der Gruppe und trägt oft genug einen Klarnamen; anders als beim Löschen
    der ganzen Runde gibt es hier auch niemanden zu vermerken, der es veranlasst hat: die
    Runde bleibt stehen, und wer in ihr was tut, geht das Log des Betreibers nichts an.

    Gemeldet wird, was **wirklich** von der Platte ist: was liegen blieb, steht daneben.
    """
    umfang = notes.delete_session(config, runde, marke)
    if umfang is None:
        return SITZUNG_SCHON_FORT
    logger.info(
        "Sitzung gelöscht: %s Notizen und %s Tondateien entfernt, %s geblieben.",
        umfang.session.note_count,
        umfang.audio,
        umfang.geblieben,
    )
    ton = ""
    if umfang.audio:
        ton += SITZUNG_FERTIG_TON.format(dateien=_anzahl(umfang.audio, "audio file", "audio files"))
    if umfang.geblieben:
        ton += SITZUNG_FERTIG_REST.format(
            dateien=_anzahl(umfang.geblieben, "audio file", "audio files")
        )
    return SITZUNG_FERTIG.format(sitzung=sitzungsname(umfang.session), ton=ton)


@dataclass(frozen=True)
class Notizwechsel:
    """Was eine geänderte Nachricht mit ihrer Notiz gemacht hat — und wo es zu sagen ist."""

    sitzung: int | None = None
    antwort: str | None = None


def _mit_chronikstand(runde: Runde, session_id: int, satz: str) -> str | None:
    """Hängt an, dass die Chronik schon steht — und nur dann, wenn sie wirklich steht."""
    if protocol.stored(runde, session_id) is None:
        return satz or None
    aktuell = letzte_sitzung(runde) == session_id
    nachsatz = CHRONIK_STEHT_SCHON if aktuell else CHRONIK_STEHT_SCHON_ALT
    return " ".join(teil for teil in (satz, nachsatz) if teil)


def notiz_aendern(runde: Runde, kanal_id: str, nachricht: Nachricht) -> Notizwechsel:
    """Eine geänderte Nachricht auf ihre Notiz ziehen — in allen drei Fällen.

    Geänderter Text zieht die Notiz nach und sagt nichts: die neue Fassung steht im Kanal,
    eine Quittung darunter wäre die zweite Hälfte jedes Satzes. **Geleerter** Text nimmt
    die Notiz fort — Discord schickt ``content: ""``, wenn eine Nachricht mit Anhang ihre
    Bildunterschrift verliert, und ein so zurückgenommener Wortlaut, der in
    ``/chronicle search`` und in der komponierten Chronik weiterlebte, wäre genau das
    Gedächtnis, gegen das die Zusage beim Start steht. **Nachgetragener** Text an einer
    Nachricht, die nie eine Notiz hatte, legt sie jetzt an; vorher traf er keine Zeile und
    war still verloren (#184).

    Gesucht wird zuerst die Sitzung der **Notiz** und erst dann die laufende: eine
    Woche alte Notiz gehört ihrem Abend, nicht dem heutigen. Seit die Grenze auf der Zeit
    liegt (#271) ist das der Unterschied — der Thread beantwortete es vorher von selbst.
    Neu **angelegt** wird trotzdem nur, wo gerade eine Sitzung läuft: sonst zöge ein
    nachgetragener Satz irgendwo im Server eine Notiz nach sich.
    """
    sitzung = notes.session_of_note(runde, nachricht.id) or sitzung_im_kanal(runde, kanal_id)
    if sitzung is None:
        return Notizwechsel()
    inhalt = nachricht.text.strip()
    if not inhalt:
        satz = NOTIZ_FORT if notes.remove_note(runde, nachricht.id) else None
    elif notes.update_note(runde, nachricht.id, inhalt):
        satz = ""
    else:
        szene = notes.scene_at(runde, sitzung, nachricht.zeitpunkt)
        angelegt = notes.add_note(runde, szene, inhalt, message_id=nachricht.id)
        satz = NOTIZ_NACHGETRAGEN if angelegt is not None else None
    if satz is None:
        return Notizwechsel()
    return Notizwechsel(sitzung, _mit_chronikstand(runde, sitzung, satz))


def notiz_entfernen(runde: Runde, message_id: str) -> bool:
    return notes.remove_note(runde, message_id)


def _wurfzeile(nachricht: ChatMessage, inhaltssprache: str) -> str:
    """Ein Wurf, wie er im Chat-Log steht — abgeschrieben, nicht gerechnet.

    Was Foundry nicht mitliefert, steht auch nicht da: ohne Summe keine Summe, ohne Titel
    das Wort »Wurf«. Eine Zeile, die eine fehlende Zahl freundlich ergänzt, wäre genau die
    Erfindung, gegen die dieser ganze Weg gebaut ist.

    Die beiden eigenen Wörter folgen der Inhaltssprache und nicht der Bedienoberfläche:
    diese Zeile steht im Kanal des Abends und geht als Fakt in dieselbe Chronik ein.
    """
    texte = sprachen.chronik(inhaltssprache)
    wurf = nachricht.roll
    kopf = wurf.title or texte.wurf
    if wurf.total is not None:
        kopf = f"{kopf}: **{wurf.total}**"
    wuerfel = ", ".join(f"{einzeln.name} {einzeln.value}" for einzeln in wurf.dice)
    zeile = " · ".join(
        teil for teil in (kopf, wuerfel, texte.kritisch if wurf.critical else "") if teil
    )
    wer = nachricht.speaker_alias
    return EREIGNIS.format(zeile=f"**{wer}** — {zeile}" if wer else zeile)


def ereignisse_abholen(config: Config, strom: Strom) -> Meldung:
    """Ein Blick nach Foundry und was davon in den Thread gehört.

    Jedes Ereignis genau einmal: gesehen ist gesehen, und der Merkzettel dafür liegt im
    Strom und nicht in der Datenbank — er beschreibt diesen einen Abend.

    Ein Ausfall wird **einmal** gesagt und danach still weiter versucht; kommt Foundry
    zurück, wird auch das einmal gesagt. Ein Fehler je Durchlauf wäre hier ein Fehler alle
    zwei Minuten, und der Thread einer Sitzung mit ausgefallenem Server bestünde am Ende
    aus nichts anderem.

    Eine ruhende Runde bekommt nichts und erfährt auch nichts: sie ist verabschiedet, und
    ein Satz in ihren Thread wäre der einer Instanz, die sie nicht mehr bedient.
    """
    gemeint = lebenszyklus.dieselbe(strom.runde)
    if gemeint is None or gemeint.gesperrt:
        return Meldung(weiter=False)
    ergebnis = foundry.beobachten(config, gemeint, gesehen=frozenset(strom.gesehen))
    if ergebnis.grund is not None:
        if strom.gemeldet:
            return Meldung(weiter=ergebnis.weiter)
        strom.gemeldet = True
        vorlage = AUSFALL if ergebnis.weiter else STROM_ENDET
        return Meldung(text=vorlage.format(grund=ergebnis.grund), weiter=ergebnis.weiter)
    vorlauf = []
    if strom.gemeldet:
        strom.gemeldet = False
        vorlauf.append(WIEDER_DA)
    strom.gesehen.update(nachricht.id for nachricht in ergebnis.neu)
    if strom.grundlinie:
        # Der Ausfall von vorhin wird trotzdem aufgelöst: hätte der erste Blick nichts
        # gesagt, bliebe die Runde mit »ich komme nicht dran« sitzen, während es längst
        # wieder geht.
        strom.grundlinie = False
        return Meldung(text="\n".join(vorlauf))
    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
    szene = notes.scene_at(gemeint, strom.session_id, jetzt)
    if szene is None:
        # Keine Szene heißt: diese Sitzung gibt es nicht mehr. Weiterzusehen hieße, Würfe
        # zu sammeln, die nirgends mehr hingehören.
        return Meldung(weiter=False)
    for nachricht in ergebnis.neu:
        notes.link_foundry_message(gemeint, szene, nachricht.id)
    inhaltssprache = settings.sprache(gemeint)
    vorlauf.extend(_wurfzeile(nachricht, inhaltssprache) for nachricht in ergebnis.neu)
    return Meldung(text="\n".join(vorlauf))


def _mit_meldung(arbeit: Callable[[], str], melden: Callable[[str], None]) -> Callable[[], str]:
    def lauf() -> str:
        try:
            ergebnis = arbeit()
        except Exception as fehler:
            # Der Lauf hängt an keinem Befehl mehr — ohne diese Zeile bliebe der Kanal
            # still, und niemand wüsste, dass nichts mehr kommt.
            melden(jobs.NICHT_DURCHGEKOMMEN.format(grund=fehler))
            raise
        melden(ergebnis)
        return ergebnis

    return lauf


def _eckpunkt(geordnet: tuple[notes.Session, ...], wert: str, ende: bool) -> notes.Session:
    """Welche Sitzung ein eingetipptes Datum meint — leer heißt: der Rand des Bestands."""
    sauber = wert.strip()
    if not sauber:
        return geordnet[-1] if ende else geordnet[0]
    passend = [sitzung for sitzung in geordnet if sitzung.played_on.startswith(sauber)]
    if not passend:
        raise ChronikFehler(
            BEREICH_UNBEKANNT.format(
                wert=sauber, erste=geordnet[0].played_on, letzte=geordnet[-1].played_on
            )
        )
    return passend[-1] if ende else passend[0]


def sitzungsbereich(runde: Runde, von: str, bis: str) -> tuple[notes.Session, notes.Session]:
    """Die beiden Eckpunkte des Bereichs, in gespielter Reihenfolge.

    Vertauschte Angaben werden geordnet statt abgewiesen: welcher Abend der frühere ist,
    weiß die Datenbank besser als der, der es tippt — und die Antwort nennt beide Daten,
    also sieht er, was er bekommt.
    """
    geordnet = tuple(
        sorted(notes.sessions(runde), key=lambda sitzung: (sitzung.played_on, sitzung.id))
    )
    if not geordnet:
        raise ChronikFehler(OHNE_SITZUNGEN)
    erste = _eckpunkt(geordnet, von, ende=False)
    letzte = _eckpunkt(geordnet, bis, ende=True)
    if (letzte.played_on, letzte.id) < (erste.played_on, erste.id):
        return letzte, erste
    return erste, letzte


def nacherzaehlung_starten(
    config: Config,
    runde: Runde,
    von: str,
    bis: str,
    kanal_id: str,
    *,
    melden: Callable[[str], None],
) -> str:
    """Der Wunsch nach Prosa über mehrere Abende — ein Lauf, eine Datei im Kanal.

    Ein eigener Lauf und kein Befehl, der wartet: über den Bereich fällt je Sitzung ein
    Modellaufruf an, und Discord lässt einem Befehl keine Viertelstunde.
    """
    erste, letzte = sitzungsbereich(runde, von, bis)
    if jobs.running(runde, jobs.NACHERZAEHLUNG):
        return ERZAEHLT_SCHON
    auftrag = jobs.start(
        config,
        runde,
        jobs.NACHERZAEHLUNG,
        _mit_meldung(
            lambda: jobs.nacherzaehlung(config, runde, erste.id, letzte.id, kanal_id), melden
        ),
    )
    if auftrag is None:
        return jobs.belegt(runde)
    return ERZAEHLT.format(von=erste.played_on, bis=letzte.played_on)


def abgleich_starten(
    config: Config,
    runde: Runde,
    passwort: str | None,
    *,
    wer: str = "",
    merken: bool = True,
    melden: Callable[[str], None],
) -> str:
    """Nur die Zahlen holen — der eine Schritt aus dem Abschluss, freistehend.

    Kein zweiter Weg, sondern derselbe: ein Auftrag über ``jobs``, dieselbe Sperre gegen
    einen zweiten Lauf, dieselbe Meldung im Kanal. Der Unterschied zu ``abschluss_starten``
    ist, was danach kommt — hier nichts, denn es gibt keine Sitzung, die geschrieben werden
    will. Nach einem Foundry-Ausfall ist genau das der fehlende Griff (#116): den Stand
    nachziehen, ohne eine Sitzung zu führen und ohne bis zum nächtlichen Lauf zu warten.

    Das Passwort reist wie beim Abschluss **mit dem Auftrag** und wird vom Abgleich
    verbraucht — auch vom gescheiterten. ``merken=False`` heißt: es kam gerade aus dem
    Merkzettel und darf dort keine neue Frist bekommen.
    """
    if jobs.running(runde, jobs.ABGLEICH):
        return ABGLEICH_LAEUFT_SCHON
    if passwort is not None and merken:
        zugang.merken(runde, passwort, wer=wer)
    auftrag = jobs.start(
        config,
        runde,
        jobs.ABGLEICH,
        _mit_meldung(lambda: jobs.abgleich(config, runde, passwort=passwort), melden),
    )
    return ABGLEICH if auftrag is not None else jobs.belegt(runde)


def abschluss_starten(
    config: Config,
    runde: Runde,
    session_id: int,
    passwort: str | None,
    *,
    wer: str = "",
    merken: bool = True,
    melden: Callable[[str], None],
) -> str:
    """Abgleich, Verschriften, Komponieren — ein Auftrag, eine Meldung im Kanal.

    Das Passwort **reist mit dem Auftrag** und wird vom Abgleich verbraucht; liegen bleibt
    es nicht. Der Auftrag holt es sich nicht selbst aus dem Merkzettel: er läuft in einem
    eigenen Faden, und was dort gelesen würde, muss nicht mehr das sein, was der Aufrufer
    geprüft hat. ``None`` heißt: keines gegeben — dann bleibt es beim Merkzettel, den der
    Abgleich als Rückfall liest, und das Gemerkte wird nicht überschrieben. Ein leerer
    Text bleibt dagegen ein leeres Feld und vergisst, was da war.

    ``merken=False`` sagt: dieses Passwort kam gerade **aus** dem Merkzettel. Es dort
    wieder abzulegen setzte die Frist neu — und da ``jobs.start`` leer ausgeht, sobald
    eine andere Runde die Maschine hält, verbrauchte es dann niemand. Jeder erneute
    Versuch schöbe die zwölf Stunden aus #64 weiter, bis sie nichts mehr begrenzen.
    """
    if jobs.running(runde, jobs.CHRONIK):
        return LAEUFT_SCHON
    if passwort is not None and merken:
        zugang.merken(runde, passwort, wer=wer)
    auftrag = jobs.start(
        config,
        runde,
        jobs.CHRONIK,
        _mit_meldung(lambda: jobs.abschluss(config, runde, session_id, passwort=passwort), melden),
        session_id=session_id,
    )
    if auftrag is None:
        return jobs.belegt(runde)
    # Erst wenn der Lauf wirklich steht: eine Sitzung, die auf einer belegten Maschine
    # abprallt, bleibt offen — sonst zählte das Getippte danach nirgends mehr, obwohl
    # niemand abgeschlossen hat.
    sitzung_schliessen(runde, session_id)
    return abschlussmeldung(runde, session_id)
