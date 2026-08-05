"""Der Dienst: serverseitig gerendertes HTML, sonst nichts.

Die Haustür steht am Proxy (ServiceBay-ADR 0001): Authelia setzt ``Remote-User``, die
App baut kein eigenes Login. Erzwungen wird der Header nur, wenn die Umgebung es sagt —
sonst wäre ``python -m chronicle`` ohne Proxy nicht startbar.

Fehlt die Foundry-Konfiguration, läuft der Dienst trotzdem und erklärt auf ``/status``,
was fehlt — eine harte Abhängigkeit rechtfertigt eine verständliche Meldung, keine
Startverweigerung. Mitgeschrieben wird auch dann.
"""

from __future__ import annotations

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from chronicle import db, foundry, notes
from chronicle.config import Config

REMOTE_USER_HEADER = "Remote-User"


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    settings = config if config is not None else Config.from_env()
    app.config["CHRONICLE"] = settings
    db.init(settings.database_path)

    @app.before_request
    def tuersteher() -> tuple[str, int] | None:
        # /healthz ist das Install-Gate der Box und wird am Proxy vorbei abgefragt.
        if not settings.require_remote_user or request.endpoint == "healthz":
            return None
        if not request.headers.get(REMOTE_USER_HEADER):
            return render_template("abgewiesen.html"), 403
        return None

    @app.get("/")
    def sitzungen() -> str:
        return render_template(
            "sitzungen.html",
            sitzungen=notes.sessions(settings.database_path),
            heute=notes.today(),
        )

    @app.post("/")
    def neue_sitzung() -> Response:
        sitzung_id = notes.create_session(
            settings.database_path,
            played_on=request.form.get("played_on", ""),
            title=request.form.get("title", ""),
        )
        return redirect(url_for("sitzung", sitzung_id=sitzung_id))

    @app.get("/sitzungen/<int:sitzung_id>")
    def sitzung(sitzung_id: int) -> str:
        daten = notes.session(settings.database_path, sitzung_id)
        if daten is None:
            abort(404)
        return render_template("sitzung.html", sitzung=daten)

    @app.post("/sitzungen/<int:sitzung_id>/szenen")
    def neue_szene(sitzung_id: int) -> Response:
        szene_id = notes.add_scene(
            settings.database_path, sitzung_id, title=request.form.get("title", "")
        )
        if szene_id is None:
            abort(404)
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

    @app.post("/szenen/<int:szene_id>/notizen")
    def neue_notiz(szene_id: int) -> Response:
        sitzung_id = notes.session_of_scene(settings.database_path, szene_id)
        if sitzung_id is None:
            abort(404)
        notes.add_note(settings.database_path, szene_id, request.form.get("text", ""))
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

    @app.get("/status")
    def status() -> str:
        return render_template(
            "status.html",
            config=settings,
            schema_version=db.current_schema_version(settings.database_path),
            abgleich=foundry.current(settings),
            remote_user=request.headers.get(REMOTE_USER_HEADER),
        )

    # Test-Seam und Install-Gate der ServiceBay-Box (servicebay.healthcheck).
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
