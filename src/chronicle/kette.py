"""Die Reihenfolge, in der aus Material eine Chronik wird — an einer Stelle.

Verschriften, übernehmen, komponieren, zurückblicken, zustellen, anhängen, vorschlagen:
diese sieben Schritte standen dreimal im Baum — hinter ``/session done``, im Nachtlauf
und im Stapelaufruf — und die drei liefen auseinander (#221). Zwei von ihnen fehlte die
**Übernahme**, und damit erreichte kein gesprochenes Wort ihre Chronik: die Komposition
liest Notizen, und ein Transkript, das erst danach zur Notiz würde, käme eine Chronik zu
spät. Genau das war die Lücke aus #140 — behoben stand sie nur in einer der drei Kopien,
und die Begründung dazu ebenfalls.

Hier steht die Reihenfolge einmal. Was ein Aufrufer daraus **sagt**, bleibt seine Sache:
ein Lauf antwortet in einem Satz, der Stapel druckt Zeilen, die Nacht schreibt eine Karte.
Gleich ist nur, was geschieht.
"""

from __future__ import annotations

from dataclasses import dataclass

from chronicle import lebenszyklus, recordings, register
from chronicle.compose.composer import Composition
from chronicle.compose.recap import Recap
from chronicle.compose.service import compose_session, recap_session
from chronicle.config import Config
from chronicle.discord.ausgabe import anhaengen
from chronicle.discord.rueckblick import Zustellung, deliver
from chronicle.register import Suggested
from chronicle.runde import Runde
from chronicle.transcribe.merge import uebernehmen
from chronicle.transcribe.service import run_queue

OHNE_SITZUNG = "Diese Sitzung gibt es nicht mehr."


@dataclass(frozen=True)
class Lauf:
    """Was der Durchgang hinterlassen hat — jeder Aufrufer nimmt sich daraus seinen Satz."""

    verschriftet: int
    offen: int
    chronik: Composition
    rueckblick: Recap | None
    zustellung: Zustellung
    ausgabe: str
    vorschlaege: Suggested
    # Was der Durchgang endgültig verloren hat — eine Spur ohne letzten Anlauf, ein Ton,
    # den die Frist geholt hat. Die beiden Zähler darüber können das nicht tragen: sie
    # zählen die Spuren, die noch einmal drankommen (#286).
    verlust: tuple[str, ...] = ()


def warum_nicht(runde: Runde) -> str:
    """Zwei Wege zu einem leeren Lauf, und der Unterschied gehört gesagt.

    Die Sitzung ist fort — oder die Runde ruht, seit der Lauf begann. »Gibt es nicht mehr«
    wäre dann falsch.
    """
    return lebenszyklus.RUHT if lebenszyklus.ruht(runde) else OHNE_SITZUNG


def _stand(runde: Runde, vorher: tuple[recordings.Recording, ...]) -> tuple[int, int]:
    """Wie viele der wartenden Spuren nun ein Transkript haben — und wie viele noch warten.

    Gezählt wird **nach** dem Lauf und Spur für Spur. Die Zahl davor sagt nur, wie viele
    anstanden; genau aus ihr wurde »4 Aufnahmen verschriftet«, während alle vier nach einem
    nicht erreichbaren Erkenner unverändert auf ``wartet`` standen (#244).

    Die zweite Zahl ist die, zu der der Satz »kommt beim nächsten Lauf wieder dran« passt —
    also ``recordings.kommt_wieder_dran`` und nicht ``wartet``. Eine einmal gescheiterte
    Spur steht seit #247 wieder in der Warteschlange und gehört damit hierher; eine, die
    ihre Anläufe verbraucht hat, zählt zu keinem von beidem, denn versprochen wäre sonst
    ein Lauf, den es nicht mehr gibt.
    """
    danach = [recordings.get(runde, aufnahme.id) for aufnahme in vorher]
    fertig = sum(1 for a in danach if a is not None and a.status == recordings.FERTIG)
    offen = sum(1 for a in danach if a is not None and recordings.kommt_wieder_dran(a))
    return fertig, offen


def schreiben(config: Config, runde: Runde, session_id: int) -> Lauf | None:
    """Erst die wartenden Aufnahmen verschriften, dann übernehmen, dann komponieren.

    Die Übernahme steht zwischen beidem und nicht daneben — siehe den Kopf dieser Datei.
    ``None`` heißt: es ist nichts entstanden; ``warum_nicht`` sagt, warum.
    """
    wartende = recordings.pending(runde)
    meldungen = run_queue(config, runde)
    verschriftet, offen = _stand(runde, wartende)
    uebernehmen(runde, session_id)
    chronik = compose_session(config, runde, session_id)
    if chronik is None:
        return None
    rueckblick = recap_session(config, runde, session_id)
    zustellung = deliver(config, runde, session_id)
    ausgabe = anhaengen(config, runde, session_id)
    vorschlaege = register.suggest(config, runde, session_id)
    return Lauf(
        verschriftet=verschriftet,
        offen=offen,
        chronik=chronik,
        rueckblick=rueckblick,
        zustellung=zustellung,
        ausgabe=ausgabe,
        vorschlaege=vorschlaege,
        verlust=meldungen.verlust,
    )
