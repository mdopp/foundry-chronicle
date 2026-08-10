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
- **Discord ist die Oberfläche** (#62, seit 2026-08-06). Gespielt wird dort, also wird
  dort auch bedient: Erfassen, Auslösen, Einrichten und Bestätigen laufen über Befehle,
  Modals und Knöpfe; Ausgaben kommen als Embed (kurz) oder als Markdown-Datei (lang).
  Die Weboberfläche wird abgeschaltet — **bis auf eine kleine Betreiber-Seite**
  (Operator-Entscheidung 2026-08-10, #69). Was die spielende Gruppe betrifft, gehört
  nach Discord; was der **Betreiber** einstellt, kann dort nicht hin, weil es keiner
  Gilde gehört: Bot-Token, Ollama-Adresse und -Modell (#87), und wer verwalten darf
  (#90). Alles andere fällt. Wer eine neue Fähigkeit baut, baut sie **in Discord**,
  nicht in einer Seite — die Betreiber-Seite ist kein Ort für Spielinhalte. Bis #69
  besteht die alte Oberfläche daneben weiter; das ist Übergang, kein zweiter Weg.
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

## Commits

- **Conventional Commits**: `type(scope): subject` — `feat`/`fix`/`refactor`/`chore`/
  `docs`/`test`. Scope spiegelt den Pfad: `feat(ui):`, `fix(foundry):`,
  `feat(discord):`, `docs:`.
- **Keine Klammern im Subject** außer dem konventionellen `(scope)`. Ein verirrtes
  `(...)` lässt Release-Werkzeuge grün laufen und trotzdem kein Release schneiden.
- Beides prüft `scripts/check_commit_subjects.py` — im `commit-msg`-Hook und in CI.
  Nach dem Klonen einmal `pre-commit install` ausführen, sonst greift der Hook nicht.

## Releases

- Automatisiert über **release-please**: es pflegt einen Release-PR, der Version und
  `CHANGELOG.md` aus den Commits ableitet. **Diesen PR zu mergen** schneidet das Release.
- **Niemals** von Hand Versionen bumpen oder Tags setzen. Ein Release zu schneiden ist
  eine menschliche Entscheidung.
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
speichert **kein fremdes Geheimnis**. Das löst die Operator-Entscheidung vom 2026-08-06
für das Passwort ab; sie gilt weiter für den Bot-Token.

**Operator-Entscheidung 2026-08-06 — der Bot-Token liegt in der SQLite.** Die Werte für
Foundry, Discord und Ollama werden in der Oberfläche gepflegt (#25, der Bot-Token seit
#19); ein dort gesetzter Wert schlägt die Umgebung, die als Vorgabe beim ersten Start
bleibt. Damit liegt der Bot-Token **im Klartext in `chronicle.sqlite3` — und die geht ins
Backup.** Das ist bewusst so entschieden: eine Homelab-Instanz, Backup auf eigenem NAS,
und der Betrieb wäre sonst nur über ServiceBay-Template-Variablen zu ändern. Er ist
außerdem **unser** Token und nicht der einer fremden Gegenstelle. Die Abwägung hängt an
drei Bedingungen, die nicht wegfallen dürfen — sie gelten für **jedes** Geheimnis in
`settings.SECRET_KEYS`:

- Die Seite steht **hinter Authelia und dem `Remote-User`-Guard** wie jede andere.
- Der Wert wird **nirgends angezeigt** — nicht im Formular, nicht auf `/status`, nicht in
  `repr`, Log oder Fehlermeldung; angezeigt wird nur, *ob* er gesetzt ist.
- Übertragen wird **nur per POST**, nie in einer URL; ein leeres Feld heißt unverändert.

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
  Verwalten (#51, `chronicle.roles`) — abgelöst mit #62: wer was darf, entscheidet
  Discord über seine eigenen Kanal- und Rollenrechte. Kein eigenes Rollenmodell mehr;
  `chronicle.roles` verschwindet mit #69.

## Kommentare

Standardmäßig keine. Einen nur für ein nicht-offensichtliches **Warum** — eine versteckte
Bedingung, ein Workaround, eine überraschende Invariante. Nicht erzählen, *was* der Code
tut.

## Skripte statt Prosa

Deterministische, wiederholbare Schritte gehören in ein eingechecktes Skript, nicht in
Prosa, die ein Agent bei jedem Lauf neu auslegt. Menschliches Urteil ist für *was* zu
prüfen ist und *warum* etwas fehlschlug — die Mechanik macht ein Skript.

## ServiceBay ist die Zielplattform — ihre ADRs binden uns

Deployt wird auf eine ServiceBay-Box (#12). Deren ADRs und Bau-Standards liegen
**nicht** in diesem Repo, sondern auf dem ServiceBay-MCP (Server `servicebay` in der
lokalen Claude-Konfiguration; Adresse und Token gehören nicht ins Repo). **Vor jeder
Architekturentscheidung:** `get_service_standards` abrufen und die dort verlinkten
Assists (`get_assist`) lesen. Dieser Abschnitt ist der Extrakt, nicht der Ersatz.

- **ADR 0001 — SSO:** user-facing läuft auf einer Subdomain hinter
  Authelia-Forward-Auth. Die App baut **kein eigenes Login**; sobald echte Inhalte
  angezeigt werden (#5/#7), erzwingt sie den `Remote-User`-Header und testet, dass
  ohne ihn abgelehnt wird — kein LAN-Bypass. „Keine Zugriffskontrolle" weiter unten
  meint Mandantentrennung zwischen Gruppen — nicht die Haustür und nicht die
  Verwaltungsrolle innerhalb der Instanz.
- **`/healthz` → 200** ist Test-Seam und Install-Gate der Box.
- **SQLite läuft im WAL-Modus** — Plattform-Lektion gegen „database is locked".
- **CI gatet den Image-Publish auf grüne Tests** (`needs: test`); eine CI, die nur
  baut, ist non-compliant. Kommt mit #12, ebenso pinned Tags statt `:latest`.
- **UI folgt dem ServiceBay-Design-Standard** (`get_assist service-ui-design-standard`).
  Mit #62/#69 bleibt davon nur die **Betreiber-Seite** — für die gilt er weiter.
  **ADR 0001 (Authelia-SSO) bleibt damit in Kraft**: auf dieser Seite liegt der
  Bot-Token, also gibt es sehr wohl etwas zu schützen. Für die *spielenden* Rechte
  gilt die Ablösung trotzdem — die liefert Discord über seine Kanal- und Rollenrechte.
  (Die frühere Fassung sagte, ADR 0001 werde gegenstandslos; das galt für eine
  Instanz ganz ohne Seite und ist mit der Entscheidung vom 2026-08-10 überholt.)
- **Läufe über ~10 s sind server-eigene, beobachtbare Jobs** — abbrechbar, neustartfest,
  Wiederanbindung über Job-Id (`get_assist long-running-process`).
- **Erklärte Abweichung, auslaufend:** Flask + Jinja statt des empfohlenen FastAPI.
  Grund war SSR für eine Handvoll Ansichten. Mit #69 fällt die Begründung weg und mit
  ihr fast der ganze Webteil — was bleibt, ist ein Prozess mit `/healthz` und der Bot.
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

## Autoloop

`.claude/skills/autoloop-issues/` orchestriert Planner → Builder → Verify. Zustand
läuft ausschließlich über `queue.py`-Verben (GitHub-Labels + Issue-Kommentare als
Wahrheit, ein winziger gitignorierter Cache für den laufenden Durchgang). Der
Verify-Schritt setzt ein deployfähiges Artefakt voraus — das kommt mit #12; vorher
meldet er ehrlich `owed` statt grün.
