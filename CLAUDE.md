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
  Gilde gehört: Bot-Token, Ollama-Adresse und -Modell (#87) und **wer die Seite selbst
  verwalten darf**. Letzteres ist keine Rolle im Spiel — es ist die Frage, wer an den
  Bot-Token darf, und die gehört dem Betrieb dieser Box (#90 regelt etwas anderes: über
  einer *Runde* steht niemand). Die Fassung vom 2026-08-11 zählte es hier heraus; mit
  #157 steht es wieder da, weil es sonst nirgends stünde.
  Alles andere fällt. Wer eine neue Fähigkeit baut, baut sie **in Discord**,
  nicht in einer Seite — die Betreiber-Seite ist kein Ort für Spielinhalte. Seit #157
  trägt die Seite keine Spielinhalte mehr; einen zweiten Weg gibt es nicht.
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
Discord und Ollama werden auf der Betreiber-Seite gepflegt (#25, der Bot-Token seit #19),
die für Foundry seit #157 in Discord unter `/setup` — die frühere Fassung nannte für
alle drei »die Oberfläche«, weil es damals nur eine gab. Ein gepflegter Wert schlägt die
Umgebung, die als Vorgabe beim ersten Start bleibt. Damit liegt der Bot-Token **im Klartext in `chronicle.sqlite3` — und die geht ins
Backup.** Das ist bewusst so entschieden: eine Homelab-Instanz, Backup auf eigenem NAS,
und der Betrieb wäre sonst nur über ServiceBay-Template-Variablen zu ändern. Er ist
außerdem **unser** Token und nicht der einer fremden Gegenstelle. Die Abwägung hängt an
drei Bedingungen, die nicht wegfallen dürfen — sie gelten für **jedes** Geheimnis in
`settings.SECRET_KEYS`:

- Die Seite steht **hinter Authelia und dem `Remote-User`-Guard**. Seit #157 ist sie die
  einzige, die es gibt — die Bedingung ist damit nicht schwächer, sondern trägt allein.
  **Was sie heißt, war bis #190 weniger, als hier stand:** der Guard prüfte nur, ob die
  Kopfzeile da ist, und die schreibt sich jeder selbst, der den Port erreicht — der
  liegt im Host-Netz auf `0.0.0.0`, der Proxy war ein Weg dorthin und nicht der einzige.
  Ein Unangemeldeter im LAN konnte damit den Bot-Token überschreiben. Seither gilt sie
  wieder, aber wörtlich: geglaubt werden `Remote-User` **und** `Remote-Groups` nur, wenn
  der Aufruf **von dieser Maschine** kommt — dort läuft der Proxy (`chronicle.herkunft`).
  Das ersetzt Authelia nicht, es macht Authelia zum einzigen Weg. Wer den Proxy woanders
  hinstellt, trägt seine Adresse in `CHRONICLE_TRUSTED_PROXIES` nach; ohne das antwortet
  die Seite 403, und der Bot-Token bleibt unerreichbar statt ungeschützt.
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
  Rollenmodell mehr für Spielinhalte. **`chronicle.roles` bleibt trotzdem** (Nachtrag
  #157; die frühere Fassung sagte, es verschwinde mit #69): `ist_verwalter` und der
  `Remote-Groups`-Header tragen die Antwort auf #90 — wer an den Bot-Token darf. Das ist
  eine Frage des Betriebs, keine des Spiels, und hat in keiner Gilde einen Ort.
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

## ServiceBay ist die Zielplattform — ihre ADRs binden uns

Deployt wird auf eine ServiceBay-Box (#12). Deren ADRs und Bau-Standards liegen
**nicht** in diesem Repo, sondern auf dem ServiceBay-MCP (Server `servicebay` in der
lokalen Claude-Konfiguration; Adresse und Token gehören nicht ins Repo). **Vor jeder
Architekturentscheidung:** `get_service_standards` abrufen und die dort verlinkten
Assists (`get_assist`) lesen. Dieser Abschnitt ist der Extrakt, nicht der Ersatz.

- **ADR 0001 — SSO:** user-facing läuft auf einer Subdomain hinter
  Authelia-Forward-Auth. Die App baut **kein eigenes Login**; sobald echte Inhalte
  angezeigt werden (#5/#7), erzwingt sie den `Remote-User`-Header — **und glaubt ihn
  nur, wenn der Aufruf von dieser Maschine kommt** (#190). Die Kopfzeile allein ist kein
  Beleg: sie schreibt sich jeder selbst, der den offenen Port erreicht. »Kein
  LAN-Bypass« hieß bis dahin nur »ohne Kopfzeile kein Zugang«, und der Test dazu prüfte
  auch nur das; geprüft wird seither der **erfundene** Fall. „Keine Zugriffskontrolle"
  weiter unten meint Mandantentrennung zwischen Gruppen — nicht die Haustür vor der
  Betreiber-Seite. Was hinter ihr liegt, sind Instanz-Werte und keine Rechte über fremde
  Runden (#90).
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
- **Erklärte Abweichung, ausgelaufen und trotzdem stehengeblieben:** Flask + Jinja statt
  des empfohlenen FastAPI. Grund war SSR für eine Handvoll Ansichten; die Handvoll ist
  mit #157 auf **eine** Seite geschrumpft. Damit ist die alte Begründung erledigt — der
  Rahmen bleibt trotzdem, weil ein Wechsel jetzt erst recht nichts einbrächte: übrig
  sind ein Formular, zwei Weiterleitungen, `/healthz` und der Bot. Ein Umbau auf FastAPI
  wäre Arbeit ohne Gegenwert und ein Risiko am Install-Gate der Box. **Wer hier etwas
  Neues baut, baut es in Discord** — eine zweite Seite entstünde ohnehin nicht.
  (Die frühere Fassung nannte die Abweichung »auslaufend« und erwartete, dass mit #69
  fast der ganze Webteil verschwindet. Das ist eingetreten; geblieben ist die
  Betreiber-Seite, und mit ihr Flask.)
- **Erklärte Abweichung, bezahlt und bis auf Weiteres bleibend:** der Pod läuft mit
  `hostNetwork: true` statt im eigenen Netz-Namensraum, den **ADR 0007** verlangt (#165).
  Der Grund steht im Template: der Dienst spricht die Nachbarn dieser Box über die
  Schleife an — Ollama auf `127.0.0.1:11434` schreibt die Chronik, `solaris-tts` auf
  `127.0.0.1:8881` spricht die Ansage —, und der Proxy findet ihn so ohne veröffentlichten
  `hostPort`. **Was sie kostet, ist seit #190 belegt und nicht mehr vermutet:** Host-Netz
  ist der Grund, warum der Port auf `0.0.0.0` im ganzen LAN erreichbar war, und das war
  ausnutzbar — ein selbst erfundener `Remote-User` genügte, um auf die Betreiber-Seite und
  damit an den Bot-Token zu kommen. Ohne Host-Netz wäre der Port nie im LAN gewesen. Die
  Rechnung dafür ist bezahlt, nicht gestundet: `chronicle.herkunft` glaubt `Remote-User`
  und `Remote-Groups` nur von einer Adresse **dieser Maschine**, `CHRONICLE_TRUSTED_PROXIES`
  ist der Ausweg für einen umgezogenen Proxy, und eine Selbstprobe beim Start sagt es, wenn
  die Prüfung im Kern nicht trägt. **Sie fiele, sobald die Nachbarn auch aus einem eigenen
  Netz-Namensraum erreichbar sind** — heute binden Ollama und `solaris-tts` nur an
  Loopback, ein isolierter Namensraum erreicht sie damit nicht. Das liegt in fremden
  Vorlagen und ist deshalb eine Bedingung, kein Versprechen. Gefragt wurde: ADR 0007 sieht
  benannte Ausnahmen vor, `mdopp/servicebay#2518` hat sie verneint — die Liste bleibt
  geschlossen. **Und angefasst wird hier nichts ohne Box-Verify:** eine Netzänderung legt
  diesen Dienst still, wenn sie falsch ist, und er hängt an einer echten Discord-Gilde.
  `tests/test_template.py` hält `hostNetwork: true` samt dieser Begründung fest, damit es
  niemand versehentlich entfernt.
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
Verify-Schritt prüft das deployte Artefakt auf der Box; was er nicht ausführen kann,
meldet er ehrlich als `owed` statt grün.
