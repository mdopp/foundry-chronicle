"""Die hörbare Ansage — erzeugt aus dem Text, der hier steht.

Der Wortlaut ist die Quelle, die Audiodatei ist abgeleitet. Deshalb wird sie beim ersten
Bedarf aus dem Text erzeugt und unter seinem Fingerabdruck abgelegt: wer den Text ändert,
bekommt zwangsläufig eine neue Datei, und Ansage und Protokolleintrag können nicht
auseinanderlaufen. Eine mitgelieferte Audiodatei wäre genau diese Falle.

**Gesprochen wird kurz.** Die Ansage trägt, was sie tragen muss — dass ab jetzt
aufgezeichnet wird und wie man das abwendet —, und verweist für alles Weitere auf den
Kanal: dort steht die Vorstellung des Bots mit Zweck, Frist und Geltungsbereich, und zwar
*bevor* die Ansage läuft. Was die Runde gelesen hat, muss sie nicht auch noch anhören.

Damit das Einwilligungsprotokoll trotzdem belegt, **worüber** eingewilligt wurde, liegt
neben dem gesprochenen Satz ``BEDINGUNGEN``; im Protokoll steht beides zusammen als
``PROTOKOLL``. Ein Eintrag, der auf einen Text draußen verweist, wäre wertlos, sobald sich
der ändert.

Gesprochen wird vom **Sprachdienst der Box** (Kokoro, OpenAI-kompatibles ``/v1/audio``);
antwortet der nicht, spricht **espeak-ng**. Die Reihenfolge ist die ganze Abwägung: eine
Ansage, die gar nicht kommt, verhindert die Aufnahme — das ist teurer als eine hässliche
Stimme. Fehlt am Ende auch espeak-ng, wird **nicht** aufgenommen.

Discord will 48 kHz, Stereo, 16 Bit; der Dienst liefert 24 kHz Mono, espeak-ng 22050 Hz
Mono. Beides ist eine WAV-Datei und geht durch dieselbe Umrechnung, von Hand mit dem
nächstgelegenen Abtastwert: ffmpeg gehört nicht ins Image, und für eine einmal je Wortlaut
erzeugte Ansage ist das hörbar genug.

Die Datei liegt im Aufnahmeverzeichnis. Sie ist jederzeit neu erzeugbar und gehört damit
genau dorthin — zu dem, was nicht ins Backup muss.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import tempfile
import wave
from array import array
from collections.abc import Callable
from pathlib import Path

import requests

from chronicle.bot import BotFehler
from chronicle.config import DEFAULT_TTS_URL
from chronicle.recordings import RETENTION_TAGE

logger = logging.getLogger(__name__)

# Vier Sätze, weil die Runde wartet, während sie laufen. Alles, was hier fehlt, steht als
# ``gateway.VORSTELLUNG`` im Kanal — geschrieben, vor der Ansage, in Ruhe zu lesen.
TEXT = (
    "Hier spricht der Chronik-Bot. Ab jetzt wird dieses Gespräch aufgezeichnet. "
    "Wer das nicht möchte, verlässt jetzt bitte den Sprachkanal. "
    "Die Einzelheiten stehen im Kanal."
)

# Die Frist im Satz kommt aus derselben Zahl, die ``recordings.sweep`` durchsetzt. Sie hier
# hineinzuschreiben wäre die teuerste Art von Fehler: eine Zusage an Menschen, die sich
# still von dem entfernt, was die Maschine tut.
BEDINGUNGEN = (
    "Aufgenommen wird nur dieser Sprachkanal, für jede und jeden eine eigene Tonspur. "
    "Die Aufnahmen dienen ausschließlich dem Sitzungsprotokoll dieser Spielrunde, "
    "sie werden auf dem Server der Gruppe verarbeitet, "
    f"höchstens {RETENTION_TAGE} Tage aufbewahrt und dann gelöscht. "
    "Wer im Kanal bleibt, ist mit der Aufnahme einverstanden."
)

# Was in die SQLite geht. Der gesprochene Satz allein belegte nur, *dass* angesagt wurde;
# die Bedingungen dahinter belegen, *worüber* eingewilligt wurde — beide im Wortlaut, wie
# er zum Zeitpunkt der Ansage galt.
PROTOKOLL = f"Gesprochen: {TEXT}\nBedingungen, auf die die Ansage verweist: {BEDINGUNGEN}"

RATE = 48000
KANAELE = 2
BREITE = 2

ESPEAK = "espeak-ng"
STIMME = "de"

TTS_PFAD = "/v1/audio/speech"
TTS_MODELL = "kokoro"

# Box-gemessen 0,1 bis 0,5 s für diese Länge. Zehn Sekunden sind keine Geduldsprobe,
# sondern die Grenze, hinter der espeak-ng schneller fertig ist als der Dienst.
TTS_TIMEOUT = 10.0

WAV_KOPF = b"RIFF"

PRAEFIX = "ansage-"

NICHT_INSTALLIERT = (
    f"{ESPEAK} ist nicht installiert — ohne Ansage wird nicht aufgenommen. "
    "Im Image ist es dabei, lokal nachrüsten mit: apt install espeak-ng"
)

KEIN_16_BIT = "Die gesprochene Ansage kam nicht in 16 Bit — so ist sie nicht brauchbar."

Sprecher = Callable[[str, Path], None]


class AnsageFehlt(BotFehler):
    """Ohne hörbare Ansage keine Aufnahme — §201 StGB ist keine Formalie."""


def kennung(text: str = TEXT) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _http_session() -> requests.Session:
    return requests.Session()


def _espeak(text: str, ziel: Path) -> None:
    subprocess.run(
        [ESPEAK, "-v", STIMME, "-b", "1", "-w", str(ziel)],
        input=text.encode("utf-8"),
        capture_output=True,
        check=True,
    )


def _kokoro(
    text: str,
    ziel: Path,
    *,
    basis: str,
    http: Callable[[], object] = _http_session,
    timeout: float = TTS_TIMEOUT,
) -> None:
    """Ein Aufruf, eine WAV-Datei.

    Ohne ``voice``: der Dienst trägt seine Stimme in der eigenen Umgebung und ignoriert das
    Feld. Einen Namen mitzuschicken hieße, die Wahl des Betreibers hier ein zweites Mal zu
    treffen — und beim nächsten Wechsel dort läge sie hier falsch.
    """
    antwort = http().post(
        basis.rstrip("/") + TTS_PFAD,
        json={"model": TTS_MODELL, "input": text, "response_format": "wav"},
        timeout=timeout,
    )
    antwort.raise_for_status()
    if not antwort.content.startswith(WAV_KOPF):
        raise ValueError("keine WAV-Datei")
    ziel.write_bytes(antwort.content)


def mit_rueckfall(basis: str, *, http: Callable[[], object] = _http_session) -> Sprecher:
    """Der Dienst spricht; schweigt er, spricht espeak-ng."""

    def sprich(text: str, ziel: Path) -> None:
        try:
            _kokoro(text, ziel, basis=basis, http=http)
        except (requests.RequestException, ValueError) as fehler:
            logger.warning(
                "Der Sprachdienst auf %s antwortet nicht (%s) — die Ansage spricht %s.",
                basis,
                type(fehler).__name__,
                ESPEAK,
            )
            _espeak(text, ziel)

    return sprich


def zu_discord(frames: bytes, rate: int, kanaele: int) -> bytes:
    """Mono wird 48 kHz Stereo — nächster Nachbar, ohne fremde Bibliothek."""
    quelle = array("h", frames)
    if kanaele > 1:
        quelle = quelle[::kanaele]
    anzahl = len(quelle) * RATE // rate
    ziel = array("h", bytes(BREITE * KANAELE * anzahl))
    for stelle in range(anzahl):
        wert = quelle[stelle * rate // RATE]
        ziel[KANAELE * stelle] = wert
        ziel[KANAELE * stelle + 1] = wert
    return ziel.tobytes()


def _umwandeln(quelle: Path, ziel: Path) -> None:
    with wave.open(str(quelle), "rb") as gelesen:
        if gelesen.getsampwidth() != BREITE:
            raise AnsageFehlt(KEIN_16_BIT)
        pcm = zu_discord(
            gelesen.readframes(gelesen.getnframes()),
            gelesen.getframerate(),
            gelesen.getnchannels(),
        )
    # Erst daneben, dann umbenennen: eine abgebrochene Erzeugung darf nicht als fertige
    # Ansage liegenbleiben und beim nächsten Lauf halb abgespielt werden.
    teil = ziel.with_name(ziel.name + ".teil")
    with wave.open(str(teil), "wb") as geschrieben:
        geschrieben.setnchannels(KANAELE)
        geschrieben.setsampwidth(BREITE)
        geschrieben.setframerate(RATE)
        geschrieben.writeframes(pcm)
    teil.replace(ziel)


def datei(
    recordings_dir: Path,
    *,
    text: str = TEXT,
    sprecher: Sprecher | None = None,
    tts_url: str | None = None,
) -> Path:
    """Der Pfad der gesprochenen Ansage; erzeugt sie beim ersten Mal."""
    recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings_dir / f"{PRAEFIX}{kennung(text)}.wav"
    if ziel.is_file():
        return ziel
    spricht = mit_rueckfall(tts_url or DEFAULT_TTS_URL) if sprecher is None else sprecher
    with tempfile.TemporaryDirectory() as ordner:
        roh = Path(ordner) / "roh.wav"
        try:
            spricht(text, roh)
        except (OSError, subprocess.CalledProcessError) as fehler:
            raise AnsageFehlt(NICHT_INSTALLIERT) from fehler
        _umwandeln(roh, ziel)
    return ziel
