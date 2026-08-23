# Foundry Chronicle

**Ein Discord-Bot, der eure Spielabende mitschreibt.** Aus dem, was ihr im Thread
notiert, was im Sprachkanal gesprochen wird und was in eurem Foundry VTT gewürfelt
wird, entsteht nach der Sitzung eine lesbare Chronik — Zahlen kommen ausschließlich
aus dem Foundry-Chat-Log, erfunden werden sie nie.

Bedient wird alles in Discord: `/chronik start` öffnet die Sitzung als Thread, jede
Nachricht darin wird eine Notiz, `/aufnahme start` schneidet je Sprecher eine Spur
mit, `/chronik fertig` schließt ab. Am Morgen liegt der Rückblick im Gruppenkanal und
die Chronik als Markdown-Datei im Thread. Der ganze Ablauf steht weiter unten unter
[Erfassen per Discord](#erfassen-per-discord-der-thread-ist-die-sitzung).

**Eine Instanz trägt mehrere Runden.** Eine Runde ist eine Discord-Gilde; sie bringt
ihren eigenen Foundry-Zugang mit und sieht nichts von den anderen. Das Foundry-Passwort
wird dabei **nirgends gespeichert** — es wird gefragt, wenn der Abgleich es braucht, und
danach vergessen.

- Architektur: [`docs/architektur.md`](docs/architektur.md)
- Foundry-Zugriff: [`docs/foundry-zugriff.md`](docs/foundry-zugriff.md)
- Hausregeln: [`CLAUDE.md`](CLAUDE.md)
- Aufbau: Epic [#1](../../issues/1) — die erste Ausbaustufe, abgeschlossen.
  Epic [#62](../../issues/62) löst sie ab: Discord wird die Oberfläche.

> Status: Umbau abgeschlossen. Erfassen, Ausgeben und das Runden-Modell laufen über
> Discord. Die Weboberfläche trug seit [#157](../../issues/157) keine Spielinhalte mehr
> und ist mit [#231](../../issues/231) **ganz gefallen** — ein Prozess, ein Container,
> und der ist der Bot. Über HTTP antwortet allein `/healthz` auf der Schleife.
> „Eine Instanz pro Gruppe" war die Entscheidung des ersten Epics — sie ist mit
> [#62](../../issues/62) abgelöst.

## Entwickeln

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
python -m chronicle.bot      # der Bot — das ist der Dienst
```

**Es gibt keine Weboberfläche mehr.** Sitzung, Szene, Notiz, Diktat, Chronik, Suche,
Register, Zuordnung, Einrichtung zogen mit [#157](../../issues/157) nach Discord; die
Betreiber-Seite darüber hinaus ist mit [#231](../../issues/231) gefallen. Über HTTP
antwortet nur noch `/healthz`, das Install-Gate der Box, aus dem Bot-Prozess und
ausschließlich auf `127.0.0.1` — den Port setzt `CHRONICLE_HEALTH_PORT`, ohne ihn bindet
der Bot gar nichts (lokal braucht das Gate niemand).

**Angestoßen wird in Discord.** `/chronik fertig` startet einen **server-eigenen Lauf**
nach dem ServiceBay-Standard für lange Prozesse: der Zustand steht in der Tabelle `job`,
überlebt Neustarts, und ein Neustart mitten im Lauf wird beim nächsten Blick ehrlich als
unterbrochen vermerkt statt für immer zu laufen. Je Art läuft höchstens einer. Der
Chronik-Lauf verschriftet erst die wartenden Aufnahmen und ruft dann dieselben Funktionen
wie `python -m chronicle.compose`: ein Befehl ist der zweite Auslöser, nicht der zweite
Weg. Die Stapel-Einstiege unten bleiben — sie sind der Weg für Cron und Betrieb.

**Warum am Ende gar keine Seite blieb.** [#89](../../issues/89) hatte 2026-08-11
entschieden, die Betreiber-Seite zu behalten, während [#157](../../issues/157) die übrige
Oberfläche abräumt: dort standen Bot-Token, Ollama-Adresse und -Modell — Werte der
Instanz, die in keiner Gilde einen Ort haben — und die Verwaltungsgruppe, also wer an
diese Seite darf. [#230](../../issues/230) holte die ersten drei in die
Template-Variablen der Box. Übrig blieb eine Seite, deren einziger Inhalt die Erlaubnis
war, sie zu betreten; [#231](../../issues/231) hat sie deshalb ganz genommen. Wer an den
Bot-Token darf, entscheidet jetzt, wer die Template-Variablen dieses Dienstes bearbeiten
darf — das regelt ServiceBay. Alles Runden-eigene — Foundry-Adresse und -Benutzer,
Zustellkanal, Uhrzeit des nächtlichen Laufs samt ihrer Zeitzone und die Quelle der
Spieldaten — wird in Discord mit `/setup` gepflegt; den Foundry-Abgleich stößt
`/chronik abgleich`, `/chronik fertig` oder der nächtliche Lauf an.
**ADR 0001 (Authelia-SSO) bindet den Dienst damit nicht mehr** — er ist nicht
user-facing: keine Seite, keine Subdomain, kein veröffentlichter Port.

Die Werte der **Runde** liegen in der SQLite und werden in Discord gepflegt; ein
gepflegter Wert gewinnt über `FOUNDRY_URL` und `FOUNDRY_USER` aus der Umgebung. Die drei
Werte der **Instanz** kommen seit [#230](../../issues/230) ausschließlich aus der
Umgebung: `DISCORD_BOT_TOKEN`, `OLLAMA_URL`, `OLLAMA_MODEL`. Es gibt für sie keinen
Schreibweg in die Datei mehr — damit liegt **kein Geheimnis dieses Dienstes im Backup**.
Das Box-Template setzt sie als Variablen (siehe unten).

**Woher die Spieldaten kommen, ist eine Auswahl je Runde:** *Echter Server* oder
*Eingebaute Testwelt*. Die Testwelt ist eine Beispielwelt im Paket — 19 Konten, 91
Figuren, 8 Chat-Nachrichten, 32 Szenen —, die der Abgleich durch dieselbe Strecke schickt
wie eine Antwort vom Server: Berechtigungsfilter, System-Adapter, Zwischenspeicher. Kein
Netz, kein Passwort, kein zweiter Prozess; damit lässt sich die Instanz auch dann prüfen,
wenn der Foundry-Server aus ist. Solange sie aktiv ist, sagt es jede Meldung des
Abgleichs: *Testwelt aktiv — das sind keine echten Kampagnendaten.* Umschalten ersetzt
Konten, Figuren und Szenen im Ganzen — die sind Spiegel; die eingespielten **Chat-Nachrichten
tragen ihre Herkunft** (`foundry_message.aus_testwelt`) und werden beim Zurückschalten aus
dem Archiv genommen, denn ein erfundener Wurf, den kein Abgleich mehr zurücknimmt, wäre
später nicht mehr von einem echten zu unterscheiden. Es gehört der Runde (`settings.save_foundry_quelle`) und steht
seit [#110](../../issues/110) als Menü unter `/setup` in Discord — nicht als Feld im
Fenster, denn ein getippter Quellenname ginge beim Vertippen still ins Leere, und die
falsche Stellung füllt eine Chronik mit erfundenen Zahlen. Der Weg zurück steht daneben:
die Ansicht bleibt nach der Wahl stehen.

**Sie ist erzeugt, nicht abgegriffen.** `src/chronicle/foundry/testwelt.json` fällt
Zeichen für Zeichen aus [`scripts/erzeuge_testwelt.py`](scripts/erzeuge_testwelt.py), und
ein Test hält das fest. Kein Wert darin stammt aus einer echten Welt — keine Kennung, kein
Zeitstempel, kein Name, kein Welttitel; geteilt wird nur die *Form* einer echten
Daggerheart-Welt, damit Filter und Adapter dieselben Fälle zu sehen bekommen. Eine bloß
pseudonymisierte Welt wäre hier falsch: Konten-Graph, Rechteverteilung und Uhrzeiten
verraten weiterhin, wer wann wie lange mit wem spielt. Wer seine **eigene** Welt lokal
durchspielen will, findet den Weg dahin in
[`docs/foundry-zugriff.md`](docs/foundry-zugriff.md); der rohe Abzug ist personenbezogen
und gehört nie ins Repo.

**Das Foundry-Passwort wird nirgends gespeichert** — es gibt kein Feld dafür, keine
Variable und keine Zeile in der SQLite. Gefragt wird beim Sitzungsstart, spätestens beim
Abschluss — und nur dort, wo überhaupt ein Foundry-Server im Spiel ist; der Abgleich
verbraucht es und vergisst es, auch der, der auf der Testwelt oder an einer ruhenden Runde
abbricht; ein Rest im Arbeitsspeicher verfällt spätestens nach zwölf Stunden. Hashen
ginge nicht: Foundry will es vorgezeigt, nicht geprüft. **Der Bot-Token wird seit
[#230](../../issues/230) ebenfalls nirgends gespeichert:** er steht in
`DISCORD_BOT_TOKEN` und wird dem Bot-Prozess gereicht — dem einzigen, den es seit
[#231](../../issues/231) noch gibt. Ob er gesetzt ist, sagt der Bot, indem er läuft oder
nicht. Eine Wanderung räumt einen Bestand aus der Zeit davor aus der SQLite — aber
erst, wenn die Variable gesetzt ist: gelöscht ohne Ersatz wäre der Token
unwiederbringlich. Die Ollama-Adresse hat eine zweite Stufe: ist `OLLAMA_URL` nicht
gesetzt, gilt `http://127.0.0.1:11434` — das Ollama der Box. Offen bleibt dann allein
die Modellwahl über `OLLAMA_MODEL`. Rein aus der Umgebung kommen weiterhin
`CHRONICLE_DATA_DIR` (Vorgabe `./data`), `CHRONICLE_RECORDINGS_DIR` (Vorgabe
`./recordings`), `CHRONICLE_WHISPER_URL` — die Adresse des Spracherkenners (Vorgabe
`http://127.0.0.1:10301`, siehe [Transkription](#transkription)) — sowie `TTS_URL`, die Adresse
des Sprachdienstes, der die Ansage spricht (Vorgabe `http://127.0.0.1:8881`, siehe
[Aufnahme per Discord](#aufnahme-per-discord)). Fehlt die
Foundry-Konfiguration, startet der Dienst trotzdem und sagt es dort, wo jemand danach
fragt: in `/setup` und in der Meldung des Abgleichs.

**Es gibt keine Haustür mehr, weil es kein Haus mehr gibt.** Bis
[#231](../../issues/231) stand die Betreiber-Seite hinter Authelia
(ServiceBay-ADR 0001): `CHRONICLE_REQUIRE_REMOTE_USER=1` wies jeden Request ohne
`Remote-User` ab, und weil die Kopfzeile allein kein Beleg ist — der Dienst hörte auf
`0.0.0.0`, und wer den Port erreichte, schrieb sie sich selbst hin —, glaubte
`chronicle.herkunft` sie seit [#190](../../issues/190) nur einem Absender mit einer
Adresse **dieser Maschine**; `CHRONICLE_TRUSTED_PROXIES` war der Weg zurück für einen
umgezogenen Proxy.

Das ist alles fort: `chronicle.herkunft`, `chronicle.roles`, beide Variablen. Der einzige
Horcher des Dienstes ist `/healthz` auf `127.0.0.1` — er liest keine Kopfzeile und
beantwortet keine Frage nach Rechten. Eine Prüfung ohne Prüfling stehen zu lassen wäre
keine Vorsicht, sondern eine falsche Zusage: drei Monate später hielte sie jemand für
einen Schutz. Was den Zugriff auf **fremde Runden** angeht, hat sich dabei nichts
geändert — den trennt `db.scoped` (#63), und über einer Runde steht weiterhin niemand
(#90).

Prüfen wie die CI: `ruff check . && ruff format --check . && pytest -q`.

## Transkription

Aus einer Audiospur wird Text mit Zeitstempeln — im Stapel nach der Sitzung:

```bash
pip install -e ".[dev]"                     # kein Whisper-Modell mehr, nur ein Client
python -m chronicle.transcribe              # die Warteschlange — von Hand angestoßen
python -m chronicle.transcribe 1 mira.ogg   # Sitzung 1, Spur aus ./recordings
python -m chronicle.transcribe 1 mira.ogg --loeschen   # Aufnahme danach entfernen
```

Von selbst läuft das alles im **nächtlichen Lauf**: abholen, verschriften, abgleichen,
komponieren — zu einer Uhrzeit, die jede Runde für sich einstellt (`/setup` in Discord,
Vorgabe 04:00). Sie gilt in einer **runden-eigenen Zeitzone**; der Container selbst bleibt
auf UTC, weil eine Instanz mehrere Runden trägt. **Eingestellt wird die Zone im Feld neben
der Uhrzeit** (`/setup`, Vorgabe `Europe/Berlin`); ein Name, den die Zonendatenbank nicht
kennt, wird abgewiesen statt still übernommen — sonst stünde er in der Einstellung und der
Lauf ginge weiter nach der Vorgabe. Die Stapel-Einstiege oben sind der Weg für Cron,
Betrieb und Ungeduld.

Ohne Argumente wird abgearbeitet, was noch wartet: ein **Diktat** — eine Sprachnotiz aus
der Sprachmemo-App des Telefons — hängt als Anhang im Sitzungs-Thread und wird in dieselbe
Warteschlange eingereiht. Beim Abschluss wird es verschriftet und findet über den
Zeitpunkt seiner Nachricht in die Szene, in die es gehört. Eine Sprachmemo-App übersteht
Bildschirmsperre und Anruf, und die Quelle bleibt erhalten, bis das Transkript taugt.

Der Dateiname wird die Quellenkennung der Spur; ein zweiter Lauf ersetzt sie, statt zu
verdoppeln. **Erkannt wird nicht hier** (#216): dieser Dienst hält kein Whisper-Modell
mehr, sondern schickt den Dateinamen an `solaris-whisper-batch` — den Stapel-Erkenner der
Box, der auf ihrer Grafikkarte `large-v3-turbo` fährt. Welches Modell das ist, entscheidet
seine Unit; wählbar je Auftrag ist es nicht, sein Endpunkt nimmt nur Pfad, Sprache und
Wortvorgaben entgegen. `CHRONICLE_WHISPER_URL` überschreibt die Adresse (Vorgabe
`http://127.0.0.1:10301`); ein Feld dafür gibt es nirgends. Als Wortvorgabe gehen die
Eigennamen dieser Sitzung mit — erst, wer im Chat-Log gesprochen hat, dann der übrige
Foundry-Zwischenspeicher, hart gekappt auf `vocabulary.MAX_TOKEN` (448 − 223 − 4 − 125 = 96
Token, deutlich unter Whispers Dekoder-Fenster von 448 — siehe
`src/chronicle/transcribe/vocabulary.py` für die Herleitung). Die Stille-Erkennung (#209)
und die Dekodierung der Telefon-Formate liegen mit dem Modell drüben.

**Was von drüben zurückkommt, wird gegengelesen.** In stillen Abschnitten gibt der
Erkenner die vorgespannten Namen als Transkript zurück, und die landeten unverändert in
der Chronik (#262) — erfundener Text, noch vor dem Sprachmodell. Ein Segment, das aus
mindestens drei bloßen Namen besteht und mehrheitlich wörtlich aus dem Register stammt,
wird deshalb verworfen. Ein einzeln stehender Name überlebt, zwei nebeneinander auch, und
ein einziges gesprochenes Wort dazwischen rettet das ganze Segment: die Grenze ist so
gezogen, dass sie eher einen Papagei durchlässt als eine Äußerung löscht. Dass sie hier
liegt und nicht drüben, ist kein Umweg — seit #216 können wir die Stille-Erkennung des
Nachbardienstes weder einstellen noch ablesen, und das Register kennen ohnehin nur wir.

**Einen Rückfall gibt es nicht.** Der CPU-Weg aus #84 ist ersatzlos entfallen — das ist so
entschieden, nicht vergessen. Ist der Erkenner aus, bleibt die Spur **wartend** liegen,
der nächtliche Lauf sagt es auf seiner Karte und schreibt für diese Sitzung **keine**
Chronik: eine ohne das gesprochene Wort sähe fertig aus, und dann fiele der Abend
stillschweigend von der Fälligkeitsliste. Die nächste Nacht holt ihn nach.

Im Repo bleibt damit nur ein HTTP-Aufruf: kein `faster-whisper`, kein PyAV, kein
onnxruntime, kein Extra. Die Tests setzen ein erfundenes Modell und eine Attrappe des
Dienstes ein und gehen nie ans Netz.

**Mehrere Spuren werden zu einer Unterhaltung.** Schneidet der Aufnahme-Bot mit, liegt je
Sprecher eine Spur; nacheinander gelesen wären das Monologe. Der Abschluss verschränkt sie
deshalb nach Zeit — die Marke zählt ab dem Aufnahmebeginn, dem gemeinsamen Nullpunkt aller
Spuren, und der Name kommt aus der bestätigten Zuordnung. Ohne Zuordnung steht der
Discord-Name da; geraten wird keiner. Das Verschränkte wird der Szene **als Notiz**
übernommen, in derselben Form, die die Eingabe am Tisch liefert — damit bleibt die
Komposition unverändert und es gibt weiterhin eine Pipeline. Die Marken bleiben draußen: die Chronik leitet aus Notizen und Foundry-Fakten
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

**`/setup` richtet ein.** Ein Fenster mit vier Feldern — Foundry-Adresse, Benutzer, und
wahlweise Uhrzeit und Zeitzone des nächtlichen Laufs —, darunter zwei Menüs: der Kanal, in
den die fertige Chronik geht, und die Quelle der Spieldaten. Discord nimmt fünf Felder je
Fenster; die Zone gehört zur Uhrzeit und steht deshalb dort, die Quelle ist ein Schalter
mit zwei Stellungen und deshalb ein Menü. Das Ollama-Modell steht nicht darin: es gehört
der Instanz und nicht der Runde (#87). Der Aufruf beansprucht die Runde für diesen Server
oder legt sie an; ein leeres Feld lässt den bisherigen Wert stehen, ein unlesbarer Wert
wird abgewiesen und gesagt. **Nach dem Passwort fragt das Fenster
nicht** — es kommt beim Sitzungsstart, wird einmal benutzt und vergessen (siehe
*Zugangsdaten*). Aufrufen darf ihn, **wer den Server verwaltet**: hier steht, welchem
Foundry-Server der Bot später das Passwort der Spielleitung vorzeigt.

**Der Rauswurf wirkt sofort.** Verlässt der Bot die Gilde, wird die Runde gesperrt: es
wird nichts mehr abgelegt und nichts mehr herausgegeben. Sofort heißt dabei in jedem
Faden — der nächtliche Lauf überspringt sie, Verschriften, Komponieren und der
Foundry-Abgleich weigern sich, und ein noch im Speicher liegendes Foundry-Passwort ist
mit dem Rauswurf vergessen. Nach **30 Tagen** (`lebenszyklus.FRIST_TAGE`) ist sie
gelöscht; eine Wiedereinladung innerhalb der Frist bringt sie vollständig zurück, danach
ist sie fort — eine Gilde, die nach der Frist zurückkommt, wird begrüßt wie eine fremde,
und der Rest wird dabei gelöscht. Beides sagt der Bot vorher, in der Einladung und vor
jeder Löschung. Die Frist prüft derselbe dauerhafte Prozess, der auch die
Aufbewahrungsfrist der Aufnahmen durchsetzt — zwei Zusagen, zwei Läufe, damit ein Fehler
in der einen die andere nicht mitnimmt.

**`/chronik loeschen` erzwingt es sofort**, nach einer Rückfrage mit Knopf und einer
vollständigen Liste dessen, was verschwindet; geben darf ihn die **Administration** des
Servers, und auch eine hinausgeworfene Runde darf es, ohne den Bot dafür wieder
einzuladen. Und das heißt vollständig: Sitzungen, Szenen, Notizen, Diktate, Transkripte,
Aufnahmen **samt Tondateien** — auch denen, die nie eine Zeile bekommen haben —,
Chroniken, Rückblicke, Register, Zuordnung, der Foundry-Zwischenspeicher, der Suchindex
und die **Einwilligungsprotokolle**. Die sind der heikle Fall, denn sie belegen, dass
angesagt wurde; sie gehen trotzdem mit. Was sie belegen, ist *wer* dabei war —
anonymisiert belegen sie nichts mehr und wären bloß noch ein personenbezogener Rest ohne
Zweck über Menschen, die mit dieser Instanz nichts mehr zu tun haben. Der Preis wird
dabei nicht verschwiegen: eine ausgelieferte Chronik liegt in einem Discord-Kanal und
bleibt dort, das Abgeleitete überdauert also den Beleg. Genau das steht in der
Rückfrage, damit sich holen kann, wer den Beleg braucht.

**`/chronik sitzung-loeschen` nimmt genau einen Abend** — der kleine Weg neben dem
großen, für den Fehlgriff beim Einlesen und den Testabend vom Einrichten. Erst ein Menü
mit den Sitzungen, dann eine Rückfrage, die benennt, was an der gewählten hängt: Szenen,
Notizen, Verschriftungen, die geschriebenen Texte — und die **Tondateien mit Zahl**, auch
die, die noch keine Zeile haben. Erst dahinter der Knopf. Hier gilt die **entgegengesetzte**
Regel zur großen Löschung: der **Nachweis der Ansage bleibt stehen**. Die Runde gibt es
weiter, und was belegt, dass im Sprachkanal angesagt wurde, ist genau dann etwas wert,
wenn das daraus Gemachte noch irgendwo liegt — in Discord tut es das. Wer auch den Beleg
nicht mehr will, löscht die ganze Runde. Geben darf den Befehl die **Administration**,
dieselbe Schranke wie vor der ganzen Runde und nicht die niedrigere der Verwaltung: diese
Löschung ist nicht die harmlosere, sondern die **lautlose** — auswählbar, ephemer, und
niemand in der Gruppe erfährt davon.

**Fortnehmen kann euch die Runde niemand sonst.** Es gibt keinen Befehl und keine Seite,
über die der Betreiber der Box eine fremde Runde sperrt oder löscht — sie verschwindet,
weil ihr es sagt, oder weil die Frist nach dem Rauswurf abläuft. Dass er *lesen* kann, was
hier liegt, steht in der ersten Nachricht und bleibt wahr; es ist seine Kiste. Fortnehmen
ist die andere Zusage, und die gibt es hier bewusst nicht.

## Erfassen per Discord: der Thread ist die Sitzung

`/chronik start [Titel]` legt beides zugleich an — die Sitzung und den Thread, in dem sie
geschrieben wird. Davor steht ein Fenster für das **Foundry-Passwort**: wer es gibt, hat
Foundry die ganze Sitzung über offen; wer das Feld leer lässt, spielt ohne die Zahlen
weiter und wird beim Abschluss noch einmal gefragt — **an einem fehlenden Passwort
scheitert keine Sitzung.** Wo gar kein Foundry-Server eingetragen ist oder die Runde auf
der Testwelt läuft, kommt das Fenster erst gar nicht. Der Thread ist der natürliche
Behälter: Anfang, Ende,
Teilnehmerliste, Zeitachse, und die Runde tippt ohnehin dort. Darin gilt:

- **Jede Nachricht ist eine Notiz** der laufenden Szene. Eingefügter Text — Log,
  Notizzettel, was auch immer — ist einfach eine Nachricht. Der Bot quittiert sie nicht:
  sie steht im Thread und *ist* die Notiz.
- **`/szene <Name>`** zieht die Trennlinie zur nächsten Szene.
- **Eine Sprachnachricht oder ein Audio-Anhang** ist ein Diktat und reiht sich in dieselbe
  Warteschlange ein wie ein Upload — quittiert wird er, weil er den Thread verlässt.
- **`/chronik fertig`** schließt die Sitzung ab: Abgleich mit Foundry, Transkription der
  wartenden Spuren, Komposition — **ein** Auftrag, mit Statusmeldung im Thread. Nach dem
  Foundry-Passwort fragt ein Fenster nur, wenn **du selbst** beim Start keines gegeben
  hast: `/chronik start` steht jedem Mitglied offen, und die Eingabe eines anderen wird
  nicht stillschweigend deinem Abschluss untergeschoben. Verwendet und vergessen wird es
  so oder so (siehe *Zugangsdaten*). Ein Befehls-Argument gibt es dafür nicht — es stünde
  als Klartext im Kanalverlauf.
- **`/chronik abgleich`** holt nur die Zahlen, ohne Sitzung und ohne Thread: der Griff für
  den Abend, an dem Foundry aus war und der Stand nachgezogen werden soll, statt bis zum
  nächtlichen Lauf zu warten ([#116](../../issues/116)). Dasselbe Fenster fürs Passwort,
  derselbe server-eigene Lauf, dieselbe Meldung im Kanal — ein dritter Auslöser, kein
  dritter Weg. Eine ruhende Runde bekommt auch hier nichts.

**Ein vorhandener Notizbestand kommt nachträglich herein.** Wer schon länger spielt und
seine Notizen in **einem** Markdown-Dokument hat — ein Abschnitt je Abend —, hängt es an
`/chronik einlesen` an, **im Kanal der Runde** und nicht im Thread: ein Dokument deckt
mehrere Abende ab und gehört in keinen einzelnen. Aufgeteilt wird an den Überschriften,
und zwar relativ statt fest: die Abende trennt die oberste Ebene, deren Überschriften ein
**Datum** tragen, alles darunter die Szenen — dasselbe Dokument mit `#` je Abend ergibt
dieselben Sitzungen wie eines mit `##`-Abenden unter einem `#`-Vorspann. Der Text wird
übernommen, wie er dasteht: kein Modell, keine Zusammenfassung, kein geratenes Datum — eine
Überschrift ohne lesbares Datum wird benannt und übersprungen. Ebenso ein Abend, unter dem
**kein einziger Satz** steht: er trüge nichts in die Chronik und wäre beim nächsten Einlesen
nicht wiederzuerkennen, also wird auch er benannt und nicht angelegt
([#172](../../issues/172)). Der Bot zeigt erst, **was
entstünde**; ohne Bestätigung entsteht nichts, und dieselbe Datei ein zweites Mal
hochgeladen verdoppelt den Bestand nicht ([#169](../../issues/169)).

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
   **Token** erzeugen. Der Token wird als Template-Variable `DISCORD_BOT_TOKEN` des
   Dienstes hinterlegt — nie ins Repo, nie in eine Nachricht.
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

Die Box ist nur im Heimnetz erreichbar, der Diktat-Moment aber auf dem Heimweg.
Discord ist von überall erreichbar und von Natur aus ein Briefkasten: einwerfen, wann es
einem einfällt — geholt wird, wenn der Dienst das nächste Mal läuft.

```bash
python -m chronicle.discord     # den Kanal #diktat leeren — vor der Transkription
```

Der Bot liest **genau einen** Kanal, nach Namenskonvention `#diktat` — und zwar den **in
der Gilde dieser Runde**. Gesucht wird nie darüber hinaus: »diktat« heißt in jeder zweiten
Gilde ein Kanal, und ein Einwurf aus einer fremden würde verschriftet und stünde danach in
einer Chronik, in die er nicht gehört ([#192](../../issues/192)). Eine Runde ohne Gilde hat
damit keinen Briefkasten; sie bekommt keinen geliehen, sondern eine Meldung. Eine
Sprachnachricht
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
ist es eine bewusste Entscheidung — der Diktat-Kanal darf leer bleiben, dann bleiben die
getippten Notizen im Sitzungs-Thread der Weg. (Bis #157 stand hier das Web-Formular; das
gibt es nicht mehr.)

## Rückblick nach Discord

Gegenrichtung: der Rückblick geht **in den Gruppenkanal**, nicht in den Briefkasten. Er
wird unmittelbar vor der nächsten Sitzung gelesen, und dort ist die Gruppe ohnehin.
Welcher Kanal, sagt die Gruppe selbst mit **`/setup` in Discord** (Feld *Zustellkanal*);
**keiner ist eine gültige Wahl und heißt: keine Zustellung.** Einen Zeitpunkt gibt es
nicht, auf den sich zielen ließe — das System kennt keinen Sitzungskalender.

**Kommt die Zustellung nicht durch, wird es gesagt** (#182): der Satz hängt an der
Antwort auf `/chronik fertig` und an der Karte des nächtlichen Laufs, also dort, wo die
Gruppe ohnehin liest. Der nicht auflösbare Wert bleibt aus dieser Nachricht heraus und
steht nur im Log des Betreibers — eine Kanal-Kennung ist nichts, was in eine
Gruppennachricht gehört. Bis dahin scheiterte die Zustellung **still**, und niemand in
der Runde konnte es merken.

Zugestellt wird am Ende der Komposition:

```bash
python -m chronicle.compose 7      # Chronik, Rückblick, Zustellung
python -m chronicle.compose 7 2    # dieselbe Sitzung, genannte Runde
```

**Die Runde nennt der Aufrufer** (#245). Trägt die Instanz mehr als eine, fragt der
Befehl nach und zählt sie auf, statt sich stillschweigend die erste zu nehmen; die
Sitzungs-Id wird nicht über die Runden hinweg aufgelöst. Wo es nur eine Runde gibt,
bleibt der Aufruf, wie er war.

**Eine Sitzung, eine Zustellung.** Der Zeitpunkt steht in `protocol.delivered_at`, ein
zweiter Lauf sieht ihn und schweigt. Auch eine *neu komponierte* Fassung wird nicht noch
einmal gepostet: der Kanal ist die Zeitachse der Gruppe, ein zweiter Rückblick darin läse
sich wie eine zweite Sitzung. Wer die neue Fassung sehen will, liest die Chronik — dorthin
zeigt auch der Link. Gepostet wird ausschließlich der abgelegte Rückblick; er ist per
Konstruktion aus berechtigungsgefiltertem Material komponiert, und daran vorbei wird
nichts hineingereicht.

Discord kappt bei **2000 Zeichen**. Ein längerer Rückblick ist ein Fehler des Rückblicks
und kein Grund zum Aufteilen: gepostet wird der Anfang plus der Hinweis, dass die ganze
Sitzung als Chronik-Datei im Thread liegt; die volle Länge steht in der Logzeile. (Bis
#157 zeigte der Hinweis auf eine Protokollseite unter `CHRONICLE_PUBLIC_URL`; die Seite
war schon damals fort, und mit #231 auch die Variable.)

## Aufnahme per Discord

```bash
pip install -e ".[dev,discord]"      # py-cord ist ein eigenes Extra
python -m chronicle.bot              # ein eigener, dauerhafter Prozess
```

Der Aufnahme-Bot ist **kein Stapellauf**: er hält eine Gateway-Verbindung, weil Sprache
nur mitgeschnitten werden kann, während sie gesprochen wird. Auf der Box ist er seit
[#231](../../issues/231) der **einzige** Container des Pods: `python -m chronicle.bot`
ist die Vorgabe des Abbilds, das Template überschreibt sie nicht. Ohne Bot-Token
verbindet er sich nicht und sagt das in einem Satz.

Er trägt seit #228 auch **`/healthz`, das Install-Gate der Box** — zehn Zeilen HTTP, kein
Flask, auf `CHRONICLE_HEALTH_PORT` und **nur auf `127.0.0.1`**: der Pod liegt im Host-Netz,
ein `0.0.0.0` stünde im ganzen LAN offen (#190). Ist die Variable nicht gesetzt, bindet er
nichts — auf einer Entwicklungsmaschine braucht das Gate niemand. Weil daran die
Installation hängt, **beendet** er sich ohne Token nicht mehr, sondern bleibt liegen und
antwortet weiter: bei der Erstinstallation ist der fehlende Token der Normalfall, und der
Poller fände sonst niemanden. Ein neuer Anmeldeversuch wird daraus trotzdem nicht.

Im Sprachkanal: **`/aufnahme start`** holt den Bot in den Kanal des Aufrufers — eine
Kanal-Konfiguration braucht es deshalb nicht —, **`/aufnahme stop`** beendet die Aufnahme
und reiht die Spuren in dieselbe Warteschlange ein wie ein Diktat, **`/aufnahme hilfe`**
sagt in drei Zeilen, was der Bot tut. Die Befehle registriert der Bot beim Start selbst.

**`/aufnahme test`** beantwortet die eine Frage, die von außen nicht zu sehen ist: kommt
hier überhaupt lesbarer Ton an? Der Bot folgt in den Sprachkanal, lauscht zehn Sekunden
und berichtet nur dem Aufrufer, was wirklich ankam — Pakete, je Sprecher eine Spur mit
ihren Bytes — und was das heißt. Verlorene Rahmen zählt er nicht mit und sagt das auch:
py-cord fängt einen Dekodierfehler an Ort und Stelle ab und füllt die Lücke, sichtbar
allein als Warnung im Log. Es ist ein Mitschnitt wie jeder andere: die hörbare Ansage
läuft, die Einwilligung wird protokolliert, und danach werden die Probespuren gelöscht
statt eingereiht. Eine laufende Aufnahme rührt er nicht an.

**Jeder Befehl antwortet, auch der gescheiterte.** Ein Befehl ohne Antwort lässt Discord
ewig „denkt nach …" anzeigen — mitten in der Runde weiß dann niemand, ob aufgenommen wird
oder nicht; das ist der schlechteste aller Ausgänge und war der erste Live-Fund (#57).
Deshalb geht jede Absage in Nutzersprache heraus („Das hat nicht geklappt: … Was du tun
kannst: …"), die Einzelheiten bleiben im Log. Die Antworten sind ephemer: sie sieht nur,
wer den Befehl gegeben hat.

### Die Ansage ist der Kern, nicht die Verpackung

Das Aufzeichnen des nichtöffentlich gesprochenen Wortes ohne Einwilligung ist strafbar
(**§201 StGB**). Beim Betreten des Kanals stellt sich der Bot **zuerst schriftlich** vor —
wer er ist, dass gleich eine hörbare Ansage kommt und erst danach mitgeschnitten wird, wie
lange die Spuren bleiben und wie man ihn bedient; hat der Sprachkanal keinen eigenen Chat,
geht die Vorstellung dorthin, wo der Befehl kam. Der Beleg ist sie nicht — sie gibt nur
Zeit zu lesen, bevor gesprochen wird. Die hörbare Ansage danach ist **kurz**: dass ab jetzt
aufgezeichnet wird, dass Verlassen des Kanals heißt: keine Aufnahme, und dass die
Einzelheiten im Kanal stehen — die Runde wartet, während sie läuft, und gelesen hat sie
den langen Text schon. **Der Mitschnitt
beginnt erst, wenn die Ansage zu Ende gespielt ist**; wer davor zu schreiben versucht,
bekommt einen Fehler und keine Datei. Wer *nach* dem Start dazukommt, hört dieselbe Ansage
noch einmal und wird eigens protokolliert — bloß zu vermerken, dass jemand sie verpasst
hat, hielte fest, dass er nicht eingewilligt hat, statt ihn zu fragen.

Protokolliert wird jede Ansage in der SQLite: Zeitpunkt, Server und Kanal, die Anwesenden
mit Id und Anzeigename — und der **Wortlaut**: der gesprochene Satz *und* die Bedingungen,
auf die er verweist. Nicht ein Verweis auf den Text im Code: ändert jemand die Ansage, darf
sich das Protokoll vergangener Sitzungen nicht mitändern, und ein Eintrag, der auf einen
Text zeigt, der später ein anderer sein kann, belegte nichts.
Der Eintrag überlebt auch das Löschen seiner Sitzung.

Gesprochen wird die Ansage vom **Sprachdienst der Box** — Kokoro mit deutscher Stimme,
OpenAI-kompatibel, per Vorgabe auf `http://127.0.0.1:8881` und über `TTS_URL` umzustellen.
Antwortet er nicht innerhalb von zehn Sekunden, springt **espeak-ng** ein: eine Ansage, die
gar nicht kommt, verhindert die Aufnahme, und das wiegt schwerer als eine hässliche Stimme.
Fehlt am Ende auch espeak-ng, wird **nicht** aufgenommen. Erzeugt wird beim ersten Bedarf
aus dem Text in `chronicle/bot/ansage.py`, abgelegt unter dessen Fingerabdruck im
Aufnahmeverzeichnis — damit können Ansage und Protokoll nicht auseinanderlaufen.
`TTS_URL` hat bewusst **keine** Template-Variable: dorthin fließt kein Wort der Runde,
sondern allein unser eigener Ansagetext, und ein falscher Wert ändert nur die Stimme,
nicht das Ergebnis.

### Die zugesagte Frist wird auch eingehalten

Die Ansage nennt eine Aufbewahrungsfrist — und **dieselbe Zahl setzt sie durch**: der Satz
wird aus `recordings.RETENTION_TAGE` (7) formatiert, und `recordings.sweep` räumt danach
auf. Ein Versprechen, das nur im Ansagetext steht, wäre keins; so können Satz und Verhalten
nicht auseinanderlaufen. Durchgesetzt wird an zwei Stellen, damit die Zusage auch gilt,
wenn eine davon eine Weile steht: **im laufenden Bot** einmal beim Start und danach täglich,
und **am Ende jedes `python -m chronicle.transcribe`-Laufs**, auch wenn nichts zu tun war.

Gelöscht wird nur die Audiodatei. Die Zeile in der Datenbank bleibt mit `deleted_at` stehen
— dass es die Spur gab, wann sie kam und was aus ihr wurde, ist die ehrliche Hälfte der
Geschichte; das Transkript bleibt ohnehin.

### Je Sprecher eine Spur

Discord trennt die Audiodaten ohnehin pro Client. Damit entfällt die Sprechertrennung
nicht bloß billiger, sondern exakt — jede Diarisierung rät bei Überlappungen, und in einer
Rollenspielrunde reden fünf Leute durcheinander. Geschrieben wird im Strom auf die Platte
und nie in einen Puffer im Speicher.

Je Sprecher entsteht dabei eine **Folge von Häppchen** von je fünf Minuten und nicht
mehr eine Datei über den ganzen Abend (#217). Jedes wird eingereiht, sobald es voll ist,
und `chronicle.mitlauf` verschriftet es **noch während der Sitzung** (#269) — in einem
eigenen Faden im Bot-Prozess, still, ohne Zwischenstand im Kanal: der Unterschied ist
»morgen früh steht die Chronik« gegen »sie steht, wenn ihr aufhört«. Bis dahin lag die
Warteschlange bis zum Abschluss oder bis zur Nacht unangetastet da. Die fünf Minuten
lösen dreißig ab, weil an ihnen jetzt hängt, wie lange es bis zum ersten Text dauert;
vier Stunden mit fünf Sprechern sind damit rund 240 Dateien statt vierzig, und jede
Meldung darüber zählt je Sitzung oder je Sprecher. Geschnitten wird nach der Zeit und
nicht an Sprechpausen: py-cord füllt die Pausen vor unserem `write` auf, sie sind an
dieser Stelle also gar nicht greifbar. Der Preis ist ein Wort je Schnitt.

**Jedes Häppchen bringt seinen Startversatz mit** (`recording.offset_ms`), und daran hängt
die Zusammenführung. `transcript_segment.start_ms` ist sitzungsabsolut; eine Datei fängt
für den Erkenner aber wieder bei null an. Der Versatz wird deshalb aufgeschlagen, bevor
gespeichert wird, und er wird aus den geschriebenen Bytes **gerechnet** und nicht nach der
Wanduhr geschätzt: weil py-cord die Pausen auffüllt, *ist* die Byte-Position die Position
auf der Sitzungsuhr. Ohne ihn stünde jedes Häppchen wieder am Anfang des Abends, die
Verschränkung zerfiele und jede Äußerung landete in der falschen Szene — still.

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

**py-cord hängt an einem unveröffentlichten Commit** — festgenagelt in `pyproject.toml`,
mit der Begründung an derselben Zeile (#60). Seit dem 2026-03-02 erzwingt Discord DAVE auf
allen Sprachkanälen, und die veröffentlichte 2.8.1 entschlüsselt **nach** dem Dekodieren:
der Dekoder sieht Rauschen, wirft `OpusError: corrupted stream`, der Empfang stirbt und die
Sitzung bekommt keine einzige Spur. Sich von DAVE abzumelden ist kein Ausweg — Discord
antwortet mit `WebSocket closed with 4017` und verweigert die Sprachverbindung ganz.
[Pycord-PR #3159](https://github.com/Pycord-Development/pycord/pull/3159) dreht die
Reihenfolge um; genagelt wird auf den **Commit**, nicht auf den Branch.

Gezogen wird dieser Commit seit dem 2026-08-12 aus dem eigenen Fork
[`mdopp/pycord`](https://github.com/mdopp/pycord) statt aus dem fremden PR-Branch: wird
`fix/voice-rec-2` gelöscht oder umgeschrieben, scheitert sonst der Image-Bau (#152). Die
README des Forks nennt Upstream-Stand, Datum und Grund. Zurück auf eine Veröffentlichung
geht es, sobald #3159 gemergt **und** ausgeliefert ist — dann fällt der Fork weg. Damit
das auffällt, fragt `scripts/pruefe_pycord_ausstieg.py` die Lage wöchentlich ab
(`.github/workflows/pycord-ausstieg.yml`) und schlägt fehl, sobald sie kippt.

**Die Senke erfüllt py-cords Empfangs-Protokoll von Hand.** Der Empfangs-Router verlangt
`__sink_listeners__`, `walk_children`, `root` und `is_opus`; in 2.8.1 brachte das **keine**
mitgelieferte Senke mit, `WaveSink` eingeschlossen
([Pycord #3139](https://github.com/Pycord-Development/pycord/issues/3139)). Der
festgenagelte Stand legt es in die Basisklasse — von Hand steht es in `gateway.py`
trotzdem weiter, weil es beides bedient und nichts kostet. Ein Test registriert unsere
Senke gegen den **echten** Router — bricht das Protokoll wieder, ist der Test rot statt
der Sitzung.

## Betrieb auf ServiceBay

Dieses Repo ist zugleich eine **ServiceBay-Registry**: unter [`templates/`](templates/)
liegt das Pod-Template `daggerheart-chronik`. Auf der Box wird das Repo einmal in
`config.registries[]` eingetragen (Git-URL dieses Repos), danach steht das Template im
Installations-Assistenten neben den mitgelieferten.

Der Assistent fragt nach dem Port des Install-Gates, dem Image-Tag — und seit
[#230](../../issues/230) nach `DISCORD_BOT_TOKEN`, `OLLAMA_URL` und `OLLAMA_MODEL`. Nach
Subdomain und HTTP-Port fragt er seit [#231](../../issues/231) **nicht** mehr: der Dienst
veröffentlicht nichts und braucht weder nginx noch Authelia davor. Der **Foundry-Zugang**
wird weiterhin in Discord unter `/setup` eingerichtet: er gehört der Runde, nicht der
Instanz.

Die drei Instanz-Variablen sind bewusst vom Typ `text` und **nicht** `secret`: für ein
`secret` würfelt der Assistent einen Zufallswert aus — richtig für ein internes
Geheimnis, falsch für Zugangsdaten, die nur die Gegenstelle kennt. Ein solcher Wert
meldete sich einmal als Bot-Token bei Discord an und scheiterte mit 401 in einer
Neustart-Schleife. Leer heißt deshalb ehrlich »nicht gesetzt«: der Bot sagt einen Satz
und bleibt liegen. Nachtragen oder wechseln lässt sich ein Wert jederzeit mit
`install_template(names=["daggerheart-chronik"], variables={"DISCORD_BOT_TOKEN": "…"})`;
der übergebene Wert gewinnt (ServiceBay-Rezept *rotate-a-service-secret*).

Das Image baut [`.github/workflows/build-images.yml`](.github/workflows/build-images.yml)
und veröffentlicht es nach GHCR — der Publish-Job hängt an `needs: test`, es wird also
nichts veröffentlicht, was nicht grün war. Für den Rollout wird ein fester Tag gepinnt
(`sha-<kurz>` oder die Release-Version), nicht `:latest`.

Im Container läuft `python -m chronicle.bot` — **kein Webserver**: Flask, Jinja2 und
waitress sind mit [#231](../../issues/231) aus dem Abhängigkeitsbaum verschwunden.

```bash
podman build -t foundry-chronicle .
# Ohne Token bleibt der Bot liegen und bedient nur das Gate — genau der Zustand einer
# frischen Installation.
podman run --rm --network host -e CHRONICLE_HEALTH_PORT=8701 foundry-chronicle
curl -sf http://127.0.0.1:8701/healthz    # {"status": "ok"}
```
