"""Die Warteschlange, während gespielt wird — verschriftet wird nicht erst nachts.

Seit #217 schneidet der Mitschnitt eine Sprecherspur in Häppchen und reiht jedes einzeln
ein, während die Sitzung noch läuft. **Abgearbeitet hat sie dabei niemand**: das taten der
Nachtlauf und der Abschluss, und in ``chronicle.bot`` stand kein einziger Aufruf von
``run_queue``. Der Docstring von damals sagte, die Verschriftung könne nun schon während
der Sitzung laufen — können, nicht tun. Dies ist die zweite Hälfte (#269): ein Faden, der
regelmäßig in die Warteschlange jeder Runde sieht und verschriftet, was neu darin steht.
Der Gewinn ist der Unterschied zwischen »morgen früh steht die Chronik« und »sie steht,
wenn ihr aufhört«.

**Er liegt neben der Ereignisschleife, nicht auf ihr.** Dasselbe Fadenmuster wie
``nightly.starten`` und aus demselben Grund — ein zweites daneben gibt es nicht: eine
Verschriftung dauert Minuten, und in der Zeit bliebe der Herzschlag zu Discord aus. Der
Bot fiele mitten in der Sitzung vom Gateway, und zwar während er sie mitschneidet.

**Er redet mit dem Erkenner nie gleichzeitig mit jemand anderem.** Das Schloss dafür steht
in ``transcribe.service`` und gilt allen vier Wegen dorthin; hier ist nur wichtig, dass
dieser Faden warten darf, ohne dass etwas anderes stehenbleibt.

**Er sagt nichts.** Ein verschriftetes Häppchen ist Material, keine Erzählung. 240 Zeilen
»Spur X: 12 Segmente« während des Spiels wären eine Wand — und eine Wand, die aussähe, als
entstünde dabei schon die Chronik, während sie in Wahrheit noch gar nicht geschrieben ist.
Zu #265 (»der Bot führt, statt auf Befehle zu warten«): angeboten wird, was es anzubieten
gibt, und ein halb verschrifteter Abend gehört nicht dazu. Sichtbar wird weiterhin erst,
was am Ende steht — nur eben früher. Ein neuer Slash-Befehl entsteht dafür nicht: der
Moment, an dem etwas zu tun ist, steht in der Warteschlange, und den erkennt der Faden
selbst.

**Ein Fehlschlag bleibt der Nacht.** Wiederholt wird hier nichts (``mitlaufend`` in
``run_queue``): eine Spur, die einmal gescheitert ist, hätte sonst ihre drei Anläufe aus
#247 binnen drei Minuten verbraucht, und der Zähler stünde für etwas anderes als das, was
neben ihm steht. Die Schlange hält das nicht auf — die gescheiterte Spur wird beim
nächsten Blick übergangen, die übrigen laufen weiter.
"""

from __future__ import annotations

import logging
import threading
import time

from chronicle import runde as runden
from chronicle.config import Config
from chronicle.transcribe.service import run_queue

logger = logging.getLogger(__name__)

# So oft wird in die Warteschlange gesehen. Feiner brächte nichts: ein Häppchen ist fünf
# Minuten lang, und schneller als es entsteht, kann es nicht verschriftet werden.
INTERVALL = 60.0

# Nach wie vielen Blicken der Faden sagt, dass es ihn noch gibt — bei ``INTERVALL`` also
# eine Viertelstunde. Derselbe Grund wie beim Nachtlauf (#237): ein Blick, der nichts
# findet, schreibt nichts, und ein stillgestorbener Faden sähe zwischen zwei Sitzungen
# genauso aus wie ein gesunder.
LEBENSZEICHEN = 15

WACH = "Mitlauf: der Faden lebt — %d Blicke in die Warteschlange seit dem Start."


@runden.instanzweit
def tick(config: Config) -> None:
    """Ein Blick in die Warteschlange jeder Runde — der Test ruft ihn von Hand.

    Gefragt wird jede Runde und nicht nur die, die gerade mitschneidet: was in der
    Warteschlange steht, steht dort unabhängig davon, wer es eingereiht hat, und ein
    Diktat, das um zehn ankommt, hat keinen Grund, bis vier Uhr früh zu warten. Eine
    ruhende Runde fällt in ``run_queue`` heraus, wo diese Frage ohnehin schon steht.
    """
    for eine in runden.alle(config.database_path):
        run_queue(config, eine, mitlaufend=True)


@runden.instanzweit
def betreiben(config: Config, *, schlafen=time.sleep, weiter=lambda: True) -> None:
    """Der Blick in die Warteschlange, immer wieder — und dazwischen ein Lebenszeichen."""
    blicke = 0
    while weiter():
        if blicke % LEBENSZEICHEN == 0:
            logger.info(WACH, blicke)
        blicke += 1
        try:
            tick(config)
        # Ein gescheiterter Blick darf den Faden nicht beenden — sonst liefe der Dienst
        # weiter und verschriftete nie wieder etwas während einer Sitzung.
        except Exception as fehler:  # noqa: BLE001
            logger.warning("Mitlauf konnte die Warteschlange nicht abarbeiten: %s", fehler)
        schlafen(INTERVALL)


@runden.instanzweit
def starten(config: Config) -> threading.Thread:
    faden = threading.Thread(target=betreiben, args=(config,), daemon=True, name="mitlauf")
    faden.start()
    return faden
