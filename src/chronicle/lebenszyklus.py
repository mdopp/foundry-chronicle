"""Der Lebenszyklus einer Runde: beanspruchen, sperren, wiederholen, löschen.

Eine Runde beginnt mit einer Einladung und endet mit einem Rauswurf. Dazwischen liegt
genau eine Zusage, und sie ist der Grund für dieses Modul:

* **Verlässt der Bot die Gilde, ist die Runde sofort still.** Nicht erst nach der Frist —
  ab dem Ereignis wird nichts mehr abgelegt und nichts mehr herausgegeben. Wer den Bot
  hinauswirft, hat damit eine Aussage gemacht, und die gilt sofort.
* **Nach dreißig Tagen ist sie fort.** Vollständig: Sitzungen, Notizen, Transkripte,
  Aufnahmen samt Dateien, Chroniken, Register, Zuordnung — und die Einwilligungsprotokolle.
* **Innerhalb der Frist bringt eine erneute Einladung sie zurück.** Danach nicht mehr, und
  das steht vorher da, nicht hinterher.

**Die Einwilligungsprotokolle gehen mit.** Sie sind der heikle Fall, denn sie belegen, dass
angesagt wurde. Sie bleiben trotzdem nicht liegen, aus zwei Gründen: Was sie belegen, ist
*wer* dabei war — anonymisiert belegen sie nichts mehr und wären bloß noch ein
personenbezogener Rest ohne Zweck. Und sie verteidigen gegen einen Vorwurf zu einer
Aufnahme, die es dann nicht mehr gibt; der Nachweis überlebt seinen Gegenstand nicht.
Übrig bliebe eine Liste von Namen und Kanälen über Menschen, die mit dieser Instanz nichts
mehr zu tun haben. Also: mit löschen.

Gelöscht wird über die Liste, die auch die Schranke der Datenschicht definiert
(``db.GESCOPTE_TABELLEN``, per Fremdschlüssel-Kaskade an der ``runde``-Zeile). Eine zweite
Liste liefe ihr davon, und eine vergessene Tabelle wäre hier kein Schönheitsfehler, sondern
ein Bestand, den jemand für gelöscht hält.

Dieses Modul ist **instanzweit**: es arbeitet auf der ``runde``-Tabelle, dem einen
Verzeichnis, das keiner Runde gehört. Wo es Zeilen einer Runde anfasst, tut es das über
einen Scope wie alle anderen auch.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chronicle import db, zugang
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

# Die Frist zwischen Rauswurf und Löschung. Keine Umgebungsvariable: sie ist eine Zusage an
# eine Gruppe, deren Chronik auf fremder Hardware liegt — dieselbe Sorte Zahl wie
# ``recordings.RETENTION_TAGE``, und aus demselben Grund im Code.
FRIST_TAGE = 30

# Einmal am Tag nachsehen genügt für eine Frist in Tagen — dieselbe Taktung wie beim
# Aufräumen der Aufnahmen, und derselbe dauerhafte Prozess trägt beide.
SWEEP_ABSTAND = 24 * 60 * 60

GESPERRT = "Runde »{name}«: gesperrt, Löschung am {datum}."
GELOESCHT = "Runde »{name}«: nach {tage} Tagen vollständig gelöscht."
ZURUECK = "Runde »{name}«: wieder freigegeben."


def _now() -> datetime:
    return datetime.now(UTC)


def _stempel(zeitpunkt: datetime) -> str:
    return zeitpunkt.isoformat(timespec="seconds")


def frist_datum(runde: Runde) -> str:
    """Das zugesagte Datum in der Form, in der es einem Menschen gesagt wird."""
    if not runde.delete_after:
        return ""
    return datetime.fromisoformat(runde.delete_after).astimezone().strftime("%d.%m.%Y")


@runden.instanzweit
def beanspruchen(database_path: Path, guild_id: str, name: str) -> Runde:
    """Die Runde dieser Gilde — vorhandene übernehmen, sonst eine anlegen.

    Eine gesperrte Runde wird dabei wieder freigegeben: wer den Bot zurückholt, holt seine
    Chronik zurück, solange sie noch da ist. Genau das ist der Sinn der Frist.
    """
    vorhanden = runden.fuer_gilde(database_path, str(guild_id))
    if vorhanden is None:
        return runden.anlegen(database_path, name, guild_id=str(guild_id))
    if vorhanden.gesperrt:
        return freigeben(database_path, vorhanden)
    return vorhanden


@runden.instanzweit
def sperren(database_path: Path, guild_id: str) -> Runde | None:
    """Sofort still, Löschung auf den Kalender. Ein zweiter Rauswurf verschiebt nichts."""
    vorhanden = runden.fuer_gilde(database_path, str(guild_id))
    if vorhanden is None or vorhanden.gesperrt:
        return vorhanden
    jetzt = _now()
    _schreiben(
        database_path,
        vorhanden.id,
        _stempel(jetzt),
        _stempel(jetzt + timedelta(days=FRIST_TAGE)),
    )
    gesperrt = runden.get(database_path, vorhanden.id)
    logger.info("%s", GESPERRT.format(name=gesperrt.name, datum=frist_datum(gesperrt)))
    return gesperrt


@runden.instanzweit
def wiedereinladung(database_path: Path, guild_id: str) -> Runde | None:
    """Der Bot ist zurück — war die Runde gesperrt, ist sie wieder da. Sonst nichts.

    Angelegt wird hier nichts: eine Gilde, die den Bot zum ersten Mal einlädt, bekommt
    ihre Runde beim Einrichten und nicht beim Betreten.
    """
    vorhanden = runden.fuer_gilde(database_path, str(guild_id))
    if vorhanden is None or not vorhanden.gesperrt:
        return None
    return freigeben(database_path, vorhanden)


@runden.instanzweit
def freigeben(database_path: Path, runde: Runde) -> Runde:
    _schreiben(database_path, runde.id, None, None)
    zurueck = runden.get(database_path, runde.id)
    logger.info("%s", ZURUECK.format(name=zurueck.name))
    return zurueck


def _schreiben(
    database_path: Path, runde_id: int, locked_at: str | None, delete_after: str | None
) -> None:
    connection = db.connect(database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE runde SET locked_at = ?, delete_after = ? WHERE id = ?",
                (locked_at, delete_after, runde_id),
            )
    finally:
        connection.close()


@runden.instanzweit
def faellig(database_path: Path, *, jetzt: datetime | None = None) -> tuple[Runde, ...]:
    """Die Runden, deren zugesagte Frist abgelaufen ist."""
    grenze = _stempel(_now() if jetzt is None else jetzt)
    return tuple(
        eine
        for eine in runden.alle(database_path)
        if eine.delete_after and eine.delete_after <= grenze
    )


def _dateien(config: Config, runde: Runde) -> tuple[Path, ...]:
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT filename FROM recording WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
    finally:
        scope.close()
    return tuple(config.recordings_dir / zeile["filename"] for zeile in zeilen)


def loeschen(config: Config, runde: Runde) -> str:
    """Alles dieser Runde — die Dateien auf der Platte und die Zeilen in der Datenbank.

    Die Audiodateien zuerst: verschwände die Zeile vorher, wüsste niemand mehr, welche
    Datei zu löschen war, und eine Stunde fremder Stimmen bliebe auf der Platte liegen.

    Der Suchindex danach ausdrücklich. Er hängt nicht am Fremdschlüssel — eine
    fts5-Tabelle kennt keine —, und die Löschtrigger der Quelltabellen feuern bei einer
    Kaskade nicht. Ohne diese Zeile stünden die Notizen einer gelöschten Runde weiter im
    Index.
    """
    for datei in _dateien(config, runde):
        datei.unlink(missing_ok=True)

    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute("DELETE FROM search_index WHERE runde_id = ?", (scope.runde_id,))
    finally:
        scope.close()

    connection = db.connect(runde.database_path)
    try:
        with connection:
            connection.execute("DELETE FROM runde WHERE id = ?", (runde.id,))
    finally:
        connection.close()

    zugang.vergiss(runde)
    meldung = GELOESCHT.format(name=runde.name, tage=FRIST_TAGE)
    logger.info("%s", meldung)
    return meldung


@runden.instanzweit
def sweep(config: Config, *, jetzt: datetime | None = None) -> tuple[str, ...]:
    """Die Frist durchsetzen. Beliebig oft aufrufbar — was fort ist, ist nicht mehr fällig."""
    return tuple(loeschen(config, eine) for eine in faellig(config.database_path, jetzt=jetzt))


@runden.instanzweit
async def taeglich(config: Config, *, schlafen=asyncio.sleep) -> None:
    """Dieselbe Taktung wie beim Aufräumen der Aufnahmen, im selben dauerhaften Prozess.

    Zwei Fristen, zwei Läufe: die eine gilt jeder Audiospur auf dieser Box, die andere
    einer verabschiedeten Runde. Sie miteinander zu verheiraten hieße, dass ein Fehler in
    der einen die andere mitnimmt.
    """
    while True:
        sweep(config)
        await schlafen(SWEEP_ABSTAND)
