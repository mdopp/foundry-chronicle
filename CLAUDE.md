# Foundry Chronicle — Hausregeln

Sitzungsprotokolle für Tisch-Rollenspiel: aus Notizen und Foundrys Chat-Log wird eine
lesbare Chronik. **Eine Instanz pro Gruppe.** Siehe Epic #1 für die tragenden
Entscheidungen und `docs/architektur.md` für das Bild.

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
werden zur Laufzeit injiziert; Platzhalter sind in Ordnung, konkrete Werte nicht. Das
gilt auch für die **Foundry-Adresse** — die gehört in die Konfiguration, nicht ins Repo.

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
- **Eine Instanz pro Gruppe** ist eine tragende Entscheidung: kein `group_id`, keine
  Mandantentrennung, keine Zugriffskontrolle zwischen Gruppen. Nicht auf Vorrat bauen.

## Kommentare

Standardmäßig keine. Einen nur für ein nicht-offensichtliches **Warum** — eine versteckte
Bedingung, ein Workaround, eine überraschende Invariante. Nicht erzählen, *was* der Code
tut.

## Skripte statt Prosa

Deterministische, wiederholbare Schritte gehören in ein eingechecktes Skript, nicht in
Prosa, die ein Agent bei jedem Lauf neu auslegt. Menschliches Urteil ist für *was* zu
prüfen ist und *warum* etwas fehlschlug — die Mechanik macht ein Skript.

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
