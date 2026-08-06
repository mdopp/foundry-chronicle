# Foundry Chronicle

Sitzungsprotokolle für Tisch-Rollenspiel. Aus den Notizen, die während des Spiels
entstehen, und dem Chat-Log aus Foundry VTT wird eine lesbare Chronik. Bei
Online-Sitzungen kommt eine Transkription der Discord-Sprachspuren dazu.

**Eine Instanz pro Gruppe.** Konfiguration ist zweierlei: Foundry-URL + Zugangsdaten und
Discord-Bot-Token — alles andere kommt aus Foundry.

- Architektur: [`docs/architektur.md`](docs/architektur.md)
- Foundry-Zugriff: [`docs/foundry-zugriff.md`](docs/foundry-zugriff.md)
- Aufbau und Reihenfolge: Epic [#1](../../issues/1)
- Hausregeln: [`CLAUDE.md`](CLAUDE.md)

> Status: Aufbau. Issues #2–#12 sind die erste Ausbaustufe; die Präsenz-Variante
> (#2–#7) läuft ohne Audio, ohne Bot und ohne Grafikkarte.

## Entwickeln

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
python -m chronicle          # http://127.0.0.1:8000
```

Unter `/` wird mitgeschrieben — Sitzung, Szenen, Notizen; `/status` sagt, was
konfiguriert ist und wann zuletzt mit Foundry abgeglichen wurde.

Foundry-Adresse, -Benutzer und -Passwort, der Discord-Bot-Token sowie Ollama-Adresse und
-Modell werden unter `/einstellungen` gepflegt und liegen in der SQLite. Die Umgebung —
`FOUNDRY_URL`, `FOUNDRY_USER`, `FOUNDRY_PASSWORD`, `DISCORD_BOT_TOKEN`, `OLLAMA_URL`,
`OLLAMA_MODEL` — bleibt die Vorgabe beim ersten Start und der Deploy-Weg; **ein in der
Oberfläche gesetzter Wert gewinnt**, und `/status` zeigt je Wert, woher er kommt. Die
beiden Geheimnisse werden nie angezeigt, nur *ob* sie gesetzt sind; ein leer
abgesendetes Feld heißt unverändert. Rein aus der Umgebung kommen weiterhin
`CHRONICLE_DATA_DIR` (Vorgabe `./data`), `CHRONICLE_RECORDINGS_DIR` (Vorgabe
`./recordings`) und `CHRONICLE_WHISPER_MODEL` (Vorgabe `small`). Fehlt die
Foundry-Konfiguration, startet der Dienst trotzdem und erklärt auf `/status`, was fehlt.

Ein eigenes Login gibt es nicht: angemeldet wird am Proxy (ServiceBay-ADR 0001), der
`Remote-User` setzt. Auf der Box gehört deshalb `CHRONICLE_REQUIRE_REMOTE_USER=1` in die
Umgebung — dann wird jeder Request ohne diesen Header abgewiesen. Lokal bleibt die
Variable aus, sonst kommt man ohne Proxy nicht hinein.

Prüfen wie die CI: `ruff check . && ruff format --check . && pytest -q`.

## Transkription

Aus einer Audiospur wird Text mit Zeitstempeln — im Stapel nach der Sitzung, auf CPU:

```bash
pip install -e ".[dev,transcribe]"          # faster-whisper ist ein eigenes Extra
python -m chronicle.transcribe              # die Warteschlange — der nächtliche Aufruf
python -m chronicle.transcribe 1 mira.ogg   # Sitzung 1, Spur aus ./recordings
python -m chronicle.transcribe 1 mira.ogg --loeschen   # Aufnahme danach entfernen
```

Ohne Argumente wird abgearbeitet, was über die Oberfläche hochgeladen wurde und noch
wartet: auf der Sitzungsseite lädt ein **Diktat** — eine Sprachnotiz aus der
Sprachmemo-App des Telefons — eine Spur hoch und reiht sie in dieselbe Warteschlange ein.
Die Seite zeigt den Stand dieses Jobs, nie einen geratenen Fortschritt; ist er fertig,
steht das Transkript dort und lässt sich einer Szene als Notiz übernehmen. Aufgenommen
wird **nicht** im Browser: Mikrofonzugriff braucht HTTPS und wäre im Heimnetz über HTTP
tot, und die Sprachmemo-App übersteht Bildschirmsperre und Anruf.

Der Dateiname wird die Quellenkennung der Spur; ein zweiter Lauf ersetzt sie, statt zu
verdoppeln. Erkannt wird `small` auf CPU mit int8 — grob das Zwei- bis Fünffache der
Echtzeit, also **keine Grafikkarte**; `CHRONICLE_WHISPER_MODEL` setzt eine andere Größe,
ein Feld in der Oberfläche gibt es dafür nicht. Als Vokabular werden die Eigennamen
dieser Sitzung vorgespannt — erst, wer im Chat-Log gesprochen hat, dann der übrige
Foundry-Zwischenspeicher, hart auf rund 224 Token gekappt.

`faster-whisper` steckt im Extra `transcribe`: das Image bringt es mit, eine
Dev-Installation muss es nicht laden. Die Tests setzen ein erfundenes Modell ein und
laden nie ein echtes herunter.

**Die Aufnahmen liegen neben dem Datenverzeichnis, nicht darin** (`recordings/` gegen
`data/`, im Image `/aufnahmen` gegen `/data`). Gesichert wird die SQLite; Audiospuren
gehören nie ins Backup und werden nach einem erfolgreichen Lauf entbehrlich — gelöscht
werden sie aber nur auf ausdrückliches Verlangen.

## Diktat per Discord

Die Oberfläche ist nur im Heimnetz erreichbar, der Diktat-Moment aber auf dem Heimweg.
Discord ist von überall erreichbar und von Natur aus ein Briefkasten: einwerfen, wann es
einem einfällt — geholt wird, wenn der Dienst das nächste Mal läuft.

```bash
python -m chronicle.discord     # den Kanal #diktat leeren — vor der Transkription
```

Der Bot liest **genau einen** Kanal, nach Namenskonvention `#diktat`. Eine Sprachnachricht
dort reiht sich in dieselbe Warteschlange ein wie ein Upload; eine Textnachricht wird zur
Notiz der zuletzt angelegten Sitzung. Beides quittiert der Bot mit ✅ und **einer** Antwort;
was weder Audio noch Text ist, bekommt ein ⚠ und bleibt liegen. Gibt es noch keine Sitzung,
wartet der Einwurf sichtbar, statt sich eine zu erfinden — angelegt wird sie von Hand, dann
holt der nächste Lauf ihn nach.

Autorisierung ist Discords eigenes Rechtemodell: **wer im Kanal schreiben darf, darf
diktieren.** Deshalb ein eigener Kanal und nie der Gruppenkanal — das Rohdiktat ist der
ungefilterte Gedankenstrom des Erzählenden, Spoiler und Spielleitungssicht inklusive.

Geholt wird **per REST im Stapel, nicht über eine dauerhafte Gateway-Verbindung**: der Lauf
fragt, was seit dem letzten Zeiger dazugekommen ist. Ein zweiter Lauf verdoppelt nichts —
neben dem Zeiger steht die Kennung jeder erledigten Nachricht in der Datenbank. Das Diktat
läuft durch Discords Cloud; für Online-Gruppen ändert das nichts, für reine Präsenzgruppen
ist es eine bewusste Entscheidung — der Discord-Teil darf leer bleiben, dann bleibt das
Web-Formular der Weg.

## Betrieb auf ServiceBay

Dieses Repo ist zugleich eine **ServiceBay-Registry**: unter [`templates/`](templates/)
liegt das Pod-Template `daggerheart-chronik`. Auf der Box wird das Repo einmal in
`config.registries[]` eingetragen (Git-URL dieses Repos), danach steht das Template im
Installations-Assistenten neben den mitgelieferten.

Das Image baut [`.github/workflows/build-images.yml`](.github/workflows/build-images.yml)
und veröffentlicht es nach GHCR — der Publish-Job hängt an `needs: test`, es wird also
nichts veröffentlicht, was nicht grün war. Für den Rollout wird ein fester Tag gepinnt
(`sha-<kurz>` oder die Release-Version), nicht `:latest`.

Im Container läuft `waitress`, nicht der Flask-Entwicklungsserver:

```bash
podman build -t foundry-chronicle .
podman run --rm -p 8000:8000 foundry-chronicle    # /healthz antwortet 200
```
