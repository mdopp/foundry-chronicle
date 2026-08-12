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

from chronicle import db, foundry, notes, protocol, search, settings
from chronicle.app import create_app
from chronicle.compose.composer import (
    BELEG_TITEL,
    NOTIZEN_TITEL,
    VERBINDUNG_TITEL,
    VERWORFEN,
)
from chronicle.compose.recap import FAEDEN_TITEL
from chronicle.compose.service import RUECKBLICK, compose_session, recap_session
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
    """Frische Instanz auf Wegwerf-Verzeichnissen: die Haustür steht zu, die Seite ist leer."""
    config = Config(
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "spuren",
        require_remote_user=True,
    )
    client = create_app(config).test_client()
    assert config.database_path.is_file()

    assert client.get("/").status_code == 403
    assert client.get("/einstellungen").status_code == 403

    # Der alte Status-Pfad steht in Lesezeichen; er landet auf der Betreiber-Seite.
    assert hole(client, "/status").status_code == 301
    seite = hole(client, "/status", folgen=True).get_data(as_text=True)
    assert "Noch kein Bot-Token gesetzt" in seite
    assert "dann wird nur geordnet, nicht formuliert" in seite
    return config, client


def station_2_konfigurieren(config, client, mock_foundry, mock_ollama):
    """Zwei Orte, zwei Zuständigkeiten: die Runde in Discord, die Instanz auf der Seite.

    Was einer Gilde gehört — Foundry-Zugang, Zustellkanal, Nachtlauf — pflegt ``/setup``
    (``bot.einrichten``); was der Instanz gehört, steht auf der Betreiber-Seite. Geprüft
    wird hier der Schreibweg, nicht das Discord-Fenster darüber: das steht in
    ``test_erinnern`` und ``test_bot``.
    """
    gruppe = runde(config)
    settings.save(
        gruppe,
        {"foundry_url": mock_foundry.url, "foundry_user": foundry_mock.BENUTZER},
    )

    antwort = sende(
        client, "/einstellungen", ollama_url=mock_ollama.url, ollama_model=ollama_mock.MODELL
    )
    assert antwort.status_code == 302

    aktuell = settings.effective(config, gruppe)
    assert aktuell.foundry_configured
    assert aktuell.ollama_model == ollama_mock.MODELL

    formular = hole(client, "/einstellungen").get_data(as_text=True)
    assert foundry_mock.PASSWORT not in formular
    assert ollama_mock.MODELL in formular
    assert ollama_mock.EINBETTUNG not in formular
    # Der Foundry-Zugang gehört der Runde: er steht auf der Betreiber-Seite nicht mehr.
    assert foundry_mock.BENUTZER not in formular


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

    assert foundry.current(config, runde(config)).snapshot.system == "daggerheart"

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


def station_4_erste_sitzung(config):
    """Sitzung, Szenen und Notizen — in Discord ``/chronik start``, ``/szene``, der Thread."""
    gruppe = runde(config)
    sitzung_id = notes.create_session(gruppe, played_on=GESPIELT_AM, title=TITEL)

    for titel in (ZWEITE_SZENE, DRITTE_SZENE):
        assert notes.add_scene(gruppe, sitzung_id, title=titel) is not None

    szenen = notes.session(gruppe, sitzung_id).scenes
    assert [szene.title for szene in szenen] == [None, ZWEITE_SZENE, DRITTE_SZENE]

    for szene, notiz in zip(szenen, NOTIZEN, strict=True):
        notes.add_note(gruppe, szene.id, notiz)

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


def station_6_wiederfinden(config, sitzung_id):
    """Wochen später: ``/suche`` findet den Begriff, die Chronik trennt sichtbar."""
    ergebnis = search.find(runde(config), SUCHBEGRIFF)
    treffer = [hit for gruppe in ergebnis.groups for hit in gruppe.hits]
    assert treffer, ergebnis
    assert any(SUCHBEGRIFF in hit.raw for hit in treffer)
    assert any(hit.session_id == sitzung_id for hit in treffer)

    abgelegt = protocol.stored(runde(config), sitzung_id).text
    for titel in (BELEG_TITEL, VERBINDUNG_TITEL):
        assert titel in abgelegt
    assert FAEDEN_TITEL in protocol.stored(runde(config), sitzung_id, RUECKBLICK).text
    assert ollama_mock.ERFUNDENE_ZAHL not in abgelegt


def test_vom_aufsetzen_bis_zur_ersten_chronik(tmp_path, mock_foundry, mock_ollama):
    """Aufsetzen, konfigurieren, abgleichen, mitschreiben, komponieren, wiederfinden."""
    config, client = station_1_aufsetzen(tmp_path)
    station_2_konfigurieren(config, client, mock_foundry, mock_ollama)
    station_3_erster_abgleich(config, client)
    sitzung_id = station_4_erste_sitzung(config)
    station_5_erste_zusammenfassung(config, sitzung_id, mock_ollama)
    station_6_wiederfinden(config, sitzung_id)


def test_dieselben_stationen_gegen_die_eingebaute_testwelt(tmp_path):
    """Dieselbe Geschichte ohne Server: die Quelle steht auf Testwelt.

    Das ist die zweite Hälfte des Beweises. Der Handschlag ist oben abgedeckt; hier zählt
    die Strecke **danach** — und dass kein Abgleich verschweigt, woher die Zahlen stammen.
    """
    config, _ = station_1_aufsetzen(tmp_path)
    # Die Quelle gehört der Runde; gewählt wird sie in Discord (``bot.einrichten``).
    assert settings.save_foundry_quelle(runde(config), settings.TESTWELT)

    stand = foundry.sync(config, runde(config))
    assert not stand.stale, stand.message
    assert testwelt.HINWEIS in stand.message
    assert "keine echten Kampagnendaten" in stand.message
    assert stand.snapshot.characters and stand.snapshot.messages

    # Mitschreiben und Komponieren laufen unverändert weiter — die Quelle ändert nur,
    # woher die Zahlen kommen.
    gruppe = runde(config)
    sitzung_id = notes.create_session(gruppe, played_on=GESPIELT_AM, title=TITEL)
    szene = notes.session(gruppe, sitzung_id).scenes[0]
    notes.add_note(gruppe, szene.id, NOTIZEN[0])
    geschrieben = notes.session(gruppe, sitzung_id).scenes[0].notes
    assert [notiz.text for notiz in geschrieben] == [NOTIZEN[0]]
