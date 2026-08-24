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
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chronicle import db, lebenszyklus, notes, settings, zugang
from chronicle.config import Config
from chronicle.foundry import mitschnitt, store, systems, testwelt
from chronicle.foundry.client import FoundryClient, FoundryError
from chronicle.foundry.model import (
    NICHT_MEHR_VORHANDEN,
    Ereignisse,
    SyncState,
    World,
    WorldSnapshot,
)
from chronicle.foundry.world import (
    fehlende_listen,
    fuer_die_gruppe,
    identity,
    kernversion,
    project,
)
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

STROM_OHNE_PASSWORT = (
    "Für den Blick nach Foundry liegt kein Passwort mehr bereit — ich sehe ab jetzt nicht "
    "mehr nach. Beim Abschluss werde ich noch einmal danach fragen."
)

NICHT_WIEDERERKANNT = (
    "Foundry hat geantwortet, aber die Antwort sieht nicht aus wie eine Welt, die ich lesen "
    "kann: {fehlend} fehlt darin oder steht an anderer Stelle.{kern} Es wurde nichts "
    "übernommen — der letzte Stand bleibt stehen, und keine Zahl gilt als verschwunden. So "
    "sieht es aus, wenn der Server auf eine neue Foundry-Hauptversion gehoben wurde; dann "
    "muss die Anbindung nachgezogen werden, bevor wieder Zahlen ankommen."
)

ANDERE_WELT = (
    "Dieser Foundry-Server zeigt gerade eine andere Welt: erwartet war »{erwartet}«, "
    "offen ist »{gefunden}«. Es wurde nichts übernommen — sonst stünde das Chat-Log einer "
    "fremden Kampagne in dieser Chronik. Entweder die richtige Welt laden und noch einmal "
    "abgleichen, oder diese Runde ausdrücklich auf die neue Welt umhängen."
)


# Foundry stempelt eine Chat-Nachricht mit ``Date.now()``: Millisekunden seit der Epoche,
# in UTC. Die Trennlinien der Szenen stehen als ISO-Zeichenkette derselben Zone in der
# Datenbank. Ohne diese Umrechnung verglichen wir eine Zahl mit einem Text — und zwar
# still, denn SQLite vergleicht beides klaglos und gibt nie den Treffer, den man meinte.
EPOCHE = datetime(1970, 1, 1, tzinfo=UTC)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _augenblick(timestamp: int) -> str:
    """Foundrys Zeitstempel im Format der Szenen-Trennlinien — Sekunden, UTC."""
    return (EPOCHE + timedelta(milliseconds=timestamp)).isoformat(timespec="seconds")


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


def _nicht_wiedererkannt(raw: Mapping) -> str | None:
    """Der fertige Satz, wenn diese Antwort keine lesbare Welt ist — sonst ``None``."""
    fehlend = fehlende_listen(raw)
    if not fehlend:
        return None
    version = kernversion(raw)
    return NICHT_WIEDERERKANNT.format(
        fehlend=", ".join(fehlend),
        kern="" if version is None else f" Der Server meldet Foundry-Kern {version}.",
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

    Ihre Nachrichten gehen mit ``testwelt=True`` in den Speicher — dort tragen sie ihre
    Herkunft und bleiben vom Archiv der Runde getrennt. Gemeldet wird deshalb hier die
    **Lieferung** und nicht der Bestand: der Satz sagt, was aus der Fixture kam, und
    zählte sonst die echten Nachrichten der Runde in eine erfundene Welt hinein.
    """
    user_id, raw = testwelt.abzug()
    eingespielt = project(raw, user_id, fetched_at=zeitpunkt)
    store.save(scope, eingespielt, testwelt=True)
    logger.info("Testwelt eingespielt: %s", _umfang(eingespielt))
    return SyncState(
        message=TESTWELT_STAND.format(hinweis=testwelt.HINWEIS, umfang=_umfang(eingespielt)),
        snapshot=store.load(scope),
    )


def geschuetzt(ziel: Path) -> Path:
    """Eine leere Datei, die nur uns gehört — für alles, was ungefiltert vom Server kommt."""
    ziel.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ``mode`` beim Anlegen ist von der umask beschnitten und wird ganz übergangen, wenn
    # das Verzeichnis schon steht — ein vorhandenes 0755 bliebe sonst offen.
    ziel.parent.chmod(0o700)
    # Erst die Rechte, dann der Inhalt: eine Datei mit Klarnamen darf nicht einmal
    # kurzzeitig für alle lesbar dastehen.
    ziel.touch(mode=0o600, exist_ok=True)
    ziel.chmod(0o600)
    return ziel


def _mitschreiben(config: Config, runde: Runde, user_id: str, raw: Mapping) -> None:
    """Das Bild dieses Blicks aufheben — wenn der Betrieb es eingeschaltet hat (#242).

    Aufgehoben wird die **rohe** Antwort und nicht das, was der Adapter daraus destilliert.
    Genau daran scheiterte #242: die Fixtures trugen, was der Adapter erwartete, und der
    echte Server etwas anderes. Aus einem Mitschnitt lässt sich der Abend ohne Foundry
    nachspielen — ``mitschnitt.Wiedergabe`` ist der Server dazu.

    Standardmäßig aus, weil hier nichts von selbst anfangen soll, Personenbezogenes
    dauerhaft wegzuschreiben. Die Zeile im Log nennt die Zahl der Bilder und die Datei,
    nie einen Inhalt.
    """
    if not config.foundry_mitschnitt:
        return
    datei = geschuetzt(mitschnitt.ziel(ABZUG_ORDNER, runde.id))
    mitschnitt.schreibe(datei, user_id, raw)
    logger.info("Mitschnitt: ein Bild nach %s", datei)


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
    geschuetzt(ziel)
    ziel.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return ABZUG_GESCHRIEBEN.format(ziel=ziel, groesse=ziel.stat().st_size // 1024)


def beobachten(
    config: Config,
    runde: Runde,
    *,
    gesehen: frozenset[str] = frozenset(),
    client: FoundryClient | None = None,
) -> Ereignisse:
    """Ein Blick in die laufende Welt — derselbe Weg wie der Abgleich, nur ohne Verbrauch.

    Der eine Unterschied zu ``sync`` ist zugleich der Grund für diese zweite Funktion: ein
    Strom, der das Passwort verbraucht, sieht genau **einmal** nach. Gemerkt wird deshalb
    nichts Neues und nichts länger — gelesen wird der Zettel aus #96, und ist er abgelaufen
    oder hat der Abschluss ihn eingelöst, endet der Strom von selbst. Nichts liegt dadurch
    länger als die zwölf Stunden aus #64, und gespeichert wird nach wie vor nichts.

    Gefiltert wird zweimal, und beides **vor** dem Speicher. ``project`` entscheidet über
    die Berechtigungsstufe des angemeldeten Kontos, was ins Archiv geht — das ist die Frage
    »darf dieses Konto das sehen«, und für die Chronik ist sie die richtige. Für den Thread
    ist sie die falsche: dort liest die **ganze Gruppe** mit. Was ihn erreicht, entscheidet
    deshalb ``fuer_die_gruppe`` enger — ein Geflüster an unser Konto und ein blinder Wurf
    bleiben draußen, gleich wie hoch das Konto steht, mit dem wir angemeldet sind.

    Voll bleibt dabei allein das **Archiv** (``foundry_message``). Die frühere Fassung
    sagte, eingeengt sei nur der Weg in den Thread und nicht der in die Chronik — das war
    ungenau (#219): die Chronik liest ``scene_foundry_message``, und dort steht nur, was
    diesen engeren Filter passiert hat. Hier im Strom wie beim Nachtrag des Abschlusses.

    Der Fehlschlag wird **nicht** als Abgleichsfehler vermerkt: der Strom sieht zu, er
    führt den Stand nicht. Wer den letzten Abgleich beurteilen will, liest den des
    Abgleichs — ein Vermerk je Minute machte daraus ein Rauschen.
    """
    zeitpunkt = _now()
    if lebenszyklus.ruht(runde):
        return Ereignisse(grund=lebenszyklus.RUHT, weiter=False)
    # Die eingebaute Testwelt ist ein Abzug und kein Server: dort passiert nichts, während
    # gespielt wird, und ein Strom davor sähe für immer dasselbe.
    if settings.foundry_quelle(runde) == settings.TESTWELT:
        return Ereignisse(weiter=False)
    geheim = zugang.passwort(runde)
    if geheim is None:
        return Ereignisse(grund=STROM_OHNE_PASSWORT, weiter=False)
    wirksam = settings.effective(config, runde)
    try:
        user_id, raw = (client or FoundryClient(wirksam, geheim)).fetch_world()
    except FoundryError as fehler:
        logger.warning("Ereignisstrom: Foundry nicht erreichbar: %s", fehler)
        return Ereignisse(grund=NICHT_ERREICHBAR.format(grund=fehler))
    _mitschreiben(config, runde, user_id, raw)
    grund = _nicht_wiedererkannt(raw)
    if grund is not None:
        logger.warning("Ereignisstrom: Weltantwort nicht wiedererkannt: %s", fehlende_listen(raw))
        return Ereignisse(grund=grund, weiter=False)
    scope = _open(config, runde)
    try:
        gefunden = identity(raw)
        gebunden = store.world(scope)
        if _falsche_welt(gebunden, gefunden):
            logger.warning("Ereignisstrom: Foundry zeigt eine andere Welt: %s", gefunden.id)
            return Ereignisse(
                grund=ANDERE_WELT.format(erwartet=gebunden.title, gefunden=gefunden.title),
                weiter=False,
            )
        schnappschuss = project(raw, user_id, fetched_at=zeitpunkt)
        store.save(scope, schnappschuss)
        store.bind_world(scope, gefunden)
    finally:
        scope.close()
    fuer_alle = fuer_die_gruppe(raw)
    neu = sorted(
        (
            n
            for n in schnappschuss.messages
            if systems.lohnt(n) and n.id not in gesehen and n.id in fuer_alle
        ),
        key=lambda n: (n.timestamp, n.id),
    )
    logger.info("Ereignisstrom: %d neue Ereignisse", len(neu))
    return Ereignisse(neu=tuple(neu))


def _nachtragen(runde: Runde, session_id: int, raw: Mapping, schnappschuss: WorldSnapshot) -> int:
    """Die Zahlen dieses Abends nachträglich an ihre Szenen hängen (#219).

    Bis hierher war der Ereignisstrom (#93/#97) der **einzige** Schreiber von
    ``scene_foundry_message`` — und nur was dort steht, wird in der Chronik zur Tatsache.
    Wer beim Sitzungsstart kein Passwort hinterlegte, spielte damit einen Abend, an dessen
    Ende der Abgleich zwar das ganze Chat-Log holte, die Chronik aber keine einzige Zahl
    trug. #93 sagt ausdrücklich zu, dass man beim Start weglassen und beim Abschluss neu
    gefragt werden darf; für die Aufnahme stimmte das, für die Zahlen nicht.

    Drei Dinge halten den Nachtrag auf der sicheren Seite:

    * **Dieselbe engere Sicht wie im Strom** — ``fuer_die_gruppe`` und nicht
      ``message_visible``. Die zweite fragt, ob *dieses Konto* etwas sehen darf, und ist
      fürs Archiv die richtige; mit einem GM-Zugang (#78) brächte sie hier durch die
      Hintertür ein, was der Strom draußen hält: geflüsterte und blinde Würfe.
    * **Nichts doppelt.** Lief der Strom, hängen die Würfe schon. Sie ein zweites Mal zu
      hängen hieße, dieselbe Zahl in zwei Szenen zu schreiben — ``INSERT OR IGNORE`` fängt
      das nicht, denn der Schlüssel ist Szene *und* Nachricht.
    * **Keine Auffanglinie** (``notes.scene_of_moment``): das Chat-Log trägt die ganze
      Kampagne, und ein Wurf, der in keine Szene dieser Sitzung fällt, wird nicht
      untergebracht, sondern gar nicht.

    Der Rohabzug wird gebraucht, weil ``fuer_die_gruppe`` an ihm hängt und nicht am
    Gespeicherten: was geflüstert war, steht im Archiv ohne diesen Vermerk.
    """
    fuer_alle = fuer_die_gruppe(raw)
    schon = notes.verknuepfte_foundry_ereignisse(runde)
    getan = 0
    for nachricht in schnappschuss.messages:
        if nachricht.id in schon or nachricht.id not in fuer_alle:
            continue
        if not systems.lohnt(nachricht):
            continue
        szene = notes.scene_of_moment(runde, session_id, _augenblick(nachricht.timestamp))
        if szene is not None and notes.link_foundry_message(runde, szene, nachricht.id):
            getan += 1
    logger.info("Nachtrag: %d Foundry-Ereignisse einer Sitzung zugeordnet", getan)
    return getan


def sync(
    config: Config,
    runde: Runde,
    *,
    passwort: str | None = None,
    umhaengen: bool = False,
    session_id: int | None = None,
    client: FoundryClient | None = None,
) -> SyncState:
    """Ein Abgleich. Das Passwort ist flüchtig, die Welt-Kennung wird geprüft.

    ``passwort`` schlägt den Merkzettel; ohne beides gibt es eine Meldung statt eines
    Versuchs. Verbraucht wird es so oder so — auch ein gescheiterter Handschlag lässt
    keines liegen, und der nächste Versuch fragt neu.

    ``umhaengen`` bindet die Runde an die Welt, die dieser Server gerade zeigt. Das ist der
    ausdrückliche Weg für eine Runde, die wirklich in einer neuen Welt weitergeht.

    ``session_id`` hängt die geholten Würfe zusätzlich an die Szenen dieser Sitzung
    (``_nachtragen``). Der Abschluss gibt sie mit, der Abgleich für sich nicht: ein
    Abgleich hat keine Sitzung, und die Zuordnung ist keine Nebenwirkung des Holens.
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
            _mitschreiben(config, runde, user_id, raw)
            # Vor der Weltfrage und vor allem Schreiben: eine Antwort, die wir nicht
            # wiedererkennen, wird nicht als leere Welt verbucht (#284).
            grund = _nicht_wiedererkannt(raw)
            if grund is not None:
                logger.warning("Weltantwort nicht wiedererkannt: %s", fehlende_listen(raw))
                store.record_failure(scope, grund, zeitpunkt)
                return _state(store.load(scope), grund, zeitpunkt)
            gefunden = identity(raw)
            gebunden = store.world(scope)
            if not umhaengen and _falsche_welt(gebunden, gefunden):
                grund = ANDERE_WELT.format(erwartet=gebunden.title, gefunden=gefunden.title)
                logger.warning("Foundry zeigt eine andere Welt: %s", gefunden.id)
                store.record_failure(scope, grund, zeitpunkt)
                return _state(store.load(scope), grund, zeitpunkt)
            schnappschuss = project(raw, user_id, fetched_at=zeitpunkt)
            store.save(scope, schnappschuss)
            store.bind_world(scope, gefunden)
            nachgetragen = (
                0 if session_id is None else _nachtragen(runde, session_id, raw, schnappschuss)
            )
            # Gemeldet wird der Bestand, nicht die Lieferung: die Nachrichten sind ein
            # Archiv, und ein Abgleich, der »3 Chat-Nachrichten« meldet, während 200
            # gespeichert sind, zählte das Falsche.
            snapshot = store.load(scope)
            logger.info("Foundry-Abgleich fertig: %s", _umfang(snapshot))
            return replace(_state(snapshot, None, None), nachgetragen=nachgetragen)
        finally:
            zugang.vergiss(runde)
    finally:
        scope.close()
