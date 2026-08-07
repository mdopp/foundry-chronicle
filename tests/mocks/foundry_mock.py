"""Ein Foundry, das den Handschlag wirklich spricht — gebaut allein aus der Doku.

Vorlage ist ``docs/foundry-zugriff.md`` und **nur** die: vier Schritte in dieser
Reihenfolge, sonst kommt keine Welt heraus.

1. ``GET /join`` setzt den ``session``-Cookie, auch ohne Anmeldung.
2. Socket.io mit diesem Cookie beantwortet ``getJoinData`` — die Benutzerliste mit IDs,
   vor jeder Anmeldung.
3. ``POST /join`` bindet die Sitzung an den Benutzer und setzt einen **neuen** Cookie.
4. Socket.io neu verbinden, mit neuem Cookie *und* ``?session=<id>``: der Server schickt
   ``session`` mit der ``userId``, erst danach beantwortet er ``world``.

Schritt 4 wird hier durchgesetzt — eine unangemeldete Verbindung bekommt auf ``world``
keine Daten. Ohne das könnte der Client die Anmeldung überspringen und der Test wäre
kein Beweis über den Handschlag.

Weicht ein echtes Foundry davon ab, ist die Doku zu korrigieren; dieser Mock zieht nicht
an Implementierungsdetails nach.

Die Welt ist erfunden. Ein echter Weltabzug trägt die Klarnamen aller Beteiligten und
gehört nie ins Repo.
"""

from __future__ import annotations

import itertools
import urllib.parse

import socketio
from flask import Flask, Response, jsonify, request

from . import adresse, serve

BENUTZER = "Chronistin"
PASSWORT = "passwort-nur-fuer-die-geschichte"

UNSER_KONTO = "u-chronistin"
LEITUNG = "u-leitung"
TARU = "u-taru"

# Zwei Figuren beweisen den Filter: die eine darf gar nicht auftauchen, die andere nur
# mit ihrem Namen.
VERBORGENE_FIGUR = "Der Wurm in der Zisterne"
LIMITIERTE_FIGUR = "Die Wirtin vom Halben Mond"

ANKUNFT = "m-ankunft"
STURZ = "m-sturz"
WIRTIN = "m-wirtin"
GM_FLUESTER = "m-fluester-gm"
BLINDER_WURF = "m-blind"

ANKUNFTSTEXT = "Wir erreichen den Halben Mond bei Regen."

STURZ_SUMME = 17
STURZ_HOFFNUNG = 9
STURZ_FURCHT = 4

WIRTIN_SUMME = 13

WELT_ID = "halber-mond"
WELT_TITEL = "Die Chronik vom Halben Mond"

WELT = {
    "world": {"id": WELT_ID, "title": WELT_TITEL},
    "system": {"id": "daggerheart"},
    "users": [
        {"_id": UNSER_KONTO, "name": BENUTZER, "role": 1},
        {"_id": LEITUNG, "name": "Spielleitung", "role": 4},
        {"_id": TARU, "name": "Taru", "role": 1},
    ],
    "actors": [
        {
            "_id": "a-nuala",
            "name": "Nuala Abendrot",
            "type": "character",
            "ownership": {"default": 2, TARU: 3},
        },
        {
            "_id": "a-hendrik",
            "name": "Hendrik Grauhand",
            "type": "character",
            "ownership": {"default": 0, UNSER_KONTO: 3},
        },
        {
            "_id": "a-wirtin",
            "name": LIMITIERTE_FIGUR,
            "type": "npc",
            "ownership": {"default": 1},
        },
        {
            "_id": "a-wurm",
            "name": VERBORGENE_FIGUR,
            "type": "npc",
            "ownership": {"default": 0, LEITUNG: 3},
        },
    ],
    # Bei Wurf-Nachrichten ist ``content`` serverseitig leer — die dokumentierte Falle.
    "messages": [
        {
            "_id": ANKUNFT,
            "timestamp": 1000,
            "author": TARU,
            "content": ANKUNFTSTEXT,
            "speaker": {"actor": "a-nuala", "alias": "Nuala Abendrot"},
        },
        {
            "_id": STURZ,
            "timestamp": 2000,
            "author": UNSER_KONTO,
            "content": "",
            "speaker": {"actor": "a-hendrik", "alias": "Hendrik Grauhand"},
            "system": {
                "roll": {
                    "title": "Agility Roll",
                    "total": STURZ_SUMME,
                    "formula": "1d12 + 1d12 + 4",
                    "type": "dualityRoll",
                    "isCritical": False,
                    "modifierTotal": 4,
                    "hope": {"dice": "d12", "value": STURZ_HOFFNUNG},
                    "fear": {"dice": "d12", "value": STURZ_FURCHT},
                }
            },
        },
        {
            "_id": WIRTIN,
            "timestamp": 3000,
            "author": TARU,
            "content": "",
            "speaker": {"actor": "a-nuala", "alias": "Nuala Abendrot"},
            "system": {
                "roll": {
                    "title": "Presence Roll",
                    "total": WIRTIN_SUMME,
                    "formula": "1d12 + 1d12 + 2",
                    "type": "dualityRoll",
                    "isCritical": False,
                    "modifierTotal": 2,
                    "hope": {"dice": "d12", "value": 8},
                    "fear": {"dice": "d12", "value": 3},
                }
            },
        },
        {
            "_id": GM_FLUESTER,
            "timestamp": 4000,
            "author": LEITUNG,
            "whisper": [LEITUNG],
            "content": "Der Wurm hoert jedes Wort im Schankraum.",
            "speaker": {},
        },
        {
            "_id": BLINDER_WURF,
            "timestamp": 5000,
            "author": LEITUNG,
            "blind": True,
            "content": "",
            "speaker": {},
            "system": {"roll": {"title": "Verborgener Wurf", "total": 20}},
        },
    ],
    "scenes": [{"_id": "s-halber-mond", "name": "Der Schankraum zum Halben Mond"}],
}


class MockFoundry:
    def __init__(self, welt: dict | None = None) -> None:
        self.welt = WELT if welt is None else welt
        self._gebunden: dict[str, str] = {}
        self._angemeldet: set[str] = set()
        self._zaehler = itertools.count(1)
        self.sio = socketio.Server(async_mode="threading")
        self._socket_io()
        self._server = serve(socketio.WSGIApp(self.sio, self._http()))

    @property
    def url(self) -> str:
        return adresse(self._server)

    def stop(self) -> None:
        self._server.shutdown()

    def _neue_sitzung(self, antwort: Response) -> str:
        sitzung = f"sitzung-{next(self._zaehler)}"
        antwort.set_cookie("session", sitzung)
        return sitzung

    def _http(self) -> Flask:
        app = Flask(__name__)

        @app.get("/join")
        def anmeldeseite() -> Response:
            antwort = jsonify({"active": False})
            self._neue_sitzung(antwort)
            return antwort

        @app.post("/join")
        def anmelden() -> Response:
            rumpf = request.get_json(silent=True) or {}
            bekannt = {benutzer["_id"] for benutzer in self.welt["users"]}
            if rumpf.get("userid") not in bekannt or rumpf.get("password") != PASSWORT:
                return jsonify({"status": "failed", "error": "Anmeldung abgelehnt"})
            antwort = jsonify({"status": "success", "redirect": "/game"})
            self._gebunden[self._neue_sitzung(antwort)] = rumpf["userid"]
            return antwort

        return app

    def _melde_sitzung(self, sid: str, sitzung: str, user_id: str) -> None:
        # Das session-Ereignis darf die CONNECT-Bestätigung nicht überholen: aus dem
        # connect-Handler heraus ginge es vor ihr auf die Leitung.
        self.sio.sleep(0.05)
        self.sio.emit("session", {"sessionId": sitzung, "userId": user_id}, to=sid)

    def _socket_io(self) -> None:
        @self.sio.event
        def connect(sid: str, environ: dict, auth: object = None) -> None:
            frage = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
            sitzung = (frage.get("session") or [""])[0]
            user_id = self._gebunden.get(sitzung)
            if user_id is not None:
                self._angemeldet.add(sid)
                self.sio.start_background_task(self._melde_sitzung, sid, sitzung, user_id)

        @self.sio.on("getJoinData")
        def join_data(sid: str) -> dict:
            return {"users": [dict(benutzer) for benutzer in self.welt["users"]]}

        @self.sio.on("world")
        def world(sid: str) -> dict | None:
            return self.welt if sid in self._angemeldet else None
