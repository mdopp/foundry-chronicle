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
`OLLAMA_MODEL` — bleibt als Vorgabe beim ersten Start lesbar und ist beim Entwickeln der
bequeme Weg; **ein in der Oberfläche gesetzter Wert gewinnt**, und `/status` zeigt je
Wert, woher er kommt. Das Box-Template setzt keine davon (siehe unten). Die
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
gehören nie ins Backup. `--loeschen` entfernt eine Spur sofort nach einem erfolgreichen
Lauf; spätestens nach der zugesagten **Aufbewahrungsfrist** (`recordings.RETENTION_TAGE`,
derzeit 7 Tage) räumt der Stapel sie ohnehin ab. Gelöscht wird dabei nur die Audiodatei —
die Zeile bleibt mit `deleted_at` stehen, damit man sieht, dass die Spur nach Frist
entfernt wurde und nicht verlorenging.

## Diktat per Discord

### Den Bot einmalig anlegen

Der Bot-Account entsteht im [Discord Developer Portal](https://discord.com/developers/applications):

1. **New Application** anlegen (Name z. B. „Chronik"), links **Bot** öffnen, den
   **Token** erzeugen. Der Token wird in der Oberfläche unter *Einstellungen*
   eingetragen — nie ins Repo, nie in eine Nachricht.
2. Unter **Bot** die **Message Content Intent** einschalten — ohne sie liefert die
   API keine Nachrichtentexte.
3. **OAuth2 → URL Generator**: Scopes `bot` **und `applications.commands`** — ohne den
   zweiten gibt es keinen Slash-Befehl. Rechte *View Channels, Read Message History,
   Send Messages, Add Reactions* (Diktat-Kanal) plus *Connect*, *Speak* und *Use Voice
   Activity* (Aufnahme). **Sprechen** ist Pflicht: ohne das Recht bleibt die
   Einwilligungs-Ansage stumm, und dann wird auch nicht aufgenommen. Die erzeugte URL
   öffnen und den Bot auf den Server einladen.
4. Einen Kanal **`#diktat`** anlegen. Für das Abholen per Stapel-Lauf muss der Bot
   nur eingeladen sein — als „online" erscheint er erst, wenn der Aufnahme-Bot
   eine Gateway-Verbindung hält.

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

## Aufnahme per Discord

```bash
pip install -e ".[dev,discord]"      # py-cord ist ein eigenes Extra
python -m chronicle.bot              # ein eigener, dauerhafter Prozess
```

Der Aufnahme-Bot ist **kein Stapellauf**: er hält eine Gateway-Verbindung, weil Sprache
nur mitgeschnitten werden kann, während sie gesprochen wird. Auf der Box läuft er deshalb
als zweiter Container im selben Pod, mit demselben Image und `python -m chronicle.bot`.
Ohne Bot-Token startet er nicht und sagt das in einem Satz.

Im Sprachkanal: **`/aufnahme start`** holt den Bot in den Kanal des Aufrufers — eine
Kanal-Konfiguration braucht es deshalb nicht —, **`/aufnahme stop`** beendet die Aufnahme
und reiht die Spuren in dieselbe Warteschlange ein wie ein Diktat. Die Befehle registriert
der Bot beim Start selbst.

### Die Ansage ist der Kern, nicht die Verpackung

Das Aufzeichnen des nichtöffentlich gesprochenen Wortes ohne Einwilligung ist strafbar
(**§201 StGB**). Der Bot spielt deshalb zuerst eine hörbare deutsche Ansage — wer
aufnimmt, wofür, und dass Verlassen des Kanals heißt: keine Aufnahme. **Der Mitschnitt
beginnt erst, wenn die Ansage zu Ende gespielt ist**; wer davor zu schreiben versucht,
bekommt einen Fehler und keine Datei. Wer *nach* dem Start dazukommt, hört dieselbe Ansage
noch einmal und wird eigens protokolliert — bloß zu vermerken, dass jemand sie verpasst
hat, hielte fest, dass er nicht eingewilligt hat, statt ihn zu fragen.

Protokolliert wird jede Ansage in der SQLite: Zeitpunkt, Server und Kanal, die Anwesenden
mit Id und Anzeigename — und der **Wortlaut**. Nicht ein Verweis auf den Text im Code:
ändert jemand die Ansage, darf sich das Protokoll vergangener Sitzungen nicht mitändern.
Der Eintrag überlebt auch das Löschen seiner Sitzung.

Gesprochen wird die Ansage von **espeak-ng**, erzeugt beim ersten Bedarf aus dem Text in
`chronicle/bot/ansage.py` und unter dessen Fingerabdruck im Aufnahmeverzeichnis abgelegt.
Damit können Ansage und Protokoll nicht auseinanderlaufen. Fehlt espeak-ng, wird **nicht**
aufgenommen.

### Die zugesagte Frist wird auch eingehalten

Die Ansage nennt eine Aufbewahrungsfrist — und **dieselbe Zahl setzt sie durch**: der Satz
wird aus `recordings.RETENTION_TAGE` (7) formatiert, und `recordings.sweep` räumt danach
auf. Ein Versprechen, das nur im Ansagetext steht, wäre keins; so können Satz und Verhalten
nicht auseinanderlaufen. Durchgesetzt wird an zwei Stellen, damit die Zusage auch gilt,
wenn eine davon eine Weile steht: **im laufenden Bot** einmal beim Start und danach täglich,
und **am Ende jedes `python -m chronicle.transcribe`-Laufs**, auch wenn nichts zu tun war.

Gelöscht wird nur die Audiodatei. Die Zeile in der Datenbank bleibt mit `deleted_at` stehen
— dass es die Spur gab, wann sie kam und was aus ihr wurde, ist die ehrliche Hälfte der
Geschichte; das Transkript bleibt ohnehin. Die Sitzungsseite sagt es in einem Satz.

### Je Sprecher eine Spur

Discord trennt die Audiodaten ohnehin pro Client. Damit entfällt die Sprechertrennung
nicht bloß billiger, sondern exakt — jede Diarisierung rät bei Überlappungen, und in einer
Rollenspielrunde reden fünf Leute durcheinander. Geschrieben wird **eine Datei je Sprecher
für die ganze Sitzung**, im Strom auf die Platte und nie in einen Puffer im Speicher.

Empfangenes Audio ist von Discord nicht offiziell unterstützt: `discord.py` kann es nicht.
Wir nehmen **py-cord**, weil es die Senken-API mitbringt, regelmäßig veröffentlicht wird
und die Sprechpausen beim Empfang anhand der RTP-Zeitstempel mit Stille auffüllt — das
hält alle Spuren auf einer Zeitachse. Das ist die eine bekannte Bruchstelle des Systems
und steckt deshalb in genau einer Datei (`chronicle/bot/gateway.py`). py-cord belegt das
Paket `discord`; ein daneben installiertes `discord.py` schlägt sich mit ihm.

**Die Sprach-Abhängigkeiten kommen aus `py-cord[voice]`, nicht aus einer eigenen Liste.**
Dahinter stecken PyNaCl und `davey`, Discords DAVE-Ende-zu-Ende-Verschlüsselung für
Sprache. Fehlt eines davon, verbindet sich py-cord anstandslos, schreibt eine einzige
Warnzeile — `davey is not installed, voice will NOT be supported` — und der Bot hört
nichts; scheitern würde erst `/aufnahme start`, mitten im Befehl. Genau so ist es einmal
passiert. Deshalb wird die Liste nicht mehr abgeschrieben, und deshalb **prüft der Bot
beim Start** (`discord.utils.get_missing_voice_dependencies()`) und beendet sich mit einem
verständlichen Satz, statt sich taub anzumelden. Beide Pakete bringen fertige
manylinux-Räder mit; im Image wird nichts übersetzt.

## Betrieb auf ServiceBay

Dieses Repo ist zugleich eine **ServiceBay-Registry**: unter [`templates/`](templates/)
liegt das Pod-Template `daggerheart-chronik`. Auf der Box wird das Repo einmal in
`config.registries[]` eingetragen (Git-URL dieses Repos), danach steht das Template im
Installations-Assistenten neben den mitgelieferten.

Der Assistent fragt nur nach Subdomain, Port und Image-Tag. **Foundry, Discord und Ollama
werden nach dem ersten Start unter `/einstellungen` eingerichtet**, nicht beim
Installieren: Der Assistent würfelt für eine Variable vom Typ `secret` einen Zufallswert
aus — richtig für ein internes Geheimnis, falsch für Zugangsdaten, die nur die
Gegenstelle kennt. Ein solcher Wert meldete sich einmal als Bot-Token bei Discord an und
scheiterte mit 401 in einer Neustart-Schleife. Deshalb deklariert das Template diese
Werte gar nicht mehr, und ein frisch installierter Pod zeigt sie auf `/status` als
*nicht gesetzt*.

Das Image baut [`.github/workflows/build-images.yml`](.github/workflows/build-images.yml)
und veröffentlicht es nach GHCR — der Publish-Job hängt an `needs: test`, es wird also
nichts veröffentlicht, was nicht grün war. Für den Rollout wird ein fester Tag gepinnt
(`sha-<kurz>` oder die Release-Version), nicht `:latest`.

Im Container läuft `waitress`, nicht der Flask-Entwicklungsserver:

```bash
podman build -t foundry-chronicle .
podman run --rm -p 8000:8000 foundry-chronicle    # /healthz antwortet 200
```
