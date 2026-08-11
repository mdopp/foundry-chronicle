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

Diese Datei kennt Discord nicht. Sie bekommt eine Gilde-Kennung und vier Texte und gibt
Sätze zurück; wer daraus Fenster, Menüs und Knöpfe baut, entscheidet ``gateway.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chronicle import lebenszyklus, settings
from chronicle.config import Config
from chronicle.runde import Runde

# -- Einladen ---------------------------------------------------------------------------

# Der Satz, für den es dieses Modul gibt. Er steht in **jeder** Begrüßung, auch in der an
# eine Gruppe, die zurückkommt: eine Runde entscheidet sonst über ihre Sitzungsprotokolle,
# ohne zu wissen, worüber sie entscheidet.
OFFENLEGUNG = (
    "**Damit ihr es wisst: ich laufe auf einem Rechner, der jemand anderem gehört.** Wer "
    "ihn betreibt, kommt an alles heran, was ihr hier ablegt — eure Notizen, das "
    "Gesprochene, eure Chroniken. Ich verspreche euch keine Vertraulichkeit, die ich "
    "nicht halten kann. Wenn das für eure Runde nicht in Ordnung ist, werft mich wieder "
    "hinaus."
)

WILLKOMMEN = (
    "Hallo. Ich schreibe eure Sitzungen mit: aus euren Notizen, aus dem, was im "
    "Sprachkanal gesprochen wird, und aus den Würfen in eurem Foundry mache ich eine "
    "lesbare Chronik.\n"
    "\n"
    f"{OFFENLEGUNG}\n"
    "\n"
    "So fangt ihr an: **`/setup`** — dort tragt ihr ein, wo euer Foundry steht, unter "
    "welchem Namen ich mich dort anmelde und in welchen Kanal ich die fertige Chronik "
    "lege. Danach beginnt `/chronik start` die erste Sitzung, und `/aufnahme hilfe` zeigt "
    "den Rest. Was von euch hier liegt, könnt ihr jederzeit selbst löschen: "
    "`/chronik loeschen`."
)

WILLKOMMEN_ZURUECK = (
    "Da bin ich wieder. Die Runde »{name}« ist noch vollständig da — alles, was ihr "
    "geschrieben habt, steht wieder bereit, und `/chronik start` beginnt die nächste "
    "Sitzung.\n"
    "\n"
    f"{OFFENLEGUNG}"
)

RUNDE_OHNE_NAMEN = "Neue Runde"

NUR_IM_SERVER = (
    "Das geht nur auf dem Server, für den ich schreiben soll — hier im Zwiegespräch weiß "
    "ich nicht, welche Runde du meinst."
)

# Die beiden Absagen an ein Mitglied ohne Recht. Sie sagen den Grund, nicht bloß »nein«:
# hinter beiden steht eine Gefahr, die man kennen sollte, auch wenn man sie nicht auslösen
# darf.
NUR_VERWALTUNG = (
    "Einrichten darf, wer diesen Server verwaltet. Hier steht, wo euer Foundry läuft — und "
    "dorthin zeige ich später das Passwort eurer Spielleitung vor. Bitte jemanden mit dem "
    "Recht »Server verwalten«, das zu tun."
)

NUR_ADMIN = (
    "Löschen darf, wer diesen Server als Administrator führt. Es gibt keine Sicherung, aus "
    "der ich eine Chronik zurückhole; deshalb liegt das nicht bei jedem Mitglied."
)

# -- Einrichten -------------------------------------------------------------------------

SETUP_TITEL = "Runde einrichten"

FELD_ADRESSE = "Adresse eures Foundry"
FELD_BENUTZER = "Name, unter dem ich mich anmelde"
FELD_MODELL = "Modell für die Chronik"
FELD_UHRZEIT = "Uhrzeit des nächtlichen Laufs"

HINWEIS_ADRESSE = "z. B. https://foundry.example"
HINWEIS_BENUTZER = "ein eigenes Konto genügt, ohne Spielleitungsrechte"
HINWEIS_MODELL = "leer lassen, wenn du nicht weißt, was hier hingehört"
HINWEIS_UHRZEIT = "leer lassen für 04:00"

# Ein leeres Feld heißt hier dasselbe wie überall sonst: unverändert. Sonst löschte ein
# zweiter Aufruf, der nur den Kanal ändern soll, die Adresse gleich mit.
LEER_BLEIBT = "Was du leer lässt, bleibt, wie es war."

KEIN_PASSWORT = (
    "Nach dem Passwort frage ich hier nicht. Es kommt am Ende der Sitzung, wird einmal "
    "benutzt und danach vergessen — abgelegt wird es nirgends."
)

EINGERICHTET = "Die Runde »{name}« steht."
UEBERNOMMEN = "Die Runde »{name}« ist aktualisiert."

KANAL_FRAGE = "Und wohin mit der fertigen Chronik?"
KANAL_WAEHLEN = "Kanal für die Chronik"
KANAL_GESETZT = "Die Chronik kommt künftig nach {kanal}."
KANAL_KEINER = "Kein Kanal — dann lege ich die Chronik im Thread der Sitzung ab."
KANAL_OHNE = "keiner — im Thread der Sitzung ablegen"

# Der Wert, mit dem ein Auswahlmenü »keiner« sagt — leer darf eine Option nicht sein.
OHNE_KANAL = "-"

# Ein Auswahlmenü fasst fünfundzwanzig Zeilen, die für »keiner« eingerechnet. Wer mehr
# Kanäle hat, wählt einen der ersten — oder ruft den Befehl im gewünschten Kanal auf.
KANAL_GRENZE = 25

# Der eine Weg zurück in den Dienst, auf dem keine Begrüßung steht: fehlt dem Bot in der
# Gilde jeder beschreibbare Kanal, hätte die Gruppe die Offenlegung nie gelesen. Dann
# bleibt die Runde still, und das wird gesagt statt verschwiegen.
STILL_GEBLIEBEN = (
    "Die Offenlegung konnte ich hier in keinen Kanal schreiben — deshalb bleibt die Runde "
    "still. Gib mir Schreibrecht in diesem Kanal und ruf `/setup` noch einmal auf."
)

FEHLT = "Es fehlt noch: {was}. Ruf `/setup` noch einmal auf, wenn du es nachtragen willst."
STEHT_BEREIT = "Weiter geht es mit `/chronik start` — das legt die erste Sitzung an."

UHRZEIT_UNLESBAR = "Mit »{wert}« kann ich nichts anfangen — ich bleibe bei {uhrzeit} Uhr."

# -- Verabschieden ----------------------------------------------------------------------

LOESCHEN_FRAGE = (
    "**Das verschwindet, endgültig und sofort:**\n"
    "• alle Sitzungen dieser Runde mit ihren Szenen und Notizen\n"
    "• alle Diktate und Aufnahmen aus dem Sprachkanal — auch die Tondateien\n"
    "• alle daraus geschriebenen Texte: Chroniken und Rückblicke\n"
    "• das Register mit Figuren, Orten und Handlungsfäden\n"
    "• wer von euch welche Foundry-Figur spielt\n"
    "• die Nachweise über die Ansagen im Sprachkanal\n"
    "• die Zahlen, die ich aus eurem Foundry geholt habe\n"
    "\n"
    "**Das bleibt:** was ich euch schon zugestellt habe — Chroniken und Rückblicke in "
    "euren Kanälen und Threads — liegt weiter in Discord; dorthin reicht mein Löschen "
    "nicht. Mit den Nachweisen geht also der Beleg, dass im Sprachkanal angesagt wurde, "
    "während das daraus Geschriebene bei euch stehen bleibt. Wer den Beleg braucht, holt "
    "ihn sich vorher.\n"
    "\n"
    "Es gibt keine Sicherung, aus der ich das zurückhole. Was ihr behalten wollt, ladet "
    "vorher herunter."
)

LOESCHEN_JA = "Ja, alles löschen"
LOESCHEN_NEIN = "Abbrechen"

LOESCHEN_FERTIG = "Fort. Von dieser Runde liegt hier nichts mehr."
LOESCHEN_ABGEBROCHEN = "Nichts gelöscht. Es bleibt alles, wie es war."

# Ein Knopf lebt eine Viertelstunde; in der Zeit kann die Runde gelöscht und die Kennung
# neu vergeben worden sein. Dann wird nicht gelöscht, sondern gefragt.
LOESCHEN_VERALTET = (
    "Diese Frage ist von vorhin, und seither hat sich hier etwas geändert. Ich habe nichts "
    "gelöscht — ruf `/chronik loeschen` noch einmal auf, wenn du es immer noch willst."
)

# Was beim Rauswurf passiert — gesagt wird es vorher, in der Einladung und beim Löschen,
# denn danach ist der Bot nicht mehr da, um es zu sagen.
ABSCHIED = (
    "Wirft ihr mich hinaus, ist die Runde sofort still und nach {tage} Tagen gelöscht. "
    "Holt ihr mich vorher zurück, ist alles wieder da; danach ist es fort."
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


def _uhrzeit(runde: Runde, wert: str) -> str:
    if not wert.strip():
        return ""
    if settings.save_nightly_time(runde, wert):
        return ""
    return UHRZEIT_UNLESBAR.format(wert=wert.strip(), uhrzeit=settings.nightly_time(runde))


def _offen(config: Config, runde: Runde) -> str:
    fehlend = settings.effective(config, runde).missing_foundry_fields
    return FEHLT.format(was=" und ".join(fehlend)) if fehlend else STEHT_BEREIT


def einrichten(
    config: Config,
    guild_id: str,
    gildenname: str,
    *,
    adresse: str = "",
    benutzer: str = "",
    modell: str = "",
    uhrzeit: str = "",
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
    werte = {
        "foundry_url": adresse.strip(),
        "foundry_user": benutzer.strip(),
        "ollama_model": modell.strip(),
    }
    settings.save(runde, {name: wert for name, wert in werte.items() if wert})
    saetze = (
        [EINGERICHTET.format(name=runde.name), KEIN_PASSWORT]
        if beansprucht.neu
        else [UEBERNOMMEN.format(name=runde.name), LEER_BLEIBT]
    )
    stolperte = _uhrzeit(runde, uhrzeit)
    if stolperte:
        saetze.append(stolperte)
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


def wieder_im_dienst(config: Config, runde: Runde) -> Runde:
    """Zurück in den Dienst — erst jetzt, mit der Offenlegung nachweislich zugestellt."""
    return lebenszyklus.freigeben(config.database_path, runde)


def loeschfrage() -> str:
    return f"{LOESCHEN_FRAGE}\n\n{ABSCHIED.format(tage=lebenszyklus.FRIST_TAGE)}"


def geloescht(config: Config, runde: Runde, *, veranlasst_von: str | None = None) -> str:
    lebenszyklus.loeschen(config, runde, veranlasst_von=veranlasst_von)
    return LOESCHEN_FERTIG


def verabschieden(database_path: Path, guild_id: str) -> Runde | None:
    return lebenszyklus.sperren(database_path, str(guild_id))
