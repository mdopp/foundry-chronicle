"""Die Betreiber-Seite: serverseitig gerendertes HTML, sonst nichts.

Die Haustür steht am Proxy (ServiceBay-ADR 0001): Authelia setzt ``Remote-User``, die
App baut kein eigenes Login. Erzwungen wird der Header nur, wenn die Umgebung es sagt —
sonst wäre ``python -m chronicle`` ohne Proxy nicht startbar. Die Tür bleibt, obwohl das
Spiel nach Discord gezogen ist: auf dieser Seite liegt der Bot-Token.

Der Header allein reicht dafür nicht: er ist eine gewöhnliche Kopfzeile, und der Port
liegt im Host-Netz offen. Geglaubt wird er deshalb nur, wenn der Aufruf **von dieser
Maschine** kommt — der Proxy läuft auf derselben Box (#190, ``chronicle.herkunft``).
Sperrt das einmal aus, weil der Proxy woanders steht: ``CHRONICLE_TRUSTED_PROXIES`` auf
seine Adresse setzen und den Dienst neu starten; das Abbild bleibt, wie es ist.

**Spielinhalte gibt es hier keine mehr** (#157). Was die spielende Gruppe betrifft —
Sitzung, Szene, Notiz, Diktat, Chronik, Suche, Register, Zuordnung, Einrichtung —, steht
in Discord. Übrig bleibt **ein** Feld: wer verwalten darf. Dazu ``/status`` als Altweg
und ``/healthz`` als Install-Gate der Box.

**Bot-Token, Ollama-Adresse und -Modell werden hier seit #230 nicht mehr gepflegt.** Sie
kommen aus der Umgebung, gesetzt von den Template-Variablen der Box; diese Seite zeigt nur
noch, *ob* sie stehen. Damit fällt auch die Auswahlliste der installierten Modelle weg —
sie war das Einzige, was die Seite konnte und eine Variable nicht kann, und der Verlust
ist bewusst in Kauf genommen.

``basis`` ist die Umgebung beim Start und für diese drei Werte das letzte Wort.

Der nächtliche Lauf hängt seit #229 **nicht** mehr hier, sondern am Bot-Prozess
(``chronicle.bot.gateway``). Diese Seite antwortet nur noch, sie arbeitet nicht.
"""

from __future__ import annotations

import logging

from flask import Flask, Response, redirect, render_template, request, url_for

from chronicle import db, herkunft, instanz, roles
from chronicle.config import DEFAULT_OLLAMA_URL, Config

logger = logging.getLogger(__name__)

REMOTE_USER_HEADER = "Remote-User"

# Was die Instanz einrichtet. Alles Übrige ist keine Seite mehr, sondern ein Befehl in
# Discord — und wer den geben darf, entscheidet die Gilde über ihre eigenen Rechte.
VERWALTUNG = frozenset({"einstellungen", "einstellungen_speichern"})

BESTAETIGT = "gruppe_bestaetigt"


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    basis = config if config is not None else Config.from_env()
    app.config["CHRONICLE"] = basis
    db.init(basis.database_path)
    vertrauen = herkunft.netze(basis.trusted_proxies)
    # Ohne gepflegte Liste hängt der Türsteher allein an der Frage an den Kern — und die
    # kann ein Kernschalter still entwerten. Einmal nachsehen; hochkommen darf der Dienst
    # trotzdem.
    if basis.require_remote_user and not vertrauen:
        herkunft.selbstprobe()

    @app.before_request
    def tuersteher() -> tuple[str, int] | None:
        # /healthz ist das Install-Gate der Box und wird am Proxy vorbei abgefragt.
        if not basis.require_remote_user or request.endpoint == "healthz":
            return None
        # Erst wer, dann was: eine Kopfzeile, die jeder selbst hinschreiben kann, sagt
        # nichts (#190). Die Antwort gilt für **beide** Kopfzeilen — wer hier scheitert,
        # erreicht ``verwaltungsrolle`` nicht, und Remote-Groups wird nie gelesen.
        if not herkunft.vertraut(request.remote_addr, vertrauen):
            logger.warning(
                "Anmeldung von %s verworfen: nicht der Proxy dieser Box. "
                "Ist sie es doch, gehört ihre Adresse in %s.",
                request.remote_addr,
                herkunft.TRUSTED_VARIABLE,
            )
            return render_template("abgewiesen.html"), 403
        if not request.headers.get(REMOTE_USER_HEADER):
            return render_template("abgewiesen.html"), 403
        return None

    @app.before_request
    def verwaltungsrolle() -> tuple[str, int] | None:
        # Ohne gesetzte Gruppe ist jeder Verwalter — eine frische Instanz kann sich also
        # nicht selbst aussperren, denn dort ist noch keine gesetzt.
        if request.endpoint not in VERWALTUNG:
            return None
        if roles.ist_verwalter(request, basis.database_path):
            return None
        return render_template("keine_verwaltung.html"), 403

    def einstellungsseite(sperr_gruppe: str | None = None) -> str:
        return render_template(
            "einstellungen.html",
            config=basis,
            ollama_url=basis.ollama_url or DEFAULT_OLLAMA_URL,
            ollama_standard=DEFAULT_OLLAMA_URL,
            schema_version=db.current_schema_version(basis.database_path),
            remote_user=request.headers.get(REMOTE_USER_HEADER),
            admin_group=instanz.admin_group(basis.database_path)
            if sperr_gruppe is None
            else sperr_gruppe,
            sperr_gruppe=sperr_gruppe,
        )

    @app.get("/einstellungen")
    def einstellungen() -> str:
        return einstellungsseite()

    @app.post("/einstellungen")
    def einstellungen_speichern() -> Response | str:
        # Seit #230 wird hier genau ein Wert gepflegt. Bot-Token und Ollama kommen aus der
        # Umgebung; ein POST, der sie mitschickt, schreibt nichts — es gibt keinen Weg mehr
        # von hier in die Datei.
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

    # Die Statusseite ist in den Einstellungen aufgegangen; die Adresse steht in
    # Lesezeichen und alten Bändern, deshalb 301 statt 404.
    @app.get("/status")
    def status() -> Response:
        return redirect(url_for("einstellungen"), 301)

    # Die Wurzel trug die Sitzungsliste; die ist mit #157 fort. Sie ist aber die Adresse,
    # auf die der Proxy zeigt — 404 an der Haustür wäre eine schlechte Auskunft.
    @app.get("/")
    def start() -> Response:
        return redirect(url_for("einstellungen"))

    # Test-Seam und Install-Gate der ServiceBay-Box (servicebay.healthcheck).
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
