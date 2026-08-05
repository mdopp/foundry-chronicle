FROM python:3.12-slim AS bau

WORKDIR /quelle
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/fertig ".[server,transcribe]"

FROM python:3.12-slim

COPY --from=bau /fertig /usr/local

# /aufnahmen liegt neben /data und nicht darin: nur /data wird gesichert, und die
# Audiospuren gehören nie ins Backup. Ein eigenes Volume dafür bekommt das
# ServiceBay-Template, sobald etwas auf der Box Spuren ablegt (Recorder-Bot, Upload).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHRONICLE_HOST=0.0.0.0 \
    CHRONICLE_PORT=8000 \
    CHRONICLE_DATA_DIR=/data \
    CHRONICLE_RECORDINGS_DIR=/aufnahmen \
    CHRONICLE_WHISPER_MODEL=small

WORKDIR /data
EXPOSE 8000

# Kein USER-Wechsel: unter rootless Podman bildet Container-uid 0 auf den unprivilegierten
# Host-Benutzer ab, dem das hostPath-Datenverzeichnis gehört. Ein eigener Benutzer landete
# auf einer subuid ohne Schreibrecht darauf — die SQLite-Datei wäre nicht anlegbar.
CMD ["sh", "-c", "exec waitress-serve --host=$CHRONICLE_HOST --port=$CHRONICLE_PORT --call chronicle.app:create_app"]
