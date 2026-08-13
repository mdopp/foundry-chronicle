"""Der Thread ist die Sitzung.

``/chronik start`` legt beides zugleich an: die Sitzung und den Discord-Thread, in dem sie
geschrieben wird. Danach braucht es kein Formular mehr — **jede Nachricht im Thread ist
eine Notiz**, ``/szene`` zieht die Trennlinie zur nächsten, eine Sprachnachricht wird ein
Diktat, und ``/chronik fertig`` stößt den einen Lauf an, der aus alldem die Chronik macht.

Der Thread ist der natürliche Behälter: Anfang, Ende, Teilnehmerliste, Zeitachse — und die
Runde tippt ohnehin dort.

Diese Datei kennt Discord nicht. Sie bekommt Kennungen, Text und Zeitpunkte und gibt Sätze
zurück; wer sie damit füttert, entscheidet ``gateway.py``.

Drei Regeln stehen über allem:

* **Die Gilde bestimmt die Runde.** Es gibt hier keinen Rückfall auf »die erste« — ein
  Thread aus einem fremden Server, der in eine fremde Chronik schriebe, wäre genau das
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
from dataclasses import dataclass
from pathlib import Path

from chronicle import dokument, jobs, lebenszyklus, notes, recordings, settings, zugang
from chronicle import runde as runden
from chronicle.bot import BotFehler
from chronicle.config import Config
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

KEINE_RUNDE = (
    "Für diesen Server ist noch keine Runde eingerichtet — ich lege hier nichts ab und "
    "erfinde auch keine. `/setup` richtet sie ein; das dauert eine Minute."
)

# Die Runde ist noch da, sie schweigt nur. Das zu sagen ist wichtiger, als es zu erklären:
# wer den Bot zurückholt, bekommt alles zurück — bis zum genannten Tag.
GESPERRT = (
    "Diese Runde ruht, seit ich von diesem Server geflogen bin. Bis zum {datum} liegt "
    "alles noch da: lade mich wieder ein, dann geht es weiter, wo ihr aufgehört habt. "
    "Danach ist es gelöscht."
)

# Was ein Klick sagt, dessen Ansicht eine Runde meint, die es so nicht mehr gibt. Getan
# wird dann nichts: die Kennung darunter kann inzwischen einer fremden Gilde gehören.
VERALTET = (
    "Diese Ansicht ist von vorhin, und seither hat sich hier etwas geändert. Ich habe "
    "nichts geändert — ruf den Befehl noch einmal auf."
)

NUR_IM_THREAD = (
    "Das geht nur im Thread einer Sitzung. `/chronik start` beginnt eine — danach dort "
    "weiterschreiben."
)

# Was der Abschluss sagt, wenn wirklich keine Sitzung offen ist. Nur dann ist der Rat zu
# `/chronik start` richtig: steht eine, legte er eine zweite an, und die Aufnahmen der
# ersten blieben liegen (#156).
KEINE_SITZUNG = (
    "Hier läuft gerade keine Sitzung, ich habe also nichts abzuschließen. "
    "`/chronik start` beginnt eine."
)

KEIN_THREAD = (
    "Ich darf in diesem Kanal keinen Thread anlegen, und eine halbe Sitzung lege ich nicht "
    "an. Gib mir das Recht dazu oder ruf mich in einem Kanal, in dem ich es habe."
)

ANGELEGT = (
    "Die Sitzung läuft. Ab jetzt hier schreiben: **jede Nachricht wird eine Notiz**, eine "
    "Sprachnachricht oder eine Audiodatei wird ein Diktat. `/szene <Name>` beginnt die "
    "nächste Szene, `/chronik fertig` schließt die Sitzung ab. Wer eine Nachricht ändert "
    "oder löscht, ändert oder löscht damit auch die Notiz."
)

THREAD_STEHT = "Die Sitzung steht: {thread} — dort geht es weiter."

# Thread und Sitzung stehen, nur die Begrüßung darin kam nicht durch. Das muss anders
# klingen als ein Fehlschlag: »noch einmal versuchen« legte hier ein zweites Paar an.
STUMM_ANGELEGT = (
    "Die Sitzung steht: {thread} — meine Begrüßung darin ist aber nicht angekommen. Ruf "
    "`/chronik start` **nicht** noch einmal auf, sonst stehen zwei; schreib einfach dort "
    "weiter, jede Nachricht wird eine Notiz."
)

SZENE = "Neue Szene »{name}«. Was ab jetzt geschrieben wird, gehört dazu."
SZENE_OHNE_NAMEN = "Neue Szene. Was ab jetzt geschrieben wird, gehört dazu."

DIKTAT = "Aufnahme angekommen — verschriftet wird sie, sobald du `/chronik fertig` gibst."

ZU_GROSS = "»{name}« ist größer als {grenze} MB und bleibt liegen."

# Kein Fehlschlag der Sitzung, sondern ein misslungener Anhang: die Meldung sagt beides,
# sonst sucht die Runde am nächsten Tag nach einer Aufnahme, die es nie gab.
LEER = (
    "»{name}« kam ohne Ton an — null Bytes. Ich reihe sie nicht ein; schick sie einfach "
    "noch einmal. An der Sitzung fehlt sonst nichts."
)

# Eine Markdown-Datei im Thread ist weder Notiz noch Diktat. Sie fiel bis #169 still
# durch — und Stille ist hier das Schlechteste: die Runde hält den Altbestand für abgelegt.
DOKUMENT_IM_THREAD = (
    "»{name}« sieht nach Notizen aus und nicht nach einer Aufnahme — hier im Thread mache "
    "ich daraus nichts. `/chronik einlesen` nimmt so eine Datei im Kanal der Runde "
    "entgegen und legt daraus Sitzungen an; vorher zeigt es, was entstünde."
)

DOKUMENT_KEINE_DATEI = (
    "»{name}« lese ich nicht: ich nehme Notizen als Markdown- oder Textdatei ({endungen})."
)

DOKUMENT_ZU_GROSS = "»{name}« ist größer als {grenze} MB — so viel Text lese ich nicht ein."

DOKUMENT_UNLESBAR = (
    "»{name}« ist kein Text, den ich lesen kann — erwartet wird UTF-8. Angelegt habe ich nichts."
)

# Kein Fehlschlag, sondern die häufigste Ursache: die Abende stehen ohne Datum da. Der
# Satz nennt die beiden Schreibweisen, die sicher gelesen werden — geraten wird keine.
DOKUMENT_OHNE_ABENDE = (
    "In »{name}« finde ich keinen Abend. Eine Sitzung erkenne ich an einer Überschrift mit "
    "Datum — `## 12.03.2026` oder `## 2026-03-12`. Angelegt habe ich nichts."
)

DOKUMENT_VORSCHAU = "**Aus »{name}« würde ich {sitzungen} anlegen:**"

DOKUMENT_ABEND = "• **{datum}**{titel} — {szenen}"

DOKUMENT_OHNE_DATUM = (
    "Ohne Datum in der Überschrift und deshalb ausgelassen: {liste}. Trag dort eines nach "
    "und schick die Datei noch einmal — raten tue ich es nicht."
)

# Ein Abend ohne einen einzigen Satz wird nicht angelegt (#172). Der Satz sagt beides: was
# ausgelassen wurde und wie es hereinkäme — sonst hielte die Runde den Abschnitt für abgelegt.
DOKUMENT_OHNE_TEXT = (
    "Ohne einen Satz darunter und deshalb ausgelassen: {liste}. Eine Sitzung, die nichts "
    "trägt, lege ich nicht an — schreib etwas darunter und schick die Datei noch einmal."
)

# Der Nachsatz allein trüge die Antwort nicht: »eine Sitzung erkenne ich an einem Datum«
# wäre hier falsch, das Datum steht ja da.
DOKUMENT_NUR_OHNE_TEXT = "In »{name}« steht unter keinem Abend ein Satz. Angelegt habe ich nichts."

DOKUMENT_SCHON_DA = "Das steht schon da und bleibt, wie es ist: {liste}."

DOKUMENT_FRAGE = "Soll ich das so anlegen? Bis du bestätigst, ist nichts geschrieben."

DOKUMENT_JA = "Ja, anlegen"
DOKUMENT_NEIN = "Abbrechen"

DOKUMENT_ANGELEGT = (
    "{sitzungen} angelegt. Sie stehen ab jetzt in der Chronik und `/suche` findet, was "
    "darin vorkommt."
)

DOKUMENT_ABGEBROCHEN = "Gut, ich habe nichts angelegt."

# Der zweite Klick auf denselben Knopf, oder die zweite Vorschau desselben Dokuments: die
# Abende stehen schon, und ein »angelegt« darunter wäre die Zusage, dass sie zweimal da sind.
DOKUMENT_NICHTS_NEU = (
    "Diese Abende stehen schon in der Chronik — angelegt habe ich nichts. Doppelt steht "
    "dadurch nichts da."
)

NICHT_ABGELEGT = (
    "Das konnte ich nicht ablegen: {grund} Schreib es noch einmal — bleibt es dabei, "
    "steht der Grund im Log des Bots."
)

FERTIG = (
    "Die Zahlen aus Foundry holen, die Aufnahmen verschriften, die Chronik schreiben — das "
    "dauert seine Zeit; ich melde mich hier, wenn sie steht."
)

# Welche Sitzung gemeint ist und wo ihre Chronik erscheint, gehört seit #156 in die
# Antwort: abgeschlossen wird auch aus dem Sprachkanal, und dort sieht man beides nicht.
SCHLIESST = "Ich schließe **{sitzung}** ab — die Chronik kommt in {thread}."

# Sitzungen aus der Weboberfläche haben keinen Thread. Das ist kein Fehlschlag: die
# Chronik entsteht trotzdem, sie wird nur nirgends angehängt.
SCHLIESST_OHNE_THREAD = (
    "Ich schließe **{sitzung}** ab — einen Thread hat sie nicht, die Chronik wird also "
    "nur abgelegt."
)

LAEUFT_SCHON = "Ich bin schon dabei — ich melde mich hier, wenn die Chronik steht."

# Der Bereich wird über **Sitzungen** benannt, nicht über einen Faden und nicht über eine
# Figur: von welchem Abend bis zu welchem. Ein Datum genügt, ein Anfang davon auch.
OHNE_SITZUNGEN = (
    "Hier ist noch keine Sitzung geschrieben — nachzuerzählen gibt es nichts. "
    "`/chronik start` beginnt die erste."
)

BEREICH_UNBEKANNT = (
    "»{wert}« passt zu keiner Sitzung. Ich kenne welche vom {erste} bis zum {letzte} — "
    "nimm ein Datum daraus oder lass das Feld leer, dann nehme ich den Rand."
)

ERZAEHLT = (
    "Ich erzähle die Sitzungen vom {von} bis zum {bis} nach — Sitzung für Sitzung, entlang "
    "des Registers. Das dauert seine Zeit; die Datei kommt hier in den Kanal."
)

ERZAEHLT_SCHON = "Ich erzähle schon nach — die Datei kommt hier in den Kanal."

ABGLEICH_TITEL = "Zahlen aus Foundry holen"

ABGLEICH = (
    "Ich hole die Zahlen aus eurem Foundry. Das dauert einen Moment — ich melde mich hier, "
    "wenn ich durch bin."
)

ABGLEICH_LAEUFT_SCHON = "Ich hole schon — ich melde mich hier, wenn ich durch bin."

PASSWORT_TITEL = "Sitzung abschließen"
PASSWORT_FELD = "Passwort für Foundry"
PASSWORT_HINWEIS = "Nur für diesen einen Abgleich — es wird nirgends gespeichert."

START_TITEL = "Sitzung beginnen"
START_FELD = "Passwort für Foundry — freiwillig"
START_HINWEIS = "Leer lassen geht: dann eben ohne die Zahlen."

MIT_FOUNDRY = (
    "Das Passwort liegt bis zum Abschluss bereit — gespeichert wird es nirgends, und "
    "`/chronik fertig` fragt nicht noch einmal."
)

OHNE_FOUNDRY = (
    "Ohne Foundry-Passwort: die Sitzung läuft, nur die Zahlen fehlen. `/chronik fertig` "
    "fragt dann noch einmal danach."
)

KEIN_FOUNDRY = (
    "Nach dem Foundry-Passwort frage ich nicht: für diese Runde ist gerade kein "
    "Foundry-Server im Spiel. `/setup` trägt Adresse und Benutzer ein, wenn doch einer "
    "dazukommt."
)

# Der Platzhalter des Abschlussfensters, wenn ein anderes Mitglied etwas hinterlegt hat.
# Discord kürzt Platzhalter bei 100 Zeichen — deshalb der kurze Satz.
FREMDES_HINWEIS = "Es liegt eines von jemand anderem bereit — es gilt deines."


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
class Nachricht:
    """Eine Nachricht im Sitzungs-Thread, ohne alles, was Discord sonst noch mitschickt."""

    id: str
    text: str = ""
    zeitpunkt: str = ""
    anhaenge: tuple[Anhang, ...] = ()
    autor_id: str | None = None


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


def sitzung_des_threads(runde: Runde, thread_id: str) -> int | None:
    return notes.session_of_thread(runde, thread_id)


def thread_der_sitzung(runde: Runde, session_id: int) -> str | None:
    return notes.thread_of_session(runde, session_id)


def sitzung_verlangen(runde: Runde, thread_id: str) -> int:
    sitzung = sitzung_des_threads(runde, thread_id)
    if sitzung is None:
        raise ChronikFehler(NUR_IM_THREAD)
    return sitzung


def laufende_sitzung(runde: Runde) -> int:
    """Die Sitzung, die der Abschluss meint — dieselbe Auskunft, die die Aufnahme nimmt.

    Nicht über den Thread: nach ``/aufnahme stop`` steht man im Sprachkanal, und ein
    Abschluss, der dort abwiese, riete zu einer zweiten Sitzung — die Aufnahmen der ersten
    blieben liegen und die Chronik entstünde für die falsche (#156). Je Runde ist genau
    eine offen, also ist die Auskunft eindeutig.
    """
    sitzung = notes.latest_session(runde)
    if sitzung is None:
        raise ChronikFehler(KEINE_SITZUNG)
    return sitzung.id


def letzte_sitzung(runde: Runde) -> int | None:
    """Dieselbe Auskunft wie ``laufende_sitzung``, nur ohne Wurf — für alles, was bloß sagt.

    Ein Befehl, der eine Auskunft gibt, darf an einer fehlenden Sitzung nicht scheitern:
    dann gibt es eben keinen Thread, in dem etwas stehen könnte.
    """
    sitzung = notes.latest_session(runde)
    return None if sitzung is None else sitzung.id


def sitzungsname(sitzung: notes.Session) -> str:
    """Wie eine Sitzung in einer Antwort heißt — ihr Titel, sonst ihr Abend."""
    return (sitzung.title or "").strip() or f"Sitzung vom {sitzung.played_on}"


def abschlussmeldung(runde: Runde, session_id: int) -> str:
    """Was der Abschluss antwortet: welche Sitzung, wohin die Chronik, wie lange es dauert."""
    sitzung = notes.session(runde, session_id)
    if sitzung is None:
        return FERTIG
    name = sitzungsname(sitzung)
    thread = thread_der_sitzung(runde, session_id)
    kopf = (
        SCHLIESST.format(sitzung=name, thread=f"<#{thread}>")
        if thread
        else SCHLIESST_OHNE_THREAD.format(sitzung=name)
    )
    return f"{kopf} {FERTIG}"


def threadname(titel: str) -> str:
    return titel.strip() or f"Sitzung vom {notes.today()}"


def sitzung_anlegen(runde: Runde, thread_id: str, titel: str = "") -> int:
    return notes.create_session(runde, title=titel, thread_id=thread_id)


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

    Nicht bloß »es liegt eines da«: ``/chronik start`` steht jedem Mitglied offen, und
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
    """``jobs.mehrzahl`` hängt ein ``n`` an — das trägt »Szene«, aber nicht »Sitzung«."""
    return f"{wieviele} {einzahl if wieviele == 1 else mehrzahl}"


def _szenenzahl(abend: dokument.Abend) -> str:
    # Auch ein Abend ohne eigene Überschriften bekommt eine Szene — die, die mit der
    # Sitzung entsteht und seinen Text trägt.
    return _anzahl(max(len(abend.szenen), 1), "Szene", "Szenen")


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
                liste=", ".join(f"»{kopf}«" for kopf in aufteilung.ohne_datum)
            )
        )
    if aufteilung.ohne_text:
        nachsatz.append(
            DOKUMENT_OHNE_TEXT.format(liste=", ".join(f"»{kopf}«" for kopf in aufteilung.ohne_text))
        )
    if schon:
        nachsatz.append(DOKUMENT_SCHON_DA.format(liste=_abendliste(schon)))
    if not frisch:
        leer = aufteilung.ohne_text and not (aufteilung.abende or aufteilung.ohne_datum)
        anfang = DOKUMENT_NUR_OHNE_TEXT if leer else DOKUMENT_OHNE_ABENDE
        return Vorschau(" ".join((anfang.format(name=datei.filename), *nachsatz)))
    kopf = DOKUMENT_VORSCHAU.format(
        name=datei.filename, sitzungen=_anzahl(len(frisch), "Sitzung", "Sitzungen")
    )
    zeilen = "\n".join(_abendzeile(abend) for abend in frisch)
    return Vorschau("\n".join((kopf, zeilen, " ".join((*nachsatz, DOKUMENT_FRAGE)))), frisch)


def dokument_anlegen(runde: Runde, abende: Sequence[dokument.Abend]) -> str:
    """Das Bestätigte anlegen — und im Log steht keine Zeile aus dem Dokument.

    Weder der Dateiname noch eine Überschrift: beides kommt von der Gruppe und kann jeden
    Klarnamen enthalten. Wie viele Abende entstanden sind, sagt genug.

    Gegen den Bestand geprüft wird **hier** und nicht nur in der Vorschau: die Vorschau
    friert ihre Abende ein und ist ephemer, also ruft auf, wer unsicher ist, ob sie durchkam
    — zwei offene Vorschauen desselben Dokuments legten den Abend sonst zweimal an. Und eine
    einzelne Sitzung wieder loszuwerden gibt es nicht; ``/chronik loeschen`` nimmt die ganze
    Runde.
    """
    angelegt = dokument.anlegen(runde, dokument.neu(runde, abende))
    logger.info("Notizdokument eingelesen: %s Sitzungen angelegt.", angelegt)
    if not angelegt:
        return DOKUMENT_NICHTS_NEU
    return DOKUMENT_ANGELEGT.format(sitzungen=_anzahl(angelegt, "Sitzung", "Sitzungen"))


def notiz_aendern(runde: Runde, message_id: str, text: str) -> bool:
    return notes.update_note(runde, message_id, text)


def notiz_entfernen(runde: Runde, message_id: str) -> bool:
    return notes.remove_note(runde, message_id)


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
    """Abgleich, Verschriften, Komponieren — ein Auftrag, eine Meldung im Thread.

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
    return abschlussmeldung(runde, session_id) if auftrag is not None else jobs.belegt(runde)
