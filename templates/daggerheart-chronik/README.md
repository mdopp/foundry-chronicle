# Daggerheart-Chronik

Sitzungsprotokolle für die Tisch-Rollenspielgruppe: aus den Notizen, die während des
Spiels entstehen, und dem Chat-Log aus Foundry VTT wird eine lesbare Chronik. Der Dienst
ist serverseitig gerendertes HTML ohne eigenes Login — angemeldet wird an Authelia, der
Proxy setzt `Remote-User`, und ein Request ohne diesen Header wird abgewiesen.

**Eine Instanz pro Gruppe.** Die Subdomain trägt deshalb den Gruppennamen; Vorgabe ist
`daggerheart`, die Domain kommt zur Installationszeit aus `PUBLIC_DOMAIN`.

## Variablen

| Variable | Bedeutung | Vorgabe |
|---|---|---|
| `CHRONICLE_SUBDOMAIN` | Subdomain hinter Authelia-Forward-Auth | `daggerheart` |
| `CHRONICLE_PORT` | HTTP-Port auf der Box | `8700` |
| `CHRONICLE_IMAGE_TAG` | Image-Tag für den Rollout | `latest` |
| `FOUNDRY_URL` | Adresse der Foundry-Instanz | — |
| `FOUNDRY_USER` | Benutzername eines Foundry-Kontos | — |
| `FOUNDRY_PASSWORD` | Passwort dazu (`secret`, beim Installieren einsetzen) | — |
| `DISCORD_BOT_TOKEN` | Optional, für Diktat-Kanal und Aufnahme-Bot | — |
| `OLLAMA_URL` / `OLLAMA_MODEL` | Optional, Sprachmodell auf der Box | — |

`DATA_DIR` und `PUBLIC_DOMAIN` sind globale ServiceBay-Variablen und werden hier nicht
noch einmal deklariert.

## Container

Der Pod hat zwei Container aus demselben Image:

| Container | Befehl | Wofür |
|---|---|---|
| `chronik` | `waitress-serve` (Vorgabe des Images) | die Oberfläche hinter Authelia |
| `bot` | `python -m chronicle.bot` | der Aufnahme-Bot am Discord-Gateway |

Der Bot ist ein eigener, dauerhafter Prozess, weil Sprache nur mitgeschnitten werden kann,
während sie gesprochen wird. **Ohne `DISCORD_BOT_TOKEN` beendet er sich mit einem Satz**
und wird von der Neustart-Regel des Pods wieder gestartet — das ist erwartet und kein
Fehler; der Token lässt sich jederzeit unter `/einstellungen` nachtragen.

## Daten

```
{{DATA_DIR}}/daggerheart/
  chronicle.sqlite3        ← Sitzungen, Szenen, Notizen, Protokolle, Einwilligungen
{{DATA_DIR}}/daggerheart-aufnahmen/
  sitzung1-…-Mira.wav      ← eine Spur je Sprecher, aus dem Backup heraushalten
```

Die SQLite-Datei ist klein und enthält alles Unersetzliche — sie gehört ins Backup, samt
dem Einwilligungsprotokoll des Aufnahme-Bots. Die Audiospuren liegen bewusst in einem
**zweiten** Verzeichnis daneben: sie werden groß, sind nach der Transkription entbehrlich
und gehören nicht ins Backup. **Beim Einrichten der Sicherung `daggerheart-aufnahmen`
ausschließen.** Gelöscht werden die Spuren nur auf ausdrückliches Verlangen
(`python -m chronicle.transcribe --loeschen`), nie still.

## Rollout

Das Image wird von der CI des Repos nach GHCR veröffentlicht, und zwar erst, wenn die
Tests grün sind. Für einen Rollout `CHRONICLE_IMAGE_TAG` auf einen festen Tag setzen —
`sha-<kurz>` oder die Release-Version —, dann pullen, dann neu starten und danach den
laufenden Digest gegen den erwarteten prüfen.

Nach dem Deploy gehört zur Abnahme: `/healthz` liefert 200, die Subdomain antwortet
unauthentifiziert mit 302 auf `auth.<domain>`, und ein Request ohne `Remote-User` wird
abgelehnt.
