"""python -m chronicle.bot — der Aufnahme-Bot, ein eigener, dauerhafter Prozess.

Anders als ``python -m chronicle.discord`` ist das kein Stapellauf: der Bot hält eine
Gateway-Verbindung, weil Sprache nur mitgeschnitten werden kann, während sie gesprochen
wird. Er läuft deshalb neben dem Webdienst, nicht in ihm.

Ohne Token startet er nicht und sagt das in einem Satz — der Token wird unter
``/einstellungen`` gepflegt und ein dort gesetzter Wert schlägt die Umgebung.

Seit #228 bedient dieser Prozess außerdem ``/healthz``, das Install-Gate der Box. Das
ändert, was »liegen bleiben« heißt: bisher endete der Bot ohne Token mit 0 und der
Container blieb aus. Am Gate hängt jetzt die Installation, und ein fehlender Token ist
bei der Erstinstallation der Normalfall — also wird der Prozess in genau diesen Fällen
**nicht** beendet, sondern bleibt liegen und antwortet weiter. Kein Neustart, kein
zweiter Anmeldeversuch; die Zusage aus ``BotHaelt`` bleibt damit unverändert, sie wird
nur nicht mehr über den Rückgabewert eingelöst.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from chronicle import db, settings
from chronicle import runde as runden
from chronicle.bot import BotFehler, BotHaelt
from chronicle.config import Config

KEIN_TOKEN = (
    "Kein Discord-Bot-Token — der Aufnahme-Bot startet nicht. "
    "Token unter /einstellungen setzen, dann den Bot neu starten."
)


def _gateway() -> Callable[[Config], None]:
    from chronicle.bot.gateway import run

    return run


def _gesundheit(config: Config):
    """Das Install-Gate, sofern die Vorlage einen Port dafür nennt — sonst nichts.

    Ohne Port bindet ``python -m chronicle.bot`` nichts: auf einer Entwicklungsmaschine
    ist ein belegter Port lästig, und ein Gate braucht dort niemand.
    """
    if config.health_port is None:
        return None
    from chronicle.bot import healthz

    return healthz.starten(config.health_port)


def _liegenbleiben(server) -> int:
    """Gesagt ist gesagt — aber erreichbar bleiben, denn am Gate hängt die Installation.

    Ohne Gate endet der Prozess wie bisher mit 0, und ``restartPolicy: OnFailure`` lässt
    ihn liegen. Mit Gate wäre genau das der Fehlschlag: der Poller fände niemanden, und
    ein Dienst ohne Bot-Token gilt bei der Erstinstallation als krank, statt auf seinen
    Token zu warten. Verbunden wird deshalb trotzdem nichts mehr.
    """
    if server is not None:
        server.warten()
    return 0


def main(
    argv: list[str] | None = None,
    *,
    gateway: Callable[[], Callable] = _gateway,
    gesundheit: Callable[[Config], object | None] = _gesundheit,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = Config.from_env()
    db.init(config.database_path)
    server = gesundheit(config)
    try:
        zugang = settings.effective(config, runden.erste(config.database_path))
        if not zugang.discord_configured:
            print(KEIN_TOKEN)
            return _liegenbleiben(server)
        try:
            gateway()(zugang)
        except BotHaelt as fehler:
            # Kein Absturz, sondern eine Entscheidung: hier hilft nur ein Mensch, und ein
            # Neustart wäre bloß der nächste Anmeldeversuch — siehe BotHaelt.
            print(str(fehler))
            return _liegenbleiben(server)
        except BotFehler as fehler:
            print(str(fehler))
            return 2
        return 0
    finally:
        if server is not None:
            server.schliessen()


if __name__ == "__main__":
    sys.exit(main())
