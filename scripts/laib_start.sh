#!/usr/bin/env bash
# Der echte Start der Laib-Vorlage (#362): kein Shim, kein Mock für Podman oder Quadlet.
#
# Wie laibs scripts/boot-test.sh, nur für diese Vorlage und über den Weg, den der Betreiber
# geht: die Vorlage liegt in einer Registry, `laib add chronik` schlägt vor, gibt frei und
# wendet an, und `laib apply` ist erst grün, wenn beide Units laufen und /healthz antwortet.
# Danach wird geprüft, was ein grünes Apply nicht sagt:
#
#   - das Abbild dieses Commits verschriftet aus seinem Container heraus eine echte Spur
#     beim Erkenner daneben (faster-whisper auf der CPU, Modell tiny) — mit Zeitstempeln
#     und nur mit dem Schlüssel;
#   - die Ansage erreicht ein echtes Piper (wyoming-piper, wie Laibs Stimme es startet);
#   - der Bot spricht beim Start den Lease-Vertrag mit Laibs eingebauten Werten: GET, und
#     sein eigenes verwaistes Fenster gibt er mit Bearer-Schlüssel und Halter zurück;
#   - kein Geheimnis steht auf der Befehlszeile eines Containers (laib #224).
#
# Braucht: einen systemd --user mit Podmans Quadlet-Generator, sudo für nft und /etc/cdi,
# python3, curl. LAIB_BIN ist laib aus mdopp/laib, CHRONIK_IMAGE das pullbare Abbild.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAIB="${LAIB_BIN:?LAIB_BIN: laib, gebaut aus mdopp/laib}"
IMAGE="${CHRONIK_IMAGE:?CHRONIK_IMAGE: das Abbild dieses Commits, pullbar}"
WORK="$(mktemp -d)"
export LAIB_HOME="$WORK/home" LAIB_ACTOR=human:ci
export LAIB_QUADLET="${LAIB_QUADLET:-$HOME/.config/containers/systemd}"
export LAIB_SYSTEMD="${LAIB_SYSTEMD:-$HOME/.config/systemd/user}"
DATA="$WORK/data"
KEY="modell-$(openssl rand -hex 12)"
TOKEN="discord-attrappe-$(openssl rand -hex 12)"
LEASE_LOG="$WORK/lease.jsonl"
PIPER_VOICE="${PIPER_VOICE:-de_DE-thorsten-medium}"

fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  set +e
  journalctl --user -u chronik.service -u chronik-whisper.service --no-pager 2>/dev/null | tail -60
  systemctl --user stop chronik.service chronik-whisper.service 2>/dev/null
  podman rm -f piper-ci >/dev/null 2>&1
  [ -n "${LEASE_PID:-}" ] && kill "$LEASE_PID" 2>/dev/null
  for s in $(podman secret ls --format '{{.Name}}' 2>/dev/null | grep '^laib\.chronik\.'); do podman secret rm "$s" >/dev/null; done
  rm -rf "$LAIB_QUADLET/chronik"
  systemctl --user daemon-reload 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

podman --version
systemctl --user --version >/dev/null
mkdir -p "$LAIB_QUADLET"

echo "--- laib template lint"
"$LAIB" template lint "$ROOT/templates/chronik"

echo "--- eine Karte für den Vertrag: CDI nvidia.com/gpu=all ohne Gerät (der Runner hat keine)"
sudo mkdir -p /etc/cdi
sudo tee /etc/cdi/ci-gpu.yaml >/dev/null <<'CDI'
cdiVersion: "0.5.0"
kind: nvidia.com/gpu
devices:
  - name: all
    containerEdits:
      env: ["CHRONIK_CI_KEINE_KARTE=1"]
CDI

echo "--- die Lease der Box als Attrappe: der Vertrag aus laib ADR 0011, Ergänzung 2026-10-14"
python3 - "$KEY" "$LEASE_LOG" <<'PY' &
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
KEY, LOG = sys.argv[1], sys.argv[2]
now = int(time.time())
# Ein Fenster, das ein toter Vorgänger offen ließ: der Bot muss es beim Start zurückgeben.
state = {"held": True}
def status():
    if not state["held"]:
        return {"state": "none", "model": "", "alias": "gemma-4-e4b", "expires_at": None, "holder": "",
                "last_renewed_at": None, "renew_after": None, "retry_after": 30}
    return {"state": "ready", "model": "shared", "alias": "gemma-4-12b", "expires_at": now + 900,
            "holder": "foundry-chronicle", "last_renewed_at": now, "renew_after": 300, "retry_after": 30}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def answer(self, code, body):
        b = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def note(self, body):
        with open(LOG, "a") as f:
            f.write(json.dumps({"method": self.command, "path": self.path,
                                "bearer_ok": self.headers.get("Authorization") == f"Bearer {KEY}",
                                "body": body}) + "\n")
    def do_GET(self):
        self.note(None); self.answer(200, status())
    def do_DELETE(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        self.note(body)
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self.answer(401, {"ok": False, "reason": "unauthorized"})
        if body != {"holder": "foundry-chronicle"}:
            return self.answer(409, {"ok": False, "reason": "held", "holder": "foundry-chronicle"})
        state["held"] = False
        self.answer(200, {"ok": True, "state": "released"})
    def do_POST(self):
        self.note("post"); self.answer(401, {"ok": False, "reason": "unauthorized"})
ThreadingHTTPServer(("0.0.0.0", 11435), H).serve_forever()
PY
LEASE_PID=$!

echo "--- Piper, wie Laibs Stimme es startet (Host-Netz, für Pods der Box erreichbar)"
mkdir -p "$WORK/piper"
podman run -d --name piper-ci --network host -v "$WORK/piper:/data:z" \
  docker.io/rhasspy/wyoming-piper:latest --voice "$PIPER_VOICE" --uri tcp://0.0.0.0:10200 \
  --data-dir /data --download-dir /data >/dev/null

echo "--- laib init, eine Box mit Karte und einem Modell-Vertrag, die Vorlage in einer Registry"
"$LAIB" init --domain ci.test --data-dir "$DATA" >/dev/null
cat > "$LAIB_HOME/state/box.yml" <<YML
domain: ci.test
dataDir: $DATA
roles: {}
gpu: {present: true}
model:
  url: http://host.containers.internal:11435/v1
  lease: http://host.containers.internal:11435/api/model-lease
  keySecret: MODEL_API_KEY
YML
printf '%s' "$TOKEN" | "$LAIB" secret set CHRONIK_DISCORD_TOKEN >/dev/null
printf '%s' "$KEY" | "$LAIB" secret set MODEL_API_KEY >/dev/null
mkdir -p "$LAIB_HOME/registries/foundry-chronicle/templates"
cp -r "$ROOT/templates/chronik" "$LAIB_HOME/registries/foundry-chronicle/templates/"

echo "--- laib add chronik: vorschlagen, freigeben, anwenden"
"$LAIB" add chronik "IMAGE=$IMAGE" WHISPER_IMAGE=lscr.io/linuxserver/faster-whisper:latest \
  WHISPER_MODEL=tiny WHISPER_DEVICE=cpu WHISPER_COMPUTE=int8 "chronik ci" \
  || fail "laib add chronik: apply exit $?"
"$LAIB" status --json | python3 -c 'import sys,json; r=json.load(sys.stdin)["ready"]; assert r["state"]=="bereit" and r["red"]==0, r; print("status:", r)'

echo "--- Quadlet -dryrun, wie laibs CI: Podman 5 wie auf Fedora CoreOS"
docker run --rm -e QUADLET_UNIT_DIRS=/q -v "$LAIB_QUADLET/chronik:/q:ro" quay.io/podman/stable:v5 \
  /usr/libexec/podman/quadlet -dryrun -user > "$WORK/quadlet.out" || { cat "$WORK/quadlet.out"; fail "quadlet -dryrun"; }
grep -q 'nvidia.com/gpu=all' "$WORK/quadlet.out" || fail "quadlet: no GPU device on the recogniser"

echo "--- das Install-Gate"
curl -fsS http://127.0.0.1:8701/healthz | grep -q '"ok"' || fail "/healthz"

echo "--- der Bot spricht beim Start den Lease-Vertrag"
for i in $(seq 1 60); do grep -q '"DELETE"' "$LEASE_LOG" 2>/dev/null && break; sleep 2; done
cat "$LEASE_LOG"
python3 - "$LEASE_LOG" <<'PY'
import json, sys
calls = [json.loads(l) for l in open(sys.argv[1])]
verbs = [c["method"] for c in calls]
assert verbs[:2] == ["GET", "DELETE"], verbs
assert all(c["path"] == "/api/model-lease" for c in calls), calls
delete = calls[1]
assert delete["bearer_ok"], "DELETE without the model key as Bearer"
assert delete["body"] == {"holder": "foundry-chronicle"}, delete
assert "POST" not in verbs, "nothing asked for a window at start"
print("lease: GET, then DELETE with the key and the holder")
PY

echo "--- der Erkenner: erst bereit, dann nur mit Schlüssel"
for i in $(seq 1 150); do curl -fsS http://127.0.0.1:10301/healthz >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://127.0.0.1:10301/healthz | grep -q '"ok"' || fail "the recogniser never answered /healthz"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"path":"x.wav"}' http://127.0.0.1:10301/transcribe)"
[ "$code" = 401 ] || fail "the recogniser answered $code without its key"

echo "--- eine echte Spur, aus dem Container des Bots verschriftet"
podman exec systemd-chronik espeak-ng -v de -w /aufnahmen/ci-probe.wav \
  "Guten Abend. Die Runde steigt hinab in den Keller unter der Mühle."
podman exec -i systemd-chronik python3 - <<'PY'
import json
from pathlib import Path
from chronicle.config import Config
from chronicle.transcribe.client import WhisperBatch
segs = list(WhisperBatch(Config.from_env()).transcribe(Path("/aufnahmen/ci-probe.wav"), sprache="de"))
print(json.dumps([s.__dict__ for s in segs], ensure_ascii=False))
assert segs, "no segments"
assert all(s.end > s.start >= 0 for s in segs), segs
assert any(s.text.strip() for s in segs), segs
PY

echo "--- die Ansage aus dem Container des Bots, gesprochen von Piper"
ok=""
for i in $(seq 1 60); do
  if podman exec -i systemd-chronik python3 - <<'PY'
import os, wave
from pathlib import Path
from chronicle.bot import ansage
ziel = Path("/tmp/piper-ci.wav")
ansage._piper("Ab jetzt wird aufgezeichnet.", ziel, basis=os.environ["TTS_URL"])
with wave.open(str(ziel)) as w:
    assert w.getsampwidth() == 2 and w.getnframes() > 0, (w.getsampwidth(), w.getnframes())
    print("piper:", w.getframerate(), "Hz,", w.getnchannels(), "Kanal,", w.getnframes(), "Rahmen")
PY
  then ok=1; break; fi
  sleep 5
done
[ -n "$ok" ] || { podman logs --tail 30 piper-ci; fail "Piper never spoke to the bot's container"; }

echo "--- kein Geheimnis auf einer Befehlszeile (laib #224)"
WKEY="$(podman secret inspect --showsecret --format '{{.SecretData}}' laib.chronik.chronik.CHRONICLE_WHISPER_KEY)"
[ -n "$WKEY" ] || fail "the recogniser key is no podman secret"
for c in systemd-chronik systemd-chronik-whisper; do
  argv="$(podman inspect "$c" --format '{{.Config.CreateCommand}}')"
  for s in "$KEY" "$TOKEN" "$WKEY"; do
    case "$argv" in *"$s"*) fail "a secret is on $c's argv";; esac
  done
  if grep -rqF -e "$KEY" -e "$TOKEN" -e "$WKEY" "$LAIB_QUADLET/chronik"; then fail "a secret is in a unit file"; fi
done

echo "laib start ok: chronik and chronik-whisper came up under real Quadlet, answered their checks, transcribed a real track with timestamps, reached Piper, and spoke the lease contract"
