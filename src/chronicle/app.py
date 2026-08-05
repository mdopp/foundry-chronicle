"""Der Dienst: serverseitig gerendertes HTML, sonst nichts.

Fehlt die Foundry-Konfiguration, startet der Dienst trotzdem und erklärt auf ``/``,
was fehlt — eine harte Abhängigkeit rechtfertigt eine verständliche Meldung, keine
Startverweigerung.
"""

from __future__ import annotations

from flask import Flask, render_template

from chronicle import db
from chronicle.config import Config


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    app.config["CHRONICLE"] = config if config is not None else Config.from_env()
    db.init(app.config["CHRONICLE"].database_path)

    @app.get("/")
    def status() -> str:
        settings: Config = app.config["CHRONICLE"]
        return render_template(
            "status.html",
            config=settings,
            schema_version=db.current_schema_version(settings.database_path),
        )

    # Test-Seam und Install-Gate der ServiceBay-Box (servicebay.healthcheck).
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
