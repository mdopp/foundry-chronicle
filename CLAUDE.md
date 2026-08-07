# Foundry Chronicle — Hausregeln

Sitzungsprotokolle für Tisch-Rollenspiel: aus Notizen und Foundrys Chat-Log wird eine
lesbare Chronik. **Eine Instanz trägt mehrere Runden** (Epic #62 löst darin »eine Instanz
pro Gruppe« aus Epic #1 ab). Siehe Epic #1 und #62 für die tragenden Entscheidungen und
`docs/architektur.md` für das Bild.

Diese Regeln gelten für jede Sitzung, Mensch oder Agent.

## Die drei Entscheidungen, die nicht neu verhandelt werden

- **Transkription ist eine vorgeschaltete Stufe, kein zweiter Weg.** Präsenz- und
  Online-Sitzungen treffen sich bei der Zusammenführung. Es gibt eine Pipeline.
- **Foundry liefert die Zahlen, der Text die Erzählung.** Würfe, Schaden und Beute
  werden eingesetzt, nie rekonstruiert.
- **Alles nach der Aufnahme läuft im Stapel.** Keine Echtzeit, keine Latenzgrenze,
  keine GPU-Pflicht.

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

**Operator-Entscheidung 2026-08-06 — das Foundry-Passwort liegt in der SQLite.** Die
Werte für Foundry, Discord und Ollama werden in der Oberfläche gepflegt (#25, der
Bot-Token seit #19); ein dort gesetzter Wert schlägt die Umgebung, die als Vorgabe beim
ersten Start bleibt. Damit liegen Passwort **und Bot-Token im Klartext in
`chronicle.sqlite3` — und die geht ins Backup.** Das ist bewusst so entschieden: eine
Homelab-Instanz, Backup auf eigenem NAS, und der Betrieb wäre sonst nur über
ServiceBay-Template-Variablen zu ändern. Die Abwägung hängt an drei Bedingungen, die
nicht wegfallen dürfen — sie gelten für **jedes** Geheimnis in `settings.SECRET_KEYS`:

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
  **Innerhalb** einer Instanz gibt es seit der Operator-Entscheidung 2026-08-06 zwei
  Rollen für die Weboberfläche: Mitspielen und Verwalten (#51). Die
  Rolle kommt aus einer Gruppe, die der Proxy mitliefert; ein leerer Gruppenname heißt
  weiterhin: alle dürfen alles. Entschieden wird sie an genau einer Stelle
  (`chronicle.roles`).

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
- **Läufe über ~10 s sind server-eigene, beobachtbare Jobs** — abbrechbar, neustartfest,
  Wiederanbindung über Job-Id (`get_assist long-running-process`).
- **Erklärte Abweichung:** Flask + Jinja statt des empfohlenen FastAPI. Grund: SSR für
  eine Handvoll Ansichten, synchron reicht; Bot und Stapel werden eigene Prozesse.
  Der Standard erlaubt begründete Abweichungen — das hier ist die Begründung.
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
