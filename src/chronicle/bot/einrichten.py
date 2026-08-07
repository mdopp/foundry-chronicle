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
  danach kommt ein Knopf — kein Befehl, der beim Vertippen eine Kampagne mitnimmt.

Diese Datei kennt Discord nicht. Sie bekommt eine Gilde-Kennung und vier Texte und gibt
Sätze zurück; wer daraus Fenster, Menüs und Knöpfe baut, entscheidet ``gateway.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chronicle import lebenszyklus, settings
from chronicle import runde as runden
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
    "Es gibt keine Sicherung, aus der ich das zurückhole. Was ihr behalten wollt, ladet "
    "vorher herunter."
)

LOESCHEN_JA = "Ja, alles löschen"
LOESCHEN_NEIN = "Abbrechen"

LOESCHEN_FERTIG = "Fort. Von dieser Runde liegt hier nichts mehr."
LOESCHEN_ABGEBROCHEN = "Nichts gelöscht. Es bleibt alles, wie es war."

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
    """
    database_path = config.database_path
    neu = runden.fuer_gilde(database_path, str(guild_id)) is None
    runde = lebenszyklus.beanspruchen(database_path, guild_id, gildenname)
    werte = {
        "foundry_url": adresse.strip(),
        "foundry_user": benutzer.strip(),
        "ollama_model": modell.strip(),
    }
    settings.save(runde, {name: wert for name, wert in werte.items() if wert})
    saetze = (
        [EINGERICHTET.format(name=runde.name), KEIN_PASSWORT]
        if neu
        else [UEBERNOMMEN.format(name=runde.name), LEER_BLEIBT]
    )
    stolperte = _uhrzeit(runde, uhrzeit)
    if stolperte:
        saetze.append(stolperte)
    saetze.append(_offen(config, runde))
    return Eingerichtet(runde=runde, neu=neu, meldung=" ".join(saetze))


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


def begruessung(database_path: Path, guild_id: str) -> str:
    """Der eine Satz beim Betreten — und die Rückkehr, wenn die Frist noch läuft.

    Angelegt wird hier nichts: eine Runde entsteht erst, wenn jemand sie einrichtet. Eine
    verabschiedete dagegen wird sofort wieder freigegeben — sonst stünde der Bot in der
    Gilde und die Chronik bliebe stumm.
    """
    zurueck = lebenszyklus.wiedereinladung(database_path, str(guild_id))
    if zurueck is None:
        return WILLKOMMEN
    return WILLKOMMEN_ZURUECK.format(name=zurueck.name)


def loeschfrage() -> str:
    return f"{LOESCHEN_FRAGE}\n\n{ABSCHIED.format(tage=lebenszyklus.FRIST_TAGE)}"


def geloescht(config: Config, runde: Runde) -> str:
    lebenszyklus.loeschen(config, runde)
    return LOESCHEN_FERTIG


def verabschieden(database_path: Path, guild_id: str) -> Runde | None:
    return lebenszyklus.sperren(database_path, str(guild_id))
