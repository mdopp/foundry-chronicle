# chronik — die Daggerheart-Chronik auf Laib

Sitzungsprotokolle für Tisch-Rollenspielrunden: aus Notizen, Sprachspuren und dem Chat-Log
aus Foundry VTT wird eine lesbare Chronik. **Bedient wird in Discord** (#62), eine Seite
gibt es nicht (#231) — deshalb hat diese Vorlage keine Route. Eine Instanz trägt mehrere
Runden; eine zweite Gruppe lädt den Bot in ihren Server ein.

Die Vorlage folgt laib ADR 0004: Quadlet-Einheiten, der Vertrag `laib.yml`,
`values.example.yml`. Sie liegt in diesem Repo, nicht in `mdopp/laib`, weil sie mit dem
Abbild dieses Repos zusammen wechselt (laib `docs/SERVICES.md`: »their repos add a
`laib.yml`; `laib template sync` pulls them«).

## Was läuft

| Einheit | Was | Netz |
|---|---|---|
| `chronik.container` | der Bot: Gateway, Aufnahme, Befehle, nächtlicher Lauf, `/healthz` | eigener Namensraum; `127.0.0.1:8701` für das Install-Gate |
| `chronik-whisper.container` | Verschriftung der Spuren **mit Zeitstempeln** (faster-whisper, `whisper_batch.py`) | eigener Namensraum; `10301` für Pods der Box, `lan: false`, dahinter ein Schlüssel |

Was der Bot erreicht, und wie:

- **Das Modell** über Laibs eingebaute Werte `${MODEL_URL}`, `${MODEL_LEASE}`, `${MODEL_KEY}`
  (laib ADR 0011). Das Sitzungsfenster fragt nach dem Preset `MODEL_PRESET`
  (`gemma-4-12b`); welches Fenster das ist, entscheidet die Lease. Der Schlüssel erreicht
  den Container als podman-Secret, nie über die Befehlszeile.
- **Die Ansage** über Piper aus Laibs Stimme (Wyoming, `TTS_URL`). Antwortet Piper nicht,
  spricht espeak-ng aus dem Abbild — eine Ansage kommt immer, sonst wird nicht
  aufgenommen (§201 StGB).
- **Den Erkenner** daneben über `host.containers.internal:10301`, mit dem Schlüssel
  `CHRONIK_WHISPER_KEY`, den Laib beim ersten Apply erzeugt.

**Warum ein eigener Erkenner und nicht Laibs Stimme-Whisper.** Der Whisper der Stimme
spricht Wyoming: ein Text je Äußerung, ohne Segmente und ohne Zeitstempel (`Transcript`
trägt `text` und `language`, sonst nichts). Die Chronik legt die Spuren der Sprecher nach
Zeit zu einem Gespräch zusammen (`chronicle.transcribe.merge`) und ordnet Äußerungen den
Szenen zu — ohne Zeitstempel ginge beides nur geraten. Der Erkenner ist der bisherige
`solaris-whisper-batch` (mdopp/solarisbay#1161), der nur diesem Dienst diente und deshalb
mit ihm umzieht: dasselbe unveränderte Abbild, dieselbe Datei, jetzt mit Schlüssel.

**Warum kein Host-Netz mehr.** Die ServiceBay-Fassung lag im Host-Netz (#165), weil
`llama-server`, `solaris-tts` und `solaris-whisper-batch` nur auf der Schleife hörten. Auf
Laib erreicht jeder Pod den Modelldienst und Piper über `host.containers.internal`, und der
Erkenner gehört diesem Dienst selbst. Damit fällt die Abweichung, wie sie es für diesen
Fall vorsah.

## Installieren

```sh
# einmal: dieses Repo als Registry (box.yml)
#   registries:
#     - {name: foundry-chronicle, url: https://github.com/mdopp/foundry-chronicle, ref: main}
laib template sync
laib secret set CHRONIK_DISCORD_TOKEN          # der Token aus dem Discord Developer Portal, auf stdin
laib propose --add chronik IMAGE=ghcr.io/mdopp/foundry-chronicle:<tag> "Chronik für die Runde"
```

`<tag>` ist ein fester Tag: `python scripts/bestimme_image_tag.py` nennt ihn, ein Release
ist die Nummer ohne führendes `v`. Ohne Karte: `WHISPER_IMAGE=lscr.io/linuxserver/faster-whisper:latest`,
`WHISPER_DEVICE=cpu`, `WHISPER_COMPUTE=int8` — der CI-Lauf der Vorlage fährt genau so.

| Wert | Bedeutung | Vorgabe |
|---|---|---|
| `IMAGE` | das Abbild dieses Repos | `ghcr.io/mdopp/foundry-chronicle:latest` |
| `MODEL_PRESET` | das Modell, um das der Abend bittet — ein Preset der Lease, kein Fenstername | `gemma-4-12b` |
| `GPU_LEASE` | `aus`: kein Aufruf an die Lease, der Haushalt behält sein Modell | `an` |
| `TTS_URL` | Piper (Wyoming); leer: espeak-ng | `tcp://host.containers.internal:10200` |
| `WHISPER_IMAGE`, `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE` | der Erkenner | `:gpu`, `large-v3-turbo`, `cuda`, `float16` |

Geheimnisse: `CHRONIK_DISCORD_TOKEN` (extern, `laib secret set`), `CHRONIK_WHISPER_KEY`
(erzeugt). Keines steht als Wert in einer Datei.

## Umzug von ServiceBay — einmal

Die Daten ziehen in Laibs Ordnung um; die Vorlage kennt die alten Pfade nicht. Der Schritt
gehört in laib `docs/MIGRATION.md` Schritt 4, nach dem `llama`-Dienst (die Lease muss
stehen) und vor `laib apply chronik`. Kopiert wird, nie verschoben: ServiceBays Pfade
bleiben, wie sie sind, und der Weg zurück ist, ServiceBays Unit wieder einzuschalten.

| ServiceBay | Laib |
|---|---|
| `${DATA_DIR}/daggerheart` (die SQLite im WAL-Modus) | `${DATA_DIR}/chronik/data` |
| `${DATA_DIR}/daggerheart-aufnahmen` (Spuren, noch nicht verschriftet) | `${DATA_DIR}/chronik/aufnahmen` |

```sh
# auf der Box, als core — der Bot darf nicht zweimal mit demselben Token angemeldet sein
systemctl --user stop daggerheart-chronik.service
( set -eu; D=/mnt/data/stacks
  test ! -e "$D/chronik/data/chronicle.sqlite3" || { echo "chronik hat schon Daten"; exit 1; }
  mkdir -p "$D/chronik/data" "$D/chronik/aufnahmen"
  cp -a "$D/daggerheart/." "$D/chronik/data/"             # samt -wal/-shm: gestoppt, also konsistent
  cp -a "$D/daggerheart-aufnahmen/." "$D/chronik/aufnahmen/" )
laib secret set CHRONIK_DISCORD_TOKEN                       # derselbe Token wie bisher (ServiceBays DISCORD_BOT_TOKEN)
```

Danach ServiceBays Datei `daggerheart-chronik.kube` aus dem Quadlet-Wurzelordner nehmen
(laib `docs/MIGRATION.md`, »A unit name«) und `laib apply chronik`. Beim ersten Start räumt
die Datenbank die alten Werte `ollama_url`/`ollama_model` fort, weil `MODEL_URL` und
`MODEL_PRESET` jetzt stehen (#230); ein Fenster des toten Vorgängers bei der Lease gibt
der Bot beim Start zurück. `solaris-whisper-batch` und die Lease auf `127.0.0.1:8787`
gehören Solaris und gehen mit ihm.

## Die Stimme der Ansage: Piper statt Martin

Bis zum Umzug sprach die Einwilligungs-Ansage **Kokoro mit der Stimme »Martin«**
(`solaris-tts`, `127.0.0.1:8881`). Den Dienst gibt es auf Laib nicht. Die Vorlage spricht
mit **Piper aus Laibs Stimme**, also mit der Stimme der Hauspersona (`PERSONA_VOICE`) —
dieselbe Ansage, eine andere Stimme am Spieltisch. Ob das so bleibt, ist eine Entscheidung
des Betreibers; bis dahin gilt Piper, und ohne Piper espeak-ng.
