"""Der nachgetragene Sprechername — gezielt geholt, nie erfunden (#250).

Geprüft wird die Sparsamkeit ebenso wie die Auskunft: dass **je Sprecher einmal** gefragt
wird statt die Mitgliederliste einer Gilde zu spiegeln, dass ein bereits bekannter Name
niemanden kostet, und dass ein Fehlschlag beim Wort »unbekannt« endet und nicht beim
Dateinamen.
"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest
from conftest import runde

from chronicle import consent, db, notes, recordings
from chronicle.bot import namen
from chronicle.config import Config

MITSCHNITT = "2026-08-18T20:10:32+00:00"


@pytest.fixture
def config(tmp_path):
    hier = Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")
    db.init(hier.database_path)
    return hier


@pytest.fixture
def sitzung_id(config):
    return notes.create_session(runde(config), played_on="2026-08-18", title="Der Keller")


def spur(config, sitzung_id, dateiname, kennung, name=None):
    return recordings.enqueue(
        runde(config),
        sitzung_id,
        dateiname,
        discord_user_id=kennung,
        discord_name=name,
        started_at=MITSCHNITT,
    )


class FakeClient:
    """Ein Discord-Client, der nur das kann, was hier gebraucht wird.

    ``zwischenspeicher`` ist, was ``get_user`` ohne Netz hergibt; ``entfernt`` ist, was
    erst ein ``fetch_user`` findet. Was in keinem von beiden steht, gibt es nicht mehr.
    """

    def __init__(self, zwischenspeicher=None, entfernt=None):
        self._nah = zwischenspeicher or {}
        self._fern = entfernt or {}
        self.geholt: list[int] = []

    def get_user(self, nummer):
        name = self._nah.get(nummer)
        return None if name is None else types.SimpleNamespace(id=nummer, display_name=name)

    async def fetch_user(self, nummer):
        self.geholt.append(nummer)
        name = self._fern.get(nummer)
        if name is None:
            raise LookupError("Unknown User")
        return types.SimpleNamespace(id=nummer, display_name=name)


# --- Das Nachschlagen selbst ---------------------------------------------------------


def test_der_zwischenspeicher_kommt_vor_der_anfrage():
    """Wer dem Prozess schon begegnet ist, kostet keinen Aufruf."""
    client = FakeClient(zwischenspeicher={4001: "Mira"})

    assert asyncio.run(namen.aufloesen(client, ("4001",))) == {"4001": "Mira"}
    assert client.geholt == []


def test_wer_nicht_im_zwischenspeicher_steht_wird_gezielt_geholt():
    client = FakeClient(entfernt={1481747560996667526: "Samuel"})

    gefunden = asyncio.run(namen.aufloesen(client, ("1481747560996667526",)))

    assert gefunden == {"1481747560996667526": "Samuel"}
    assert client.geholt == [1481747560996667526]


def test_ein_gelöschtes_konto_fehlt_im_ergebnis_statt_geraten_zu_werden(caplog):
    client = FakeClient()

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(namen.aufloesen(client, ("4009",))) == {}

    # Der Grund gehört ins Log, die Kennung nicht — sie benennt eine Person.
    assert "LookupError" in caplog.text
    assert "4009" not in caplog.text


# --- Das Nachtragen an den Spuren ----------------------------------------------------


def test_die_alten_spuren_bekommen_ihre_namen_aus_der_gespeicherten_kennung(config, sitzung_id):
    """Der Altbestand vom 18.08.: die Kennung stand die ganze Zeit da, nur der Name fehlte."""
    spur(config, sitzung_id, "sitzung4-20260818T201032-Daniel.wav", "650266892736397338", "Daniel")
    spur(config, sitzung_id, "sitzung4-20260818T201032-unbekannt.wav", "1481747560996667526")
    spur(config, sitzung_id, "sitzung4-20260818T201032-unbekannt-2.wav", "769625925851021343")
    client = FakeClient(entfernt={1481747560996667526: "Samuel", 769625925851021343: "Aelin"})

    zeilen = asyncio.run(
        namen.nachtragen(lambda ids: namen.aufloesen(client, ids), runde(config), sitzung_id)
    )

    assert zeilen == 2
    benannt = {
        eintrag.discord_user_id: eintrag.discord_name
        for eintrag in recordings.for_session(runde(config), sitzung_id)
    }
    assert benannt == {
        "650266892736397338": "Daniel",
        "1481747560996667526": "Samuel",
        "769625925851021343": "Aelin",
    }
    # Nur die beiden Namenlosen kosten eine Anfrage — der dritte stand schon da.
    assert sorted(client.geholt) == [769625925851021343, 1481747560996667526]


def test_alle_haeppchen_eines_sprechers_werden_von_einer_anfrage_benannt(config, sitzung_id):
    """Eine Spur zerfällt in Häppchen (#217) — gefragt wird trotzdem nur einmal."""
    for nummer in range(1, 4):
        spur(config, sitzung_id, f"sitzung4-teil{nummer}.wav", "4009")
    client = FakeClient(entfernt={4009: "Samuel"})

    zeilen = asyncio.run(
        namen.nachtragen(lambda ids: namen.aufloesen(client, ids), runde(config), sitzung_id)
    )

    assert zeilen == 3
    assert client.geholt == [4009]


def test_ohne_ergebnis_bleibt_die_zeile_leer_und_nichts_wird_hingeschrieben(config, sitzung_id):
    spur(config, sitzung_id, "sitzung4-unbekannt.wav", "4009")
    client = FakeClient()

    zeilen = asyncio.run(
        namen.nachtragen(lambda ids: namen.aufloesen(client, ids), runde(config), sitzung_id)
    )

    assert zeilen == 0
    assert [e.discord_name for e in recordings.for_session(runde(config), sitzung_id)] == [None]


def test_ein_zweiter_lauf_ueberschreibt_keinen_namen(config, sitzung_id):
    """Idempotent, und in eine Richtung: die Spur gehört dem Abend, an dem sie entstand."""
    spur(config, sitzung_id, "sitzung4-unbekannt.wav", "4009")
    erst = FakeClient(entfernt={4009: "Samuel"})
    asyncio.run(namen.nachtragen(lambda ids: namen.aufloesen(erst, ids), runde(config), sitzung_id))

    danach = FakeClient(zwischenspeicher={4009: "Samuel der Zweite"})
    zeilen = asyncio.run(
        namen.nachtragen(lambda ids: namen.aufloesen(danach, ids), runde(config), sitzung_id)
    )

    assert zeilen == 0
    assert [e.discord_name for e in recordings.for_session(runde(config), sitzung_id)] == ["Samuel"]


def test_ohne_sitzung_gilt_das_nachtragen_der_ganzen_runde(config, sitzung_id):
    """Der Weg, den es weiter gibt: was ein abgestürzter Lauf liegen ließ, wird nachgeholt."""
    zweite = notes.create_session(runde(config), played_on="2026-08-25", title="Der Turm")
    spur(config, sitzung_id, "sitzung4-unbekannt.wav", "4009")
    spur(config, zweite, "sitzung5-unbekannt.wav", "4010")
    client = FakeClient(entfernt={4009: "Samuel", 4010: "Aelin"})

    zeilen = asyncio.run(namen.nachtragen(lambda ids: namen.aufloesen(client, ids), runde(config)))

    assert zeilen == 2


def test_ein_diktat_ohne_kennung_wird_nicht_nachgeschlagen(config, sitzung_id):
    """Ohne Discord-Kennung gibt es nichts zu holen — eine Präsenznotiz hat keine."""
    spur(config, sitzung_id, "memo.m4a", None)

    assert recordings.namenlose_sprecher(runde(config), sitzung_id) == ()


# --- Die Grenze zum Einwilligungsprotokoll -------------------------------------------


def test_der_nachgetragene_name_landet_nicht_im_einwilligungsprotokoll(config, sitzung_id):
    """Eine Tonspur belegt nicht, dass jemand die Ansage gehört hat.

    Der Nachweis nach §201 sagt, wer bei der Ansage im Kanal stand. Ihn aus einer
    Sprecherspur zu ergänzen hieße, eine Zustimmung zu behaupten, die niemand gegeben hat
    — der Name gehört deshalb an die Spur und nirgendwo sonst hin.
    """
    spur(config, sitzung_id, "sitzung4-unbekannt.wav", "4009")
    client = FakeClient(entfernt={4009: "Samuel"})

    asyncio.run(
        namen.nachtragen(lambda ids: namen.aufloesen(client, ids), runde(config), sitzung_id)
    )

    assert consent.for_session(runde(config), sitzung_id) == ()


# --- Der Aufruf von Hand -------------------------------------------------------------


def test_der_aufruf_ohne_runde_sagt_wie_er_geht(capsys):
    assert namen.main([]) == 2
    assert namen.AUFRUF in capsys.readouterr().out


def test_eine_runde_die_es_nicht_gibt_wird_benannt(config, capsys, monkeypatch):
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))

    assert namen.main(["99"]) == 2
    assert "99" in capsys.readouterr().out


def test_ohne_bot_token_wird_niemand_nachgeschlagen(config, sitzung_id, capsys, monkeypatch):
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    assert namen.main([str(runde(config).id)]) == 2
    assert namen.OHNE_TOKEN in capsys.readouterr().out
