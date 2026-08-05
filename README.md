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

Die Konfiguration kommt ausschließlich aus der Umgebung: `FOUNDRY_URL`, `FOUNDRY_USER`,
`FOUNDRY_PASSWORD`, dazu optional `DISCORD_BOT_TOKEN` und `CHRONICLE_DATA_DIR` (Vorgabe
`./data`). Fehlt die Foundry-Konfiguration, startet der Dienst trotzdem und erklärt auf
`/`, was fehlt.

Prüfen wie die CI: `ruff check . && ruff format --check . && pytest -q`.
