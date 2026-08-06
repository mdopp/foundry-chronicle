"""Der Dienst: serverseitig gerendertes HTML, sonst nichts.

Die Haustür steht am Proxy (ServiceBay-ADR 0001): Authelia setzt ``Remote-User``, die
App baut kein eigenes Login. Erzwungen wird der Header nur, wenn die Umgebung es sagt —
sonst wäre ``python -m chronicle`` ohne Proxy nicht startbar.

Fehlt die Foundry-Konfiguration, läuft der Dienst trotzdem und erklärt im Abschnitt
``Zustand`` der Einstellungen, was fehlt — eine harte Abhängigkeit rechtfertigt eine
verständliche Meldung, keine Startverweigerung. Mitgeschrieben wird auch dann.

Beim ersten Mal führt ``/einrichtung`` durch dieselben Speicherwege in Schritten, statt
fünf gleichrangige Reiter hinzustellen. Der Wizard schreibt nichts selbst: er füllt
dasselbe Formular wie ``/einstellungen`` und ruft denselben ``settings.save``-Weg auf.

``basis`` ist die Umgebung beim Start; gefragt wird nie sie, sondern
``settings.effective(basis)`` — ein in ``/einstellungen`` gesetzter Wert gewinnt und
wirkt ohne Neustart.
"""

from __future__ import annotations

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for

from chronicle import db, foundry, notes, people, protocol, recordings, search, settings
from chronicle.compose import client as sprachmodell
from chronicle.compose.client import ModelError
from chronicle.compose.service import RUECKBLICK
from chronicle.config import Config
from chronicle.transcribe import merge

REMOTE_USER_HEADER = "Remote-User"

UNKONFIGURIERT = "unkonfiguriert"
VERALTET = "veraltet"

# Die Einstellungen erklären den Verbindungszustand selbst und ausführlich, der Wizard
# ist die Einrichtung; ein Band darüber wäre dort nur eine Dopplung.
OHNE_BAND = frozenset(
    {
        "einstellungen",
        "einstellungen_speichern",
        "einrichtung",
        "einrichtung_schritt",
        "einrichtung_speichern",
        "healthz",
    }
)

# Die Reihenfolge des Wizards und die Felder, für die ein Schritt zuständig ist. Ein
# Schritt speichert nur seine eigenen — sonst nähme er den übrigen Schritten den Wert.
SCHRITTE = (
    ("foundry", ("foundry_url", "foundry_user", "foundry_password")),
    ("discord", ("discord_bot_token", "discord_recap_channel")),
    ("ollama", ("ollama_url", "ollama_model")),
)

SCHRITT_FELDER = dict(SCHRITTE)

UEBERSPRINGEN = "ueberspringen"


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
            g.abgewiesen = True
            return render_template("abgewiesen.html"), 403
        return None

    def zugang_ziel() -> str:
        # Eine bestehende Instanz hat längst eine Sitzung und käme über den Erststart
        # nie in den Wizard — das Band ist für sie der Weg hinein.
        if settings.onboarding_done(basis.database_path):
            return url_for("einstellungen")
        return url_for("einrichtung")

    @app.context_processor
    def verbindungsband() -> dict[str, str | None]:
        if g.get("abgewiesen") or request.endpoint in OHNE_BAND:
            return {"verbindung": None, "zugang": None}
        if not settings.effective(basis).foundry_configured:
            return {"verbindung": UNKONFIGURIERT, "zugang": zugang_ziel()}
        return {"verbindung": VERALTET if foundry.failed(basis) else None, "zugang": None}

    def erststart() -> bool:
        if settings.onboarding_done(basis.database_path):
            return False
        if settings.effective(basis).foundry_configured:
            return False
        return not notes.sessions(basis.database_path)

    @app.get("/")
    def sitzungen() -> str | Response:
        if erststart():
            return redirect(url_for("einrichtung"))
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

    def sitzungsseite(
        sitzung_id: int,
        diktat_fehler: str | None = None,
        transkript_fehler: str | None = None,
    ) -> str:
        daten = notes.session(basis.database_path, sitzung_id)
        if daten is None:
            abort(404)
        return render_template(
            "sitzung.html",
            sitzung=daten,
            aufnahmen=recordings.for_session(basis.database_path, sitzung_id),
            sprecher=people.speakers(basis.database_path),
            transkript=merge.conversation(basis.database_path, sitzung_id),
            frist=recordings.RETENTION_TAGE,
            diktat_fehler=diktat_fehler,
            transkript_fehler=transkript_fehler,
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

    @app.post("/sitzungen/<int:sitzung_id>/transkript")
    def transkript_uebernehmen(sitzung_id: int) -> Response | tuple[str, int]:
        gewaehlt = request.form.get("scene_id", "")
        if not gewaehlt.isdigit():
            abort(404)
        szene_id = int(gewaehlt)
        if notes.session_of_scene(basis.database_path, szene_id) != sitzung_id:
            abort(404)
        try:
            von = merge.marke_ms(request.form.get("von", ""))
            bis = merge.marke_ms(request.form.get("bis", ""))
        except ValueError as fehler:
            return sitzungsseite(sitzung_id, transkript_fehler=str(fehler)), 400
        abschnitt = merge.span(
            merge.conversation(basis.database_path, sitzung_id), von=von, bis=bis
        )
        if not abschnitt:
            return sitzungsseite(sitzung_id, transkript_fehler=merge.LEERE_SPANNE), 400
        notes.add_note(basis.database_path, szene_id, merge.note_text(abschnitt))
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

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

    @app.get("/zuordnung")
    def zuordnung() -> str:
        return render_template(
            "zuordnung.html",
            uebersicht=people.overview(basis.database_path),
            feld=people.FELD,
        )

    @app.post("/zuordnung")
    def zuordnung_speichern() -> Response:
        uebersicht = people.overview(basis.database_path)
        bekannt = {spieler.id for spieler in uebersicht.spieler}
        # Gespeichert wird nur, was in der angezeigten Liste stand: diese Tabelle sagt
        # später, wessen Stimme wessen Absatz ist, und nimmt deshalb keinen Wert an, den
        # niemand gesehen hat.
        auswahl = {}
        for person in uebersicht.personen:
            gewaehlt = request.form.get(people.FELD + person.discord_user_id, "").strip()
            auswahl[person.discord_user_id] = gewaehlt if gewaehlt in bekannt else ""
        people.confirm(basis.database_path, auswahl)
        return redirect(url_for("zuordnung"))

    def felder(*, mit_modellen: bool = True) -> dict[str, object]:
        aktuell = settings.effective(basis)
        adresse = aktuell.ollama_url or settings.DEFAULT_OLLAMA_URL
        modelle, hinweis = _modelle(adresse) if mit_modellen else ((), "")
        return {
            "foundry_url": aktuell.foundry_url or "",
            "foundry_user": aktuell.foundry_user or "",
            "passwort_gesetzt": bool(aktuell.foundry_password),
            "bot_token_gesetzt": bool(aktuell.discord_bot_token),
            "discord_recap_channel": aktuell.discord_recap_channel or "",
            "ollama_url": adresse,
            "ollama_eigen": adresse != settings.DEFAULT_OLLAMA_URL,
            "ollama_standard": settings.DEFAULT_OLLAMA_URL,
            "ollama_model": aktuell.ollama_model or "",
            "modelle": modelle,
            "modell_hinweis": hinweis,
        }

    def uebernehmen(namen: tuple[str, ...]) -> None:
        """Der einzige Schreibweg für die gepflegten Werte — Formular wie Wizard."""
        werte = {
            name: request.form.get(name, "") for name in namen if name not in settings.SECRET_KEYS
        }
        # Ein leer abgesendetes Geheimnis heißt »unverändert«, nicht »löschen« — sonst
        # wäre jedes Speichern der übrigen Werte ein Abmelden.
        for name in namen:
            if name in settings.SECRET_KEYS and request.form.get(name, "").strip():
                werte[name] = request.form[name]
        settings.save(basis.database_path, werte)

    @app.get("/einstellungen")
    def einstellungen() -> str:
        return render_template(
            "einstellungen.html",
            quellen=settings.sources(basis),
            config=settings.effective(basis),
            schema_version=db.current_schema_version(basis.database_path),
            abgleich=foundry.current(basis),
            remote_user=request.headers.get(REMOTE_USER_HEADER),
            **felder(),
        )

    @app.post("/einstellungen")
    def einstellungen_speichern() -> Response:
        uebernehmen(settings.KEYS)
        return redirect(url_for("einstellungen"))

    @app.get("/einrichtung")
    def einrichtung() -> Response:
        return redirect(url_for("einrichtung_schritt", schritt=SCHRITTE[0][0]))

    @app.get("/einrichtung/<schritt>")
    def einrichtung_schritt(schritt: str) -> str:
        if schritt not in SCHRITT_FELDER:
            abort(404)
        namen = [name for name, _ in SCHRITTE]
        return render_template(
            "einrichtung.html",
            schritt=schritt,
            nummer=namen.index(schritt) + 1,
            anzahl=len(namen),
            letzter=namen[-1] == schritt,
            **felder(mit_modellen=schritt == "ollama"),
        )

    @app.post("/einrichtung/<schritt>")
    def einrichtung_speichern(schritt: str) -> Response:
        if schritt not in SCHRITT_FELDER:
            abort(404)
        if request.form.get("tat") != UEBERSPRINGEN:
            uebernehmen(SCHRITT_FELDER[schritt])
        namen = [name for name, _ in SCHRITTE]
        naechster = namen.index(schritt) + 1
        if naechster == len(namen):
            settings.finish_onboarding(basis.database_path)
            return redirect(url_for("sitzungen"))
        return redirect(url_for("einrichtung_schritt", schritt=namen[naechster]))

    # Die Statusseite ist im Abschnitt »Zustand« der Einstellungen aufgegangen; die
    # Adresse steht in Lesezeichen und alten Bändern, deshalb 301 statt 404.
    @app.get("/status")
    def status() -> Response:
        return redirect(url_for("einstellungen", _anchor="zustand"), 301)

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
