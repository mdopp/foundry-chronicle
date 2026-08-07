"""Abgleich und Auskunft.

Ist Foundry nicht erreichbar, bleibt der letzte Stand stehen und der Grund wird
mitgespeichert — angezeigt wird dann beides. Eine leere Liste ohne Erklärung wäre ein
kaputtes Protokoll, kein Zustand.

Zwei Dinge holt sich der Abgleich nicht aus der Datenbank: das **Passwort** (es lebt im
Arbeitsspeicher und wird hier verbraucht) und die **Welt-Kennung** des Servers. Die zweite
entscheidet vor dem Speichern, ob dieser Server überhaupt die Welt dieser Runde zeigt.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from chronicle import db, settings, zugang
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.foundry.client import FoundryClient, FoundryError
from chronicle.foundry.model import NICHT_MEHR_VORHANDEN, SyncState, World, WorldSnapshot
from chronicle.foundry.world import identity, project
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

NICHT_ERREICHBAR = "Foundry war nicht erreichbar: {grund}"

ANDERE_WELT = (
    "Dieser Foundry-Server zeigt gerade eine andere Welt: erwartet war »{erwartet}«, "
    "offen ist »{gefunden}«. Es wurde nichts übernommen — sonst stünde das Chat-Log einer "
    "fremden Kampagne in dieser Chronik. Entweder die richtige Welt laden und noch einmal "
    "abgleichen, oder diese Runde ausdrücklich auf die neue Welt umhängen."
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _open(config: Config, runde: Runde) -> db.Scope:
    db.init(config.database_path)
    return db.scoped(runde)


def _umfang(snapshot: WorldSnapshot) -> str:
    verschwunden = sum(1 for n in snapshot.messages if n.vanished_at)
    umfang = (
        f"{len(snapshot.players)} Spieler, {len(snapshot.characters)} Charaktere, "
        f"{len(snapshot.messages)} Chat-Nachrichten"
    )
    if verschwunden:
        umfang += f", davon {verschwunden} {NICHT_MEHR_VORHANDEN}"
    return umfang


def _state(snapshot: WorldSnapshot | None, reason: str | None, at: str | None) -> SyncState:
    """Der gemerkte Grund steht als ganzer Satz in der Meldung.

    Nicht erreichbar ist nur einer von zwei Gründen — der andere ist die falsche Welt, und
    die als »nicht erreichbar« zu melden wäre gelogen. Der Rahmen sagt deshalb nur *wann*,
    den Grund bringt der Grund mit.
    """
    if reason is None:
        if snapshot is None:
            return SyncState(message="Noch kein Abgleich mit Foundry gelaufen.")
        return SyncState(
            message=f"Stand vom {snapshot.fetched_at} — {_umfang(snapshot)}.",
            snapshot=snapshot,
        )
    kopf = f"Der Abgleich um {at} ging nicht durch. {reason}"
    if snapshot is None:
        return SyncState(message=f"{kopf} Es liegt noch kein Stand vor.", stale=True)
    return SyncState(
        message=(
            f"{kopf} Angezeigt wird der Stand vom {snapshot.fetched_at} — {_umfang(snapshot)}."
        ),
        stale=True,
        snapshot=snapshot,
    )


def _falsche_welt(gebunden: World | None, gefunden: World) -> bool:
    """Nur ein belegter Widerspruch verweigert den Abgleich.

    Eine Welt ohne Kennung — ältere Foundry-Stände, ein Mock, ein Dump ohne ``world`` —
    lässt sich nicht vergleichen. Sie wird nicht als Wechsel gewertet: eine Verweigerung
    ohne Beleg brächte den Abgleich zum Erliegen, statt vor etwas zu schützen.
    """
    if gebunden is None or not gebunden.id or not gefunden.id:
        return False
    return gebunden.id != gefunden.id


def current(config: Config, runde: Runde) -> SyncState:
    scope = _open(config, runde)
    try:
        return _state(store.load(scope), *store.last_failure(scope))
    finally:
        scope.close()


def failed(config: Config, runde: Runde) -> bool:
    """Ob der letzte Abgleich gescheitert ist — ohne den Stand zu laden.

    Das Band auf den Arbeitsseiten fragt das bei jedem Seitenaufruf; ``current`` würde
    dafür jedes Mal die ganze Welt aus der SQLite holen.
    """
    scope = _open(config, runde)
    try:
        return store.last_failure(scope)[0] is not None
    finally:
        scope.close()


def sync(
    config: Config,
    runde: Runde,
    *,
    passwort: str | None = None,
    umhaengen: bool = False,
    client: FoundryClient | None = None,
) -> SyncState:
    """Ein Abgleich. Das Passwort ist flüchtig, die Welt-Kennung wird geprüft.

    ``passwort`` schlägt den Merkzettel; ohne beides gibt es eine Meldung statt eines
    Versuchs. Verbraucht wird es so oder so — auch ein gescheiterter Handschlag lässt
    keines liegen, und der nächste Versuch fragt neu.

    ``umhaengen`` bindet die Runde an die Welt, die dieser Server gerade zeigt. Das ist der
    ausdrückliche Weg für eine Runde, die wirklich in einer neuen Welt weitergeht.
    """
    zeitpunkt = _now()
    scope = _open(config, runde)
    try:
        try:
            geheim = passwort or zugang.passwort(runde)
            wirksam = settings.effective(config, runde)
            user_id, raw = (client or FoundryClient(wirksam, geheim)).fetch_world()
        except FoundryError as fehler:
            grund = NICHT_ERREICHBAR.format(grund=fehler)
            logger.warning("Foundry-Abgleich fehlgeschlagen: %s", fehler)
            store.record_failure(scope, grund, zeitpunkt)
            return _state(store.load(scope), grund, zeitpunkt)
        finally:
            # Ein Abgleich verbraucht das Passwort — auch der gescheiterte. Sonst läge es
            # nach einem »Foundry ist aus« bis zur harten Frist herum.
            zugang.vergiss(runde)
        gefunden = identity(raw)
        gebunden = store.world(scope)
        if not umhaengen and _falsche_welt(gebunden, gefunden):
            grund = ANDERE_WELT.format(erwartet=gebunden.title, gefunden=gefunden.title)
            logger.warning("Foundry zeigt eine andere Welt: %s", gefunden.id)
            store.record_failure(scope, grund, zeitpunkt)
            return _state(store.load(scope), grund, zeitpunkt)
        store.save(scope, project(raw, user_id, fetched_at=zeitpunkt))
        store.bind_world(scope, gefunden)
        # Gemeldet wird der Bestand, nicht die Lieferung: die Nachrichten sind ein Archiv,
        # und ein Abgleich, der »3 Chat-Nachrichten« meldet, während 200 gespeichert sind,
        # zählte das Falsche.
        snapshot = store.load(scope)
        logger.info("Foundry-Abgleich fertig: %s", _umfang(snapshot))
        return _state(snapshot, None, None)
    finally:
        scope.close()
