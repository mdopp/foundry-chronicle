# Daggerheart-Chronik

Sitzungsprotokolle für Tisch-Rollenspielrunden: aus den Notizen, die während des
Spiels entstehen, und dem Chat-Log aus Foundry VTT wird eine lesbare Chronik.

**Bedient wird in Discord** (#62). Was die spielende Gruppe betrifft — Sitzung, Szene,
Notiz, Diktat, Chronik, Suche, Register, Zuordnung, Einrichtung — ist seit #157 ein
Befehl im eigenen Server. **Seit #231 gibt es daneben gar nichts mehr:** die
Betreiber-Seite ist gefallen, mit ihr Subdomain, Proxy-Route und der zweite Container.
Der Dienst ist ein Prozess, und dieser Prozess ist der Bot.

Der letzte Grund für die Seite fiel mit #230: Bot-Token, Ollama-Adresse und -Modell
gehören keiner Gilde und wurden deshalb dort gepflegt. Sie kommen jetzt aus den
Template-Variablen weiter unten. Was danach noch dastand — die Verwaltungsgruppe — war
nur die Antwort auf die Frage, wer an diese Seite darf; ohne Seite stellt sie niemand
mehr, und `admin_group` wird beim nächsten Start aus der Datenbank geräumt.

**Über HTTP antwortet nur noch `/healthz`**, das Install-Gate der Box, seit #228 aus dem
Bot-Prozess und **ausschließlich auf `127.0.0.1`**. Der Poller der Box läuft daneben und
braucht nichts weiter; ein Gate, das im LAN hörte, wäre wieder der offene Port aus #190.

**ServiceBay-ADR 0001 (Authelia-SSO) bindet diesen Dienst nicht mehr.** Er ist nicht
user-facing: es gibt keine Seite, keine Subdomain und keinen veröffentlichten Port. Wer
was darf, entscheidet Discord über seine eigenen Kanal- und Rollenrechte — und wer an
den Bot-Token darf, entscheidet, wer die Template-Variablen dieses Dienstes bearbeiten
darf, also ServiceBay selbst.

**Eine Instanz trägt mehrere Runden** (#62). Eine Runde ist eine Discord-Gilde mit eigenem
Foundry-Zugang; für eine zweite Gruppe wird der Bot in ihren Server eingeladen, nicht der
Dienst ein zweites Mal installiert.

## Variablen

| Variable | Bedeutung | Vorgabe |
|---|---|---|
| `CHRONICLE_HEALTH_PORT` | Port des Install-Gates, nur auf `127.0.0.1` | `8701` |
| `CHRONICLE_IMAGE_TAG` | Image-Tag für den Rollout | `latest` |
| `CHRONICLE_GPU_LEASE` | Sitzungsfenster bei `solaris` an-/abmelden (#299); `aus` verlässt den Vertrag | `an` |
| `DISCORD_BOT_TOKEN` | Token des Bots — ohne ihn bleibt der Bot aus | *(leer)* |
| `OLLAMA_URL` | Modelldienst, der die Chronik formuliert; leer = `127.0.0.1:11435` | *(leer)* |
| `OLLAMA_MODEL` | Textmodell dort; auf dem `/v1`-Weg eine Bitte, kein Beleg (#320); leer = geordnet statt formuliert | *(leer)* |

`DATA_DIR` ist eine globale ServiceBay-Variable und wird hier nicht noch einmal
deklariert. `PUBLIC_DOMAIN` kommt hier **nicht** mehr vor — es gibt keinen Proxy-Host,
für den der Assembler sie injizieren müsste.

## Was der Instanz gehört, steht im Assistenten — was der Runde gehört, in Discord

**Discord-Bot-Token, Ollama-Adresse und -Modell sind seit #230 Template-Variablen**
(`DISCORD_BOT_TOKEN`, `OLLAMA_URL`, `OLLAMA_MODEL`). Sie gehören der Instanz, werden beim
Start gelesen und **nirgends gespeichert**; damit liegt kein Geheimnis dieses Dienstes in
`chronicle.sqlite3` und keines im Backup. Eine Wanderung räumt einen Bestand aus der Zeit
davor fort — aber erst, wenn die jeweilige Variable gesetzt ist: gelöscht ohne Ersatz
wäre der Bot-Token unwiederbringlich, und der Dienst sagte es erst, wenn der Bot schweigt.
Bis dahin bleibt der Wert liegen und das Log nennt bei jedem Start die fehlende Variable —
nie den Wert.

**Foundry-Adresse und -Benutzer sind keine Variablen.** Sie gehören der Runde und werden
in Discord unter `/chronicle setup` eingetragen; das Foundry-Passwort wird gar nicht vorgehalten —
es wird beim Abgleich gefragt und danach vergessen. Bis alles gesetzt ist, startet der
Dienst trotzdem, und der Bot sagt in Discord, was noch fehlt.

Die drei Variablen sind bewusst vom Typ `text` und **nicht** `secret`: der Assistent
erzeugt für ein `secret` einen **Zufallswert**. Für ein internes Geheimnis ist das
richtig, für Zugangsdaten einer fremden Gegenstelle falsch — der ausgewürfelte Bot-Token
meldete sich bei Discord an und scheiterte mit 401 in einer Neustart-Schleife (#33). Leer
heißt hier ehrlich »nicht gesetzt«. Nachtragen oder wechseln geht jederzeit mit
`install_template(names=["daggerheart-chronik"], variables={"DISCORD_BOT_TOKEN": "…"})` —
der übergebene Wert gewinnt über den gespeicherten (Assist
*recipe-rotate-a-service-secret*).

## Container

Der Pod hat **einen** Container: `chronik`, Befehl `python -m chronicle.bot` (die Vorgabe
des Abbilds, hier nicht überschrieben). Er hält die Gateway-Verbindung, weil Sprache nur
mitgeschnitten werden kann, während sie gesprochen wird; er trägt seit #229 den
nächtlichen Lauf und seit #228 das Install-Gate.

Der zweite Container daneben trug bis #231 die Betreiber-Seite. Sie ist fort, und mit ihr
`waitress`, Flask und Jinja2 aus dem Abbild.

**Ohne Token bleibt der Bot liegen und sagt einen Satz** — er beendet sich dabei
ausdrücklich *nicht*, denn am Prozess hängt das Install-Gate, und ein fehlender Token ist
bei der Erstinstallation der Normalfall. Nach dem Hinterlegen der Variablen findet ihn
der nächste Start. Was ein Prozess nicht bekommt, kann er auch nicht in eine Logzeile
schreiben; deshalb steht der Token in der Umgebung dieses einen Containers und nirgends
sonst.

**Einen zweiten Container für den nächtlichen Lauf gibt es bewusst nicht.** Der Zeitplan
hängt seit #229 hier: ohne Bot gibt es keinen Eingang mehr — weder Notiz noch Aufnahme
noch Runde —, also gibt es auch nichts zu verschriften, wo der Bot nicht läuft. Er sieht in
einem Faden **neben** der Gateway-Verbindung auf die Uhr, und der Lauf selbst bekommt noch
einen daneben; auf der Ereignisschleife bliebe während einer Verschriftung der Herzschlag
zu Discord aus. Die Uhrzeit gehört der Runde und steht in Discord unter `/chronicle setup`, Vorgabe
04:00 nach der Zone der Runde; ein verpasstes Fenster wird nicht nachgeholt.

## Der Pod hängt im Host-Netz — erklärte Abweichung von ADR 0007

`spec.hostNetwork: true` steht bewusst da, obwohl ServiceBays **ADR 0007** App-Container
in einen eigenen Netz-Namensraum stellen will (#165). Der Grund sind die Nachbarn auf
derselben Box, die dieser Dienst über die Schleife anspricht: **Ollama** auf
`127.0.0.1:11434` schreibt die Chronik, **`solaris-tts`** auf `127.0.0.1:8881` spricht die
Ansage im Sprachkanal, **`solaris-whisper-batch`** auf `127.0.0.1:10301` verschriftet die
Spuren, und **`solaris`** auf `127.0.0.1:8787` nimmt das Sitzungsfenster entgegen (#299:
`POST`/`DELETE /api/model-lease`, damit der Nachbar das große Modell während eines
Spielabends nicht wegzieht — abschaltbar mit `CHRONICLE_GPU_LEASE`). Alle vier binden nur
an Loopback — aus einem eigenen Namensraum wären sie nicht
erreichbar, auch nicht über `host.containers.internal`: das führt an das Gateway der Box
und nicht an ihre Schleife.

ADR 0007 sieht benannte Ausnahmen vor; ob dieser Dienst eine ist, wurde in
`mdopp/servicebay#2518` gefragt und **verneint** — die Liste bleibt geschlossen.

**Was die Abweichung kostete, ist mit #231 nicht mehr abgesichert, sondern weg.** Sie war
der Grund, warum der Port der Betreiber-Seite auf `0.0.0.0` im ganzen LAN stand und ein
erfundener `Remote-User` bis an den Bot-Token führte (#190). Gedeckt wurde das seither von
`chronicle.herkunft`. Jetzt gibt es die Seite nicht mehr und damit keinen Port im LAN: der
einzige Horcher dieses Pods ist `/healthz` auf `127.0.0.1`. Es bleibt nichts, was das
Host-Netz freilegen könnte — deshalb ist `chronicle.herkunft` mit derselben Änderung
gefallen, als Prüfung ohne Prüfling.

**Sie fällt, sobald diese Nachbarn auch aus einem eigenen Netz-Namensraum
erreichbar sind** — das liegt in deren Vorlagen, nicht in dieser. Bis dahin gilt: an der
Netzkonfiguration dieses Dienstes wird nichts ohne Verify auf der Box geändert. Falsch
gemacht legt sie eine laufende Discord-Gilde still.

## Die Grafikkarte — dieser Pod hat keine

**Der Pod reicht bewusst keine NVIDIA-Karte durch.** Der Versuch stand hier schon einmal
als `resources.limits.nvidia.com/gpu` samt SELinux-Freigabe; der Lauf auf der Box am
2026-08-12 hat gezeigt, dass davon nichts ankommt: `podman kube play` (5.8.2) **verwirft
`resources.limits` für eine Pod-Spezifikation still** — kein Fehler, kein Hinweis, der Pod
startet ohne Karte (`cuda_verfuegbar() -> False`, keine `nvidia-*`-Knoten unter `/dev`).
Das ist ServiceBays Lücke #1026/#2174, unser Fall als `mdopp/servicebay#2517`.

Der bekannte Fix wäre ein einzelnes `.container`-Quadlet mit `AddDevice=` und
`SecurityLabelDisable=true` — so hängen `ollama` und `solaris-whisper` auf derselben Box
an der Karte.

**Seit #216 braucht dieser Pod auch keine.** Er hält kein Whisper-Modell mehr; die
Verschriftung ist ein HTTP-Aufruf gegen `solaris-whisper-batch`, der auf der Karte der
Box rechnet (`mdopp/solarisbay#1161`). Erreicht wird er über die Schleife wie Ollama und
`solaris-tts` auch — Vorgabe `http://127.0.0.1:10301`, überschreibbar mit
`CHRONICLE_WHISPER_URL`; das Template setzt nichts.

**Einen Rückfall gibt es nicht mehr.** Der CPU-Weg aus #84 ist ersatzlos entfallen.
Ist `solaris-whisper-batch` aus, bleibt die Tonspur wartend liegen und der nächtliche
Lauf sagt es auf seiner Karte — er schreibt für diese Sitzung **keine** Chronik, denn
eine ohne das gesprochene Wort sähe fertig aus. Die nächste Nacht holt sie nach.

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
danebengreift: `build-images.yml` ist pfadgefiltert, ein Push ohne Bau-Pfade
veröffentlicht kein Image. Das Skript rät den Tag deshalb nicht aus der Historie, sondern
**fragt GHCR**, welche es wirklich gibt: ab `--ref` rückwärts der erste Commit, dessen
Manifest sich abrufen lässt. Getaggt wird nämlich der **Push**, nicht der einzelne Commit
— ein Rebase-Merge landet einen ganzen Batch auf einmal, und dessen letzter Commit trägt
den Tag, auch wenn er nur die README anfasst. Hat `main` selbst kein Image, sagt das
Skript das: entweder als Hinweis, dass der ältere Tag inhaltlich derselbe Stand ist, oder
— wenn seither Bau-Pfade geändert wurden — mit einer Verweigerung, denn dann fehlt ein
Image, das es geben müsste.

**Ein Release ist der andere feste Tag — und heißt nicht wie der Git-Tag.** Aus
`v0.3.1` entsteht das Image `0.3.1`, ohne führendes `v`, daneben `0.3`. Wer `v0.3.1`
einträgt, greift ins Leere. Gebaut wird der Release-Stand im selben Lauf, der den Tag
schneidet — `release-please.yml` ruft `build-images.yml` auf, sobald ein Release steht
(#309); ein Tag-Push für sich löst nichts aus.

**`latest` ist keine Einstellung, sondern eine fehlende.** Damit gibt es keinen Tag, der
den Stand von vorgestern benennt — ein misslungenes Rollout wäre nur über einen Revert
auf `main` und einen neuen Bau zu heilen. Mit einem festen Tag ist der Weg zurück eine
Zeile im Spec: `--zurueck 1` fragen, eintragen, neu starten. Dass `AutoUpdate=registry`
bei diesem Dienst ohnehin nicht greift, kommt dazu — mit `latest` liest niemand am Spec
ab, welcher Stand läuft.

Nach dem Deploy gehört zur Abnahme:

- Der Pod läuft mit **genau einem** Container, und `podman ps` zeigt ihn als
  `daggerheart-chronik-chronik`.
- `/healthz` liefert 200 — auf `CHRONICLE_HEALTH_PORT`, aus dem **Bot**-Prozess: es ist
  das Install-Gate der Box (#228). Auf der Box gemessen, denn es hört nur auf
  `127.0.0.1`; von einem anderen Rechner im LAN muss derselbe Port **abgewiesen** werden.
- Der Bot hängt am Discord-Gateway und antwortet auf `/session help`.
- **Auf `CHRONICLE_PORT` hört nichts mehr** — die Variable gibt es nicht, und auf dem
  alten Port (`8700`) darf nichts antworten. Die frühere Subdomain zeigt ins Leere; die
  Proxy-Route gehört nach dem Deploy entfernt, sonst steht ein `auth`-geschützter Host
  vor einem Dienst, den es nicht gibt.
- Kein `flask`, kein `jinja2`, kein `waitress` im Abbild:
  `podman exec … pip list | grep -Ei "flask|jinja|waitress"` bleibt leer.
