# Foundry Chronicle — Hausregeln

Sitzungsprotokolle für Tisch-Rollenspiel: aus Notizen und Foundrys Chat-Log wird eine
lesbare Chronik. **Eine Instanz trägt mehrere Runden** (Epic #62 löst darin »eine Instanz
pro Gruppe« aus Epic #1 ab). Siehe Epic #1 und #62 für die tragenden Entscheidungen und
`docs/architektur.md` für das Bild.

Diese Regeln gelten für jede Sitzung, Mensch oder Agent.

## Die Entscheidungen, die nicht neu verhandelt werden

- **Transkription ist eine vorgeschaltete Stufe, kein zweiter Weg.** Präsenz- und
  Online-Sitzungen treffen sich bei der Zusammenführung. Es gibt eine Pipeline.
- **Foundry liefert die Zahlen, der Text die Erzählung.** Würfe, Schaden und Beute
  werden eingesetzt, nie rekonstruiert.
- **Alles nach der Aufnahme läuft im Stapel.** Keine Echtzeit, keine Latenzgrenze,
  keine GPU-Pflicht.
  **Ergänzt am 2026-08-25 (#294): der Szenenschnitt ist ein Stapellauf, er liegt nur
  früher.** Schließt die Runde eine Szene, wird *diese* Szene sofort verschriftet,
  verdichtet und als Zwischenstand in den Thread gestellt, während weitergespielt wird.
  Das nimmt von den drei Zusagen **keine** zurück: niemand wartet auf das Ergebnis, die
  nächste Szene läuft bereits — es gibt also weiterhin keine Latenzgrenze und keinen
  Fortschrittsbalken, der Echtzeit vortäuscht. Verschoben ist allein der *Zeitpunkt* des
  Laufs, vom Sitzungsende an den Schnitt.
  Die GPU-Pflicht gilt für diesen einen Weg trotzdem: verschriftet wird währenddessen
  nur, wo eine Karte steht (#295). Ohne Karte fällt der Zwischenstand aus — die Chronik
  am Ende entsteht wie bisher.
  **Der Modelldienst dieser Box ist `llama-server`, nicht mehr Ollama** (#329, seit
  2026-09-06; Konzept in `mdopp/solarisbay#1332`, Operator-Auftrag »ollama ganz weg«). Er
  hört auf `127.0.0.1:11435` und spricht `/v1/chat/completions`; zu wählen gibt es nichts,
  es ist der einzige Weg. **Er hält das Modell von sich aus** — welches geladen
  ist, entscheidet ein Profil, und umgeschaltet wird über das Sitzungsfenster weiter unten.
  Damit ist die `keep_alive`-Mechanik im nächsten Absatz **Historie**: sie beantwortete
  eine Frage, die Ollama stellte und die dieser Server nicht mehr stellt. Sie bleibt
  lesbar, weil sie erklärt, warum das Fenster so aussieht, wie es aussieht.
  **Der Ollama-Weg ist raus** (#329, zweite Hälfte, 2026-09-06). `OllamaClient`, seine
  Attrappe und der Schalter `CHRONICLE_LLM_BACKEND` standen bis dahin weiter im Code, weil
  ein Weg, der noch läuft, nicht entfernt wird, bevor er wirklich niemanden mehr trägt.
  Genau diese Bedingung ist eingetreten: `mdopp/solarisbay#1332` ist gemerged, und aus dem
  deployten Container gemessen antwortet `127.0.0.1:11434` nicht mehr. Der Grundsatz bleibt
  stehen — er erklärt die Wartezeit. **Der Schalter ging mit statt einwertig
  stehenzubleiben:** einer mit genau einem gültigen Wert ist eine falsche Zusage, denn wer
  `ollama` in das Textfeld tippt, bekäme still `openai`.
  Zwei Zahlen weiter unten sind überholt und im Imperfekt zu lesen: der Modelltausch
  kostet **20 s**, nicht 56, und ein Szenen-Zwischenstand auf dem großen Modell braucht
  durch den deployten Dienst **157 s** — weshalb seine Frist mit #330 auf acht Minuten
  steht (beides gemessen 2026-09-06).

  **Gehalten wurde das Modell dafür nicht** (#303, seit 2026-08-26). #295 hatte die
  Sitzung das große Modell festhalten lassen, damit der Szenenschnitt es nicht jedes Mal
  neu lädt. Die Messung des Nachbardienstes hat das entschieden: unser Chronik-Modell und
  seines passen auf der Karte dieser Box **nicht** nebeneinander, jedes Laden verdrängt
  ihn. Damit greift die vorab verabredete Rückfallebene — wir halten nicht, der Nachbar
  behält sein Modell, und wir nehmen den Tausch je Szenenschnitt in Kauf. **Ersatzlos
  streichen wäre das Gegenteil gewesen:** der Ollama-Dienst dieser Box setzt
  `OLLAMA_KEEP_ALIVE=24h`, und ein Aufruf ohne ausdrückliches `keep_alive` erbt diese
  vierundzwanzig Stunden statt unserer zwei. Jeder Aufruf schickte deshalb eine knappe
  Frist mit, und am Ende jedes Aufschriebs wurde ausdrücklich freigegeben (#300) — **die
  eine Freigabestelle gibt es weiterhin**, sie schließt heute das Fenster statt einer
  Haltefrist.

  **Die benannte Ausnahme dazu ist das Sitzungsfenster** (#299, seit 2026-08-27,
  Gegenstück `mdopp/solarisbay#1260`). Beginnt ein Abend, meldet der Bot ihn beim
  Nachbardienst über die Schleife an — `POST /api/model-lease {model, ttl_s}` (**nicht**
  unter `/napi/`: dieses Präfix ist beim Nachbarn token-pflichtig, #306), fünfzehn
  Minuten, alle fünf erneuert; am Ende jedes Aufschriebs, an derselben einen Freigabestelle,
  geht ein `DELETE` hinaus. Solange das Fenster steht, antwortet der Nachbar mit *unserem*
  Modell, statt seines bei jeder Anfrage zurückzuholen — auf dieser Karte kostet jeder
  Wechsel rund 56 s, in beide Richtungen. **Das nahm #303 nicht zurück:** die knappe
  Frist blieb die Norm, das Fenster war die Ausnahme, und beide Zahlen kamen aus
  **derselben Konstante** — was wir dem Nachbarn zusagten, war genau das `keep_alive`, das
  unsere Aufrufe trugen. Seit der Ollama-Weg gefallen ist (#329), trägt allein die Frist
  des Fensters; die Paarung ist Historie, und sie steht hier, weil sie erklärt, warum die
  Zahl so gewählt ist. Dass die beiden Modelle nicht nebeneinander passen, ist die
  *Prämisse* des Vertrags und nicht der Einwand dagegen; passten sie, bräuchte es ihn
  nicht. Der Aufruf geht über die Schleife, ohne Token und ausdrücklich **ohne gemeinsames
  Geheimnis** (seit #230 hat diese Instanz keines mehr), er ist in beide Richtungen bester
  Wille — ein gescheitertes Fenster hält weder den Beginn noch den Abschluss einer Sitzung
  auf —, und **die Nutzlast trägt keine Runden-, Gilden- oder Sitzungskennung**: der
  Nachbar muss nicht wissen, wer spielt. Verlassen wird der Vertrag ohne Neubau über
  `CHRONICLE_GPU_LEASE`.
  **Seit #321 gilt das Fenster auf beiden Modellwegen.** Auf dem `/v1`-Weg nennt die
  Nutzlast ein **Profil** (`foundry`) statt eines Modellnamens — `llama-server` ignoriert
  den Namen der Anfrage, und der Nachbar schaltet am Profil, welches Modell er geladen
  hält; die Zusage »keine Runden-, Gilden- oder Sitzungskennung« bleibt damit wörtlich
  erfüllt, denn ein Profil benennt die Arbeit und nicht die Runde. Die Paarung »eine
  Konstante, zwei Verwendungen« galt dabei nur für den Ollama-Weg und ist mit ihm gefallen:
  der Ablöser kennt kein `keep_alive`, hier trägt allein die Frist des Fensters. Antwortet
  der Nachbar mit
  `202 preparing`, wird **nicht** erneut angemeldet, sondern gefragt — frühestens nach der
  von ihm genannten Wartezeit, mit Obergrenze, und die Frist beginnt erst mit `ready`.
  Ohne Fenster wäre der Leerlauf auf diesem Weg nicht »wir warten auf einen Vertrag«,
  sondern »still schreibt das Haushaltsmodell«.
  **Ein Zeittakt wäre die falsche Grenze gewesen.** Erwogen und verworfen wurde ein
  Fenster alle zehn Minuten: es endet mitten im Satz, und ein Modell, das über eine
  unfertige Szene schreibt, erfindet den Abschluss — genau der Fehler, der hier der
  teuerste ist. Ein Szenenschnitt ist von einem Menschen erklärt.
  **Und der Zwischenstand ist Deutung, nie Beleg.** Er wird als solcher ausgewiesen und
  fließt nicht als Fakt in die Endchronik zurück.
- **Discord ist die Oberfläche** (#62, seit 2026-08-06). Gespielt wird dort, also wird
  dort auch bedient: Erfassen, Auslösen, Einrichten und Bestätigen laufen über Befehle,
  Modals und Knöpfe; Ausgaben kommen als Embed (kurz) oder als Markdown-Datei (lang).
  **Die Weboberfläche ist abgeschaltet — vollständig** (#231, Abschluss der Kette aus
  #227). Es gibt **keine** Seite mehr, auch keine Betreiber-Seite: ein Prozess, ein
  Container, und der ist der Bot. Über HTTP antwortet allein `/healthz` auf der Schleife,
  das Install-Gate der Box (#228). Wer eine neue Fähigkeit baut, baut sie **in Discord** —
  eine Seite, in der sie sonst landen könnte, gibt es nicht mehr.

  Die Entscheidungen, die dahin geführt haben, bleiben lesbar, weil sie erklären, warum es
  am Ende keine Seite braucht:
  - **2026-08-10, #69:** die Oberfläche fällt bis auf eine kleine Betreiber-Seite. Was die
    spielende Gruppe betrifft, gehört nach Discord; was der **Betreiber** einstellt, kann
    dort nicht hin, weil es keiner Gilde gehört: Bot-Token, Ollama-Adresse und -Modell
    (#87) und wer die Seite selbst verwalten darf.
  - **#157:** die Seite trägt keine Spielinhalte mehr; übrig bleiben die Instanz-Werte.
  - **#230:** »keiner Gilde« begründet einen Ort *außerhalb von Discord*, aber keine
    *Seite*. Bot-Token, Ollama-Adresse und -Modell kommen aus den **Template-Variablen der
    Box**. Was die Seite dabei verlor — die Auswahlliste der installierten Ollama-Modelle
    — ist bewusst in Kauf genommen.
  - **#231:** damit stand auf der Seite nur noch die Verwaltungsgruppe, und die war die
    Antwort auf die Frage, wer an *diese Seite* darf. Eine Seite, deren einziger Inhalt
    die Erlaubnis ist, sie zu betreten, ist keine Seite, sondern ein Türsteher vor einem
    leeren Raum. Wer an den Bot-Token darf, entscheidet jetzt, wer die Template-Variablen
    dieses Dienstes bearbeiten darf — also ServiceBay, wo die Frage ohnehin hingehört
    (#90 bleibt davon unberührt: über einer *Runde* steht weiterhin niemand).
- **Der Bot führt, statt auf Befehle zu warten** (Betreiber-Entscheidung 2026-08-23,
  #265). Das schärft die Entscheidung darüber, *wo* bedient wird, zu einer darüber, *wie*:
  kann der Bot einen Moment selbst erkennen und von sich aus anbieten — ein Knopf im
  Thread, eine Rückfrage, ein Hinweis zur richtigen Zeit —, ist das besser als ein Befehl.
  Gebraucht wird ein **Führer durch den Abend**, kein passiver Zuhörer. Ein Slash-Befehl
  ist die **Rückfallebene**, nicht der Normalfall: jeder ist etwas, das jemand kennen,
  erinnern und richtig eintippen muss. Der Anlass war der erste echte Spielabend am
  2026-08-18 — schiefgegangen ist nie ein einzelner Befehl, sondern die Reihenfolge und
  das Nichtwissen, welche es überhaupt gibt; der Betreiber konnte mehrere der siebzehn
  selbst nicht erklären, und was der Erbauer nicht erklären kann, findet eine spielende
  Gruppe nicht. **Folge für den PR-Alltag: kommt ein neuer Slash-Befehl dazu, steht im PR
  ein Satz, warum der Bot den Moment nicht selbst erkennen kann.**
- **Eine Instanz trägt mehrere Runden** (#62/#63). Eine Runde ist eine Discord-Gilde
  mit eigenem Foundry-Zugang. Das löst »eine Instanz pro Gruppe« aus Epic #1 ab.

Die alten Entscheidungen bleiben lesbar, wo sie abgelöst wurden — die Begründung von
damals erklärt, warum die neue anders ausfällt.

Wer davon abweichen muss, ändert das Epic per PR und verlinkt ihn. Eine Abweichung ohne
Doku-PR gilt als nicht gemeldet.

## Erfinden ist der teuerste Fehler

Das Sprachmodell **ordnet und verknüpft**. Es rechnet nicht, und es füllt keine Lücken.
Bei dünner Eingabe wird geordnet, nicht ausgeschmückt — ein Protokoll, dem man nicht
ansieht, welcher Satz erfunden war, ist schlimmer als eine Lücke, weil es Wochen später
als Gedächtnisstütze gelesen wird und niemand mehr nachprüft.

Konkret: **keine Zahl im Protokoll, die nicht im Foundry-Chat-Log steht.** Wer Prosa
erzeugt, trennt sichtbar zwischen Belegtem und Verbindungssätzen.

Dasselbe gilt für den Herkunftsvermerk im Kopf: **er kommt aus der Antwort, nie aus der
Einstellung** (#320). Auf dem `/v1`-Weg ignoriert der Server den angefragten Modellnamen,
und wer die Einstellung hineinschreibt, behauptet etwas über einen fremden Prozess. Trägt
die Antwort keinen verwertbaren Namen, entfällt er — eine Chronik ohne Herkunftsangabe ist
ehrlich, eine mit falscher nicht.

## Commits

- **Conventional Commits**: `type(scope): subject` — `feat`/`fix`/`refactor`/`chore`/
  `docs`/`test`/`revert`. Scope spiegelt den Pfad: `feat(ui):`, `fix(foundry):`,
  `feat(discord):`, `docs:`.
- **Eine Rücknahme heißt `revert`, nicht `fix`.** release-please führt sie in einem
  eigenen Changelog-Abschnitt »Reverts« und hebt dabei den Patch-Stand; als `fix`
  geschrieben stünde sie unter »Bug Fixes« — nicht falsch, aber unehrlich, weil das
  Changelog Wochen später als Gedächtnisstütze gelesen wird. Gits eigener Vorspann
  `Revert "…"` bleibt daneben zulässig und wird ungeprüft übersprungen.
- **Keine Klammern im Subject** außer dem konventionellen `(scope)`. Ein verirrtes
  `(...)` lässt Release-Werkzeuge grün laufen und trotzdem kein Release schneiden.
- Beides prüft `scripts/check_commit_subjects.py` — im `commit-msg`-Hook und in CI.
  Nach dem Klonen einmal `pre-commit install` ausführen, sonst greift der Hook nicht.
- **Was die Box erreicht, muss unter einer Art stehen, die eine Version schneidet**
  (#343, Betreiber-Entscheidung 2026-09-06). Von den sieben Arten schneiden nur `feat`,
  `fix` und `revert` (sowie jedes `!`) ein Release; `refactor`, `chore`, `docs` und `test`
  nicht. Ein Commit unter einer dieser vier, der `src/**`, `templates/**`,
  `pyproject.toml`, `Dockerfile`, `.dockerignore` oder `scripts/verify_e2e.py` anfasst,
  landet nie im Abbild und damit nie auf dem Server — und nichts meldete das, bis
  `scripts/check_release_reach.py` es im CI-Job `commits` laut scheitern lässt. Geprüft
  wird nach **Wirkung**, nicht nach Benennung: mehr Arten Versionen auslösen zu lassen
  wäre die verworfene Alternative, weil sie Versionen ohne Änderung für die Runde erzeugt.
  Der Notausstieg ist der Trailer **`Ohne-Auslieferung: <Grund>`** im Commit-Rumpf — für
  den wirklich verhaltensneutralen Fall, etwa einen Docstring in `src/`. Er wird bei jedem
  Lauf samt Grund gedruckt, auch bei grünem Ergebnis; ohne Grundtext zählt er nicht.
  **Die eine Stelle, an der dieser Wächter strenger ist als die Regel oben:** Gits
  Vorspann `Revert "…"` wird hier **nicht** übersprungen. Er trägt keine
  Conventional-Art, schneidet also kein Release — eine Rücknahme, die den Server nicht
  erreicht, ist genau der Fehler, den der Wächter sucht. Wer eine ausgelieferte Datei
  zurücknimmt, schreibt `revert(scope): …`.

## Releases

- Automatisiert über **release-please**: es pflegt einen Release-PR, der Version und
  `CHANGELOG.md` aus den Commits ableitet. **Diesen PR zu mergen** schneidet das Release.
- **Niemals** von Hand Versionen bumpen oder Tags setzen — das tut release-please, und
  daran vorbei zu arbeiten macht das Changelog unehrlich.
- **Wer den Release-PR mergen darf, entscheidet die Umkehrbarkeit** (Betreiber-Entscheidung
  2026-08-30). Ist alles darin zurückrollbar, schneidet die Schleife das Release selbst.
  Ist es das nicht — eine Wanderung, die Daten umschreibt, eine einwilligungsrelevante
  Änderung, alles, was sich nach dem Ausrollen nicht mehr zurücknehmen lässt —, entscheidet
  ein Mensch.
  **Abgelöst wird damit »ein Release zu schneiden ist eine menschliche Entscheidung«**, und
  die Begründung von damals war falsch: sie lautete, auf der Box lägen Stimmaufnahmen
  fremder Menschen, also sei jedes Release heikel. Das macht eine Eigenschaft des *Repos*
  zur Eigenschaft *jeder Änderung darin*. §201 regelt das Aufzeichnen, nicht das Ausliefern
  eines Bugfixes; eine Kappungsregel für einen Discord-Embed ändert nichts daran, was
  aufgenommen oder eingewilligt wird.
  Das Ärgerliche daran: **dieses Haus hatte die richtige Achse bereits** — der Draft-Gate
  weiter unten trennt seit #212 ausdrücklich danach, *was eine Änderung tut, nicht in welcher
  Datei sie steht*. Gelernt für das eine Tor, nicht übertragen auf das andere. Im Abgleich
  mit `mdopp/solaris-android` und `mdopp/servicebay` ist dieselbe Verwechslung in drei
  Häusern unabhängig aufgetreten, jedes Mal mit der richtigen Achse anderswo im eigenen
  Regelwerk. Die Prüffrage dagegen: **trennt dieses Tor nach Ort oder nach Wirkung?**
  Die beiden Tore hier tragen deshalb **zwei verschiedene** Wirkungsachsen: der Draft-Gate
  fragt nach *Preisgabe*, der Release-Gate nach *Umkehrbarkeit*.
- Eingerichtet in `release-please-config.json` mit `release-type: python` gegen
  `pyproject.toml`; der Workflow läuft bei jedem Push nach `main`.
- **`main` bleibt linear: PRs werden rebase-gemergt, nie mit Merge-Commit** (#70).
  `Merge pull request #N from …` ist kein Conventional Commit; release-please nimmt
  dafür ersatzweise den **PR-Titel** und schreibt dieselbe Änderung damit **zweimal**
  ins Changelog — einmal aus dem Merge-Commit, einmal aus dem echten Commit dahinter.
  Rebase statt Squash, weil ein Batch je Issue schon einen fertigen Conventional Commit
  trägt und Squash acht Einträge zu einem zusammenfalten würde. Ein rein lokales
  `Merge branch '…'` ohne PR ist unschädlich — ohne PR gibt es keinen Ersatztitel, und
  release-please überspringt den Commit still.
- **Voraussetzung auf GitHub-Seite:** „Allow GitHub Actions to create and approve pull
  requests" muss in den Repo-Einstellungen an sein. Ist sie aus, legt der Lauf den
  Release-Branch zwar an und scheitert erst am PR mit `GitHub Actions is not permitted
  to create or approve pull requests`. Das ist eine Operator-Einstellung, keine Datei
  im Repo.

## Tests

- Neuer und geänderter Code trägt Tests. **Diff-Coverage-Untergrenze 70 %** auf den
  geänderten Zeilen.
- Jedes Abnahmekriterium bekommt möglichst einen Test, damit es nicht still zurückfällt.
- Gemessen wird mit `pytest -q --cov --cov-report=xml`, geprüft von
  `scripts/check_diff_coverage.py` gegen den PR-Base — im `test`-Job der CI.

## Geheimnisse

Im Repo stehen **keine echten Geheimnisse** — keine Tokens, Passwörter, Schlüssel, auch
nicht in Tests, Fixtures oder Doku. Die Foundry-Zugangsdaten und der Discord-Bot-Token
werden zur Laufzeit gesetzt; Platzhalter sind in Ordnung, konkrete Werte nicht. Das
gilt auch für die **Foundry-Adresse** — die gehört in die Konfiguration, nicht ins Repo.

**Das Foundry-Passwort wird nirgends gespeichert (#64).** Es gibt kein Feld dafür, keine
Umgebungsvariable und keine Zeile in der SQLite; die Wanderung löscht einen Bestand aus
der Zeit davor. Hashen scheidet aus — wir müssen es Foundry **vorzeigen**, nicht prüfen —,
also gilt die härtere Regel: gar nicht erst vorhalten. Es lebt in `chronicle.zugang` im
Arbeitsspeicher, wird beim Abgleich erfragt und von ihm verbraucht, auch vom gescheiterten;
was liegen bleibt, verfällt nach zwölf Stunden. Ergebnis: eine Instanz mit mehreren Runden
speichert **kein fremdes Geheimnis**. Das löste die Operator-Entscheidung vom 2026-08-06
zuerst für das Passwort ab; für den Bot-Token galt sie weiter — bis #230, siehe unten.

**Der Bot-Token liegt nicht mehr in der SQLite (#230, seit 2026-08-18).** Er kommt —
zusammen mit Ollama-Adresse und -Modell — aus den **Template-Variablen der Box**
(`DISCORD_BOT_TOKEN`, `OLLAMA_URL`, `OLLAMA_MODEL`), wird beim Start gelesen und nirgends
gespeichert. Es gibt kein Feld dafür, keinen Schreibweg und keine Zeile in der Datei; die
Wanderung räumt einen Bestand aus der Zeit davor fort. Damit gilt für ihn dieselbe harte
Regel wie seit #64 für das Foundry-Passwort: **gar nicht erst vorhalten.** Eine Instanz
speichert danach **kein Geheimnis mehr** — weder ein fremdes noch ihr eigenes —, und die
SQLite geht ohne eines ins Backup.

Damit sind die drei Bedingungen der abgelösten Entscheidung **gegenstandslos, nicht
gelockert**: sie schützten einen Wert, den es dort nicht mehr gibt. Geblieben ist die
Zusage, die auch ohne sie trägt — **kein Token in einer Logzeile**. Seit #231 gibt es
daneben keinen zweiten Prozess mehr, der ihn nicht bekommen dürfte: es läuft nur noch der
Bot, und ob der Token steht, sagt er, indem er läuft oder nicht.

**Die Wanderung löscht ihn nur gegen Ersatz.** Auf einer laufenden Instanz ist die Zeile
in `meta` die einzige Kopie des echten Tokens; gelöscht, während die Variable leer ist,
wäre er unwiederbringlich, und bis zum nächsten Gang ins Discord-Portal schwiege der Bot
in einer echten Gilde. Deshalb: steht die Variable, wird gelöscht; steht sie nicht, bleibt
der Wert liegen und `chronicle.db` sagt es bei jedem Start — mit den Namen, nie mit dem
Wert (`db._abgeloeste_werte_verwerfen`, `tests/test_db.py`).

> **Abgelöst: Operator-Entscheidung 2026-08-06 — der Bot-Token liegt in der SQLite.**
> Die Werte für Discord und Ollama wurden auf der Betreiber-Seite gepflegt (#25, der
> Bot-Token seit #19), die für Foundry seit #157 in Discord unter `/setup`. Ein gepflegter
> Wert schlug die Umgebung, die als Vorgabe beim ersten Start blieb. Damit lag der
> Bot-Token **im Klartext in `chronicle.sqlite3` — und die geht ins Backup.** Bewusst so
> entschieden: eine Homelab-Instanz, Backup auf eigenem NAS, und der Betrieb wäre sonst
> nur über ServiceBay-Template-Variablen zu ändern. Er ist außerdem **unser** Token und
> nicht der einer fremden Gegenstelle. Die Abwägung hing an drei Bedingungen, die für
> jedes Geheimnis in `settings.SECRET_KEYS` galten: die Seite **stand** hinter Authelia und
> dem `Remote-User`-Guard (seit #190 wörtlich: geglaubt wurde die Kopfzeile nur von einer
> Adresse dieser Maschine, `chronicle.herkunft` — davor konnte ein Unangemeldeter im LAN
> den Token überschreiben); der Wert wurde nirgends angezeigt, sondern nur *ob* er gesetzt
> war; übertragen wurde nur per POST, nie in einer URL, und ein leeres Feld hieß
> unverändert. Alle drei sind mit der Seite gefallen und stehen hier im Imperfekt, weil
> keine Zeile dieser Datei eine Seite behaupten soll, die es nicht gibt.
>
> **Warum sie fällt:** der harte Einwand gegen Template-Variablen war #33 — der
> Installations-Assistent würfelte für `type: secret` einen Zufallswert, der sich als
> Bot-Token bei Discord anmeldete und in einer Neustart-Schleife mit 401 hing. Das
> ServiceBay-Rezept *rotate-a-service-secret* beantwortet ihn: ein bei `install_template`
> **übergebener** Wert gewinnt über den gespeicherten und wird nicht gewürfelt. Die
> Variable ist trotzdem `type: text` und nicht `secret` — für einen Wert, den nur die
> Gegenstelle kennt, ist ein Zufallswert kein Platzhalter, sondern ein falscher Wert, und
> leer heißt ehrlich »nicht gesetzt«. Und die Begründung von damals — »der Betrieb wäre
> sonst nur über Template-Variablen zu ändern« — war eine Bequemlichkeit; sie wog ein
> Klartext-Geheimnis im Backup nicht auf. Der Umbau kostet die Auswahlliste der
> installierten Ollama-Modelle: der einzige Punkt, an dem die Seite etwas konnte, was eine
> Variable nicht kann (#227).

Fixtures aus einem echten Foundry-Dump sind **personenbezogen**: der Weltabzug enthält
die Klarnamen aller Beteiligten. Vor jedem Einchecken anonymisieren.

Und: **kein Token in einer Logzeile.** Das ist hier der wahrscheinlichste Weg, wie einer
doch nach draußen gelangt.

## Aufnahmen sind personenbezogen

Das System verarbeitet Stimmen und Namen echter Personen. Das Aufzeichnen des
nichtöffentlich gesprochenen Wortes ohne Einwilligung ist strafbar (§201 StGB) — der
Bot macht deshalb eine hörbare Ansage und protokolliert die Zustimmung. Wer an Aufnahme,
Einwilligung, Zugangsdaten oder der Personen-Zuordnung arbeitet, öffnet einen **Draft-PR
und wartet auf menschliche Review**, statt automatisch zu mergen.

**Wo die Grenze verläuft** (Operator-Entscheidung 2026-08-14, nach #212). Maßgeblich ist,
was eine Änderung **tut**, nicht in welcher Datei sie steht:

- **Draft**, wenn sie ändert, **was** aufgenommen, eingewilligt, gespeichert oder
  zugeordnet wird — wer erfasst wird, worauf sich die Zustimmung bezieht, wie lange etwas
  liegt, wem eine Stimme zugeschrieben wird, wer etwas löschen oder fortnehmen darf.
- **Kein Draft**, wenn sie an denselben Stellen die Preisgabe nur **verringert** oder
  nichts an ihr ändert: einen Namen aus einer Logzeile nehmen, einen Text umformulieren,
  einen Test nachziehen, einen Kommentar schreiben.

Der Grund für die Schärfung: nach dem Dateikriterium ist derselbe Handgriff dreimal
verschieden ausgefallen — #164 wurde als Draft nachgereicht (#168), #194, #206 und #212
liefen durch. Alle vier nahmen einen Namen aus einem Log. Eine Regel, die bei gleicher
Arbeit verschieden entscheidet, schützt niemanden; sie erzeugt nur Zeremonie an
zufälligen Stellen. Wer unsicher ist, nimmt den Draft — die Frage »verringert das die
Preisgabe oder nicht« lässt sich in einem Satz beantworten, und im Zweifel ist die
Antwort nein.

Audiospuren sind nach erfolgreichem Lauf löschbar und gehören **nicht** ins Backup. Die
SQLite-Datei ist klein und enthält alles Unersetzliche.

## Umfangsdisziplin

- Kleinste Änderung, die die Aufgabe löst. Ein Fehler braucht kein Aufräumen drumherum.
- Drei ähnliche Zeilen schlagen eine verfrühte Abstraktion. Keine spekulative
  Fehlerbehandlung für Fälle, die nicht eintreten können.
- **Die Trennung zwischen Runden ist die wichtigste Sicherheitseigenschaft** (#62/#63 —
  sie ersetzt »eine Instanz pro Gruppe«). Jede runden-eigene Tabelle trägt `runde_id`,
  jeder Zugriff läuft über `db.scoped(runde)`, rohes SQL daneben gibt es nicht, und
  `tests/test_isolation.py` ist das Dauergate: eine neue Funktion ohne Runden-Argument
  fällt durch. Ein vergessenes `WHERE runde_id = ?` ist kein Fehler, sondern ein Leck
  zwischen fremden Kampagnen.
  **Innerhalb** einer Instanz trennte die Weboberfläche zwei Rollen, Mitspielen und
  Verwalten (#51, `chronicle.roles`) — für das *Spielen* abgelöst mit #62: wer was darf,
  entscheidet Discord über seine eigenen Kanal- und Rollenrechte. Kein eigenes
  Rollenmodell mehr für Spielinhalte. **`chronicle.roles` ist mit #231 gefallen** (und
  damit doch, was die Fassung vor #157 schon für #69 erwartet hatte). Der Nachtrag #157
  hatte es behalten, weil `ist_verwalter` und der `Remote-Groups`-Header die Antwort auf
  »wer an die Betreiber-Seite darf« trugen — eine Frage des Betriebs, keine des Spiels,
  und in keiner Gilde beheimatet. Die Seite gibt es nicht mehr, also gibt es die Frage
  nicht mehr: an den Bot-Token kommt, wer die Template-Variablen dieses Dienstes
  bearbeiten darf, und darüber wacht ServiceBay. Mit `roles` sind `chronicle.instanz`,
  die gespeicherte `admin_group` und `chronicle.herkunft` gefallen; die Datei räumt den
  alten Wert beim nächsten Start fort (`db.VERWORFENE_SCHLUESSEL`). **Kein eigenes
  Rollenmodell mehr, Punkt.**
- **Über der Runde steht niemand — der Betreiber löscht keine fremde Runde**
  (Operator-Entscheidung 2026-08-11, #90). Eine Runde verschwindet auf genau zwei Wegen:
  die **Gruppe selbst** löscht sie (`/chronik loeschen`, Administrator-Recht in ihrer
  Gilde), oder die **Frist** läuft ab — dreißig Tage nach dem Rauswurf. Eine Instanz-Ebene
  daneben gibt es nicht, und sie wird auch nicht nachgerüstet: der Bot sagt jeder Gruppe
  im ersten Satz, dass der Betreiber der Box alles **lesen** kann; ein Löschknopf für ihn
  machte daraus die zweite Zusage, dass er ihre Chronik auch **fortnehmen** kann, ohne
  dass sie es merkt. Wer sie nicht braucht, soll sie nicht bekommen — und wer sie nicht
  hat, kann sie nicht missbrauchen. An SQLite und Dateisystem steht dem Betreiber ohnehin
  alles offen; der Unterschied ist, dass es dafür **kein Bedienelement** gibt: keine
  Route, keinen Befehl, keinen Knopf. Wer so etwas tut, tut es an der Datenbank und weiß,
  dass er die Regel verlässt. `tests/test_lebenszyklus.py` hält den Zustand fest — ein
  neuer Aufrufer von `lebenszyklus.loeschen`/`sperren` fällt durch.

## Kommentare

Standardmäßig keine. Einen nur für ein nicht-offensichtliches **Warum** — eine versteckte
Bedingung, ein Workaround, eine überraschende Invariante. Nicht erzählen, *was* der Code
tut.

## Skripte statt Prosa

Deterministische, wiederholbare Schritte gehören in ein eingechecktes Skript, nicht in
Prosa, die ein Agent bei jedem Lauf neu auslegt. Menschliches Urteil ist für *was* zu
prüfen ist und *warum* etwas fehlschlug — die Mechanik macht ein Skript.

## Der Zeiger auf die Standards ist erzeugt, nicht abgeschrieben

Der folgende Block steht **wörtlich und auf Englisch** hier, weil er erzeugt ist: er kommt
aus `get_service_standards(flavor="servicebay")` → `repoBootstrap.claudeMdBlock` und wird
drüben mit `npm run standards:bootstrap -- --flavor servicebay --check <repo>` gegen
Abweichung geprüft. Übernommen statt nachgeschrieben, damit er nicht wieder altert.

Denn genau das war er: unsere eigene Fassung sagte »`get_service_standards` abrufen« **ohne
Varianten-Angabe** (#313). Wer ihr folgte, bekam die servicebay-Variante — und ging damit an
den gemeinsamen Arbeitsverabredungen der Sitzungen vorbei, die an der **generischen**
Variante hängen, im Block `workingAgreements`. Eine Anweisung, der man wörtlich folgen und
die Hälfte trotzdem verfehlen kann, ist die unangenehmste Sorte: sie sieht befolgt aus.
Schritt 2 unten holt sie nach.

Die **generische** Fassung desselben Blocks passt uns nicht: sie beginnt mit »This project
does not run on a ServiceBay box«, und das ist für uns falsch (#12). Wir nehmen die
servicebay-Fassung — die zeigt von sich aus auf beide Varianten.

<!-- BEGIN SERVICEBAY STANDARDS POINTER (generated — do not edit by hand) -->

## Standards: fetch them, never re-derive them

This repo is built for a ServiceBay box, so **ServiceBay's standards catalog is
the binding source of its architecture decisions** — this file only points at it.

1. **Before the first stack, CI, storage, or auth decision**, call the ServiceBay
   MCP tool `get_service_standards(flavor="servicebay")` and fetch every id it
   lists under `assistsToRead` via `get_assist(id)`. Read first, design second —
   a stack chosen before reading is a stack chosen against the ADRs by accident.
2. **Then call `get_service_standards(flavor="generic")` and read every id under
   `workingAgreements`.** They are the cross-repo agreements on how work enters,
   how it is gated, when to ask the operator, and how sessions hand over — they
   are platform-agnostic, so they hang off the *generic* flavor and the
   servicebay index does NOT repeat them. Fetching only one flavor is how a repo
   follows this file exactly and still never hears about them.
   Start with `get_assist("footgun-importing-a-working-agreement-from-another-repo")`:
   the questions and mechanisms port between repos, the thresholds and autonomy
   levels do not.
3. **If the ServiceBay MCP is not connected in this session, stop and say so.**
   An unconnected session cannot see the ADRs, so anything it decides about auth,
   health, storage, or CI is a guess. Connecting it is the first task, not an
   optional extra.
4. **The catalog wins.** Where this file and the catalog disagree, this file is
   the stale one — fix it here, not in your head. The catalog is read from the
   box at runtime, so it can be newer than any release you are running.
5. **Report gaps back.** A missing, ambiguous, or wrong standard is itself a
   finding: file a `standards-gap` issue on `mdopp/servicebay` and propose the
   assist/docs fix. See `get_assist("report-standards-gaps")`.

This block is generated. Regenerate or verify it from a `mdopp/servicebay`
checkout: `npm run standards:bootstrap -- --flavor servicebay --write <repo>` /
`-- --flavor servicebay --check <repo>`.

<!-- END SERVICEBAY STANDARDS POINTER -->

## ServiceBay ist die Zielplattform — ihre ADRs binden uns

Deployt wird auf eine ServiceBay-Box (#12). Deren ADRs und Bau-Standards liegen
**nicht** in diesem Repo, sondern auf dem ServiceBay-MCP (Server `servicebay` in der
lokalen Claude-Konfiguration; Adresse und Token gehören nicht ins Repo). Abgerufen werden
sie über den Zeiger oben, und zwar in **beiden** Varianten — die servicebay-Variante trägt
die ADRs, die generische die Arbeitsverabredungen. Dieser Abschnitt ist der Extrakt, nicht
der Ersatz: er sagt, was von den Standards diesen Dienst bindet und **warum** — die
Volltexte stehen im Katalog.

- **ADR 0001 — SSO: bindet diesen Dienst seit #231 nicht mehr.** Der ADR gilt für
  *user-facing* Dienste, und dieser ist keiner: keine Seite, keine Subdomain, kein
  veröffentlichter Port. Was über HTTP antwortet, ist `/healthz` auf `127.0.0.1` — ein
  Poller-Endpunkt ohne Inhalt und ohne Kopfzeilenprüfung. Ein Login zu bauen, wo niemand
  hinkommt, wäre Zeremonie.
  Die Fassung davor gilt weiter für die **Geschichte**, und sie erklärt, warum das kein
  Nachlassen ist: solange es die Betreiber-Seite gab, stand sie hinter
  Authelia-Forward-Auth und erzwang `Remote-User` — **und glaubte ihn nur, wenn der Aufruf
  von dieser Maschine kam** (#190). Die Kopfzeile allein war kein Beleg: sie schreibt sich
  jeder selbst, der den offenen Port erreicht. »Kein LAN-Bypass« hieß bis dahin nur »ohne
  Kopfzeile kein Zugang«, und der Test dazu prüfte auch nur das. Der Weg von damals ist
  jetzt geschlossen, weil es das Ziel nicht mehr gibt, nicht weil der Türsteher besser
  wurde. **Wer je wieder etwas user-facing baut, baut ADR 0001 mit — dann gilt er
  sofort.** „Keine Zugriffskontrolle" weiter unten meint Mandantentrennung zwischen
  Gruppen; die liefert Discord (#62) und `db.scoped` (#63), und über einer Runde steht
  weiterhin niemand (#90).
- **`/healthz` → 200** ist Test-Seam und Install-Gate der Box.
- **SQLite läuft im WAL-Modus** — Plattform-Lektion gegen „database is locked".
- **CI gatet den Image-Publish auf grüne Tests** (`needs: test`); eine CI, die nur
  baut, ist non-compliant. Kommt mit #12, ebenso pinned Tags statt `:latest`.
- **UI folgt dem ServiceBay-Design-Standard** (`get_assist service-ui-design-standard`) —
  **gegenstandslos seit #231:** es gibt keine gerenderte Seite mehr. Was davon trotzdem
  trägt, ist `get_assist service-ui-user-language`, und zwar für **Discord**: ein Satz an
  eine Gruppe nennt keinen Umgebungsvariablen- oder Header-Namen und sagt, was als
  Nächstes zu tun ist — »trag das mit `/setup` ein« statt »FOUNDRY_URL fehlt«.
  (Zwischenstand #62/#69: übrig blieb die Betreiber-Seite, und für die galt der
  Design-Standard weiter. Die Fassung davor sagte, ADR 0001 werde gegenstandslos; das
  galt für eine Instanz ganz ohne Seite — mit #231 ist genau das eingetreten.)
- **Läufe über ~10 s sind server-eigene, beobachtbare Jobs** — abbrechbar, neustartfest,
  Wiederanbindung über Job-Id (`get_assist long-running-process`).
- **Erledigte Abweichung: Flask + Jinja statt des empfohlenen FastAPI — beendet mit
  #231.** Es gibt kein Webrahmenwerk mehr im Abhängigkeitsbaum des Dienstes: kein Flask,
  kein Jinja2, kein waitress. Der Weg dahin war nicht der erwartete — die Abweichung
  wurde nicht *aufgelöst*, ihr **Gegenstand** ist verschwunden. Übrig ist `/healthz`, und
  das sind zehn Zeilen `http.server` aus der Standardbibliothek
  (`chronicle.bot.healthz`, #228). Die Frage FastAPI-oder-Flask stellt sich nicht mehr,
  weil dieser Dienst nichts mehr ausliefert.
  `markupsafe` bleibt als **direkte** Abhängigkeit — `chronicle.search` hebt damit
  Treffer hervor; es steht dort für sich und nicht als Anhängsel von Jinja. Flask steht
  weiterhin im `dev`-Extra: `tests/mocks` stellt Foundry und Ollama als **echte**
  WSGI-Server auf einen Wegwerf-Port, weil ein Funktions-Fake nur beweist, dass der Code
  sich selbst aufruft. Das sind Attrappen fremder Gegenstellen, nicht unsere Oberfläche,
  und ins Image kommen sie nicht.
  (Die Fassungen davor nannten die Abweichung erst »auslaufend«, dann »ausgelaufen und
  trotzdem stehengeblieben«, weil ein Umbau auf FastAPI für ein Formular und zwei
  Weiterleitungen Arbeit ohne Gegenwert gewesen wäre. Das stimmte — und war zugleich das
  Anzeichen, dass die Seite selbst der überflüssige Teil war.)
- **Erklärte Abweichung, bleibend — und seit #231 fast kostenlos:** der Pod läuft mit
  `hostNetwork: true` statt im eigenen Netz-Namensraum, den **ADR 0007** verlangt (#165).
  Der Grund steht im Template: der Dienst spricht die Nachbarn dieser Box über die
  Schleife an — `llama-server` auf `127.0.0.1:11435` schreibt die Chronik, `solaris-tts` auf
  `127.0.0.1:8881` spricht die Ansage, `solaris-whisper-batch` auf `127.0.0.1:10301`
  verschriftet die Spuren. Alle drei binden **nur** Loopback; aus einer isolierten netns
  wären sie unerreichbar, auch über `host.containers.internal` — das führt an das Gateway
  der Box, nicht an ihre Schleife.
  **Was sie kostete, ist mit #231 nicht mehr gedeckt, sondern weg.** Host-Netz war der
  Grund, warum der Port der Betreiber-Seite auf `0.0.0.0` im ganzen LAN stand, und das
  war ausnutzbar: ein selbst erfundener `Remote-User` genügte, um an den Bot-Token zu
  kommen (#190). Bezahlt wurde das seither von `chronicle.herkunft` — geglaubt wurde die
  Kopfzeile nur von einer Adresse dieser Maschine. Jetzt gibt es die Seite nicht mehr und
  damit keinen Port im LAN: der einzige Horcher dieses Pods ist `/healthz` auf
  `127.0.0.1`. Es bleibt nichts, was das Host-Netz freilegen könnte. **Genau deshalb ist
  `chronicle.herkunft` mit #231 gefallen** — eine Prüfung ohne Prüfling ist keine
  Vorsicht, sondern eine falsche Zusage; `CHRONICLE_TRUSTED_PROXIES` und
  `CHRONICLE_REQUIRE_REMOTE_USER` sind mit ihr fort.
  **Die Abweichung fiele, sobald die drei Nachbarn auch aus einem eigenen Netz-Namensraum
  erreichbar sind.** Das liegt in fremden Vorlagen und ist deshalb eine Bedingung, kein
  Versprechen. Gefragt wurde: ADR 0007 sieht benannte Ausnahmen vor,
  `mdopp/servicebay#2518` hat sie verneint — die Liste bleibt geschlossen. **Und angefasst
  wird hier nichts ohne Box-Verify:** eine Netzänderung legt diesen Dienst still, wenn sie
  falsch ist, und er hängt an einer echten Discord-Gilde. `tests/test_template.py` hält
  `hostNetwork: true` samt dieser Begründung fest, damit es niemand versehentlich
  entfernt.
- **Standards-Lücken werden zurückgemeldet:** `standards-gap`-Issue in
  `mdopp/servicebay` (`get_assist report-standards-gaps`).

## Foundry ist eine harte Abhängigkeit

- Nur **flach andocken**: Chat-Log, Aktoren, Kampfzustand.
- **Regelwerks-Interna enden am Adapter.** Sie ganz zu vermeiden geht nicht — die Zahlen
  eines Wurfs stehen in `message.system.roll`, und dessen Form bestimmt das System
  (Daggerheart legt dort `hope`/`fear` ab, 5e und PF2e etwas anderes). Also ein dünner
  Adapter je System auf ein gemeinsames Modell; dahinter weiß nichts mehr, was gespielt
  wurde.
- **Der Server filtert nicht.** Foundry schickt jedem angemeldeten Benutzer die
  komplette Welt, GM-Inhalte und Klarnamen inklusive. Die Berechtigungsfilterung ist
  unsere Aufgabe und gehört **vor** den Speicher, nicht vor die Anzeige.
- **Zwischenspeichern.** Foundry ist zwischen den Sitzungen oft aus; ein Protokoll, das
  dann nicht angezeigt wird, ist kaputt.
- Bei Nichterreichbarkeit eine **verständliche Meldung**, keine leere Liste ohne Erklärung.
- Der Handschlag ist aus dem Client nachgebaut, kein dokumentiertes API — siehe
  `docs/foundry-zugriff.md`. Ein Foundry-Hauptversionssprung kann ihn brechen.

## Niemals

- `--no-verify` / Hooks überspringen — den Fehler beheben statt ihn umgehen.
- Die Lint-Basis oder eine CI-Prüfung lockern, damit etwas durchgeht.

## Issues

Symptom + Repro + Startpunkt-Dateien — kein Fix-Plan. Der Fix wird im PR entschieden.
**Ausnahme:** die Aufbau-Issues aus Epic #1 beschreiben ein *Ergebnis*, weil es bei
einem leeren Repo weder Symptome noch Startpunkte gibt.

**Und die schärfere Grenze daneben: Befund gegen Vermutung** (2026-08-30, aus dem Abgleich
mit `mdopp/solaris-android`). »Kein Fix-Plan« reicht nicht, weil man ihn einhalten und
trotzdem falsch liegen kann. Ein **Befund** ist gemessen und gehört ins Ticket — er erspart
dem Bauenden echte Arbeit, der jede Stufe hier kalt startet und die Diagnose sonst neu
herleitet. Eine **Vermutung** gehört ebenfalls hinein, aber **als Vermutung gekennzeichnet**,
damit sie verworfen werden darf, ohne sich gegen das Ticket zu stellen.

Der Anlass ist #309, geschrieben am selben Tag. Dort stand unter der Überschrift
»Ursache«, dass ein `paths`-Filter die Tag-Auslösung verschluckt. Das war geraten und sah
aus wie gemessen. Der tatsächliche, davon unabhängige Grund war ein anderer: release-please
setzt den Tag mit `GITHUB_TOKEN`, und daraus startet GitHub grundsätzlich keinen Lauf. Wer
der Überschrift geglaubt hätte, hätte den Filter entfernt, einen grünen PR gemergt — und das
nächste Release hätte wieder kein Image gebaut, diesmal mit einem »ist doch behoben« davor.
Eine Hypothese im Ticket verhindert den Fehler nicht, sie verbreitet ihn schneller.

## Autoloop

`.claude/skills/autoloop-issues/` orchestriert Planner → Builder → Verify. Zustand
läuft ausschließlich über `queue.py`-Verben (GitHub-Labels + Issue-Kommentare als
Wahrheit, ein winziger gitignorierter Cache für den laufenden Durchgang). Der
Verify-Schritt prüft das deployte Artefakt auf der Box; was er nicht ausführen kann,
meldet er ehrlich als `owed` statt grün.
