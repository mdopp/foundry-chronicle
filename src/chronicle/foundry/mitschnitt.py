"""Die rohe Antwort des Servers mitschreiben — und ohne Server wieder abspielen.

Warum das hier steht und nicht nur ein Test mehr geschrieben wurde: #242 war ein Fehler,
den die Tests **nicht** finden konnten. Die Fixtures trugen ``system.roll``, weil jemand
sie von Hand geschrieben hatte; ein echter Server tut das nicht, und der Adapter fiel
gegen ihn aus, während alles grün blieb. Eine Fixture, die die Annahme des Adapters
bestätigt, beweist nichts. Also wird nicht mehr nachgebildet, sondern **aufgehoben**: was
der Server wirklich geschickt hat, in der Form, in der er es geschickt hat.

Ein Mitschnitt ist eine Datei mit einem **Bild** je Zeile (JSONL) — Zeitpunkt, Konto-Id
und die ungefilterte Weltantwort. Mehrere Bilder hintereinander sind der Verlauf eines
Abends: der Strom sieht während der Sitzung immer wieder nach, jeder Blick hängt ein Bild
an. ``Wiedergabe`` gibt sie in derselben Reihenfolge zurück und hat dieselbe Oberfläche
wie ``FoundryClient`` — sie ist der Mock-Server, gegen den ein Abgleich ohne laufendes
Foundry durchläuft.

**Ein Mitschnitt ist personenbezogen.** Er ist der Weltabzug, nur mehrmals: Klarnamen
aller Konten, Geflüster, GM-Inhalte. Er liegt deshalb im selben gitignorierten Ordner mit
denselben Rechten und wird **nur** geschrieben, wenn der Betrieb es ausdrücklich
einschaltet (``CHRONICLE_FOUNDRY_MITSCHNITT``). Eingecheckt wird davon nichts, was nicht
durch ``scripts/anonymisiere_welt.py`` gelaufen ist — das Skript kennt die Bildform.
In eine Logzeile geht von seinem Inhalt nichts, nur wie viele Bilder es waren.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Ein Mitschnitt je Runde und Tag. Der Tag hält zwei Abende auseinander, die Runde zwei
# Gruppen: eine Instanz trägt mehrere, und zwei ineinander geschriebene Welten ließen sich
# hinterher nicht mehr trennen — der Abend der einen Gruppe stünde in dem der anderen.
NAME = "mitschnitt-runde-{runde}-{tag}.jsonl"

ZEIT = "zeit"
KONTO = "userId"
WELT = "welt"


@dataclass(frozen=True)
class Bild:
    """Ein Blick in die Welt: wann, mit wessen Augen, und was der Server geschickt hat."""

    zeit: str
    user_id: str
    welt: Mapping


def ziel(ordner: Path, runde_id: int, *, tag: str | None = None) -> Path:
    return ordner / NAME.format(runde=runde_id, tag=tag or datetime.now(UTC).date().isoformat())


def zeile(user_id: str, raw: Mapping, *, zeit: str | None = None) -> str:
    """Ein Bild als eine Zeile — ``default=str`` statt eines Abbruchs mitten im Abend."""
    return json.dumps(
        {
            ZEIT: zeit or datetime.now(UTC).isoformat(timespec="seconds"),
            KONTO: user_id,
            WELT: raw,
        },
        ensure_ascii=False,
        default=str,
    )


def schreibe(datei: Path, user_id: str, raw: Mapping, *, zeit: str | None = None) -> None:
    with datei.open("a", encoding="utf-8") as mitschrift:
        mitschrift.write(zeile(user_id, raw, zeit=zeit) + "\n")


def _bild(roh: object) -> Bild | None:
    if not isinstance(roh, Mapping) or not isinstance(roh.get(WELT), Mapping):
        return None
    return Bild(zeit=str(roh.get(ZEIT) or ""), user_id=str(roh.get(KONTO) or ""), welt=roh[WELT])


def lies(datei: Path) -> tuple[Bild, ...]:
    """Die Bilder einer Mitschnittdatei, in der Reihenfolge des Abends."""
    gelesen = []
    for zeile_ in datei.read_text(encoding="utf-8").splitlines():
        if not zeile_.strip():
            continue
        try:
            roh = json.loads(zeile_)
        except ValueError:
            continue
        bild = _bild(roh)
        if bild is not None:
            gelesen.append(bild)
    return tuple(gelesen)


class Leer(RuntimeError):
    """Ein Mitschnitt ohne ein einziges Bild — es gibt nichts abzuspielen."""


class Wiedergabe:
    """Der Mock-Server: dieselbe Oberfläche wie ``FoundryClient``, nur aus einer Datei.

    Jeder Aufruf gibt das nächste Bild zurück. Ist das letzte erreicht, bleibt es stehen,
    statt zu enden: ein Abgleich fragt öfter, als der Strom mitgeschrieben hat, und ein
    Fehler an dieser Stelle sagte etwas über die Länge der Aufnahme, nicht über die
    Anbindung.
    """

    def __init__(self, bilder: tuple[Bild, ...]) -> None:
        if not bilder:
            raise Leer("Der Mitschnitt enthält kein Bild.")
        self._bilder = bilder
        self._naechstes = 0

    @classmethod
    def aus_datei(cls, datei: Path) -> Wiedergabe:
        return cls(lies(datei))

    def __len__(self) -> int:
        return len(self._bilder)

    def __iter__(self) -> Iterator[Bild]:
        return iter(self._bilder)

    def fetch_world(self) -> tuple[str, Mapping]:
        bild = self._bilder[self._naechstes]
        self._naechstes = min(self._naechstes + 1, len(self._bilder) - 1)
        return bild.user_id, bild.welt
