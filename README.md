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

Die Konfiguration kommt ausschließlich aus der Umgebung: `FOUNDRY_URL`, `FOUNDRY_USER`,
`FOUNDRY_PASSWORD`, dazu optional `DISCORD_BOT_TOKEN` und `CHRONICLE_DATA_DIR` (Vorgabe
`./data`). Fehlt die Foundry-Konfiguration, startet der Dienst trotzdem und erklärt auf
`/status`, was fehlt.

Ein eigenes Login gibt es nicht: angemeldet wird am Proxy (ServiceBay-ADR 0001), der
`Remote-User` setzt. Auf der Box gehört deshalb `CHRONICLE_REQUIRE_REMOTE_USER=1` in die
Umgebung — dann wird jeder Request ohne diesen Header abgewiesen. Lokal bleibt die
Variable aus, sonst kommt man ohne Proxy nicht hinein.

Prüfen wie die CI: `ruff check . && ruff format --check . && pytest -q`.

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
