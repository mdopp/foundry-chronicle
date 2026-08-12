"""Die Personen-Zuordnung Discord ↔ Foundry.

Discord-Ids stehen nirgends in Foundry. Über die Namen lässt sich ein **Vorschlag** machen
— bestätigt wird er von Hand, genau einmal, danach steht die Zuordnung und die Frage
stellt sich nie wieder. Die Bestätigung ist der Punkt, nicht die Erkennung: ein
stillschweigend übernommener Vorschlag ordnete irgendwann Aussagen der falschen Person
zu, und das fiele im fertigen Protokoll niemandem mehr auf.

Der eine Fall daneben ist die **1:1 gleiche Schreibweise** (Betreiber-Entscheidung vom
2026-08-12 zu #76): heißt jemand in Discord genau wie ein Foundry-Konto oder genau wie
dessen Figur, ist das kein Vorschlag mehr, sondern ein Beleg — und er wird ohne Rückfrage
festgeschrieben. »Genau« heißt hier wörtlich: ``genau`` vergleicht Zeichen für Zeichen,
kein Abstandsmaß, keine Ähnlichkeit. Und er gilt nur, solange die Gleichheit **eindeutig**
ist; heißen zwei Konten gleich, ist auch Gleichheit keine Antwort, dann wird gefragt.
Ungefragt entsteht damit nur, was der Runde auch gesagt wird — den Vermerk setzt
``chronicle.bot.gateway``, gleich nachdem hier geschrieben wurde, und kommt er nicht
hinaus, nimmt es dieselbe Stelle wieder zurück.

Gespeichert wird deshalb Bestätigtes und Gleichnamiges — nie ein Vorschlag. Vorschläge
entstehen bei jedem Aufruf neu und bleiben, wo man sie als solche sieht: im Menü.

Die Namen kommen aus dem, was ohnehin liegt: die Discord-Anzeigenamen aus dem
Einwilligungsprotokoll, die Foundry-Spieler und ihre Figuren aus dem Zwischenspeicher.
Hier steht nur, welche Id zu welcher gehört.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher

from chronicle import db
from chronicle.foundry.model import SPIELFIGUR
from chronicle.runde import Runde

# Ab hier ist eine Namensähnlichkeit einen **Vorschlag** wert. Beide Maße gelten allein für
# ``suggest`` und damit für das Menü; was ohne Rückfrage festgeschrieben wird, entscheidet
# ``genau`` und hat mit Ähnlichkeit nichts zu tun.
SCHWELLE = 0.8

# Liegen zwei Foundry-Spieler ähnlich nah, wird nichts vorgeschlagen: dann ist die Frage
# echt, und eine echte Frage gehört dem Menschen und nicht der Vorauswahl.
ABSTAND = 0.1

# Der Leerraum, den ``genau`` am Rand abschneidet — und nur dieser. ``str.strip()`` ohne
# Argument nähme jedes Zeichen mit, das Unicode für Leerraum hält, das geschützte
# Leerzeichen eingeschlossen; damit wären zwei verschieden geschriebene Namen gleich, und
# das ist die weitende Richtung, die hier nicht gilt.
LEERRAUM = " \t\n\r\f\v"


@dataclass(frozen=True)
class Spieler:
    id: str
    name: str
    characters: tuple[str, ...] = ()


@dataclass(frozen=True)
class Person:
    discord_user_id: str
    discord_name: str
    confirmed: Spieler | None = None
    suggestion: Spieler | None = None


@dataclass(frozen=True)
class Uebersicht:
    """Wer aufgenommen wurde, welche Konten es gibt — und welche davon noch zu haben sind.

    ``frei`` ist kein Ausschnitt zur Bequemlichkeit, sondern die Antwort auf »was darf ich
    anbieten«: ein Menü mit einem bereits vergebenen Konto lädt dazu ein, sich die Identität
    einer Mitspielerin zu nehmen. ``spieler`` bleibt vollständig, weil die Übersicht auch
    zeigen muss, was schon vergeben *ist*.
    """

    personen: tuple[Person, ...] = ()
    spieler: tuple[Spieler, ...] = ()
    frei: tuple[Spieler, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _spieler(scope: db.Scope) -> tuple[Spieler, ...]:
    # Nur Spielfiguren: die Spielleitung besitzt in Foundry auch jeden NSC, und »Die Wirtin
    # zum Krummen Ast« ist der Name einer Wirtin und nicht der einer Mitspielerin. Ohne den
    # Filter machte ein Spitzname aus dem NSC-Fundus seinen Träger zur Spielleitung.
    #
    # ``= ?`` lässt dabei auch alles liegen, wo kein Typ steht: ``NULL`` und der leere
    # String vergleichen sich mit nichts. Das trifft die **eingeschränkte** Projektion aus
    # ``foundry.world`` — wer eine Figur nur sehen, aber nicht führen darf, bekommt Id und
    # Namen und keinen Typ. Solche Figuren zählen hier also nicht mit; die Folge ist allein
    # Verlust: es wird gefragt, wo vielleicht nicht hätte gefragt werden müssen. Eine
    # falsche Zuordnung kann daraus nicht werden, und das ist die Richtung, die zählt.
    figuren: dict[str, list[str]] = {}
    for zeile in scope.execute(
        "SELECT name, owner_ids FROM foundry_character "
        "WHERE runde_id = ? AND type = ? ORDER BY name",
        (scope.runde_id, SPIELFIGUR),
    ):
        for besitzer in json.loads(zeile["owner_ids"]):
            figuren.setdefault(besitzer, []).append(zeile["name"])
    return tuple(
        Spieler(id=z["id"], name=z["name"], characters=tuple(figuren.get(z["id"], ())))
        for z in scope.execute(
            "SELECT id, name FROM foundry_player WHERE runde_id = ? ORDER BY name",
            (scope.runde_id,),
        )
    )


def _mitglieder(scope: db.Scope) -> tuple[tuple[str, str], ...]:
    # Der zuletzt protokollierte Anzeigename gewinnt — Discord-Namen ändern sich.
    zeilen = scope.execute(
        "SELECT user_id, name FROM consent_member WHERE runde_id = ? ORDER BY event_id",
        (scope.runde_id,),
    ).fetchall()
    neueste = {z["user_id"]: z["name"] for z in zeilen}
    return tuple(sorted(neueste.items(), key=lambda paar: paar[1].casefold()))


def _bestaetigt(scope: db.Scope) -> dict[str, str]:
    return {
        z["discord_user_id"]: z["foundry_user_id"]
        for z in scope.execute(
            "SELECT discord_user_id, foundry_user_id FROM person_mapping WHERE runde_id = ?",
            (scope.runde_id,),
        )
    }


def _stand(runde: Runde) -> tuple[tuple[Spieler, ...], dict[str, str], tuple]:
    scope = db.scoped(runde)
    try:
        return _spieler(scope), _bestaetigt(scope), _mitglieder(scope)
    finally:
        scope.close()


def _aehnlich(links: str, rechts: str) -> float:
    return SequenceMatcher(None, links.casefold().strip(), rechts.casefold().strip()).ratio()


def _naehe(name: str, kandidat: Spieler) -> float:
    """Wie nah ein Discord-Name diesem Spieler kommt — er selbst oder eine seiner Figuren.

    Beides zählt, weil beides vorkommt: die einen heißen in Discord wie ihr Foundry-Konto,
    die anderen wie die Figur, die sie spielen.
    """
    return max(_aehnlich(name, wert) for wert in (kandidat.name, *kandidat.characters))


def _schluessel(name: str) -> str:
    """Woran ``genau`` zwei Namen misst: NFC, Ränder ab, Kleinschreibung — mehr nicht.

    ``lower`` und nicht ``casefold``: casefold ist die **weitende** Richtung. Es macht
    ``Straße`` zu ``strasse`` und die Ligatur ``ﬁ`` zu ``fi``, und damit stünden zwei
    verschiedene Namen als gleich da — die Vorgabe heißt 1:1 und nicht »ungefähr«. Die
    Kleinschreibung bleibt, weil »mira« und »Mira« derselbe Name sind.

    NFC ist keine Weitung, sondern die Auflösung zweier Schreibweisen **desselben**
    Zeichens: ``é`` als ein Zeichen und ``e`` mit angehängtem Akzent sehen gleich aus,
    also sind es gleiche Namen. Ohne die Normalisierung hinge die Gleichheit daran, welche
    Tastatur jemand benutzt.
    """
    return unicodedata.normalize("NFC", name).strip(LEERRAUM).lower()


def getroffen(name: str, kandidat: Spieler) -> str | None:
    """Welcher Name dieses Kontos ``name`` **genau** trifft — sein eigener oder der einer Figur.

    ``genau`` sagt, *wer* getroffen hat; das hier sagt, *womit*. Der Unterschied gehört in
    den Satz, mit dem die Runde davon erfährt: heißt jemand wie die **Figur**, ist der
    Kontoname dort eine Behauptung über Namensgleichheit, die im selben Satz sichtbar
    nicht stimmt.

    Zurück kommt der Name so, wie er in Foundry steht — verglichen wird über ``_schluessel``,
    angezeigt wird das Original.
    """
    gesucht = _schluessel(name)
    return next(
        (wert for wert in (kandidat.name, *kandidat.characters) if _schluessel(wert) == gesucht),
        None,
    )


def _gleich(name: str, kandidat: Spieler) -> bool:
    return getroffen(name, kandidat) is not None


def genau(name: str, kandidaten: Sequence[Spieler]) -> Spieler | None:
    """Der eine Spieler, der **genau** so heißt — er selbst oder eine seiner Figuren.

    Kein Abstandsmaß, keine Ähnlichkeit: Zeichen für Zeichen. Übersehen wird genau
    dreierlei, und die Aufzählung ist vollständig — Groß- und Kleinschreibung
    (``lower``, nicht ``casefold``), gewöhnlicher Leerraum am Rand, und ob ein Zeichen
    zusammengesetzt oder als eines geschrieben ist (NFC). Das ist der Unterschied zu
    ``suggest`` und der Grund, warum ein Treffer hier ohne Rückfrage gilt — »Mira« neben
    »Mirah« ist keine Gleichheit, sondern eine Verwechslung, und die gehört ins Menü.

    Und **eindeutig** muss sie sein: heißen zwei Konten gleich, ist auch Gleichheit keine
    Antwort. Dann kommt keiner zurück und es wird gefragt.

    Eindeutig heißt dabei: unter **diesen** Kandidaten. Wer hier nur die freien Konten
    einwirft, fragt etwas anderes — dann verschwindet die Mehrdeutigkeit zweier Gleichnamiger
    genau dann, wenn einer davon schon vergeben ist, und übrig bleibt eine Gleichheit, die
    keine ist. Die Aufrufer geben deshalb **alle** Konten der Runde und ziehen erst danach
    ab, was vergeben ist.
    """
    treffer = [kandidat for kandidat in kandidaten if _gleich(name, kandidat)]
    return treffer[0] if len(treffer) == 1 else None


def suggest(name: str, kandidaten: Sequence[Spieler]) -> Spieler | None:
    """Der eine naheliegende Foundry-Spieler zu einem Discord-Namen — oder keiner.

    Unscharf, und das darf er sein: was hier herauskommt, steht im Menü als »vielleicht …«
    und wartet auf einen Klick. Geschrieben wird davon nichts — dafür ist ``genau`` da.
    """
    bewertet = sorted(
        ((_naehe(name, k), k) for k in kandidaten),
        key=lambda paar: paar[0],
        reverse=True,
    )
    if not bewertet or bewertet[0][0] < SCHWELLE:
        return None
    if len(bewertet) > 1 and bewertet[0][0] - bewertet[1][0] < ABSTAND:
        return None
    return bewertet[0][1]


def overview(runde: Runde) -> Uebersicht:
    """Wer aufgenommen wurde, wem er zugeordnet ist und was vorzuschlagen wäre."""
    spieler, bestaetigt, mitglieder = _stand(runde)
    nach_id = {s.id: s for s in spieler}
    vergeben = set(bestaetigt.values())
    frei = [s for s in spieler if s.id not in vergeben]
    personen = []
    for user_id, name in mitglieder:
        zugeordnet = nach_id.get(bestaetigt.get(user_id, ""))
        personen.append(
            Person(
                discord_user_id=user_id,
                discord_name=name,
                confirmed=zugeordnet,
                suggestion=None if zugeordnet else suggest(name, frei),
            )
        )
    return Uebersicht(personen=tuple(personen), spieler=spieler, frei=tuple(frei))


def speakers(runde: Runde) -> dict[str, Person]:
    """Je Discord-Id, wie eine Spur zu beschriften ist — ohne Vorschläge.

    Ein Vorschlag darf hier nicht auftauchen: an einer Spur stünde er wie eine Tatsache.
    Was hier steht, ist entweder von Hand bestätigt oder 1:1 derselbe Name (#76) — beides
    ist eine Tatsache, ein »vielleicht Mira« ist keine.
    """
    spieler, bestaetigt, mitglieder = _stand(runde)
    nach_id = {s.id: s for s in spieler}
    return {
        user_id: Person(
            discord_user_id=user_id,
            discord_name=name,
            confirmed=nach_id.get(bestaetigt.get(user_id, "")),
        )
        for user_id, name in mitglieder
    }


def confirm(runde: Runde, auswahl: Mapping[str, str]) -> None:
    """Schreibt fest, was entschieden ist; ein leerer Wert nimmt zurück.

    Entschieden heißt: ein Mensch hat gewählt, oder der Name ist 1:1 derselbe (#76). Ein
    Vorschlag kommt hier nie an.
    """
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        with scope:
            for user_id, foundry_user_id in auswahl.items():
                if not foundry_user_id:
                    scope.execute(
                        "DELETE FROM person_mapping WHERE runde_id = ? AND discord_user_id = ?",
                        (scope.runde_id, user_id),
                    )
                    continue
                scope.execute(
                    "INSERT INTO person_mapping "
                    "(runde_id, discord_user_id, foundry_user_id, confirmed_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (runde_id, discord_user_id) DO UPDATE SET "
                    "foundry_user_id = excluded.foundry_user_id, "
                    "confirmed_at = excluded.confirmed_at",
                    (scope.runde_id, user_id, foundry_user_id, zeitpunkt),
                )
    finally:
        scope.close()
