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

    def channel_id(self, name: str) -> str | None:
        """Der eine Kanal, gefunden über die Namenskonvention.

        Wer darin schreiben darf, darf diktieren — die Autorisierung ist Discords
        eigenes Rechtemodell, hier gibt es keine zweite Liste.
        """
        logger.info("Discord: suche Kanal #%s", name)
        for gilde in self._liste("/users/@me/guilds", "Gildenliste"):
            kennung = gilde.get("id") if isinstance(gilde, Mapping) else None
            if not kennung:
                continue
            for kanal in self._liste(f"/guilds/{kennung}/channels", "Kanalliste"):
                if not isinstance(kanal, Mapping) or not kanal.get("id"):
                    continue
                if kanal.get("name") == name and kanal.get("type") == GUILD_TEXT:
                    return str(kanal["id"])
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
