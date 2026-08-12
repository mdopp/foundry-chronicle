"""Der Handschlag — aus dem Browser-Client nachgebaut, siehe docs/foundry-zugriff.md.

Vier Schritte, alle nötig, in dieser Reihenfolge:

1. ``GET /join`` holt einen ``session``-Cookie, auch ohne Anmeldung.
2. Socket.io mit diesem Cookie verbinden und ``getJoinData`` senden → Benutzerliste mit
   IDs. Schritt 3 braucht die ID, nicht den Namen.
3. ``POST /join`` bindet die Sitzung an den Benutzer; die Antwort setzt einen neuen Cookie.
4. Socket.io neu verbinden — neuer Cookie *und* ``?session=<id>`` — auf das
   ``session``-Ereignis warten und dann ``world`` abrufen.

Es gibt keinen API-Token: der Zugang ist Benutzer und Passwort eines echten Kontos.
Adresse und Benutzer kommen aus der Konfiguration, **das Passwort als Argument** — es wird
nirgends gespeichert und lebt nur im Arbeitsspeicher (``chronicle.zugang``).

``fetch_world`` fragt einmal und legt auf. ``verbindung`` macht denselben Handschlag und
lässt die Leitung offen, damit ein Aufrufer hören kann, was der Server von sich aus
schickt — was das ist, weiß hier noch niemand (#146).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

import requests
import socketio

from chronicle.config import Config

logger = logging.getLogger(__name__)

JOIN_PATH = "/join"
SESSION_COOKIE = "session"

# python-socketio nimmt ``*`` als Handler für alles, wofür kein eigener registriert ist.
AUFFANG = "*"
DEFAULT_TIMEOUT = 120.0


class FoundryError(RuntimeError):
    """Alles, was den Abgleich verhindert — der Dienst zeigt daraufhin den letzten Stand."""


class FoundryNotConfigured(FoundryError):
    pass


class FoundryUnreachable(FoundryError):
    pass


class FoundryLoginFailed(FoundryError):
    pass


OHNE_PASSWORT = (
    "Für den Abgleich fehlt das Foundry-Passwort. Es wird nirgends gespeichert — es "
    "gilt für einen Abgleich und wird danach vergessen."
)


def _zugang_fehlt(felder: tuple[str, ...]) -> str:
    aufzaehlung = felder[0] if len(felder) == 1 else f"{', '.join(felder[:-1])} und {felder[-1]}"
    return (
        f"Für den Zugang zu Foundry {'fehlt' if len(felder) == 1 else 'fehlen'} noch "
        f"{aufzaehlung}. Eintragen kannst du das in den Einstellungen."
    )


def _http_session() -> requests.Session:
    return requests.Session()


def _socket_client() -> socketio.Client:
    return socketio.Client()


class FoundryClient:
    def __init__(
        self,
        config: Config,
        passwort: str | None = None,
        *,
        http: Callable[[], object] = _http_session,
        socket: Callable[[], object] = _socket_client,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not config.foundry_configured:
            # Der Grund wird in den Einstellungen angezeigt und landet in der SQLite; die
            # Namen aus der Umgebung bleiben deshalb im Log.
            logger.warning(
                "Foundry ist nicht konfiguriert; es fehlt: %s",
                ", ".join(config.missing_foundry_variables),
            )
            raise FoundryNotConfigured(_zugang_fehlt(config.missing_foundry_fields))
        if not passwort:
            logger.warning("Foundry-Abgleich ohne Passwort im Speicher")
            raise FoundryNotConfigured(OHNE_PASSWORT)
        self._config = config
        self._passwort = passwort
        self._http_factory = http
        self._socket_factory = socket
        self._timeout = timeout

    @property
    def _base(self) -> str:
        return str(self._config.foundry_url).rstrip("/")

    def fetch_world(self) -> tuple[str, Mapping]:
        logger.info("Foundry-Abgleich: %s als %s", self._base, self._config.foundry_user)
        http = self._http_factory()
        user_id = self._handschlag(http)
        socket = self._angemeldet(http, user_id)
        try:
            return user_id, self._call(socket, "world")
        finally:
            socket.disconnect()

    @contextmanager
    def verbindung(self, *, mithoeren: Callable[..., None] | None = None) -> Iterator:
        """Dieselbe angemeldete Leitung, aber offen — der Aufrufer hört zu, statt zu fragen.

        ``mithoeren`` wird **vor** dem Verbinden als Auffanghandler gesetzt; danach
        gesetzt entginge ihm, was der Server gleich nach dem Verbinden schickt. Was
        socket.io selbst auslöst — ``connect``, ``disconnect``, ``connect_error`` —
        erreicht ihn nicht, das sind reservierte Ereignisse der Bibliothek.
        """
        logger.info("Foundry-Leitung: %s als %s", self._base, self._config.foundry_user)
        http = self._http_factory()
        socket = self._angemeldet(http, self._handschlag(http), mithoeren=mithoeren)
        try:
            yield socket
        finally:
            socket.disconnect()

    def _handschlag(self, http) -> str:
        self._request(http, "GET", JOIN_PATH)
        user_id = self._user_id(self._join_data(http))
        self._login(http, user_id)
        return user_id

    def _request(self, http, method: str, path: str, **kwargs):
        try:
            antwort = http.request(method, self._base + path, timeout=self._timeout, **kwargs)
            antwort.raise_for_status()
        except requests.RequestException as fehler:
            # Bewusst ohne ``raise ... from``: der Anmelde-Aufruf trägt das Passwort im
            # Rumpf, und eine verkettete Ursache landet in jedem logger.exception.
            raise FoundryUnreachable(
                f"{method} {path} fehlgeschlagen: {type(fehler).__name__}"
            ) from None
        return antwort

    def _cookie_header(self, http) -> str:
        return "; ".join(f"{name}={wert}" for name, wert in http.cookies.items())

    def _connect(self, http, *, session_id: str | None = None, on_session=None, mithoeren=None):
        socket = self._socket_factory()
        if on_session is not None:
            socket.on(SESSION_COOKIE, on_session)
        if mithoeren is not None:
            socket.on(AUFFANG, mithoeren)
        url = self._base if session_id is None else f"{self._base}?session={session_id}"
        try:
            socket.connect(
                url,
                headers={"Cookie": self._cookie_header(http)},
                transports=["websocket"],
                wait_timeout=self._timeout,
            )
        except socketio.exceptions.ConnectionError as fehler:
            raise FoundryUnreachable(
                f"Socket.io-Verbindung fehlgeschlagen: {type(fehler).__name__}"
            ) from None
        return socket

    def _call(self, socket, event: str) -> Mapping:
        try:
            antwort = socket.call(event, timeout=self._timeout)
        except socketio.exceptions.SocketIOError:
            raise FoundryUnreachable(f"Foundry hat auf {event!r} nicht geantwortet") from None
        if not isinstance(antwort, Mapping):
            raise FoundryUnreachable(f"Foundry hat auf {event!r} keine Daten geschickt")
        return antwort

    def _join_data(self, http) -> Mapping:
        socket = self._connect(http)
        try:
            return self._call(socket, "getJoinData")
        finally:
            socket.disconnect()

    def _user_id(self, join_data: Mapping) -> str:
        name = self._config.foundry_user
        for user in join_data.get("users") or []:
            if isinstance(user, Mapping) and user.get("name") == name:
                return str(user.get("_id") or user.get("id") or "")
        raise FoundryLoginFailed(f"Benutzer {name!r} ist in dieser Welt nicht bekannt")

    def _login(self, http, user_id: str) -> None:
        antwort = self._request(
            http,
            "POST",
            JOIN_PATH,
            json={
                "action": "join",
                "userid": user_id,
                "password": self._passwort,
            },
        )
        try:
            rumpf = antwort.json()
        except ValueError:
            rumpf = {}
        if not isinstance(rumpf, Mapping) or rumpf.get("status") == "failed" or rumpf.get("error"):
            raise FoundryLoginFailed("Foundry hat die Anmeldung abgelehnt")

    def _angemeldet(self, http, user_id: str, *, mithoeren=None):
        angemeldet = threading.Event()
        sitzung: dict = {}

        def on_session(payload=None) -> None:
            # ``session`` hat einen eigenen Handler und käme beim Auffanghandler sonst nie
            # an — wer mithört, soll aber das erste Ereignis der Leitung auch sehen.
            if mithoeren is not None:
                mithoeren(SESSION_COOKIE, payload)
            if isinstance(payload, Mapping):
                sitzung.update(payload)
            angemeldet.set()

        socket = self._connect(
            http,
            session_id=http.cookies.get(SESSION_COOKIE),
            on_session=on_session,
            mithoeren=mithoeren,
        )
        try:
            if not angemeldet.wait(self._timeout):
                raise FoundryLoginFailed("Foundry hat kein session-Ereignis geschickt")
            if str(sitzung.get("userId")) != str(user_id):
                raise FoundryLoginFailed(
                    "Die Sitzung wurde nicht an den erwarteten Benutzer gebunden"
                )
        except FoundryError:
            socket.disconnect()
            raise
        return socket
