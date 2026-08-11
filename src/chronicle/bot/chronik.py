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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from chronicle import jobs, lebenszyklus, notes, recordings, settings, zugang
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

NICHT_ABGELEGT = (
    "Das konnte ich nicht ablegen: {grund} Schreib es noch einmal — bleibt es dabei, "
    "steht der Grund im Log des Bots."
)

FERTIG = (
    "Ich schließe die Sitzung ab: die Zahlen aus Foundry holen, die Aufnahmen verschriften, "
    "die Chronik schreiben. Das dauert seine Zeit — ich melde mich hier, wenn sie steht."
)

LAEUFT_SCHON = "Ich bin schon dabei — ich melde mich hier, wenn die Chronik steht."

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
    config: Config, runde: Runde, session_id: int, anhang: Anhang, autor_id: str | None
) -> str:
    if anhang.size > recordings.MAX_BYTES:
        return ZU_GROSS.format(name=anhang.filename, grenze=recordings.MAX_BYTES // (1024 * 1024))
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings.target_path(config.recordings_dir, session_id, anhang.filename)
    await anhang.speichern(ziel)
    recordings.enqueue(runde, session_id, ziel.name, discord_user_id=autor_id)
    logger.info("Diktat aus dem Thread: %s → Sitzung %s", ziel.name, session_id)
    return DIKTAT


async def aufnehmen(
    config: Config, runde: Runde, session_id: int, nachricht: Nachricht
) -> tuple[str, ...]:
    """Text wird Notiz, Audio wird Diktat — beides an derselben Nachricht.

    Zurück kommt nur, was gesagt werden muss. Eine abgelegte Notiz sagt nichts: die
    Nachricht steht im Thread und **ist** die Notiz; eine Quittung darunter wäre die zweite
    Hälfte jedes Satzes eines ganzen Abends.
    """
    meldungen = [
        await _diktat(config, runde, session_id, anhang, nachricht.autor_id)
        for anhang in nachricht.anhaenge
        if ist_diktat(anhang.filename)
    ]
    if nachricht.text.strip():
        szene = notes.scene_at(runde, session_id, nachricht.zeitpunkt)
        notes.add_note(runde, szene, nachricht.text, message_id=nachricht.id)
    return tuple(meldungen)


def notiz_aendern(runde: Runde, message_id: str, text: str) -> bool:
    return notes.update_note(runde, message_id, text)


def notiz_entfernen(runde: Runde, message_id: str) -> bool:
    return notes.remove_note(runde, message_id)


def _mit_meldung(
    config: Config,
    runde: Runde,
    session_id: int,
    passwort: str | None,
    melden: Callable[[str], None],
) -> Callable[[], str]:
    def lauf() -> str:
        try:
            ergebnis = jobs.abschluss(config, runde, session_id, passwort=passwort)
        except Exception as fehler:
            # Der Lauf hängt an keinem Befehl mehr — ohne diese Zeile bliebe der Thread
            # still, und niemand wüsste, dass die Chronik nicht kommt.
            melden(jobs.NICHT_DURCHGEKOMMEN.format(grund=fehler))
            raise
        melden(ergebnis)
        return ergebnis

    return lauf


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
        _mit_meldung(config, runde, session_id, passwort, melden),
        session_id=session_id,
    )
    return FERTIG if auftrag is not None else jobs.BELEGT
