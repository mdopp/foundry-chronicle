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

Beim ersten Mal führt `/` durch die Einrichtung — Foundry, dann Discord, dann Ollama,
jeder Schritt überspringbar; danach wird unter `/` mitgeschrieben — Sitzung, Szenen,
Notizen. Der Abschnitt *Zustand* unter `/einstellungen#zustand` sagt, was konfiguriert
ist und wann zuletzt mit Foundry abgeglichen wurde; `/status` leitet mit 301 dorthin. Am Notizfeld sitzt ein
**Diktier-Knopf** für kurze Notizen: er nutzt die Spracherkennung des Browsers, die über
die **Cloud des Browser-Herstellers** läuft und nicht auf dieser Box — Browser ohne
`SpeechRecognition` zeigen ihn gar nicht erst.

**Anstoßen kann der Nutzer selbst.** *Chronik erstellen* (Sitzungs- und Chronikseite) und
*Jetzt abgleichen* (Band und *Zustand*) starten **server-eigene Läufe** nach dem
ServiceBay-Standard für lange Prozesse: der Zustand steht in der Tabelle `job`, überlebt
Neuladen und geschlossenen Reiter, und ein Neustart mitten im Lauf wird beim nächsten
Blick ehrlich als unterbrochen vermerkt statt für immer zu laufen. Je Art läuft höchstens
einer. Der Chronik-Lauf verschriftet erst die wartenden Aufnahmen und ruft dann dieselben
Funktionen wie `python -m chronicle.compose`: ein Knopf ist der zweite Auslöser, nicht der
zweite Weg. Die Stapel-Einstiege unten bleiben — sie sind der Weg für Cron und Betrieb.

Foundry-Adresse und -Benutzer, der Discord-Bot-Token sowie Ollama-Adresse und -Modell
werden unter `/einstellungen` gepflegt und liegen in der SQLite. Die Umgebung —
`FOUNDRY_URL`, `FOUNDRY_USER`, `DISCORD_BOT_TOKEN`, `OLLAMA_URL`, `OLLAMA_MODEL` — bleibt
als Vorgabe beim ersten Start lesbar und ist beim Entwickeln der bequeme Weg; **ein in der
Oberfläche gesetzter Wert gewinnt**, und der Abschnitt *Zustand* zeigt je Wert, woher er
kommt. Das Box-Template setzt keine davon (siehe unten).

**Das Foundry-Passwort wird nirgends gespeichert** — es gibt kein Feld dafür, keine
Variable und keine Zeile in der SQLite. Der Abgleich fragt danach, verbraucht es und
vergisst es; ein Rest im Arbeitsspeicher verfällt spätestens nach zwölf Stunden. Hashen
ginge nicht: Foundry will es vorgezeigt, nicht geprüft. Der Bot-Token bleibt gespeichert,
wird aber nie angezeigt, nur *ob* er gesetzt ist; ein leer abgesendetes Feld heißt
unverändert. Die Ollama-Adresse hat eine dritte Stufe: ist weder
etwas gespeichert noch etwas in der Umgebung gesetzt, gilt `http://127.0.0.1:11434` — das
Ollama der Box. Offen bleibt dann allein die Modellwahl. Rein aus der Umgebung kommen weiterhin
`CHRONICLE_DATA_DIR` (Vorgabe `./data`), `CHRONICLE_RECORDINGS_DIR` (Vorgabe
`./recordings`) und `CHRONICLE_WHISPER_MODEL` (Vorgabe `small`). Fehlt die
Foundry-Konfiguration, startet der Dienst trotzdem und erklärt im *Zustand*, was fehlt.

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
steht das Transkript dort und lässt sich einer Szene als Notiz übernehmen. Lange Diktate
werden **nicht** im Browser aufgenommen: eine Sprachmemo-App übersteht Bildschirmsperre
und Anruf, ein Browser-Tab nicht — und die Quelle bleibt erhalten, bis das Transkript
taugt.

Der Dateiname wird die Quellenkennung der Spur; ein zweiter Lauf ersetzt sie, statt zu
verdoppeln. Erkannt wird `small` auf CPU mit int8 — grob das Zwei- bis Fünffache der
Echtzeit, also **keine Grafikkarte**; `CHRONICLE_WHISPER_MODEL` setzt eine andere Größe,
ein Feld in der Oberfläche gibt es dafür nicht. Als Vokabular werden die Eigennamen
dieser Sitzung vorgespannt — erst, wer im Chat-Log gesprochen hat, dann der übrige
Foundry-Zwischenspeicher, hart auf rund 224 Token gekappt.

`faster-whisper` steckt im Extra `transcribe`: das Image bringt es mit, eine
Dev-Installation muss es nicht laden. Die Tests setzen ein erfundenes Modell ein und
laden nie ein echtes herunter.

**Mehrere Spuren werden zu einer Unterhaltung.** Schneidet der Aufnahme-Bot mit, liegt je
Sprecher eine Spur; nacheinander gelesen wären das Monologe. Die Sitzungsseite zeigt sie
deshalb nach Zeit verschränkt — die Marke zählt ab dem Aufnahmebeginn, dem gemeinsamen
Nullpunkt aller Spuren, und der Name kommt aus der bestätigten Zuordnung. Ohne Zuordnung
steht der Discord-Name da; geraten wird keiner. Ein Abschnitt zwischen zwei Zeitmarken
lässt sich einer Szene **als Notiz** übernehmen, in derselben Form, die die Eingabe am
Tisch liefert — damit bleibt die Komposition unverändert und es gibt weiterhin eine
Pipeline. Die Marken bleiben draußen: die Chronik leitet aus Notizen und Foundry-Fakten
ab, welche Zahl belegt ist, und eine Uhrzeit steht in keinem Chat-Log. Ein Diktat vom
Heimweg hat keinen Bezug zu einer Sitzungsuhr und bleibt deshalb außerhalb dieser Achse —
für die Präsenzrunde ist die Szenenfolge weiterhin die einzige Zeitachse.

**Die Aufnahmen liegen neben dem Datenverzeichnis, nicht darin** (`recordings/` gegen
`data/`, im Image `/aufnahmen` gegen `/data`). Gesichert wird die SQLite; Audiospuren
gehören nie ins Backup. `--loeschen` entfernt eine Spur sofort nach einem erfolgreichen
Lauf; spätestens nach der zugesagten **Aufbewahrungsfrist** (`recordings.RETENTION_TAGE`,
derzeit 7 Tage) räumt der Stapel sie ohnehin ab. Gelöscht wird dabei nur die Audiodatei —
die Zeile bleibt mit `deleted_at` stehen, damit man sieht, dass die Spur nach Frist
entfernt wurde und nicht verlorenging.

## Einladen und Verabschieden: der Lebenszyklus einer Runde

**Die Einladung ist ehrlich.** Betritt der Bot eine Gilde, sagt er in einer Nachricht, was
er tut, wie man anfängt — und **dass er auf einem Rechner läuft, der jemand anderem
gehört, dessen Betreiber alles lesen kann, was hier abgelegt wird.** Das steht in der
ersten Nachricht und nicht im Kleingedruckten: eine Gruppe entscheidet sonst über ihre
Sitzungsprotokolle, ohne zu wissen, worüber sie entscheidet. Angelegt wird beim Betreten
noch nichts.

**`/setup` richtet ein.** Ein Fenster für die Foundry-Adresse und den Benutzer, dazu die
Wahl des Kanals, in den die fertige Chronik geht, und wahlweise Modell und Uhrzeit des
nächtlichen Laufs. Der Aufruf beansprucht die Runde für diesen Server oder legt sie an;
ein leeres Feld lässt den bisherigen Wert stehen. **Nach dem Passwort fragt das Fenster
nicht** — es kommt am Sitzungsende, wird einmal benutzt und vergessen (siehe
*Zugangsdaten*).

**Der Rauswurf wirkt sofort.** Verlässt der Bot die Gilde, wird die Runde gesperrt: es
wird nichts mehr abgelegt und nichts mehr herausgegeben. Nach **30 Tagen**
(`lebenszyklus.FRIST_TAGE`) ist sie gelöscht; eine Wiedereinladung innerhalb der Frist
bringt sie vollständig zurück, danach ist sie fort. Beides sagt der Bot vorher, in der
Einladung und vor jeder Löschung. Die Frist prüft derselbe dauerhafte Prozess, der auch
die Aufbewahrungsfrist der Aufnahmen durchsetzt — zwei Zusagen, zwei Läufe, damit ein
Fehler in der einen die andere nicht mitnimmt.

**`/chronik loeschen` erzwingt es sofort**, nach einer Rückfrage mit Knopf und einer
vollständigen Liste dessen, was verschwindet. Und das heißt vollständig: Sitzungen,
Szenen, Notizen, Diktate, Transkripte, Aufnahmen **samt Tondateien**, Chroniken,
Rückblicke, Register, Zuordnung, der Foundry-Zwischenspeicher, der Suchindex — und die
**Einwilligungsprotokolle**. Die sind der heikle Fall, denn sie belegen, dass angesagt
wurde; sie gehen trotzdem mit. Was sie belegen, ist *wer* dabei war — anonymisiert belegen
sie nichts mehr und wären bloß noch ein personenbezogener Rest ohne Zweck. Und sie
verteidigen gegen einen Vorwurf zu einer Aufnahme, die es dann nicht mehr gibt.

## Erfassen per Discord: der Thread ist die Sitzung

`/chronik start [Titel]` legt beides zugleich an — die Sitzung und den Thread, in dem sie
geschrieben wird. Der Thread ist der natürliche Behälter: Anfang, Ende, Teilnehmerliste,
Zeitachse, und die Runde tippt ohnehin dort. Darin gilt:

- **Jede Nachricht ist eine Notiz** der laufenden Szene. Eingefügter Text — Log,
  Notizzettel, was auch immer — ist einfach eine Nachricht. Der Bot quittiert sie nicht:
  sie steht im Thread und *ist* die Notiz.
- **`/szene <Name>`** zieht die Trennlinie zur nächsten Szene.
- **Eine Sprachnachricht oder ein Audio-Anhang** ist ein Diktat und reiht sich in dieselbe
  Warteschlange ein wie ein Upload — quittiert wird er, weil er den Thread verlässt.
- **`/chronik fertig`** schließt die Sitzung ab: Abgleich mit Foundry, Transkription der
  wartenden Spuren, Komposition — **ein** Auftrag, mit Statusmeldung im Thread. Das
  Foundry-Passwort wird dabei in einem Fenster erfragt, einmal verwendet und vergessen
  (siehe *Zugangsdaten*); ein Befehls-Argument stünde als Klartext im Kanalverlauf.

**Nachträgliches Erfassen geht.** Eine Nachricht Tage später im Thread gehört weiter zu
dieser Sitzung, und in welche **Szene** sie fällt, entscheidet ihr eigener Zeitpunkt: die
letzte Trennlinie *vor* ihr. Eine bearbeitete Nachricht ändert ihre Notiz, eine gelöschte
entfernt sie — Discord meldet beides, und ein Protokoll, das eine zurückgenommene Zeile
festhält, wäre die falsche Sorte Gedächtnis.

**Der Server bestimmt die Runde.** Eine Discord-Gilde gehört genau einer Runde; ist für
einen Server noch keine eingerichtet, sagt der Bot das und verweist auf `/setup`, statt in
irgendeine Chronik zu schreiben. Eine gesperrte Runde gilt dabei als keine — sie ist
verabschiedet und wartet nur noch auf ihre Frist. Ohne das Recht, im Kanal einen Thread
anzulegen, entsteht keine halbe Sitzung, sondern eine Meldung.

Die Befehle trägt derselbe dauerhafte Prozess wie die Aufnahme (`python -m chronicle.bot`,
siehe *Aufnahme per Discord*); er muss dafür laufen. Und er braucht die **Message Content
Intent** — ohne sie kämen die Nachrichten leer an.

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

## Rückblick nach Discord

Gegenrichtung: der Rückblick geht **in den Gruppenkanal**, nicht in den Briefkasten. Er
wird unmittelbar vor der nächsten Sitzung gelesen, und dort ist die Gruppe ohnehin.
Welcher Kanal, sagt *Einstellungen → Zustellkanal für den Rückblick* (z. B. `chronik`);
**leer heißt: keine Zustellung.** Einen Zeitpunkt gibt es nicht, auf den sich zielen ließe
— das System kennt keinen Sitzungskalender.

Zugestellt wird am Ende der Komposition:

```bash
python -m chronicle.compose 7    # Chronik, Rückblick, Zustellung
```

**Eine Sitzung, eine Zustellung.** Der Zeitpunkt steht in `protocol.delivered_at`, ein
zweiter Lauf sieht ihn und schweigt. Auch eine *neu komponierte* Fassung wird nicht noch
einmal gepostet: der Kanal ist die Zeitachse der Gruppe, ein zweiter Rückblick darin läse
sich wie eine zweite Sitzung. Wer die neue Fassung sehen will, liest die Chronik — dorthin
zeigt auch der Link. Gepostet wird ausschließlich der abgelegte Rückblick; er ist per
Konstruktion aus berechtigungsgefiltertem Material komponiert, und daran vorbei wird
nichts hineingereicht.

Discord kappt bei **2000 Zeichen**. Ein längerer Rückblick ist ein Fehler des Rückblicks
und kein Grund zum Aufteilen: gepostet wird der Anfang plus ein Link auf die
Protokollseite (`CHRONICLE_PUBLIC_URL`), die volle Länge steht in der Logzeile.

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
und reiht die Spuren in dieselbe Warteschlange ein wie ein Diktat, **`/aufnahme hilfe`**
sagt in drei Zeilen, was der Bot tut. Die Befehle registriert der Bot beim Start selbst.

**Jeder Befehl antwortet, auch der gescheiterte.** Ein Befehl ohne Antwort lässt Discord
ewig „denkt nach …" anzeigen — mitten in der Runde weiß dann niemand, ob aufgenommen wird
oder nicht; das ist der schlechteste aller Ausgänge und war der erste Live-Fund (#57).
Deshalb geht jede Absage in Nutzersprache heraus („Das hat nicht geklappt: … Was du tun
kannst: …"), die Einzelheiten bleiben im Log. Die Antworten sind ephemer: sie sieht nur,
wer den Befehl gegeben hat.

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

**Die Senke erfüllt py-cords Empfangs-Protokoll von Hand.** In 2.8.1 verlangt der neue
Empfangs-Router `__sink_listeners__`, `walk_children`, `root` und `is_opus` — und **keine**
mitgelieferte Senke bringt das mit, `WaveSink` eingeschlossen; py-cord warnt beim
Aufnehmen selbst, dass der Empfang wegen Discords DAVE-Umstellung derzeit kaputt ist
([Pycord #3139](https://github.com/Pycord-Development/pycord/issues/3139)). Geerbt werden
kann das also nicht, es steht in `gateway.py`. Ein Test registriert unsere Senke gegen den
**echten** Router — bricht das Protokoll wieder, ist der Test rot statt der Sitzung.

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
Werte gar nicht mehr, und ein frisch installierter Pod führt beim ersten Aufruf durch
den Einrichtungs-Wizard.

Das Image baut [`.github/workflows/build-images.yml`](.github/workflows/build-images.yml)
und veröffentlicht es nach GHCR — der Publish-Job hängt an `needs: test`, es wird also
nichts veröffentlicht, was nicht grün war. Für den Rollout wird ein fester Tag gepinnt
(`sha-<kurz>` oder die Release-Version), nicht `:latest`.

Im Container läuft `waitress`, nicht der Flask-Entwicklungsserver:

```bash
podman build -t foundry-chronicle .
podman run --rm -p 8000:8000 foundry-chronicle    # /healthz antwortet 200
```
