"""Rohdump → gefiltertes Modell. Diese Stufe liegt vor dem Zwischenspeicher.

Zwei Reduktionen greifen hier, keine später: die Berechtigungsstufe des angemeldeten
Kontos entscheidet, *was* übernommen wird, und eine feste Feldauswahl entscheidet,
*wieviel* davon. Der Rohdump enthält die Klarnamen aller Konten der Welt — gespeichert
werden nur die Konten, denen eine sichtbare Figur gehört.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from chronicle.foundry import permissions, systems
from chronicle.foundry.model import (
    Character,
    ChatMessage,
    Player,
    Scene,
    World,
    WorldSnapshot,
)

UNKNOWN_SYSTEM = "unbekannt"

UNKNOWN_WORLD = "unbenannte Welt"


def _system_id(raw: Mapping) -> str:
    system = raw.get("system")
    if isinstance(system, Mapping):
        return str(system.get("id") or UNKNOWN_SYSTEM)
    return str(system or UNKNOWN_SYSTEM)


def identity(raw: Mapping) -> World:
    """Die Welt-Kennung aus dem Rohdump — der ``world``-Block trägt sie.

    Sie steht bewusst nicht im ``WorldSnapshot``: sie ist keine gefilterte Spielinformation,
    sondern eine Eigenschaft der Verbindung, und sie wird gebraucht, **bevor** irgendetwas
    gespeichert wird.
    """
    welt = raw.get("world")
    welt = welt if isinstance(welt, Mapping) else {}
    kennung = str(welt.get("id") or "")
    return World(id=kennung, title=str(welt.get("title") or kennung or UNKNOWN_WORLD))


def kernversion(raw: Mapping) -> str | None:
    """Die Foundry-Hauptversion aus dem ``world``-Block — ``None``, wenn sie fehlt.

    Sie steht in jeder echten Antwort und wird nur für **eine** Frage gebraucht: wenn wir
    eine Antwort nicht wiedererkennen, ist die Version der einzige Hinweis darauf, warum.
    """
    welt = raw.get("world")
    welt = welt if isinstance(welt, Mapping) else {}
    version = welt.get("coreVersion")
    return str(version) if version else None


# Die vier Listen, an denen eine Weltantwort wiederzuerkennen ist: sie sind alles, was
# dieser Adapter aus ihr liest. Ein echter Server schickt sie auch dann, wenn sie leer sind.
GELESENE_LISTEN = ("users", "actors", "messages", "scenes")


def _ist_liste(wert) -> bool:
    """Eine Folge von Dokumenten — geprüft wird die Form, nicht der genaue Typ.

    Aus JSON kommt immer eine ``list``; darauf zu bestehen hieße nur, dass ein Aufrufer mit
    einem Tupel als unverständlich gälte. Ausgeschlossen ist, was diese Prüfung sonst still
    bestünde: eine Zeichenkette ist auch eine Folge, aber keine Dokumentenliste.
    """
    return isinstance(wert, Sequence) and not isinstance(wert, str | bytes)


def fehlende_listen(raw: Mapping) -> tuple[str, ...]:
    """Welche der gelesenen Listen diese Antwort nicht an der erwarteten Stelle trägt.

    Zwischen »der Socket hat geantwortet« und »wir schreiben in den Speicher« braucht es
    eine Plausibilitätsprüfung, sonst wird der dokumentierte Fall des
    Foundry-Hauptversionssprungs zu einer **gültigen, leeren** Welt (#284): ``_documents``
    gibt für jeden umbenannten oder verschachtelten Schlüssel still ``[]`` zurück, und der
    Speicher kann das nicht von »die Spielleitung hat alles gelöscht« unterscheiden. Eine
    Lücke mit Erklärung ist besser als ein stiller Datenschaden mit grüner Meldung.

    ``project`` selbst bleibt nachsichtig — es ist der Übersetzer, nicht der Türsteher.
    Geprüft wird davor, wo noch nichts geschrieben ist.
    """
    return tuple(name for name in GELESENE_LISTEN if not _ist_liste(raw.get(name)))


def _documents(raw: Mapping, name: str) -> list[Mapping]:
    return [eintrag for eintrag in (raw.get(name) or []) if isinstance(eintrag, Mapping)]


def _id(document: Mapping) -> str:
    return str(document.get("_id") or document.get("id") or "")


def _player(user: Mapping) -> Player:
    role = user.get("role")
    return Player(
        id=_id(user),
        name=str(user.get("name") or ""),
        role=role if isinstance(role, int) and not isinstance(role, bool) else 0,
        is_gm=permissions.is_gm(user),
    )


def _character(actor: Mapping, user_id: str, gm: bool) -> Character | None:
    ownership = actor.get("ownership")
    stufe = permissions.OWNER if gm else permissions.effective_level(ownership, user_id)
    if stufe == permissions.NONE:
        return None
    name = str(actor.get("name") or "")
    if stufe == permissions.LIMITED:
        return Character(id=_id(actor), name=name, limited=True)
    return Character(
        id=_id(actor),
        name=name,
        type=str(actor.get("type") or "") or None,
        owner_ids=permissions.owner_ids(ownership),
    )


def _scene(scene: Mapping, user_id: str, gm: bool) -> Scene | None:
    """Eine Szene, die die Gruppe nie gesehen hat, gibt es für die Chronik nicht.

    Ein Ortsname ist ein Vorgriff, solange die Karte nur die Spielleitung kennt: »Das Grab
    des Verräters« im Protokoll verriete genau das Geheimnis, gegen das hier gefiltert
    wird. Jede Stufe über ``NONE`` heißt dagegen, dass der Name in der Kartenleiste stand.
    """
    ownership = scene.get("ownership")
    stufe = permissions.OWNER if gm else permissions.effective_level(ownership, user_id)
    if stufe == permissions.NONE:
        return None
    return Scene(
        id=_id(scene),
        name=str(scene.get("navName") or scene.get("name") or ""),
        active=bool(scene.get("active")),
    )


def _message(message: Mapping, system: str) -> ChatMessage:
    speaker = message.get("speaker")
    speaker = speaker if isinstance(speaker, Mapping) else {}
    timestamp = message.get("timestamp")
    return ChatMessage(
        id=_id(message),
        timestamp=int(timestamp) if isinstance(timestamp, int | float) else 0,
        speaker_actor=str(speaker.get("actor")) if speaker.get("actor") else None,
        speaker_alias=str(speaker.get("alias")) if speaker.get("alias") else None,
        content=str(message.get("content") or ""),
        roll=systems.read_roll(system, message),
    )


def fuer_die_gruppe(raw: Mapping) -> frozenset[str]:
    """Die Kennungen der Nachrichten, die für die **ganze** Gruppe bestimmt waren.

    Sie steht neben ``project`` und nicht darin: das Ergebnis von ``project`` ist das
    Archiv, und das bleibt voll. Hier geht es um die engere Grenze davor — was in den
    laufenden Sitzungs-Thread darf, in dem alle mitlesen. Was der Rohdump als Geflüster
    oder blind führt, gehört nicht dazu, auch wenn unser Konto es sehen darf.
    """
    return frozenset(
        _id(message)
        for message in _documents(raw, "messages")
        if permissions.message_fuer_die_gruppe(message)
    )


def project(raw: Mapping, user_id: str, *, fetched_at: str) -> WorldSnapshot:
    system = _system_id(raw)
    users = _documents(raw, "users")
    gm = any(_id(user) == user_id and permissions.is_gm(user) for user in users)

    characters = tuple(
        figur
        for figur in (_character(actor, user_id, gm) for actor in _documents(raw, "actors"))
        if figur is not None
    )
    beteiligt = {user_id}
    for figur in characters:
        beteiligt.update(figur.owner_ids)

    return WorldSnapshot(
        system=system,
        fetched_at=fetched_at,
        players=tuple(_player(user) for user in users if _id(user) in beteiligt),
        characters=characters,
        messages=tuple(
            _message(message, system)
            for message in _documents(raw, "messages")
            if permissions.message_visible(message, user_id, gm)
        ),
        scenes=tuple(
            szene
            for szene in (_scene(scene, user_id, gm) for scene in _documents(raw, "scenes"))
            if szene is not None
        ),
    )
