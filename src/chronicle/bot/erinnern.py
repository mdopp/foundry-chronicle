"""Erinnern per Befehl: suchen, nachschlagen, bestätigen, zuordnen.

Die Erinnern-Schicht hing bisher an Formularen. In Discord wird sie nicht nur anders,
sondern besser: gefragt wird dort, wo gespielt wird, ein Treffer springt in die Nachricht
zurück, aus der er stammt, und ein Vorschlag fürs Register braucht einen Knopf statt einer
Liste mit Ja und Nein je Zeile.

Diese Datei kennt Discord nicht. Sie bekommt eine Runde, einen Namen oder eine Kennung und
gibt Sätze und die Bausteine eines Embeds zurück; wer daraus Knöpfe und Auswahlmenüs baut,
entscheidet ``gateway.py``.

Drei Regeln stehen über allem:

* **Alles bleibt in der Runde.** Gesucht, nachgeschlagen und zugeordnet wird ausschließlich
  in der Runde der fragenden Gilde. Eine Antwort, die eine fremde Kampagne streift, ist ein
  Leck und kein Suchtreffer.
* **Ein Vorschlag wird nie von allein zur Tatsache.** Bestätigt wird per Knopf, verworfen
  ebenso — und was schon entschieden ist, wird als entschieden gemeldet, statt beim zweiten
  Klick zu scheitern. Dieselbe Antwort bekommt eine Ansicht, die noch von vorgestern offen
  im Kanal steht.
* **Wer entscheiden darf, regelt Discord.** Kanal- und Rollenrechte bestimmen das; ein
  eigenes Rollenmodell gibt es nicht mehr.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches

from chronicle import people, register, search
from chronicle.discord import grenzen
from chronicle.runde import Runde

# Gesucht wird breit, gezeigt wird schmal: die Gruppen sollen nebeneinander lesbar bleiben.
TREFFER = 30
PRO_GRUPPE = 3

# Discord lässt fünf Reihen je Nachricht zu, und eine Reihe fasst fünf Knöpfe: Name, drei
# Arten, ein Nein. Mehr als fünf Einträge passen deshalb nicht auf eine Ansicht — der Rest
# kommt beim nächsten Aufruf, und das steht auch da.
PRO_SEITE = 5

# Und ein Auswahlmenü fasst fünfundzwanzig Zeilen, die Zeile für »niemand« eingerechnet.
OPTIONEN_GRENZE = 25

# Nach dieser Zeit hört eine Ansicht auf zuzuhören. Ein Knopf, der still ins Leere führt,
# ist schlechter als einer, der gar nicht mehr da ist — der Befehl holt sie neu.
FRIST = 900

# Die Beschriftung eines Knopfes und der Platzhalter eines Auswahlmenüs sind bei Discord
# begrenzt; ein langer Name wird gekürzt statt abgewiesen.
KNOPF_GRENZE = 80
PLATZHALTER_GRENZE = 150
ZEILEN_GRENZE = 300

# Und der Wert eines Embed-Feldes ebenso: dreißig Erwähnungen sind eine Kampagne von zwei
# Jahren, und ein Feld darüber lässt Discord die **ganze** Nachricht fallen. Geteilt werden
# kann ein Embed nicht, also wird hier gekappt — das Register selbst bleibt vollständig.
FELD_GEKUERZT = "\n… und weitere."

# Ab hier ist ein Name nah genug an einem Registereintrag, um ihn als Rückfrage anzubieten.
NAEHE = 0.6

# Der Wert, mit dem ein Auswahlmenü »niemand« sagt — leer darf eine Option nicht sein.
KEINE = "-"

VERWERFEN = ""

# Die Knöpfe je Vorschlag, in der Reihenfolge, in der sie stehen sollen.
ENTSCHEIDUNGEN = (
    (register.FIGUR, "Figur"),
    (register.ORT, "Ort"),
    (register.FADEN, "Faden"),
    (VERWERFEN, "Nein"),
)

SUCHE_TITEL = "Gefunden zu »{begriff}«"
OHNE_BEGRIFF = "Sag mir, wonach ich suchen soll — ein Wort genügt."
NOCH_NICHTS = (
    "Hier ist noch nichts zu finden. Sobald die erste Sitzung geschrieben ist, "
    "durchsuche ich Notizen, Diktate, Chroniken und das Register."
)
NICHTS_GEFUNDEN = (
    "Nichts zu »{begriff}«. Ein kürzeres Wort oder eine andere Schreibweise hilft oft — "
    "ich suche auch nach Wortanfängen."
)
WEITERE_TREFFER = "Es gibt mehr; genauer gefragt kommst du schneller hin."
SPRUNG = "hin"

WER_UNBEKANNT = "»{name}« steht nicht im Register."
WER_AEHNLICH = " Meintest du {namen}?"
WER_NACHSCHLAG = (
    " Wartet der Eintrag noch auf ein Ja, findest du ihn unter den offenen Vorschlägen."
)
WER_ART = "Art"
WER_ERWAEHNT = "Kommt vor in"
WER_FIGUR = "Figur in Foundry"
WER_SITZUNG = "Sitzung vom {datum}"
WER_OHNE_ERWAEHNUNG = "noch keiner Sitzung zugeordnet"

OFFEN_TITEL = "Vorschläge fürs Register"
OFFEN_HINWEIS = (
    "Je Vorschlag eine Reihe: die Art bestätigt ihn, »Nein« verwirft ihn. "
    "Die Reihen stehen in derselben Reihenfolge wie die Einträge."
)
NICHTS_OFFEN = "Nichts zu entscheiden — das Register ist auf dem Stand der letzten Chronik."
OFFEN_WEITERE = "Danach warten noch {anzahl} weitere."
OFFEN_VORGESCHLAGEN = "vorgeschlagen als {art}"
BESTAETIGT = "»{name}« steht jetzt als {art} im Register."
VERWORFEN = "»{name}« ist verworfen."
SCHON_ENTSCHIEDEN = "Darüber ist schon entschieden — ich habe nichts geändert."
SCHON_IM_REGISTER = (
    "»{name}« steht schon als {art} im Register — den doppelten Vorschlag habe ich weggeräumt."
)
NICHT_EINGETRAGEN = (
    "»{name}« als {art} einzutragen ging nicht: der Name ist unter dieser Art vergeben. "
    "Der Vorschlag steht weiter offen — »Nein« räumt ihn weg."
)

ZUORDNUNG_TITEL = "Wer spricht als wer"
ZUORDNUNG_HINWEIS = (
    "Je Person ein Menü. Die Zuordnung entscheidet, welcher Name später an einer "
    "Tonspur steht — sie bleibt, bis du sie änderst."
)
NIEMAND_AUFGENOMMEN = (
    "Ich habe noch niemanden aufgenommen. Nach der ersten Aufnahme steht hier, wer zu wem gehört."
)
KEINE_SPIELER = (
    "Ich kenne die Foundry-Spieler dieser Runde noch nicht. Sobald eine Sitzung "
    "abgeschlossen ist, stehen sie hier zur Wahl."
)
ZUORDNUNG_WAEHLEN = "{name} spielt …"
ZUORDNUNG_KEINE = "— niemand —"
ZUORDNUNG_OFFEN = "noch offen"
ZUORDNUNG_VORSCHLAG = "vielleicht {spieler}"
ZUGEORDNET = "{name} spielt als {spieler}."
ZUORDNUNG_GELOEST = "Die Zuordnung von {name} ist aufgehoben."
ZU_VIELE_SPIELER = (
    "Auf dem Foundry-Server stehen mehr Konten, als in ein Menü passen — "
    "zur Wahl stehen die ersten {anzahl}."
)
NICHT_AUFGENOMMEN = "Diese Person habe ich nicht aufgenommen — ich habe nichts geändert."
SPIELER_WEG = "Diesen Foundry-Spieler kenne ich nicht mehr — ich habe nichts geändert."
SPIELER_VERGEBEN = (
    "Dieses Foundry-Konto gehört schon jemand anderem — ich habe nichts geändert. "
    "Soll es wechseln, geht das in `/zuordnung`: dort darf ein Konto umgehängt werden, "
    "und die Runde erfährt es dann im Thread."
)
UEBERNOMMEN = (
    "{name} spielt als {spieler}. Das Konto hatte {vorher} — dort steht jetzt wieder nichts."
)

# Und dieselbe Übernahme, gesagt statt nur beantwortet. Die Antwort auf den Klick ist
# ephemer und `/zuordnung` zeigt nur ``PRO_SEITE`` Personen — ab der sechsten stünde die
# Vorbesitzerin weder vorher noch nachher darin. Der Schritt mit der größten Folge wäre
# damit der stillste; deshalb der Thread für die Runde und ein Wort an sie selbst
# (Betreiber-Entscheidung vom 2026-08-12 zu #76).
UEBERNAHME_VERMERK = (
    "**{name}** spielt in eurem Foundry als **{spieler}** — bis eben gehörte das Konto "
    "**{vorher}**, dort steht jetzt wieder nichts. Umgehängt wurde es in `/zuordnung`; "
    "dort geht es auch wieder zurück."
)

# Auch dieser Satz geht ins Zwiegespräch und nennt deshalb die **Runde** — dieselbe Regel
# wie bei ``BETRETEN_FRAGE``: eine Instanz trägt mehrere (#62/#63), und weder eine
# Direktnachricht noch der Verweis auf `/zuordnung` sagt sonst, welche gemeint ist.
UEBERNAHME_ANGESAGT = (
    "In **{runde}** hat **{name}** deine Zuordnung zum Foundry-Konto **{spieler}** "
    "übernommen — deine Spuren dort tragen ab jetzt wieder deinen Discord-Namen. War das "
    "ein Versehen, holt `/zuordnung` in dieser Runde es zurück."
)

# Gefragt wird beim Betreten des Sprachkanals, damit jede Äußerung von Anfang an einer
# Figur gehört (#76). Der Satz geht ins Zwiegespräch und nennt deshalb den Sprachkanal:
# in einer Direktnachricht steht sonst nirgends, welche Runde gemeint ist.
BETRETEN_FRAGE = (
    "Ich schneide gerade in **#{kanal}** mit und weiß noch nicht, wer du dort in Foundry "
    "bist. Wähl dich aus — dann steht dein Name später an deiner Spur statt deines "
    "Discord-Namens. Antwortest du nicht, bleibt es beim Discord-Namen; das Protokoll "
    "sagt es dann auch so. Ändern kannst du es jederzeit mit `/zuordnung`."
)

# Und der eine Fall, in dem nicht gefragt wird: der Name ist 1:1 derselbe. Gesagt wird es
# trotzdem — eine Zuordnung, die von selbst entstanden ist und die niemand sieht, ist
# genau die stillschweigend übernommene Vermutung, gegen die es die Bestätigung gibt.
#
# Zwei Sätze, weil ``people.getroffen`` zwei Antworten hat: der Kontoname oder der einer
# seiner Figuren. Einen Satz für beides gibt es nicht — er nennte im Fall der Figur einen
# Namen, der im selben Satz sichtbar nicht der gleiche ist.
BETRETEN_VERMERK = (
    "**{name}** heißt hier genauso wie das Foundry-Konto **{spieler}** — ich habe beide "
    "verbunden, damit die Spur von Anfang an den richtigen Namen trägt. Stimmt das nicht, "
    "ändert `/zuordnung` es."
)
BETRETEN_VERMERK_FIGUR = (
    "**{name}** heißt hier genauso wie **{figur}**, die Figur des Foundry-Kontos "
    "**{spieler}** — ich habe beide verbunden, damit die Spur von Anfang an den richtigen "
    "Namen trägt. Stimmt das nicht, ändert `/zuordnung` es."
)

# Der Weg über das Menü im Zwiegespräch: jemand wählt sich ein Konto. Er wird genauso
# vermerkt wie die Namensgleichheit — je schwächer der Beleg, desto mehr Tageslicht
# (Betreiber-Entscheidung vom 2026-08-12 zu #76). Der Satz sagt, wie es zustande kam, und
# **nichts über die Namen**: ins Menü führt auch die Mehrdeutigkeit, und dort ist der Name
# gerade derselbe — nur eben nicht nur bei einer.
MENUE_VERMERK = (
    "**{name}** spielt in eurem Foundry als **{spieler}** — so gewählt, nicht erkannt: "
    "ich habe gefragt, und das ist die Antwort."
)

NUR_SELBST = (
    "Diese Frage galt jemand anderem — wer wer ist, entscheidet jede und jeder über sich "
    "selbst. Ich habe nichts geändert."
)


@dataclass(frozen=True)
class Antwort:
    """Was der Bot sagt: ein Satz, ein Embed oder beides."""

    text: str = ""
    embed: dict | None = None


@dataclass(frozen=True)
class Offen:
    antwort: Antwort
    eintraege: tuple[register.Entry, ...] = ()


@dataclass(frozen=True)
class Zuordnung:
    antwort: Antwort
    personen: tuple[people.Person, ...] = ()
    spieler: tuple[people.Spieler, ...] = ()


@dataclass(frozen=True)
class Zugeordnet:
    """Was ``zuordnen`` getan hat: der Satz dazu und das Konto, das jetzt festgeschrieben ist.

    ``spieler`` bleibt ``None``, wenn nichts geschrieben wurde — abgewiesen, oder
    zurückgenommen. Der Unterschied ist nicht Zierde: gesagt wird nur, was auch entstanden
    ist, sonst stünde im Thread eine Verbindung, die es nicht gibt.

    ``wer`` und ``vorher`` stehen zusammen da, wo das Konto einer **anderen** abgenommen
    wurde. Wer die war, weiß nur dieser Aufruf: danach trägt die Zuordnung genau eine Zeile,
    und die alte ist fort. Ohne die beiden ließe sich die Übernahme weder vermerken noch der
    Betroffenen sagen.
    """

    satz: str
    spieler: people.Spieler | None = None
    wer: people.Person | None = None
    vorher: people.Person | None = None


@dataclass(frozen=True)
class Betreten:
    """Was beim Betreten des Sprachkanals mit **einer** Person zu geschehen ist.

    Leer heißt: nichts. Mit ``automatisch`` ist sie zuzuordnen und ``vermerk`` sagt es der
    Runde; sonst steht die Frage aus und ``spieler`` sind die Zeilen ihres Menüs.

    Eine Entscheidung, kein Vollzug: geschrieben hat das hier noch nichts. Wer beides tut —
    ``zuordnen`` und den Vermerk hinausgeben —, tut es in **dieser** Reihenfolge: erst
    schreiben, dann sagen. Über SQLite und Discord hinweg gibt es kein gemeinsames
    Zusammenschreiben, also entscheidet, welcher Fehlerfall der erträglichere ist, und das
    ist **Schweigen** und nicht eine Lüge im Thread. Was der Vermerk nicht erreicht, nimmt
    ``zuruecknehmen`` wieder zurück. Scheitert **auch** die Rücknahme, steht eine wahre
    Zuordnung ohne Ansage da — selten, aber echt, und nachgeholt wird nichts: ein Satz, der
    Stunden später eine Verbindung von vorhin behauptet, wäre die teurere Erfindung.
    """

    person: people.Person | None = None
    automatisch: people.Spieler | None = None
    spieler: tuple[people.Spieler, ...] = ()
    vermerk: str = ""


def gekuerzt(text: str, grenze: int) -> str:
    sauber = " ".join(text.split())
    return sauber if len(sauber) <= grenze else sauber[: grenze - 1].rstrip() + "…"


def _mit(meldung: str, satz: str) -> str:
    return f"{meldung} {satz}".strip()


def _beschreibung(meldung: str, hinweis: str, zeilen: list[str]) -> str:
    """Meldung, Hinweis, Leerzeile, Liste — die Meldung fällt weg, wenn es keine gibt."""
    kopf = [teil for teil in (meldung, hinweis) if teil]
    return "\n".join([*kopf, "", *zeilen])


def _hervorgehoben(hit: search.Hit) -> str:
    text = hit.raw.replace(search.AUF, "**").replace(search.ZU, "**")
    return gekuerzt(text, ZEILEN_GRENZE)


def _sitzungsname(hit: search.Hit) -> str:
    titel = (hit.title or "").strip()
    return titel or WER_SITZUNG.format(datum=hit.played_on)


def _sprung(runde: Runde, thread_id: str | None, message_id: str | None = None) -> str | None:
    """Der Weg zurück an die Stelle — solange es eine gibt.

    Eine Sitzung aus der Zeit vor dem Sitzungs-Thread hat keine; dann steht der Treffer
    ohne Verweis da, statt hinter einem Verweis ins Leere.
    """
    if not runde.guild_id or not thread_id:
        return None
    ziel = f"https://discord.com/channels/{runde.guild_id}/{thread_id}"
    return f"{ziel}/{message_id}" if message_id else ziel


def _trefferzeile(runde: Runde, hit: search.Hit) -> str:
    ziel = _sprung(runde, hit.thread_id, hit.message_id)
    kopf = _sitzungsname(hit)
    verweis = f" [{SPRUNG}]({ziel})" if ziel else ""
    return f"**{kopf}** — {_hervorgehoben(hit)}{verweis}"


def suche(runde: Runde, begriff: str) -> Antwort:
    """Die Volltextsuche als Embed: je Art eine Gruppe, jeder Treffer mit seinem Sprung."""
    ergebnis = search.find(runde, begriff, limit=TREFFER)
    if not ergebnis.query:
        return Antwort(text=OHNE_BEGRIFF)
    if not ergebnis.indexed:
        return Antwort(text=NOCH_NICHTS)
    if not ergebnis.groups:
        return Antwort(text=NICHTS_GEFUNDEN.format(begriff=ergebnis.query))
    felder = []
    gekappt = False
    for gruppe in ergebnis.groups:
        gezeigt = gruppe.hits[:PRO_GRUPPE]
        gekappt = gekappt or len(gezeigt) < len(gruppe.hits)
        felder.append(
            {
                "name": gruppe.label,
                "value": grenzen.zeilenweise(
                    [_trefferzeile(runde, hit) for hit in gezeigt],
                    grenzen.EMBED_FELD,
                    FELD_GEKUERZT,
                ),
            }
        )
    gebaut: dict = {"title": SUCHE_TITEL.format(begriff=ergebnis.query), "fields": felder}
    if gekappt:
        gebaut["footer"] = {"text": WEITERE_TREFFER}
    return Antwort(embed=gebaut)


def _erwaehnungen(runde: Runde, eintrag: register.Entry) -> str:
    zeilen = []
    for erwaehnung in eintrag.mentions:
        ziel = _sprung(runde, erwaehnung.thread_id)
        name = erwaehnung.title or WER_SITZUNG.format(datum=erwaehnung.played_on)
        zeilen.append(f"[{name}]({ziel})" if ziel else name)
    if not zeilen:
        return WER_OHNE_ERWAEHNUNG
    return grenzen.zeilenweise(zeilen, grenzen.EMBED_FELD, FELD_GEKUERZT)


def wer(runde: Runde, name: str) -> Antwort:
    """Der Registereintrag zu einem Namen — oder eine ehrliche Rückfrage."""
    eintraege = [eintrag for gruppe in register.overview(runde) for eintrag in gruppe.entries]
    gesucht = name.strip().casefold()
    for eintrag in eintraege:
        if eintrag.name.casefold() == gesucht:
            felder = [
                {"name": WER_ART, "value": eintrag.label, "inline": True},
                {"name": WER_ERWAEHNT, "value": _erwaehnungen(runde, eintrag)},
            ]
            if eintrag.actor_name:
                felder.insert(1, {"name": WER_FIGUR, "value": eintrag.actor_name, "inline": True})
            return Antwort(
                embed={
                    "title": grenzen.gekappt(eintrag.name, grenzen.EMBED_TITEL),
                    "description": grenzen.gekappt(eintrag.description, grenzen.EMBED_TEXT),
                    "fields": felder,
                }
            )
    nach_name = {eintrag.name.casefold(): eintrag.name for eintrag in eintraege}
    nah = get_close_matches(gesucht, list(nach_name), n=3, cutoff=NAEHE)
    satz = WER_UNBEKANNT.format(name=name.strip())
    if nah:
        return Antwort(text=satz + WER_AEHNLICH.format(namen=", ".join(nach_name[k] for k in nah)))
    return Antwort(text=satz + WER_NACHSCHLAG)


def offen(runde: Runde, *, meldung: str = "") -> Offen:
    """Was auf ein Ja oder Nein wartet — eine Seite voll, der Rest beim nächsten Mal."""
    wartend = register.pending(runde)
    gezeigt = wartend[:PRO_SEITE]
    if not gezeigt:
        return Offen(antwort=Antwort(text=_mit(meldung, NICHTS_OFFEN)))
    zeilen = [
        f"**{eintrag.name}** — {eintrag.description} "
        f"_{OFFEN_VORGESCHLAGEN.format(art=eintrag.label)}_"
        for eintrag in gezeigt
    ]
    gebaut: dict = {
        "title": OFFEN_TITEL,
        "description": _beschreibung(meldung, OFFEN_HINWEIS, zeilen),
    }
    if len(wartend) > len(gezeigt):
        gebaut["footer"] = {"text": OFFEN_WEITERE.format(anzahl=len(wartend) - len(gezeigt))}
    return Offen(antwort=Antwort(embed=gebaut), eintraege=gezeigt)


def entscheiden(runde: Runde, eintrag_id: int, art: str) -> str:
    """Ein Knopf, eine Entscheidung — und beim zweiten Klick eine ehrliche Auskunft.

    Gesagt wird, was ``decide`` *getan* hat, nicht was geklickt wurde: derselbe Name unter
    zwei Arten ist ein alltäglicher Vorschlag des Modells, und die Bestätigung der zweiten
    Zeile lief vorher ins Leere, während die Antwort »steht jetzt im Register« behauptete
    (#183).
    """
    wartend = {eintrag.id: eintrag for eintrag in register.pending(runde)}
    eintrag = wartend.get(eintrag_id)
    if eintrag is None:
        return SCHON_ENTSCHIEDEN
    ergebnis = register.decide(runde, {eintrag_id: register.Entscheidung(ja=bool(art), kind=art)})
    stand = ergebnis[eintrag_id]
    label = register.EINZAHL.get(art, art)
    if stand == register.UNVERAENDERT:
        if not art:
            return SCHON_ENTSCHIEDEN
        return NICHT_EINGETRAGEN.format(name=eintrag.name, art=label)
    if not art:
        return VERWORFEN.format(name=eintrag.name)
    if stand == register.DOPPELT:
        return SCHON_IM_REGISTER.format(name=eintrag.name, art=label)
    return BESTAETIGT.format(name=eintrag.name, art=label)


def _zuordnungszeile(person: people.Person) -> str:
    if person.confirmed is not None:
        figuren = ", ".join(person.confirmed.characters)
        stand = f"{person.confirmed.name} — {figuren}" if figuren else person.confirmed.name
    elif person.suggestion is not None:
        stand = ZUORDNUNG_VORSCHLAG.format(spieler=person.suggestion.name)
    else:
        stand = ZUORDNUNG_OFFEN
    return f"**{person.discord_name}** — {stand}"


def zuordnung(runde: Runde, *, meldung: str = "") -> Zuordnung:
    """Wer aufgenommen wurde, gegen die Foundry-Spieler dieser Runde."""
    stand = people.overview(runde)
    if not stand.personen:
        return Zuordnung(antwort=Antwort(text=_mit(meldung, NIEMAND_AUFGENOMMEN)))
    if not stand.spieler:
        return Zuordnung(antwort=Antwort(text=_mit(meldung, KEINE_SPIELER)))
    gezeigt = stand.personen[:PRO_SEITE]
    zeilen = [_zuordnungszeile(person) for person in gezeigt]
    gebaut: dict = {
        "title": ZUORDNUNG_TITEL,
        "description": _beschreibung(meldung, ZUORDNUNG_HINWEIS, zeilen),
    }
    hinweise = []
    if len(stand.personen) > len(gezeigt):
        hinweise.append(OFFEN_WEITERE.format(anzahl=len(stand.personen) - len(gezeigt)))
    if len(stand.spieler) >= OPTIONEN_GRENZE:
        hinweise.append(ZU_VIELE_SPIELER.format(anzahl=OPTIONEN_GRENZE - 1))
    if hinweise:
        gebaut["footer"] = {"text": " ".join(hinweise)}
    return Zuordnung(antwort=Antwort(embed=gebaut), personen=gezeigt, spieler=stand.spieler)


def wahlmoeglichkeiten(
    person: people.Person, spieler: tuple[people.Spieler, ...]
) -> tuple[tuple[str, str, bool], ...]:
    """Beschriftung, Wert und Vorauswahl je Zeile eines Auswahlmenüs.

    Ein Foundry-Server trägt alle seine Konten in jede Welt, auch die fremder Runden; mehr
    als fünfundzwanzig davon nimmt ein Menü nicht an. Gekappt wird deshalb hier — das
    bereits Zugeordnete steht dabei immer mit drin, sonst nähme die Ansicht die Zuordnung
    beim ersten Blick zurück.
    """
    gewaehlt = person.confirmed.id if person.confirmed is not None else ""
    bevorzugt = [eintrag for eintrag in spieler if eintrag.id == gewaehlt]
    weitere = [eintrag for eintrag in spieler if eintrag.id != gewaehlt]
    zeilen = [(ZUORDNUNG_KEINE, KEINE, not gewaehlt)]
    zeilen.extend(
        (gekuerzt(eintrag.name, KNOPF_GRENZE), eintrag.id, eintrag.id == gewaehlt)
        for eintrag in (bevorzugt + weitere)[: OPTIONEN_GRENZE - 1]
    )
    return tuple(zeilen)


def _sonst_noch_so(stand: people.Uebersicht, person: people.Person, konto: people.Spieler) -> bool:
    """Ob außer ihr noch jemand einen Namen trägt, der genau dieses Konto trifft.

    Die Gegenrichtung der Gleichheit, und die Zugehörigkeitsprüfung, die es hier überhaupt
    geben kann: sitzt neben der echten Mira ein Gast, der ebenso heißt, ist die Gleichheit
    kein Beleg mehr, sondern eine Verwechslung mit Folgen — wer zuerst hereinkommt, nähme
    der anderen ihr Konto, und aus ``frei`` wäre es danach verschwunden. Dann wird gefragt,
    und zwar beide, und das Konto bleibt so lange zu haben.

    Dasselbe greift, wenn zwei Namen dasselbe Konto von zwei Seiten treffen — die eine heißt
    wie das Konto, die andere wie dessen Figur.

    Gefragt wird dabei das **ganze Einwilligungsprotokoll der Runde, und zwar für immer**:
    ``people.overview`` führt jede Person, die je aufgenommen wurde, nicht nur die von
    heute Abend. Ein Gast, der vor einem Jahr einmal dabei war und längst fort ist, schaltet
    den Zweig ohne Rückfrage für sein Namensdoppel dauerhaft ab — dann steht dort auf Dauer
    das Menü. Das ist Absicht und die konservative Richtung: es wird gefragt statt geraten,
    und der Preis ist ein Klick. Eng zu ziehen wäre es nur um den Preis, dass die Gleichheit
    wieder mehrdeutig wird, sobald jemand von damals zurückkommt.
    """
    return any(
        andere.discord_user_id != person.discord_user_id
        and people.genau(andere.discord_name, (konto,)) is not None
        for andere in stand.personen
    )


def _vermerk(name: str, konto: people.Spieler) -> str:
    """Der Satz an die Runde — mit dem Namen, der **wirklich** getroffen hat.

    ``people.genau`` trifft über den Kontonamen **oder** über den einer Figur. Immer den
    Kontonamen zu melden hieße im zweiten Fall: »**Aelin Sturmwind** heißt hier genauso wie
    **Mira**« — eine Behauptung, die der eigene Satz widerlegt.
    """
    figur = people.getroffen(name, konto)
    if figur == konto.name:
        return BETRETEN_VERMERK.format(name=name, spieler=konto.name)
    return BETRETEN_VERMERK_FIGUR.format(name=name, figur=figur, spieler=konto.name)


def betreten(runde: Runde, discord_user_id: str) -> Betreten:
    """Wer den Sprachkanal betritt: schon zugeordnet, zuzuordnen oder zu fragen.

    Der **1:1 gleiche Name** ist der eine Fall, der ohne Rückfrage auskommt (#76,
    Betreiber-Entscheidung vom 2026-08-12): er ist kein Vorschlag, sondern ein Beleg.
    Deshalb steht hier ``people.genau`` und nicht ``people.suggest`` — »Mira« neben »Mirah«
    ist keine Gleichheit, und wer »Miral« heißt, wird gefragt statt geraten.

    Drei Dinge machen aus der Gleichheit erst eine Antwort: sie muss **eindeutig** sein
    (zwei gleichnamige Konten sind keine), das Konto muss **frei** sein, und niemand sonst
    darf denselben Namen tragen. Sonst steht das Menü an — mit den freien Konten und keinem
    fremden darin.

    Die Eindeutigkeit wird gegen **alle** Konten der Runde geprüft und erst danach das
    Vergebene abgezogen; die umgekehrte Reihenfolge hebt die Mehrdeutigkeit auf, statt sie
    zu sehen. Heißen zwei Konten »Mira« und ist eines schon vergeben, bliebe unter den
    freien genau eines übrig — und geschrieben würde ohne Rückfrage, obwohl der Name in
    dieser Runde gerade **nicht** eindeutig ist. Dasselbe von zwei Seiten: ein vergebenes
    Konto »Mira« und ein freies mit der Figur »Mira«.

    Geschrieben wird hier nichts; das tut ``zuordnen``, und der Vermerk geht danach hinaus.

    Ohne freies Konto steht nichts zur Wahl. Dann wird auch nicht gefragt — eine Frage mit
    einem leeren Menü ist keine.
    """
    stand = people.overview(runde)
    person = next((wer for wer in stand.personen if wer.discord_user_id == discord_user_id), None)
    if person is None or person.confirmed is not None or not stand.frei:
        return Betreten()
    treffer = people.genau(person.discord_name, stand.spieler)
    vergeben = treffer is not None and treffer.id not in {konto.id for konto in stand.frei}
    if treffer is None or vergeben or _sonst_noch_so(stand, person, treffer):
        return Betreten(person=person, spieler=stand.frei)
    return Betreten(
        person=person,
        automatisch=treffer,
        vermerk=_vermerk(person.discord_name, treffer),
    )


def zuruecknehmen(runde: Runde, discord_user_id: str, foundry_user_id: str) -> bool:
    """Eine Zuordnung lösen — aber nur, wenn dort noch **genau dieses** Konto steht.

    Der Unterschied zu ``zuordnen(…, KEINE)`` ist die Bedingung, und sie ist der ganze
    Punkt: wer zurücknimmt, nimmt zurück, was **er selbst** geschrieben hat. Zwischen
    beidem liegt ein Gang ans Netz, und in diesem Fenster kann ``/zuordnung`` dieselbe
    Person längst umgehängt haben; blind zu löschen nähme dieser Entscheidung still ihre
    Wirkung. ``False`` heißt deshalb: dort steht etwas anderes, und das bleibt stehen.
    """
    person = people.speakers(runde).get(discord_user_id)
    if person is None or person.confirmed is None or person.confirmed.id != foundry_user_id:
        return False
    people.confirm(runde, {discord_user_id: ""})
    return True


def zuordnen(
    runde: Runde, discord_user_id: str, foundry_user_id: str, *, uebernehmen: bool = False
) -> Zugeordnet:
    """Was entschieden wurde — festgeschrieben und in einem Satz bestätigt.

    Ein Konto gehört genau einer Person: zwei Spuren mit demselben Namen fielen im fertigen
    Protokoll niemandem mehr auf. Wer es einer anderen **abnehmen** darf, hängt am Weg, auf
    dem gewählt wurde, und das entscheidet ``uebernehmen``.

    Ohne ``uebernehmen`` wird ein vergebenes Konto abgewiesen. So kommt das Menü im
    Zwiegespräch her: dort sitzt eine Person allein vor einer Liste, niemand sieht zu, und
    ein bereits vergebenes Konto steht dort ohnehin nur, wenn die Ansicht von vorhin ist.

    Mit ``uebernehmen`` wechselt es und die Vorbesitzerin verliert es — so kommt
    ``/zuordnung`` her, und das ist seit dem 2026-08-12 die Antwort darauf, wie eine
    **falsche** Zuordnung wieder weggeht. Der Fall ist echt: benennt sich Brok in »Mira«
    um, während die echte Mira »Mira am Handy« heißt, bekommt Brok beim Betreten Miras
    Konto — 1:1 gleicher Name. Ohne Umhängen käme Mira nie daran, und der Satz im Vermerk
    (»ändert `/zuordnung` es«) wäre gelogen. Der Weg über »erst zurücknehmen« wäre die
    schlechtere Antwort: er verlangt zwei Schritte in der Zeile einer **anderen** Person,
    und dazwischen liegt das Konto frei — auch für das Menü der nächsten, die den
    Sprachkanal betritt.

    Was das Umhängen **sichtbar** macht, steht nicht hier: ``/zuordnung`` zeigt nur
    ``PRO_SEITE`` Personen, die Vorbesitzerin fehlt darin ab der sechsten, und die Antwort
    auf den Klick sieht ohnehin nur, wer geklickt hat. Getragen wird es deshalb vom Thread
    und von einem Wort an die Vorbesitzerin — dafür steht ``vorher`` in der Antwort.
    """
    stand = people.overview(runde)
    person = next((wer for wer in stand.personen if wer.discord_user_id == discord_user_id), None)
    if person is None:
        return Zugeordnet(satz=NICHT_AUFGENOMMEN)
    gewaehlt = "" if foundry_user_id == KEINE else foundry_user_id
    if not gewaehlt:
        people.confirm(runde, {discord_user_id: ""})
        return Zugeordnet(satz=ZUORDNUNG_GELOEST.format(name=person.discord_name))
    spieler = next((eintrag for eintrag in stand.spieler if eintrag.id == gewaehlt), None)
    if spieler is None:
        return Zugeordnet(satz=SPIELER_WEG)
    vorher = next(
        (
            andere
            for andere in stand.personen
            if andere.confirmed is not None
            and andere.confirmed.id == gewaehlt
            and andere.discord_user_id != discord_user_id
        ),
        None,
    )
    if vorher is None:
        people.confirm(runde, {discord_user_id: gewaehlt})
        return Zugeordnet(
            satz=ZUGEORDNET.format(name=person.discord_name, spieler=spieler.name),
            spieler=spieler,
        )
    if not uebernehmen:
        return Zugeordnet(satz=SPIELER_VERGEBEN)
    people.confirm(runde, {vorher.discord_user_id: "", discord_user_id: gewaehlt})
    return Zugeordnet(
        satz=UEBERNOMMEN.format(
            name=person.discord_name, spieler=spieler.name, vorher=vorher.discord_name
        ),
        spieler=spieler,
        wer=person,
        vorher=vorher,
    )
