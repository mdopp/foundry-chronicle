"""Das Foundry-Passwort — im Arbeitsspeicher und sonst nirgends.

Ein Passwort lässt sich nicht hashen: wir müssen es Foundry **vorzeigen**, nicht prüfen.
Damit bleibt nur die härtere Regel — es wird gar nicht erst vorgehalten. Es kommt einmal
herein, lebt in diesem Modul und ist danach weg. Nebenwirkung: eine Instanz, die mehrere
Runden trägt, speichert kein einziges fremdes Geheimnis.

Drei Enden hat die Frist, alle drei absichtlich:

* **Der Abgleich verbraucht es.** ``chronicle.foundry.service.sync`` vergisst es, sobald
  der Handschlag durch ist — auch wenn er schiefging. Ein zweiter Versuch fragt neu, und
  genau das war die Zusage: Foundry aus am Sitzungsende heißt »nochmal eingeben«, nicht
  »liegt schon da«.
* **Der Prozess nimmt es mit.** Nichts hiervon wird geschrieben.
* **Die harte Frist** fängt den Rest: ein Passwort, das eingegeben, aber nie verbraucht
  wurde, weil der Lauf gar nicht erst begann. ``FRIST`` sind zwölf Stunden — lang genug,
  dass eine Runde, die um sieben anfängt und um drei Uhr nachts aufhört, ihren Abgleich
  noch bekommt, und kurz genug, dass am nächsten Spielabend garantiert nichts mehr liegt.

Das Passwort steht in keinem ``repr``: der Merkzettel maskiert sich selbst, und nach außen
gibt es ``ist_gemerkt`` — *ob*, nie *was*. Gemerkt wird **je Runde**; eine Runde kommt an
das Passwort einer anderen so wenig wie an deren Chronik.
"""

from __future__ import annotations

import threading
from time import monotonic

from chronicle.runde import Runde

FRIST = 12 * 60 * 60.0

MASKE = "***"


class _Merkzettel:
    __slots__ = ("passwort", "ablauf")

    def __init__(self, passwort: str, ablauf: float) -> None:
        self.passwort = passwort
        self.ablauf = ablauf

    def __repr__(self) -> str:
        return f"_Merkzettel(passwort='{MASKE}', ablauf={self.ablauf!r})"


_gemerkt: dict[int, _Merkzettel] = {}
_schloss = threading.RLock()


def _gueltig(runde_id: int, jetzt: float) -> _Merkzettel | None:
    zettel = _gemerkt.get(runde_id)
    if zettel is None:
        return None
    if zettel.ablauf <= jetzt:
        del _gemerkt[runde_id]
        return None
    return zettel


def merken(runde: Runde, passwort: str | None, *, frist: float = FRIST) -> None:
    """Ein leeres Passwort heißt vergessen — ein leer abgesendetes Feld setzt nichts fort."""
    sauber = (passwort or "").strip()
    with _schloss:
        if not sauber:
            _gemerkt.pop(runde.id, None)
            return
        _gemerkt[runde.id] = _Merkzettel(sauber, monotonic() + frist)


def passwort(runde: Runde) -> str | None:
    with _schloss:
        zettel = _gueltig(runde.id, monotonic())
    return None if zettel is None else zettel.passwort


def ist_gemerkt(runde: Runde) -> bool:
    """Ob eines bereitliegt — das ist alles, was eine Anzeige erfahren darf."""
    with _schloss:
        return _gueltig(runde.id, monotonic()) is not None


def vergiss(runde: Runde) -> None:
    with _schloss:
        _gemerkt.pop(runde.id, None)


def vergiss_alle() -> None:
    with _schloss:
        _gemerkt.clear()
