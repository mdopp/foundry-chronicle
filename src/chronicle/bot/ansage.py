"""Die hörbare Ansage — erzeugt aus dem Text, der hier steht.

Der Wortlaut ist die Quelle, die Audiodatei ist abgeleitet. Deshalb wird sie beim ersten
Bedarf aus dem Text erzeugt und unter seinem Fingerabdruck abgelegt: wer den Text ändert,
bekommt zwangsläufig eine neue Datei, und Ansage und Protokolleintrag können nicht
auseinanderlaufen. Eine mitgelieferte Audiodatei wäre genau diese Falle.

**Gesprochen wird kurz.** Die Ansage trägt, was sie tragen muss — dass ab jetzt
aufgezeichnet wird und wie man das abwendet —, und verweist für alles Weitere auf den
Kanal: dort steht die Vorstellung des Bots mit Zweck, Frist und Geltungsbereich, und zwar
*bevor* die Ansage läuft. Was die Runde gelesen hat, muss sie nicht auch noch anhören.

Damit das Einwilligungsprotokoll trotzdem belegt, **worüber** eingewilligt wurde, steht
neben dem gesprochenen Satz ``bedingungen_fuer``; im Protokoll steht beides zusammen als
``protokoll_fuer``. Ein Eintrag, der auf einen Text draußen verweist, wäre wertlos,
sobald sich der ändert.

**Der Wortlaut folgt der Runde und nicht der Bedienoberfläche** (#268). Die Ansage ist
kein Hinweistext, sondern der Vorgang, der das Aufzeichnen zulässig macht — und das tut
sie nur, wenn die Anwesenden sie verstehen. Die Sätze stehen deshalb je Sprache in
``chronicle.sprache``.

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

from chronicle import sprache as sprachen
from chronicle.bot import BotFehler
from chronicle.config import DEFAULT_TTS_URL
from chronicle.recordings import RETENTION_TAGE

logger = logging.getLogger(__name__)

RATE = 48000
KANAELE = 2
BREITE = 2

ESPEAK = "espeak-ng"

TTS_PFAD = "/v1/audio/speech"
TTS_MODELL = "kokoro"

# Box-gemessen 0,1 bis 0,5 s für diese Länge. Zehn Sekunden sind keine Geduldsprobe,
# sondern die Grenze, hinter der espeak-ng schneller fertig ist als der Dienst.
TTS_TIMEOUT = 10.0

WAV_KOPF = b"RIFF"

PRAEFIX = "ansage-"

NICHT_INSTALLIERT = (
    f"{ESPEAK} is not installed — without an announcement nothing is recorded. "
    "The image ships it; locally, install it with: apt install espeak-ng"
)

KEIN_16_BIT = "The spoken announcement did not arrive in 16 bit — it is unusable that way."


# Der Wortlaut steht in ``chronicle.sprache`` — je Sprache einer, und dort steht auch,
# warum er der Runde folgt und nicht der Bedienoberfläche. Hier bleibt nur, wie aus ihm
# eine Datei wird.
def text_fuer(inhaltssprache: str) -> str:
    return sprachen.ANSAGE[sprachen.zurechtgelegt(inhaltssprache)]


def bedingungen_fuer(inhaltssprache: str) -> str:
    """Die Bedingungen mit der Frist, die ``recordings.sweep`` wirklich durchsetzt.

    Die Zahl wird hier eingesetzt und steht nicht im Satz: sie zweimal zu schreiben wäre
    die teuerste Art von Fehler — eine Zusage an Menschen, die sich still von dem
    entfernt, was die Maschine tut.
    """
    vorlage = sprachen.BEDINGUNGEN[sprachen.zurechtgelegt(inhaltssprache)]
    return vorlage.format(tage=RETENTION_TAGE)


def protokoll_fuer(inhaltssprache: str) -> str:
    """Was in die SQLite geht — beides im Wortlaut und in der Sprache, in der es lief."""
    gewaehlt = sprachen.zurechtgelegt(inhaltssprache)
    return sprachen.PROTOKOLL[gewaehlt].format(
        text=text_fuer(gewaehlt), bedingungen=bedingungen_fuer(gewaehlt)
    )


def stimme_fuer(inhaltssprache: str) -> str:
    """Die espeak-ng-Stimme. Der Sprachdienst der Box wählt seine selbst."""
    return sprachen.ESPEAK[sprachen.zurechtgelegt(inhaltssprache)]


Sprecher = Callable[[str, Path], None]


class AnsageFehlt(BotFehler):
    """Ohne hörbare Ansage keine Aufnahme — §201 StGB ist keine Formalie."""


def kennung(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _http_session() -> requests.Session:
    return requests.Session()


def _espeak(text: str, ziel: Path, *, stimme: str) -> None:
    subprocess.run(
        [ESPEAK, "-v", stimme, "-b", "1", "-w", str(ziel)],
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
        raise ValueError("not a WAV file")
    ziel.write_bytes(antwort.content)


def mit_rueckfall(
    basis: str,
    *,
    stimme: str = sprachen.ESPEAK[sprachen.DEFAULT],
    http: Callable[[], object] = _http_session,
) -> Sprecher:
    """Der Dienst spricht; schweigt er, spricht espeak-ng.

    ``stimme`` erreicht nur den Rückfall: der Sprachdienst der Box wählt seine Stimme
    selbst, und ihm eine vorzuschreiben hieße, die Wahl des Betreibers hier ein zweites
    Mal zu treffen. espeak-ng hat keine solche Wahl — ohne ``-v`` läse es einen
    deutschen Satz mit englischer Aussprache vor, und die Ansage muss verstanden werden.
    """

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
            _espeak(text, ziel, stimme=stimme)

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
    inhaltssprache: str = sprachen.DEFAULT,
    text: str | None = None,
    sprecher: Sprecher | None = None,
    tts_url: str | None = None,
) -> Path:
    """Der Pfad der gesprochenen Ansage; erzeugt sie beim ersten Mal.

    Der Dateiname trägt den Fingerabdruck des **Wortlauts**, nicht die Sprachkennung —
    damit liegen die Ansagen zweier Sprachen von selbst nebeneinander, und eine geänderte
    Formulierung bekommt zwangsläufig eine neue Datei. Zwei Runden derselben Sprache
    teilen sie sich; darin steht nichts, was einer von beiden gehörte.
    """
    gesagt = text_fuer(inhaltssprache) if text is None else text
    recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings_dir / f"{PRAEFIX}{kennung(gesagt)}.wav"
    if ziel.is_file():
        return ziel
    spricht = (
        mit_rueckfall(tts_url or DEFAULT_TTS_URL, stimme=stimme_fuer(inhaltssprache))
        if sprecher is None
        else sprecher
    )
    with tempfile.TemporaryDirectory() as ordner:
        roh = Path(ordner) / "roh.wav"
        try:
            spricht(gesagt, roh)
        except (OSError, subprocess.CalledProcessError) as fehler:
            raise AnsageFehlt(NICHT_INSTALLIERT) from fehler
        _umwandeln(roh, ziel)
    return ziel
