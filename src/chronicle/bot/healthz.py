"""Das Install-Gate der Box, bedient aus dem Bot-Prozess — zehn Zeilen HTTP, kein Flask.

``/healthz`` ist der eine Endpunkt, an dem der Dienst hängt: antwortet er nicht mit 200,
gilt die Installation als gescheitert. Bislang kam er aus der Betreiber-Seite; die fällt
mit #227, und der Bot ist der Prozess, der bleibt.

**Gebunden wird ausschließlich an die Schleife**, und das ist keine Vorgabe, sondern die
Eigenschaft: der Pod läuft im Host-Netz (#165), ein ``0.0.0.0`` stünde damit im ganzen
LAN offen — genau der Weg, über den sich in #190 jemand einen ``Remote-User`` erfinden
konnte. Der Poller der Box fragt ``localhost`` und liegt auf derselben Maschine; er
braucht nichts weiter. Deshalb gibt es hier keine Umgebungsvariable für die Adresse.

Der Endpunkt sagt nichts über den Zustand des Bots aus, sondern nur, dass der Prozess
lebt und antwortet. Mehr wäre eine Zusage, die niemand hält: eine Gateway-Verbindung, die
gerade neu aufgebaut wird, ist kein kranker Dienst.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"

PFAD = "/healthz"

ANTWORT = b'{"status": "ok"}'


class _Gate(BaseHTTPRequestHandler):
    # Der voreingestellte Kopf nennt Python-Version und Modulname. Beides sagt einem
    # Poller nichts und einem Neugierigen zu viel.
    server_version = "chronicle"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 — von BaseHTTPRequestHandler vorgegeben
        if self.path.split("?", 1)[0] != PFAD:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(ANTWORT)))
        self.end_headers()
        self.wfile.write(ANTWORT)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Der Poller fragt alle dreißig Sekunden; im Log des Bots stünde sonst nichts
        # anderes mehr.
        pass


class Gesundheit:
    """Der laufende Server: starten im Faden daneben, abwarten, schließen."""

    def __init__(self, port: int) -> None:
        self._server = ThreadingHTTPServer((HOST, port), _Gate)
        self._faden = threading.Thread(
            target=self._server.serve_forever, name="chronicle-healthz", daemon=True
        )
        self._faden.start()

    @property
    def adresse(self) -> tuple[str, int]:
        return self._server.server_address[0], self._server.server_address[1]

    def warten(self) -> None:
        """Hierbleiben, solange der Server läuft — der Aufrufer hat sonst nichts mehr vor."""
        self._faden.join()

    def schliessen(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._faden.join()


def starten(port: int) -> Gesundheit:
    server = Gesundheit(port)
    logger.info("Install-Gate: %s hört auf %s:%d", PFAD, *server.adresse)
    return server
