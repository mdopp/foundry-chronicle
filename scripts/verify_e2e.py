#!/usr/bin/env python3
"""Der synthetische Durchstich: eine Wegwerf-Instanz, ein Durchlauf, ein Exit-Code.

Gefragt wird das, was Healthcheck und Statuscode nicht beantworten: kommt eine Notiz
durch den ganzen Weg bis in Chronik, Rückblick und Suche — und steht die Haustür zu?
Die Schritte stehen deshalb hier als Skript und nicht als Prosa in einem Playbook, das
bei jedem Lauf neu ausgelegt wird (CLAUDE.md » Skripte statt Prosa).

**Gefahren wird über die Bot-Befehle, nicht über HTTP-Routen** (#158). Mit #157 trägt die
Weboberfläche keine Spielinhalte mehr; sie ist die Betreiber-Seite, und die wird hier nur
noch daraufhin geprüft, dass sie steht und zu ist. Die Sitzung selbst — anlegen, Szene
ziehen, mitschreiben, diktieren, abschließen, wiederfinden — läuft über
``chronicle.bot.chronik`` und ``chronicle.bot.erinnern``, also über genau die Schicht,
die in Discord unter den Befehlen liegt. Der **Discord-Mock** ist der Rumpf hier unten:
ein Thread ist eine Zeichenkette, eine Nachricht ist ``chronik.Nachricht``, ein Anhang
ist ``chronik.Anhang``. Ein echter Discord ist nicht im Spiel — kein Token, keine Gilde.

**Angefasst wird nichts Echtes.** Der Lauf startet seinen eigenen Dienst mit eigenem
Datenverzeichnis unter ``/tmp`` und eigenem Port und räumt beides am Ende ab; die
Datenbank der Gruppe sieht er nie.

Die Umgebung wird **gebaut, nicht geerbt**: ohne ``OLLAMA_URL`` komponiert der Lauf
geordnet statt formuliert, und genau das macht ihn wiederholbar — ein Sprachmodell
schriebe jedes Mal etwas anderes. Ohne die Foundry- und Discord-Werte fasst die
Wegwerf-Instanz auch dort nichts an. Ein Mock-Foundry und ein Mock-Ollama gehören
deshalb ausdrücklich nicht hierher, sondern in die Testreihe (``tests/test_story.py``):
im Image liegt kein Mock, und ein Durchstich, der einen mitbrächte, prüfte das Image
nicht mehr.

Das Diktat kommt **nach** dem Abschluss: verschriftet wird beim Abschluss, und eine
wartende Spur riefe dabei ``solaris-whisper-batch`` — der liest nur das echte
Aufnahmeverzeichnis der Box, nicht das dieser Wegwerf-Instanz. Geprüft wird deshalb, dass
der Anhang als wartende Spur ankommt, nicht was der Erkenner daraus macht.

Nur die Standardbibliothek und ``chronicle``: das Skript läuft im Image, und dort liegt
kein pytest und keine Testabhängigkeit.

    python /app/scripts/verify_e2e.py        # im Container nach dem Deploy
    python scripts/verify_e2e.py             # lokal im Dev-Venv
"""

from __future__ import annotations

import asyncio
import os
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

from chronicle import db, jobs, notes, protocol, recordings, settings
from chronicle import runde as runden
from chronicle.bot import chronik, erinnern
from chronicle.compose.service import RUECKBLICK
from chronicle.config import Config

BENUTZER = "durchstich"

STARTFRIST = 60.0

ABRUFFRIST = 30.0

# Der Abschluss holt die Zahlen, verschriftet und komponiert — alles in einem Faden.
KOMPOSITIONSFRIST = 300.0

TITEL = "Synthetischer Durchstich — Wegwerf-Instanz, echte Daten unberührt"

BESTANDEN = "Durchstich bestanden"

GESCHEITERT = "Durchstich gescheitert"

# Die Umgebungswerte, die eine Wegwerf-Instanz braucht; alles andere bleibt draußen.
GEERBT = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH", "SSL_CERT_FILE")

# Der Discord-Mock: eine Gilde, ein Thread, eine Person. Zahlen, keine Zugangsdaten.
GILDE = "durchstich-gilde"
THREAD = "durchstich-thread"
WER = "durchstich-mitglied"

SITZUNGSTITEL = "Durchstich"
SZENE = "Der Keller"

DIKTAT = "sprachnachricht.m4a"

# Woran die Betreiber-Seite zu erkennen ist: das Feld für den Bot-Token. Es ist der Grund,
# warum sie #69 überlebt (#89) — fehlt es, ist die Instanz ohne Weg zum Token.
BETREIBERSEITE = 'name="discord_bot_token"'


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


def abruf(url: str, *, benutzer: str | None = BENUTZER):
    anfrage = urllib.request.Request(url)
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


def betreiberseite(basis: str, lauf: Lauf) -> None:
    """Was von der Weboberfläche bleibt: eine Seite, ein Geheimnis, eine geschlossene Tür."""
    status, seite, ziel = abruf(basis + "/")
    pruefe(status == 200, f"Betreiber-Seite: HTTP {status}")
    pfad = urllib.parse.urlsplit(ziel)
    pruefe(pfad.path == "/einstellungen", f"Betreiber-Seite: / landete auf {ziel}")
    pruefe(BETREIBERSEITE in seite, "Betreiber-Seite: das Feld für den Bot-Token fehlt")
    lauf.ok("Die Wurzel führt auf die Betreiber-Seite mit dem Bot-Token")

    status, _, ziel = abruf(basis + "/status")
    pruefe(status == 200, f"Alter Status-Pfad: HTTP {status}")
    pruefe(
        urllib.parse.urlsplit(ziel).path == "/einstellungen",
        f"Alter Status-Pfad: /status landete auf {ziel}",
    )
    lauf.ok("/status leitet weiter auf die Betreiber-Seite")

    status, _, _ = abruf(basis + "/", benutzer=None)
    pruefe(status == 403, f"Haustür: ohne Remote-User kam HTTP {status} statt 403")
    lauf.ok("Ohne Remote-User: 403")


def anhang(quelle: Path, name: str = DIKTAT) -> chronik.Anhang:
    """Ein Discord-Anhang, so weit die Stufe ihn braucht: Name, Größe, und wie man ihn holt."""

    async def speichern(ziel: Path) -> None:
        ziel.write_bytes(quelle.read_bytes())

    return chronik.Anhang(filename=name, size=quelle.stat().st_size, speichern=speichern)


def abschluss_abwarten(runde, session_id: int) -> str:
    """``/chronik fertig`` reiht einen Auftrag ein; hier wird auf dessen Ende gewartet."""
    ende = time.monotonic() + KOMPOSITIONSFRIST
    while time.monotonic() < ende:
        auftrag = jobs.latest(runde, jobs.CHRONIK, session_id)
        if auftrag is not None and not auftrag.laeuft:
            pruefe(auftrag.fertig, f"Abschluss gescheitert: {auftrag.error}")
            return auftrag.result or ""
        time.sleep(0.2)
    raise Fehlschlag(f"Der Abschluss war nach {KOMPOSITIONSFRIST:.0f} s nicht durch.")


def durchlauf(config: Config, lauf: Lauf) -> None:
    marke = "Durchstichmarke" + uuid.uuid4().hex[:8]
    notiz = f"Die Kammer hinter dem Krummen Ast, Kennzeichen {marke}."

    db.init(config.database_path)
    runde = runden.anlegen(config.database_path, SITZUNGSTITEL, guild_id=GILDE)
    pruefe(chronik.runde_der_gilde(config, GILDE) is not None, "Die Runde der Gilde fehlt.")
    lauf.ok("Runde der Gilde angelegt")

    # /chronik start — der Thread ist die Sitzung.
    sitzung = chronik.sitzung_anlegen(runde, THREAD, SITZUNGSTITEL)
    pruefe(
        chronik.sitzung_verlangen(runde, THREAD) == sitzung,
        "Der Thread findet seine Sitzung nicht wieder.",
    )
    lauf.ok(f"Sitzung {sitzung} angelegt, Thread verknüpft")

    # /szene — die Trennlinie zur nächsten Szene.
    chronik.szene_setzen(runde, sitzung, SZENE)
    szenen = notes.session(runde, sitzung).scenes
    pruefe(szenen[-1].title == SZENE, f"Szene nicht angelegt: {[s.title for s in szenen]}")
    lauf.ok(f"Szene {szenen[-1].id} angelegt")

    # Eine Nachricht im Thread wird zur Notiz — ohne Quittung, wie in Discord.
    asyncio.run(chronik.aufnehmen(config, runde, sitzung, chronik.Nachricht(id="m-1", text=notiz)))
    geschrieben = [n.text for szene in notes.session(runde, sitzung).scenes for n in szene.notes]
    pruefe(notiz in geschrieben, f"Die Notiz steht in keiner Szene: {geschrieben}")
    lauf.ok("Nachricht im Thread wurde zur Notiz")

    # /chronik fertig — Zahlen holen, verschriften, komponieren, zustellen.
    meldungen: list[str] = []
    antwort = chronik.abschluss_starten(
        config, runde, sitzung, None, wer=WER, melden=meldungen.append
    )
    pruefe(antwort != jobs.BELEGT, f"Der Abschluss kam nicht an: {antwort}")
    ergebnis = abschluss_abwarten(runde, sitzung)
    lauf.ok("Abschluss gelaufen — " + (ergebnis.strip() or "ohne Meldung"))
    pruefe(meldungen, "Der Abschluss meldete nichts in den Thread.")
    lauf.ok("Der Abschluss meldet sich im Thread")

    abgelegt = protocol.stored(runde, sitzung)
    pruefe(abgelegt is not None, "Es wurde keine Chronik abgelegt.")
    pruefe(notiz in abgelegt.text, "Chronik: die Notiz steht nicht darin")
    lauf.ok("Die Chronik trägt die Notiz")

    pruefe(protocol.stored(runde, sitzung, RUECKBLICK) is not None, "Kein Rückblick abgelegt")
    lauf.ok("Der Rückblick liegt neben der Chronik")

    # /suche — wochenlang später wiederfinden.
    gefunden = erinnern.suche(runde, marke)
    pruefe(gefunden.embed is not None, f"Suche: keine Trefferliste zu »{marke}« — {gefunden.text}")
    pruefe(marke in str(gefunden.embed), f"Suche: »{marke}« steht in keinem Treffer")
    lauf.ok("Die Suche findet die Marke wieder")

    # Ein Anhang im Thread wird eine wartende Spur. Bewusst **nach** dem Abschluss: davor
    # ginge das Verschriften an den Nachbardienst, und der kennt dieses Verzeichnis nicht.
    quelle = config.recordings_dir / "durchstich-quelle.m4a"
    quelle.parent.mkdir(parents=True, exist_ok=True)
    quelle.write_bytes(b"kein echtes Audio, aber Bytes")
    asyncio.run(
        chronik.aufnehmen(
            config,
            runde,
            sitzung,
            chronik.Nachricht(
                id="m-2", zeitpunkt=notes.today(), anhaenge=(anhang(quelle),), autor_id="4001"
            ),
        )
    )
    wartend = [spur.filename for spur in recordings.pending(runde)]
    pruefe(wartend, "Diktat: der Anhang steht in keiner Warteschlange")
    lauf.ok(f"Diktat aus dem Thread eingereiht: {wartend[-1]}")

    # Fehlt die Zonendatenbank im Image, liefe der Nachtlauf nie — und zwar still, weil
    # der Faden den Fehlschlag nur wegloggt. Hier fällt es auf, bevor eine Nacht ausbleibt.
    zonen = len(zoneinfo.available_timezones())
    pruefe(zonen > 100, f"Zonendatenbank: nur {zonen} Zonen im Image")
    zoneinfo.ZoneInfo(settings.DEFAULT_NIGHTLY_ZONE)
    lauf.ok(f"Zonendatenbank im Image: {zonen} Zonen, {settings.DEFAULT_NIGHTLY_ZONE} auflösbar")


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
    config = Config(data_dir=daten, recordings_dir=aufnahmen)
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
        betreiberseite(basis, lauf)
        durchlauf(config, lauf)
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
