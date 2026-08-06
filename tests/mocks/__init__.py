"""Die Mocks der Geschichte sind echte Server, keine Funktions-Fakes.

Der Handschlag mit Foundry und der Aufruf des Sprachmodells sind die beiden Außengrenzen
des Dienstes. Über Funktions-Fakes geprüft, beweisen sie nur, dass der Code sich selbst
aufruft; über eine echte Verbindung geprüft, beweisen sie Cookie, Query, Ereignisfolge
und Antwortform mit. Deshalb läuft hier ein WSGI-Server auf einem Wegwerf-Port.

Das kostet keine neue Abhängigkeit: Werkzeug kommt mit Flask, und die Server-Hälfte von
python-socketio braucht nur ``simple-websocket``, das python-engineio ohnehin mitbringt.
Deshalb steht im dev-Extra dafür nichts.
"""

from __future__ import annotations

import threading

from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server


class _Stumm(WSGIRequestHandler):
    def log(self, *args: object, **kwargs: object) -> None:
        """Ein Testlauf ist kein Webserver-Log — auch der Abbau einer WebSocket nicht."""


def serve(app: object) -> BaseWSGIServer:
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=_Stumm)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def adresse(server: BaseWSGIServer) -> str:
    return f"http://127.0.0.1:{server.server_port}"
