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
| `DISCORD_BOT_TOKEN` | Optional, erst ab dem Audio-Zweig | — |
| `OLLAMA_URL` / `OLLAMA_MODEL` | Optional, Sprachmodell auf der Box | — |

`DATA_DIR` und `PUBLIC_DOMAIN` sind globale ServiceBay-Variablen und werden hier nicht
noch einmal deklariert.

## Daten

```
{{DATA_DIR}}/daggerheart/
  chronicle.sqlite3        ← Sitzungen, Szenen, Notizen, Protokolle (WAL-Modus)
```

Die SQLite-Datei ist klein und enthält alles Unersetzliche — sie gehört ins Backup.
Audiospuren sind nach erfolgreichem Lauf löschbar und gehören nicht hinein.

## Rollout

Das Image wird von der CI des Repos nach GHCR veröffentlicht, und zwar erst, wenn die
Tests grün sind. Für einen Rollout `CHRONICLE_IMAGE_TAG` auf einen festen Tag setzen —
`sha-<kurz>` oder die Release-Version —, dann pullen, dann neu starten und danach den
laufenden Digest gegen den erwarteten prüfen.

Nach dem Deploy gehört zur Abnahme: `/healthz` liefert 200, die Subdomain antwortet
unauthentifiziert mit 302 auf `auth.<domain>`, und ein Request ohne `Remote-User` wird
abgelehnt.
