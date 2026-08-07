"""Der Dienst: serverseitig gerendertes HTML, sonst nichts.

Die Haustür steht am Proxy (ServiceBay-ADR 0001): Authelia setzt ``Remote-User``, die
App baut kein eigenes Login. Erzwungen wird der Header nur, wenn die Umgebung es sagt —
sonst wäre ``python -m chronicle`` ohne Proxy nicht startbar.

Fehlt die Foundry-Konfiguration, läuft der Dienst trotzdem und erklärt in der
Foundry-Karte der Einstellungen, was fehlt — eine harte Abhängigkeit rechtfertigt eine
verständliche Meldung, keine Startverweigerung. Mitgeschrieben wird auch dann.

Beim ersten Mal führt ``/einrichtung`` durch dieselben Speicherwege in Schritten, statt
fünf gleichrangige Reiter hinzustellen. Der Wizard schreibt nichts selbst: er füllt
dasselbe Formular wie ``/einstellungen`` und ruft denselben ``settings.save``-Weg auf.

``basis`` ist die Umgebung beim Start; gefragt wird nie sie, sondern
``settings.effective(basis, runde)`` — ein in ``/einstellungen`` gesetzter Wert gewinnt und
wirkt ohne Neustart.

Diese Oberfläche kennt **keine Runden**: sie arbeitet stillschweigend in der ersten. Mit
#69 verschwindet sie ganz, Discord wird die Oberfläche — bis dahin ist das die ehrlichste
Übersetzung des alten »eine Instanz pro Gruppe« in die neue Welt.

Der nächtliche Lauf hängt an ``dienst()`` und nicht an ``create_app``: eine App, die nur
befragt wird — im Test, im Skript —, soll nicht anfangen zu arbeiten. Er läuft hier und
nicht im Aufnahme-Bot, weil es den ohne Bot-Token gar nicht gibt (siehe
``chronicle.nightly``).
"""

from __future__ import annotations

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for

from chronicle import (
    db,
    foundry,
    instanz,
    jobs,
    nightly,
    notes,
    people,
    protocol,
    recordings,
    register,
    roles,
    search,
    settings,
    zugang,
)
from chronicle import runde as runden
from chronicle.compose import client as sprachmodell
from chronicle.compose.client import ModelError
from chronicle.compose.service import RUECKBLICK
from chronicle.config import Config
from chronicle.transcribe import merge

REMOTE_USER_HEADER = "Remote-User"

UNKONFIGURIERT = "unkonfiguriert"
VERALTET = "veraltet"
ABGLEICH_LAEUFT = "laeuft"

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
    ("foundry", ("foundry_url", "foundry_user")),
    ("discord", ("discord_bot_token", "discord_recap_channel")),
    ("ollama", ("ollama_url", "ollama_model")),
)

SCHRITT_FELDER = dict(SCHRITTE)

UEBERSPRINGEN = "ueberspringen"
SPAETER = "spaeter"

# Was die Runde einmal einrichtet, statt es beim Spielen zu brauchen. Alles andere —
# Sitzungen, Notizen, Diktate, Protokolle, Suche — steht jedem offen.
VERWALTUNG = frozenset(
    {
        "einstellungen",
        "einstellungen_speichern",
        "einrichtung",
        "einrichtung_schritt",
        "einrichtung_speichern",
        "zuordnung",
        "zuordnung_speichern",
        "register_vorschlaege",
        "register_entscheiden",
        "abgleich_anstossen",
        "chronik_anstossen",
    }
)

BESTAETIGT = "gruppe_bestaetigt"


def create_app(config: Config | None = None, *, zeitplan: bool = False) -> Flask:
    app = Flask(__name__)
    basis = config if config is not None else Config.from_env()
    app.config["CHRONICLE"] = basis
    app.config["MAX_CONTENT_LENGTH"] = recordings.MAX_BYTES
    db.init(basis.database_path)
    # Die Oberfläche kennt keine Runden: sie arbeitet stillschweigend in der ersten. Mit
    # #69 verschwindet sie, und mit ihr diese Zeile — bis dahin ist sie die ehrlichste
    # Übersetzung von »eine Instanz pro Gruppe« in die neue Welt.
    runde = runden.erste(basis.database_path)
    if zeitplan:
        nightly.starten(basis)

    @app.before_request
    def tuersteher() -> tuple[str, int] | None:
        # /healthz ist das Install-Gate der Box und wird am Proxy vorbei abgefragt.
        if not basis.require_remote_user or request.endpoint == "healthz":
            return None
        if not request.headers.get(REMOTE_USER_HEADER):
            g.abgewiesen = True
            return render_template("abgewiesen.html"), 403
        return None

    @app.before_request
    def verwaltungsrolle() -> tuple[str, int] | None:
        # Ohne gesetzte Gruppe ist jeder Verwalter — der Wizard beim ersten Start kann
        # sich also nicht selbst aussperren, denn dort ist noch keine gesetzt.
        if request.endpoint not in VERWALTUNG:
            return None
        if roles.ist_verwalter(request, basis.database_path):
            return None
        return render_template("keine_verwaltung.html"), 403

    def zugang_ziel() -> str:
        # Nach einem »Später« ist die Einrichtung abgehakt und das Band führt in die
        # Einstellungen — sonst zurück in den Wizard.
        if instanz.onboarding_done(basis.database_path):
            return url_for("einstellungen")
        return url_for("einrichtung")

    @app.context_processor
    def verbindungsband() -> dict[str, object]:
        # ``abgleich_job`` steht auch dort, wo kein Band hängt: die Einstellungen zeigen
        # denselben Lauf im Abschnitt »Zustand«, nur ausführlicher.
        if g.get("abgewiesen"):
            return {"verbindung": None, "zugang": None, "abgleich_job": None, "verwalter": False}
        verwalter = roles.ist_verwalter(request, basis.database_path)
        lauf = jobs.latest(runde, jobs.ABGLEICH)
        rahmen = {"verbindung": None, "zugang": None, "abgleich_job": lauf, "verwalter": verwalter}
        if request.endpoint in OHNE_BAND:
            return rahmen
        if not settings.effective(basis, runde).foundry_configured:
            # Der Weg in die Einrichtung steht nur dem offen, der sie auch gehen darf.
            return rahmen | {
                "verbindung": UNKONFIGURIERT,
                "zugang": zugang_ziel() if verwalter else None,
            }
        if lauf is not None and lauf.laeuft:
            return rahmen | {"verbindung": ABGLEICH_LAEUFT}
        return rahmen | {"verbindung": VERALTET if foundry.failed(basis, runde) else None}

    def zurueck(fallback: str) -> str:
        ziel = request.form.get("zurueck", "")
        # Nur ein Pfad dieses Dienstes — »//woanders« führte aus ihm hinaus.
        return ziel if ziel.startswith("/") and not ziel.startswith("//") else fallback

    @app.post("/abgleich")
    def abgleich_anstossen() -> Response:
        # Das Passwort kommt mit dem Knopf und geht in den Arbeitsspeicher, nicht in die
        # Datenbank; der Lauf verbraucht es. Der ganze Weg fällt mit #69 weg — in Discord
        # fragt ein Modal danach.
        zugang.merken(runde, request.form.get("foundry_password", ""))
        jobs.start(basis, runde, jobs.ABGLEICH, lambda: jobs.abgleich(basis, runde))
        return redirect(zurueck(url_for("einstellungen", _anchor="zustand")))

    @app.post("/sitzungen/<int:sitzung_id>/chronik")
    def chronik_anstossen(sitzung_id: int) -> Response:
        if notes.session(runde, sitzung_id) is None:
            abort(404)
        jobs.start(
            basis,
            runde,
            jobs.CHRONIK,
            lambda: jobs.chronik(basis, runde, sitzung_id),
            session_id=sitzung_id,
        )
        return redirect(url_for("protokoll", sitzung_id=sitzung_id))

    def einrichtung_offen() -> bool:
        # Die Einrichtung steht offen, solange Foundry fehlt — nicht nur beim ersten
        # Aufruf. Wer sie nicht jetzt machen will, legt sie mit »Später« beiseite.
        if instanz.onboarding_done(basis.database_path):
            return False
        return not settings.effective(basis, runde).foundry_configured

    @app.get("/")
    def sitzungen() -> str | Response:
        # Wer die Einrichtung nicht machen darf, wird auch nicht in sie geschickt.
        if einrichtung_offen() and roles.ist_verwalter(request, basis.database_path):
            return redirect(url_for("einrichtung"))
        return render_template(
            "sitzungen.html",
            sitzungen=notes.sessions(runde),
            heute=notes.today(),
        )

    @app.post("/")
    def neue_sitzung() -> Response:
        sitzung_id = notes.create_session(
            runde,
            played_on=request.form.get("played_on", ""),
            title=request.form.get("title", ""),
        )
        return redirect(url_for("sitzung", sitzung_id=sitzung_id))

    def sitzungsseite(
        sitzung_id: int,
        diktat_fehler: str | None = None,
        transkript_fehler: str | None = None,
    ) -> str:
        daten = notes.session(runde, sitzung_id)
        if daten is None:
            abort(404)
        return render_template(
            "sitzung.html",
            sitzung=daten,
            aufnahmen=recordings.for_session(runde, sitzung_id),
            sprecher=people.speakers(runde),
            transkript=merge.conversation(runde, sitzung_id),
            frist=recordings.RETENTION_TAGE,
            diktat_fehler=diktat_fehler,
            transkript_fehler=transkript_fehler,
            **chronik_stand(sitzung_id),
        )

    def chronik_stand(sitzung_id: int) -> dict[str, object]:
        return {
            "chronik_job": jobs.latest(runde, jobs.CHRONIK, sitzung_id),
            "chronik_laeuft": jobs.running(runde, jobs.CHRONIK),
        }

    @app.get("/sitzungen/<int:sitzung_id>")
    def sitzung(sitzung_id: int) -> str:
        return sitzungsseite(sitzung_id)

    @app.post("/sitzungen/<int:sitzung_id>/szenen")
    def neue_szene(sitzung_id: int) -> Response:
        szene_id = notes.add_scene(runde, sitzung_id, title=request.form.get("title", ""))
        if szene_id is None:
            abort(404)
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

    @app.post("/szenen/<int:szene_id>/notizen")
    def neue_notiz(szene_id: int) -> Response:
        sitzung_id = notes.session_of_scene(runde, szene_id)
        if sitzung_id is None:
            abort(404)
        notes.add_note(runde, szene_id, request.form.get("text", ""))
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

    @app.post("/sitzungen/<int:sitzung_id>/diktat")
    def neues_diktat(sitzung_id: int) -> Response | tuple[str, int]:
        if notes.session(runde, sitzung_id) is None:
            abort(404)
        try:
            recordings.accept(basis, runde, sitzung_id, request.files.get("datei"))
        except recordings.Rejected as fehler:
            return sitzungsseite(sitzung_id, diktat_fehler=str(fehler)), 400
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor="diktat"))

    @app.post("/aufnahmen/<int:aufnahme_id>/notiz")
    def diktat_uebernehmen(aufnahme_id: int) -> Response:
        aufnahme = recordings.get(runde, aufnahme_id)
        if aufnahme is None or not aufnahme.text:
            abort(404)
        gewaehlt = request.form.get("scene_id", "")
        if not gewaehlt.isdigit():
            abort(404)
        szene_id = int(gewaehlt)
        if notes.session_of_scene(runde, szene_id) != aufnahme.session_id:
            abort(404)
        notes.add_note(runde, szene_id, aufnahme.text)
        return redirect(
            url_for("sitzung", sitzung_id=aufnahme.session_id, _anchor=f"szene-{szene_id}")
        )

    @app.post("/sitzungen/<int:sitzung_id>/transkript")
    def transkript_uebernehmen(sitzung_id: int) -> Response | tuple[str, int]:
        gewaehlt = request.form.get("scene_id", "")
        if not gewaehlt.isdigit():
            abort(404)
        szene_id = int(gewaehlt)
        if notes.session_of_scene(runde, szene_id) != sitzung_id:
            abort(404)
        try:
            von = merge.marke_ms(request.form.get("von", ""))
            bis = merge.marke_ms(request.form.get("bis", ""))
        except ValueError as fehler:
            return sitzungsseite(sitzung_id, transkript_fehler=str(fehler)), 400
        abschnitt = merge.span(merge.conversation(runde, sitzung_id), von=von, bis=bis)
        if not abschnitt:
            return sitzungsseite(sitzung_id, transkript_fehler=merge.LEERE_SPANNE), 400
        notes.add_note(runde, szene_id, merge.note_text(abschnitt))
        return redirect(url_for("sitzung", sitzung_id=sitzung_id, _anchor=f"szene-{szene_id}"))

    @app.errorhandler(413)
    def zu_gross(_fehler: object) -> tuple[str, int]:
        return render_template("zu_gross.html", grenze=recordings.MAX_BYTES // (1024 * 1024)), 413

    @app.get("/protokolle")
    def protokolle() -> str:
        return render_template("protokolle.html", eintraege=protocol.entries(runde))

    @app.get("/sitzungen/<int:sitzung_id>/protokoll")
    def protokoll(sitzung_id: int) -> str:
        daten = notes.session(runde, sitzung_id)
        if daten is None:
            abort(404)
        return render_template(
            "protokoll.html",
            sitzung=daten,
            protokoll=protocol.stored(runde, sitzung_id),
            rueckblick=protocol.stored(runde, sitzung_id, RUECKBLICK),
            **chronik_stand(sitzung_id),
        )

    @app.get("/suche")
    def suche() -> str:
        return render_template(
            "suche.html",
            ergebnis=search.find(runde, request.args.get("q", "")),
        )

    @app.get("/register")
    def register_seite() -> str:
        return render_template(
            "register.html",
            gruppen=register.overview(runde),
            offene=len(register.pending(runde)),
        )

    @app.get("/register/vorschlaege")
    def register_vorschlaege() -> str:
        return render_template(
            "register_vorschlaege.html",
            vorschlaege=register.pending(runde),
            feld=register.FELD,
            name_feld=register.NAME_FELD,
            satz_feld=register.SATZ_FELD,
        )

    @app.post("/register/vorschlaege")
    def register_entscheiden() -> Response:
        # Entschieden wird nur über Zeilen, die in der angezeigten Liste standen — ein
        # Ja auf einen Eintrag, den niemand gesehen hat, wäre keine Bestätigung.
        auswahl = {}
        for eintrag in register.pending(runde):
            wahl = request.form.get(register.FELD + str(eintrag.id), "").strip()
            if wahl not in (register.JA, register.NEIN):
                continue
            auswahl[eintrag.id] = register.Entscheidung(
                ja=wahl == register.JA,
                name=request.form.get(register.NAME_FELD + str(eintrag.id), ""),
                description=request.form.get(register.SATZ_FELD + str(eintrag.id), ""),
            )
        register.decide(runde, auswahl)
        return redirect(url_for("register_vorschlaege"))

    @app.get("/zuordnung")
    def zuordnung() -> str:
        return render_template(
            "zuordnung.html",
            uebersicht=people.overview(runde),
            feld=people.FELD,
        )

    @app.post("/zuordnung")
    def zuordnung_speichern() -> Response:
        uebersicht = people.overview(runde)
        bekannt = {spieler.id for spieler in uebersicht.spieler}
        # Gespeichert wird nur, was in der angezeigten Liste stand: diese Tabelle sagt
        # später, wessen Stimme wessen Absatz ist, und nimmt deshalb keinen Wert an, den
        # niemand gesehen hat.
        auswahl = {}
        for person in uebersicht.personen:
            gewaehlt = request.form.get(people.FELD + person.discord_user_id, "").strip()
            auswahl[person.discord_user_id] = gewaehlt if gewaehlt in bekannt else ""
        people.confirm(runde, auswahl)
        return redirect(url_for("zuordnung"))

    def felder(*, mit_modellen: bool = True) -> dict[str, object]:
        aktuell = settings.effective(basis, runde)
        adresse = str(aktuell.ollama_url)
        modelle, hinweis, erreichbar = _modelle(adresse) if mit_modellen else ((), "", True)
        return {
            "foundry_url": aktuell.foundry_url or "",
            "foundry_user": aktuell.foundry_user or "",
            "bot_token_gesetzt": bool(aktuell.discord_bot_token),
            "discord_recap_channel": aktuell.discord_recap_channel or "",
            "ollama_url": adresse,
            "ollama_eigen": adresse != settings.DEFAULT_OLLAMA_URL,
            "ollama_standard": settings.DEFAULT_OLLAMA_URL,
            "ollama_model": aktuell.ollama_model or "",
            "modelle": modelle,
            "modell_hinweis": hinweis,
            "modell_erreichbar": erreichbar,
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
        settings.save(runde, werte)

    def einstellungsseite(sperr_gruppe: str | None = None) -> str:
        return render_template(
            "einstellungen.html",
            quellen=settings.sources(basis, runde),
            config=settings.effective(basis, runde),
            schema_version=db.current_schema_version(basis.database_path),
            abgleich=foundry.current(basis, runde),
            remote_user=request.headers.get(REMOTE_USER_HEADER),
            nightly_time=settings.nightly_time(runde),
            nightly_zone=settings.nightly_zone(runde),
            zonen=settings.ZONEN,
            nachtlauf=nightly.letzter(runde),
            admin_group=instanz.admin_group(basis.database_path)
            if sperr_gruppe is None
            else sperr_gruppe,
            sperr_gruppe=sperr_gruppe,
            **felder(),
        )

    @app.get("/einstellungen")
    def einstellungen() -> str:
        return einstellungsseite()

    @app.post("/einstellungen")
    def einstellungen_speichern() -> Response | str:
        uebernehmen(settings.KEYS)
        settings.save_nightly_time(runde, request.form.get(settings.NIGHTLY_KEY, ""))
        settings.save_nightly_zone(runde, request.form.get(settings.NIGHTLY_ZONE_KEY, ""))
        # Ein gar nicht abgesendetes Feld heißt »unverändert«: ein Formular ohne dieses
        # Feld darf die Verwaltungsrolle nicht still zurücknehmen.
        gruppe = request.form.get(instanz.ADMIN_GROUP_KEY)
        if gruppe is None:
            return redirect(url_for("einstellungen"))
        if request.form.get(BESTAETIGT) != "ja" and not roles.traegt(
            request.headers.get(roles.GRUPPEN_HEADER), gruppe
        ):
            return einstellungsseite(sperr_gruppe=gruppe.strip())
        instanz.save_admin_group(basis.database_path, gruppe)
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
        tat = request.form.get("tat")
        if tat == SPAETER:
            instanz.finish_onboarding(basis.database_path)
            return redirect(url_for("sitzungen"))
        if tat != UEBERSPRINGEN:
            uebernehmen(SCHRITT_FELDER[schritt])
        namen = [name for name, _ in SCHRITTE]
        naechster = namen.index(schritt) + 1
        if naechster == len(namen):
            instanz.finish_onboarding(basis.database_path)
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


def dienst() -> Flask:
    """Der Einstieg des Servers — dieselbe App, dazu der nächtliche Zeitplan.

    Der Faden hängt hier und nicht an ``create_app``, damit eine App, die nur befragt
    wird, nicht anfängt zu arbeiten.
    """
    return create_app(zeitplan=True)


def _modelle(adresse: str) -> tuple[tuple[str, ...], str, bool]:
    try:
        namen = sprachmodell.installed_models(adresse)
    except ModelError:
        return (), "Die Auswahl lädt gerade nicht — Modellnamen von Hand eintragen.", False
    if not namen:
        return (), "Dort liegt noch kein Textmodell — Modellnamen von Hand eintragen.", True
    return namen, f"{len(namen)} Modelle stehen zur Auswahl.", True
