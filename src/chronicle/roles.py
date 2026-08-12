"""Wer die Betreiber-Seite verwalten darf — die eine Stelle, an der das entschieden wird.

Mitgespielt wird in Discord, und wer dort was darf, entscheidet die Gilde über ihre
eigenen Rechte (#62). Hier geht es um etwas anderes: wer an den **Bot-Token** darf. Das
gehört dem Betrieb dieser Box und hat in keiner Gilde einen Ort, weshalb dieses Modul
#157 überlebt hat. Die Gruppenzugehörigkeit kommt mit jedem Aufruf vom Proxy; Konten und
Gruppen pflegt die Benutzerverwaltung der Box, nicht die Chronik.

Gespeichert ist hier nur der *Name* der Gruppe. **Leer heißt: alle dürfen alles** — das
ist die Vorgabe und das bisherige Verhalten; kleine Runden ohne Rollenbedarf müssen
nichts tun.
"""

from __future__ import annotations

from pathlib import Path

from flask import Request

from chronicle import instanz

# Der Proxy schickt die Gruppen als kommagetrennte Liste. Der Name steht hier und in
# keiner gerenderten Seite: Proxy-Innenleben hilft beim Bedienen niemandem.
GRUPPEN_HEADER = "Remote-Groups"


def gruppen(kopfzeile: str | None) -> frozenset[str]:
    """Die Gruppen einer Kopfzeile — Leerzeichen weg, Groß und Klein egal."""
    return frozenset(
        teil.strip().casefold() for teil in (kopfzeile or "").split(",") if teil.strip()
    )


def traegt(kopfzeile: str | None, gruppe: str) -> bool:
    gesucht = gruppe.strip().casefold()
    return not gesucht or gesucht in gruppen(kopfzeile)


def ist_verwalter(request: Request, database_path: Path) -> bool:
    return traegt(request.headers.get(GRUPPEN_HEADER), instanz.admin_group(database_path))
