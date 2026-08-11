"""Einladen und Verabschieden — gegen ein nachgebautes Discord, ohne Netz und ohne py-cord.

Die Sätze, die dieser Suite ihren Sinn geben: **der erste Satz in einer fremden Gilde sagt,
wem die Kiste gehört**, **ein Rauswurf wirkt sofort und nicht erst nach der Frist** — und
**gelöscht heißt vollständig**, bis hin zur Tondatei auf der Platte und der Zeile im
Suchindex. Was innerhalb der Frist zurückkommt, kommt ganz zurück; danach ist es fort, und
genau das steht vorher da.

»Sofort« heißt dabei in jedem Faden und nicht nur in Discord, und »wer darf das« beantwortet
Discord — beides wird hier geprüft, denn beides ist das, was ohne Prüfung still zurückfällt.
Und niemand sonst darf es: dass es keinen Betreiber-Weg an einer fremden Runde gibt, ist
kein Versehen, sondern eine Entscheidung (#90), und deshalb steht auch dafür ein Gate hier.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
import threading
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_bot import FakeBot, FakeIntents, FakePCMAudio, FakePermissions, FakeRechte, FakeSenke
from test_chronik import FakeHTTPException, FakeInputText, FakeModal
from test_erinnern import FakeButton, FakeEmbed, FakeSelect, FakeSelectOption, FakeView

from chronicle import (
    consent,
    db,
    lebenszyklus,
    notes,
    people,
    recordings,
    register,
    settings,
    zugang,
)
from chronicle import runde as runden
from chronicle.bot import chronik, einrichten, gateway
from chronicle.compose import service as compose_service
from chronicle.config import Config
from chronicle.foundry import service as foundry_service
from chronicle.transcribe import service as transcribe_service

GILDE = "1101"
GILDENAME = "Der Krumme Ast"
NACHBARGILDE = "2202"

MIRA = consent.Member(id="d-mira", name="Mira")


# -- Die Attrappen ----------------------------------------------------------------------


class FakeKanal:
    def __init__(self, kennung, name, *, darf=True, bricht=False):
        self.id = kennung
        self.name = name
        self._darf = darf
        self._bricht = bricht
        self.gesendet: list[str] = []

    def permissions_for(self, _wer):
        return types.SimpleNamespace(send_messages=self._darf)

    async def send(self, text):
        # Das Recht sagt Discord vorher zu; halten muss es der Aufruf, und der kann
        # trotzdem scheitern — genau dazwischen liegt der Fall, um den es hier geht.
        if self._bricht:
            raise RuntimeError("Discord nimmt die Nachricht nicht an")
        self.gesendet.append(text)


class FakeGilde:
    def __init__(self, kennung=GILDE, name=GILDENAME, *, kanaele=(), system=None):
        self.id = kennung
        self.name = name
        self.text_channels = tuple(kanaele)
        self.system_channel = system
        self.me = object()


class FakeAntwort:
    def __init__(self):
        self.gesendet: list[dict] = []
        self.bearbeitet: list[dict] = []

    async def send_message(self, text=None, *, view=None, **rest):
        self.gesendet.append({"text": text, "view": view})

    async def edit_message(self, *, content=None, view=None, **rest):
        self.bearbeitet.append({"content": content, "view": view})


class FakeMitglied:
    """Ein Mitglied mit genau den Rechten, die Discord für diesen Server meldet."""

    def __init__(self, kennung="4001", name="Mira", *, verwaltet=False, admin=False):
        self.id = kennung
        self.display_name = name
        self.guild_permissions = FakeRechte(manage_guild=verwaltet, administrator=admin)


LEITUNG = FakeMitglied("4000", "Spielleitung", verwaltet=True, admin=True)
MITGLIED = FakeMitglied()


class FakeInteraction:
    def __init__(self, *, guild_id=GILDE, wer=LEITUNG, kanal=None):
        self.response = FakeAntwort()
        self.guild_id = guild_id
        self.user = wer
        self.channel = kanal


class FakeCtx:
    def __init__(self, *, guild_id=GILDE, gilde=None, autor=LEITUNG):
        self.guild_id = guild_id
        self.guild = gilde
        self.author = autor
        self.channel_id = 900
        self.antworten: list[str | None] = []
        self.ansichten: list = []
        self.modale: list = []

    async def defer(self, **rest):
        pass

    async def respond(self, text=None, *, view=None, **rest):
        self.antworten.append(text)
        self.ansichten.append(view)

    async def send_modal(self, modal):
        self.modale.append(modal)


# -- Die Bühne --------------------------------------------------------------------------


@pytest.fixture
def pycord(monkeypatch):
    modul = types.ModuleType("discord")
    modul.Intents = FakeIntents
    modul.Bot = FakeBot
    modul.Permissions = FakePermissions
    modul.PCMAudio = FakePCMAudio
    modul.HTTPException = FakeHTTPException
    modul.Embed = FakeEmbed
    modul.SelectOption = FakeSelectOption
    senken = types.ModuleType("discord.sinks")
    senken.Sink = FakeSenke
    modul.sinks = senken
    werkzeug = types.ModuleType("discord.utils")
    werkzeug.get_missing_voice_dependencies = lambda: ()
    modul.utils = werkzeug
    oberflaeche = types.ModuleType("discord.ui")
    oberflaeche.Modal = FakeModal
    oberflaeche.InputText = FakeInputText
    oberflaeche.View = FakeView
    oberflaeche.Button = FakeButton
    oberflaeche.Select = FakeSelect
    modul.ui = oberflaeche
    monkeypatch.setitem(sys.modules, "discord", modul)
    monkeypatch.setattr(FakeBot, "erzeugt", [])
    return modul


@pytest.fixture
def konfiguration(tmp_path):
    config = Config(data_dir=tmp_path / "daten", recordings_dir=tmp_path / "aufnahmen")
    db.init(config.database_path)
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def bot(konfiguration, pycord):
    return gateway.baue(konfiguration)


def fuellen(config, runde, marke):
    """Eine Runde mit allem, was beim Löschen verschwinden muss — Datei inbegriffen."""
    sitzung = notes.create_session(runde, played_on="2026-05-01", title=f"Sitzung {marke}")
    szene = notes.session(runde, sitzung).scenes[0]
    notes.add_note(runde, szene.id, f"Im Keller stand {marke}.")
    spur = config.recordings_dir / f"{marke}-spur.wav"
    spur.write_bytes(b"ton")
    recordings.enqueue(runde, sitzung, spur.name, discord_user_id=MIRA.id)
    consent.record(
        runde,
        session_id=sitzung,
        kind=consent.ANSAGE,
        guild_id=f"gilde-{marke}",
        channel_id="kanal",
        channel_name=f"Runde {marke}",
        text=f"Ansage {marke}",
        members=(MIRA,),
    )
    people.confirm(runde, {MIRA.id: "u-1"})
    scope = db.scoped(runde)
    try:
        with scope:
            scope.execute(
                "INSERT INTO register_entry (runde_id, kind, name, description, state, "
                "suggested_at) VALUES (?, 'figur', ?, 'Eine Kundschafterin', 'bestaetigt', "
                "'2026-05-01T21:00:00')",
                (scope.runde_id, f"Mira {marke}"),
            )
    finally:
        scope.close()
    settings.save(runde, {"foundry_url": f"https://{marke}.example"})
    return {"sitzung": sitzung, "spur": spur}


def zeilen(config, tabelle, runde_id):
    connection = db.connect(config.database_path)
    try:
        zeile = connection.execute(
            f"SELECT COUNT(*) AS anzahl FROM {tabelle} WHERE runde_id = ?", (runde_id,)
        ).fetchone()
    finally:
        connection.close()
    return int(zeile["anzahl"])


def alle_zeilen(config, runde_id):
    return {
        tabelle: zeilen(config, tabelle, runde_id)
        for tabelle in sorted(db.GESCOPTE_TABELLEN)
        if tabelle != "search_index"
    }


def eintritt(bot, gilde):
    asyncio.run(bot.ereignisse["on_guild_join"](gilde))


def austritt(bot, gilde):
    asyncio.run(bot.ereignisse["on_guild_remove"](gilde))


def einrichtungsfenster(bot, ctx):
    asyncio.run(bot.befehle[gateway.BEFEHL_SETUP](ctx))
    return ctx.modale[-1]


def ausfuellen(fenster, adresse="", benutzer="", uhrzeit="", *, kanal=None):
    for feld, wert in zip(fenster.children, (adresse, benutzer, uhrzeit), strict=True):
        feld.value = wert
    interaktion = FakeInteraction(kanal=kanal)
    asyncio.run(fenster.callback(interaktion))
    return interaktion


def loeschbefehl(bot, ctx):
    asyncio.run(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle["loeschen"](ctx))
    return ctx


def frist_setzen(config, runde, wann: str) -> None:
    """Die zugesagte Frist von Hand — schneller als dreißig Tage zu warten."""
    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE runde SET locked_at = ?, delete_after = ? WHERE id = ?",
                ("2026-05-01T20:00:00+00:00", wann, runde.id),
            )
    finally:
        connection.close()


# -- Einladen ---------------------------------------------------------------------------


def test_die_einladung_sagt_wem_die_kiste_gehoert(bot):
    """Der harte Teil des Epics: das steht in der ersten Nachricht, nicht im Kleingedruckten."""
    kanal = FakeKanal("300", "allgemein")
    eintritt(bot, FakeGilde(system=kanal))

    (gesagt,) = kanal.gesendet
    assert einrichten.OFFENLEGUNG in gesagt
    assert "jemand anderem gehört" in gesagt
    assert "kommt an alles heran" in gesagt
    assert "/setup" in gesagt


def test_die_einladung_geht_in_den_ersten_kanal_in_dem_der_bot_reden_darf(bot):
    stumm = FakeKanal("300", "regeln", darf=False)
    offen = FakeKanal("301", "tisch")
    eintritt(bot, FakeGilde(system=stumm, kanaele=(stumm, offen)))

    assert stumm.gesendet == []
    assert offen.gesendet == [einrichten.WILLKOMMEN]


def test_ohne_einen_kanal_zum_reden_bleibt_es_bei_einer_zeile_im_log(bot, caplog):
    eintritt(bot, FakeGilde(kanaele=(FakeKanal("300", "regeln", darf=False),)))
    assert "Kein Kanal" in caplog.text


def test_die_einladung_allein_legt_noch_keine_runde_an(konfiguration, bot):
    eintritt(bot, FakeGilde(system=FakeKanal("300", "allgemein")))
    assert runden.fuer_gilde(konfiguration.database_path, GILDE) is None


# -- Einrichten -------------------------------------------------------------------------


def test_setup_legt_die_runde_fuer_diesen_server_an(konfiguration, bot):
    ctx = FakeCtx(gilde=FakeGilde())

    fenster = einrichtungsfenster(bot, ctx)
    ausfuellen(fenster, adresse="https://foundry.example", benutzer="Chronist")

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert unsere is not None
    assert unsere.name == GILDENAME
    wirksam = settings.effective(konfiguration, unsere)
    assert wirksam.foundry_url == "https://foundry.example"
    assert wirksam.foundry_user == "Chronist"


def test_setup_beansprucht_eine_vorhandene_runde_statt_einer_zweiten(konfiguration, bot):
    vorher = runden.anlegen(konfiguration.database_path, "Alte Runde", guild_id=GILDE)

    ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronist")

    beansprucht = [
        eine.id for eine in runden.alle(konfiguration.database_path) if eine.guild_id == GILDE
    ]
    assert beansprucht == [vorher.id]
    assert runden.get(konfiguration.database_path, vorher.id).name == "Alte Runde"


def test_das_einrichtungsfenster_fragt_nicht_nach_dem_passwort(bot):
    """Es gibt kein Feld dafür — das Passwort kommt am Sitzungsende und wird verbraucht."""
    fenster = einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde()))

    beschriftet = " ".join(f"{feld.label} {feld.placeholder}" for feld in fenster.children)
    assert "asswort" not in beschriftet
    # Adresse, Benutzer, Uhrzeit — das Modell gehört der Instanz und steht nicht hier.
    assert len(fenster.children) == 3
    assert "odell" not in beschriftet


def test_ohne_server_gibt_es_keine_runde_zu_beanspruchen(konfiguration, bot):
    ctx = FakeCtx(guild_id=None)

    asyncio.run(bot.befehle[gateway.BEFEHL_SETUP](ctx))

    assert ctx.antworten == [einrichten.NUR_IM_SERVER]
    assert ctx.modale == []
    assert runden.alle(konfiguration.database_path)[0].guild_id is None


def test_ein_leeres_feld_laesst_den_wert_stehen(konfiguration, bot):
    ctx = FakeCtx(gilde=FakeGilde())
    ausfuellen(
        einrichtungsfenster(bot, ctx), adresse="https://foundry.example", benutzer="Chronist"
    )

    ausfuellen(einrichtungsfenster(bot, ctx), benutzer="Chronistin")

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    wirksam = settings.effective(konfiguration, unsere)
    assert wirksam.foundry_url == "https://foundry.example"
    assert wirksam.foundry_user == "Chronistin"


def test_eine_unlesbare_uhrzeit_laesst_die_bisherige_stehen(konfiguration, bot):
    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), uhrzeit="viertel nach drei"
    )

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.nightly_time(unsere) == settings.DEFAULT_NIGHTLY_TIME
    assert "viertel nach drei" in interaktion.response.gesendet[0]["text"]


def test_der_zustellkanal_wird_gewaehlt_und_wirkt_sofort(konfiguration, bot):
    kanal = FakeKanal("777", "chroniken")
    ctx = FakeCtx(gilde=FakeGilde(kanaele=(kanal,)))
    interaktion = ausfuellen(einrichtungsfenster(bot, ctx), benutzer="Chronist")

    ansicht = interaktion.response.gesendet[0]["view"]
    (menue,) = ansicht.items
    assert [option.value for option in menue.options] == [einrichten.OHNE_KANAL, "777"]

    menue.values = ["777"]
    klick = FakeInteraction()
    asyncio.run(menue.callback(klick))

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.effective(konfiguration, unsere).discord_recap_channel == "777"
    assert "777" in klick.response.bearbeitet[0]["content"]


def test_kein_kanal_ist_eine_gueltige_wahl(konfiguration, bot):
    ctx = FakeCtx(gilde=FakeGilde(kanaele=(FakeKanal("777", "chroniken"),)))
    interaktion = ausfuellen(einrichtungsfenster(bot, ctx), benutzer="Chronist")
    (menue,) = interaktion.response.gesendet[0]["view"].items

    menue.values = [einrichten.OHNE_KANAL]
    asyncio.run(menue.callback(FakeInteraction()))

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.effective(konfiguration, unsere).discord_recap_channel is None


# -- Verabschieden ----------------------------------------------------------------------


def test_ein_lauf_von_vorhin_haelt_die_frische_runde_fuer_ruhend(konfiguration):
    """``ruht`` ist die Schranke jedes langen Laufs — Abgleich, Verschriften, Komponieren.

    Sie liest neu, aber die Kennung allein trägt das nicht: nach einer Löschung gehört sie
    einer fremden Gilde. Ein Lauf, dessen Runde fort ist, schweigt.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    assert not lebenszyklus.ruht(unsere)

    lebenszyklus.loeschen(konfiguration, unsere)
    frisch = runden.anlegen(konfiguration.database_path, "Fremde", guild_id=NACHBARGILDE)

    assert frisch.id == unsere.id
    assert lebenszyklus.ruht(unsere)
    assert not lebenszyklus.ruht(frisch)


def test_die_freigabe_entsperrt_die_frische_runde_nicht(konfiguration):
    """Zwischen Offenlegung und Freigabe liegt ein ``await``. Was danach dort steht, hat den
    Satz nie gelesen — und wird deshalb auch nicht entsperrt."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    frist_setzen(konfiguration, unsere, "2026-06-30T20:00:00+00:00")
    gesperrt = runden.get(konfiguration.database_path, unsere.id)

    lebenszyklus.loeschen(konfiguration, unsere)
    frisch = runden.anlegen(konfiguration.database_path, "Fremde", guild_id=NACHBARGILDE)
    lebenszyklus.sperren(konfiguration.database_path, NACHBARGILDE)

    assert einrichten.wieder_im_dienst(konfiguration, gesperrt) is None
    assert runden.get(konfiguration.database_path, frisch.id).gesperrt


def test_der_rauswurf_sperrt_die_runde_sofort(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    fuellen(konfiguration, unsere, "alpha")

    austritt(bot, FakeGilde())

    gesperrt = runden.get(konfiguration.database_path, unsere.id)
    assert gesperrt.gesperrt
    frist = datetime.fromisoformat(gesperrt.delete_after) - datetime.fromisoformat(
        gesperrt.locked_at
    )
    assert frist == timedelta(days=lebenszyklus.FRIST_TAGE)
    # Sofort still: der Weg über die Gilde führt nicht mehr zu ihr.
    assert chronik.runde_der_gilde(konfiguration, GILDE) is None


def test_die_gesperrte_runde_sagt_bis_wann_alles_noch_da_ist(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    austritt(bot, FakeGilde())

    with pytest.raises(chronik.ChronikFehler) as gefangen:
        chronik.runde_verlangen(konfiguration, GILDE)

    gesperrt = runden.get(konfiguration.database_path, unsere.id)
    assert lebenszyklus.frist_datum(gesperrt) in str(gefangen.value)
    assert "lade mich wieder ein" in str(gefangen.value)


def test_ein_zweiter_rauswurf_verschiebt_die_frist_nicht(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    austritt(bot, FakeGilde())
    zuerst = runden.get(konfiguration.database_path, unsere.id).delete_after

    austritt(bot, FakeGilde())

    assert runden.get(konfiguration.database_path, unsere.id).delete_after == zuerst


def test_innerhalb_der_frist_bringt_die_wiedereinladung_alles_zurueck(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    austritt(bot, FakeGilde())

    kanal = FakeKanal("300", "allgemein")
    eintritt(bot, FakeGilde(system=kanal))

    zurueck = runden.get(konfiguration.database_path, unsere.id)
    assert not zurueck.gesperrt
    assert notes.session(zurueck, ids["sitzung"]).title == "Sitzung alpha"
    (gesagt,) = kanal.gesendet
    assert GILDENAME in gesagt
    # Auch der Rückkehr wird gesagt, wo sie steht.
    assert einrichten.OFFENLEGUNG in gesagt


def test_nach_der_frist_ist_die_runde_fort(konfiguration):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    lebenszyklus.sperren(konfiguration.database_path, GILDE)

    spaeter = datetime.now(UTC) + timedelta(days=lebenszyklus.FRIST_TAGE + 1)
    (meldung,) = lebenszyklus.sweep(konfiguration, jetzt=spaeter)

    assert GILDENAME in meldung
    assert runden.fuer_gilde(konfiguration.database_path, GILDE) is None
    assert not ids["spur"].exists()
    assert alle_zeilen(konfiguration, unsere.id) == dict.fromkeys(
        db.GESCOPTE_TABELLEN - {"search_index"}, 0
    )


def test_vor_der_frist_raeumt_der_lauf_nichts_ab(konfiguration):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    fuellen(konfiguration, unsere, "alpha")
    lebenszyklus.sperren(konfiguration.database_path, GILDE)

    frueher = datetime.now(UTC) + timedelta(days=lebenszyklus.FRIST_TAGE - 1)

    assert lebenszyklus.sweep(konfiguration, jetzt=frueher) == ()
    assert runden.fuer_gilde(konfiguration.database_path, GILDE) is not None


def test_eine_runde_ohne_rauswurf_wird_nie_faellig(konfiguration):
    runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    spaeter = datetime.now(UTC) + timedelta(days=365)
    assert lebenszyklus.faellig(konfiguration.database_path, jetzt=spaeter) == ()


def test_die_frist_wird_beim_start_und_danach_taeglich_geprueft(konfiguration):
    class Schluss(Exception):
        pass

    laeufe = []

    async def schlafen(sekunden):
        laeufe.append(sekunden)
        if len(laeufe) == 2:
            raise Schluss

    with pytest.raises(Schluss):
        asyncio.run(lebenszyklus.taeglich(konfiguration, schlafen=schlafen))

    assert laeufe == [lebenszyklus.SWEEP_ABSTAND, lebenszyklus.SWEEP_ABSTAND]


def test_der_bot_haelt_beide_fristen_nebeneinander(konfiguration, bot, monkeypatch):
    """Zwei Zusagen, zwei Läufe — die Aufnahmen und die verabschiedete Runde."""
    gestartet = []

    async def nie(config, **rest):
        gestartet.append(config)
        await asyncio.sleep(0)

    monkeypatch.setattr(recordings, "taeglich", nie)
    monkeypatch.setattr(lebenszyklus, "taeglich", nie)

    async def einmal():
        await bot.ereignisse["on_ready"]()
        await asyncio.sleep(0)

    asyncio.run(einmal())

    assert len(gestartet) == 2


# -- Sofort löschen ---------------------------------------------------------------------


def test_die_loeschfrage_sagt_was_verschwindet(konfiguration, bot):
    runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ctx = FakeCtx(gilde=FakeGilde())

    asyncio.run(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle["loeschen"](ctx))

    (frage,) = ctx.antworten
    for satzteil in ("Notizen", "Tondateien", "Chroniken", "Register", "Nachweise"):
        assert satzteil in frage
    assert "keine Sicherung" in frage
    assert f"{lebenszyklus.FRIST_TAGE} Tagen" in frage


def test_der_knopf_loescht_dateien_zeilen_und_den_suchindex(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    nachbar = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    daneben = fuellen(konfiguration, nachbar, "beta")

    ctx = FakeCtx(gilde=FakeGilde())
    asyncio.run(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle["loeschen"](ctx))
    ja, _nein = ctx.ansichten[0].items
    klick = FakeInteraction()
    asyncio.run(ja.callback(klick))

    assert klick.response.bearbeitet[0]["content"] == einrichten.LOESCHEN_FERTIG
    assert runden.get(konfiguration.database_path, unsere.id) is None
    assert not ids["spur"].exists()
    assert zeilen(konfiguration, "search_index", unsere.id) == 0

    # Und der Nachbar merkt nichts davon.
    assert daneben["spur"].exists()
    assert notes.session(nachbar, daneben["sitzung"]).title == "Sitzung beta"
    assert zeilen(konfiguration, "search_index", nachbar.id) > 0


def test_die_einwilligungsprotokolle_gehen_mit(konfiguration, bot):
    """Die eine Entscheidung dieses Umbaus: der Nachweis überlebt seinen Gegenstand nicht.

    Anonymisiert belegte er nicht mehr, *wer* dabei war — und damit nichts. Übrig bliebe
    eine Liste von Namen über Menschen, die mit dieser Instanz nichts mehr zu tun haben.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    fuellen(konfiguration, unsere, "alpha")
    assert zeilen(konfiguration, "consent_member", unsere.id) == 1

    lebenszyklus.loeschen(konfiguration, unsere)

    assert zeilen(konfiguration, "consent_event", unsere.id) == 0
    assert zeilen(konfiguration, "consent_member", unsere.id) == 0


def test_abbrechen_loescht_nichts(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = FakeCtx(gilde=FakeGilde())
    asyncio.run(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle["loeschen"](ctx))
    _ja, nein = ctx.ansichten[0].items
    klick = FakeInteraction()
    asyncio.run(nein.callback(klick))

    assert klick.response.bearbeitet[0]["content"] == einrichten.LOESCHEN_ABGEBROCHEN
    assert runden.get(konfiguration.database_path, unsere.id) is not None
    assert ids["spur"].exists()


def test_ohne_runde_gibt_es_nichts_zu_loeschen(konfiguration, bot):
    ctx = FakeCtx(gilde=FakeGilde())

    asyncio.run(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle["loeschen"](ctx))

    (antwort,) = ctx.antworten
    assert chronik.KEINE_RUNDE in antwort
    assert ctx.ansichten == [None]


def test_das_register_der_geloeschten_runde_ist_nicht_mehr_zu_finden(konfiguration):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    fuellen(konfiguration, unsere, "alpha")
    assert register.overview(unsere)

    lebenszyklus.loeschen(konfiguration, unsere)

    assert runden.fuer_gilde(konfiguration.database_path, GILDE) is None


# -- Wer darf das -----------------------------------------------------------------------


def test_einrichten_ist_kein_befehl_fuer_jedes_mitglied(konfiguration, bot):
    """Die Kette, die hier abgeschnitten wird: wer die Adresse setzt, bestimmt, welchem
    Server das nächste ``/chronik fertig`` das Foundry-Passwort vorzeigt."""
    ctx = FakeCtx(gilde=FakeGilde(), autor=MITGLIED)

    asyncio.run(bot.befehle[gateway.BEFEHL_SETUP](ctx))

    assert ctx.antworten == [einrichten.NUR_VERWALTUNG]
    assert ctx.modale == []
    assert runden.fuer_gilde(konfiguration.database_path, GILDE) is None


def test_discord_kennt_die_schranke_vor_dem_einrichten(bot):
    """Nicht nur wir rechnen sie aus — der Befehl trägt sie bei Discord."""
    assert bot.rechte[gateway.BEFEHL_SETUP].rechte == {"manage_guild": True}


def test_loeschen_ist_kein_befehl_fuer_jedes_mitglied(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = loeschbefehl(bot, FakeCtx(gilde=FakeGilde(), autor=MITGLIED))

    assert ctx.antworten == [einrichten.NUR_ADMIN]
    assert ctx.ansichten == [None]
    assert ids["spur"].exists()


def test_der_knopf_prueft_das_recht_noch_einmal(konfiguration, bot):
    """Die Frage stellt die Leitung, klicken könnte jeder, der die Nachricht sieht."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ctx = loeschbefehl(bot, FakeCtx(gilde=FakeGilde()))
    ja, _nein = ctx.ansichten[0].items

    klick = FakeInteraction(wer=MITGLIED)
    asyncio.run(ja.callback(klick))

    assert klick.response.bearbeitet[0]["content"] == einrichten.NUR_ADMIN
    assert runden.get(konfiguration.database_path, unsere.id) is not None


def test_die_loeschung_sagt_im_log_wer_sie_wollte(konfiguration, bot, caplog):
    """Danach steht es nirgends mehr — die Runde, in der es stünde, ist ja fort."""
    caplog.set_level(logging.INFO)
    runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ctx = loeschbefehl(bot, FakeCtx(gilde=FakeGilde()))
    ja, _nein = ctx.ansichten[0].items

    asyncio.run(ja.callback(FakeInteraction()))

    assert LEITUNG.display_name in caplog.text
    assert str(LEITUNG.id) in caplog.text


def test_ein_alter_knopf_loescht_die_frische_runde_nicht(konfiguration, bot):
    """``runde.id`` wird nach einer Löschung neu vergeben — der Knopf lebt länger."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ctx = loeschbefehl(bot, FakeCtx(gilde=FakeGilde()))
    ja, _nein = ctx.ansichten[0].items

    lebenszyklus.loeschen(konfiguration, unsere)
    nachbar = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    assert nachbar.id == unsere.id
    klick = FakeInteraction()
    asyncio.run(ja.callback(klick))

    assert klick.response.bearbeitet[0]["content"] == einrichten.LOESCHEN_VERALTET
    assert runden.get(konfiguration.database_path, nachbar.id) is not None


def test_ein_altes_kanalmenue_stellt_nicht_in_die_fremde_runde_zu(konfiguration, bot):
    """Der schlimmere Zwilling des alten Knopfes: keine einmalige Fehlhandlung, sondern
    eine dauerhafte Fehlzustellung — ab dem Klick gingen die Chroniken der Nachbarrunde in
    einen Kanal dieser Gilde."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde(kanaele=(FakeKanal("777", "hier"),)))),
        benutzer="Chronist",
    )
    (menue,) = interaktion.response.gesendet[0]["view"].items

    lebenszyklus.loeschen(konfiguration, unsere)
    nachbar = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    assert nachbar.id == unsere.id
    menue.values = ["777"]
    klick = FakeInteraction()
    asyncio.run(menue.callback(klick))

    assert klick.response.bearbeitet[0]["content"] == chronik.VERALTET
    assert settings.effective(konfiguration, nachbar).discord_recap_channel is None


def test_zwei_runden_derselben_sekunde_sind_nicht_dieselbe(konfiguration, monkeypatch):
    """``created_at`` steht auf die Sekunde genau; der Zufallswert daneben nicht."""
    monkeypatch.setattr(runden, "_now", lambda: "2026-05-01T20:00:00+00:00")
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    lebenszyklus.loeschen(konfiguration, unsere)
    frisch = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=GILDE)

    assert (frisch.id, frisch.created_at) == (unsere.id, unsere.created_at)
    assert frisch.token and frisch.token != unsere.token
    assert chronik.dieselbe_runde(konfiguration, GILDE, unsere) is None
    assert chronik.dieselbe_runde(konfiguration, GILDE, frisch) == frisch


# -- Und über der Runde steht niemand -----------------------------------------------------

# Der Betreiber der Box löscht keine fremde Runde (#90). Technisch stünde ihm alles offen —
# es ist seine SQLite —, der Unterschied ist das fehlende Bedienelement. Deshalb wird hier
# nicht ein Verhalten geprüft, sondern die Menge der Aufrufer: eine neue Route, ein CLI, ein
# zweiter Knopf tauchen als zusätzlicher Eintrag auf und fallen durch.
QUELLEN = Path(lebenszyklus.__file__).parent
ZERSTOEREND = frozenset({"loeschen", "sperren"})
NUR_DIE_GRUPPE = {"bot/einrichten.py"}


def _aufrufer() -> set[str]:
    gefunden: set[str] = set()
    for datei in sorted(QUELLEN.rglob("*.py")):
        if datei.samefile(lebenszyklus.__file__):
            continue
        for knoten in ast.walk(ast.parse(datei.read_text(encoding="utf-8"))):
            ruft = (
                isinstance(knoten, ast.Call)
                and isinstance(knoten.func, ast.Attribute)
                and knoten.func.attr in ZERSTOEREND
                and isinstance(knoten.func.value, ast.Name)
                and knoten.func.value.id == "lebenszyklus"
            )
            holt = (
                isinstance(knoten, ast.ImportFrom)
                and knoten.module == "chronicle.lebenszyklus"
                and any(name.name in ZERSTOEREND for name in knoten.names)
            )
            if ruft or holt:
                gefunden.add(datei.relative_to(QUELLEN).as_posix())
    return gefunden


def test_nur_die_gruppe_selbst_loescht_oder_sperrt_ihre_runde():
    """Kein Betreiber-Weg: außerhalb des Moduls ruft das nur der Discord-Weg der Gruppe."""
    assert _aufrufer() == NUR_DIE_GRUPPE


# -- Sofort still, und zwar überall ------------------------------------------------------


def test_der_rauswurf_vergisst_das_foundry_passwort(konfiguration, bot):
    """Sonst zöge ein Abgleich zwölf Stunden lang die Welt einer Gruppe, die widerrufen hat."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    zugang.merken(unsere, "passwort-nur-fuer-den-test")

    austritt(bot, FakeGilde())

    assert not zugang.ist_gemerkt(unsere)


def test_die_ruhende_runde_holt_nichts_mehr_und_legt_nichts_mehr_ab(konfiguration, bot):
    """Die Kernaussage des Umbaus, geprüft an den Stufen, die Daten erzeugen und holen."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    austritt(bot, FakeGilde())

    # ``unsere`` ist der Stand von vor dem Rauswurf — genau den hält ein laufender Stapel.
    assert transcribe_service.run_queue(konfiguration, unsere) == ()
    assert compose_service.compose_session(konfiguration, unsere, ids["sitzung"]) is None
    assert compose_service.recap_session(konfiguration, unsere, ids["sitzung"]) is None
    zustand = foundry_service.sync(konfiguration, unsere, passwort="passwort-nur-fuer-den-test")
    assert zustand.stale
    assert zustand.message == lebenszyklus.RUHT


# -- Die Frist ---------------------------------------------------------------------------


def test_das_zugesagte_datum_steht_in_der_zone_der_runde(konfiguration):
    """Der Container läuft in UTC; die Gruppe liest ihren Kalender nicht in UTC."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    settings.save_nightly_zone(unsere, "Pacific/Auckland")
    frist_setzen(konfiguration, unsere, "2026-09-05T22:00:00+00:00")

    gesperrt = runden.get(konfiguration.database_path, unsere.id)

    assert lebenszyklus.frist_datum(gesperrt) == "06.09.2026"


def test_nach_der_frist_bringt_die_wiedereinladung_nichts_zurueck(konfiguration, bot):
    """Am vierzigsten Tag zurückzukommen holt nicht zurück, was als fort zugesagt war."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    frist_setzen(konfiguration, unsere, "2026-05-31T20:00:00+00:00")

    kanal = FakeKanal("300", "allgemein")
    eintritt(bot, FakeGilde(system=kanal))

    assert runden.get(konfiguration.database_path, unsere.id) is None
    assert not ids["spur"].exists()
    assert kanal.gesendet == [einrichten.WILLKOMMEN]


def test_nach_der_frist_richtet_setup_eine_frische_runde_ein(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    frist_setzen(konfiguration, unsere, "2026-05-31T20:00:00+00:00")

    ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronist")

    frisch = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert frisch is not None and not frisch.gesperrt
    assert notes.session(frisch, ids["sitzung"]) is None
    assert not ids["spur"].exists()


def test_ein_fehlschlag_beendet_die_taegliche_loeschung_nicht(konfiguration, monkeypatch, caplog):
    """Ohne diese Zeile hörte das Löschen nach dem ersten Stolpern für **alle** Runden auf."""

    class Schluss(Exception):
        pass

    laeufe = []

    def stolpert(config, **rest):
        laeufe.append(config)
        raise PermissionError("die Datei gehört jemand anderem")

    async def schlafen(_sekunden):
        if len(laeufe) == 2:
            raise Schluss

    monkeypatch.setattr(lebenszyklus, "sweep", stolpert)

    with pytest.raises(Schluss):
        asyncio.run(lebenszyklus.taeglich(konfiguration, schlafen=schlafen))

    assert len(laeufe) == 2
    assert "jemand anderem" in caplog.text


def test_eine_sture_runde_haelt_die_naechste_nicht_auf(konfiguration, monkeypatch, caplog):
    """Sonst hielte eine, die dauerhaft nicht wegzubekommen ist, jede hinter ihr auf —
    jeden Tag dieselben, und niemand sähe, dass eine Zusage seit Wochen offen ist."""
    stur = runden.anlegen(konfiguration.database_path, "Sture", guild_id=GILDE)
    dahinter = runden.anlegen(konfiguration.database_path, "Dahinter", guild_id=NACHBARGILDE)
    for eine in (stur, dahinter):
        frist_setzen(konfiguration, eine, "2026-05-31T20:00:00+00:00")
    echt = lebenszyklus.loeschen

    def stolpert(config, runde, **rest):
        if runde.id == stur.id:
            raise PermissionError("die Datei gehört jemand anderem")
        return echt(config, runde, **rest)

    monkeypatch.setattr(lebenszyklus, "loeschen", stolpert)

    meldungen = lebenszyklus.sweep(konfiguration, jetzt=datetime(2026, 7, 1, 20, tzinfo=UTC))

    assert runden.get(konfiguration.database_path, stur.id) is not None
    assert runden.get(konfiguration.database_path, dahinter.id) is None
    assert [meldung for meldung in meldungen if "Sture" in meldung and "31 Tagen" in meldung]
    assert "Sture" in caplog.text


def test_ein_beendeter_faden_wird_beim_naechsten_anmelden_neu_gestartet(
    konfiguration, bot, monkeypatch
):
    """Ein fertiger Task ist nicht ``None`` — sonst bliebe eine Zusage für immer liegen."""
    gestartet = []

    async def sofort_fertig(config, **rest):
        gestartet.append(config)

    monkeypatch.setattr(recordings, "taeglich", sofort_fertig)
    monkeypatch.setattr(lebenszyklus, "taeglich", sofort_fertig)

    async def zweimal_anmelden():
        await bot.ereignisse["on_ready"]()
        await asyncio.sleep(0)
        await bot.ereignisse["on_ready"]()
        await asyncio.sleep(0)

    asyncio.run(zweimal_anmelden())

    assert len(gestartet) == 4


def test_eine_tonspur_ohne_zeile_verschwindet_mit(konfiguration):
    """Eine Spur liegt die ganze Sitzung auf der Platte und wird erst am Ende eingereiht —
    ein Rauswurf mittendrin hinterlässt sonst Aufnahmen, die niemand mehr findet."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    nachbar = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    daneben = fuellen(konfiguration, nachbar, "beta")
    verwaist = konfiguration.recordings_dir / f"sitzung{ids['sitzung']}-20260501T200000-Mira.wav"
    verwaist.write_bytes(b"ton")
    fremd = konfiguration.recordings_dir / f"sitzung{daneben['sitzung']}-20260501T200000-Mira.wav"
    fremd.write_bytes(b"ton")

    lebenszyklus.loeschen(konfiguration, unsere)

    assert not verwaist.exists()
    assert fremd.exists()


# -- Was das Löschen kostet --------------------------------------------------------------


def test_die_loeschfrage_sagt_auch_was_bleibt(konfiguration, bot):
    """Der Nachweis geht, das aus ihm Geschriebene bleibt in Discord — das wird gesagt."""
    runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)

    ctx = loeschbefehl(bot, FakeCtx(gilde=FakeGilde()))

    (frage,) = ctx.antworten
    assert "Das bleibt:" in frage
    assert "liegt weiter in Discord" in frage
    assert "Wer den Beleg braucht" in frage


def test_die_hinausgeworfene_runde_darf_sofort_loeschen(konfiguration, bot):
    """Vergessen darf keine Rückkehr verlangen — sonst muss man den Bot erst wiederholen."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    austritt(bot, FakeGilde())

    ctx = loeschbefehl(bot, FakeCtx(gilde=FakeGilde()))
    ja, _nein = ctx.ansichten[0].items
    asyncio.run(ja.callback(FakeInteraction()))

    assert runden.get(konfiguration.database_path, unsere.id) is None
    assert not ids["spur"].exists()


# -- Zurück in den Dienst ----------------------------------------------------------------


def test_ohne_kanal_zum_reden_bleibt_die_runde_still(konfiguration, bot):
    """Wieder im Dienst zu sein, ohne dass die Gruppe die Offenlegung gelesen hat, ist
    genau der Zustand, für den es sie gibt."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    austritt(bot, FakeGilde())

    eintritt(bot, FakeGilde(kanaele=(FakeKanal("300", "regeln", darf=False),)))

    assert runden.get(konfiguration.database_path, unsere.id).gesperrt


def test_setup_holt_die_offenlegung_nach(konfiguration, bot):
    """Und zwar im Kanal: die Offenlegung ist eine Aussage an die Gruppe, nicht an den
    einen, der eingerichtet hat — eine flüchtige Antwort liest sonst niemand sonst."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    austritt(bot, FakeGilde())
    kanal = FakeKanal("300", "allgemein")

    ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronist", kanal=kanal
    )

    assert kanal.gesendet == [einrichten.OFFENLEGUNG]
    assert not runden.get(konfiguration.database_path, unsere.id).gesperrt


def test_ohne_zugestellte_offenlegung_bleibt_die_runde_still(konfiguration, bot):
    """Freigegeben wird erst, wenn sie angekommen ist — geschrieben zu haben genügt nicht."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    austritt(bot, FakeGilde())

    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())),
        benutzer="Chronist",
        kanal=FakeKanal("300", "allgemein", bricht=True),
    )

    assert einrichten.STILL_GEBLIEBEN in interaktion.response.gesendet[0]["text"]
    assert runden.get(konfiguration.database_path, unsere.id).gesperrt


def test_eine_begruessung_die_nicht_ankommt_gibt_die_runde_nicht_frei(konfiguration, bot):
    """Die Rechteprüfung sagt zu, dass gesendet werden *darf* — nicht, dass es gelang."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    austritt(bot, FakeGilde())

    with pytest.raises(RuntimeError):
        eintritt(bot, FakeGilde(system=FakeKanal("300", "allgemein", bricht=True)))

    assert runden.get(konfiguration.database_path, unsere.id).gesperrt


def test_nach_der_frist_sagt_setup_nicht_es_bleibe_alles_wie_es_war(konfiguration, bot):
    """Die frische Runde bekommt dieselbe Kennung — die Antwort darf trotzdem nicht
    behaupten, die Einstellungen von vorher stünden noch."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    fuellen(konfiguration, unsere, "alpha")
    frist_setzen(konfiguration, unsere, "2026-05-31T20:00:00+00:00")

    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronist"
    )

    frisch = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert frisch.id == unsere.id
    gesagt = interaktion.response.gesendet[0]["text"]
    assert einrichten.LEER_BLEIBT not in gesagt
    assert einrichten.EINGERICHTET.format(name=GILDENAME) in gesagt


def test_geloescht_wird_neben_der_ereignisschleife(konfiguration, bot, monkeypatch):
    """Dateien und Zeilen einer großen Runde dauern; solange die Schleife rechnet,
    antwortet der Bot niemandem — weder beim Wiedersehen noch am Knopf."""
    abgelaufen = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    frist_setzen(konfiguration, abgelaufen, "2026-05-31T20:00:00+00:00")
    faeden = []
    echt = lebenszyklus.loeschen

    def merken(config, runde, **rest):
        faeden.append(threading.get_ident())
        return echt(config, runde, **rest)

    monkeypatch.setattr(lebenszyklus, "loeschen", merken)

    eintritt(bot, FakeGilde(system=FakeKanal("300", "allgemein")))

    runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    ctx = loeschbefehl(bot, FakeCtx(guild_id=NACHBARGILDE, gilde=FakeGilde(kennung=NACHBARGILDE)))
    ja, _nein = ctx.ansichten[0].items
    asyncio.run(ja.callback(FakeInteraction(guild_id=NACHBARGILDE)))

    assert len(faeden) == 2
    assert threading.get_ident() not in faeden


class Genug(Exception):
    """Hält den dauerhaften Lauf nach einem Durchgang an."""


def _loeschfaeden(monkeypatch) -> list[int]:
    """Sammelt, in welchem Faden gelöscht wurde — der der Schleife darf nicht dabei sein."""
    faeden: list[int] = []
    echt = lebenszyklus.loeschen

    def merken(config, runde, **rest):
        faeden.append(threading.get_ident())
        return echt(config, runde, **rest)

    monkeypatch.setattr(lebenszyklus, "loeschen", merken)
    return faeden


def test_setup_loescht_die_abgelaufene_runde_neben_der_ereignisschleife(
    konfiguration, bot, monkeypatch
):
    """``/setup`` erreicht denselben Löschweg wie das Wiedersehen — und darf es genauso wenig
    auf der Schleife tun."""
    abgelaufen = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    fuellen(konfiguration, abgelaufen, "alpha")
    frist_setzen(konfiguration, abgelaufen, "2026-05-31T20:00:00+00:00")
    faeden = _loeschfaeden(monkeypatch)

    ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronist")

    assert len(faeden) == 1
    assert threading.get_ident() not in faeden


def test_der_taegliche_lauf_loescht_neben_der_ereignisschleife(konfiguration, monkeypatch):
    """Der tägliche Lauf nimmt *jede* überfällige Runde — auf der Schleife stünde der Bot
    so lange für alle still."""
    for gilde, name in ((GILDE, GILDENAME), (NACHBARGILDE, "Nachbarn")):
        ueberfaellig = runden.anlegen(konfiguration.database_path, name, guild_id=gilde)
        frist_setzen(konfiguration, ueberfaellig, "2026-05-31T20:00:00+00:00")
    faeden = _loeschfaeden(monkeypatch)

    async def genug(_abstand):
        raise Genug

    with pytest.raises(Genug):
        asyncio.run(lebenszyklus.taeglich(konfiguration, schlafen=genug))

    assert len(faeden) == 2
    assert threading.get_ident() not in faeden
    assert runden.fuer_gilde(konfiguration.database_path, GILDE) is None
    assert runden.fuer_gilde(konfiguration.database_path, NACHBARGILDE) is None
