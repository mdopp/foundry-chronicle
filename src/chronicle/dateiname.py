"""Aus einer Eingabe von außen einen Dateinamen machen, der nur ein Dateiname ist.

Zwei Stellen schreiben eine Datei, deren Name von einem Menschen kommt: der
Anzeigename eines Sprechers wird zur Spurdatei (``chronicle.bot.recorder``), und der
Name eines hochgeladenen Diktats wird zum Stamm im Aufnahmeverzeichnis
(``chronicle.recordings``). Beide Namen dürfen alles enthalten — Schrägstriche, ``..``,
Steuerzeichen, jede Schrift der Welt. Was hier herauskommt, darf nichts davon mehr sein:
kein Pfadtrenner, kein Verweis nach oben, kein führender Punkt.

Das kam bisher aus ``werkzeug.utils.secure_filename`` — und werkzeug kam als Anhängsel
von Flask. Mit #231 ist die Betreiber-Seite gefallen und Flask ins ``dev``-Extra
gezogen; werkzeug fiel dabei still mit weg und das Abbild startete nicht mehr (#259).
Der Rückweg wäre, werkzeug als direkte Laufzeit-Abhängigkeit nachzutragen. Dagegen
spricht dieselbe Entscheidung, die Flask hinausgeworfen hat: dieser Dienst ist ein
Bot-Prozess ohne WSGI-Fläche, und das Werkzeugkasten-Paket eines Webrahmenwerks für
**eine** Funktion vorzuhalten holte durch die Hintertür zurück, was #231 vorne
hinausgetragen hat.

Die Regel ist dieselbe wie dort, absichtlich Zeichen für Zeichen: NFKD zerlegen und auf
ASCII eindampfen (ein Dateiname, der über Container, Backup und Fremddienst wandert,
soll keine Kodierungsfrage aufwerfen), Schrägstriche zu Zwischenraum, Zwischenraum zu
``_``, alles außerhalb von ``[A-Za-z0-9_.-]`` fällt weg, und ``.`` wie ``_`` an den
Rändern werden abgeschnitten. Letzteres erledigt ``..`` und die versteckte Datei in
einem Zug.

Was werkzeug zusätzlich tut, fehlt hier bewusst: die Sonderbehandlung der
Windows-Gerätenamen (``CON``, ``LPT1``) greift dort nur unter ``os.name == "nt"``, und
dieser Dienst läuft im Linux-Container.

**Das Ergebnis kann leer sein** — ein Name aus lauter Emoji hat nach dem ASCII-Schritt
nichts mehr übrig. Der Aufrufer muss das abfangen; beide tun es mit einem Ersatznamen.
"""

from __future__ import annotations

import re
import unicodedata

_VERBOTEN = re.compile(r"[^A-Za-z0-9_.-]")


def sicherer_dateiname(name: str) -> str:
    """Ein Dateiname ohne Pfad, ohne Überraschungen — möglicherweise der leere String."""
    nur_ascii = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return _VERBOTEN.sub("", "_".join(nur_ascii.replace("/", " ").split())).strip("._")
