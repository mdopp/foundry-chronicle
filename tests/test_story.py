"""Der Lebenslauf einer Instanz an einem Stück — vom Aufsetzen bis zur ersten Chronik.

Die Stationen stehen in der Reihenfolge, in der eine Gruppe sie durchläuft, und jede
prüft, was an ihr neu ist. Der Test ist damit auch die Beschreibung des Systems: wer
wissen will, was Foundry Chronicle tut, liest ihn von oben nach unten.

Zwei Außengrenzen laufen dabei **echt**, nicht als Funktions-Fake:

* Ein Mock-Foundry spricht den Handschlag aus ``docs/foundry-zugriff.md`` über HTTP und
  Socket.io. Der Client-Code ist der echte; erst hier hängen Cookie, Query-Parameter,
  Ereignisfolge und Antwortform mit an der Prüfung.
* Ein Mock-Ollama antwortet auf ``/api/tags`` und ``/api/chat`` — und erfindet für eine
  Szene eine Zahl. Die Zahlenschranke ist die teuerste Regel des Hauses; hier bekommt
  sie ihren Beweis gegen ein Modell, das sie wirklich verletzt.

Alle Namen sind erfunden. Ein echter Weltabzug ist personenbezogen und gehört nie ins
Repo.
"""

from __future__ import annotations

import pytest
from conftest import runde
from mocks import foundry_mock, ollama_mock

from chronicle import db, foundry, notes, protocol, settings
from chronicle.app import create_app
from chronicle.compose.composer import (
    BELEG_TITEL,
    NOTIZEN_TITEL,
    VERBINDUNG_TITEL,
    VERWORFEN,
)
from chronicle.compose.recap import FAEDEN_TITEL
from chronicle.compose.service import compose_session, recap_session
from chronicle.config import Config
from chronicle.foundry import testwelt

# Authelia setzt den Header am Proxy; die App baut kein eigenes Login.
KOPF = {"Remote-User": "erzaehlerin"}

GESPIELT_AM = "2026-05-16"
TITEL = "Der Halbe Mond"

ZWEITE_SZENE = "Der Sturz in die Zisterne"
DRITTE_SZENE = "Was die Wirtin verschwieg"

# Bewusst ohne Ziffern: so stammt jede Zahl in der Chronik aus Foundry und aus sonst
# nichts — genau das prüft Station 5.
NOTIZEN = (
    "Wir kamen im Regen am Halben Mond an und nahmen im Schankraum Platz.",
    "Hendrik rutschte auf dem nassen Rand ab und fiel in die Zisterne.",
    "Die Wirtin wich der Frage nach dem Keller aus.",
)

FAKTEN = (foundry_mock.ANKUNFT, foundry_mock.STURZ, foundry_mock.WIRTIN)

SUCHBEGRIFF = "Zisterne"


class NurEineWelt:
    """Ein Foundry, das eine andere Welt offen hat — dafür reicht der Rohdump."""

    def __init__(self, welt):
        self._welt = welt

    def fetch_world(self):
        return foundry_mock.UNSER_KONTO, self._welt


@pytest.fixture
def mock_foundry():
    server = foundry_mock.MockFoundry()
    yield server
    server.stop()


@pytest.fixture
def mock_ollama():
    server = ollama_mock.MockOllama(vergiftete_szene=ZWEITE_SZENE)
    yield server
    server.stop()


def hole(client, pfad: str, *, folgen: bool = False):
    return client.get(pfad, headers=KOPF, follow_redirects=folgen)


def sende(client, pfad: str, **felder: str):
    return client.post(pfad, data=felder, headers=KOPF)


def verknuepfe(config: Config, paare: list[tuple[int, str]]) -> None:
    """Nachricht an Szene hängen — dafür gibt es noch keine Ansicht, nur die Tabelle."""
    scope = db.scoped(runde(config))
    try:
        with scope:
            scope.executemany(
                "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) "
                "VALUES (?, ?, ?)",
                [(scope.runde_id, *paar) for paar in paare],
            )
    finally:
        scope.close()


def station_1_aufsetzen(tmp_path):
    """Frische Instanz auf Wegwerf-Verzeichnissen: sie führt in die Einrichtung."""
    config = Config(
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "spuren",
        require_remote_user=True,
    )
    client = create_app(config).test_client()
    assert config.database_path.is_file()

    assert client.get("/status").status_code == 403
    assert client.get("/einrichtung/foundry").status_code == 403

    assert hole(client, "/").headers["Location"] == "/einrichtung"
    schritt = hole(client, "/einrichtung", folgen=True).get_data(as_text=True)
    assert "Schritt 1 von 3" in schritt

    # Der alte Status-Pfad steht in Lesezeichen; er landet bei der Foundry-Karte.
    assert hole(client, "/status").status_code == 301
    seite = hole(client, "/status", folgen=True).get_data(as_text=True)
    assert "Noch kein Zugang zu Foundry" in seite
    assert "dann wird nur geordnet, nicht formuliert" in seite
    return config, client


def station_2_konfigurieren(client, mock_foundry, mock_ollama):
    """Der Wizard, Schritt für Schritt — das Passwort wird hier gar nicht erst gefragt."""
    antwort = sende(
        client,
        "/einrichtung/foundry",
        foundry_url=mock_foundry.url,
        foundry_user=foundry_mock.BENUTZER,
    )
    assert antwort.status_code == 302
    assert antwort.headers["Location"] == "/einrichtung/discord"

    # Eine Präsenzgruppe braucht keinen Bot — überspringen ist immer möglich.
    antwort = sende(client, "/einrichtung/discord", tat="ueberspringen")
    assert antwort.headers["Location"] == "/einrichtung/ollama"

    antwort = sende(
        client,
        "/einrichtung/ollama",
        ollama_url=mock_ollama.url,
        ollama_model=ollama_mock.MODELL,
    )
    assert antwort.headers["Location"] == "/"

    # Fertig heißt fertig: die Startseite ist wieder die Sitzungsseite.
    assert hole(client, "/").status_code == 200

    formular = hole(client, "/einstellungen").get_data(as_text=True)
    assert foundry_mock.PASSWORT not in formular
    assert "das Passwort wird nirgends gespeichert" in formular
    assert ollama_mock.MODELL in formular
    assert ollama_mock.EINBETTUNG not in formular

    status = hole(client, "/status", folgen=True).get_data(as_text=True)
    assert "Zugang steht" in status
    assert foundry_mock.PASSWORT not in status


def station_3_erster_abgleich(config, client):
    """Der Handschlag über eine echte Verbindung — und die Filterung vor dem Speicher."""
    # Ohne Passwort gibt es keinen Versuch: es steht nirgends und wird hier gereicht.
    ohne = foundry.sync(config, runde(config))
    assert ohne.stale
    assert "nirgends gespeichert" in ohne.message

    stand = foundry.sync(config, runde(config), passwort=foundry_mock.PASSWORT)
    assert not stand.stale, stand.message
    welt = stand.snapshot
    assert welt.system == "daggerheart"

    figuren = {figur.name: figur for figur in welt.characters}
    assert foundry_mock.VERBORGENE_FIGUR not in figuren
    wirtin = figuren[foundry_mock.LIMITIERTE_FIGUR]
    assert wirtin.limited
    assert wirtin.type is None
    assert wirtin.owner_ids == ()

    nachrichten = {nachricht.id: nachricht for nachricht in welt.messages}
    assert foundry_mock.GM_FLUESTER not in nachrichten
    assert foundry_mock.BLINDER_WURF not in nachrichten

    sturz = nachrichten[foundry_mock.STURZ]
    assert sturz.content == ""
    assert sturz.roll.total == foundry_mock.STURZ_SUMME
    assert [(wuerfel.name, wuerfel.value) for wuerfel in sturz.roll.dice] == [
        ("hope", foundry_mock.STURZ_HOFFNUNG),
        ("fear", foundry_mock.STURZ_FURCHT),
    ]

    assert "daggerheart" in hole(client, "/status", folgen=True).get_data(as_text=True)

    # Die Runde hängt jetzt an dieser Welt. Zeigt der Server eine andere, wird nichts
    # übernommen — sonst stünde die falsche Kampagne in dieser Chronik.
    fremde = dict(foundry_mock.WELT, world={"id": "andere-welt", "title": "Eine andere Welt"})
    verweigert = foundry.sync(
        config, runde(config), passwort=foundry_mock.PASSWORT, client=NurEineWelt(fremde)
    )
    assert verweigert.stale
    assert "andere Welt" in verweigert.message
    assert foundry_mock.WELT_TITEL in verweigert.message
    assert len(foundry.current(config, runde(config)).snapshot.messages) == len(welt.messages)


def station_4_erste_sitzung(client, config):
    """Sitzung, Szenen und Notizen per HTTP; die Foundry-Fakten kommen an die Szenen."""
    antwort = sende(client, "/", played_on=GESPIELT_AM, title=TITEL)
    assert antwort.status_code == 302
    sitzung_id = int(antwort.headers["Location"].rsplit("/", 1)[-1])

    for titel in (ZWEITE_SZENE, DRITTE_SZENE):
        assert sende(client, f"/sitzungen/{sitzung_id}/szenen", title=titel).status_code == 302

    szenen = notes.session(runde(config), sitzung_id).scenes
    assert [szene.title for szene in szenen] == [None, ZWEITE_SZENE, DRITTE_SZENE]

    for szene, notiz in zip(szenen, NOTIZEN, strict=True):
        assert sende(client, f"/szenen/{szene.id}/notizen", text=notiz).status_code == 302

    verknuepfe(config, list(zip((szene.id for szene in szenen), FAKTEN, strict=True)))
    return sitzung_id


def station_5_erste_zusammenfassung(config, sitzung_id, mock_ollama):
    """Komposition gegen das Mock-Modell — und die Zahlenschranke gegen dessen Erfindung."""
    chronik = compose_session(config, runde(config), sitzung_id)
    assert chronik.reason is None
    assert chronik.model_name == ollama_mock.MODELL
    assert (chronik.scene_count, chronik.fact_count, chronik.prose_count) == (3, 3, 2)

    assert f"Summe {foundry_mock.STURZ_SUMME}" in chronik.text
    assert f"Summe {foundry_mock.WIRTIN_SUMME}" in chronik.text
    assert ollama_mock.ERFUNDENE_ZAHL in "\n".join(mock_ollama.antworten)
    assert ollama_mock.ERFUNDENE_ZAHL not in chronik.text
    assert VERWORFEN in chronik.text

    for titel in (NOTIZEN_TITEL, BELEG_TITEL, VERBINDUNG_TITEL):
        assert titel in chronik.text
    assert protocol.stored(runde(config), sitzung_id).text == chronik.text

    rueckblick = recap_session(config, runde(config), sitzung_id)
    assert rueckblick.reason is None
    assert rueckblick.thread_count == 2
    assert FAEDEN_TITEL in rueckblick.text
    assert ollama_mock.ERFUNDENE_ZAHL not in rueckblick.text


def station_6_wiederfinden(client, sitzung_id):
    """Wochen später: die Suche findet den Begriff, die Seite trennt sichtbar."""
    treffer = hole(client, f"/suche?q={SUCHBEGRIFF}").get_data(as_text=True)
    assert f"<mark>{SUCHBEGRIFF}" in treffer
    assert f"/sitzungen/{sitzung_id}/protokoll" in treffer

    seite = hole(client, f"/sitzungen/{sitzung_id}/protokoll").get_data(as_text=True)
    for klasse in ("belegt", "verbindung", "deutung"):
        assert f'class="abschnitt {klasse}"' in seite
    assert ollama_mock.ERFUNDENE_ZAHL not in seite


def test_vom_aufsetzen_bis_zur_ersten_chronik(tmp_path, mock_foundry, mock_ollama):
    """Aufsetzen, konfigurieren, abgleichen, mitschreiben, komponieren, wiederfinden."""
    config, client = station_1_aufsetzen(tmp_path)
    station_2_konfigurieren(client, mock_foundry, mock_ollama)
    station_3_erster_abgleich(config, client)
    sitzung_id = station_4_erste_sitzung(client, config)
    station_5_erste_zusammenfassung(config, sitzung_id, mock_ollama)
    station_6_wiederfinden(client, sitzung_id)


def test_dieselben_stationen_gegen_die_eingebaute_testwelt(tmp_path):
    """Dieselbe Geschichte ohne Server: die Quelle steht auf Testwelt.

    Das ist die zweite Hälfte des Beweises. Der Handschlag ist oben abgedeckt; hier zählt
    die Strecke **danach** — und dass die Oberfläche keinen Zweifel lässt, woher die
    Zahlen stammen.
    """
    config, client = station_1_aufsetzen(tmp_path)
    umstellen = sende(client, "/einstellungen", **{settings.QUELLE_KEY: settings.TESTWELT})
    assert umstellen.status_code == 302

    stand = foundry.sync(config, runde(config))
    assert not stand.stale, stand.message
    assert "keine echten Kampagnendaten" in stand.message
    assert stand.snapshot.characters and stand.snapshot.messages

    # Die Einrichtung ist damit entschieden — und jede Seite sagt, was hier läuft.
    assert hole(client, "/").status_code == 200
    for pfad in ("/", "/protokolle", "/einstellungen"):
        assert testwelt.HINWEIS in hole(client, pfad).get_data(as_text=True), pfad

    # Mitschreiben und Komponieren laufen unverändert weiter — die Quelle ändert nur,
    # woher die Zahlen kommen.
    angelegt = sende(client, "/", played_on=GESPIELT_AM, title=TITEL)
    sitzung_id = int(angelegt.headers["Location"].rsplit("/", 1)[-1])
    szene = notes.session(runde(config), sitzung_id).scenes[0]
    assert sende(client, f"/szenen/{szene.id}/notizen", text=NOTIZEN[0]).status_code == 302
    geschrieben = notes.session(runde(config), sitzung_id).scenes[0].notes
    assert [notiz.text for notiz in geschrieben] == [NOTIZEN[0]]
