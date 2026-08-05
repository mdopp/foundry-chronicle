import logging

import pytest
import requests
import socketio
from conftest import PASSWORT, UNSER_KONTO

from chronicle.config import Config
from chronicle.foundry.client import (
    FoundryClient,
    FoundryLoginFailed,
    FoundryNotConfigured,
    FoundryUnreachable,
)

JOIN_DATEN = {
    "users": [
        {"_id": UNSER_KONTO, "name": "Chronist"},
        {"_id": "u-mira", "name": "Mira"},
    ]
}


class Antwort:
    def __init__(self, payload=None, fehler=None):
        self._payload = payload
        self._fehler = fehler

    def raise_for_status(self):
        if self._fehler is not None:
            raise self._fehler

    def json(self):
        if self._payload is None:
            raise ValueError("kein JSON")
        return self._payload


class Http:
    def __init__(self, *, login=None, fehler=None):
        self.cookies = {}
        self.aufrufe = []
        self._login = {} if login is None else login
        self._fehler = fehler

    def request(self, method, url, **kwargs):
        self.aufrufe.append((method, url, kwargs))
        if self._fehler is not None:
            raise self._fehler
        self.cookies["session"] = "sitzung-1" if method == "GET" else "sitzung-2"
        return Antwort({} if method == "GET" else self._login)


class Socket:
    def __init__(self, antworten, sitzung, verbindungsfehler):
        self.antworten = antworten
        self._sitzung = sitzung
        self._verbindungsfehler = verbindungsfehler
        self.handler = {}
        self.url = None
        self.headers = None
        self.getrennt = False

    def on(self, event, handler):
        self.handler[event] = handler

    def connect(self, url, **kwargs):
        self.url = url
        self.headers = kwargs.get("headers")
        if self._verbindungsfehler is not None:
            raise self._verbindungsfehler
        if "session" in self.handler and self._sitzung is not None:
            self.handler["session"](self._sitzung)

    def call(self, event, timeout=None):
        antwort = self.antworten.get(event)
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    def disconnect(self):
        self.getrennt = True


STANDARD_SITZUNG = {"userId": UNSER_KONTO}


class Sockets:
    def __init__(self, antworten, *, sitzung=STANDARD_SITZUNG, verbindungsfehler=None):
        self.antworten = antworten
        self.sitzung = sitzung
        self.verbindungsfehler = verbindungsfehler
        self.erzeugt = []

    def __call__(self):
        socket = Socket(self.antworten, self.sitzung, self.verbindungsfehler)
        self.erzeugt.append(socket)
        return socket


WELT_ANTWORT = {"users": [], "actors": [], "messages": []}


def client(config, http, sockets, **kwargs):
    return FoundryClient(config, http=lambda: http, socket=sockets, **kwargs)


def test_handschlag_laeuft_in_vier_schritten(config):
    http = Http()
    sockets = Sockets({"getJoinData": JOIN_DATEN, "world": WELT_ANTWORT})

    user_id, welt = client(config, http, sockets).fetch_world()

    assert user_id == UNSER_KONTO
    assert welt == WELT_ANTWORT
    assert [(m, u) for m, u, _ in http.aufrufe] == [
        ("GET", "https://foundry.example/join"),
        ("POST", "https://foundry.example/join"),
    ]
    assert len(sockets.erzeugt) == 2
    assert sockets.erzeugt[0].url == "https://foundry.example"
    assert sockets.erzeugt[0].headers == {"Cookie": "session=sitzung-1"}
    assert sockets.erzeugt[1].url == "https://foundry.example?session=sitzung-2"
    assert sockets.erzeugt[1].headers == {"Cookie": "session=sitzung-2"}
    assert all(socket.getrennt for socket in sockets.erzeugt)


def test_die_anmeldung_nimmt_die_id_aus_getjoindata(config):
    http = Http()
    client(config, http, Sockets({"getJoinData": JOIN_DATEN, "world": WELT_ANTWORT})).fetch_world()
    _, _, kwargs = http.aufrufe[1]
    assert kwargs["json"] == {
        "action": "join",
        "userid": UNSER_KONTO,
        "password": PASSWORT,
    }


def test_unbekannter_benutzer_meldet_einen_anmeldefehler(config):
    sockets = Sockets({"getJoinData": {"users": [{"_id": "u-mira", "name": "Mira"}]}})
    with pytest.raises(FoundryLoginFailed):
        client(config, Http(), sockets).fetch_world()


def test_abgelehnte_anmeldung_meldet_einen_anmeldefehler(config):
    http = Http(login={"status": "failed"})
    sockets = Sockets({"getJoinData": JOIN_DATEN, "world": WELT_ANTWORT})
    with pytest.raises(FoundryLoginFailed):
        client(config, http, sockets).fetch_world()


def test_fremde_sitzungsbindung_meldet_einen_anmeldefehler(config):
    sockets = Sockets(
        {"getJoinData": JOIN_DATEN, "world": WELT_ANTWORT},
        sitzung={"userId": "u-mira"},
    )
    with pytest.raises(FoundryLoginFailed):
        client(config, Http(), sockets).fetch_world()


def test_ohne_session_ereignis_meldet_es_einen_anmeldefehler(config):
    sockets = Sockets({"getJoinData": JOIN_DATEN, "world": WELT_ANTWORT}, sitzung=None)
    with pytest.raises(FoundryLoginFailed):
        client(config, Http(), sockets, timeout=0.05).fetch_world()


def test_netzwerkfehler_wird_zu_nicht_erreichbar(config):
    sockets = Sockets({"getJoinData": JOIN_DATEN})
    with pytest.raises(FoundryUnreachable):
        client(config, Http(fehler=requests.ConnectionError("weg")), sockets).fetch_world()


def test_socketio_verbindungsfehler_wird_zu_nicht_erreichbar(config):
    sockets = Sockets(
        {"getJoinData": JOIN_DATEN},
        verbindungsfehler=socketio.exceptions.ConnectionError("weg"),
    )
    with pytest.raises(FoundryUnreachable):
        client(config, Http(), sockets).fetch_world()


def test_zeitueberschreitung_beim_weltabruf_wird_zu_nicht_erreichbar(config):
    sockets = Sockets(
        {"getJoinData": JOIN_DATEN, "world": socketio.exceptions.TimeoutError()},
    )
    with pytest.raises(FoundryUnreachable):
        client(config, Http(), sockets).fetch_world()


def test_antwort_ohne_daten_wird_zu_nicht_erreichbar(config):
    with pytest.raises(FoundryUnreachable):
        client(config, Http(), Sockets({"getJoinData": None})).fetch_world()


def test_ohne_konfiguration_wird_gar_nicht_erst_verbunden(tmp_path):
    with pytest.raises(FoundryNotConfigured) as fehler:
        FoundryClient(Config(data_dir=tmp_path))
    assert "FOUNDRY_PASSWORD" in str(fehler.value)


def test_das_passwort_steht_in_keiner_logzeile(config, caplog):
    http = Http()
    sockets = Sockets({"getJoinData": JOIN_DATEN, "world": WELT_ANTWORT})
    with caplog.at_level(logging.DEBUG):
        client(config, http, sockets).fetch_world()
    assert caplog.records
    assert PASSWORT not in caplog.text


def test_das_passwort_steht_in_keiner_fehlermeldung(config, caplog):
    http = Http(fehler=requests.ConnectionError(f"POST-Rumpf: {PASSWORT}"))
    sockets = Sockets({"getJoinData": JOIN_DATEN})
    with caplog.at_level(logging.DEBUG), pytest.raises(FoundryUnreachable) as fehler:
        client(config, http, sockets).fetch_world()
    assert PASSWORT not in str(fehler.value)
    assert fehler.value.__cause__ is None
    assert PASSWORT not in caplog.text
