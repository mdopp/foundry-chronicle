"""Einladen, einrichten, verabschieden — was der Bot beim Kommen und beim Gehen sagt.

Drei Sätze tragen diese Datei:

* **Die Einladung ist ehrlich.** Der erste Satz in einer fremden Gilde sagt nicht nur, was
  der Bot kann, sondern auch, wo er steht: auf einer Kiste, die jemand anderem gehört, und
  deren Betreiber alles lesen kann, was hier abgelegt wird. Das gehört in die erste
  Nachricht und nicht ins Kleingedruckte — eine Gruppe entscheidet sonst über ihre
  Sitzungsprotokolle, ohne zu wissen, worüber sie entscheidet.
* **Eingerichtet wird in einem Fenster, nicht in einer Befehlszeile.** Und das Passwort
  kommt hier nicht vor: es wird beim Abschluss der Sitzung erfragt, verbraucht und
  vergessen. Ein Feld dafür gäbe es nur, wenn wir es behalten wollten.
* **Löschen wird gesagt, bevor es passiert.** Was verschwindet, steht vollständig da, und
  danach kommt ein Knopf — kein Befehl, der beim Vertippen eine Kampagne mitnimmt. Und was
  *nicht* verschwindet, steht ebenso da: eine ausgelieferte Chronik liegt in einem
  Discord-Kanal, und dorthin reicht kein Löschlauf dieser Box.
* **Einrichten und Löschen sind keine Handlungen für jedes Mitglied.** Wer was darf,
  entscheidet Discord (#62) — aber nur, wenn der Code eine Berechtigung verlangt. Diese
  beiden tun es: die Adresse entscheidet, wohin das Foundry-Passwort der Spielleitung
  geht, und das Löschen nimmt der Runde alles.

Diese Datei kennt Discord nicht. Sie bekommt eine Gilde-Kennung und ein paar Texte und gibt
Sätze zurück; wer daraus Fenster, Menüs und Knöpfe baut, entscheidet ``gateway.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chronicle import db, lebenszyklus, settings
from chronicle import sprache as sprachen
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.runde import Runde

# -- Einladen ---------------------------------------------------------------------------

# Der Satz, für den es dieses Modul gibt. Er steht in **jeder** Begrüßung, auch in der an
# eine Gruppe, die zurückkommt: eine Runde entscheidet sonst über ihre Sitzungsprotokolle,
# ohne zu wissen, worüber sie entscheidet.
OFFENLEGUNG = (
    "**So you know: I run on a machine that belongs to somebody else.** Whoever operates "
    "it can reach everything you file here — your notes, what was spoken, your chronicles. "
    "I promise you no confidentiality I cannot keep. If that is not acceptable for your "
    "group, throw me out again."
)

WILLKOMMEN = (
    "Hello. I write your sessions down: out of your notes, out of what is spoken in the "
    "voice channel, and out of the rolls in your Foundry I make a readable chronicle.\n"
    "\n"
    f"{OFFENLEGUNG}\n"
    "\n"
    "This is how you start: **`/chronicle setup`** — there you enter where your Foundry "
    "lives, under which account I log in there, in which channel I file the finished "
    "chronicle, and in which language the content is written. After that `/session start` "
    "begins the first session, and `/session help` shows the rest. Whatever of yours lies "
    "here you can delete yourselves at any time: `/chronicle delete`."
)

WILLKOMMEN_ZURUECK = (
    "Here I am again. The round “{name}” is still complete — everything you wrote stands "
    "ready again, and `/session start` begins the next session.\n"
    "\n"
    f"{OFFENLEGUNG}"
)

RUNDE_OHNE_NAMEN = "New round"

NUR_IM_SERVER = (
    "This only works on the server I am supposed to write for — here in a direct message "
    "I do not know which round you mean."
)

# Die beiden Absagen an ein Mitglied ohne Recht. Sie sagen den Grund, nicht bloß »nein«:
# hinter beiden steht eine Gefahr, die man kennen sollte, auch wenn man sie nicht auslösen
# darf.
NUR_VERWALTUNG = (
    "Setting up is for whoever manages this server. This is where your Foundry lives — and "
    "that is where I later present your game master's password. Please ask somebody with "
    "the “Manage Server” permission to do it."
)

NUR_ADMIN = (
    "Deleting is for whoever runs this server as an administrator. There is no backup I "
    "can restore a chronicle from; that is why this is not open to every member."
)

# -- Einrichten -------------------------------------------------------------------------

SETUP_TITEL = "Set up the round"

FELD_ADRESSE = "Address of your Foundry"
FELD_BENUTZER = "Foundry account I see through"
FELD_UHRZEIT = "Time of the nightly run"
# Die Zone steht neben der Uhrzeit und nicht woanders: sie sagt nichts für sich, sondern
# nur, welche Uhr das Feld darüber meint. Wer 04:00 einträgt, entscheidet in demselben
# Atemzug, wessen vier Uhr gemeint ist — getrennte Bedienstellen hießen, dass beides
# auseinanderlaufen kann, ohne dass es jemand merkt.
FELD_ZONE = "Time zone this time refers to"

HINWEIS_ADRESSE = "e.g. https://foundry.example"
# Discord lässt 45 Zeichen für die Beschriftung und 100 für den Hinweis; der ganze Satz
# passt in keins von beidem und steht deshalb als AUGEN in der Antwort.
HINWEIS_BENUTZER = "best a player account — otherwise I also see what you have not played yet"
HINWEIS_UHRZEIT = "leave empty for 04:00"
HINWEIS_ZONE = "e.g. Europe/Berlin — leave empty for Europe/Berlin"

# Die Wahl, die niemand trifft und die trotzdem alles prägt (#78): der Zugang trägt die
# Rechte genau eines Kontos, und was es nicht sieht, kommt in keine Chronik.
AUGEN = (
    "Which account I log in with decides what I see of your world: a player account shows "
    "me what the group has lived through — a game master account also shows unplayed plot "
    "lines, hidden NPCs and traps. Take the player account; cleanest is a Foundry account "
    "of its own called “Chronicle” with the same permissions.\n"
    "\n"
    "**“With the same permissions” is handwork.** Foundry grants sight of a character "
    "**per character**, in its ownership settings; a freshly created account stands in "
    "none of them and therefore sees **less than any player account**. Enter it on the "
    "group's characters — at least as observer, because “limited” shows me only the name. "
    "How that works in detail is in `docs/foundry-zugriff.md`."
)

# Ein leeres Feld heißt hier dasselbe wie überall sonst: unverändert. Sonst löschte ein
# zweiter Aufruf, der nur den Kanal ändern soll, die Adresse gleich mit.
LEER_BLEIBT = "Whatever you leave empty stays as it was."

KEIN_PASSWORT = (
    "I do not ask for the password here. It comes at the end of the session, is used once "
    "and forgotten afterwards — it is stored nowhere."
)

EINGERICHTET = "The round “{name}” stands."
UEBERNOMMEN = "The round “{name}” is updated."

KANAL_FRAGE = "And where does the finished chronicle go?"
KANAL_WAEHLEN = "Channel for the chronicle"
KANAL_GESETZT = "The chronicle will go to {kanal} from now on."
KANAL_KEINER = "No channel — then I file the chronicle in the session's channel."
KANAL_OHNE = "none — file it in the session's channel"

# -- In welcher Sprache der Inhalt entsteht ----------------------------------------------

# Wieder ein Menü und kein Feld, und diesmal wiegt der Grund schwerer als bei der Quelle:
# an dieser Wahl hängt die hörbare Einwilligungs-Ansage. Ein getipptes »deutsch« ginge als
# unbekannter Wert zurück, und wer die Absage überliest, spielt den Abend mit einer Ansage,
# die am Tisch niemand versteht. Ein Menü kennt nur die Sprachen, für die es Texte gibt.
SPRACHE_FRAGE = "And in which language is the content written?"
SPRACHE_WAEHLEN = "Language of the content"
SPRACHE_GESETZT = (
    "Content is written in **{sprache}** from now on: the audible announcement in the voice "
    "channel, the transcription of the recordings, and the chronicle and recap the language "
    "model writes. My own replies stay English.\n"
    "The announcement is the one that matters — it is what makes recording lawful, and it "
    "only does that if the people present understand it. Check it once with "
    "`/session check` before the next evening."
)

# Der Wert, mit dem ein Auswahlmenü »keiner« sagt — leer darf eine Option nicht sein.
OHNE_KANAL = "-"

# Ein Auswahlmenü fasst fünfundzwanzig Zeilen, die für »keiner« eingerechnet. Wer mehr
# Kanäle hat, wählt einen der ersten — oder ruft den Befehl im gewünschten Kanal auf.
KANAL_GRENZE = 25

# Der eine Weg zurück in den Dienst, auf dem keine Begrüßung steht: fehlt dem Bot in der
# Gilde jeder beschreibbare Kanal, hätte die Gruppe die Offenlegung nie gelesen. Dann
# bleibt die Runde still, und das wird gesagt statt verschwiegen.
STILL_GEBLIEBEN = (
    "I could not write the disclosure into any channel here — so the round stays silent. "
    "Give me write permission in this channel and call `/chronicle setup` again."
)

FEHLT = "Still missing: {was}. Call `/chronicle setup` again when you want to add it."
STEHT_BEREIT = "Carry on with `/session start` — that creates the first session."

UHRZEIT_UNLESBAR = "I cannot make sense of “{wert}” — I am staying with {uhrzeit}."

# Eine Adresse, die sich nicht zerlegen lässt, wird **abgewiesen** und nicht zurechtgebogen:
# ein stillschweigend vorangestelltes »http://« erriete das Schema eines fremden Servers,
# und ein abgeschnittener Pfad verschwiege, dass jemand die Browserzeile eines
# Anmeldebildschirms kopiert hat. Beides endete wie #243 — eine Runde spielt einen Abend
# lang, und es kommt kein einziger Wurf an, ohne dass irgendwo etwas dazu steht.
ADRESSE_UNBRAUCHBAR = (
    "I cannot make sense of “{wert}” as an address — I have **not** taken it. What is "
    "meant is the root of your Foundry with scheme and port and nothing after it, e.g. "
    "https://foundry.example:30000 — not the line from the browser while a login screen or "
    "a world is open there."
)

# Eine unbekannte Zone wird **abgewiesen**, nicht stillschweigend übernommen: gespeichert
# würde sie sonst, gelesen aber nicht — ``settings.nightly_zone`` fällt auf die Vorgabe
# zurück, und der nächtliche Lauf liefe fortan zu einer anderen Stunde als der, die in der
# Einstellung steht. Ein Tippfehler verschöbe damit stumm die ganze Nacht.
ZONE_UNBEKANNT = (
    "I do not know “{wert}” as a time zone — I am staying with {zone}. What is meant is a "
    "name from the zone database, e.g. Europe/Berlin or America/New_York."
)

# Discord nimmt in einem Feld des Fensters bis zu 4000 Zeichen an, lässt in der Antwort
# darauf aber nur 2000 zu. Eine zurückgespiegelte Eingabe wird deshalb gekürzt, bevor sie
# in den Satz geht: sonst weist Discord die ganze Antwort ab, und der Einrichtende erfährt
# nicht einmal mehr, ob seine Runde nun steht.
ECHO_GRENZE = 60
ECHO_GEKUERZT = "…"

# -- Woher die Zahlen kommen ------------------------------------------------------------

# Kein Feld im Fenster, sondern ein Menü — und das aus einem inhaltlichen Grund, nicht aus
# Platzmangel: ein getipptes »testwelt« wäre die eine Eingabe, deren Vertipper still
# durchgeht (``save_foundry_quelle`` lässt einen unbekannten Wert stehen) und deren
# richtige Schreibweise eine Chronik voller erfundener Zahlen bedeutet. Ein Menü kennt nur
# zwei Antworten, zeigt die geltende an und schreibt die Folge an die Wahl.
QUELLE_FRAGE = "And where do I take the numbers from?"
QUELLE_WAEHLEN = "Source of the game data"
QUELLE_SERVER = "Your Foundry server"
QUELLE_TESTWELT = "Built-in test world — invented numbers"
QUELLE_GESETZT_SERVER = (
    "The numbers will come from your Foundry from now on. Accounts, characters and scenes "
    "are fetched fresh by the next sync."
)

# Gesagt wird, was geschah, und nur wenn es geschah: Konten, Figuren und Szenen sind
# Spiegel und werden ersetzt, Chat-Nachrichten nicht — die bleiben liegen, bis jemand sie
# herausnimmt. Das tut das Zurückschalten selbst, statt es dem nächsten Abgleich zu
# versprechen: der kommt vielleicht nie, und bis dahin stünden erfundene Würfe als Beleg
# in einer Chronik dieser Runde.
TESTWELT_GERAEUMT = "I have taken the test world's invented rolls out of the archive — {anzahl}."
EINE_NACHRICHT = "one chat message"
MEHRERE_NACHRICHTEN = "{anzahl} chat messages"
QUELLE_GESETZT_TESTWELT = (
    "I will take the built-in test world from now on. **What then stands in your chronicles "
    "is invented** — no roll from it ever happened at your table, and the sync talks to no "
    "server any more. Going back works the same way here."
)

# -- Verabschieden ----------------------------------------------------------------------

LOESCHEN_FRAGE = (
    "**This disappears, finally and immediately:**\n"
    "• every session of this round with its scenes and notes\n"
    "• every dictation and recording from the voice channel — the audio files too\n"
    "• every text written from them: chronicles and recaps\n"
    "• the register with characters, places and plot threads\n"
    "• who among you plays which Foundry character\n"
    "• the records of the announcements in the voice channel\n"
    "• the numbers I fetched from your Foundry\n"
    "\n"
    "**This stays:** what I have already delivered to you — chronicles and recaps in your "
    "channels and threads — remains in Discord; my deleting does not reach there. So the "
    "proof that an announcement was made in the voice channel goes with the records, while "
    "what was written from it stays with you. Whoever needs the proof fetches it "
    "beforehand.\n"
    "\n"
    "There is no backup I can restore this from. Download beforehand whatever you want to "
    "keep."
)

LOESCHEN_JA = "Yes, delete everything"
LOESCHEN_NEIN = "Cancel"

LOESCHEN_FERTIG = "Gone. Nothing of this round is left here."
LOESCHEN_ABGEBROCHEN = "Nothing deleted. Everything stays as it was."

# Ein Knopf lebt eine Viertelstunde; in der Zeit kann die Runde gelöscht und die Kennung
# neu vergeben worden sein. Dann wird nicht gelöscht, sondern gefragt.
LOESCHEN_VERALTET = (
    "This question is from earlier, and something has changed here since. I have deleted "
    "nothing — call `/chronicle delete` again if you still want it."
)

# Was beim Rauswurf passiert — gesagt wird es vorher, in der Einladung und beim Löschen,
# denn danach ist der Bot nicht mehr da, um es zu sagen.
ABSCHIED = (
    "If you throw me out, the round falls silent immediately and is deleted after {tage} "
    "days. If you bring me back before that, everything is there again; after that it is "
    "gone."
)


@dataclass(frozen=True)
class Eingerichtet:
    runde: Runde
    neu: bool
    meldung: str
    ruhte: bool = False


@dataclass(frozen=True)
class Begruessung:
    """Was gesagt wird — und die ruhende Runde, die auf ihre Freigabe wartet.

    Zwei Felder und nicht ein Satz: freigegeben wird erst, wenn der Satz zugestellt ist.
    """

    text: str
    wartet: Runde | None = None


def zurueckgespiegelt(wert: str) -> str:
    """Eine Eingabe, die in einer Antwort wiederholt wird — auf ein zitierbares Maß."""
    gestutzt = wert.strip()
    if len(gestutzt) <= ECHO_GRENZE:
        return gestutzt
    return gestutzt[:ECHO_GRENZE] + ECHO_GEKUERZT


def _uhrzeit(runde: Runde, wert: str) -> str:
    if not wert.strip():
        return ""
    if settings.save_nightly_time(runde, wert):
        return ""
    return UHRZEIT_UNLESBAR.format(
        wert=zurueckgespiegelt(wert), uhrzeit=settings.nightly_time(runde)
    )


def _adresse(runde: Runde, wert: str) -> str:
    if not wert.strip():
        return ""
    if settings.save_foundry_url(runde, wert):
        return ""
    return ADRESSE_UNBRAUCHBAR.format(wert=zurueckgespiegelt(wert))


def _zone(runde: Runde, wert: str) -> str:
    if not wert.strip():
        return ""
    if settings.save_nightly_zone(runde, wert):
        return ""
    return ZONE_UNBEKANNT.format(wert=zurueckgespiegelt(wert), zone=settings.nightly_zone(runde))


def _offen(config: Config, runde: Runde) -> str:
    fehlend = settings.effective(config, runde).missing_foundry_fields
    return FEHLT.format(was=" and ".join(fehlend)) if fehlend else STEHT_BEREIT


def einrichten(
    config: Config,
    guild_id: str,
    gildenname: str,
    *,
    adresse: str = "",
    benutzer: str = "",
    uhrzeit: str = "",
    zone: str = "",
) -> Eingerichtet:
    """Beansprucht die Runde dieser Gilde — oder legt sie an — und übernimmt die Werte.

    Ein leeres Feld ändert nichts. Deshalb wird gefiltert, bevor gespeichert wird: die
    Einstellungen lesen einen leeren Wert sonst als »wieder wegnehmen«, und ein zweiter
    Aufruf, der bloß den Kanal ändern soll, nähme die Adresse mit.

    Ob die Runde neu ist, sagt der Lebenszyklus und nicht ein Vergleich der Kennung: nach
    einer abgelaufenen Frist wird gelöscht und neu angelegt, und SQLite vergibt dieselbe
    Kennung wieder. »Was du leer lässt, bleibt, wie es war« wäre dann eine Lüge über
    Werte, die gerade fortgelöscht wurden.
    """
    beansprucht = lebenszyklus.beanspruchen(config, guild_id, gildenname)
    runde = beansprucht.runde
    benutzername = benutzer.strip()
    if benutzername:
        settings.save(runde, {"foundry_user": benutzername})
    abgewiesen = _adresse(runde, adresse)
    saetze = (
        [EINGERICHTET.format(name=runde.name), KEIN_PASSWORT]
        if beansprucht.neu
        else [UEBERNOMMEN.format(name=runde.name), LEER_BLEIBT]
    )
    # Der Satz steht dort, wo die Wahl getroffen wird — aber nicht bei jedem Aufruf: wer
    # bloß die Uhrzeit richtet, hat über das Konto gerade nichts entschieden.
    if beansprucht.neu or benutzername:
        saetze.append(AUGEN)
    saetze.extend(
        satz for satz in (abgewiesen, _uhrzeit(runde, uhrzeit), _zone(runde, zone)) if satz
    )
    saetze.append(_offen(config, runde))
    return Eingerichtet(
        runde=runde, neu=beansprucht.neu, meldung=" ".join(saetze), ruhte=beansprucht.ruhte
    )


def kanalwahl(
    config: Config, runde: Runde, kanaele: tuple[tuple[str, str], ...]
) -> tuple[tuple[str, str, bool], ...]:
    """Beschriftung, Wert und ob vorgewählt — je Kanal eine Zeile, »keiner« zuerst."""
    gewaehlt = settings.effective(config, runde).discord_recap_channel or ""
    zeilen = [(KANAL_OHNE, OHNE_KANAL, not gewaehlt)]
    for kennung, name in kanaele[: KANAL_GRENZE - 1]:
        zeilen.append((f"#{name}", str(kennung), str(kennung) == gewaehlt))
    return tuple(zeilen)


def kanal_setzen(runde: Runde, kanal_id: str) -> str:
    """Der Zustellkanal der fertigen Chronik — »keiner« ist eine gültige Wahl."""
    if kanal_id == OHNE_KANAL:
        settings.save(runde, {"discord_recap_channel": ""})
        return KANAL_KEINER
    settings.save(runde, {"discord_recap_channel": kanal_id})
    return KANAL_GESETZT.format(kanal=f"<#{kanal_id}>")


def sprachwahl(runde: Runde) -> tuple[tuple[str, str, bool], ...]:
    """Beschriftung, Wert und ob vorgewählt — je Sprache eine Zeile, die geltende angehakt.

    Die Sprachen stehen in ihrer eigenen Schreibweise da (»Deutsch«, nicht »German«): wer
    sie sucht, sucht nach dem Wort, das er selbst benutzt.
    """
    gewaehlt = settings.sprache(runde)
    return tuple(
        (sprachen.NAMEN[kennung], kennung, kennung == gewaehlt) for kennung in sprachen.SPRACHEN
    )


def sprache_setzen(runde: Runde, wert: str) -> str:
    """Die Sprache der Inhalte — was daran hängt, steht in der Antwort.

    Genannt wird die Ansage zuerst und nicht zuletzt: sie ist der Teil, an dem §201 StGB
    hängt, und sie läuft beim nächsten `/session start` ungefragt los. Wer hier umstellt,
    soll das gelesen haben, bevor eine Runde davor sitzt.
    """
    settings.save_sprache(runde, wert)
    gewaehlt = settings.sprache(runde)
    return SPRACHE_GESETZT.format(sprache=sprachen.NAMEN[gewaehlt])


def quellenwahl(runde: Runde) -> tuple[tuple[str, str, bool], ...]:
    """Beschriftung, Wert und ob vorgewählt — zwei Zeilen, die geltende steht angehakt da."""
    gewaehlt = settings.foundry_quelle(runde)
    return (
        (QUELLE_SERVER, settings.SERVER, gewaehlt == settings.SERVER),
        (QUELLE_TESTWELT, settings.TESTWELT, gewaehlt == settings.TESTWELT),
    )


def quelle_setzen(runde: Runde, wert: str) -> str:
    """Echter Server oder Testwelt — die Folge steht in der Antwort, nicht im Kleingedruckten.

    Der Weg zurück räumt gleich mit auf: die Fixture-Nachrichten gehen hier heraus, und
    was herausging, steht in der Antwort.
    """
    settings.save_foundry_quelle(runde, wert)
    if settings.foundry_quelle(runde) == settings.TESTWELT:
        return QUELLE_GESETZT_TESTWELT
    scope = db.scoped(runde)
    try:
        geraeumt = store.testwelt_raeumen(scope)
    finally:
        scope.close()
    if not geraeumt:
        return QUELLE_GESETZT_SERVER
    anzahl = EINE_NACHRICHT if geraeumt == 1 else MEHRERE_NACHRICHTEN.format(anzahl=geraeumt)
    return f"{QUELLE_GESETZT_SERVER} {TESTWELT_GERAEUMT.format(anzahl=anzahl)}"


def begruessung(config: Config, guild_id: str) -> Begruessung:
    """Der eine Satz beim Betreten — und die Rückkehr, wenn die Frist noch läuft.

    Angelegt wird hier nichts: eine Runde entsteht erst, wenn jemand sie einrichtet. Eine
    abgelaufene wird gelöscht und dann begrüßt wie eine fremde Gilde: was als fort zugesagt
    war, kommt nicht als Überraschung zurück.

    Freigegeben wird eine verabschiedete Runde hier noch nicht — sie kommt mit
    ``wieder_im_dienst`` zurück, wenn der Satz zugestellt ist. Ein Satz, den niemand liest,
    weil das Senden scheiterte, ist keine Offenlegung.
    """
    wartet = lebenszyklus.rueckkehr(config, str(guild_id))
    if wartet is None:
        return Begruessung(text=WILLKOMMEN)
    return Begruessung(text=WILLKOMMEN_ZURUECK.format(name=wartet.name), wartet=wartet)


def wieder_im_dienst(config: Config, runde: Runde) -> Runde | None:
    """Zurück in den Dienst — erst jetzt, mit der Offenlegung nachweislich zugestellt."""
    return lebenszyklus.freigeben(config.database_path, runde)


def loeschfrage() -> str:
    return f"{LOESCHEN_FRAGE}\n\n{ABSCHIED.format(tage=lebenszyklus.FRIST_TAGE)}"


def geloescht(config: Config, runde: Runde, *, veranlasst_von: str | None = None) -> str:
    lebenszyklus.loeschen(config, runde, veranlasst_von=veranlasst_von)
    return LOESCHEN_FERTIG


def verabschieden(database_path: Path, guild_id: str) -> Runde | None:
    return lebenszyklus.sperren(database_path, str(guild_id))
