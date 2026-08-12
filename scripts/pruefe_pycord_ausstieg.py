#!/usr/bin/env python3
"""Meldet, wenn der py-cord-Fork wieder weg darf.

``pyproject.toml`` zieht py-cord aus ``mdopp/pycord``, einem Fork, der einzig den Kopf
von Pycord-PR #3159 festhält (#145, #152). Ein Fork, den niemand mehr beobachtet, ist
schlimmer als der fremde Branch, von dem er kommt: er altert still und niemand merkt,
dass der Grund weggefallen ist.

Deshalb dieser Lauf — **wöchentlich in der CI, nie im Testlauf**. Er fragt zwei Dinge:
ist #3159 gemergt, und gibt es danach eine py-cord-Veröffentlichung? Beides ja heißt:
zurück auf eine Versionsangabe, Fork löschen. Dann schlägt der Lauf fehl, damit die
Nachricht von selbst ankommt.

Erreicht er GitHub oder PyPI nicht, meldet er das und bleibt grün — ein Wächter, der
bei jedem Netzhusten Alarm schlägt, wird abgeschaltet.

    scripts/pruefe_pycord_ausstieg.py
"""

import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent

PR = "https://api.github.com/repos/Pycord-Development/pycord/pulls/3159"
PYPI = "https://pypi.org/pypi/py-cord/json"
BEOBACHTUNGSPOSTEN = "https://github.com/mdopp/foundry-chronicle/issues/145"


def genagelter_commit(text):
    """Der Commit aus der ``py-cord``-Zeile des Extras ``[discord]``."""
    extras = tomllib.loads(text)["project"]["optional-dependencies"]
    for zeile in extras["discord"]:
        if zeile.startswith("py-cord"):
            return zeile.rsplit("@", 1)[1].strip()
    raise SystemExit("py-cord steht nicht mehr im Extra [discord] — Zeile von Hand prüfen")


def veroeffentlichung_nach(pypi, zeitpunkt):
    """Die erste py-cord-Fassung, die nach ``zeitpunkt`` hochgeladen wurde."""
    for fassung, dateien in sorted(pypi.get("releases", {}).items()):
        for datei in dateien:
            if datei.get("upload_time_iso_8601", "") > zeitpunkt:
                return fassung
    return None


def urteil(pr, pypi):
    """(darf_weg, Satz) — der Fork darf weg, wenn #3159 gemergt und veröffentlicht ist."""
    if not pr.get("merged"):
        return False, f"Pycord #3159 ist {pr.get('state', 'unbekannt')} und nicht gemergt"
    gemergt_am = pr.get("merged_at") or ""
    fassung = veroeffentlichung_nach(pypi, gemergt_am)
    if fassung is None:
        return False, f"Pycord #3159 ist seit {gemergt_am} gemergt, aber noch nicht veröffentlicht"
    return True, (
        f"Pycord #3159 ist seit {gemergt_am} gemergt und in py-cord {fassung} veröffentlicht"
    )


def _hole(url):
    with urllib.request.urlopen(url, timeout=30) as antwort:
        return json.load(antwort)


def main(hole=_hole):
    commit = genagelter_commit((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        pr, pypi = hole(PR), hole(PYPI)
    except (urllib.error.URLError, TimeoutError, ValueError) as fehler:
        print(f"nicht erreichbar, keine Aussage: {fehler}")
        return 0

    darf_weg, satz = urteil(pr, pypi)
    if not darf_weg:
        print(f"Fork bleibt: {satz} (genagelt auf {commit[:8]})")
        return 0

    print(
        f"Der Fork darf weg: {satz}.\n"
        f"Zu tun: py-cord in [discord] und [dev] von "
        f"»git+https://github.com/mdopp/pycord@{commit}« auf eine Versionsangabe "
        f"zurücksetzen, mdopp/pycord löschen, {BEOBACHTUNGSPOSTEN} schließen."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
