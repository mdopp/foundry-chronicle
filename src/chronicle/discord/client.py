"""Discords REST-API — abgeholt, nicht abonniert.

Discord bietet zweierlei an: eine dauerhafte Gateway-Verbindung, die Ereignisse
zustellt, und ein gewöhnliches REST-API, das man fragen kann. Für einen Briefkasten
genügt das zweite. Der Stapellauf fragt, was seit dem letzten Mal dazugekommen ist,
holt es ab und legt es ab — genau die Store-and-Forward-Bauart, die den Kanal
überhaupt erst nützlich macht: einwerfen kann man immer, auch wenn hier nichts läuft.
Ein Prozess, der Tag und Nacht an einer WebSocket hängt, müsste dafür laufen, damit
nichts verlorengeht. Die dauerhafte Verbindung kommt erst mit dem Recorder-Bot (#8),
der Sprache mitschneidet, während sie gesprochen wird.

Deshalb auch kein ``discord.py``: das ist eine Gateway-Bibliothek mit eigenem
Ereignis-Loop. Hier reichen sechs Aufrufe über ``requests``, das für Foundry und
Ollama ohnehin im Bild ist.

Der Token steht in genau einem Header — nicht in einer Logzeile, nicht in ``repr``,
nicht in einer Fehlermeldung. Der Anhang wird **ohne** ihn geholt: die CDN-Adresse ist
bereits signiert, und einen Bot-Token an einen fremden Host zu schicken wäre der
kürzeste Weg nach draußen.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

from chronicle.config import Config, masked

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10"

GUILD_TEXT = 0

# Alles andere sind Beitritts-, Anheft- und Systemmeldungen; die sind kein Diktat.
STANDARD_NACHRICHT = 0

# Eine Nachricht je Einwurf, ein Lauf je Nacht — hundert sind reichlich, und was darüber
# liegt, holt der nächste Lauf: der Zeiger rückt nur über Erledigtes.
LIMIT = 100

DEFAULT_TIMEOUT = 60.0

BLOCK = 64 * 1024

MARKDOWN = "text/markdown"


class DiscordError(RuntimeError):
    """Alles, was das Leeren des Briefkastens verhindert."""


class DiscordNotConfigured(DiscordError):
    pass


class DiscordUnreachable(DiscordError):
    pass


@dataclass(frozen=True)
class Attachment:
    filename: str
    url: str
    size: int


@dataclass(frozen=True)
class Message:
    id: str
    content: str
    from_bot: bool
    attachments: tuple[Attachment, ...] = ()
    # Wann sie eingeworfen wurde, nicht wann sie abgeholt wird: der Briefkasten wird
    # nachts geleert, und ein Diktat vom Heimweg fände seine Szene sonst eine Nacht
    # später (#222).
    timestamp: str = ""


def _http_session() -> requests.Session:
    return requests.Session()


def _anhang(rohdaten: Mapping) -> Attachment:
    return Attachment(
        filename=str(rohdaten.get("filename") or ""),
        url=str(rohdaten.get("url") or ""),
        size=int(rohdaten.get("size") or 0),
    )


def _nachricht(rohdaten: Mapping) -> Message:
    autor = rohdaten.get("author")
    anhaenge = rohdaten.get("attachments")
    return Message(
        id=str(rohdaten.get("id") or ""),
        content=str(rohdaten.get("content") or ""),
        from_bot=bool(autor.get("bot")) if isinstance(autor, Mapping) else False,
        attachments=tuple(
            _anhang(eintrag)
            for eintrag in (anhaenge if isinstance(anhaenge, list) else ())
            if isinstance(eintrag, Mapping)
        ),
        timestamp=str(rohdaten.get("timestamp") or ""),
    )


class DiscordClient:
    def __init__(
        self,
        config: Config,
        *,
        http: Callable[[], object] = _http_session,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not config.discord_configured:
            raise DiscordNotConfigured(
                "Kein Discord-Bot-Token gesetzt; es fehlt: DISCORD_BOT_TOKEN"
            )
        self._token = str(config.discord_bot_token)
        self._http = http()
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"DiscordClient(token={masked(self._token)})"

    def _call(self, method: str, path: str, **kwargs):
        try:
            antwort = self._http.request(
                method,
                API + path,
                headers={"Authorization": f"Bot {self._token}"},
                timeout=self._timeout,
                **kwargs,
            )
            antwort.raise_for_status()
        except requests.RequestException as fehler:
            # Bewusst ohne ``raise ... from``: die verkettete Ursache trägt den Request
            # samt Authorization-Header, und der landet in jedem logger.exception.
            raise DiscordUnreachable(
                f"{method} {path} fehlgeschlagen: {type(fehler).__name__}"
            ) from None
        return antwort

    def _json(self, antwort, pfad: str):
        try:
            return antwort.json()
        except ValueError:
            raise DiscordUnreachable(f"{pfad} hat kein JSON geliefert") from None

    def _liste(self, pfad: str, was: str) -> list:
        rumpf = self._json(self._call("GET", pfad), pfad)
        if not isinstance(rumpf, list):
            raise DiscordUnreachable(f"{pfad} hat keine {was} geliefert")
        return rumpf

    def guild_channel_id(self, guild_id: str, kanal: str) -> str | None:
        """Der Textkanal **einer** Gilde — angegeben als Id oder als Name.

        Zwei Formen, weil die Einstellung zwei Herkünfte hat: ``/setup`` schreibt die Id des
        gewählten Kanals, ältere Runden und ``DISCORD_RECAP_CHANNEL`` tragen den Namen.

        Gesucht wird nur in dieser einen Gilde, und das ist der Punkt: »chronik« heißt in
        jeder zweiten Gilde ein Kanal, und eine Id aus einer fremden ist hier so wenig
        erreichbar wie ein fremder Name. Sonst stünde der Rückblick der einen Runde im
        Kanal einer anderen — ein Leck, das niemand bemerkte, weil es aussieht wie eine
        gelungene Zustellung.

        Eine Suche **über** die Gilden gibt es hier nicht mehr: sie war der Fehler in
        Schreib- (#182) und Leserichtung (#192), und in Leserichtung wog sie schwerer —
        ein Diktat aus einer fremden Gilde wird verschriftet und steht danach in der
        Chronik einer Gruppe, die es nie gesprochen hat.

        Wer darin schreiben darf, darf diktieren — die Autorisierung ist Discords eigenes
        Rechtemodell, hier gibt es keine zweite Liste.
        """
        logger.info("Discord: suche Kanal %s in Gilde %s", kanal, guild_id)
        gilden = self._liste("/users/@me/guilds", "Gildenliste")
        if not any(
            isinstance(gilde, Mapping) and str(gilde.get("id")) == str(guild_id) for gilde in gilden
        ):
            return None
        for eintrag in self._liste(f"/guilds/{guild_id}/channels", "Kanalliste"):
            if not isinstance(eintrag, Mapping) or eintrag.get("type") != GUILD_TEXT:
                continue
            kennung = str(eintrag.get("id") or "")
            if kennung and kanal in (kennung, eintrag.get("name")):
                return kennung
        return None

    def messages(
        self, channel_id: str, *, after: str | None = None, limit: int = LIMIT
    ) -> tuple[Message, ...]:
        params: dict[str, object] = {"limit": limit}
        if after:
            params["after"] = after
        pfad = f"/channels/{channel_id}/messages"
        rumpf = self._json(self._call("GET", pfad, params=params), pfad)
        if not isinstance(rumpf, list):
            raise DiscordUnreachable(f"{pfad} hat keine Nachrichtenliste geliefert")
        eintraege = (
            _nachricht(eintrag)
            for eintrag in rumpf
            if isinstance(eintrag, Mapping)
            and eintrag.get("id")
            and eintrag.get("type", STANDARD_NACHRICHT) == STANDARD_NACHRICHT
        )
        # Discord liefert die neueste zuerst; abgelegt wird in der Reihenfolge des
        # Einwurfs, und der Zeiger wandert mit ihr.
        return tuple(sorted(eintraege, key=lambda eintrag: int(eintrag.id)))

    def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        self._call(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me",
        )

    def post(self, channel_id: str, text: str) -> None:
        self._call("POST", f"/channels/{channel_id}/messages", json={"content": text})

    def post_embed(self, channel_id: str, embed: Mapping) -> None:
        self._call("POST", f"/channels/{channel_id}/messages", json={"embeds": [embed]})

    def post_file(self, channel_id: str, filename: str, inhalt: bytes, text: str = "") -> None:
        """Eine Datei in einen Kanal — ein Thread ist für Discord auch nur einer.

        Anhang und Begleittext gehen in *einer* Anfrage hinaus; getrennt stünden sie im
        Verlauf als zwei Nachrichten und ließen sich später umsortieren.
        """
        self._call(
            "POST",
            f"/channels/{channel_id}/messages",
            data={"payload_json": json.dumps({"content": text}, ensure_ascii=False)},
            files={"files[0]": (filename, inhalt, MARKDOWN)},
        )

    def reply(self, channel_id: str, message_id: str, text: str) -> None:
        self._call(
            "POST",
            f"/channels/{channel_id}/messages",
            json={"content": text, "message_reference": {"message_id": message_id}},
        )

    def download(self, attachment: Attachment, ziel: Path) -> None:
        """Holt den Anhang vom CDN — ohne Token, die Adresse ist schon signiert."""
        try:
            antwort = self._http.get(attachment.url, timeout=self._timeout, stream=True)
            antwort.raise_for_status()
            with ziel.open("wb") as datei:
                for stueck in antwort.iter_content(BLOCK):
                    datei.write(stueck)
        except requests.RequestException as fehler:
            ziel.unlink(missing_ok=True)
            raise DiscordUnreachable(
                f"Anhang »{attachment.filename}« nicht geholt: {type(fehler).__name__}"
            ) from None
