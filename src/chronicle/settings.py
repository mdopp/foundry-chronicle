"""Die Werte, die eine Runde pflegt.

Die Umgebung ist die Vorgabe beim ersten Start und bleibt der Deploy-Weg; **ein gepflegter
Wert gewinnt.** Damit das keine zwei Wahrheiten werden, liest kein Aufrufer diese Werte
mehr selbst aus der Umgebung — er nimmt ``effective``.

**Gepflegt wird je Runde.** Foundry-Adresse, Konto und Zustellkanal gehören der Gruppe,
die spielt — was ihr Spiel betrifft. Der Instanz gehört ihre eigene Infrastruktur: der
**Discord-Bot-Token** (unser Token) und **Ollama** (#87 — wohin gesprochenes Wort fließt,
entscheidet nicht eine fremde Gruppe). Beides steht in ``chronicle.instanz`` und wird hier
nur noch eingeblendet, damit ``effective`` eine vollständige ``Config`` liefert.

**Das Foundry-Passwort gibt es hier nicht mehr** (#64). Es ist der Schlüssel zu einem
fremden Server, und eine Instanz, die mehrere Runden trägt, hält kein fremdes Geheimnis
vor. Es lebt im Arbeitsspeicher (``chronicle.zugang``) und wird vom Abgleich verbraucht.

Der Bot-Token liegt dadurch als einziger im Klartext in der SQLite. Aus diesem Modul kommt
er nur dahin zurück, wo er hingehört: in den Authorization-Header. Für die Anzeige gibt es
``is_set`` — *ob* etwas gesetzt ist, nie *was*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import time
from urllib.parse import urlsplit
from zoneinfo import available_timezones

from chronicle import db, instanz
from chronicle.config import DEFAULT_OLLAMA_URL, Config
from chronicle.runde import Runde

KEYS = (
    "foundry_url",
    "foundry_user",
    "discord_bot_token",
    "discord_recap_channel",
    "ollama_url",
    "ollama_model",
)

SECRET_KEYS = ("discord_bot_token",)

# Die Werte aus KEYS, die nicht der Runde gehören. Sie werden nach ``chronicle.instanz``
# durchgereicht, statt in der Einstellungstabelle einer Runde zu liegen.
INSTANZ_KEYS = instanz.KEYS

RUNDEN_KEYS = tuple(name for name in KEYS if name not in INSTANZ_KEYS)

# Steht bewusst nicht in KEYS: die Uhrzeit des nächtlichen Laufs gibt es nur hier. Sie über
# die Umgebung vorzugeben hieße, sie beim Deploy zu entscheiden — sie gehört aber der
# Gruppe, die weiß, wann ihr Server ungestört ist.
NIGHTLY_KEY = "nightly_time"
DEFAULT_NIGHTLY_TIME = "04:00"

# Und dieselbe Überlegung eine Stufe weiter: die Uhrzeit allein sagt nicht, welche Uhr
# gemeint ist. Der Container läuft auf der Box in UTC, die Runde nicht — deshalb steht die
# Zone neben der Zeit und gehört ebenfalls der Runde. Eine Instanz kann Runden in
# verschiedenen Zonen tragen; ein TZ im Pod könnte immer nur einer davon recht geben.
NIGHTLY_ZONE_KEY = "nightly_zone"
DEFAULT_NIGHTLY_ZONE = "Europe/Berlin"

# Aus der Zonendatenbank des Systems, einmal beim Import. Was hier nicht drinsteht, wird
# nicht gespeichert — ein aus dem Formular kommender Name geht so nie an ``ZoneInfo``.
ZONEN: tuple[str, ...] = tuple(sorted(available_timezones()))

# Woher die Spieldaten kommen. Auch das gehört der Runde und nicht der Umgebung: die eine
# Gruppe spielt auf ihrem Server, die andere probiert die Instanz erst einmal aus. Steht
# hier ``testwelt``, redet der Abgleich mit niemandem — er liest die mitgelieferte,
# erzeugte Welt. Was dann angezeigt wird, ist erfunden, und die Oberfläche sagt das
# an jeder Stelle dazu; eine Verwechslung wäre der teuerste Fehler dieses Schalters.
QUELLE_KEY = "foundry_quelle"
SERVER = "server"
TESTWELT = "testwelt"
QUELLEN = (SERVER, TESTWELT)
DEFAULT_QUELLE = SERVER

# Womit eine Foundry-Adresse anfangen muss. Mehr als eine Wahl gibt es nicht: der Client
# spricht HTTP, und alles andere wäre ein Tippfehler, der als Schema durchginge.
FOUNDRY_SCHEMATA = ("http", "https")

FRONTEND = "Frontend"
UMGEBUNG = "Umgebung"
STANDARD = "Standard dieser Box"
UNGESETZT = "nicht gesetzt"


def _lesen(runde: Runde, key: str) -> str | None:
    scope = db.scoped(runde)
    try:
        zeile = scope.execute(
            "SELECT value FROM settings WHERE runde_id = ? AND key = ?", (scope.runde_id, key)
        ).fetchone()
    finally:
        scope.close()
    return None if zeile is None else str(zeile["value"])


def stored(runde: Runde) -> dict[str, str]:
    """Was gepflegt ist — die Werte der Runde und der eine der Instanz."""
    scope = db.scoped(runde)
    try:
        zeilen = scope.execute(
            "SELECT key, value FROM settings WHERE runde_id = ?", (scope.runde_id,)
        ).fetchall()
    finally:
        scope.close()
    gepflegt = {
        z["key"]: z["value"] for z in zeilen if z["key"] in RUNDEN_KEYS and z["value"].strip()
    }
    return gepflegt | instanz.stored(runde.database_path)


def effective(config: Config, runde: Runde) -> Config:
    """Gepflegtes schlägt die Umgebung, die Umgebung schlägt den Box-Standard.

    Der Standard ist hier Verhalten und nicht bloß Formulartext: wer keine Adresse
    einträgt, bekommt das Ollama dieser Box — sonst versprächen die Einstellungen etwas,
    das der Lauf dann nicht tut.
    """
    zusammen = replace(config, **stored(runde))
    if zusammen.ollama_url:
        return zusammen
    return replace(zusammen, ollama_url=DEFAULT_OLLAMA_URL)


def sources(config: Config, runde: Runde) -> dict[str, str]:
    wirksam = effective(config, runde)
    gepflegt = stored(runde)
    return {
        name: FRONTEND
        if name in gepflegt
        else UMGEBUNG
        if getattr(config, name)
        else (STANDARD if getattr(wirksam, name) else UNGESETZT)
        for name in KEYS
    }


def is_set(config: Config, runde: Runde, name: str) -> bool:
    return bool(getattr(effective(config, runde), name))


def nightly_at(value: str) -> time:
    """Die Uhrzeit hinter dem Wert — was sich nicht lesen lässt, ist die Vorgabe."""
    try:
        return time.fromisoformat(value)
    except ValueError:
        return time.fromisoformat(DEFAULT_NIGHTLY_TIME)


def nightly_time(runde: Runde) -> str:
    wert = _lesen(runde, NIGHTLY_KEY)
    if wert is None:
        return DEFAULT_NIGHTLY_TIME
    return nightly_at(wert).strftime("%H:%M")


def save_nightly_time(runde: Runde, value: str) -> bool:
    """Speichert eine Uhrzeit; eine unlesbare lässt die bisherige stehen."""
    try:
        gewaehlt = time.fromisoformat(value.strip())
    except ValueError:
        return False
    save(runde, {NIGHTLY_KEY: gewaehlt.strftime("%H:%M")})
    return True


def nightly_zone(runde: Runde) -> str:
    """Die Zone, in der die Uhrzeit gilt — ein unbekannter Name ist die Vorgabe."""
    wert = _lesen(runde, NIGHTLY_ZONE_KEY)
    if wert is None or wert not in ZONEN:
        return DEFAULT_NIGHTLY_ZONE
    return wert


def save_nightly_zone(runde: Runde, value: str) -> bool:
    """Speichert eine Zone; ein unbekannter Name lässt die bisherige stehen."""
    gewaehlt = value.strip()
    if gewaehlt not in ZONEN:
        return False
    save(runde, {NIGHTLY_ZONE_KEY: gewaehlt})
    return True


def foundry_quelle(runde: Runde) -> str:
    """Echter Server oder eingebaute Testwelt — ein unbekannter Wert ist der Server."""
    wert = _lesen(runde, QUELLE_KEY)
    return wert if wert in QUELLEN else DEFAULT_QUELLE


def brauchbare_adresse(wert: str) -> bool:
    """Ob sich aus der Eingabe ein Foundry-Server ansprechen lässt.

    Verlangt wird die **Wurzel** mit Schema. Ohne Schema liest ``urlsplit`` den Hostnamen
    als Schema und liefert ``hostname=None`` — der Strom kam an keinen Server und schwieg
    dazu einen ganzen Abend lang (#243). Ein Pfad samt Query ist derselbe Fund aus der
    anderen Richtung: was aus der Browserzeile kopiert wurde, während der Anmeldebildschirm
    offen stand, zeigt nicht auf den Server, sondern in eine Weltinstanz.
    """
    teile = urlsplit(wert)
    if teile.scheme not in FOUNDRY_SCHEMATA or not teile.hostname:
        return False
    if teile.query or teile.fragment or teile.path.strip("/"):
        return False
    try:
        # Ein unsinniger Port fällt erst beim Zugriff auf, nicht beim Zerlegen.
        return teile.port is None or teile.port > 0
    except ValueError:
        return False


def save_foundry_url(runde: Runde, value: str) -> bool:
    """Speichert die Adresse; eine unbrauchbare lässt die bisherige stehen."""
    gewaehlt = value.strip()
    if not brauchbare_adresse(gewaehlt):
        return False
    save(runde, {"foundry_url": gewaehlt})
    return True


def save_foundry_quelle(runde: Runde, value: str) -> bool:
    """Speichert die Quelle; ein unbekannter Wert lässt die bisherige stehen."""
    gewaehlt = value.strip()
    if gewaehlt not in QUELLEN:
        return False
    save(runde, {QUELLE_KEY: gewaehlt})
    return True


def save(runde: Runde, values: Mapping[str, str | None]) -> None:
    """Leerer Wert heißt: Eintrag weg, die Umgebung gilt wieder.

    Ein Geheimnis, das unverändert bleiben soll, gehört deshalb gar nicht erst in
    ``values`` — sonst löscht ein leer abgesendetes Formularfeld es.
    """
    instanz.save(runde.database_path, values)
    erlaubt = (*RUNDEN_KEYS, NIGHTLY_KEY, NIGHTLY_ZONE_KEY, QUELLE_KEY)
    scope = db.scoped(runde)
    try:
        with scope:
            for name, wert in values.items():
                if name not in erlaubt:
                    continue
                sauber = (wert or "").strip()
                if sauber:
                    scope.execute(
                        "INSERT INTO settings (runde_id, key, value) VALUES (?, ?, ?) "
                        "ON CONFLICT (runde_id, key) DO UPDATE SET value = excluded.value",
                        (scope.runde_id, name, sauber),
                    )
                else:
                    scope.execute(
                        "DELETE FROM settings WHERE runde_id = ? AND key = ?",
                        (scope.runde_id, name),
                    )
    finally:
        scope.close()
