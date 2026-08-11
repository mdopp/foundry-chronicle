"""Der Lebenszyklus einer Runde: beanspruchen, sperren, wiederholen, löschen.

Eine Runde beginnt mit einer Einladung und endet mit einem Rauswurf. Dazwischen liegt
genau eine Zusage, und sie ist der Grund für dieses Modul:

* **Verlässt der Bot die Gilde, ist die Runde sofort still.** Nicht erst nach der Frist —
  ab dem Ereignis wird nichts mehr abgelegt und nichts mehr herausgegeben. Wer den Bot
  hinauswirft, hat damit eine Aussage gemacht, und die gilt sofort. Sofort heißt in jedem
  Faden: der nächtliche Stapel überspringt sie, Verschriften, Komponieren und der
  Foundry-Abgleich weigern sich (``ruht``), und ein noch im Speicher liegendes
  Foundry-Passwort ist mit dem Rauswurf vergessen.
* **Nach dreißig Tagen ist sie fort.** Vollständig: Sitzungen, Notizen, Transkripte,
  Aufnahmen samt Dateien, Chroniken, Register, Zuordnung — und die Einwilligungsprotokolle.
* **Innerhalb der Frist bringt eine erneute Einladung sie zurück.** Danach nicht mehr: eine
  Runde, deren Tag vorbei ist, wird beim Wiedersehen gelöscht statt wiederbelebt, auch wenn
  der tägliche Lauf noch nicht bei ihr war. Das steht vorher da, nicht hinterher.

**Die Einwilligungsprotokolle gehen mit** — die eine Entscheidung dieses Moduls, die weh
tut, und sie ist nicht bequem. Was sie belegen, ist *wer* dabei war; anonymisiert belegen
sie nichts mehr und wären bloß noch ein personenbezogener Rest ohne Zweck. Übrig bliebe
eine Liste von Namen und Kanälen über Menschen, die mit dieser Instanz nichts mehr zu tun
haben, geführt von einem Betreiber, den sie nicht mehr kennen.

**Was dabei verloren geht, wird nicht schöngeredet:** die Aufnahme ist fort, die Chronik
aus ihr aber liegt längst in einem Discord-Kanal, und dorthin reicht kein Löschlauf. Das
Abgeleitete überdauert also den Beleg, dass angesagt wurde. Wir nehmen das in Kauf, weil
die Alternative — den Nachweis über Jahre auf fremder Hardware vorzuhalten — für dieselben
Menschen die schlechtere ist. Wer den Beleg braucht, holt ihn sich vor dem Löschen; die
Löschfrage sagt das, statt es im Kleingedruckten zu lassen.

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
from zoneinfo import ZoneInfo

from chronicle import db, settings, zugang
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
AUF_VERLANGEN = "Runde »{name}«: auf Verlangen von {wer} vollständig gelöscht."
ZURUECK = "Runde »{name}«: wieder freigegeben."

# Was eine ruhende Runde einem Lauf antwortet, der trotzdem an sie herantritt.
RUHT = "Diese Runde ruht — es wird nichts mehr geholt und nichts mehr abgelegt."


def _now() -> datetime:
    return datetime.now(UTC)


def _stempel(zeitpunkt: datetime) -> str:
    return zeitpunkt.isoformat(timespec="seconds")


def frist_datum(runde: Runde) -> str:
    """Das zugesagte Datum in der Form, in der es einem Menschen gesagt wird.

    In der Zone der Runde und nicht in der des Containers: der läuft auf der Box in UTC,
    und ein Datum, das um zwei Stunden danebenliegt, ist als Zusage eines Tages falsch.
    """
    if not runde.delete_after:
        return ""
    zone = ZoneInfo(settings.nightly_zone(runde))
    return datetime.fromisoformat(runde.delete_after).astimezone(zone).strftime("%d.%m.%Y")


def _abgelaufen(runde: Runde, jetzt: datetime | None = None) -> bool:
    """Ob der zugesagte Tag vorbei ist. Was fällig ist, kommt nicht mehr zurück."""
    if not runde.delete_after:
        return False
    return runde.delete_after <= _stempel(_now() if jetzt is None else jetzt)


def ruht(runde: Runde) -> bool:
    """Ob diese Runde schweigt — gegen den Stand von jetzt, nicht gegen den mitgereichten.

    Ein Lauf hält seine ``Runde`` stunden- oder tagelang in der Hand; der Rauswurf
    dazwischen steht nur in der Datenbank. Gefragt wird deshalb dort, und eine Runde, die
    es nicht mehr gibt, schweigt erst recht.
    """
    frisch = runden.get(runde.database_path, runde.id)
    return frisch is None or frisch.gesperrt


@runden.instanzweit
def beanspruchen(config: Config, guild_id: str, name: str) -> Runde:
    """Die Runde dieser Gilde — vorhandene übernehmen, sonst eine anlegen.

    Eine gesperrte Runde wird dabei wieder freigegeben: wer den Bot zurückholt, holt seine
    Chronik zurück, solange sie noch da ist. Genau das ist der Sinn der Frist — und genau
    bis zu ihrem Ende. Eine abgelaufene wird hier gelöscht und nicht wiederbelebt: dass der
    tägliche Lauf noch nicht bei ihr war, ist kein Grund, ein Versprechen zurückzunehmen.
    """
    vorhanden = runden.fuer_gilde(config.database_path, str(guild_id))
    if vorhanden is not None and _abgelaufen(vorhanden):
        loeschen(config, vorhanden)
        vorhanden = None
    if vorhanden is None:
        return runden.anlegen(config.database_path, name, guild_id=str(guild_id))
    if vorhanden.gesperrt:
        return freigeben(config.database_path, vorhanden)
    return vorhanden


@runden.instanzweit
def sperren(database_path: Path, guild_id: str) -> Runde | None:
    """Sofort still, Löschung auf den Kalender. Ein zweiter Rauswurf verschiebt nichts."""
    vorhanden = runden.fuer_gilde(database_path, str(guild_id))
    if vorhanden is None or vorhanden.gesperrt:
        return vorhanden
    # Zuerst das Passwort: es lebt zwölf Stunden weiter, und ein Abgleich nach dem Rauswurf
    # zöge die Welt einer Gruppe, die gerade widerrufen hat.
    zugang.vergiss(vorhanden)
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
def wiedereinladung(config: Config, guild_id: str) -> Runde | None:
    """Der Bot ist zurück — war die Runde gesperrt, ist sie wieder da. Sonst nichts.

    Angelegt wird hier nichts: eine Gilde, die den Bot zum ersten Mal einlädt, bekommt
    ihre Runde beim Einrichten und nicht beim Betreten. Und eine, deren Frist abgelaufen
    ist, kommt nicht zurück, sondern geht jetzt — begrüßt wird dann wie beim ersten Mal.
    """
    vorhanden = runden.fuer_gilde(config.database_path, str(guild_id))
    if vorhanden is None or not vorhanden.gesperrt:
        return None
    if _abgelaufen(vorhanden):
        loeschen(config, vorhanden)
        return None
    return freigeben(config.database_path, vorhanden)


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
    """Die Tondateien dieser Runde — die eingereihten und die, die es nie wurden.

    Die Zeile allein genügt nicht: eine Spur wird während der ganzen Sitzung auf die Platte
    geschrieben und erst am Ende eingereiht. Ein Absturz oder ein Rauswurf mittendrin
    hinterlässt fertige Aufnahmen ohne Zeile, die sonst niemand mehr fände. Gesucht wird
    deshalb auch im Verzeichnis, und zwar über die Sitzungen dieser Runde: der Dateiname
    trägt die Sitzungskennung, und die gehört genau einer Runde.
    """
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT filename FROM recording WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
        sitzungen = scope.execute(
            "SELECT id FROM session WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
    finally:
        scope.close()
    gefunden = {config.recordings_dir / zeile["filename"] for zeile in zeilen}
    for sitzung in sitzungen:
        gefunden.update(config.recordings_dir.glob(f"sitzung{int(sitzung['id'])}-*"))
    return tuple(sorted(gefunden))


def loeschen(config: Config, runde: Runde, *, veranlasst_von: str | None = None) -> str:
    """Alles dieser Runde — die Dateien auf der Platte und die Zeilen in der Datenbank.

    Die Audiodateien zuerst: verschwände die Zeile vorher, wüsste niemand mehr, welche
    Datei zu löschen war, und eine Stunde fremder Stimmen bliebe auf der Platte liegen.

    ``veranlasst_von`` steht in der Logzeile: eine Löschung auf Knopfdruck ist die
    folgenschwerste Handlung dieses Systems, und wer sie ausgelöst hat, ist danach nicht
    mehr zu ermitteln — die Runde, in der es stünde, ist ja fort.

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
    meldung = (
        GELOESCHT.format(name=runde.name, tage=FRIST_TAGE)
        if veranlasst_von is None
        else AUF_VERLANGEN.format(name=runde.name, wer=veranlasst_von)
    )
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
        try:
            sweep(config)
        # Eine gesperrte Datei, ein »database is locked« — und ohne diese Zeile endete der
        # Faden, das Löschen hörte für **alle** Runden auf, und niemand merkte es. Am
        # nächsten Tag wird es wieder versucht.
        except Exception as fehler:  # noqa: BLE001
            logger.warning("Die Löschfrist konnte nicht durchgesetzt werden: %s", fehler)
        await schlafen(SWEEP_ABSTAND)
