"""Die Kette, die nachts von allein läuft — abholen, verschriften, abgleichen, schreiben.

Der Knopf in der Oberfläche und der Stapelaufruf bleiben, was sie sind: der Weg für
»jetzt sofort«. Der Normalfall ist diese Kette, und sie ruft dieselben Funktionen auf —
ein dritter Auslöser, kein dritter Weg.

Sie läuft **im Webdienst**, in einem eigenen Faden, und nur dort. Der Aufnahme-Bot schiede
aus, denn ohne Bot-Token gibt es ihn gar nicht, eine Präsenzgruppe hat trotzdem Aufnahmen
zu verschriften und Chroniken zu schreiben; ein dritter Container wäre eine Betriebseinheit
mehr für einen Faden, der einmal je Minute auf die Uhr sieht. (Die frühere Fassung nannte
noch einen schärferen Grund: ``chronicle.jobs`` habe einen abgestürzten Lauf nur erkennen
können, solange **ein** Prozess Zeilen anlegt. Die Vorlage hielt das nie ein — der Bot legt
ebenfalls welche an —, und seit #178 hängt die Erkennung an Besitzer und Herzschlag in der
Zeile statt an der Merkliste eines Prozesses.)

Ein verpasstes Fenster wird nicht nachgeholt. War der Server um vier Uhr aus, ist das
Material am nächsten Morgen so alt wie vorher — ein Lauf um zehn Uhr vormittags brächte
niemandem etwas und stünde mitten im Arbeitstag auf der Maschine. Die nächste Nacht
genügt, und in den Einstellungen steht das auch so.

Verpasst heißt aber »niemand hat hingesehen«, nicht »die Maschine war besetzt«. Bei
mehreren Runden mit derselben Uhrzeit — der Vorgabe — kommt nur eine sofort dran; die
übrigen fielen aus ihrem Fenster, sobald die erste Nacht länger als eine Stunde dauerte,
und zwar wegen der festen Reihenfolge jede Nacht dieselben (#180). Wer fällig war und die
Maschine besetzt vorfand, wird deshalb **vorgemerkt** und behält seinen Anspruch bis zur
nächsten angesetzten Zeit. Der Vermerk lebt im Speicher dieses Fadens und nirgends sonst:
startet der Dienst neu, war eben doch niemand da, der zugesehen hätte, und dann gilt
wieder die Regel darüber.

Die angesetzte Zeit gilt in der **Zone der Runde**, nicht in der des Prozesses. Der
Container läuft auf der Box in UTC, und das bleibt auch so: eine Instanz trägt mehrere
Runden, ein ``TZ`` im Pod könnte immer nur einer davon recht geben. Gerechnet wird
deshalb über ``ZoneInfo`` — das leitet den Versatz aus der Wanduhrzeit ab, sodass 04:00
im Sommer wie im Winter 04:00 heißt.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from chronicle import db, jobs, register, settings, zugang
from chronicle import runde as runden
from chronicle.compose.service import KIND, compose_session, recap_session
from chronicle.config import Config
from chronicle.discord.ausgabe import anhaengen
from chronicle.discord.rueckblick import deliver
from chronicle.discord.service import LEER as BRIEFKASTEN_LEER
from chronicle.discord.service import run as diktat_abholen
from chronicle.foundry.service import sync
from chronicle.runde import Runde
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
OHNE_PASSWORT = (
    "Kein Foundry-Passwort im Speicher — die Zahlen wurden nicht geholt. Der Abgleich "
    "gehört ans Sitzungsende, solange Foundry ohnehin noch läuft."
)
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


def _diktat(config: Config, runde: Runde) -> Schritt:
    meldungen = diktat_abholen(config, runde)
    return Schritt(DIKTAT, " ".join(meldungen) if meldungen else BRIEFKASTEN_LEER)


def _transkript(config: Config, runde: Runde) -> Schritt:
    meldungen = run_queue(config, runde)
    return Schritt(TRANSKRIPT, " ".join(meldungen) if meldungen else WARTESCHLANGE_LEER)


def _abgleich(config: Config, runde: Runde) -> Schritt:
    if not settings.effective(config, runde).foundry_configured:
        return Schritt(ABGLEICH, OHNE_FOUNDRY)
    # Das Passwort ist flüchtig, und der Nachtlauf kann keines erfragen. Dass keines
    # daliegt, ist der Normalfall und kein Fehlschlag — deshalb ein Satz statt roter Farbe.
    if not zugang.ist_gemerkt(runde):
        return Schritt(ABGLEICH, OHNE_PASSWORT)
    zustand = sync(config, runde)
    return Schritt(ABGLEICH, zustand.message, gelungen=not zustand.stale)


def offen(runde: Runde) -> tuple[tuple[int, str], ...]:
    """Sitzungen, deren Material jünger ist als ihre Chronik — und solche ohne eine.

    Alle Zeitpunkte sind gleich geformte ISO-Zeichenketten, ein Vergleich Zeichen für
    Zeichen ist deshalb ein Vergleich der Zeit.
    """
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT s.id, s.played_on, "
            "(SELECT MAX(n.updated_at) FROM note n JOIN scene c ON n.scene_id = c.id "
            " WHERE c.session_id = s.id) AS notiz, "
            "(SELECT MAX(t.created_at) FROM transcript t WHERE t.session_id = s.id) AS spur, "
            "(SELECT p.created_at FROM protocol p WHERE p.session_id = s.id AND p.kind = ?) "
            " AS chronik "
            "FROM session s WHERE s.runde_id = ? ORDER BY s.id",
            (KIND, scope.runde_id),
        ).fetchall()
    finally:
        scope.close()

    faellig = []
    for zeile in zeilen:
        material = max((wert for wert in (zeile["notiz"], zeile["spur"]) if wert), default=None)
        if material is None:
            continue
        if zeile["chronik"] is None or material > zeile["chronik"]:
            faellig.append((int(zeile["id"]), str(zeile["played_on"])))
    return tuple(faellig)


def _chronik(config: Config, runde: Runde) -> Schritt:
    faellig = offen(runde)
    if not faellig:
        return Schritt(CHRONIK, NICHTS_ZU_SCHREIBEN)
    meldungen = []
    for sitzung_id, datum in faellig:
        compose_session(config, runde, sitzung_id)
        recap_session(config, runde, sitzung_id)
        deliver(config, runde, sitzung_id)
        ausgabe = anhaengen(config, runde, sitzung_id)
        register.suggest(config, runde, sitzung_id)
        meldungen.append(GESCHRIEBEN.format(datum=datum) + (f" {ausgabe}" if ausgabe else ""))
    return Schritt(CHRONIK, " ".join(meldungen))


def lauf(config: Config, runde: Runde) -> str:
    """Die Kette in ihrer Reihenfolge; jeder Schritt meldet für seine eigene Karte."""
    schritte = (
        _sammeln(DIKTAT, lambda: _diktat(config, runde)),
        _sammeln(TRANSKRIPT, lambda: _transkript(config, runde)),
        _sammeln(ABGLEICH, lambda: _abgleich(config, runde)),
        _sammeln(CHRONIK, lambda: _chronik(config, runde)),
    )
    return json.dumps(
        [{"name": s.name, "text": s.text, "gelungen": s.gelungen} for s in schritte],
        ensure_ascii=False,
    )


def _ortszeit(zeitstempel: str, zone: str) -> datetime:
    return datetime.fromisoformat(zeitstempel).astimezone(ZoneInfo(zone))


def letzter(runde: Runde) -> Lauf | None:
    """Der jüngste nächtliche Lauf, aufgeschlüsselt nach Schritten."""
    job = jobs.latest(runde, jobs.NACHTLAUF)
    if job is None:
        return None
    schritte = {}
    if job.result:
        schritte = {
            eintrag["name"]: Schritt(eintrag["name"], eintrag["text"], eintrag["gelungen"])
            for eintrag in json.loads(job.result)
        }
    return Lauf(
        zeitpunkt=_ortszeit(job.started_at, settings.nightly_zone(runde)).strftime(
            "%d.%m.%Y um %H:%M"
        ),
        schritte=schritte,
        laeuft=job.laeuft,
        fehler=job.error,
    )


def ziel(jetzt: datetime, geplant: str, zone: str) -> datetime:
    """Die angesetzte Zeit des heutigen Tages — in der Zone der Runde.

    Der Wechsel in die Zone ist der ganze Punkt: erst danach heißt ``hour=4`` vier Uhr für
    die Leute, die die Einstellung gesetzt haben.
    """
    stunde = settings.nightly_at(geplant)
    return jetzt.astimezone(ZoneInfo(zone)).replace(
        hour=stunde.hour, minute=stunde.minute, second=0, microsecond=0
    )


def faellig(
    jetzt: datetime,
    geplant: str,
    zuletzt: datetime | None,
    zone: str,
    vermerk: datetime | None = None,
) -> bool:
    """Ist die angesetzte Zeit erreicht und die Nacht noch nicht gelaufen?

    Verglichen wird über Zeitpunkte, nicht über Wanduhrzeiten — beide Seiten tragen ihren
    Versatz. ``vermerk`` ist die angesetzte Zeit, für die diese Runde schon einmal fällig
    war und die Maschine besetzt vorfand: dann läuft ihr Fenster nicht ab, denn sie hat
    ihre Nacht nicht verpasst, sondern nicht bekommen.
    """
    heute = ziel(jetzt, geplant, zone)
    if jetzt < heute:
        return False
    if jetzt - heute > FENSTER and vermerk != heute:
        return False
    return zuletzt is None or zuletzt < heute


# Runden, die fällig waren und die Maschine besetzt vorfanden, je mit der angesetzten Zeit,
# für die sie warten. Nur im Speicher — siehe den Kopf dieser Datei.
_vorgemerkt: dict[int, datetime] = {}


def _naechste(config: Config, jetzt: datetime) -> list[tuple[Runde, datetime, datetime | None]]:
    faellige = []
    for eine in runden.alle(config.database_path):
        # Eine verabschiedete Runde bekommt keine Nacht mehr. Dieser Faden läuft im
        # Webdienst und weiß vom Rauswurf nur hier: ohne diese Zeile verschriftete er
        # dreißig Tage lang weiter, was eine Gruppe längst widerrufen hat.
        if eine.gesperrt:
            _vorgemerkt.pop(eine.id, None)
            continue
        zone = settings.nightly_zone(eine)
        geplant = settings.nightly_time(eine)
        vorher = jobs.latest(eine, jobs.NACHTLAUF)
        zuletzt = None if vorher is None else _ortszeit(vorher.started_at, zone)
        if not faellig(jetzt, geplant, zuletzt, zone, _vorgemerkt.get(eine.id)):
            _vorgemerkt.pop(eine.id, None)
            continue
        faellige.append((eine, ziel(jetzt, geplant, zone), zuletzt))
    # Wer am längsten auf eine Nacht wartet, ist zuerst dran. An der Id festzuhalten hieße
    # bei gleicher Uhrzeit, dass jede Nacht dieselbe Runde gewinnt.
    nie = datetime.min.replace(tzinfo=UTC)
    faellige.sort(key=lambda eintrag: (eintrag[2] or nie, eintrag[0].id))
    return faellige


@runden.instanzweit
def tick(config: Config, *, jetzt: datetime | None = None) -> jobs.Job | None:
    """Ein Blick auf die Uhr — der Test dreht sie von Hand weiter.

    Jede Runde hat ihre eigene Uhrzeit und ihren eigenen Lauf; die Maschine bleibt eine.
    Es beginnt deshalb höchstens einer. Wer dabei leer ausgeht, wird vorgemerkt und
    verliert sein Fenster nicht — sonst bekäme bei gleicher Uhrzeit die zweite Runde nie
    eine Nacht.
    """
    jetzt = datetime.now().astimezone() if jetzt is None else jetzt
    for eine, angesetzt, _zuletzt in _naechste(config, jetzt):
        # Ein laufender Lauf — auch der von Hand angestoßene — schreibt dieselben Zeilen.
        if not jobs.running(eine):
            logger.info("Nachtlauf der Runde %s beginnt", eine.name)
            angestossen = jobs.start(
                config, eine, jobs.NACHTLAUF, lambda dran=eine: lauf(config, dran)
            )
            if angestossen is not None:
                _vorgemerkt.pop(eine.id, None)
                return angestossen
        _vorgemerkt[eine.id] = angesetzt
    return None


@runden.instanzweit
def betreiben(config: Config, *, schlafen=time.sleep, weiter=lambda: True) -> None:
    while weiter():
        try:
            tick(config)
        # Ein Fehler beim Blick auf die Uhr darf den Faden nicht beenden — sonst liefe
        # der Dienst weiter und nachts nie wieder etwas.
        except Exception as fehler:  # noqa: BLE001
            logger.warning("Nachtlauf konnte nicht angestoßen werden: %s", fehler)
        schlafen(INTERVALL)


@runden.instanzweit
def starten(config: Config) -> threading.Thread:
    faden = threading.Thread(target=betreiben, args=(config,), daemon=True, name="nachtlauf")
    faden.start()
    return faden
