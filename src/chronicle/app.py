"""Der Dienst: serverseitig gerendertes HTML, sonst nichts.

Die Haustür steht am Proxy (ServiceBay-ADR 0001): Authelia setzt ``Remote-User``, die
App baut kein eigenes Login. Erzwungen wird der Header nur, wenn die Umgebung es sagt —
sonst wäre ``python -m chronicle`` ohne Proxy nicht startbar.

Fehlt die Foundry-Konfiguration, läuft der Dienst trotzdem und erklärt auf ``/status``,
was fehlt — eine harte Abhängigkeit rechtfertigt eine verständliche Meldung, keine
Startverweigerung. Mitgeschrieben wird auch dann.

``basis`` ist die Umgebung beim Start; gefragt wird nie sie, sondern
``settings.effective(basis)`` — ein in ``/einstellungen`` gesetzter Wert gewinnt und
wirkt ohne Neustart.
"""

from __future__ import annotations

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from chronicle import db, foundry, notes, protocol, recordings, search, settings
from chronicle.compose import client as sprachmodell
from chronicle.compose.client import ModelError
from chronicle.compose.service import RUECKBLICK
from chronicle.config import Config

REMOTE_USER_HEADER = "Remote-User"


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    basis = config if config is not None else Config.from_env()
    app.config["CHRONICLE"] = basis
    app.config["MAX_CONTENT_LENGTH"] = recordings.MAX_BYTES
    db.init(basis.database_path)

    @app.before_request
    def tuersteher() -> tuple[str, int] | None:
        # /healthz ist das Install-Gate der Box und wird am Proxy vorbei abgefragt.
        if not basis.require_remote_user or request.endpoint == "healthz":
            return None
        if not request.headers.get(REMOTE_USER_HEADER):
            return render_template("abgewiesen.html"), 403
        return None

    @app.get("/")
    def sitzungen() -> str:
        return render_template(
            "sitzungen.html",
            sitzungen=notes.sessions(basis.database_path),
            heute=notes.today(),
        )

    @app.post("/")
    def neue_sitzung() -> Response:
        sitzung_id = notes.create_session(
            basis.database_path,
            played_on=request.form.get("played_on", ""),
            title=request.form.get("title", ""),
        )
        return redirect(url_for("sitzung", sitzung_id=sitzung_id))

    def sitzungsseite(sitzung_id: int, diktat_fehler: str | None = None) -> str:
        daten = notes.session(basis.database_path, sitzung_id)
        if daten is None:
            abort(404)
        return render_template(
            "sitzung.html",
            sitzung=daten,
            aufnahmen=recordings.for_session(basis.database_path, sitzung_id),
            frist=recordings.RETENTION_TAGE,
            diktat_fehler=diktat_fehler,
        )

    @app.get("/sitzungen/<int:sitzung_id>")
    def sitzung(sitzung_id: int) -> str:
        return sitzungsseite(sitzung_id)

    @app.post("/sitzungen/<int:sitzung_id>/szenen")
    def neue_szene(sitzung_id: int) -> Response:
        szene_id = notes.add_scene(
            basis.database_path, sitzung_id, title=request.form.get("title", "")
        )
        if szene_id is None:
            abort(404)
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

    @app.post("/szenen/<int:szene_id>/notizen")
    def neue_notiz(szene_id: int) -> Response:
        sitzung_id = notes.session_of_scene(basis.database_path, szene_id)
        if sitzung_id is None:
            abort(404)
        notes.add_note(basis.database_path, szene_id, request.form.get("text", ""))
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

    @app.post("/sitzungen/<int:sitzung_id>/diktat")
    def neues_diktat(sitzung_id: int) -> Response | tuple[str, int]:
        if notes.session(basis.database_path, sitzung_id) is None:
            abort(404)
        try:
            recordings.accept(basis, sitzung_id, request.files.get("datei"))
        except recordings.Rejected as fehler:
            return sitzungsseite(sitzung_id, diktat_fehler=str(fehler)), 400
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor="diktat"))

    @app.post("/aufnahmen/<int:aufnahme_id>/notiz")
    def diktat_uebernehmen(aufnahme_id: int) -> Response:
        aufnahme = recordings.get(basis.database_path, aufnahme_id)
        if aufnahme is None or not aufnahme.text:
            abort(404)
        gewaehlt = request.form.get("scene_id", "")
        if not gewaehlt.isdigit():
            abort(404)
        szene_id = int(gewaehlt)
        if notes.session_of_scene(basis.database_path, szene_id) != aufnahme.session_id:
            abort(404)
        notes.add_note(basis.database_path, szene_id, aufnahme.text)
        return redirect(
            url_for("sitzung", sitzung_id=aufnahme.session_id, _anchor=f"szene-{szene_id}")
        )

    @app.errorhandler(413)
    def zu_gross(_fehler: object) -> tuple[str, int]:
        return render_template("zu_gross.html", grenze=recordings.MAX_BYTES // (1024 * 1024)), 413

    @app.get("/protokolle")
    def protokolle() -> str:
        return render_template("protokolle.html", eintraege=protocol.entries(basis.database_path))

    @app.get("/sitzungen/<int:sitzung_id>/protokoll")
    def protokoll(sitzung_id: int) -> str:
        daten = notes.session(basis.database_path, sitzung_id)
        if daten is None:
            abort(404)
        return render_template(
            "protokoll.html",
            sitzung=daten,
            protokoll=protocol.stored(basis.database_path, sitzung_id),
            rueckblick=protocol.stored(basis.database_path, sitzung_id, RUECKBLICK),
        )

    @app.get("/suche")
    def suche() -> str:
        return render_template(
            "suche.html",
            ergebnis=search.find(basis.database_path, request.args.get("q", "")),
        )

    @app.get("/einstellungen")
    def einstellungen() -> str:
        aktuell = settings.effective(basis)
        adresse = aktuell.ollama_url or settings.DEFAULT_OLLAMA_URL
        modelle, hinweis = _modelle(adresse)
        return render_template(
            "einstellungen.html",
            foundry_url=aktuell.foundry_url or "",
            foundry_user=aktuell.foundry_user or "",
            passwort_gesetzt=bool(aktuell.foundry_password),
            bot_token_gesetzt=bool(aktuell.discord_bot_token),
            ollama_url=adresse,
            ollama_model=aktuell.ollama_model or "",
            modelle=modelle,
            modell_hinweis=hinweis,
            quellen=settings.sources(basis),
        )

    @app.post("/einstellungen")
    def einstellungen_speichern() -> Response:
        werte = {
            name: request.form.get(name, "")
            for name in settings.KEYS
            if name not in settings.SECRET_KEYS
        }
        # Ein leer abgesendetes Geheimnis heißt »unverändert«, nicht »löschen« — sonst
        # wäre jedes Speichern der übrigen Werte ein Abmelden.
        for name in settings.SECRET_KEYS:
            if request.form.get(name, "").strip():
                werte[name] = request.form[name]
        settings.save(basis.database_path, werte)
        return redirect(url_for("einstellungen"))

    @app.get("/status")
    def status() -> str:
        return render_template(
            "status.html",
            config=settings.effective(basis),
            quellen=settings.sources(basis),
            schema_version=db.current_schema_version(basis.database_path),
            abgleich=foundry.current(basis),
            remote_user=request.headers.get(REMOTE_USER_HEADER),
        )

    # Test-Seam und Install-Gate der ServiceBay-Box (servicebay.healthcheck).
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _modelle(adresse: str) -> tuple[tuple[str, ...], str]:
    try:
        namen = sprachmodell.installed_models(adresse)
    except ModelError as fehler:
        return (), f"{fehler} — Modellnamen von Hand eintragen."
    if not namen:
        return (), f"{adresse} antwortet, hat aber kein Textmodell installiert."
    return namen, f"{len(namen)} Modelle auf {adresse} gefunden."
