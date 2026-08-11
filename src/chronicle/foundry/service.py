"""Abgleich und Auskunft.

Ist Foundry nicht erreichbar, bleibt der letzte Stand stehen und der Grund wird
mitgespeichert — angezeigt wird dann beides. Eine leere Liste ohne Erklärung wäre ein
kaputtes Protokoll, kein Zustand.

Zwei Dinge holt sich der Abgleich nicht aus der Datenbank: das **Passwort** (es lebt im
Arbeitsspeicher und wird hier verbraucht) und die **Welt-Kennung** des Servers. Die zweite
entscheidet vor dem Speichern, ob dieser Server überhaupt die Welt dieser Runde zeigt.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from chronicle import db, lebenszyklus, settings, zugang
from chronicle.config import Config
from chronicle.foundry import store, testwelt
from chronicle.foundry.client import FoundryClient, FoundryError
from chronicle.foundry.model import NICHT_MEHR_VORHANDEN, SyncState, World, WorldSnapshot
from chronicle.foundry.world import identity, project
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

NICHT_ERREICHBAR = "Foundry war nicht erreichbar: {grund}"

ABZUG_GESCHRIEBEN = (
    "Der Weltabzug liegt in {ziel} — {groesse} KB. Er trägt die Klarnamen aller Konten "
    "dieser Welt und gehört nie ins Repo. Für eine eincheckbare Fixture erst durch "
    "scripts/anonymisiere_welt.py schicken."
)

# Der Abzug ist personenbezogen und landet deshalb immer im selben, gitignorierten Ordner:
# ein frei wählbarer Pfad hieße, dass ein Tippfehler ihn neben den Quelltext legt, wo ihn
# keine Ignore-Regel mehr fängt.
ABZUG_ORDNER = Path("dumps")
ABZUG_ZIEL = ABZUG_ORDNER / "welt-dump.json"

# Ein Wortlaut für alle Stellen: Band, Karte und Abgleichsmeldung sagen denselben Satz.
# Zwei Formulierungen wären zwei Gelegenheiten, eine davon zu übersehen.
TESTWELT_STAND = (
    "{hinweis} Eingespielt aus der mitgelieferten Fixture: {umfang}. Es war kein Server im Spiel."
)

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


def _testwelt(scope: db.Scope, zeitpunkt: str) -> SyncState:
    """Die eingebaute Welt durch dieselbe Strecke — nur ohne Netz davor.

    Gebunden wird die Runde an sie **nicht**: die Testwelt ist keine Kampagne, und eine
    Bindung an sie machte das Zurückschalten auf den echten Server zu einem Weltwechsel,
    den der Abgleich dann verweigern müsste.
    """
    user_id, raw = testwelt.abzug()
    store.save(scope, project(raw, user_id, fetched_at=zeitpunkt))
    snapshot = store.load(scope)
    logger.info("Testwelt eingespielt: %s", _umfang(snapshot))
    return SyncState(
        message=TESTWELT_STAND.format(hinweis=testwelt.HINWEIS, umfang=_umfang(snapshot)),
        snapshot=snapshot,
    )


def abzug(
    config: Config,
    runde: Runde,
    ziel: Path,
    *,
    passwort: str | None = None,
    client: FoundryClient | None = None,
) -> str:
    """Der rohe Weltabzug in eine Datei — derselbe Handschlag, kein Filter, kein Speicher.

    Was hier herauskommt, ist die ungefilterte Antwort des Servers: die Klarnamen aller
    Konten, Journale, Charakterbiografien. Sie geht deshalb bewusst **nicht** durch
    ``project`` und **nicht** in die SQLite — sie ist Rohstoff für den Anonymisierer und
    sonst nichts.
    """
    geheim = passwort or zugang.passwort(runde)
    wirksam = settings.effective(config, runde)
    try:
        _user_id, raw = (client or FoundryClient(wirksam, geheim)).fetch_world()
    finally:
        zugang.vergiss(runde)
    ziel.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ``mode`` beim Anlegen ist von der umask beschnitten und wird ganz übergangen, wenn
    # das Verzeichnis schon steht — ein vorhandenes 0755 bliebe sonst offen.
    ziel.parent.chmod(0o700)
    # Erst die Rechte, dann der Inhalt: eine Datei mit Klarnamen darf nicht einmal
    # kurzzeitig für alle lesbar dastehen.
    ziel.touch(mode=0o600, exist_ok=True)
    ziel.chmod(0o600)
    ziel.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return ABZUG_GESCHRIEBEN.format(ziel=ziel, groesse=ziel.stat().st_size // 1024)


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
        # Ein Abgleich verbraucht das Passwort — der gescheiterte ebenso wie der, der gar
        # nicht erst zu einem Server geht. Deshalb umschließt das ``vergiss`` auch die
        # beiden Abkürzungen darunter: sonst läge eine Eingabe nach »ruhende Runde« oder
        # »Testwelt« bis zur harten Frist im Speicher.
        try:
            # Nach dem Rauswurf wird bei dieser Gruppe nichts mehr geholt — auch nicht mit
            # einem Passwort, das noch im Speicher liegt. Die Sperre steht vor der
            # Quellwahl: auch die Testwelt schriebe in die Runde, und eine ruhende Runde
            # bekommt nichts mehr, gleich woher es käme.
            if lebenszyklus.ruht(runde):
                return SyncState(message=lebenszyklus.RUHT, stale=True)
            if settings.foundry_quelle(runde) == settings.TESTWELT:
                return _testwelt(scope, zeitpunkt)
            try:
                geheim = passwort or zugang.passwort(runde)
                wirksam = settings.effective(config, runde)
                user_id, raw = (client or FoundryClient(wirksam, geheim)).fetch_world()
            except FoundryError as fehler:
                grund = NICHT_ERREICHBAR.format(grund=fehler)
                logger.warning("Foundry-Abgleich fehlgeschlagen: %s", fehler)
                store.record_failure(scope, grund, zeitpunkt)
                return _state(store.load(scope), grund, zeitpunkt)
            gefunden = identity(raw)
            gebunden = store.world(scope)
            if not umhaengen and _falsche_welt(gebunden, gefunden):
                grund = ANDERE_WELT.format(erwartet=gebunden.title, gefunden=gefunden.title)
                logger.warning("Foundry zeigt eine andere Welt: %s", gefunden.id)
                store.record_failure(scope, grund, zeitpunkt)
                return _state(store.load(scope), grund, zeitpunkt)
            store.save(scope, project(raw, user_id, fetched_at=zeitpunkt))
            store.bind_world(scope, gefunden)
            # Gemeldet wird der Bestand, nicht die Lieferung: die Nachrichten sind ein
            # Archiv, und ein Abgleich, der »3 Chat-Nachrichten« meldet, während 200
            # gespeichert sind, zählte das Falsche.
            snapshot = store.load(scope)
            logger.info("Foundry-Abgleich fertig: %s", _umfang(snapshot))
            return _state(snapshot, None, None)
        finally:
            zugang.vergiss(runde)
    finally:
        scope.close()
