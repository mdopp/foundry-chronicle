"""Der Stapellauf: lesen, komponieren, ablegen.

Die Fakten hängen über ``scene_foundry_message`` an der Szene — ohne Zuordnung trägt
die Chronik allein die Notizen. Das ist der erwartete Normalfall einer Präsenzrunde und
kein Fehler: eine Zuordnung zu erraten wäre bereits Erfinden.

Ein zweiter Lauf ersetzt das Protokoll der Sitzung; es gibt je Sitzung genau eine
Chronik und genau einen Rückblick. Der Rückblick liest die abgelegte Chronik, nicht das
Rohmaterial — er ist der zweite Ausgang derselben Komposition.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from chronicle import db, lebenszyklus, settings
from chronicle.compose import client
from chronicle.compose.client import TextModel
from chronicle.compose.composer import (
    Composition,
    Notiz,
    SceneMaterial,
    SessionMaterial,
    compose,
)
from chronicle.compose.nacherzaehlung import (
    Abschnitt,
    ErzaehlStoff,
    Nacherzaehlung,
    nacherzaehlen,
)
from chronicle.compose.recap import Recap, RecapMaterial, recap
from chronicle.compose.zwischenstand import Zwischenstand, zwischenstand
from chronicle.config import Config
from chronicle.foundry import store
from chronicle.runde import Runde
from chronicle.transcribe.merge import TRANSKRIPT

KIND = "chronik"
RUECKBLICK = "rueckblick"

# So viele frühere Rückblicke gehen in den Aufruf: genug für den Faden über mehrere
# Sitzungen, wenig genug für ein Kontextfenster.
FRUEHERE = 3

# Der Bereich läuft über die gespielte Reihenfolge, nicht über die Kennung: zwei Sitzungen
# desselben Tages trennt erst die Id, und nachgetragene Abende bekämen sonst ihren Platz
# danach, wo sie hingehören, aber im Bereich nicht mehr vorkämen.
BEREICH = (
    "SELECT s.id, s.played_on, s.title, p.text FROM session s "
    "LEFT JOIN protocol p ON p.session_id = s.id AND p.runde_id = s.runde_id AND p.kind = ? "
    "WHERE s.runde_id = ? "
    "AND (s.played_on > ? OR (s.played_on = ? AND s.id >= ?)) "
    "AND (s.played_on < ? OR (s.played_on = ? AND s.id <= ?)) "
    "ORDER BY s.played_on, s.id"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def material(scope: db.Scope, session_id: int) -> SessionMaterial | None:
    kopf = scope.execute(
        "SELECT id, played_on, title FROM session WHERE runde_id = ? AND id = ?",
        (scope.runde_id, session_id),
    ).fetchone()
    if kopf is None:
        return None
    szenen = scope.execute(
        "SELECT id, position, title FROM scene WHERE runde_id = ? AND session_id = ? "
        "ORDER BY position",
        (scope.runde_id, session_id),
    ).fetchall()
    notizen = scope.execute(
        "SELECT n.scene_id, n.text, n.origin FROM note n JOIN scene c ON n.scene_id = c.id "
        "WHERE n.runde_id = ? AND c.session_id = ? ORDER BY n.id",
        (scope.runde_id, session_id),
    ).fetchall()
    fakten = scope.execute(
        "SELECT v.scene_id AS scene_id, m.* FROM scene_foundry_message v "
        "JOIN scene c ON c.id = v.scene_id "
        "JOIN foundry_message m ON m.id = v.message_id AND m.runde_id = v.runde_id "
        "WHERE v.runde_id = ? AND c.session_id = ? ORDER BY m.timestamp, m.id",
        (scope.runde_id, session_id),
    ).fetchall()

    je_szene_notizen: dict[int, list[Notiz]] = {}
    for zeile in notizen:
        # Nur die Herkunft aus den Spuren heißt »verschriftet«. Ein eingelesenes
        # Notizdokument trägt ebenfalls ein ``origin``, ist aber geschriebenes Wort —
        # es käme unter der Aufnahme-Überschrift als Fehlauskunft heraus.
        je_szene_notizen.setdefault(zeile["scene_id"], []).append(
            Notiz(text=zeile["text"], verschriftet=zeile["origin"] == TRANSKRIPT)
        )
    je_szene_fakten: dict[int, list] = {}
    for zeile in fakten:
        je_szene_fakten.setdefault(zeile["scene_id"], []).append(store.message(zeile))

    return SessionMaterial(
        session_id=kopf["id"],
        played_on=kopf["played_on"],
        title=kopf["title"],
        scenes=tuple(
            SceneMaterial(
                position=s["position"],
                title=s["title"],
                notes=tuple(je_szene_notizen.get(s["id"], ())),
                facts=tuple(je_szene_fakten.get(s["id"], ())),
            )
            for s in szenen
        ),
    )


def szenenstoff(scope: db.Scope, session_id: int, scene_id: int) -> SceneMaterial | None:
    """Eine einzelne Szene mit allem, was an ihr hängt — die Vorlage des Zwischenstands.

    Dieselben zwei Quellen wie in ``material``, nur auf eine Szene verengt: die Notizen
    dieser Szene und die Foundry-Ereignisse, die der Strom **während des Spiels** an sie
    gehängt hat. Ein Abgleich läuft hier ausdrücklich nicht — die Zahlen liegen zu diesem
    Zeitpunkt schon vor, und ein zweiter Weg zu Foundry verbrauchte das Passwort, das der
    Abschluss am Abendende noch braucht (#64).

    Die Sitzung steht mit in der Bedingung: eine Szenenkennung aus einer anderen Sitzung
    holte sonst deren Material unter dem Namen dieser hier.
    """
    kopf = scope.execute(
        "SELECT id, position, title FROM scene WHERE runde_id = ? AND id = ? AND session_id = ?",
        (scope.runde_id, scene_id, session_id),
    ).fetchone()
    if kopf is None:
        return None
    notizen = scope.execute(
        "SELECT text, origin FROM note WHERE runde_id = ? AND scene_id = ? ORDER BY id",
        (scope.runde_id, scene_id),
    ).fetchall()
    fakten = scope.execute(
        "SELECT m.* FROM scene_foundry_message v "
        "JOIN foundry_message m ON m.id = v.message_id AND m.runde_id = v.runde_id "
        "WHERE v.runde_id = ? AND v.scene_id = ? ORDER BY m.timestamp, m.id",
        (scope.runde_id, scene_id),
    ).fetchall()
    return SceneMaterial(
        position=kopf["position"],
        title=kopf["title"],
        notes=tuple(
            Notiz(text=zeile["text"], verschriftet=zeile["origin"] == TRANSKRIPT)
            for zeile in notizen
        ),
        facts=tuple(store.message(zeile) for zeile in fakten),
    )


def recap_material(scope: db.Scope, session_id: int) -> RecapMaterial | None:
    kopf = scope.execute(
        "SELECT s.id, s.played_on, s.title, p.text FROM session s "
        "JOIN protocol p ON p.session_id = s.id AND p.kind = ? "
        "WHERE s.runde_id = ? AND s.id = ?",
        (KIND, scope.runde_id, session_id),
    ).fetchone()
    if kopf is None:
        return None
    frueher = scope.execute(
        "SELECT p.text FROM protocol p JOIN session s ON s.id = p.session_id "
        "WHERE p.runde_id = ? AND p.kind = ? "
        "AND (s.played_on < ? OR (s.played_on = ? AND s.id < ?)) "
        "ORDER BY s.played_on DESC, s.id DESC LIMIT ?",
        (scope.runde_id, RUECKBLICK, kopf["played_on"], kopf["played_on"], session_id, FRUEHERE),
    ).fetchall()
    return RecapMaterial(
        session_id=kopf["id"],
        played_on=kopf["played_on"],
        title=kopf["title"],
        chronicle=kopf["text"],
        previous=tuple(zeile["text"] for zeile in frueher),
    )


def erzaehl_material(
    scope: db.Scope, von: int, bis: int, eintraege: Mapping[int, tuple[str, ...]]
) -> ErzaehlStoff:
    """Die Sitzungen zwischen zwei Eckpunkten, jede mit dem, was das Register zu ihr führt.

    ``eintraege`` kommt von außen und nicht aus einer Abfrage hier: das Register ist die
    Auswahl, und wer sie trifft, soll am Aufruf stehen. Die Chronik geht mit, aber nicht in
    den Aufruf ans Modell — sie ist die Vorlage der Zahlenschranke.
    """
    ecken = [
        scope.execute(
            "SELECT played_on, id FROM session WHERE runde_id = ? AND id = ?",
            (scope.runde_id, session_id),
        ).fetchone()
        for session_id in (von, bis)
    ]
    if any(ecke is None for ecke in ecken):
        return ErzaehlStoff()
    (erste, letzte) = sorted((ecke["played_on"], ecke["id"]) for ecke in ecken)
    zeilen = scope.execute(
        BEREICH,
        (KIND, scope.runde_id, erste[0], erste[0], erste[1], letzte[0], letzte[0], letzte[1]),
    ).fetchall()
    return ErzaehlStoff(
        abschnitte=tuple(
            Abschnitt(
                session_id=zeile["id"],
                played_on=zeile["played_on"],
                title=zeile["title"],
                chronicle=zeile["text"] or "",
                entries=tuple(eintraege.get(zeile["id"], ())),
            )
            for zeile in zeilen
        )
    )


def save(scope: db.Scope, session_id: int, text: str, at: str, kind: str = KIND) -> None:
    with scope:
        scope.execute(
            "INSERT INTO protocol (runde_id, session_id, kind, text, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (session_id, kind) DO UPDATE SET text = excluded.text, "
            "created_at = excluded.created_at",
            (scope.runde_id, session_id, kind, text, at),
        )


def compose_session(
    config: Config, runde: Runde, session_id: int, *, model: TextModel | None = None
) -> Composition | None:
    db.init(config.database_path)
    # Eine ruhende Runde bekommt keinen neuen Text mehr — auch nicht aus altem Material.
    if lebenszyklus.ruht(runde):
        return None
    scope = db.scoped(runde)
    try:
        stoff = material(scope, session_id)
        if stoff is None:
            return None
        gewaehlt = (
            model if model is not None else client.from_config(settings.effective(config, runde))
        )
        ergebnis = compose(stoff, gewaehlt, inhaltssprache=settings.sprache(runde))
        save(scope, session_id, ergebnis.text, _now())
        return ergebnis
    finally:
        scope.close()


def erzaehlen(
    config: Config,
    runde: Runde,
    von: int,
    bis: int,
    eintraege: Mapping[int, tuple[str, ...]],
    *,
    model: TextModel | None = None,
) -> Nacherzaehlung | None:
    """Einen Sitzungsbereich nacherzählen — abgelegt wird nichts.

    Die Nacherzählung ist eine Ansicht auf Chroniken und Register, keine dritte Fassung der
    Sitzung: sie entsteht auf Wunsch neu und geht als Datei hinaus. Eine Kopie in der
    Datenbank veraltete beim nächsten bestätigten Registereintrag, ohne dass jemand es sähe.
    """
    db.init(config.database_path)
    if lebenszyklus.ruht(runde):
        return None
    scope = db.scoped(runde)
    try:
        stoff = erzaehl_material(scope, von, bis, eintraege)
        if not stoff.abschnitte:
            return None
        gewaehlt = (
            model if model is not None else client.from_config(settings.effective(config, runde))
        )
        return nacherzaehlen(stoff, gewaehlt, inhaltssprache=settings.sprache(runde))
    finally:
        scope.close()


def zwischenstand_der_szene(
    config: Config,
    runde: Runde,
    session_id: int,
    scene_id: int,
    *,
    model: TextModel | None = None,
) -> Zwischenstand | None:
    """Die eben geschlossene Szene verdichten — abgelegt wird nichts.

    Kein ``save``, und das ist keine Auslassung: der Zwischenstand ist Deutung, und was
    hier in ``protocol`` läge, läse die Endkomposition oder der Rückblick eine Stufe
    später als Vorlage zurück. Er geht in den Thread und sonst nirgendwohin.
    """
    db.init(config.database_path)
    if lebenszyklus.ruht(runde):
        return None
    scope = db.scoped(runde)
    try:
        stoff = szenenstoff(scope, session_id, scene_id)
        if stoff is None:
            return None
        gewaehlt = (
            model if model is not None else client.from_config(settings.effective(config, runde))
        )
        return zwischenstand(stoff, gewaehlt, inhaltssprache=settings.sprache(runde))
    finally:
        scope.close()


def recap_session(
    config: Config, runde: Runde, session_id: int, *, model: TextModel | None = None
) -> Recap | None:
    db.init(config.database_path)
    if lebenszyklus.ruht(runde):
        return None
    scope = db.scoped(runde)
    try:
        stoff = recap_material(scope, session_id)
        if stoff is None:
            return None
        gewaehlt = (
            model if model is not None else client.from_config(settings.effective(config, runde))
        )
        ergebnis = recap(stoff, gewaehlt, inhaltssprache=settings.sprache(runde))
        save(scope, session_id, ergebnis.text, _now(), kind=RUECKBLICK)
        return ergebnis
    finally:
        scope.close()
