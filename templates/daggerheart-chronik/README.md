# Daggerheart-Chronik

Sitzungsprotokolle für Tisch-Rollenspielrunden: aus den Notizen, die während des
Spiels entstehen, und dem Chat-Log aus Foundry VTT wird eine lesbare Chronik.

**Bedient wird in Discord** (#62). Was die spielende Gruppe betrifft — Sitzung, Szene,
Notiz, Diktat, Chronik, Suche, Register, Zuordnung, Einrichtung — ist seit #157 ein
Befehl im eigenen Server. Über HTTP liefert dieser Dienst nur noch **eine Seite**: die
Betreiber-Seite unter `/einstellungen`, dazu `/status` (301 dorthin) und `/`
(Weiterleitung dorthin). Dort steht, was *keiner Gilde gehört* und deshalb in Discord
keinen Ort hat: der Discord-Bot-Token, Ollama-Adresse und -Modell, und wer diese Seite
verwalten darf.

`/healthz` steht daneben und nicht darin: das Install-Gate der Box kommt seit #228 aus
dem **Bot**-Prozess, auf einem eigenen Port und nur auf `127.0.0.1`. Der Bot ist der
Prozess, der bleibt, wenn die Seite mit #227 fällt — und ein Gate, das im LAN hörte,
wäre wieder der offene Port aus #190.

Die Seite ist serverseitig gerendertes HTML **ohne eigenes Login** — angemeldet wird an
Authelia, der Proxy setzt `Remote-User`, und ein Request ohne diesen Header wird
abgewiesen. **Die Kopfzeile allein ist dabei kein Beleg** (#190): der Port liegt im
Host-Netz offen, und wer ihn direkt erreicht, schreibt sie sich selbst hin. Geglaubt
wird sie — und `Remote-Groups` mit ihr — nur einem Absender, der eine Adresse *dieser
Maschine* trägt; das ist genau der Proxy, der auf derselben Box läuft. Aus dem übrigen
LAN kommt niemand mehr an den Bot-Token, auch nicht mit erfundenen Kopfzeilen. Das ist
kein Erbstück aus der Zeit der großen Oberfläche: auf dieser Seite liegt der Bot-Token,
ServiceBay-ADR 0001 gilt für sie also unverändert. Subdomain, Proxy-Route und die
`auth`-Abhängigkeit bleiben deshalb.

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

### Wenn die Seite 403 sagt, obwohl die Anmeldung geklappt hat

Dann kommt der Proxy nicht mehr von dieser Box — er ist umgezogen, oder er wählt über
einen Zwischenweg an, der eine fremde Absenderadresse zeigt. Welche Adresse abgewiesen
wurde, steht im Log des Containers (`Anmeldung von … verworfen`). Diese Adresse in die
Umgebung des Containers `chronik` eintragen und den Dienst neu starten:

```yaml
      - name: CHRONICLE_TRUSTED_PROXIES
        value: "192.0.2.10"
```

Kommagetrennt sind mehrere Einträge erlaubt, Adressen wie Netze (`10.0.0.0/8`, `::1`).
**Gesetzt ersetzt der Wert die errechnete Antwort** — dann zählt nur noch, was dort
steht, Loopback eingeschlossen; wer beides braucht, schreibt beides hin. Leer bleibt es
bei der Maschine selbst, und genau so ist es hier gemeint: die Box hängt an DHCP, eine
abgeschriebene Adresse wäre nach der nächsten Lease dieselbe Aussperrung. Ein neues
Abbild braucht es dafür nicht, nur diese Zeile und einen Neustart.

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

## Der Pod hängt im Host-Netz — erklärte Abweichung von ADR 0007

`spec.hostNetwork: true` steht bewusst da, obwohl ServiceBays **ADR 0007** App-Container
in einen eigenen Netz-Namensraum stellen will (#165). Der Grund sind die Nachbarn auf
derselben Box, die dieser Dienst über die Schleife anspricht: **Ollama** auf
`127.0.0.1:11434` schreibt die Chronik, **`solaris-tts`** auf `127.0.0.1:8881` spricht die
Ansage im Sprachkanal. Beide binden nur an Loopback — aus einem eigenen Namensraum wären
sie nicht erreichbar. Dazu kommt, dass der Proxy den Dienst so ohne veröffentlichten
`hostPort` findet.

ADR 0007 sieht benannte Ausnahmen vor; ob dieser Dienst eine ist, wurde in
`mdopp/servicebay#2518` gefragt und **verneint** — die Liste bleibt geschlossen. Die
Abweichung bleibt deshalb, aber nicht gratis: sie ist der Grund, warum der Port auf
`0.0.0.0` im ganzen LAN stand und ein erfundener `Remote-User` bis an den Bot-Token
führte (#190). Die Rechnung ist bezahlt — die Anmelde-Kopfzeilen gelten seither nur von
einer Adresse dieser Maschine, siehe oben und `CHRONICLE_TRUSTED_PROXIES`.

**Sie fällt, sobald Ollama und `solaris-tts` auch aus einem eigenen Netz-Namensraum
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
danebengreift: `build-images.yml` ist pfadgefiltert, ein Push ohne Bau-Pfade
veröffentlicht kein Image. Das Skript rät den Tag deshalb nicht aus der Historie, sondern
**fragt GHCR**, welche es wirklich gibt: ab `--ref` rückwärts der erste Commit, dessen
Manifest sich abrufen lässt. Getaggt wird nämlich der **Push**, nicht der einzelne Commit
— ein Rebase-Merge landet einen ganzen Batch auf einmal, und dessen letzter Commit trägt
den Tag, auch wenn er nur die README anfasst. Hat `main` selbst kein Image, sagt das
Skript das: entweder als Hinweis, dass der ältere Tag inhaltlich derselbe Stand ist, oder
— wenn seither Bau-Pfade geändert wurden — mit einer Verweigerung, denn dann fehlt ein
Image, das es geben müsste.

**`latest` ist keine Einstellung, sondern eine fehlende.** Damit gibt es keinen Tag, der
den Stand von vorgestern benennt — ein misslungenes Rollout wäre nur über einen Revert
auf `main` und einen neuen Bau zu heilen. Mit einem festen Tag ist der Weg zurück eine
Zeile im Spec: `--zurueck 1` fragen, eintragen, neu starten. Dass `AutoUpdate=registry`
bei diesem Dienst ohnehin nicht greift, kommt dazu — mit `latest` liest niemand am Spec
ab, welcher Stand läuft.

Nach dem Deploy gehört zur Abnahme:

- `/healthz` liefert 200 — auf `CHRONICLE_HEALTH_PORT`, aus dem **Bot**-Prozess, am Proxy
  vorbei: es ist das Install-Gate der Box (#228). Auf der Box gemessen, denn es hört nur
  auf `127.0.0.1`; von einem anderen Rechner im LAN muss derselbe Port **abgewiesen**
  werden, und der Bot muss dabei am Gateway hängen bleiben.
- Die Subdomain antwortet unauthentifiziert mit 302 auf `auth.<domain>`, und ein Request
  ohne `Remote-User` wird mit 403 abgelehnt.
- **Und mit erfundenem `Remote-User` auch:** von einem anderen Rechner im LAN direkt auf
  `http://<box>:<port>/` mit gesetzter Kopfzeile — die Antwort muss 403 sein, nicht 302.
  Das ist der Fall, den #190 geschlossen hat; von der Box selbst aus ist er nicht
  prüfbar, weil dort jeder Aufruf zu Recht als eigener zählt.
- Angemeldet führt die Wurzel `/` auf `/einstellungen` — der Proxy zeigt auf die Wurzel,
  und dort lag bis #157 die Sitzungsliste; ein 404 an der Haustür wäre eine schlechte
  Auskunft. `/status` führt mit 301 an dieselbe Stelle.
- Eine Adresse für Spielinhalte gibt es nicht mehr. Angemeldet liefern `/sitzungen`,
  `/protokolle`, `/suche` und `/register` zu Recht 404 — das ist der Umzug nach Discord,
  kein kaputtes Deployment. Unangemeldet kommt auch dort erst der Türsteher: 403.
