# Sitzungsprotokoll — Architektur

Eine Instanz pro Gruppe. Konfiguration: Foundry-URL + Zugangsdaten, Discord-Bot-Token.
Alles andere kommt aus Foundry.

Wie der Zugriff auf Foundry technisch läuft, steht in [`foundry-zugriff.md`](foundry-zugriff.md).

```mermaid
flowchart TB
    subgraph EXT["Extern · das Einzige, was konfiguriert wird"]
        FOUNDRY[("Foundry VTT<br/>URL + Zugangsdaten")]
        DISCORD["Discord<br/>Bot-Token · darf leer bleiben"]
    end

    subgraph CAP["Erfassung · während und direkt nach der Sitzung"]
        NOTE["Notiz-Eingabe je Szene · #5<br/>tippen oder Tastatur-Diktat"]
        UPLOAD["Diktat-Upload · #14<br/>Sprachmemo, ein Sprecher"]
        DIKT["Diktat-Kanal · #19<br/>Audio oder Text, von überall"]
        REC["Recorder-Bot · #8<br/>nur online · hörbare Ansage<br/>Spur je Sprecher"]
    end

    subgraph FA["Foundry-Anbindung · #3"]
        HS["Handschlag<br/>/join + Socket.io"]
        FILTER["Berechtigungsfilter<br/>vor dem Speicher"]
        ADAPTER["System-Adapter je Regelwerk<br/>dahinter neutral"]
    end

    subgraph BATCH["Verarbeitung · nachts im Stapel, CPU reicht"]
        TRANS["Transkription · #10<br/>faster-whisper, Spur für Spur<br/>Foundry-Namen als Vokabular"]
        MERGE["Zusammenführung<br/>online: Zeitachse · #11<br/>vor Ort: Szenenfolge"]
        COMP["Komposition · #6<br/>ordnet, schmückt nicht aus"]
    end

    subgraph STORE["Lokal · verlässt die Maschine nicht"]
        DB[("SQLite<br/>Sitzungen · Szenen · Notizen · Transkripte<br/>Foundry-Fakten · Chroniken · Rückblicke<br/>Register · Personen-Zuordnung")]
        AUDIO[("Audiospuren<br/>Dateisystem · nach Lauf löschbar · nie Backup")]
    end

    subgraph MEM["Erinnern und Wiedergeben · die Schicht über den Sitzungen"]
        VIEW["Protokoll-Ansicht · #7<br/>Chronik und Rückblick · #13"]
        SEARCH["Volltextsuche · #17<br/>SQLite FTS5"]
        REGUI["Register bestätigen · #15<br/>Ja/Nein je Vorschlag"]
        RETELL["Nacherzählung · #18<br/>auf Wunsch, über das Register"]
        DELIVER["Zustellung · #16<br/>Bot postet den Rückblick"]
    end

    DISCORD -. "nur Online-Sitzungen" .-> REC
    DISCORD --> DIKT
    REC --> AUDIO
    UPLOAD --> AUDIO
    DIKT -- "Audio" --> AUDIO
    DIKT -- "Text" --> DB
    NOTE --> DB

    FOUNDRY --> HS --> FILTER --> ADAPTER
    ADAPTER -- "Würfe · Aktoren · Kampfzustand" --> DB

    AUDIO --> TRANS
    DB -- "Eigennamen" --> TRANS
    TRANS -- "Sitzungsspuren" --> MERGE
    TRANS -- "Diktat: als Notizen" --> DB
    DB -- "Notizen" --> MERGE
    MERGE --> COMP
    DB -- "Fakten" --> COMP
    COMP -- "Chronik + Rückblick" --> DB
    COMP -- "Register-Vorschläge" --> REGUI
    REGUI -- "bestätigt" --> DB

    DB <--> VIEW
    DB <--> SEARCH
    DB --> RETELL
    DB -- "Rückblick" --> DELIVER
    DELIVER --> DISCORD
```

## Die drei tragenden Entscheidungen

**Die Transkription ist eine vorgeschaltete Stufe, kein zweiter Weg.** Beide
Betriebsarten treffen sich bei der Zusammenführung. Eine Präsenzsitzung überspringt
schlicht den Audio-Zweig — es gibt keine zweite Pipeline zu pflegen. Das Diktat (#14,
#19) ist derselbe Transkriptionskern, nur ohne die Discord-Vorstufen: ein Sprecher,
eine Spur, Ergebnis wird zu Notizen.

**Foundry liefert die Zahlen, der Text die Erzählung.** Würfe, Schaden und Beute
werden nie aus gesprochener oder getippter Sprache rekonstruiert, sondern aus dem
Chat-Log eingesetzt. Das Modell ordnet und verknüpft; es rechnet und rät nicht.

**Alles nach der Aufnahme läuft im Stapel.** Keine Echtzeit-Transkription, damit keine
GPU-Konkurrenz und keine Latenzfrage. Läuft nachts; auf CPU langsam genug, um ohne
Grafikkarte auszukommen. Auch die Erfassung folgt dem Prinzip: der Diktat-Kanal ist
ein Briefkasten — jetzt einwerfen, geholt wird, wenn der Dienst das nächste Mal läuft.

## Die Schicht über den Sitzungen

Das Ziel ist nicht das Archiv, sondern **wissen, was zuletzt in der Geschichte
passiert ist** — und Teile davon nacherzählen können.

- Der **Rückblick** (#13) ist das Artefakt, das wöchentlich konsumiert wird; die
  Chronik ist das Archiv dahinter. Die Zustellung nach Discord (#16) bringt ihn
  dorthin, wo die Gruppe ohnehin ist.
- Das **Register** (#15) ist ein Index, kein Wiki: Name, ein Satz, Verweise. Die
  Wahrheit über Figuren wohnt in Foundry. Vorschläge macht das Modell, bestätigt wird
  von Hand — dasselbe Muster wie die Personen-Zuordnung.
- Die **Nacherzählung** (#18) kommt bewusst zuletzt: verdichtete Prosa über Wochen von
  Material ist die Stelle mit dem höchsten Erfindungsrisiko. Sie navigiert über das
  Register; was das Register nicht kennt, kommt nicht vor.

## Was die Kanten nicht zeigen

- Alle Web-Kästen — Notiz-Eingabe, Upload, Ansicht, Suche, Register — sind **eine**
  schlanke, serverseitig gerenderte Oberfläche (#2), kein Frontend-Gerüst.
- Die **Personen-Zuordnung** Discord ↔ Foundry entsteht einmalig: automatisch
  vorgeschlagen, vom Menschen bestätigt, danach im Speicher.
- **Foundry ist eine harte Abhängigkeit.** Ist es beim Einrichten aus, bleibt das
  System leer — das braucht eine verständliche Meldung, keine leere Liste. Die
  Fakten werden deshalb zwischengespeichert, nicht bei jedem Aufruf geholt.
- Der Stapel zeigt **ehrlichen Status statt Fortschritt**: „läuft im nächsten Stapel,
  Ergebnis morgen früh" — kein Balken, der Echtzeit vortäuscht, die es nicht gibt.
- Die **Audiospuren sind das Einzige, was groß wird.** Nach erfolgreichem Lauf
  löschbar; Protokoll und Transkript sind klein. Der Diktat-Kanal läuft durch Discords
  Cloud — für Online-Gruppen kein Unterschied, für reine Präsenzgruppen eine bewusste
  Entscheidung.
