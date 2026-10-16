"""Verschriftung mit Zeitstempeln für die Chronik — der zweite Container der Vorlage (#362).

``POST /transcribe {"path", "language", "hotwords": [...], "vad": true}`` vor einem
faster-whisper-Modell, das nur auf der Karte liegt, solange es Arbeit gibt. Übernommen aus
``solaris-whisper-batch`` (``mdopp/solarisbay#1161``, #1204, #1259), das nur uns diente und
deshalb mit uns zu Laib umzieht. Laibs eigener Whisper in der Stimme spricht Wyoming und
liefert einen Text ohne Zeitstempel; die Chronik legt die Spuren der Sprecher nach Zeit
zusammen und braucht sie.

Das Abbild ist das unveränderte ``lscr.io/linuxserver/faster-whisper`` (``:gpu`` auf der
Karte, ``:latest`` ohne) — es bringt faster-whisper, CTranslate2 und CUDA mit. Diese Datei
liegt im Dienstordner und wird schreibgeschützt eingehängt; ein eigenes Abbild gibt es
nicht.

Was sich gegenüber dem Vorgänger geändert hat, und warum:

- **Ein Schlüssel** (``WHISPER_BATCH_KEY``, ``Authorization: Bearer``). Der Vorgänger lag im
  Host-Netz auf der Schleife; dieser Port steht jedem Pod der Box offen (``lan: false``
  hält nur das LAN fern), und wer ihn erreicht, könnte jede Spur verschriften lassen. Ohne
  Schlüssel startet er nicht.
- **``GET /healthz``**, damit der Container eine Prüfung hat, die nichts verschriftet.
- **``WHISPER_BATCH_DEVICE``**: ``cuda`` auf der Box, ``cpu`` im CI-Lauf, der die Vorlage
  wirklich startet.
- Die CUDA-Bibliotheken des ``:gpu``-Abbilds stehen auf keinem Bibliothekspfad (#1162);
  der Vorgänger setzte sie in einem s6-Skript, hier bekommt sie der Arbeitsprozess.

Nichts von den Daten der Runde berührt die Platte: die Spur wird an Ort und Stelle gelesen,
die Segmente leben bis zur Antwort im Speicher, und das Zugriffsprotokoll nennt Methode und
Endpunkt, nie den Pfad — der trägt den Namen eines Sprechers — und nie den Text.
"""

import dataclasses
import hmac
import importlib.util
import inspect
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("WHISPER_BATCH_ROOT", "")
PORT = int(os.environ.get("WHISPER_BATCH_PORT") or 0)
KEY = os.environ.get("WHISPER_BATCH_KEY", "")
MODEL = os.environ.get("WHISPER_BATCH_MODEL", "large-v3-turbo")
DEVICE = os.environ.get("WHISPER_BATCH_DEVICE", "cuda")
COMPUTE = os.environ.get("WHISPER_BATCH_COMPUTE", "float16")
BEAM = int(os.environ.get("WHISPER_BATCH_BEAM") or 1)
CACHE = os.environ.get("WHISPER_BATCH_CACHE", "/config")
IDLE_S = float(os.environ.get("WHISPER_BATCH_IDLE_S") or 300)
IDLE_TICK_S = 15.0
WORKER_FLAG = "--worker"
NAME = "chronik-whisper"

# Silero für einen Tisch, an dem Menschen zehn Minuten am Stück reden (#1204):
# faster-whispers eigene Vorgaben, festgeschrieben statt geerbt.
VAD_PARAMETERS = {
    "threshold": 0.5,
    "min_silence_duration_ms": 2000,
    "speech_pad_ms": 400,
}

_loaded = {}
_worker = {"proc": None, "used": 0.0}
_worker_lock = threading.Lock()


def log(text):
    sys.stderr.write(f"{NAME}: {text}\n")
    sys.stderr.flush()


def shape_problem():
    """Warum dieser Dienst nicht liefern kann, was er zusagt — oder ''.

    Das Abbild aktualisiert sich selbst; faster-whisper bewegt sich darunter. Geprüft wird,
    worauf ein Aufrufer sich verlässt: Hotwords, Stillenerkennung samt Zahl, Zeitstempel.
    """
    import faster_whisper
    from faster_whisper.transcribe import Segment, TranscriptionInfo

    params = inspect.signature(faster_whisper.WhisperModel.transcribe).parameters
    for needed in ("hotwords", "vad_filter", "vad_parameters"):
        if needed not in params:
            return f"faster_whisper.WhisperModel.transcribe takes no {needed}"
    if not callable(getattr(faster_whisper, "download_model", None)):
        return "faster_whisper no longer exposes download_model"
    info = {f.name for f in dataclasses.fields(TranscriptionInfo)}
    if not {"duration", "duration_after_vad"} <= info:
        return "faster_whisper TranscriptionInfo no longer reports duration_after_vad"
    source = inspect.getsource(faster_whisper.WhisperModel)
    for needed in ("self.max_length", "self.hf_tokenizer"):
        if needed not in source:
            return f"faster_whisper.WhisperModel has no {needed}"
    fields = set(getattr(Segment, "_fields", ()) or ())
    if not fields and dataclasses.is_dataclass(Segment):
        fields = {f.name for f in dataclasses.fields(Segment)}
    if not {"start", "end", "text"} <= fields:
        return "faster_whisper Segment no longer carries start/end/text"
    return ""


def fit_hotwords(model, words):
    """Ganze Namen, die ins Hotword-Budget passen, und die, die nicht passen.

    faster-whisper kürzt die Token-Liste und schneidet dabei einen Namen in der Mitte
    durch, ohne es zu sagen (foundry-chronicle#141). Deshalb fallen hier ganze Namen, und
    der Aufrufer erfährt, wie viele.
    """
    budget = model.max_length // 2 - 1
    kept = []
    for index, word in enumerate(words):
        candidate = " " + ", ".join(kept + [word])
        encoded = model.hf_tokenizer.encode(candidate, add_special_tokens=False)
        if len(encoded.ids) > budget:
            return kept, list(words[index:])
        kept.append(word)
    return kept, []


def resolve_path(candidate):
    """Die angefragte Spur, oder '' wenn sie keine echte Datei unter der Wurzel ist.

    Erst ``realpath``: es lässt ``../`` und Symlinks außerhalb landen und abweisen statt
    folgen.
    """
    if not ROOT or not candidate:
        return ""
    root = os.path.realpath(ROOT)
    full = os.path.realpath(os.path.join(root, candidate))
    if full != root and not full.startswith(root + os.sep):
        return ""
    return full if os.path.isfile(full) else ""


def authorized(header):
    """Der Schlüssel als Bearer, verglichen in konstanter Zeit."""
    if not KEY:
        return False
    scheme, _, token = (header or "").strip().partition(" ")
    return scheme == "Bearer" and hmac.compare_digest(token.strip().encode(), KEY.encode())


def transcribe_file(model, path, language, hotwords, vad):
    """Segmente einer Datei, gemessen ab **ihrem** Anfang, und wie viel Stille fiel.

    Zustandslos: der Aufrufer teilt eine Sitzung in Stücke und rechnet ihren Versatz selbst
    hinzu. Ein Versatz hier stapelte alle Spuren auf den Sitzungsbeginn.
    """
    segments, info = model.transcribe(
        path,
        language=language,
        beam_size=BEAM,
        hotwords=", ".join(hotwords) or None,
        vad_filter=vad,
        vad_parameters=VAD_PARAMETERS,
    )
    return (
        [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
        round(info.duration, 3),
        round(info.duration - info.duration_after_vad, 3),
    )


def run_one(model, job):
    kept, dropped = fit_hotwords(model, job.get("hotwords") or [])
    vad = bool(job.get("vad", True))
    segments, audio, silence = transcribe_file(model, job["path"], job.get("language"), kept, vad)
    return {
        "segments": segments,
        "hotwords_dropped_count": len(dropped),
        "hotwords_dropped": dropped,
        "vad": vad,
        "audio_seconds": audio,
        "silence_dropped_seconds": silence,
    }


def cuda_library_path():
    """cuBLAS und cuDNN des ``:gpu``-Abbilds, die es auf keinen Bibliothekspfad legt (#1162).

    Ohne sie lädt ``--device cuda`` das Modell in den Speicher und stirbt beim ersten
    Kodieren an ``libcublas.so.12``. Ein stiller Rückfall auf die CPU verdeckte genau das.
    """
    dirs = []
    for pkg, soname in (
        ("nvidia.cublas.lib", "libcublas.so.12"),
        ("nvidia.cudnn.lib", "libcudnn.so.9"),
    ):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            spec = None
        d = pathlib.Path(list(spec.submodule_search_locations)[0]) if spec else None
        if d is None or not (d / soname).exists():
            raise RuntimeError(f"{soname} missing for {pkg}")
        dirs.append(str(d))
    return ":".join(dirs)


def worker_main():
    """Das Kind, das die Karte hält: einmal laden, Aufträge beantworten, bei EOF enden.

    Eine JSON-Zeile hinein, eine heraus; stdout gehört den Antworten. Das Ende des
    Prozesses ist der Zweck: erst das gibt dem Treiber den Speicher zurück (#1259).
    """
    import faster_whisper

    model = faster_whisper.WhisperModel(
        MODEL, device=DEVICE, compute_type=COMPUTE, download_root=CACHE
    )
    log(f"worker loaded {MODEL}/{COMPUTE} on {DEVICE}")
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        if not line.strip():
            continue
        try:
            answer = run_one(model, json.loads(line))
        except Exception as e:  # eine schlechte Datei legt den Arbeiter nicht um
            answer = {"error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(answer) + "\n")
        sys.stdout.flush()


def start_worker():
    env = dict(os.environ)
    if DEVICE == "cuda":
        libs = cuda_library_path()
        env["LD_LIBRARY_PATH"] = libs + (
            ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
        )
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), WORKER_FLAG],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )


def _stop_locked():
    proc = _worker["proc"]
    _worker["proc"] = None
    if proc is None:
        return False
    try:
        proc.stdin.close()
        proc.wait(timeout=30)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        proc.kill()
        proc.wait()
    return True


def reap_idle_worker():
    with _worker_lock:
        if _worker["proc"] is None or time.monotonic() - _worker["used"] < IDLE_S:
            return False
        log(f"idle {IDLE_S:.0f}s, releasing the model")
        return _stop_locked()


def idle_reaper():
    while True:
        time.sleep(IDLE_TICK_S)
        reap_idle_worker()


def run_job(job):
    """Einen Auftrag an den Arbeiter, einer nach dem anderen: ein Modell, eine Karte."""
    with _worker_lock:
        proc = _worker["proc"]
        if proc is None or proc.poll() is not None:
            proc = _worker["proc"] = start_worker()
        try:
            proc.stdin.write(json.dumps(job) + "\n")
            proc.stdin.flush()
            answer = proc.stdout.readline()
        except (OSError, ValueError) as e:
            _stop_locked()
            raise RuntimeError(f"worker went away: {e}") from e
        _worker["used"] = time.monotonic()
        if not answer:
            _stop_locked()
            raise RuntimeError("worker died before it answered")
        return json.loads(answer)


class SegmentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = NAME
    sys_version = ""

    def do_GET(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/healthz":
            self._json(404, {"error": "unknown endpoint"})
            return
        self._json(200, {"status": "ok"})

    def do_POST(self):  # noqa: N802
        if self.path != "/transcribe":
            self._json(404, {"error": "unknown endpoint, expected /transcribe"})
            return
        if not authorized(self.headers.get("Authorization")):
            self._json(401, {"error": "this endpoint wants its key: Authorization: Bearer <key>"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "body must be JSON"})
            return
        if not isinstance(request, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return
        path = resolve_path(str(request.get("path") or ""))
        if not path:
            self._json(403, {"error": "path must be a file under the recordings directory"})
            return
        words = [w for w in (str(x).strip() for x in request.get("hotwords") or []) if w]
        job = {
            "path": path,
            "language": request.get("language"),
            "hotwords": words,
            "vad": bool(request.get("vad", True)),
        }
        try:
            answer = run_job(job)
        except (OSError, ValueError, RuntimeError) as e:
            self._json(503, {"error": f"worker unavailable: {type(e).__name__}: {e}"})
            return
        if "error" in answer:
            self._json(500, answer)
            return
        self._json(200, answer)

    def log_message(self, fmt, *args):
        # Methode und Endpunkt, nie der Pfad einer Spur und nie ihr Text.
        log(f"{self.command} {self.path.split('?', 1)[0]}")

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port=None):
    # Der eigene Netz-Namensraum des Containers; wer ihn erreicht, entscheidet die Vorlage
    # (``PublishPort`` und ``lan: false``) — und dahinter der Schlüssel.
    server = ThreadingHTTPServer(("0.0.0.0", PORT if port is None else port), SegmentHandler)
    server.daemon_threads = True
    _loaded["server"] = server
    return server


def main():
    if not (ROOT and PORT and KEY):
        log("no WHISPER_BATCH_ROOT/PORT/KEY - refusing to start a listener without a key")
        return 1
    import faster_whisper

    problem = shape_problem()
    if problem:
        log(
            f"NOT APPLIED to faster-whisper {faster_whisper.__version__} - {problem}. "
            "Refusing to start rather than serve segments a caller cannot trust."
        )
        return 1
    # Den Zwischenspeicher füllen, bevor gebunden wird, auf der CPU: der erste Download
    # gehört in den Start und nicht in die Anfrage des ersten Aufrufers.
    faster_whisper.download_model(MODEL, cache_dir=CACHE)
    threading.Thread(target=idle_reaper, daemon=True).start()
    log(
        f"faster-whisper {faster_whisper.__version__}, {MODEL}/{COMPUTE} on {DEVICE} per job, "
        f"idle release after {IDLE_S:.0f}s, POST :{PORT}/transcribe, reading {ROOT} read-only"
    )
    serve().serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(worker_main() if WORKER_FLAG in sys.argv[1:] else main())
