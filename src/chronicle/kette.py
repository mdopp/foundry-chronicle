"""Die Reihenfolge, in der aus Material eine Chronik wird — an einer Stelle.

Verschriften, übernehmen, komponieren, zurückblicken, zustellen, anhängen, vorschlagen:
diese sieben Schritte standen dreimal im Baum — hinter ``/chronik fertig``, im Nachtlauf
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

    wartend: int
    chronik: Composition
    rueckblick: Recap | None
    zustellung: Zustellung
    ausgabe: str
    vorschlaege: Suggested


def warum_nicht(runde: Runde) -> str:
    """Zwei Wege zu einem leeren Lauf, und der Unterschied gehört gesagt.

    Die Sitzung ist fort — oder die Runde ruht, seit der Lauf begann. »Gibt es nicht mehr«
    wäre dann falsch.
    """
    return lebenszyklus.RUHT if lebenszyklus.ruht(runde) else OHNE_SITZUNG


def schreiben(config: Config, runde: Runde, session_id: int) -> Lauf | None:
    """Erst die wartenden Aufnahmen verschriften, dann übernehmen, dann komponieren.

    Die Übernahme steht zwischen beidem und nicht daneben — siehe den Kopf dieser Datei.
    ``None`` heißt: es ist nichts entstanden; ``warum_nicht`` sagt, warum.
    """
    wartend = len(recordings.pending(runde))
    run_queue(config, runde)
    uebernehmen(runde, session_id)
    chronik = compose_session(config, runde, session_id)
    if chronik is None:
        return None
    rueckblick = recap_session(config, runde, session_id)
    zustellung = deliver(config, runde, session_id)
    ausgabe = anhaengen(config, runde, session_id)
    vorschlaege = register.suggest(config, runde, session_id)
    return Lauf(
        wartend=wartend,
        chronik=chronik,
        rueckblick=rueckblick,
        zustellung=zustellung,
        ausgabe=ausgabe,
        vorschlaege=vorschlaege,
    )
