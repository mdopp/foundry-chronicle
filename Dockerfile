FROM python:3.12-slim AS bau

WORKDIR /quelle
COPY pyproject.toml README.md ./
COPY src ./src
# git nur in der Baustufe: py-cord hängt an einem unveröffentlichten Commit (siehe
# pyproject.toml), und ohne git kann pip die ``git+https``-Zeile nicht auflösen — der Bau
# bräche mit »Cannot find command 'git'«. Das Laufzeit-Image erbt nur /fertig.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --prefix=/fertig ".[discord]"

FROM python:3.12-slim

COPY --from=bau /fertig /usr/local

# libopus0 kodiert die Ansage und dekodiert die empfangenen Sprachpakete — py-cord bringt
# nur die Bindung mit, nicht die Bibliothek. espeak-ng spricht die Einwilligungs-Ansage;
# beides zusammen liegt im einstelligen Megabyte-Bereich.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Kein ffmpeg und kein PyAV im Image: seit #216 dekodiert **niemand hier** mehr Audio.
# Ein Diktat vom Telefon (m4a/AAC, ogg/opus) wird als Datei weitergereicht, und
# entpackt wird es im Nachbardienst ``solaris-whisper-batch``. Die Ansage kommt darum
# von Hand auf 48 kHz Stereo (chronicle/bot/ansage.py).
#
# /aufnahmen liegt neben /data und nicht darin: nur /data wird gesichert, und die
# Audiospuren gehören nie ins Backup. Seit #231 hängt nur noch **ein** Container darin —
# derselbe Prozess schreibt die Spuren nachts und liest sie im Stapel.
# Der synthetische Durchstich der Verify-Stufe läuft im Container gegen eine
# Wegwerf-Instanz; ohne ihn im Image bliebe er ein Playbook-Absatz statt eines Laufs.
COPY scripts/verify_e2e.py /app/scripts/verify_e2e.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHRONICLE_DATA_DIR=/data \
    CHRONICLE_RECORDINGS_DIR=/aufnahmen

WORKDIR /data

# Kein EXPOSE: dieses Abbild veröffentlicht nichts mehr. Der einzige Horcher ist
# ``/healthz`` auf ``127.0.0.1:$CHRONICLE_HEALTH_PORT`` — das Install-Gate der Box, und
# der Poller läuft daneben auf derselben Maschine (#228). Ein EXPOSE behauptete das
# Gegenteil.

# Kein USER-Wechsel: unter rootless Podman bildet Container-uid 0 auf den unprivilegierten
# Host-Benutzer ab, dem das hostPath-Datenverzeichnis gehört. Ein eigener Benutzer landete
# auf einer subuid ohne Schreibrecht darauf — die SQLite-Datei wäre nicht anlegbar.
# Der Dienst **ist** der Bot: Gateway-Verbindung, Aufnahme, Befehle, der nächtliche Lauf
# (#229) und das Install-Gate (#228). Seit #231 gibt es daneben nichts mehr, was ein
# eigener Container tragen müsste — deshalb steht der Befehl hier und nicht in der Vorlage.
CMD ["python", "-m", "chronicle.bot"]
