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

`DATA_DIR` und `PUBLIC_DOMAIN` sind globale ServiceBay-Variablen und werden hier nicht
noch einmal deklariert.

## Eingerichtet wird nach dem ersten Start, nicht im Assistenten

**Foundry-Adresse, -Benutzer und -Passwort, der Discord-Bot-Token sowie Ollama-Adresse
und -Modell sind keine Template-Variablen.** Sie werden unter `/einstellungen` gepflegt
und liegen in der SQLite; der Dienst liest sie von dort, nicht aus der Umgebung. Beim
ersten Aufruf führt er in Schritten hindurch; bis sie gesetzt sind, startet er trotzdem
und erklärt im Abschnitt *Zustand* der Einstellungen, was fehlt.

Das ist kein Weglassen aus Bequemlichkeit: Der Assistent erzeugt für eine Variable vom
Typ `secret` einen **Zufallswert**. Für ein internes Geheimnis ist das richtig, für
Zugangsdaten einer fremden Gegenstelle falsch — der ausgewürfelte Bot-Token meldete sich
bei Discord an und scheiterte mit 401 in einer Neustart-Schleife.

## Container

Der Pod hat zwei Container aus demselben Image:

| Container | Befehl | Wofür |
|---|---|---|
| `chronik` | `waitress-serve` (Vorgabe des Images) | die Oberfläche hinter Authelia **und der nächtliche Lauf** |
| `bot` | `python -m chronicle.bot` | der Aufnahme-Bot am Discord-Gateway |

Der Bot ist ein eigener, dauerhafter Prozess, weil Sprache nur mitgeschnitten werden kann,
während sie gesprochen wird. Den Token liest er aus derselben SQLite wie die Oberfläche —
deshalb teilen beide Container `/data`. **Ohne Token beendet er sich mit einem Satz** und
wird von der Neustart-Regel des Pods wieder gestartet — das ist erwartet und kein Fehler;
nach dem Eintragen unter `/einstellungen` findet ihn der nächste Start.

**Einen dritten Container für den nächtlichen Lauf gibt es bewusst nicht.** Der Zeitplan
hängt in `chronik`, und zwar aus zwei Gründen: der Bot existiert ohne Token gar nicht, eine
Präsenzgruppe hat aber trotzdem Aufnahmen zu verschriften; und ein Lauf ist eine Zeile in
der `job`-Tabelle, deren Absturzerkennung nur trägt, solange **ein** Prozess solche Zeilen
anlegt. Wer hier einen Container ergänzt, macht aus jedem laufenden Lauf einen
»unterbrochenen« im Auge des anderen Prozesses. Die Uhrzeit steht unter `/einstellungen`,
Vorgabe 04:00 nach der Uhr der Box; ein verpasstes Fenster wird nicht nachgeholt.

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
ausschließen.**

Der Bot sagt im Sprachkanal eine **Aufbewahrungsfrist von 7 Tagen** zu und hält sie selbst
ein: er räumt einmal beim Start und danach täglich ab, der nächtliche Stapel zusätzlich am
Ende jedes Laufs. `python -m chronicle.transcribe --loeschen` entfernt eine Spur schon
vorher. Gelöscht wird dabei nur die Audiodatei — die Zeile in der SQLite bleibt stehen.

## Rollout

Das Image wird von der CI des Repos nach GHCR veröffentlicht, und zwar erst, wenn die
Tests grün sind. Für einen Rollout `CHRONICLE_IMAGE_TAG` auf einen festen Tag setzen —
`sha-<kurz>` oder die Release-Version —, dann pullen, dann neu starten und danach den
laufenden Digest gegen den erwarteten prüfen.

Nach dem Deploy gehört zur Abnahme: `/healthz` liefert 200, die Subdomain antwortet
unauthentifiziert mit 302 auf `auth.<domain>`, und ein Request ohne `Remote-User` wird
abgelehnt.
