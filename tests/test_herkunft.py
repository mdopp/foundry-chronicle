"""Wem die Anmelde-Kopfzeilen geglaubt werden (#190).

``Remote-User`` und ``Remote-Groups`` sind gewöhnliche Kopfzeilen; was sie wert macht,
ist allein der Absender. Diese Datei prüft die Trennlinie selbst — dass ein Aufruf von
dieser Maschine als eigener erkannt wird und einer von woanders nicht. Was die Seite
daraus macht, steht in ``test_app``.
"""

import ipaddress
import logging
import socket

import pytest

from chronicle import herkunft

# TEST-NET-1 (RFC 5737): eine Adresse, die keiner Maschine gehören darf.
FREMD = herkunft.PROBE_ADRESSE


def eigene_lan_adresse():
    """Die Adresse, mit der diese Maschine nach draußen spricht — die des Proxys auf der Box.

    Kein Byte geht dabei ins Netz: ein verbundenes UDP-Socket wählt nur die Route und
    verrät ihre Quelladresse.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sonde:
        try:
            sonde.connect(("192.0.2.1", 9))
            return sonde.getsockname()[0]
        except OSError:
            return None


def test_loopback_ist_diese_maschine():
    assert herkunft.vertraut("127.0.0.1")
    assert herkunft.vertraut("::1")


def test_eine_fremde_adresse_ist_es_nicht():
    """Zugleich die Probe auf ``ip_nonlocal_bind``: steht der, ist die ganze Prüfung hin."""
    assert not herkunft.vertraut(FREMD)
    assert not herkunft.vertraut("192.168.178.55")


def test_die_eigene_lan_adresse_zaehlt_dazu():
    """Der echte Weg: der Proxy auf derselben Box wählt sie an und trägt sie als Absender."""
    adresse = eigene_lan_adresse()
    if adresse is None or adresse.startswith("127."):
        pytest.skip("Diese Maschine hat keine LAN-Adresse.")
    assert herkunft.vertraut(adresse)


def test_ein_v4_aufruf_am_v6_horcher_bleibt_derselbe_absender():
    assert herkunft.adresse("::ffff:127.0.0.1") == ipaddress.ip_address("127.0.0.1")
    assert herkunft.vertraut("::ffff:127.0.0.1")


def test_ohne_absender_wird_nichts_geglaubt():
    assert not herkunft.vertraut(None)
    assert not herkunft.vertraut("")
    assert not herkunft.vertraut("kein-hostname")


def test_die_gepflegte_liste_ersetzt_die_maschine():
    """Der Weg zurück: steht der Proxy woanders, zählt seine Adresse — und nur sie."""
    erlaubt = herkunft.netze(("192.168.178.55",))
    assert herkunft.vertraut("192.168.178.55", erlaubt)
    assert not herkunft.vertraut("127.0.0.1", erlaubt)


def test_die_liste_nimmt_auch_ein_netz():
    erlaubt = herkunft.netze(("192.168.178.0/24", "::1"))
    assert herkunft.vertraut("192.168.178.55", erlaubt)
    assert herkunft.vertraut("::1", erlaubt)
    assert not herkunft.vertraut("10.0.0.5", erlaubt)


def test_die_selbstprobe_besteht_wo_die_pruefung_traegt():
    assert herkunft.selbstprobe()


def test_die_selbstprobe_meldet_einen_kern_dem_jede_adresse_eigen_ist(monkeypatch, caplog):
    """Der Fall, den kein Test von der Box aus sieht: ``ip_nonlocal_bind`` sagt zu allem ja."""
    monkeypatch.setattr(herkunft, "ist_eigene", lambda gefragt: True)
    with caplog.at_level(logging.ERROR):
        assert not herkunft.selbstprobe()
    assert herkunft.NONLOCAL_SCHALTER in caplog.text
    assert herkunft.TRUSTED_VARIABLE in caplog.text


def test_ein_unlesbarer_eintrag_faellt_heraus_statt_zu_sperren():
    """Ein Tippfehler darf nicht dieselbe Aussperrung noch einmal sein."""
    assert herkunft.netze(("kein Netz", "127.0.0.1")) == (ipaddress.ip_network("127.0.0.1/32"),)
    # Fällt alles heraus, gilt wieder die Maschine selbst — enger, nicht weiter.
    assert herkunft.netze(("kein Netz",)) == ()
    assert not herkunft.vertraut(FREMD, herkunft.netze(("kein Netz",)))
