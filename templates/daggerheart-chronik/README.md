# Daggerheart-Chronik

Sitzungsprotokolle für Tisch-Rollenspielrunden: aus den Notizen, die während des
Spiels entstehen, und dem Chat-Log aus Foundry VTT wird eine lesbare Chronik.

**Bedient wird in Discord** (#62). Was die spielende Gruppe betrifft — Sitzung, Szene,
Notiz, Diktat, Chronik, Suche, Register, Zuordnung, Einrichtung — ist seit #157 ein
Befehl im eigenen Server. Über HTTP liefert dieser Dienst nur noch **eine Seite**: die
Betreiber-Seite unter `/einstellungen`, dazu `/status` (301 dorthin), `/` (Weiterleitung
dorthin) und `/healthz`. Dort steht, was *keiner Gilde gehört* und deshalb in Discord
keinen Ort hat: der Discord-Bot-Token, Ollama-Adresse und -Modell, und wer diese Seite
verwalten darf.

Die Seite ist serverseitig gerendertes HTML **ohne eigenes Login** — angemeldet wird an
Authelia, der Proxy setzt `Remote-User`, und ein Request ohne diesen Header wird
abgewiesen. Das ist kein Erbstück aus der Zeit der großen Oberfläche: auf dieser Seite
liegt der Bot-Token, ServiceBay-ADR 0001 gilt für sie also unverändert. Subdomain,
Proxy-Route und die `auth`-Abhängigkeit bleiben deshalb.

**Eine Instanz trägt mehrere Runden** (#62). Eine Runde ist eine Discord-Gilde mit eigenem
Foundry-Zugang; für eine zweite Gruppe wird der Bot in ihren Server eingeladen, nicht der
Dienst ein zweites Mal installiert. Die Subdomain benennt deshalb die Instanz und nicht
eine Gruppe; Vorgabe ist `daggerheart`, die Domain kommt zur Installationszeit aus
`PUBLIC_DOMAIN`.

## Variablen

| Variable | Bedeutung | Vorgabe |
|---|---|---|
| `CHRONICLE_SUBDOMAIN` | Subdomain der Betreiber-Seite, hinter Authelia-Forward-Auth | `daggerheart` |
| `CHRONICLE_PORT` | HTTP-Port auf der Box | `8700` |
| `CHRONICLE_IMAGE_TAG` | Image-Tag für den Rollout | `latest` |

`DATA_DIR` und `PUBLIC_DOMAIN` sind globale ServiceBay-Variablen und werden hier nicht
noch einmal deklariert.

## Eingerichtet wird nach dem ersten Start, nicht im Assistenten

**Foundry-Adresse und -Benutzer, der Discord-Bot-Token sowie Ollama-Adresse und -Modell
sind keine Template-Variablen.** Gepflegt werden sie an zwei Orten: Bot-Token,
Ollama-Adresse und -Modell gehören der Instanz und stehen unter `/einstellungen`;
Foundry-Adresse und -Benutzer gehören der Runde und werden in Discord unter `/setup`
eingetragen. Beides liegt in der SQLite; der Dienst liest es von dort, nicht aus der
Umgebung. Das Foundry-Passwort liegt auch dort nicht — es wird beim Abgleich gefragt und
danach vergessen. Bis alles gesetzt ist, startet der Dienst trotzdem, und der Bot sagt in
Discord, was noch fehlt.

Das ist kein Weglassen aus Bequemlichkeit: Der Assistent erzeugt für eine Variable vom
Typ `secret` einen **Zufallswert**. Für ein internes Geheimnis ist das richtig, für
Zugangsdaten einer fremden Gegenstelle falsch — der ausgewürfelte Bot-Token meldete sich
bei Discord an und scheiterte mit 401 in einer Neustart-Schleife.

## Container

Der Pod hat zwei Container aus demselben Image:

| Container | Befehl | Wofür |
|---|---|---|
| `chronik` | `waitress-serve` (Vorgabe des Images) | die Betreiber-Seite hinter Authelia **und der nächtliche Lauf** |
| `bot` | `python -m chronicle.bot` | der Aufnahme-Bot am Discord-Gateway |

Der Bot ist ein eigener, dauerhafter Prozess, weil Sprache nur mitgeschnitten werden kann,
während sie gesprochen wird. Den Token liest er aus derselben SQLite wie die Betreiber-Seite —
deshalb teilen beide Container `/data`. **Ohne Token beendet er sich mit einem Satz** und
wird von der Neustart-Regel des Pods wieder gestartet — das ist erwartet und kein Fehler;
nach dem Eintragen unter `/einstellungen` findet ihn der nächste Start.

**Einen dritten Container für den nächtlichen Lauf gibt es bewusst nicht.** Der Zeitplan
hängt in `chronik`, und zwar aus zwei Gründen: der Bot existiert ohne Token gar nicht, eine
Präsenzgruppe hat aber trotzdem Aufnahmen zu verschriften; und ein Lauf ist eine Zeile in
der `job`-Tabelle, deren Absturzerkennung nur trägt, solange **ein** Prozess solche Zeilen
anlegt. Wer hier einen Container ergänzt, macht aus jedem laufenden Lauf einen
»unterbrochenen« im Auge des anderen Prozesses. Die Uhrzeit gehört der Runde und steht in
Discord unter `/setup`, Vorgabe 04:00 nach der Zone der Runde; ein verpasstes Fenster wird
nicht nachgeholt.

## Die Grafikkarte — dieser Pod hat keine

**Der Pod reicht bewusst keine NVIDIA-Karte durch.** Der Versuch stand hier schon einmal
als `resources.limits.nvidia.com/gpu` samt SELinux-Freigabe; der Lauf auf der Box am
2026-08-12 hat gezeigt, dass davon nichts ankommt: `podman kube play` (5.8.2) **verwirft
`resources.limits` für eine Pod-Spezifikation still** — kein Fehler, kein Hinweis, der Pod
startet ohne Karte (`cuda_verfuegbar() -> False`, keine `nvidia-*`-Knoten unter `/dev`).
Das ist ServiceBays Lücke #1026/#2174, unser Fall als `mdopp/servicebay#2517`.

Der bekannte Fix wäre ein einzelnes `.container`-Quadlet mit `AddDevice=` und
`SecurityLabelDisable=true` — so hängen `ollama` und `solaris-whisper` auf derselben Box
an der Karte. Für einen Pod aus zwei Containern, die sich `/data` teilen, gibt es diese
Form nicht.

**Der beschlossene Weg führt deshalb nicht über eine eigene Karte**, sondern über den
vorhandenen `solaris-whisper` der Box (#141), sobald der Wortvorgaben annimmt
(`mdopp/solarisbay#1142`). Bis dahin verschriftet der nächtliche Lauf auf der CPU — das
ist der Rückfall aus #84, absichtlich und nicht aus Versehen. `CHRONICLE_WHISPER_DEVICE`
bleibt ungesetzt; das Template erzwingt kein Gerät.

Wer die drei Zeilen wieder einbaut, gewinnt nichts außer einem Versprechen, das die Datei
nicht hält.

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

### Warum daneben und nicht darin

ServiceBay sichert pro Dienst keine ganzen Bäume, sondern eine **Auswahlliste**, und die
ist am Verzeichnis `{{DATA_DIR}}/<dienst>` verankert (ADR 0002 trennt kleinen,
unersetzlichen Zustand von großen Massendaten). Daraus folgt die Wahl des Host-Pfads:

- `{{DATA_DIR}}/daggerheart/aufnahmen` — ein Unterverzeichnis läge im Wurzelverzeichnis
  einer künftigen Auswahlliste und bliebe nur draußen, solange jemand daran denkt, es
  auszunehmen. Für eine Zusage an Menschen, deren Stimme aufgenommen wurde, ist das zu
  wenig.
- Ein absoluter Pfad neben `{{DATA_DIR}}` — fällt aus der Abdeckungsprüfung der Plattform
  heraus, die absolute Host-Pfade nicht als Volume zählt. Das Verzeichnis wäre dann nicht
  ausgenommen, sondern unsichtbar, und `DATA_DIR` gibt es gerade, damit kein Pfad der Box
  im Repo steht.
- Eine eigene Template-Variable — ein Knopf, den niemand verstellt, und der Assistent
  fragte ihn bei jeder Installation ab.

Bleibt `{{DATA_DIR}}/daggerheart-aufnahmen`: neben dem Datenverzeichnis, also außer
Reichweite jeder dienstbezogenen Auswahlliste, und trotzdem unter `DATA_DIR`, also
sichtbar für Abdeckungsprüfung und Massendaten-Sicherung. Der gemeinsame Namensanfang
hält beide Verzeichnisse im Blick — auch beim Deinstallieren, wo `daggerheart-aufnahmen`
stehen bleibt und von Hand zu löschen ist.

Der Bot sagt im Sprachkanal eine **Aufbewahrungsfrist von 7 Tagen** zu und hält sie selbst
ein: er räumt einmal beim Start und danach täglich ab, der nächtliche Stapel zusätzlich am
Ende jedes Laufs. `python -m chronicle.transcribe --loeschen` entfernt eine Spur schon
vorher. Gelöscht wird dabei nur die Audiodatei — die Zeile in der SQLite bleibt stehen.

## Rollout

Das Image wird von der CI des Repos nach GHCR veröffentlicht, und zwar erst, wenn die
Tests grün sind. Für einen Rollout `CHRONICLE_IMAGE_TAG` auf einen festen Tag setzen,
dann pullen, dann neu starten und danach den laufenden Digest gegen den erwarteten
prüfen. Welcher Tag das ist, sagt das Repo:

```
python scripts/bestimme_image_tag.py              # sha-1234567 — der aktuelle Stand
python scripts/bestimme_image_tag.py --zurueck 1  # der Stand davor — der Weg zurück
```

**Abgeleitet und nicht abgetippt**, weil `sha-` plus die Kurzform von `HEAD` regelmäßig
danebengreift: `build-images.yml` ist pfadgefiltert, ein reiner Doku- oder
Template-Commit veröffentlicht kein Image. Das Skript geht deshalb bis zum jüngsten
Commit zurück, der wirklich einen Bau ausgelöst hat.

**`latest` ist keine Einstellung, sondern eine fehlende.** Damit gibt es keinen Tag, der
den Stand von vorgestern benennt — ein misslungenes Rollout wäre nur über einen Revert
auf `main` und einen neuen Bau zu heilen. Mit einem festen Tag ist der Weg zurück eine
Zeile im Spec: `--zurueck 1` fragen, eintragen, neu starten. Dass `AutoUpdate=registry`
bei diesem Dienst ohnehin nicht greift, kommt dazu — mit `latest` liest niemand am Spec
ab, welcher Stand läuft.

Nach dem Deploy gehört zur Abnahme:

- `/healthz` liefert 200 — am Proxy vorbei, es ist das Install-Gate der Box.
- Die Subdomain antwortet unauthentifiziert mit 302 auf `auth.<domain>`, und ein Request
  ohne `Remote-User` wird mit 403 abgelehnt.
- Angemeldet führt die Wurzel `/` auf `/einstellungen` — der Proxy zeigt auf die Wurzel,
  und dort lag bis #157 die Sitzungsliste; ein 404 an der Haustür wäre eine schlechte
  Auskunft. `/status` führt mit 301 an dieselbe Stelle.
- Eine Adresse für Spielinhalte gibt es nicht mehr. Angemeldet liefern `/sitzungen`,
  `/protokolle`, `/suche` und `/register` zu Recht 404 — das ist der Umzug nach Discord,
  kein kaputtes Deployment. Unangemeldet kommt auch dort erst der Türsteher: 403.
