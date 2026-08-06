"""Die hörbare Ansage — erzeugt aus dem Text, der hier steht.

Der Wortlaut ist die Quelle, die Audiodatei ist abgeleitet. Deshalb wird sie beim ersten
Bedarf aus dem Text erzeugt und unter seinem Fingerabdruck abgelegt: wer den Text ändert,
bekommt zwangsläufig eine neue Datei, und Ansage und Protokolleintrag können nicht
auseinanderlaufen. Eine mitgelieferte Audiodatei wäre genau diese Falle.

Gesprochen wird mit **espeak-ng**: ein kleines Systempaket statt eines Modells, das
niemand für eine Handvoll Sätze herunterladen will. Ist es nicht da, wird **nicht**
aufgenommen — eine Aufnahme ohne Ansage ist der Fehler, den dieses Modul verhindert.

Discord will 48 kHz, Stereo, 16 Bit; espeak-ng liefert 22050 Hz Mono. Umgerechnet wird
hier von Hand, mit dem nächstgelegenen Abtastwert: ffmpeg gehört nicht ins Image, und für
eine einmal je Wortlaut erzeugte Ansage ist das hörbar genug.

Die Datei liegt im Aufnahmeverzeichnis. Sie ist jederzeit neu erzeugbar und gehört damit
genau dorthin — zu dem, was nicht ins Backup muss.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import wave
from array import array
from collections.abc import Callable
from pathlib import Path

from chronicle.bot import BotFehler
from chronicle.recordings import RETENTION_TAGE

# Die Frist im Satz kommt aus derselben Zahl, die ``recordings.sweep`` durchsetzt. Sie hier
# hineinzuschreiben wäre die teuerste Art von Fehler: eine Zusage an Menschen, die sich
# still von dem entfernt, was die Maschine tut.
TEXT = (
    "Hier spricht der Chronik-Bot. Ab jetzt wird dieses Gespräch aufgezeichnet, "
    "für jede und jeden im Kanal eine eigene Tonspur. "
    "Die Aufnahmen dienen ausschließlich dem Sitzungsprotokoll dieser Spielrunde, "
    "sie werden auf dem Server der Gruppe verarbeitet, "
    f"höchstens {RETENTION_TAGE} Tage aufbewahrt und dann gelöscht. "
    "Wer nicht aufgezeichnet werden möchte, verlässt jetzt bitte den Sprachkanal. "
    "Außerhalb dieses Kanals wird nichts aufgenommen. "
    "Wer im Kanal bleibt, ist mit der Aufnahme einverstanden. "
    "Die Aufnahme beginnt nach dieser Ansage."
)

RATE = 48000
KANAELE = 2
BREITE = 2

ESPEAK = "espeak-ng"
STIMME = "de"

PRAEFIX = "ansage-"

NICHT_INSTALLIERT = (
    f"{ESPEAK} ist nicht installiert — ohne Ansage wird nicht aufgenommen. "
    "Im Image ist es dabei, lokal nachrüsten mit: apt install espeak-ng"
)

KEIN_16_BIT = f"{ESPEAK} hat keine 16-Bit-Spur geliefert — die Ansage ist nicht brauchbar."

Sprecher = Callable[[str, Path], None]


class AnsageFehlt(BotFehler):
    """Ohne hörbare Ansage keine Aufnahme — §201 StGB ist keine Formalie."""


def kennung(text: str = TEXT) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _espeak(text: str, ziel: Path) -> None:
    subprocess.run(
        [ESPEAK, "-v", STIMME, "-b", "1", "-w", str(ziel)],
        input=text.encode("utf-8"),
        capture_output=True,
        check=True,
    )


def zu_discord(frames: bytes, rate: int, kanaele: int) -> bytes:
    """22050 Hz Mono wird 48 kHz Stereo — nächster Nachbar, ohne fremde Bibliothek."""
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


def datei(recordings_dir: Path, *, text: str = TEXT, sprecher: Sprecher = _espeak) -> Path:
    """Der Pfad der gesprochenen Ansage; erzeugt sie beim ersten Mal."""
    recordings_dir.mkdir(parents=True, exist_ok=True)
    ziel = recordings_dir / f"{PRAEFIX}{kennung(text)}.wav"
    if ziel.is_file():
        return ziel
    with tempfile.TemporaryDirectory() as ordner:
        roh = Path(ordner) / "roh.wav"
        try:
            sprecher(text, roh)
        except (OSError, subprocess.CalledProcessError) as fehler:
            raise AnsageFehlt(NICHT_INSTALLIERT) from fehler
        _umwandeln(roh, ziel)
    return ziel
