"""Woher ein Aufruf kommt — und ob seine Anmelde-Kopfzeilen etwas gelten.

``Remote-User`` und ``Remote-Groups`` setzt Authelia am Proxy (ServiceBay-ADR 0001).
Beides sind gewöhnliche Kopfzeilen: wer den Port **direkt** erreicht, schreibt sie selbst
hin und ist damit angemeldet als wer er will — samt Verwaltungsgruppe (#190). Ihr Wert
hängt also nicht am Inhalt, sondern daran, **wer sie geschickt hat**. Beide hängen an
derselben Frage, und deshalb wird sie an einer Stelle gestellt: vor allem anderen im
Türsteher (``chronicle.app``). Wer sie nicht besteht, erreicht die Verwaltungsschranke
gar nicht erst — eine Gruppe zu prüfen, deren Benutzername frei erfunden sein darf, wäre
ohnehin wertlos.

Der Dienst hängt im Netz-Namensraum der Box (``hostNetwork``) und hört auf ``0.0.0.0``;
der Proxy läuft auf **derselben** Box, wählt sie aber über ihre LAN-Adresse an. Auf
Loopback zu bestehen ginge deshalb nicht — es wäre genau der echte Weg, der aussperrt.
Was den echten Weg vom falschen trennt, ist: ein Aufruf **von dieser Maschine** trägt
eine Adresse dieser Maschine als Absender — ``127.0.0.1`` oder ihre eigene LAN-Adresse;
ein anderer Rechner im LAN trägt seine eigene.

Gefragt wird der Kern und keine gepflegte Liste: an eine fremde Adresse lässt sich nicht
binden (``EADDRNOTAVAIL``). Der Umweg lohnt, weil die Box per DHCP adressiert ist — eine
abgeschriebene Adresse wäre nach der nächsten Lease eine Aussperrung, diese Frage
beantwortet sich bei jedem Aufruf neu. (Wo im Kern ``ip_nonlocal_bind`` steht, gilt jede
Adresse als eigene; dort trägt die Prüfung nicht, und es zählt allein die Liste unten.
``tests/test_herkunft.py`` schlägt in diesem Fall fehl, statt still durchzuwinken, und
``selbstprobe`` sagt es beim Start jedes Dienstes — auch dem, unter dem niemand testet.)

**Falls doch einmal ausgesperrt** — die Seite antwortet 403, obwohl die Anmeldung am
Proxy geklappt hat: ``CHRONICLE_TRUSTED_PROXIES`` in der Dienstbeschreibung auf die
Adresse setzen, von der der Proxy anwählt, und den Dienst neu starten. Eine
kommagetrennte Liste aus Adressen oder Netzen: ``192.0.2.10``,
``192.0.2.0/24``, ``127.0.0.1``, ``::1``. Gesetzt **ersetzt** sie die errechnete
Antwort, leer bleibt es bei der Maschine selbst. Welche Adresse gemeint ist, sagt die
Logzeile bei jeder Abweisung. Ein neues Abbild braucht es dafür nicht.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Iterable

logger = logging.getLogger(__name__)

TRUSTED_VARIABLE = "CHRONICLE_TRUSTED_PROXIES"

# TEST-NET-1 (RFC 5737): eine Adresse, die per Norm keiner Maschine gehört. Wer sie für
# eigen hält, hält jede für eigen.
PROBE_ADRESSE = "192.0.2.1"

NONLOCAL_SCHALTER = "net.ipv4.ip_nonlocal_bind"

Adresse = ipaddress.IPv4Address | ipaddress.IPv6Address
Netz = ipaddress.IPv4Network | ipaddress.IPv6Network


def netze(eintraege: Iterable[str]) -> tuple[Netz, ...]:
    """Die gepflegte Liste als Netze — eine nackte Adresse ist ihr eigenes /32.

    Ein unlesbarer Eintrag fällt heraus statt den Start zu verhindern: eine Instanz, die
    an einem Tippfehler nicht mehr hochkommt, ist dieselbe Aussperrung noch einmal. Fällt
    dadurch alles heraus, gilt wieder die Maschine selbst — also enger, nicht weiter.
    """
    gelesen = []
    for eintrag in eintraege:
        try:
            gelesen.append(ipaddress.ip_network(eintrag.strip(), strict=False))
        except ValueError:
            logger.warning(
                "%s: »%s« ist keine Adresse und kein Netz — übergangen.",
                TRUSTED_VARIABLE,
                eintrag,
            )
    return tuple(gelesen)


def adresse(wert: str | None) -> Adresse | None:
    """Der Absender als Adresse — ``None``, wenn daraus keine wird.

    Ein v6-Horcher meldet einen v4-Aufruf als ``::ffff:192.0.2.10``; ohne das
    Zurückfalten läge derselbe Absender einmal in der Liste und einmal nicht.
    """
    if not wert:
        return None
    try:
        gelesen = ipaddress.ip_address(wert.strip().split("%")[0])
    except ValueError:
        return None
    if isinstance(gelesen, ipaddress.IPv6Address) and gelesen.ipv4_mapped:
        return gelesen.ipv4_mapped
    return gelesen


def ist_eigene(gefragt: Adresse) -> bool:
    """Gehört die Adresse einer Schnittstelle dieser Maschine? Der Kern weiß es."""
    familie = socket.AF_INET6 if gefragt.version == 6 else socket.AF_INET
    try:
        with socket.socket(familie, socket.SOCK_DGRAM) as pruefer:
            pruefer.bind((str(gefragt), 0))
    except OSError:
        return False
    return True


def selbstprobe() -> bool:
    """Trägt die Frage an den Kern hier überhaupt? Einmal beim Start gestellt.

    Steht ``net.ipv4.ip_nonlocal_bind`` auf 1, lässt sich an **jede** Adresse binden;
    ``ist_eigene`` sagt dann zu allem ja, und der Türsteher winkt jeden Absender durch,
    ohne dass irgendetwas rot würde. Von der Box selbst ist das nicht testbar — dort ist
    jeder Aufruf zu Recht ein eigener —, also fragt der Dienst es beim Start an einer
    Adresse, die niemandem gehören darf.

    Der Dienst startet auch dann; eine Instanz, die daran nicht mehr hochkommt, wäre
    dieselbe Aussperrung noch einmal.
    """
    if not ist_eigene(ipaddress.ip_address(PROBE_ADRESSE)):
        return True
    logger.error(
        "Die Herkunftsprüfung trägt auf dieser Maschine nicht: %s gilt ihr als eigene "
        "Adresse, also gilt ihr jede. Damit wird Remote-User wieder jedem geglaubt, der "
        "den Port erreicht (#190). Vermutlich steht %s auf 1 — auf 0 setzen, oder die "
        "Adresse des Proxys in %s eintragen; dann zählt nur noch sie.",
        PROBE_ADRESSE,
        NONLOCAL_SCHALTER,
        TRUSTED_VARIABLE,
    )
    return False


def vertraut(absender: str | None, erlaubt: tuple[Netz, ...] = ()) -> bool:
    """Darf dieser Absender ``Remote-User`` und ``Remote-Groups`` behaupten?"""
    gefragt = adresse(absender)
    if gefragt is None:
        return False
    if erlaubt:
        return any(netz.version == gefragt.version and gefragt in netz for netz in erlaubt)
    return ist_eigene(gefragt)
