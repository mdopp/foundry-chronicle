"""Die eingebaute Testwelt — eine Welt im Umfang einer echten, mitgeliefert.

Der Foundry-Server gehört der Spielleitung und ist zwischen den Sitzungen meistens aus.
Wer prüfen will, ob Filter, Adapter und Zwischenspeicher mit einer Welt in echtem Umfang
zurechtkommen — 19 Konten, 91 Figuren, 8 Chat-Nachrichten, 32 Szenen —, wartet sonst auf
einen fremden Rechner. Diese Datei nimmt ihm das ab.

**Woher sie stammt: sie ist erzeugt, nicht abgegriffen.** ``testwelt.json`` fällt Zeichen
für Zeichen aus ``scripts/erzeuge_testwelt.py`` heraus, und ``tests/test_testwelt.py``
hält das als Dauergate fest: wer die Datei anfasst, ohne das Skript anzufassen, fällt
durch. Kein Wert darin kommt aus einer echten Welt — keine Kennung, kein Zeitstempel
(die Zeitachse beginnt an einem erfundenen 1. Januar 2000), kein Name, kein Welttitel.
Auch eine *pseudonymisierte* Welt wäre hier falsch: der Konten-Graph, die
Berechtigungsverteilung und die Uhrzeiten sagen weiterhin, wer wann wie lange mit wem
spielt, und das gehört in kein veröffentlichtes Image.

Was sie mit einer echten Welt teilt, ist die **Form**. Ein echter Abzug wurde einmal
ausgezählt — Schlüssel je Ebene, Figurentypen, Verteilung der ``ownership``-Stufen,
Rollen der Konten, welche Nachrichten einen ``system.roll`` tragen, wie ein
Daggerheart-Wurf mit ``hope``/``fear`` aussieht, was in den eingebetteten JSON-Strings
unter ``rolls[]`` steht —, und diese Auszählung ist in das Skript eingeflossen. Ihr
Inhalt nicht. ``scripts/anonymisiere_welt.py`` bleibt daneben stehen: es macht aus einer
**eigenen** Welt eine lokale Fixture; eingecheckt ist die erfundene.

Gelesen wird sie über **dieselbe** Strecke wie eine Antwort vom Server:
Berechtigungsfilter → System-Adapter → Zwischenspeicher. Kein Netz, kein zweiter Prozess,
kein zweiter Weg — sonst prüfte die Testwelt etwas anderes als den Betrieb.

Und sie ist **keine Kampagne**: eine Runde wird nie an diese Welt gebunden, damit das
Zurückschalten auf den echten Server nicht als Weltwechsel gilt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from chronicle.foundry import permissions

DATEI = Path(__file__).with_name("testwelt.json")

HINWEIS = "Testwelt aktiv — das sind keine echten Kampagnendaten."


@lru_cache(maxsize=1)
def welt() -> Mapping:
    """Der Rohdump der Testwelt — dieselbe Form, die ``FoundryClient`` zurückgibt."""
    return json.loads(DATEI.read_text(encoding="utf-8"))


def konto(raw: Mapping) -> str:
    """Mit wessen Augen die Testwelt gelesen wird.

    Nicht mit denen der Spielleitung: die sieht alles, und ein Filter, der nichts
    wegnimmt, beweist nichts. Gewählt wird deshalb das erste Konto ohne Leitungsrechte,
    dem eine Figur gehört — deterministisch aus der Datei, damit zwei Abgleiche derselben
    Fixture denselben Ausschnitt ergeben.
    """
    benutzer = [eintrag for eintrag in raw.get("users") or [] if isinstance(eintrag, Mapping)]
    besitzer: set[str] = set()
    for figur in raw.get("actors") or []:
        if isinstance(figur, Mapping):
            besitzer.update(permissions.owner_ids(figur.get("ownership")))
    for pruefung in (
        lambda eintrag: not permissions.is_gm(eintrag) and _id(eintrag) in besitzer,
        lambda eintrag: not permissions.is_gm(eintrag),
        lambda eintrag: True,
    ):
        for eintrag in benutzer:
            if pruefung(eintrag):
                return _id(eintrag)
    return ""


def abzug() -> tuple[str, Mapping]:
    """Was ein Abgleich sonst vom Server bekommt: Konto-Id und Rohdump."""
    raw = welt()
    return konto(raw), raw


def _id(dokument: Mapping) -> str:
    return str(dokument.get("_id") or dokument.get("id") or "")
