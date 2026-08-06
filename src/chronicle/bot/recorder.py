"""Der Ablauf einer Aufnahme: ansagen, protokollieren, je Sprecher eine Spur.

Die Reihenfolge ist der Kern und keine Höflichkeit: **erst ist die Ansage zu Ende
gespielt, dann beginnt der Mitschnitt.** ``schreiben`` weigert sich, bevor die Ansage
protokolliert ist — ein Aufrufer, der die Reihenfolge dreht, bekommt einen Fehler und
keine Datei.

Discord trennt die Audiodaten ohnehin je Client; damit entfällt die Sprechertrennung
nicht bloß billiger, sondern exakt. Geschrieben wird **eine Datei je Sprecher für die
ganze Sitzung**, im Strom auf die Platte. Kein Puffer im Speicher: vier Stunden Runde
sind je Spur ein knappes Gigabyte, und in kleine Schnipsel geschnitten verlöre die
Erkennung genau den Kontext, von dem sie lebt.

Die fertige Spur reiht sich in dieselbe Warteschlange ein wie ein Diktat-Upload — einen
zweiten Verarbeitungsweg gibt es nicht. Gelöscht wird eine Aufnahme nur auf ausdrückliches
Verlangen (``python -m chronicle.transcribe --loeschen``).

Diese Datei kennt Discord nicht. Sie spricht mit einer ``Stimme``; wer die ist, entscheidet
``gateway.py``, und in den Tests ist es eine Attrappe.
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from werkzeug.utils import secure_filename

from chronicle import consent, notes, recordings
from chronicle.bot import BotFehler, ansage
from chronicle.config import Config

logger = logging.getLogger(__name__)

OHNE_SITZUNG = "Noch keine Sitzung angelegt — leg eine an, dann schneide ich mit."
NICHT_ANGESAGT = "Es wurde noch nichts angesagt — ohne Ansage wird nichts geschrieben."
GESTARTET = "Ansage gelaufen, ich schneide mit — je Sprecher eine eigene Spur."
NICHTS_GESPROCHEN = "Es hat niemand gesprochen — keine Spur abgelegt."


@dataclass(frozen=True)
class Kanal:
    guild_id: str
    id: str
    name: str


class AufnahmeFehler(BotFehler):
    """Was den Start verhindert — eine fehlende Sitzung etwa."""


class NichtAngesagt(BotFehler):
    """Der Versuch, vor der Ansage zu schreiben."""


class Stimme(Protocol):
    """Die ganze Abhängigkeit dieser Stufe zu Discord."""

    kanal: Kanal

    def mitglieder(self) -> tuple[consent.Member, ...]: ...

    async def ansagen(self, datei: Path) -> None: ...

    def mitschneiden(self, aufnahme: Aufnahme) -> None: ...

    def mitschnitt_beenden(self) -> None: ...

    async def trennen(self) -> None: ...


class _Spur:
    """Ein Sprecher, eine Datei, im Strom geschrieben.

    ``writeframes`` schreibt den WAV-Kopf bei jedem Aufruf mit fort. Stirbt der Prozess
    mitten in der Sitzung, liegt trotzdem eine abspielbare Spur da statt eines Rumpfes.
    """

    def __init__(self, pfad: Path) -> None:
        self.pfad = pfad
        self.bytes = 0
        self._datei = wave.open(str(pfad), "wb")
        self._datei.setnchannels(ansage.KANAELE)
        self._datei.setsampwidth(ansage.BREITE)
        self._datei.setframerate(ansage.RATE)

    def schreiben(self, pcm: bytes) -> None:
        self._datei.writeframes(pcm)
        self.bytes += len(pcm)

    def schliessen(self) -> None:
        self._datei.close()


def _spurname(sprecher: consent.Member) -> str:
    # Der Anzeigename kann aus Zeichen bestehen, die kein Dateiname sein können; dann
    # bleibt die Id. Wer wirklich gesprochen hat, steht ohnehin im Einwilligungsprotokoll.
    return f"{secure_filename(sprecher.name) or f'sprecher-{sprecher.id}'}.wav"


class Aufnahme:
    def __init__(self, config: Config, session_id: int, kanal: Kanal) -> None:
        self.session_id = session_id
        self.kanal = kanal
        self._config = config
        self._spuren: dict[str, _Spur] = {}
        self._angesagt = False

    @property
    def laeuft(self) -> bool:
        return self._angesagt

    def ansage_protokollieren(
        self, mitglieder: tuple[consent.Member, ...], *, art: str = consent.ANSAGE
    ) -> int:
        kennung = consent.record(
            self._config.database_path,
            session_id=self.session_id,
            kind=art,
            guild_id=self.kanal.guild_id,
            channel_id=self.kanal.id,
            channel_name=self.kanal.name,
            text=ansage.TEXT,
            members=mitglieder,
        )
        if art == consent.ANSAGE:
            self._angesagt = True
        logger.info(
            "Einwilligung %s in #%s protokolliert: %s Anwesende",
            art,
            self.kanal.name,
            len(mitglieder),
        )
        return kennung

    def schreiben(self, sprecher: consent.Member, pcm: bytes) -> None:
        if not self._angesagt:
            raise NichtAngesagt(NICHT_ANGESAGT)
        spur = self._spuren.get(sprecher.id)
        if spur is None:
            self._config.recordings_dir.mkdir(parents=True, exist_ok=True)
            ziel = recordings.target_path(
                self._config.recordings_dir, self.session_id, _spurname(sprecher)
            )
            spur = _Spur(ziel)
            self._spuren[sprecher.id] = spur
            logger.info("Spur für %s: %s", sprecher.name, ziel.name)
        spur.schreiben(pcm)

    def beenden(self) -> tuple[str, ...]:
        """Schließt die Spuren und reiht sie ein; leere Spuren bleiben nicht liegen."""
        meldungen = []
        for user_id, spur in self._spuren.items():
            spur.schliessen()
            if not spur.bytes:
                spur.pfad.unlink()
                continue
            recordings.enqueue(
                self._config.database_path,
                self.session_id,
                spur.pfad.name,
                discord_user_id=user_id,
            )
            meldungen.append(
                f"Spur »{spur.pfad.stem}« → Sitzung {self.session_id}, wartet auf den Stapel."
            )
        self._spuren.clear()
        self._angesagt = False
        return tuple(meldungen) or (NICHTS_GESPROCHEN,)


async def starten(config: Config, stimme: Stimme) -> Aufnahme:
    """Ansage spielen, Einwilligung protokollieren, dann erst mitschneiden."""
    sitzung = notes.latest_session(config.database_path)
    if sitzung is None:
        raise AufnahmeFehler(OHNE_SITZUNG)
    gesprochen = ansage.datei(config.recordings_dir)

    aufnahme = Aufnahme(config, sitzung.id, stimme.kanal)
    await stimme.ansagen(gesprochen)
    aufnahme.ansage_protokollieren(stimme.mitglieder())
    stimme.mitschneiden(aufnahme)
    return aufnahme


async def nachzuegler(
    config: Config, stimme: Stimme, aufnahme: Aufnahme, wer: consent.Member
) -> int:
    """Wer später dazukommt, hört dieselbe Ansage noch einmal — und steht im Protokoll.

    Das ist die ehrlichere Hälfte der Wahl: bloß zu vermerken, dass jemand die Ansage
    verpasst hat, hielte fest, dass er nicht eingewilligt hat, statt ihn zu fragen.
    """
    kennung = aufnahme.ansage_protokollieren((wer,), art=consent.NACHZUEGLER)
    await stimme.ansagen(ansage.datei(config.recordings_dir))
    return kennung


async def stoppen(stimme: Stimme, aufnahme: Aufnahme) -> tuple[str, ...]:
    stimme.mitschnitt_beenden()
    await stimme.trennen()
    return aufnahme.beenden()
