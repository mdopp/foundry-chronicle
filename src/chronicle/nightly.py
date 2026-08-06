"""Die Kette, die nachts von allein läuft — abholen, verschriften, abgleichen, schreiben.

Der Knopf in der Oberfläche und der Stapelaufruf bleiben, was sie sind: der Weg für
»jetzt sofort«. Der Normalfall ist diese Kette, und sie ruft dieselben Funktionen auf —
ein dritter Auslöser, kein dritter Weg.

Sie läuft **im Webdienst**, in einem eigenen Faden. Der Aufnahme-Bot schiede aus, denn
ohne Bot-Token gibt es ihn gar nicht, eine Präsenzgruppe hat trotzdem Aufnahmen zu
verschriften und Chroniken zu schreiben. Ein dritter Container schiede ebenfalls aus,
und zwar aus einem schärferen Grund als »eine Betriebseinheit mehr«: ``chronicle.jobs``
erkennt einen abgestürzten Lauf daran, dass eine ``laeuft``-Zeile in der Datenbank steht,
die der eigene Prozess nicht in seiner Merkliste hat. Diese Invariante trägt nur, solange
**ein** Prozess Job-Zeilen anlegt. Ein zweiter Anleger machte aus jedem laufenden Lauf
des einen Prozesses einen »unterbrochenen« im Auge des anderen.

Ein verpasstes Fenster wird nicht nachgeholt. War der Server um vier Uhr aus, ist das
Material am nächsten Morgen so alt wie vorher — ein Lauf um zehn Uhr vormittags brächte
niemandem etwas und stünde mitten im Arbeitstag auf der Maschine. Die nächste Nacht
genügt, und in den Einstellungen steht das auch so.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from chronicle import db, jobs, register, settings
from chronicle.compose.service import KIND, compose_session, recap_session
from chronicle.config import Config
from chronicle.discord.rueckblick import deliver
from chronicle.discord.service import LEER as BRIEFKASTEN_LEER
from chronicle.discord.service import run as diktat_abholen
from chronicle.foundry.service import sync
from chronicle.transcribe.service import run_queue

logger = logging.getLogger(__name__)

DIKTAT = "diktat"
TRANSKRIPT = "transkript"
ABGLEICH = "abgleich"
CHRONIK = "chronik"

# So oft wird auf die Uhr gesehen. Feiner brächte nichts: der Lauf ist auf die Minute
# genau angesetzt, nicht auf die Sekunde.
INTERVALL = 60.0

# Wie lange nach der angesetzten Zeit ein Lauf noch beginnen darf. Wer den Server um
# 04:20 einschaltet, bekommt seine Nacht noch; wer ihn erst mittags einschaltet, nicht.
FENSTER = timedelta(hours=1)

OHNE_FOUNDRY = "Kein Zugang zu Foundry — die Zahlen wurden nicht geholt."
NICHTS_ZU_SCHREIBEN = "Keine Sitzung mit neuem Material — es blieb alles, wie es war."
WARTESCHLANGE_LEER = "Nichts in der Warteschlange."
NICHT_DURCHGEKOMMEN = "Nicht durchgekommen: {grund}"

GESCHRIEBEN = "Sitzung vom {datum}: Chronik und Rückblick geschrieben."


@dataclass(frozen=True)
class Schritt:
    name: str
    text: str
    gelungen: bool = True


@dataclass(frozen=True)
class Lauf:
    """Was aus der letzten Nacht in den Einstellungen zu lesen ist."""

    zeitpunkt: str
    schritte: dict[str, Schritt]
    laeuft: bool = False
    fehler: str | None = None

    @property
    def diktat(self) -> Schritt | None:
        return self.schritte.get(DIKTAT)

    @property
    def transkript(self) -> Schritt | None:
        return self.schritte.get(TRANSKRIPT)

    @property
    def abgleich(self) -> Schritt | None:
        return self.schritte.get(ABGLEICH)

    @property
    def chronik(self) -> Schritt | None:
        return self.schritte.get(CHRONIK)


def _sammeln(name: str, arbeit) -> Schritt:
    """Ein gescheiterter Schritt nimmt die übrigen nicht mit — die Nacht hat vier davon."""
    try:
        return arbeit()
    except Exception as fehler:  # noqa: BLE001
        logger.warning("Nachtlauf, Schritt %s: %s", name, fehler)
        return Schritt(name, NICHT_DURCHGEKOMMEN.format(grund=fehler), gelungen=False)


def _diktat(config: Config) -> Schritt:
    meldungen = diktat_abholen(config)
    return Schritt(DIKTAT, " ".join(meldungen) if meldungen else BRIEFKASTEN_LEER)


def _transkript(config: Config) -> Schritt:
    meldungen = run_queue(config)
    return Schritt(TRANSKRIPT, " ".join(meldungen) if meldungen else WARTESCHLANGE_LEER)


def _abgleich(config: Config) -> Schritt:
    if not settings.effective(config).foundry_configured:
        return Schritt(ABGLEICH, OHNE_FOUNDRY)
    zustand = sync(config)
    return Schritt(ABGLEICH, zustand.message, gelungen=not zustand.stale)


def offen(database_path: Path) -> tuple[tuple[int, str], ...]:
    """Sitzungen, deren Material jünger ist als ihre Chronik — und solche ohne eine.

    Alle Zeitpunkte sind gleich geformte ISO-Zeichenketten, ein Vergleich Zeichen für
    Zeichen ist deshalb ein Vergleich der Zeit.
    """
    connection = db.connect(database_path)
    try:
        zeilen = connection.execute(
            "SELECT s.id, s.played_on, "
            "(SELECT MAX(n.updated_at) FROM note n JOIN scene c ON n.scene_id = c.id "
            " WHERE c.session_id = s.id) AS notiz, "
            "(SELECT MAX(t.created_at) FROM transcript t WHERE t.session_id = s.id) AS spur, "
            "(SELECT p.created_at FROM protocol p WHERE p.session_id = s.id AND p.kind = ?) "
            " AS chronik "
            "FROM session s ORDER BY s.id",
            (KIND,),
        ).fetchall()
    finally:
        connection.close()

    faellig = []
    for zeile in zeilen:
        material = max((wert for wert in (zeile["notiz"], zeile["spur"]) if wert), default=None)
        if material is None:
            continue
        if zeile["chronik"] is None or material > zeile["chronik"]:
            faellig.append((int(zeile["id"]), str(zeile["played_on"])))
    return tuple(faellig)


def _chronik(config: Config) -> Schritt:
    faellig = offen(config.database_path)
    if not faellig:
        return Schritt(CHRONIK, NICHTS_ZU_SCHREIBEN)
    meldungen = []
    for sitzung_id, datum in faellig:
        compose_session(config, sitzung_id)
        recap_session(config, sitzung_id)
        deliver(config, sitzung_id)
        register.suggest(config, sitzung_id)
        meldungen.append(GESCHRIEBEN.format(datum=datum))
    return Schritt(CHRONIK, " ".join(meldungen))


def lauf(config: Config) -> str:
    """Die Kette in ihrer Reihenfolge; jeder Schritt meldet für seine eigene Karte."""
    schritte = (
        _sammeln(DIKTAT, lambda: _diktat(config)),
        _sammeln(TRANSKRIPT, lambda: _transkript(config)),
        _sammeln(ABGLEICH, lambda: _abgleich(config)),
        _sammeln(CHRONIK, lambda: _chronik(config)),
    )
    return json.dumps(
        [{"name": s.name, "text": s.text, "gelungen": s.gelungen} for s in schritte],
        ensure_ascii=False,
    )


def _ortszeit(zeitstempel: str) -> datetime:
    return datetime.fromisoformat(zeitstempel).astimezone()


def letzter(database_path: Path) -> Lauf | None:
    """Der jüngste nächtliche Lauf, aufgeschlüsselt nach Schritten."""
    job = jobs.latest(database_path, jobs.NACHTLAUF)
    if job is None:
        return None
    schritte = {}
    if job.result:
        schritte = {
            eintrag["name"]: Schritt(eintrag["name"], eintrag["text"], eintrag["gelungen"])
            for eintrag in json.loads(job.result)
        }
    return Lauf(
        zeitpunkt=_ortszeit(job.started_at).strftime("%d.%m.%Y um %H:%M"),
        schritte=schritte,
        laeuft=job.laeuft,
        fehler=job.error,
    )


def faellig(jetzt: datetime, geplant: str, zuletzt: datetime | None) -> bool:
    """Ist die angesetzte Zeit erreicht und die Nacht noch nicht gelaufen?"""
    stunde = settings.nightly_at(geplant)
    ziel = jetzt.replace(hour=stunde.hour, minute=stunde.minute, second=0, microsecond=0)
    if jetzt < ziel or jetzt - ziel > FENSTER:
        return False
    return zuletzt is None or zuletzt < ziel


def tick(config: Config, *, jetzt: datetime | None = None) -> jobs.Job | None:
    """Ein Blick auf die Uhr — der Test dreht sie von Hand weiter."""
    jetzt = datetime.now().astimezone() if jetzt is None else jetzt
    vorher = jobs.latest(config.database_path, jobs.NACHTLAUF)
    zuletzt = None if vorher is None else _ortszeit(vorher.started_at)
    if not faellig(jetzt, settings.nightly_time(config.database_path), zuletzt):
        return None
    # Ein laufender Lauf — auch der von Hand angestoßene — schreibt dieselben Zeilen.
    # Der nächste Blick in derselben Stunde versucht es wieder.
    if jobs.running(config.database_path):
        return None
    logger.info("Nachtlauf beginnt")
    return jobs.start(config, jobs.NACHTLAUF, lambda: lauf(config))


def betreiben(config: Config, *, schlafen=time.sleep, weiter=lambda: True) -> None:
    while weiter():
        try:
            tick(config)
        # Ein Fehler beim Blick auf die Uhr darf den Faden nicht beenden — sonst liefe
        # der Dienst weiter und nachts nie wieder etwas.
        except Exception as fehler:  # noqa: BLE001
            logger.warning("Nachtlauf konnte nicht angestoßen werden: %s", fehler)
        schlafen(INTERVALL)


def starten(config: Config) -> threading.Thread:
    faden = threading.Thread(target=betreiben, args=(config,), daemon=True, name="nachtlauf")
    faden.start()
    return faden
