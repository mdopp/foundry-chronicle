"""Den Rückblick dorthin stellen, wo die Gruppe ohnehin ist.

Der Rückblick wird unmittelbar vor der nächsten Sitzung gelesen — also gehört er in den
Gruppenkanal und nicht in eine Oberfläche, die jemand extra öffnen muss. Nicht in
``#diktat``: der ist der Briefkasten des Erzählenden, hier geht es in die Runde. Welcher
Kanal es ist, sagt eine Einstellung; **leer heißt: keine Zustellung.** Es gibt keinen
Sitzungskalender, also auch keinen Zeitpunkt, auf den sich zielen ließe.

Gepostet wird der abgelegte Rückblick, unverändert — als **Embed**: er ist kurz genug
dafür, und ein Embed hebt ihn im Kanalverlauf ab, statt ihn zwischen den Gesprächen des
Abends untergehen zu lassen. Der Rückblick ist per Konstruktion aus berechtigungs­
gefiltertem Material komponiert; daran vorbei wird nichts hineingereicht.

Seine eigenen Überschriften bleiben stehen, und sie sind der Punkt: »Offene Fäden —
Deutung des Modells, keine Fakten« steht im Embed genauso da wie im abgelegten Text. Wer
ihn vor der nächsten Sitzung liest, sieht damit, was belegt ist und was gedeutet.

**Welcher Kanal, steht in zwei Formen da.** ``/chronicle setup`` schreibt die Id des gewählten
Kanals, ältere Runden und ``DISCORD_RECAP_CHANNEL`` tragen seinen Namen. Beides wird
angenommen und nichts gewandert: eine Wanderung müsste Namen gegen Discord auflösen, um
sie in Ids zu übersetzen — zu einem Zeitpunkt, an dem kein Bot läuft und kein Netz zugesagt
ist —, und was sie nicht auflösen kann, träfe sie am Ende leer. Die Umgebungsvariable
bleibt ohnehin ein Name, also bleiben beide Formen lebendig, gewandert oder nicht.

Gesucht wird **in der Gilde dieser Runde**, nie darüber hinaus: »chronik« heißt in jeder
zweiten Gilde ein Kanal. Eine Runde ohne Gilde hat keinen Ort, an den etwas gehen könnte;
dann wird nicht geraten, sondern gesagt.

**Was nicht ankommt, wird gesagt.** Der Rückgabewert trägt deshalb nicht nur einen Satz,
sondern auch, ob er die Gruppe angeht: der Fehler aus #182 war nicht die verwechselte
Kanalform allein, sondern dass beide Aufrufer die Meldung verwarfen und das Schweigen
sich wie eine gelungene Zustellung las. Ein gescheiterter Versuch steht jetzt im Ergebnis
des Laufs — im Nachtbericht und in der Antwort auf ``/chronicle`` — und als Warnung im Log.

**Warum das ein anderer Weg ist als der der Chronik** (#261). Beides geht nach Discord und
im selben Lauf, aber es sind drei Unterschiede auf einmal: ein anderes **Ziel** — die
Chronik in den Sitzungs-Thread, den der Bot selbst angelegt hat, der Rückblick in den
Kanal, den ``/chronicle setup`` aus der Gildenliste anbietet —, eine andere **Form** — Anhang gegen
Embed —, und damit ein anderes **Recht**, das Discord dafür verlangt. Aus »die Chronik kam
an« folgt deshalb nichts über den Rückblick; drei Läufe lang sah es trotzdem so aus, als
müsste es das. Zusammengelegt wird darum nichts: der Rückblick wird vor der nächsten
Sitzung im Gruppenkanal gelesen und nicht in einem Thread gesucht, und eine ganze Chronik
passt in kein Embed. Was fehlte, war kein gemeinsamer Weg, sondern eine Meldung, die sagt,
welcher der beiden woran scheiterte — die trägt seit #261 ``chronicle.discord.client``.

**Eine Sitzung, eine Zustellung.** Der Zeitpunkt steht in ``protocol.delivered_at``; ein
zweiter Lauf sieht ihn und schweigt. Eine neu komponierte Fassung wird deshalb *nicht*
noch einmal gepostet: der Kanal ist die Zeitachse der Gruppe, ein zweiter Rückblick darin
läse sich wie eine zweite Sitzung. Die jeweils gültige Fassung hängt als Chronik-Datei im
Sitzungs-Thread — dort ist der Ort für Fassungen, siehe ``chronicle.discord.ausgabe``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from chronicle import db, settings
from chronicle import sprache as sprachen
from chronicle.compose.service import RUECKBLICK
from chronicle.config import Config
from chronicle.discord.client import DiscordClient, DiscordError
from chronicle.discord.grenzen import EMBED_TEXT, EMBED_TITEL, gekappt
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

# Discords Maße für ein Embed stehen in ``chronicle.discord.grenzen``. Die Prosa liegt weit
# darunter — sie ist auf zehn bis fünfzehn Sätze angelegt. ``### Belegt aus der Chronik``
# ist es nicht: der Block wächst 1:1 mit der Wurfzahl des Abends, und ein gemessener echter
# Abend brachte vierzig Würfe (``foundry/systems.py``). Ein Rückblick, der die Grenze reißt,
# ist deshalb der Normalfall und kein Fehler des Rückblicks. Verteilt wird er trotzdem nicht
# auf mehrere Nachrichten — ein Embed ist ein Feld. Gekürzt wird er, aber nur an einer
# Zeilengrenze: die Prosa steht oben, der Beleg unten, also frisst die Kürzung ausgerechnet
# das Belegte, und eine mittendurch geschnittene Faktenzeile sähe aus wie eine vollständige.
# »Formel 1d12 + 1d12« statt »1d12 + 1d12 + 2« ist eine Zahl, die so nicht im Chat-Log steht.
TITEL_GRENZE = EMBED_TITEL
TEXT_GRENZE = EMBED_TEXT

NICHT_EINGERICHTET = "No bot token — the recap stays in the chronicle."
KEIN_ZUSTELLKANAL = "No delivery channel entered — the recap stays in the chronicle."
# Der eingetragene Wert steht bewusst nicht drin: als Id sagt er niemandem etwas, und als
# Id aus einer fremden Gilde wäre er das eine, was hier nicht hinausgehen soll. Ins Log
# gehört er, dorthin sieht der Betreiber.
KEIN_KANAL = (
    "I do not see the configured delivery channel in your guild — the recap for session "
    "{sitzung} stayed put. Pick the channel again in `/chronicle setup`."
)
OHNE_GILDE = (
    "This round hangs on no Discord guild — I can deliver the recap for session {sitzung} "
    "nowhere. Set it up with `/chronicle setup` in your guild."
)
KEIN_RUECKBLICK = "Session {sitzung} has no recap."
SCHON_ZUGESTELLT = "Recap for session {sitzung} had already been delivered."
ZUGESTELLT = "Recap for session {sitzung} delivered."
GESCHEITERT = "Recap for session {sitzung} not delivered: {grund}"

GEKUERZT = "\n\n… cut short here. The whole session is in the chronicle file in the thread."
GEKUERZT_FAKTEN = (
    "\n\n… cut short here: {fehlend} of {gesamt} Foundry facts are missing. The whole "
    "session is in the chronicle file in the thread."
)


@dataclass(frozen=True)
class Zustellung:
    """Was über die Zustellung zu sagen ist — und ob es die Gruppe angeht.

    ``meldung`` steht immer da; sie ist die Zeile für den Stapelaufruf und das Log.
    ``gescheitert`` trennt davon die Fälle, in denen etwas **nicht** ankam, obwohl es
    sollte. Ohne diese Trennung müsste jeder Aufrufer die Sätze wieder auseinanderraten —
    und genau das taten sie nicht, sondern warfen den Rückgabewert weg (#182).
    """

    meldung: str
    gescheitert: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _protokoll(runde: Runde, session_id: int) -> tuple[str, str | None] | None:
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT text, delivered_at FROM protocol "
            "WHERE runde_id = ? AND session_id = ? AND kind = ?",
            (scope.runde_id, session_id, RUECKBLICK),
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else (str(zeile["text"]), zeile["delivered_at"])


def _merken(runde: Runde, session_id: int, at: str) -> None:
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "UPDATE protocol SET delivered_at = ? "
                "WHERE runde_id = ? AND session_id = ? AND kind = ?",
                (at, scope.runde_id, session_id, RUECKBLICK),
            )
    finally:
        scope.close()


# Die Zeile, unter der die belegten Fakten stehen — in **jeder** Inhaltssprache. Der
# Rückblick liegt fertig in der Sprache seiner Runde, und eine Runde darf umstellen; nur
# die eine Fassung zu kennen hieße, unter dem gekürzten Embed »0 von 0 Fakten« zu melden,
# wo welche stehen.
FAKTEN_ZEILE = tuple(texte.fakten_zeile for texte in sprachen.RUECKBLICK.values())


def _fakten(zeilen: Sequence[str]) -> int:
    """Wie viele belegte Foundry-Fakten in diesen Zeilen stehen.

    Gezählt wird nur, was unter der Fakten-Zeile steht — die Szenenliste darüber und die
    offenen Fäden benutzen dieselbe Aufzählungsform, sind aber keine Fakten.
    """
    zahl = 0
    im_beleg = False
    for rohzeile in zeilen:
        zeile = rohzeile.strip()
        if zeile in FAKTEN_ZEILE:
            im_beleg = True
        elif im_beleg and zeile.startswith("- "):
            zahl += 1
        else:
            im_beleg = False
    return zahl


def _gekuerzt(rumpf: str) -> str:
    """Der Rumpf auf Embed-Maß — nur ganze Zeilen fallen weg, und der Hinweis sagt welche.

    Der Platz für den Hinweis wird nach der **Gesamtzahl** der Fakten bemessen; die Zahl der
    fehlenden steht erst fest, wenn der Schnitt liegt, und sie ist nie länger geschrieben
    als die Gesamtzahl.
    """
    zeilen = rumpf.split("\n")
    gesamt = _fakten(zeilen)
    platz = TEXT_GRENZE - max(
        len(GEKUERZT), len(GEKUERZT_FAKTEN.format(fehlend=gesamt, gesamt=gesamt))
    )
    passend: list[str] = []
    laenge = 0
    for zeile in zeilen:
        gewachsen = laenge + len(zeile) + (1 if passend else 0)
        if gewachsen > platz:
            break
        passend.append(zeile)
        laenge = gewachsen
    if not passend:
        # Eine einzige Zeile, die schon allein zu lang ist: hier gibt es keine Zeilengrenze,
        # an der zu schneiden wäre, und nichts anzuzeigen wäre die schlechtere Antwort.
        return gekappt(rumpf, TEXT_GRENZE, GEKUERZT)
    fehlend = gesamt - _fakten(passend)
    if not fehlend:
        return "\n".join(passend) + GEKUERZT
    return "\n".join(passend) + GEKUERZT_FAKTEN.format(fehlend=fehlend, gesamt=gesamt)


def embed(text: str) -> dict[str, str]:
    """Der abgelegte Rückblick als Embed: die Titelzeile als Titel, der Rest darunter.

    Umgeschrieben wird nichts. Die Überschriften des Rückblicks tragen die Trennung
    zwischen Belegtem und Gedeutetem, und Discord stellt sie genauso dar — eine zweite
    Auszeichnung wäre eine zweite Wahrheit.
    """
    zeilen = text.strip().splitlines()
    titel = ""
    if zeilen and zeilen[0].startswith("# "):
        titel = zeilen[0][2:].strip()
        zeilen = zeilen[1:]
    rumpf = "\n".join(zeilen).strip()
    if len(rumpf) > TEXT_GRENZE:
        logger.warning(
            "Rückblick ist %s Zeichen lang, ein Embed nimmt %s — gekürzt zugestellt.",
            len(rumpf),
            TEXT_GRENZE,
        )
    gebaut = {"description": rumpf if len(rumpf) <= TEXT_GRENZE else _gekuerzt(rumpf)}
    if titel:
        gebaut["title"] = gekappt(titel, TITEL_GRENZE)
    return gebaut


def deliver(
    config: Config, runde: Runde, session_id: int, *, client: DiscordClient | None = None
) -> Zustellung:
    """Stellt den Rückblick dieser Sitzung zu, wenn er noch nicht zugestellt ist."""
    zugang = settings.effective(config, runde)
    if not zugang.discord_configured:
        return Zustellung(NICHT_EINGERICHTET)
    gewaehlt = (zugang.discord_recap_channel or "").strip().lstrip("#")
    if not gewaehlt:
        return Zustellung(KEIN_ZUSTELLKANAL)

    abgelegt = _protokoll(runde, session_id)
    if abgelegt is None:
        return Zustellung(KEIN_RUECKBLICK.format(sitzung=session_id))
    text, zugestellt = abgelegt
    if zugestellt is not None:
        return Zustellung(SCHON_ZUGESTELLT.format(sitzung=session_id))

    if not runde.guild_id:
        logger.warning(
            "Runde %s hat keine Gilde — Rückblick zur Sitzung %s bleibt liegen.",
            runde.id,
            session_id,
        )
        return Zustellung(OHNE_GILDE.format(sitzung=session_id), gescheitert=True)

    bot = client if client is not None else DiscordClient(zugang)
    try:
        kanal = bot.guild_channel_id(runde.guild_id, gewaehlt)
        if kanal is None:
            logger.warning(
                "Zustellkanal %s liegt nicht in Gilde %s — Rückblick zur Sitzung %s bleibt liegen.",
                gewaehlt,
                runde.guild_id,
                session_id,
            )
            return Zustellung(KEIN_KANAL.format(sitzung=session_id), gescheitert=True)
        bot.post_embed(kanal, embed(text))
    except DiscordError as fehler:
        # Der Rückblick steht bereits in der Datenbank; ein Discord, das gerade nicht
        # antwortet, macht daraus keinen fehlgeschlagenen Stapellauf. ``delivered_at``
        # bleibt leer, der nächste Lauf holt die Zustellung nach.
        #
        # Die Gilde steht nur hier, nicht in der Meldung an die Gruppe: sie sagt der Gruppe
        # nichts und dem Betreiber alles — er sieht sonst drei Läufe lang denselben Satz,
        # ohne zu wissen, welche Runde ihn schreibt.
        logger.warning(
            "Rückblick zur Sitzung %s nicht zugestellt — Gilde %s: %s",
            session_id,
            runde.guild_id,
            fehler,
        )
        return Zustellung(GESCHEITERT.format(sitzung=session_id, grund=fehler), gescheitert=True)
    # Erst posten, dann merken: ein fehlgeschlagener Post soll wiederholt werden. Die
    # Lücke dazwischen ist ein Prozessabbruch zwischen HTTP-200 und einem lokalen UPDATE.
    _merken(runde, session_id, _now())
    return Zustellung(ZUGESTELLT.format(sitzung=session_id))
