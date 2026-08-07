#!/usr/bin/env python3
"""Der synthetische Durchstich: eine Wegwerf-Instanz, ein Durchlauf, ein Exit-Code.

Gefragt wird das, was Healthcheck und Statuscode nicht beantworten: kommt eine Notiz
durch den ganzen Weg bis in Chronik, Rückblick und Suche — und steht die Haustür zu?
Die Schritte stehen deshalb hier als Skript und nicht als Prosa in einem Playbook, das
bei jedem Lauf neu ausgelegt wird (CLAUDE.md » Skripte statt Prosa).

**Angefasst wird nichts Echtes.** Der Lauf startet seinen eigenen Dienst mit eigenem
Datenverzeichnis unter ``/tmp`` und eigenem Port und räumt beides am Ende ab; die
Datenbank der Gruppe sieht er nie.

Die Umgebung wird **gebaut, nicht geerbt**: ohne ``OLLAMA_URL`` komponiert der Lauf
geordnet statt formuliert, und genau das macht ihn wiederholbar — ein Sprachmodell
schriebe jedes Mal etwas anderes. Ohne die Foundry- und Discord-Werte fasst die
Wegwerf-Instanz auch dort nichts an.

Nur Standardbibliothek: das Skript läuft im Image, und dort liegt kein pytest.

    python /app/scripts/verify_e2e.py        # im Container nach dem Deploy
    python scripts/verify_e2e.py             # lokal im Dev-Venv
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zoneinfo
from pathlib import Path

BENUTZER = "durchstich"

# Muss zu ``settings.DEFAULT_NIGHTLY_ZONE`` passen; hier von Hand, weil dieses Skript nur
# auf der Standardbibliothek steht.
STANDARDZONE = "Europe/Berlin"

STARTFRIST = 60.0

ABRUFFRIST = 30.0

KOMPOSITIONSFRIST = 300.0

TITEL = "Synthetischer Durchstich — Wegwerf-Instanz, echte Daten unberührt"

BESTANDEN = "Durchstich bestanden"

GESCHEITERT = "Durchstich gescheitert"

# Die Umgebungswerte, die eine Wegwerf-Instanz braucht; alles andere bleibt draußen.
GEERBT = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "SSL_CERT_FILE")

SITZUNGSADRESSE = re.compile(r"/sitzungen/(\d+)")

NOTIZFORMULAR = re.compile(r'action="/szenen/(\d+)/notizen"')

# Die Protokollseite kennzeichnet den Rückblick über seine Klasse (protokoll.html).
RUECKBLICK = 'class="chronik rueckblick"'

# Der erste Schritt des Erststart-Wizards und der Anker, in dem /status aufgegangen ist.
ERSTER_SCHRITT = "/einrichtung/foundry"

ZUSTAND = 'id="zustand"'


class Fehlschlag(AssertionError):
    """Ein Schritt, der nicht ergab, was er ergeben muss."""


class Lauf:
    def __init__(self) -> None:
        self.schritte: list[str] = []

    def ok(self, name: str) -> None:
        self.schritte.append(name)
        print(f"  ok      {name}", flush=True)


def pruefe(bedingung: object, meldung: str) -> None:
    if not bedingung:
        raise Fehlschlag(meldung)


def freier_port() -> int:
    with socket.socket() as horcher:
        horcher.bind(("127.0.0.1", 0))
        return int(horcher.getsockname()[1])


def umgebung(daten: Path, aufnahmen: Path, port: int) -> dict[str, str]:
    gebaut = {name: os.environ[name] for name in GEERBT if name in os.environ}
    gebaut.update(
        PYTHONUNBUFFERED="1",
        CHRONICLE_DATA_DIR=str(daten),
        CHRONICLE_RECORDINGS_DIR=str(aufnahmen),
        CHRONICLE_HOST="127.0.0.1",
        CHRONICLE_PORT=str(port),
        CHRONICLE_REQUIRE_REMOTE_USER="1",
    )
    return gebaut


def abruf(url: str, *, daten: dict[str, str] | None = None, benutzer: str | None = BENUTZER):
    rumpf = urllib.parse.urlencode(daten).encode("utf-8") if daten is not None else None
    anfrage = urllib.request.Request(url, data=rumpf)
    if benutzer is not None:
        anfrage.add_header("Remote-User", benutzer)
    try:
        with urllib.request.urlopen(anfrage, timeout=ABRUFFRIST) as antwort:
            return antwort.status, antwort.read().decode("utf-8", "replace"), antwort.geturl()
    except urllib.error.HTTPError as fehler:
        return fehler.code, fehler.read().decode("utf-8", "replace"), url


def warten(basis: str, prozess: subprocess.Popen) -> None:
    """Bis ``/healthz`` antwortet — derselbe Endpunkt, den die Box als Install-Gate pollt."""
    ende = time.monotonic() + STARTFRIST
    while time.monotonic() < ende:
        pruefe(prozess.poll() is None, f"Der Dienst endete mit Rückgabewert {prozess.returncode}.")
        try:
            with urllib.request.urlopen(basis + "/healthz", timeout=2.0) as antwort:
                if antwort.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise Fehlschlag(f"{basis}/healthz antwortet nicht binnen {STARTFRIST:.0f} s.")


def durchlauf(basis: str, umgeb: dict[str, str], lauf: Lauf) -> None:
    marke = "Durchstichmarke" + uuid.uuid4().hex[:8]
    notiz = f"Die Kammer hinter dem Krummen Ast, Kennzeichen {marke}."

    # Eine frische Instanz ist der Erststart: die Startseite führt in die Einrichtung.
    status, _, ziel = abruf(basis + "/")
    pruefe(status == 200, f"Erststart: HTTP {status}")
    pruefe(ziel.endswith(ERSTER_SCHRITT), f"Erststart: / landete auf {ziel} statt {ERSTER_SCHRITT}")
    lauf.ok("Erststart führt in die Einrichtung")

    status, seite, ziel = abruf(basis + "/status")
    pruefe(status == 200, f"Zustand: HTTP {status}")
    pfad = urllib.parse.urlsplit(ziel)
    pruefe(pfad.path == "/einstellungen", f"Zustand: /status landete auf {ziel}")
    pruefe(pfad.fragment == "zustand", f"Zustand: kein Anker in {ziel}")
    pruefe(ZUSTAND in seite, "Zustand: der Abschnitt fehlt auf der Einstellungsseite")
    lauf.ok("/status leitet in den Zustand der Einstellungen um")

    status, _, ziel = abruf(basis + "/", daten={"played_on": "", "title": "Durchstich"})
    pruefe(status == 200, f"Sitzung anlegen: HTTP {status}")
    gefunden = SITZUNGSADRESSE.search(ziel)
    pruefe(gefunden is not None, f"Sitzung anlegen: keine Sitzungsadresse in {ziel}")
    sitzung = gefunden.group(1)
    lauf.ok(f"Sitzung {sitzung} angelegt")

    status, seite, _ = abruf(f"{basis}/sitzungen/{sitzung}/szenen", daten={"title": "Der Keller"})
    pruefe(status == 200, f"Szene anlegen: HTTP {status}")
    szenen = NOTIZFORMULAR.findall(seite)
    pruefe(szenen, "Szene anlegen: kein Notizformular auf der Sitzungsseite")
    szene = szenen[-1]
    lauf.ok(f"Szene {szene} angelegt")

    status, seite, _ = abruf(f"{basis}/szenen/{szene}/notizen", daten={"text": notiz})
    pruefe(status == 200, f"Notiz sichern: HTTP {status}")
    pruefe(notiz in seite, "Notiz sichern: die Notiz steht nicht auf der Sitzungsseite")
    lauf.ok("Notiz gesichert und wieder angezeigt")

    # Rückgabewert 1 heißt »abgelegt, aber ohne Sprachmodell« — der deterministische Fall,
    # den diese Umgebung absichtlich erzwingt.
    ergebnis = subprocess.run(
        [sys.executable, "-m", "chronicle.compose", sitzung],
        env=umgeb,
        capture_output=True,
        text=True,
        timeout=KOMPOSITIONSFRIST,
    )
    pruefe(
        ergebnis.returncode in (0, 1),
        f"Komposition: Rückgabewert {ergebnis.returncode}\n{ergebnis.stdout}{ergebnis.stderr}",
    )
    meldungen = [zeile.strip() for zeile in ergebnis.stdout.splitlines() if zeile.strip()]
    lauf.ok("Komposition gelaufen — " + (" · ".join(meldungen) or "ohne Meldung"))

    status, seite, _ = abruf(f"{basis}/sitzungen/{sitzung}/protokoll")
    pruefe(status == 200, f"Protokollseite: HTTP {status}")
    pruefe(notiz in seite, "Protokollseite: die Notiz steht nicht in der Chronik")
    lauf.ok("Protokollseite trägt die Notiz")
    pruefe(RUECKBLICK in seite, "Protokollseite: kein Rückblick abgelegt")
    lauf.ok("Rückblick steht über der Chronik")

    status, seite, _ = abruf(f"{basis}/suche?q={urllib.parse.quote(marke)}")
    pruefe(status == 200, f"Suche: HTTP {status}")
    pruefe(marke in seite, f"Suche: »{marke}« nicht gefunden")
    lauf.ok("Suche findet die Marke wieder")

    status, _, _ = abruf(basis + "/", benutzer=None)
    pruefe(status == 403, f"Haustür: ohne Remote-User kam HTTP {status} statt 403")
    lauf.ok("Ohne Remote-User: 403")

    # Fehlt die Zonendatenbank im Image, liefe der Nachtlauf nie — und zwar still, weil
    # der Faden den Fehlschlag nur wegloggt. Hier fällt es auf, bevor eine Nacht ausbleibt.
    zonen = len(zoneinfo.available_timezones())
    pruefe(zonen > 100, f"Zonendatenbank: nur {zonen} Zonen im Image")
    zoneinfo.ZoneInfo(STANDARDZONE)
    lauf.ok(f"Zonendatenbank im Image: {zonen} Zonen, {STANDARDZONE} auflösbar")


def beenden(prozess: subprocess.Popen) -> None:
    if prozess.poll() is not None:
        return
    prozess.terminate()
    try:
        prozess.wait(timeout=10)
    except subprocess.TimeoutExpired:
        prozess.kill()
        prozess.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("Aufruf: verify_e2e.py — ohne Argumente.")
        return 2

    lauf = Lauf()
    verzeichnis = Path(tempfile.mkdtemp(prefix="chronik-durchstich-"))
    daten = verzeichnis / "daten"
    aufnahmen = verzeichnis / "aufnahmen"
    daten.mkdir()
    aufnahmen.mkdir()
    port = freier_port()
    basis = f"http://127.0.0.1:{port}"
    umgeb = umgebung(daten, aufnahmen, port)
    mitschrift = verzeichnis / "dienst.log"

    print(TITEL)
    print(f"  Instanz {verzeichnis} auf {basis}", flush=True)

    fehler: str | None = None
    datei = mitschrift.open("wb")
    prozess = subprocess.Popen(
        [sys.executable, "-m", "chronicle"], env=umgeb, stdout=datei, stderr=subprocess.STDOUT
    )
    try:
        warten(basis, prozess)
        lauf.ok("Dienst läuft, /healthz antwortet ohne Remote-User")
        durchlauf(basis, umgeb, lauf)
    except (Fehlschlag, OSError, subprocess.SubprocessError) as ausnahme:
        fehler = f"{type(ausnahme).__name__}: {ausnahme}"
    finally:
        beenden(prozess)
        datei.close()
        dienstlog = mitschrift.read_text("utf-8", "replace") if mitschrift.exists() else ""
        shutil.rmtree(verzeichnis, ignore_errors=True)

    if fehler is None:
        print(f"{BESTANDEN}: {len(lauf.schritte)} Schritte, Wegwerf-Instanz abgeräumt.")
        return 0

    print(f"  FEHLER  {fehler}")
    zeilen = dienstlog.strip().splitlines()[-15:]
    if zeilen:
        print("  Letzte Zeilen des Dienstes:")
        for zeile in zeilen:
            print(f"    {zeile}")
    print(f"{GESCHEITERT} nach {len(lauf.schritte)} Schritten.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
