"""Der Diktat-Kanal — gegen ein nachgebautes Discord, ohne Netz.

Kein Test spricht mit discord.com. Das Gegenüber ist ein paar Zeilen Wörterbuch: zwei
Gilden, ein paar Kanäle, Nachrichten im Speicher. Der Token in diesen Tests ist erfunden
und steht nur hier.

**Zwei** Gilden, und in beiden ein ``#diktat`` — seit #192 ist das die tragende Form
dieser Suite: die fremde steht in der Gildenliste zuerst, und ihr Briefkasten beantwortet
keinen Abruf. Eine Suche über alle Gilden fände dort die erstbeste und fiele hier über
jeden zweiten Test. Der Briefkasten gehört der Gilde der Runde, und nur ihr.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
import requests
from conftest import runde

import chronicle.discord.__main__ as entry
from chronicle import db, lebenszyklus, notes, recordings, settings
from chronicle import runde as runden
from chronicle.config import Config
from chronicle.discord import service
from chronicle.discord.client import (
    API,
    Attachment,
    DiscordClient,
    DiscordNotConfigured,
    DiscordUnreachable,
)
from chronicle.discord.service import run

TOKEN = "bot-token-nur-fuer-den-test"

GILDE = "g-runde"
DIKTAT_KANAL = "c-diktat"
GRUPPEN_KANAL = "c-allgemein"

FREMDE_GILDE = "g-nachbarn"
FREMDER_KANAL = "c-nachbardiktat"

CDN = "https://cdn.discord.example"

DIKTAT = b"kein echtes Audio, aber Bytes"


class Antwort:
    def __init__(self, payload=None, inhalt=b"", fehler=None):
        self._payload = payload
        self._inhalt = inhalt
        self._fehler = fehler

    def raise_for_status(self):
        if self._fehler is not None:
            raise self._fehler

    def json(self):
        if self._payload is None:
            raise ValueError("kein JSON")
        return self._payload

    def iter_content(self, groesse):
        yield from (self._inhalt[i : i + groesse] for i in range(0, len(self._inhalt), groesse))


def nachricht(kennung, *, text="", anhaenge=(), bot=False, typ=0):
    return {
        "id": kennung,
        "type": typ,
        "content": text,
        "author": {"id": "u-erzaehler", "bot": bot},
        "attachments": [
            {"filename": name, "url": f"{CDN}/{name}", "size": groesse}
            for name, groesse in anhaenge
        ],
    }


class FakeDiscord:
    """Discords REST-API, so weit der Briefkasten es braucht.

    ``echo`` legt die Antwort des Bots als neue Nachricht in den Kanal — so, wie es
    im echten Discord passiert. Das braucht genau ein Test; sonst wäre in jedem
    anderen der Zeiger schwer zu lesen.

    Der Bot steht in zwei Gilden, die fremde zuerst genannt, und auch sie hat ein
    ``#diktat``. Wer über die Gilden hinaus suchte, fände ihres — und stünde vor einem
    Briefkasten, der keinen Abruf beantwortet.
    """

    def __init__(self, *nachrichten, kanal="diktat", dateien=None, echo=False):
        self.nachrichten = list(nachrichten)
        self.kanal = kanal
        self.dateien = dateien or {}
        self.echo = echo
        self.reaktionen = []
        self.antworten = []
        self.kopfzeilen = []
        self.abrufe = []

    # -- die API-Aufrufe des Klienten --------------------------------------------------

    def request(self, method, url, **kwargs):
        self.kopfzeilen.append((url, kwargs.get("headers", {})))
        pfad = url[len(API) :]
        if pfad == "/users/@me/guilds":
            return Antwort(
                [{"id": FREMDE_GILDE, "name": "Die Nachbarn"}, {"id": GILDE, "name": "Die Runde"}]
            )
        if pfad == f"/guilds/{FREMDE_GILDE}/channels":
            return Antwort([{"id": FREMDER_KANAL, "name": self.kanal, "type": 0}])
        if pfad == f"/guilds/{GILDE}/channels":
            return Antwort(
                [
                    {"id": GRUPPEN_KANAL, "name": "allgemein", "type": 0},
                    {"id": "c-sprache", "name": self.kanal, "type": 2},
                    {"id": DIKTAT_KANAL, "name": self.kanal, "type": 0},
                ]
            )
        if pfad.startswith(f"/channels/{DIKTAT_KANAL}/messages") and method == "GET":
            return Antwort(self._seit(kwargs.get("params", {})))
        if method == "PUT" and "/reactions/" in pfad:
            teile = pfad.split("/")
            self.reaktionen.append((teile[4], unquote(teile[6])))
            return Antwort({})
        if method == "POST" and pfad == f"/channels/{DIKTAT_KANAL}/messages":
            rumpf = kwargs["json"]
            self.antworten.append((rumpf["message_reference"]["message_id"], rumpf["content"]))
            if self.echo:
                quittung = nachricht(self._naechste(), text=rumpf["content"], bot=True)
                self.nachrichten.append(quittung)
            return Antwort({})
        raise AssertionError(f"unerwarteter Aufruf: {method} {pfad}")

    def _naechste(self):
        return str(max(int(eintrag["id"]) for eintrag in self.nachrichten) + 1)

    def get(self, url, **kwargs):
        self.abrufe.append((url, kwargs.get("headers")))
        name = url.rsplit("/", 1)[-1]
        if name not in self.dateien:
            raise requests.ConnectionError("weg")
        return Antwort(inhalt=self.dateien[name])

    def _seit(self, params):
        after = params.get("after")
        offen = [
            eintrag
            for eintrag in self.nachrichten
            if after is None or int(eintrag["id"]) > int(after)
        ]
        # Discord liefert die neueste zuerst.
        return sorted(offen, key=lambda eintrag: int(eintrag["id"]), reverse=True)


@pytest.fixture
def config(tmp_path):
    gesetzt = Config(
        discord_bot_token=TOKEN,
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "aufnahmen",
    )
    db.init(gesetzt.database_path)
    # Die Runde dieser Tests hängt an ihrer Gilde. Ohne sie gibt es keinen Briefkasten:
    # gesucht wird der Kanal in *ihrer* Gilde, nicht in irgendeiner, in der der Bot auch
    # steht (#192). Ein eigener Test hält den Fall ohne Gilde fest.
    lebenszyklus.verwaiste_uebernehmen(gesetzt, (lebenszyklus.Gilde(GILDE, "Die Runde"),))
    return gesetzt


@pytest.fixture
def sitzung_id(config):
    return notes.create_session(runde(config), played_on="2026-08-06", title="Der Keller")


def klient(config, api):
    return DiscordClient(config, http=lambda: api)


def leeren(config, api):
    return run(config, runde(config), client=klient(config, api))


# --- Der Kanal: genau einer, gefunden über den Namen, in der eigenen Gilde -----------


def test_geleert_wird_der_briefkasten_der_eigenen_gilde(config, sitzung_id):
    """Beide Gilden haben ein ``#diktat``; die fremde steht zuerst in der Liste.

    Bis #192 gewann sie damit — und ihre Einwürfe landeten in dieser Chronik. Der Beleg,
    dass sie es nicht mehr tut, ist der Abruf selbst: der Briefkasten der Nachbarn
    beantwortet keinen, ein Griff dorthin flöge auf.
    """
    api = FakeDiscord(nachricht("100", text="Die Wirtin hat gelogen."))

    leeren(config, api)

    angefasst = {url for url, _ in api.kopfzeilen if "/channels/" in url}
    assert angefasst and all(FREMDER_KANAL not in url for url in angefasst)
    assert any(url.startswith(f"{API}/channels/{DIKTAT_KANAL}/messages") for url in angefasst)
    szene = notes.session(runde(config), sitzung_id).scenes[-1]
    assert [eintrag.text for eintrag in szene.notes] == ["Die Wirtin hat gelogen."]


def test_ohne_gilde_wird_kein_briefkasten_geraten(tmp_path, caplog):
    """Die Runde aus der Zeit der Weboberfläche hängt an keiner Gilde.

    Sie hat damit keinen Briefkasten — und bekommt keinen geliehen. Geraten wäre hier das
    Leck selbst: der geratene Kanal gehört einer fremden Gruppe, und was darin liegt,
    würde verschriftet und stünde danach in dieser Chronik.
    """
    ohne = Config(
        discord_bot_token=TOKEN,
        data_dir=tmp_path / "daten",
        recordings_dir=tmp_path / "aufnahmen",
    )
    db.init(ohne.database_path)
    api = FakeDiscord(nachricht("100", text="Die Wirtin hat gelogen."))

    with caplog.at_level("WARNING"):
        meldungen = run(ohne, runden.erste(ohne.database_path), client=klient(ohne, api))

    assert meldungen == (service.OHNE_GILDE,)
    assert api.kopfzeilen == []
    assert "keiner Gilde" in caplog.text


def test_ohne_den_kanal_sagt_der_lauf_das_statt_still_zu_stehen(config, caplog):
    """Kein ``#diktat`` in der eigenen Gilde — auch dann keines aus einer fremden.

    Der Satz an die Gruppe nennt die Gilde nicht: als Kennung sagt sie niemandem etwas,
    und die einer fremden wäre das eine, was hier nicht hinausgehen soll. Der Betreiber
    findet sie im Log.
    """
    api = FakeDiscord(nachricht("100", text="Hallo"), kanal="plauderei")

    with caplog.at_level("WARNING"):
        meldungen = leeren(config, api)

    assert meldungen == (service.KEIN_KANAL,)
    assert api.antworten == []
    assert FREMDE_GILDE not in service.KEIN_KANAL
    assert GILDE in caplog.text


# --- Der Kanal einer Gilde: über die Id oder den Namen, aber nie über die Gilde hinaus --


def test_der_kanal_einer_gilde_wird_ueber_die_id_gefunden(config):
    api = FakeDiscord()
    assert klient(config, api).guild_channel_id(GILDE, DIKTAT_KANAL) == DIKTAT_KANAL


def test_der_kanal_einer_gilde_wird_auch_ueber_den_namen_gefunden(config):
    api = FakeDiscord()
    assert klient(config, api).guild_channel_id(GILDE, service.KANAL) == DIKTAT_KANAL


def test_in_einer_fremden_gilde_wird_gar_nicht_erst_gesucht(config):
    """Der Bot steht in mehreren Gilden; eine, die dieser Runde nicht gehört, ist keine
    Quelle und kein Ziel — auch dann nicht, wenn sie einen passenden Kanal hätte."""
    api = FakeDiscord()
    assert klient(config, api).guild_channel_id("g-fremde", service.KANAL) is None


def test_ohne_token_wird_kein_klient_gebaut(tmp_path):
    with pytest.raises(DiscordNotConfigured) as fehler:
        DiscordClient(Config(data_dir=tmp_path))
    assert "DISCORD_BOT_TOKEN" in str(fehler.value)


def test_ohne_token_laeuft_der_stapel_trotzdem_und_sagt_es(tmp_path):
    config = Config(data_dir=tmp_path)
    assert run(config, runde(config)) == (service.NICHT_EINGERICHTET,)


def test_ein_in_der_oberflaeche_gesetzter_token_reicht(tmp_path, monkeypatch):
    config = Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")
    db.init(config.database_path)
    lebenszyklus.verwaiste_uebernehmen(config, (lebenszyklus.Gilde(GILDE, "Die Runde"),))
    settings.save(runde(config), {"discord_bot_token": TOKEN})
    notes.create_session(runde(config), played_on="2026-08-06")
    api = FakeDiscord(nachricht("100", text="Die Wirtin hat gelogen."))
    gebaut = []

    def gemerkt(zugang):
        gebaut.append(zugang.discord_bot_token)
        return klient(zugang, api)

    monkeypatch.setattr(service, "DiscordClient", gemerkt)
    meldungen = run(config, runde(config))

    assert gebaut == [TOKEN]
    assert "Notiz" in meldungen[0]


# --- Audio: derselbe Eingang wie der Web-Upload --------------------------------------


def test_eine_sprachnachricht_landet_in_derselben_warteschlange(config, sitzung_id):
    api = FakeDiscord(
        nachricht("100", anhaenge=[("voice-message.ogg", len(DIKTAT))]),
        dateien={"voice-message.ogg": DIKTAT},
    )

    meldungen = leeren(config, api)

    warteschlange = recordings.pending(runde(config))
    assert [(zeile.session_id, zeile.status) for zeile in warteschlange] == [
        (sitzung_id, recordings.WARTET)
    ]
    spur = config.recordings_dir / warteschlange[0].filename
    assert spur.read_bytes() == DIKTAT
    assert "wartet auf den Stapel" in meldungen[0]


def test_audio_wird_mit_haken_und_genau_einer_antwort_quittiert(config, sitzung_id):
    api = FakeDiscord(
        nachricht("100", anhaenge=[("memo.m4a", len(DIKTAT))]),
        dateien={"memo.m4a": DIKTAT},
    )

    leeren(config, api)

    assert api.reaktionen == [("100", service.HAKEN)]
    assert api.antworten == [("100", service.QUITTUNG)]


def test_der_token_geht_an_discord_aber_nie_an_das_cdn(config, sitzung_id):
    api = FakeDiscord(
        nachricht("100", anhaenge=[("memo.m4a", len(DIKTAT))]),
        dateien={"memo.m4a": DIKTAT},
    )

    leeren(config, api)

    assert all(kopf["Authorization"] == f"Bot {TOKEN}" for _, kopf in api.kopfzeilen)
    assert api.abrufe == [(f"{CDN}/memo.m4a", None)]


def test_ein_zu_grosser_anhang_wird_verstaendlich_abgewiesen(config, sitzung_id):
    api = FakeDiscord(
        nachricht("100", anhaenge=[("mitschnitt.m4a", recordings.MAX_BYTES + 1)]),
        dateien={"mitschnitt.m4a": DIKTAT},
    )

    meldungen = leeren(config, api)

    assert "größer als" in meldungen[0]
    assert api.abrufe == []
    assert recordings.pending(runde(config)) == ()
    assert api.reaktionen == [("100", service.WARNUNG)]


def test_ein_abgebrochener_download_laesst_keine_halbe_spur_liegen(config, sitzung_id):
    api = FakeDiscord(nachricht("100", anhaenge=[("memo.m4a", len(DIKTAT))]))

    with pytest.raises(DiscordUnreachable) as fehler:
        leeren(config, api)

    assert "memo.m4a" in str(fehler.value)
    assert TOKEN not in str(fehler.value)
    assert list(config.recordings_dir.iterdir()) == []


# --- Text: direkt zur Notiz ----------------------------------------------------------


def test_eine_textnachricht_wird_zur_notiz_der_sitzung(config, sitzung_id):
    api = FakeDiscord(nachricht("100", text="  Die Wirtin hat gelogen.  "))

    leeren(config, api)

    szene = notes.session(runde(config), sitzung_id).scenes[-1]
    assert [eintrag.text for eintrag in szene.notes] == ["Die Wirtin hat gelogen."]
    assert api.reaktionen == [("100", service.HAKEN)]
    assert api.antworten == [("100", service.NOTIZ_QUITTUNG.format(datum="2026-08-06"))]


def test_die_notiz_geht_an_die_zuletzt_angelegte_sitzung(config):
    aelter = notes.create_session(runde(config), played_on="2026-09-01")
    juenger = notes.create_session(runde(config), played_on="2026-08-01")
    api = FakeDiscord(nachricht("100", text="Der Keller war eine Falle."))

    leeren(config, api)

    assert notes.session(runde(config), aelter).note_count == 0
    assert notes.session(runde(config), juenger).note_count == 1


def test_was_weder_audio_noch_text_ist_wird_sichtbar_uebersprungen(config, sitzung_id):
    api = FakeDiscord(nachricht("100", anhaenge=[("karte.png", 4096)]))

    meldungen = leeren(config, api)

    assert "weder Audio noch Text" in meldungen[0]
    assert api.reaktionen == [("100", service.WARNUNG)]
    assert api.antworten == []
    assert api.abrufe == []


def test_die_eigene_quittung_kommt_nicht_als_diktat_zurueck(config, sitzung_id):
    api = FakeDiscord(nachricht("100", text="Die Wirtin hat gelogen."), echo=True)

    leeren(config, api)
    leeren(config, api)

    assert notes.session(runde(config), sitzung_id).note_count == 1
    assert len(api.antworten) == 1
    assert api.reaktionen == [("100", service.HAKEN)]


# --- Ohne Sitzung wird nichts erfunden -----------------------------------------------


def test_ohne_sitzung_wartet_das_diktat_sichtbar(config):
    api = FakeDiscord(
        nachricht("100", anhaenge=[("memo.m4a", len(DIKTAT))]),
        dateien={"memo.m4a": DIKTAT},
    )

    meldungen = leeren(config, api)

    assert meldungen == (service.OHNE_SITZUNG,)
    assert api.reaktionen == [("100", service.WARNUNG)]
    assert api.antworten == [("100", service.OHNE_SITZUNG)]
    assert recordings.pending(runde(config)) == ()
    assert service.cursor(runde(config)) is None


def test_ein_wartendes_diktat_wird_nachgeholt_und_nicht_zweimal_beantwortet(config):
    api = FakeDiscord(
        nachricht("100", anhaenge=[("memo.m4a", len(DIKTAT))]),
        dateien={"memo.m4a": DIKTAT},
    )
    leeren(config, api)

    sitzung_id = notes.create_session(runde(config), played_on="2026-08-06")
    leeren(config, api)

    assert [zeile.session_id for zeile in recordings.pending(runde(config))] == [sitzung_id]
    assert len(api.antworten) == 2
    assert api.antworten[1] == ("100", service.QUITTUNG)
    assert service.cursor(runde(config)) == "100"


# --- Zweimal laufen darf nichts verdoppeln -------------------------------------------


def test_ein_zweiter_lauf_holt_nichts_doppelt(config, sitzung_id):
    api = FakeDiscord(
        nachricht("100", text="Die Wirtin hat gelogen."),
        nachricht("101", anhaenge=[("memo.m4a", len(DIKTAT))]),
        dateien={"memo.m4a": DIKTAT},
    )

    leeren(config, api)
    zweiter = leeren(config, api)

    assert zweiter == ()
    assert notes.session(runde(config), sitzung_id).note_count == 1
    assert len(recordings.for_session(runde(config), sitzung_id)) == 1
    assert service.cursor(runde(config)) == "101"


def test_ohne_zeiger_bleibt_die_kennung_die_garantie(config, sitzung_id):
    api = FakeDiscord(nachricht("100", text="Die Wirtin hat gelogen."))
    leeren(config, api)

    connection = db.connect(config.database_path)
    with connection:
        connection.execute("DELETE FROM meta WHERE key = ?", (service.CURSOR,))
    connection.close()

    assert leeren(config, api) == ()
    assert notes.session(runde(config), sitzung_id).note_count == 1


def test_neue_nachrichten_werden_ab_dem_zeiger_geholt(config, sitzung_id):
    api = FakeDiscord(nachricht("100", text="Erste."))
    leeren(config, api)

    api.nachrichten.append(nachricht("102", text="Zweite."))
    leeren(config, api)

    szene = notes.session(runde(config), sitzung_id).scenes[-1]
    assert [eintrag.text for eintrag in szene.notes] == ["Erste.", "Zweite."]
    assert service.cursor(runde(config)) == "102"


def test_der_zeiger_rueckt_nur_ueber_erledigtes(config):
    api = FakeDiscord(
        nachricht("100", text="Wartet."),
        nachricht("101", text="Wartet auch."),
    )

    leeren(config, api)

    assert service.cursor(runde(config)) is None
    assert len(api.antworten) == 2


# --- Der Token taucht nirgends auf ---------------------------------------------------


def test_der_klient_zeigt_den_token_nicht_einmal_im_repr(config):
    assert TOKEN not in repr(klient(config, FakeDiscord()))


def test_keine_logzeile_traegt_den_token(config, sitzung_id, caplog):
    api = FakeDiscord(
        nachricht("100", anhaenge=[("memo.m4a", len(DIKTAT))]),
        dateien={"memo.m4a": DIKTAT},
    )

    with caplog.at_level("DEBUG"):
        leeren(config, api)

    assert TOKEN not in caplog.text
    assert caplog.text


def test_ein_kaputtes_discord_ist_eine_verstaendliche_meldung_ohne_token(config):
    class Weg:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError(f"Bot {TOKEN} abgelehnt")

    with pytest.raises(DiscordUnreachable) as fehler:
        klient(config, Weg()).guild_channel_id(GILDE, service.KANAL)

    assert "ConnectionError" in str(fehler.value)
    assert TOKEN not in str(fehler.value)


def test_eine_antwort_ohne_json_ist_keine_kanalliste(config):
    class Stumm:
        def request(self, *args, **kwargs):
            return Antwort()

    with pytest.raises(DiscordUnreachable):
        klient(config, Stumm()).guild_channel_id(GILDE, service.KANAL)


def test_eine_unerwartete_antwort_ist_keine_nachrichtenliste(config):
    class Wirr:
        def request(self, *args, **kwargs):
            return Antwort({"nachrichten": "keine Liste"})

    with pytest.raises(DiscordUnreachable):
        klient(config, Wirr()).messages(DIKTAT_KANAL)


# --- Der Stapelaufruf ----------------------------------------------------------------


def test_der_stapelaufruf_leert_den_kanal(config, sitzung_id, monkeypatch, capsys):
    api = FakeDiscord(nachricht("100", text="Die Wirtin hat gelogen."))
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "DiscordClient", lambda zugang: klient(zugang, api))

    assert entry.main() == 0

    assert "Notiz" in capsys.readouterr().out
    assert notes.session(runde(config), sitzung_id).note_count == 1


def test_ein_leerer_briefkasten_ist_kein_fehlschlag(config, sitzung_id, monkeypatch, capsys):
    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "DiscordClient", lambda zugang: klient(zugang, FakeDiscord()))

    assert entry.main() == 0
    assert service.LEER in capsys.readouterr().out


def test_der_stapelaufruf_meldet_ein_unerreichbares_discord(config, monkeypatch, capsys):
    class Weg:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError(f"Bot {TOKEN} abgelehnt")

    monkeypatch.setattr(entry.Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(service, "DiscordClient", lambda zugang: klient(zugang, Weg()))

    assert entry.main() == 2

    ausgabe = capsys.readouterr().out
    assert "fehlgeschlagen" in ausgabe
    assert TOKEN not in ausgabe


def test_der_download_geht_ohne_authorization_header(config, tmp_path):
    api = FakeDiscord(dateien={"memo.m4a": DIKTAT})
    ziel = tmp_path / "memo.m4a"

    klient(config, api).download(Attachment("memo.m4a", f"{CDN}/memo.m4a", len(DIKTAT)), ziel)

    assert ziel.read_bytes() == DIKTAT
    assert api.abrufe == [(f"{CDN}/memo.m4a", None)]
