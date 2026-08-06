"""Ein Ollama, das antwortet wie eines — und einmal absichtlich eine Zahl erfindet.

Es spricht die beiden Endpunkte, die der Dienst benutzt: ``/api/tags`` für die Auswahl
in der Oberfläche und ``/api/chat`` für die Komposition.

Der Zweck ist die eine Antwort, die ein braves Modell nicht gibt: für eine benannte
Szene schreibt es eine Zahl, die in keinem Foundry-Fakt und in keiner Notiz steht. Die
Zahlenschranke ist die teuerste Regel des Hauses, und sie hatte bis hierher keinen
Beweis gegen ein Modell, das aktiv erfindet.

Welchen Auftrag es gerade hat, erkennt es am System-Text — so, wie ein Modell es müsste;
die Aufträge unterscheiden sich für den Leser sichtbar.
"""

from __future__ import annotations

from flask import Flask, Response, jsonify, request

from . import adresse, serve

MODELL = "chronist-mock"
# ``installed_models`` sortiert Einbettungsmodelle aus: sie schreiben keinen Text.
EINBETTUNG = "nomic-embed-mock"

ERFUNDENE_ZAHL = "4711"

FAEDEN_AUFTRAG = "offenen Fäden"
RUECKBLICK_AUFTRAG = "was zuletzt geschah"

SZENE = "Die Gruppe blieb beieinander und ging weiter, wie besprochen."
VERGIFTET = (
    "Der Sturz ging tief hinab; er kostete Hendrik "
    f"{ERFUNDENE_ZAHL} Lebenspunkte, bevor die Seile hielten."
)
RUECKBLICK = (
    "Die Gruppe erreichte den Halben Mond im Regen und nahm im Schankraum Platz. "
    "Hendrik stürzte in die Zisterne und wurde wieder herausgeholt. "
    "Die Wirtin wich der Frage nach dem Keller aus."
)
FAEDEN = (
    "- Der Keller unter dem Halben Mond bleibt unbetreten.\n"
    "- Die Wirtin schuldet der Gruppe eine Antwort."
)


class MockOllama:
    def __init__(self, *, vergiftete_szene: str) -> None:
        self._vergiftete_szene = vergiftete_szene
        self.antworten: list[str] = []
        self._server = serve(self._http())

    @property
    def url(self) -> str:
        return adresse(self._server)

    def stop(self) -> None:
        self._server.shutdown()

    def _schreibt(self, system: str, auftrag: str) -> str:
        if FAEDEN_AUFTRAG in system:
            return FAEDEN
        if RUECKBLICK_AUFTRAG in system:
            return RUECKBLICK
        return VERGIFTET if self._vergiftete_szene in auftrag else SZENE

    def _http(self) -> Flask:
        app = Flask(__name__)

        @app.get("/api/tags")
        def tags() -> Response:
            return jsonify({"models": [{"name": MODELL}, {"name": EINBETTUNG}]})

        @app.post("/api/chat")
        def chat() -> Response:
            rumpf = request.get_json(silent=True) or {}
            rollen = {n.get("role"): n.get("content", "") for n in rumpf.get("messages", [])}
            text = self._schreibt(rollen.get("system", ""), rollen.get("user", ""))
            self.antworten.append(text)
            return jsonify({"model": rumpf.get("model"), "message": {"content": text}})

        return app
