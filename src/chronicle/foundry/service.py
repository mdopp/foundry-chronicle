"""Abgleich und Auskunft.

Ist Foundry nicht erreichbar, bleibt der letzte Stand stehen und der Grund wird
mitgespeichert — angezeigt wird dann beides. Eine leere Liste ohne Erklärung wäre ein
kaputtes Protokoll, kein Zustand.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from chronicle import db, settings
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.foundry.client import FoundryClient, FoundryError
from chronicle.foundry.model import SyncState, WorldSnapshot
from chronicle.foundry.world import project
from chronicle.runde import Runde

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _open(config: Config, runde: Runde) -> db.Scope:
    db.init(config.database_path)
    return db.scoped(runde)


def _umfang(snapshot: WorldSnapshot) -> str:
    return (
        f"{len(snapshot.players)} Spieler, {len(snapshot.characters)} Charaktere, "
        f"{len(snapshot.messages)} Chat-Nachrichten"
    )


def _state(snapshot: WorldSnapshot | None, reason: str | None, at: str | None) -> SyncState:
    if reason is None:
        if snapshot is None:
            return SyncState(message="Noch kein Abgleich mit Foundry gelaufen.")
        return SyncState(
            message=f"Stand vom {snapshot.fetched_at} — {_umfang(snapshot)}.",
            snapshot=snapshot,
        )
    if snapshot is None:
        return SyncState(
            message=(
                f"Foundry war beim letzten Versuch um {at} nicht erreichbar: {reason}. "
                "Es liegt noch kein Stand vor."
            ),
            stale=True,
        )
    return SyncState(
        message=(
            f"Foundry war beim letzten Versuch um {at} nicht erreichbar: {reason}. "
            f"Angezeigt wird der Stand vom {snapshot.fetched_at} — {_umfang(snapshot)}."
        ),
        stale=True,
        snapshot=snapshot,
    )


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


def sync(config: Config, runde: Runde, *, client: FoundryClient | None = None) -> SyncState:
    zeitpunkt = _now()
    scope = _open(config, runde)
    try:
        try:
            zugang = settings.effective(config, runde)
            user_id, raw = (client or FoundryClient(zugang)).fetch_world()
        except FoundryError as fehler:
            grund = str(fehler)
            logger.warning("Foundry-Abgleich fehlgeschlagen: %s", grund)
            store.record_failure(scope, grund, zeitpunkt)
            return _state(store.load(scope), grund, zeitpunkt)
        snapshot = project(raw, user_id, fetched_at=zeitpunkt)
        store.save(scope, snapshot)
        logger.info("Foundry-Abgleich fertig: %s", _umfang(snapshot))
        return _state(snapshot, None, None)
    finally:
        scope.close()
