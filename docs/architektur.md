# Sitzungsprotokoll — Architektur

Eine Instanz pro Gruppe. Konfiguration: Foundry-URL + Token, Discord-Bot-Token.
Alles andere kommt aus Foundry.

```mermaid
flowchart TB
    subgraph EXT["Extern · das Einzige, was konfiguriert wird"]
        FOUNDRY[("Foundry VTT<br/>URL + Token")]
        DISCORD["Discord<br/>Bot-Token"]
    end

    subgraph CAP["Aufnahme · während der Sitzung"]
        REC["Recorder-Bot<br/>Slash-Befehl holt ihn in den Kanal<br/>hörbare Einwilligungs-Ansage"]
        NOTE["Notiz-Eingabe je Szene<br/>der einzige wöchentliche Handgriff"]
    end

    subgraph BATCH["Verarbeitung · nach der Sitzung, ohne Zeitdruck"]
        TRANS["Transkription<br/>faster-whisper, Spur für Spur<br/>Foundry-Namen als Vokabular"]
        MERGE["Zusammenführung<br/>online: gemeinsame Zeitachse<br/>vor Ort: Szenenfolge"]
        COMPOSE["Komposition<br/>ordnet Text, setzt Fakten ein"]
    end

    subgraph STORE["Lokal · verlässt die Maschine nicht"]
        DB[("SQLite<br/>Sitzungen · Notizen · Transkripte<br/>Protokolle · Personen-Zuordnung")]
        AUDIO[("Audiospuren<br/>Dateisystem · nach Lauf löschbar")]
    end

    UI["Weboberfläche<br/>Notizen · Protokolle · Zuordnung bestätigen"]

    DISCORD -. "nur Online-Sitzungen" .-> REC
    REC --> AUDIO
    AUDIO --> TRANS
    FOUNDRY -- "Spieler · Charaktere" --> DB
    FOUNDRY -- "Chat-Log: Würfe, Schaden, Beute" --> DB
    NOTE --> DB
    DB -- "Eigennamen" --> TRANS
    TRANS --> MERGE
    DB -- "Notizen" --> MERGE
    MERGE --> COMPOSE
    DB -- "Fakten" --> COMPOSE
    COMPOSE -- "Protokoll" --> DB
    DB <--> UI
    UI -. "stellt bereit" .-> NOTE
```

## Die drei tragenden Entscheidungen

**Die Transkription ist eine vorgeschaltete Stufe, kein zweiter Weg.** Beide
Betriebsarten treffen sich bei der Zusammenführung. Eine Präsenzsitzung überspringt
schlicht den Audio-Zweig — es gibt keine zweite Pipeline zu pflegen.

**Foundry liefert die Zahlen, der Text die Erzählung.** Würfe, Schaden und Beute
werden nie aus gesprochener oder getippter Sprache rekonstruiert, sondern aus dem
Chat-Log eingesetzt. Das Modell ordnet und verknüpft; es rechnet und rät nicht.

**Alles nach der Aufnahme läuft im Stapel.** Keine Echtzeit-Transkription, damit keine
GPU-Konkurrenz und keine Latenzfrage. Läuft nachts; auf CPU langsam genug, um ohne
Grafikkarte auszukommen.

## Was die Kanten nicht zeigen

- Die **Personen-Zuordnung** Discord ↔ Foundry entsteht einmalig: automatisch
  vorgeschlagen, vom Menschen bestätigt, danach im Speicher.
- **Foundry ist eine harte Abhängigkeit.** Ist es beim Einrichten aus, bleibt das
  System leer — das braucht eine verständliche Meldung, keine leere Liste.
- Die **Audiospuren sind das Einzige, was groß wird.** Nach erfolgreichem Lauf
  löschbar; das Protokoll und das Transkript sind klein.
