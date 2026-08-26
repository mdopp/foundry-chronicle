"""Die Mitschrift: Sitzung, Szene, Notiz.

Geschrieben wird während des Spiels. Deshalb liegt jede Eingabe mit dem Absenden auf
der Platte und nichts davon im Browser — ein Neuladen mitten in der Sitzung darf nichts
kosten.

Die Szenenfolge ist im Präsenzfall die einzige Zeitachse: ``position`` zählt hoch, die
Zeitstempel aus Foundry taugen dafür nicht.

Seit die Sitzung im Chat eines Discord-Kanals läuft, hat eine Szene außerdem einen
**Zeitpunkt**: den
ihrer Trennlinie. Eine Notiz gehört in die letzte Szene, deren Trennlinie vor ihr liegt —
nicht in »die aktuelle«. Nur so landet eine Nachricht, die Tage später nachgetragen wird,
dort, wo sie hingehört, statt in der Szene, die gerade zufällig die letzte ist.

Alles hier gehört einer Runde und wird nur über sie erreicht — auch dort, wo eine Id
allein schon eindeutig wäre. Eine Szenen-Id aus einer fremden Runde ist kein Fund,
sondern ein Datenleck.

**Eine Sitzung wieder loszuwerden gehört dazu** (``delete_session``, #171). Sie nimmt
mit, was an ihr hängt — bis zu den Tondateien auf der Platte, und das sind die Stimmen
echter Menschen. Rückgängig gibt es dafür nichts, also fragt der Weg dorthin vorher
nach (``bot.chronik``). Es ist keine Betreiber-Fähigkeit: über der Runde steht niemand
(#90), und hier steht niemand über der Sitzung.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

from chronicle import db
from chronicle.runde import Runde

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Note:
    id: int
    text: str
    created_at: str


@dataclass(frozen=True)
class Scene:
    id: int
    position: int
    title: str | None = None
    notes: tuple[Note, ...] = ()


@dataclass(frozen=True)
class Session:
    id: int
    played_on: str
    title: str | None = None
    scene_count: int = 0
    note_count: int = 0
    scenes: tuple[Scene, ...] = ()
    token: str = ""


@dataclass(frozen=True)
class Running:
    """Die laufende Sitzung: ihre Kennung und der Kanal, in dem sie geführt wird."""

    id: int
    kanal_id: str | None


@dataclass(frozen=True)
class Contents:
    """Was an einer Sitzung hängt — die Zahlen, mit denen die Rückfrage sie benennt.

    ``audio`` zählt nicht die Spuren, sondern die Dateien, die dafür **noch** auf der
    Platte liegen: nach der Aufbewahrungsfrist bleibt die Zeile stehen und der Ton ist
    fort. Der Unterschied gehört in die Frage — »drei Aufnahmen« und »drei Stunden
    Stimmen« sind nicht dasselbe Versprechen.

    ``geblieben`` gibt es nur nach dem Löschen und ist dort fast immer ``0``: die Zahl der
    Dateien, die sich **nicht** entfernen ließen. Sie steht hier, damit die Meldung danach
    nicht mehr verspricht, als geschehen ist — bei Stimmen echter Menschen ist ein »fort«,
    das keines war, die teuerste Sorte Unwahrheit.
    """

    session: Session
    recordings: int
    audio: int
    transcripts: int
    protocols: int
    geblieben: int = 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def create_session(
    runde: Runde,
    *,
    played_on: str = "",
    title: str = "",
    kanal_id: str = "",
    laeuft: bool = False,
) -> int:
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "INSERT INTO session "
                "(runde_id, played_on, title, created_at, kanal_id, laeuft, token) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    played_on.strip() or today(),
                    title.strip() or None,
                    zeitpunkt,
                    str(kanal_id).strip() or None,
                    1 if laeuft else 0,
                    db.kennung(),
                ),
            )
            sitzung = int(cursor.lastrowid)
            # Wer eine Sitzung anlegt, will sofort tippen — die erste Szene steht schon da.
            scope.execute(
                "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
                "VALUES (?, ?, 1, NULL, ?)",
                (scope.runde_id, sitzung, zeitpunkt),
            )
        return sitzung
    finally:
        scope.close()


def sessions(runde: Runde) -> tuple[Session, ...]:
    scope = db.scoped(runde)
    try:
        rows = scope.execute(
            "SELECT s.id, s.played_on, s.title, s.token, "
            "(SELECT COUNT(*) FROM scene WHERE session_id = s.id) AS szenen, "
            "(SELECT COUNT(*) FROM note n JOIN scene c ON n.scene_id = c.id "
            " WHERE c.session_id = s.id) AS notizen "
            "FROM session s WHERE s.runde_id = ? ORDER BY s.played_on DESC, s.id DESC",
            (scope.runde_id,),
        ).fetchall()
    finally:
        scope.close()
    return tuple(
        Session(
            id=r["id"],
            played_on=r["played_on"],
            title=r["title"],
            scene_count=r["szenen"],
            note_count=r["notizen"],
            token=r["token"] or "",
        )
        for r in rows
    )


def session(runde: Runde, session_id: int) -> Session | None:
    scope = db.scoped(runde)
    try:
        kopf = scope.execute(
            "SELECT id, played_on, title, token FROM session WHERE runde_id = ? AND id = ?",
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
            "SELECT n.id, n.scene_id, n.text, n.created_at FROM note n "
            "JOIN scene c ON n.scene_id = c.id "
            "WHERE n.runde_id = ? AND c.session_id = ? ORDER BY n.id",
            (scope.runde_id, session_id),
        ).fetchall()
    finally:
        scope.close()
    je_szene: dict[int, list[Note]] = {}
    for r in notizen:
        eintrag = Note(id=r["id"], text=r["text"], created_at=r["created_at"])
        je_szene.setdefault(r["scene_id"], []).append(eintrag)
    scenes = tuple(
        Scene(
            id=r["id"],
            position=r["position"],
            title=r["title"],
            notes=tuple(je_szene.get(r["id"], ())),
        )
        for r in szenen
    )
    return Session(
        id=kopf["id"],
        played_on=kopf["played_on"],
        title=kopf["title"],
        scene_count=len(scenes),
        note_count=len(notizen),
        scenes=scenes,
        token=kopf["token"] or "",
    )


def latest_session(runde: Runde) -> Session | None:
    """Die zuletzt angelegte Sitzung — das Ziel für alles, was ohne Adresse hereinkommt.

    Nicht die zuletzt gespielte: ein nachgetragenes Datum würde sonst die Zielsitzung
    eines Diktats verschieben, das längst unterwegs ist.
    """
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT id FROM session WHERE runde_id = ? ORDER BY id DESC LIMIT 1",
            (scope.runde_id,),
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else session(runde, int(zeile["id"]))


def running_session(runde: Runde) -> Running | None:
    """Die Sitzung, die gerade läuft — Kennung und Kanal, oder nichts.

    Seit #271 ist das die ganze Grenze: kein Thread zieht sie mehr, also steht in der
    Datenbank, ob eine Sitzung zwischen Start und Abschluss liegt. Je Runde ist höchstens
    eine offen — ``sitzung_anlegen`` fragt genau hier, bevor es eine zweite anlegt.
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT id, kanal_id FROM session WHERE runde_id = ? AND laeuft = 1 "
            "ORDER BY id DESC LIMIT 1",
            (scope.runde_id,),
        ).fetchone()
    finally:
        scope.close()
    if row is None:
        return None
    return Running(id=int(row["id"]), kanal_id=row["kanal_id"])


def closed_session_in_channel(runde: Runde, kanal_id: str) -> int | None:
    """Der zuletzt in diesem Kanal geführte Abend, sofern er schon abgeschlossen ist.

    Die Gegenfrage zu ``running_session``: eine Zeile, die hier steht, gehört in keine
    Sitzung mehr — aber es gab eine, und wer sie schreibt, ist kein Zaungast, sondern
    jemand, der noch abmoderiert. Nur daran unterscheidet sich der Satz an ihn von einem
    Satz an jeden beliebigen Kanal der Gilde.
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT id FROM session WHERE runde_id = ? AND kanal_id = ? AND laeuft = 0 "
            "ORDER BY id DESC LIMIT 1",
            (scope.runde_id, str(kanal_id)),
        ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["id"])


def close_session(runde: Runde, session_id: int) -> bool:
    """Die Sitzung ist zu — was danach im Kanal steht, ist keine Notiz mehr."""
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "UPDATE session SET laeuft = 0 WHERE runde_id = ? AND id = ? AND laeuft = 1",
                (scope.runde_id, session_id),
            )
        return cursor.rowcount > 0
    finally:
        scope.close()


def channel_of_session(runde: Runde, session_id: int) -> str | None:
    """Der Kanal einer Sitzung — die Gegenrichtung, für wen ihr etwas zu sagen hat.

    Er bleibt auch nach dem Abschluss stehen: der Sprung aus der Suche zeigt Wochen später
    noch dorthin, und die fertige Chronik wird dort angehängt.
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT kanal_id FROM session WHERE runde_id = ? AND id = ?",
            (scope.runde_id, session_id),
        ).fetchone()
    finally:
        scope.close()
    return None if row is None else row["kanal_id"]


def session_of_note(runde: Runde, message_id: str) -> int | None:
    """Die Sitzung, in der diese Nachricht zur Notiz wurde — auch eine Woche später.

    Eine nachträglich bearbeitete Nachricht gehört ihrer eigenen Sitzung und nicht der,
    die gerade läuft. Ohne diesen Weg landete die Korrektur einer alten Notiz am heutigen
    Abend, sobald die Sitzungsgrenze nicht mehr am Ort hängt (#271).
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT c.session_id FROM note n JOIN scene c "
            "ON c.id = n.scene_id AND c.runde_id = n.runde_id "
            "WHERE n.runde_id = ? AND n.discord_message_id = ?",
            (scope.runde_id, str(message_id)),
        ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["session_id"])


def latest_scene(runde: Runde, session_id: int) -> int | None:
    """Die zuletzt gezogene Szene dieser Sitzung — die, die ein Schnitt abschließt.

    Über die Position und nicht über die Kennung: ``add_scene`` hängt die neue an
    ``MAX(position) + 1``, also ist die höchste Position die laufende. Gefragt wird
    **vor** dem Schnitt; danach wäre es die neue, die eben erst begonnen hat.
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT id FROM scene WHERE runde_id = ? AND session_id = ? "
            "ORDER BY position DESC LIMIT 1",
            (scope.runde_id, session_id),
        ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["id"])


def add_scene(runde: Runde, session_id: int, *, title: str = "", at: str = "") -> int | None:
    scope = db.scoped(runde)
    try:
        bekannt = scope.execute(
            "SELECT 1 FROM session WHERE runde_id = ? AND id = ?", (scope.runde_id, session_id)
        ).fetchone()
        if bekannt is None:
            return None
        with scope:
            cursor = scope.execute(
                "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
                "SELECT ?, ?, COALESCE(MAX(position), 0) + 1, ?, ? FROM scene "
                "WHERE session_id = ?",
                (
                    scope.runde_id,
                    session_id,
                    title.strip() or None,
                    at.strip() or _now(),
                    session_id,
                ),
            )
        return int(cursor.lastrowid)
    finally:
        scope.close()


def rename_scene(runde: Runde, session_id: int, scene_id: int, title: str) -> bool:
    """Benennt die noch **namenlose** Szene einer Sitzung nach — die erste, die mit ihr kam.

    Sie steht am Anfang jeder Sitzung und hat keinen Namen; wer eine fertige Gliederung
    einliest (``chronicle.dokument``), hätte sonst eine leere Szene vor der ersten.

    Enger als ein allgemeines Umbenennen, und das mit Absicht: die Sitzung muss genannt
    werden, und ein Name, den ein Mensch gesetzt hat, bleibt stehen. Wer hier ohne diese
    beiden Schranken durchkäme, überschriebe mit einer Zahl aus einer alten Ansicht die
    Szenentitel eines fremden Abends.
    """
    sauber = title.strip()
    if not sauber:
        return False
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "UPDATE scene SET title = ? "
                "WHERE runde_id = ? AND id = ? AND session_id = ? AND title IS NULL",
                (sauber, scope.runde_id, scene_id, session_id),
            )
        return cursor.rowcount > 0
    finally:
        scope.close()


def scene_at(runde: Runde, session_id: int, moment: str) -> int | None:
    """In welche Szene eine Notiz dieses Zeitpunkts gehört.

    Die letzte Trennlinie **vor** dem Zeitpunkt, nicht die zuletzt gezogene: eine
    Nachricht von Dienstag bleibt damit in der Szene von Dienstag, auch wenn sie erst
    Freitag im Thread landet. Liegt sie vor jeder Trennlinie, bleibt die erste Szene —
    eine Notiz ohne Szene gäbe es sonst nicht zu speichern.
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT id FROM scene WHERE runde_id = ? AND session_id = ? AND created_at <= ? "
            "ORDER BY position DESC LIMIT 1",
            (scope.runde_id, session_id, moment),
        ).fetchone()
        if row is None:
            row = scope.execute(
                "SELECT id FROM scene WHERE runde_id = ? AND session_id = ? "
                "ORDER BY position LIMIT 1",
                (scope.runde_id, session_id),
            ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["id"])


def scene_of_moment(runde: Runde, session_id: int, moment: str) -> int | None:
    """In welche Szene **dieser** Sitzung ein Zeitpunkt fällt — ohne Auffanglinie.

    Der Unterschied zu ``scene_at`` ist der Absender. Dort kommt der Zeitpunkt aus einer
    Notiz, die zu dieser Sitzung gehört, also fängt die erste Szene auf, was vor jeder
    Trennlinie liegt. Hier kommt er aus Foundrys Chat-Log, und das trägt die **ganze**
    Kampagne: dieselbe Auffanglinie zöge die Würfe aller früheren Abende in die erste
    Szene dieses einen.

    Gefragt wird deshalb über alle Szenen der Runde, und geantwortet wird nur, wenn die
    letzte Trennlinie vor dem Zeitpunkt zu dieser Sitzung gehört. Das setzt beide Grenzen
    auf einmal: was vor ihrem Beginn liegt, gehört einem früheren Abend, was danach kommt,
    dem nächsten — und ein Wurf ohne Szene wird nicht untergebracht, sondern gar nicht.
    """
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT id, session_id FROM scene WHERE runde_id = ? AND created_at <= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (scope.runde_id, moment),
        ).fetchone()
    finally:
        scope.close()
    if row is None or int(row["session_id"]) != session_id:
        return None
    return int(row["id"])


def verknuepfte_foundry_ereignisse(runde: Runde) -> frozenset[str]:
    """Welche Foundry-Ereignisse schon an einer Szene hängen — gleich an welcher.

    Runden-weit und nicht je Sitzung, weil die Frage runden-weit ist: ein Wurf ist an
    einem Zeitpunkt gefallen und gehört zu genau einer Szene. Wer sie schon hat, behält
    sie — der Nachtrag beim Abschluss darf denselben Wurf nicht ein zweites Mal, in einer
    zweiten Szene, in die Chronik hängen.
    """
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT message_id FROM scene_foundry_message WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
    finally:
        scope.close()
    return frozenset(zeile["message_id"] for zeile in zeilen)


def session_of_scene(runde: Runde, scene_id: int) -> int | None:
    scope = db.scoped(runde)
    try:
        row = scope.execute(
            "SELECT session_id FROM scene WHERE runde_id = ? AND id = ?",
            (scope.runde_id, scene_id),
        ).fetchone()
    finally:
        scope.close()
    return None if row is None else int(row["session_id"])


def link_foundry_message(runde: Runde, scene_id: int, message_id: str) -> bool:
    """Ein Foundry-Ereignis an die Szene hängen, in der es passiert ist.

    Das ist der Ursprungsvermerk des Ereignisstroms (#97): der Wurf selbst steht in
    ``foundry_message``, hier steht nur, wohin er gehört. Er wird ausdrücklich **nicht**
    als Notiz abgelegt — eine Notiz ist Text vom Tisch, und eine Zahl, die als Notiz
    dasteht, hat ihren Beleg unterwegs verloren. Die Komposition liest beides getrennt und
    kann deshalb weiter zwischen Belegtem und Verbindungssätzen unterscheiden.

    Zweimal dasselbe Ereignis ist keine zweite Zuordnung: ein Blick, der eine Nachricht
    erneut sieht, soll die Szene von damals nicht überschreiben.
    """
    scope = db.scoped(runde)
    try:
        bekannt = scope.execute(
            "SELECT 1 FROM scene WHERE runde_id = ? AND id = ?", (scope.runde_id, scene_id)
        ).fetchone()
        if bekannt is None:
            return False
        with scope:
            cursor = scope.execute(
                "INSERT OR IGNORE INTO scene_foundry_message (runde_id, scene_id, message_id) "
                "VALUES (?, ?, ?)",
                (scope.runde_id, scene_id, str(message_id)),
            )
        return cursor.rowcount > 0
    finally:
        scope.close()


def add_note(
    runde: Runde, scene_id: int, text: str, *, message_id: str = "", origin: str | None = None
) -> int | None:
    """``origin`` kennzeichnet Abgeleitetes — ``None`` heißt, ein Mensch hat es geschrieben."""
    inhalt = text.strip()
    if not inhalt:
        return None
    zeitpunkt = _now()
    scope = db.scoped(runde)
    try:
        bekannt = scope.execute(
            "SELECT 1 FROM scene WHERE runde_id = ? AND id = ?", (scope.runde_id, scene_id)
        ).fetchone()
        if bekannt is None:
            return None
        with scope:
            cursor = scope.execute(
                "INSERT INTO note (runde_id, scene_id, text, created_at, updated_at, "
                "discord_message_id, origin) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scope.runde_id,
                    scene_id,
                    inhalt,
                    zeitpunkt,
                    zeitpunkt,
                    str(message_id).strip() or None,
                    origin,
                ),
            )
        return int(cursor.lastrowid)
    finally:
        scope.close()


def drop_derived(runde: Runde, session_id: int, origin: str) -> int:
    """Nimmt die abgeleiteten Notizen einer Sitzung fort — Geschriebenes bleibt stehen.

    Der Weg, auf dem ein zweiter Abschluss nichts verdoppelt: was aus den Spuren kommt,
    wird vor jedem Lauf verworfen und neu gelegt. Ersetzen statt Überspringen, damit eine
    Spur, die erst später verschriftet wurde, noch in die Szenen findet.
    """
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "DELETE FROM note WHERE runde_id = ? AND origin = ? AND scene_id IN "
                "(SELECT id FROM scene WHERE runde_id = ? AND session_id = ?)",
                (scope.runde_id, origin, scope.runde_id, session_id),
            )
        return cursor.rowcount
    finally:
        scope.close()


def update_note(runde: Runde, message_id: str, text: str) -> bool:
    """Zieht eine im Thread geänderte Nachricht nach — nur mit Text.

    Eine leergeräumte Nachricht ist hier kein Fall, sondern einer für ``remove_note``: eine
    Notiz ohne Text gibt es nicht. Dass diese Weiche in ``bot.chronik`` fehlte, war der
    Weg, auf dem der zurückgenommene Wortlaut samt Suchzeile stehenblieb (#184).
    """
    inhalt = text.strip()
    if not inhalt:
        return False
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "UPDATE note SET text = ?, updated_at = ? "
                "WHERE runde_id = ? AND discord_message_id = ?",
                (inhalt, _now(), scope.runde_id, str(message_id)),
            )
        return cursor.rowcount > 0
    finally:
        scope.close()


def remove_note(runde: Runde, message_id: str) -> bool:
    """Was im Thread gelöscht wurde, verschwindet auch hier — sonst hielten wir es fest."""
    scope = db.scoped(runde)
    try:
        with scope:
            cursor = scope.execute(
                "DELETE FROM note WHERE runde_id = ? AND discord_message_id = ?",
                (scope.runde_id, str(message_id)),
            )
        return cursor.rowcount > 0
    finally:
        scope.close()


def _audio(config, runde: Runde, session_id: int) -> tuple[Path, ...]:
    """Die Tondateien dieser Sitzung — die eingereihten und die, die es nie wurden.

    Die Zeile allein genügt nicht: eine Spur wird während der ganzen Sitzung auf die Platte
    geschrieben und erst am Ende eingereiht. Ein Absturz mittendrin hinterlässt fertige
    Aufnahmen ohne Zeile, die sonst niemand mehr fände. Der Dateiname trägt die
    Sitzungskennung, und über die wird deshalb auch im Verzeichnis gesucht.

    Der Griff ins Verzeichnis geht **an der Runden-Schranke vorbei** — er sieht nur den
    Namen, nicht die Runde. Deshalb wird er erst aufgerufen, wenn feststeht, dass diese
    Sitzung dieser Runde gehört (``session_contents``); ohne diese Reihenfolge löschte eine
    fremde Sitzungskennung die Tondateien der Nachbarrunde.
    """
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT filename FROM recording WHERE runde_id = ? AND session_id = ?",
            (scope.runde_id, session_id),
        ).fetchall()
    finally:
        scope.close()
    gefunden = {config.recordings_dir / zeile["filename"] for zeile in zeilen}
    gefunden.update(config.recordings_dir.glob(f"sitzung{int(session_id)}-*"))
    # Der Name kommt aus einer Spalte, gelöscht wird eine Datei: ein ``../`` darin zeigte
    # aus dem Aufnahmeverzeichnis heraus. Heute schreibt jeder Aufrufer einen bloßen Namen
    # hinein — an einer Stelle, die *löscht*, ist das keine Zusage, auf die man sich verlässt.
    return tuple(
        sorted(
            datei for datei in gefunden if datei.parent == config.recordings_dir and datei.exists()
        )
    )


def sitzungsmarke(sitzung: Session) -> str:
    """Woran diese Sitzung über einen Klick hinweg wiedererkannt wird — Nummer und Kennung.

    Das Gegenstück zu ``bot.chronik.dieselbe_runde`` eine Ebene tiefer, und aus demselben
    Grund: ``session.id`` ist ein ``INTEGER PRIMARY KEY`` ohne ``AUTOINCREMENT``, SQLite
    vergibt die Nummer der zuletzt gelöschten Zeile also wieder. Ein Menü und ein Knopf
    leben eine Viertelstunde; wird die gewählte Sitzung in der Zeit anderswo gelöscht und
    danach eine neue angelegt, trägt die neue dieselbe Nummer — und der Klick von vorhin
    nähme den frisch begonnenen Abend samt seiner Tondateien fort.

    ``created_at`` täte es nicht: es steht auf die Sekunde genau, und Löschen und Anlegen
    fallen leicht in dieselbe. Verglichen wird deshalb der Zufallswert aus ``token``.
    """
    return f"{sitzung.id}:{sitzung.token}"


def gemeinte_sitzung(runde: Runde, marke: str) -> Session | None:
    """Die Sitzung hinter einer Marke — ``None``, wenn unter der Nummer etwas anderes steht.

    Was hier hereinkommt, kommt aus einer Discord-Interaktion und ist deshalb nichts, worauf
    man sich verlässt: eine Marke, die keine ist, ist keine Sitzung. Eine **leere** Kennung
    ebenso wenig — sonst fiele der Vergleich für zwei Sitzungen ohne Kennung still auf
    Nummern-Gleichheit zurück, und das ist genau der Fehler, gegen den die Kennung steht.
    """
    kennung, _, wert = str(marke).partition(":")
    # ``isdigit`` sagt auch zu »²« ja, und ``int`` wirft dann. Eine Marke kommt aus einer
    # Discord-Interaktion, deren Wert ein Client frei setzt — abgewiesen wird sie hier,
    # nicht von einer Ausnahme drei Zeilen später.
    if not wert or not (kennung.isascii() and kennung.isdigit()) or len(kennung) > 18:
        return None
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT id FROM session WHERE runde_id = ? AND id = ? AND token = ?",
            (scope.runde_id, int(kennung), wert),
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else session(runde, int(kennung))


def session_contents(config, runde: Runde, marke: str) -> Contents | None:
    """Was an dieser Sitzung hängt — die Auskunft **vor** dem Löschen, ohne zu löschen.

    Keine Sitzung dieser Runde, oder nicht mehr die gemeinte, heißt ``None``. Das ist
    zugleich die Schranke, hinter der ``_audio`` erst ins Verzeichnis greifen darf.
    """
    gefunden = gemeinte_sitzung(runde, marke)
    if gefunden is None:
        return None
    session_id = gefunden.id
    scope = db.scoped(runde)
    try:
        zahlen = scope.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM recording WHERE runde_id = ? AND session_id = ?) AS spuren, "
            "(SELECT COUNT(*) FROM transcript WHERE runde_id = ? AND session_id = ?) AS texte, "
            "(SELECT COUNT(*) FROM protocol WHERE runde_id = ? AND session_id = ?) AS geschrieben",
            (scope.runde_id, session_id) * 3,
        ).fetchone()
    finally:
        scope.close()
    return Contents(
        session=gefunden,
        recordings=int(zahlen["spuren"]),
        audio=len(_audio(config, runde, session_id)),
        transcripts=int(zahlen["texte"]),
        protocols=int(zahlen["geschrieben"]),
    )


def _index_nachziehen(scope: db.Scope, session_id: int) -> None:
    """Den Suchindex hinter der Kaskade aufräumen — ausdrücklich, wie beim Löschen der Runde.

    Er hängt an keinem Fremdschlüssel — eine fts5-Tabelle kennt keine —, und die
    Löschtrigger der Quelltabellen feuern bei einer Kaskade nicht. Ohne diese Zeilen stünden
    Notizen, Chronik und Diktat einer gelöschten Sitzung weiter im Index.

    Ein Registereintrag ist der eine Fall, der **nicht** mitgeht: er gehört der Runde und
    überlebt jede einzelne Sitzung. Im Index steht bei ihm die erste Sitzung, in der er
    vorkommt; war es genau diese, bekommt er seine nächste. Bleibt keine, wird der Verweis
    leer — genau das, was auch der Neuaufbau beim nächsten Start schriebe.
    """
    scope.execute(
        "DELETE FROM search_index WHERE runde_id = ? AND session_id = ? AND kind <> 'register'",
        (scope.runde_id, session_id),
    )
    scope.execute(
        "UPDATE search_index SET session_id = (SELECT MIN(m.session_id) FROM register_mention m "
        "WHERE m.runde_id = search_index.runde_id AND m.entry_id = search_index.ref_id) "
        "WHERE runde_id = ? AND kind = 'register' AND session_id = ?",
        (scope.runde_id, session_id),
    )


def _tondateien_loeschen(dateien: tuple[Path, ...]) -> int:
    """Die Tondateien fortnehmen und sagen, wie viele blieben. **Nie mit dem Namen im Log.**

    Der Stamm eines Aufnahmenamens ist der Sprechername; ein ungefangenes ``unlink`` schriebe
    ihn über den Traceback ins Log des Betreibers, und zwar auf einem Weg, den niemand
    bestellt hat. Was schiefging, sagt die Art des Fehlers.
    """
    geblieben = 0
    for datei in dateien:
        try:
            datei.unlink(missing_ok=True)
        except OSError as fehler:
            geblieben += 1
            logger.warning("Eine Tondatei ließ sich nicht löschen: %s.", type(fehler).__name__)
    return geblieben


def delete_session(config, runde: Runde, marke: str) -> Contents | None:
    """Eine einzelne Sitzung und alles, was an ihr hängt. Rückgängig gibt es nicht.

    **Die Tondateien zuerst**, wie beim Löschen der ganzen Runde: verschwände die Zeile
    vorher, wüsste niemand mehr, welche Datei zu löschen war, und eine Stunde fremder
    Stimmen bliebe auf der Platte liegen. Der Rest fällt an der ``session``-Zeile über die
    Fremdschlüssel-Kaskade: Szenen, Notizen, Diktate, Transkripte, Chronik und Rückblick,
    die Läufe und die Erwähnungen im Register.

    **Der Nachweis der Ansage bleibt** (``consent_event.session_id`` fällt auf ``NULL``).
    Das ist die eine Zeile, die ihren Gegenstand überlebt, und sie tut es mit Absicht: die
    Runde gibt es weiter, und was belegt, dass im Sprachkanal angesagt wurde, ist genau
    dann etwas wert, wenn das daraus Gemachte noch irgendwo liegt. Beim Löschen der ganzen
    Runde geht er mit — dort ist niemand mehr da, für den er belegte.

    Gelöscht wird gegen eine **Marke** und nicht gegen eine nackte Nummer: unter der Nummer
    von vorhin kann eine Viertelstunde später ein anderer Abend stehen (``sitzungsmarke``).

    Zurück kommt, was fort ist — für die Meldung danach: ``audio`` zählt dort die Dateien,
    die wirklich von der Platte sind, ``geblieben`` die, die es nicht wurden. ``None`` heißt:
    diese Sitzung gehört dieser Runde nicht (mehr) oder ist nicht mehr die gemeinte, und dann
    ist auch nichts geschehen.
    """
    gefunden = session_contents(config, runde, marke)
    if gefunden is None:
        return None
    session_id = gefunden.session.id
    geblieben = _tondateien_loeschen(_audio(config, runde, session_id))
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "DELETE FROM session WHERE runde_id = ? AND id = ?", (scope.runde_id, session_id)
            )
            _index_nachziehen(scope, session_id)
    finally:
        scope.close()
    return replace(gefunden, audio=gefunden.audio - geblieben, geblieben=geblieben)
