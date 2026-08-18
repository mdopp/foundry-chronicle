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
from test_bot import (
    FakeAnhang,
    FakeBot,
    FakeButton,
    FakeIntents,
    FakePCMAudio,
    FakePermissions,
    FakeRechte,
    FakeSenke,
    FakeView,
)
from test_chronik import FakeHTTPException, FakeInputText, FakeModal
from test_erinnern import FakeEmbed, FakeSelect, FakeSelectOption

from chronicle import (
    consent,
    db,
    lebenszyklus,
    nightly,
    notes,
    people,
    recordings,
    register,
    search,
    settings,
    zugang,
)
from chronicle import runde as runden
from chronicle.bot import chronik, einrichten, gateway
from chronicle.compose import service as compose_service
from chronicle.config import Config
from chronicle.discord import grenzen
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
    """Die erste Antwort auf eine Interaktion — mit dem Aufschub, den Discord auch kennt.

    ``defer`` und ``is_done`` gehören zum echten ``InteractionResponse``. Fehlten sie hier,
    ginge ein Rückruf, der aufschiebt, im Test an einem ``AttributeError`` unter statt an
    dem, was er tut.
    """

    def __init__(self):
        self.gesendet: list[dict] = []
        self.bearbeitet: list[dict] = []
        self.aufgeschoben = False

    def is_done(self) -> bool:
        return self.aufgeschoben or bool(self.gesendet)

    async def defer(self, **rest):
        self.aufgeschoben = True

    async def send_message(self, text=None, *, view=None, **rest):
        # Discord weist eine zweite *erste* Antwort ab. Der Fake tut es auch, sonst
        # bliebe ein Rückruf grün, der in echt an genau dieser Stelle scheiterte.
        if self.is_done():
            raise RuntimeError("InteractionResponded: schon beantwortet")
        self.gesendet.append({"text": text, "view": view})

    async def edit_message(self, *, content=None, view=None, **rest):
        self.bearbeitet.append({"content": content, "view": view})


class FakeNachreichung:
    """Was nach einem Aufschub nachgereicht wird — für den Absender dieselbe Antwort.

    Sie landet in derselben Liste wie eine unmittelbare Antwort: geprüft wird, *dass*
    jemand etwas erfährt, nicht über welchen der beiden Wege Discord es zustellt.
    """

    def __init__(self, gesendet: list[dict]):
        self._gesendet = gesendet

    async def send(self, text=None, *, view=None, **rest):
        self._gesendet.append({"text": text, "view": view})


class FakeMitglied:
    """Ein Mitglied mit genau den Rechten, die Discord für diesen Server meldet."""

    def __init__(self, kennung="4001", name="Mira", *, verwaltet=False, admin=False):
        self.id = kennung
        self.display_name = name
        self.guild_permissions = FakeRechte(manage_guild=verwaltet, administrator=admin)


LEITUNG = FakeMitglied("4000", "Spielleitung", verwaltet=True, admin=True)
MITGLIED = FakeMitglied()
# Verwaltet den Server, führt ihn aber nicht: genau die Person, an der sich die Schranke
# vor dem Löschen von der vor dem Einrichten unterscheidet.
VERWALTUNG = FakeMitglied("4002", "Verwaltung", verwaltet=True)


class FakeInteraction:
    def __init__(self, *, guild_id=GILDE, wer=LEITUNG, kanal=None):
        self.response = FakeAntwort()
        self.followup = FakeNachreichung(self.response.gesendet)
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


class FakeOption:
    """Das Feld eines Slash-Befehls, so weit die Doppel es brauchen.

    Es hält seinen ``input_type`` wie das echte: dort muss eine **Klasse** stehen. Eine
    Zeichenkette ist der Fehler, an dem py-cord beim ersten echten Aufruf stirbt.
    """

    def __init__(self, input_type, description="", default=None, required=True, **rest):
        self.input_type = input_type
        self.description = description
        self.default = default
        self.required = required

    def __repr__(self) -> str:
        return f"FakeOption({self.input_type!r}, default={self.default!r})"


@pytest.fixture
def pycord(monkeypatch):
    modul = types.ModuleType("discord")
    modul.Option = FakeOption
    modul.Intents = FakeIntents
    modul.Bot = FakeBot
    modul.Permissions = FakePermissions
    modul.PCMAudio = FakePCMAudio
    modul.HTTPException = FakeHTTPException
    modul.Embed = FakeEmbed
    modul.SelectOption = FakeSelectOption
    modul.Attachment = FakeAnhang
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


def ausfuellen(fenster, adresse="", benutzer="", uhrzeit="", zone="", *, kanal=None):
    for feld, wert in zip(fenster.children, (adresse, benutzer, uhrzeit, zone), strict=True):
        feld.value = wert
    interaktion = FakeInteraction(kanal=kanal)
    asyncio.run(fenster.callback(interaktion))
    return interaktion


def menues(interaktion):
    """Die beiden Menüs unter der Antwort von ``/setup`` — Zustellkanal und Quelle."""
    kanal, quelle = interaktion.response.gesendet[0]["view"].items
    return kanal, quelle


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


# -- Eine Runde aus der Zeit vor den Gilden -----------------------------------------------


@pytest.fixture
def stiller_bot(bot, monkeypatch):
    """Der Bot mit stillgelegten Dauerläufen — hier geht es um das, was ``on_ready`` sonst tut."""

    async def nichts(config, **rest):
        return None

    monkeypatch.setattr(recordings, "taeglich", nichts)
    monkeypatch.setattr(lebenszyklus, "taeglich", nichts)
    return bot


def anmelden(bot, *gilden):
    """Der Bot meldet sich an — in genau den Gilden, in denen er steht."""
    bot.guilds = tuple(gilden)

    async def lauf():
        await bot.ereignisse["on_ready"]()
        # Die beiden Dauerläufe werden als Task gestellt; ohne diesen Takt endete die
        # Schleife mit zwei Tasks, die nie gelaufen sind.
        await asyncio.sleep(0)

    asyncio.run(lauf())


def gilde_von(config, runde_id):
    return runden.get(config.database_path, runde_id).guild_id


def test_die_einzige_runde_ohne_gilde_bekommt_die_einzige_gilde(konfiguration, stiller_bot, caplog):
    """Der Fall aus #121: sonst legt ``/setup`` eine zweite Runde an, und die alte ist
    über Discord für immer unerreichbar — samt Sitzungen, Chroniken und Einwilligungen."""
    verwaist = runden.erste(konfiguration.database_path)
    fuellen(konfiguration, verwaist, "alpha")
    kanal = FakeKanal("300", "allgemein")

    with caplog.at_level(logging.INFO):
        anmelden(stiller_bot, FakeGilde(system=kanal))

    assert gilde_von(konfiguration, verwaist.id) == GILDE
    # Und damit ist der Weg zurück offen: ``/setup`` findet sie, statt daneben eine zweite
    # anzulegen.
    beansprucht = lebenszyklus.beanspruchen(konfiguration, GILDE, GILDENAME)
    assert beansprucht.neu is False
    assert beansprucht.runde.id == verwaist.id
    assert len(runden.alle(konfiguration.database_path)) == 1
    assert notes.sessions(beansprucht.runde)

    assert verwaist.name in caplog.text and GILDENAME in caplog.text
    assert kanal.gesendet == [lebenszyklus.UEBERNOMMEN_GESAGT]


def test_der_uebernahmesatz_kommt_auch_gewachsen_an(konfiguration, stiller_bot, monkeypatch):
    """Er ist eine wachsende Konstante wie die Befehlsliste — also geht auch er geteilt raus.

    Der Satz erklärt einer Gruppe, dass ihre Runde wieder erreichbar ist. Wüchse er über
    Discords Grenze, käme davon nichts an (#109) und die Gruppe säße vor einer leeren Runde,
    ohne zu erfahren, warum.
    """
    monkeypatch.setattr(lebenszyklus, "UEBERNOMMEN_GESAGT", "Satz zur Übernahme.\n" * 200)
    fuellen(konfiguration, runden.erste(konfiguration.database_path), "alpha")
    kanal = FakeKanal("300", "allgemein")

    anmelden(stiller_bot, FakeGilde(system=kanal))

    assert len(kanal.gesendet) > 1
    assert all(len(gesagt) <= grenzen.NACHRICHT for gesagt in kanal.gesendet)
    assert "".join(kanal.gesendet) == lebenszyklus.UEBERNOMMEN_GESAGT


def test_neben_einer_zweiten_runde_wird_nichts_uebernommen(konfiguration, stiller_bot):
    """Zwei Runden, eine ohne Gilde: welche gemeint ist, weiß niemand — also keine."""
    verwaist = runden.erste(konfiguration.database_path)
    runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    kanal = FakeKanal("300", "allgemein")

    anmelden(stiller_bot, FakeGilde(system=kanal))

    assert gilde_von(konfiguration, verwaist.id) is None
    assert kanal.gesendet == []


def test_eine_runde_mit_gilde_wird_nicht_umgehaengt(konfiguration, stiller_bot):
    """Die härteste Probe: eine fremde Kampagne an die eigene Gilde zu hängen wäre das Leck."""
    lebenszyklus.loeschen(konfiguration, runden.erste(konfiguration.database_path))
    fremde = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    kanal = FakeKanal("300", "allgemein")

    anmelden(stiller_bot, FakeGilde(system=kanal))

    assert gilde_von(konfiguration, fremde.id) == NACHBARGILDE
    assert kanal.gesendet == []


def test_in_zwei_gilden_wird_nichts_uebernommen(konfiguration, stiller_bot):
    """Die erstbeste zu nehmen hieße, fremde Klarnamen an eine beliebige Gruppe zu geben."""
    verwaist = runden.erste(konfiguration.database_path)
    hier = FakeKanal("300", "allgemein")
    dort = FakeKanal("400", "allgemein")

    anmelden(
        stiller_bot,
        FakeGilde(system=hier),
        FakeGilde(kennung=NACHBARGILDE, name="Nachbarn", system=dort),
    )

    assert gilde_von(konfiguration, verwaist.id) is None
    assert hier.gesendet == [] and dort.gesendet == []


def test_ohne_jede_runde_meldet_sich_der_bot_trotzdem_an(konfiguration, stiller_bot):
    lebenszyklus.loeschen(konfiguration, runden.erste(konfiguration.database_path))
    kanal = FakeKanal("300", "allgemein")

    anmelden(stiller_bot, FakeGilde(system=kanal))

    assert runden.alle(konfiguration.database_path) == ()
    assert kanal.gesendet == []


def test_das_zweite_anmelden_uebernimmt_nicht_noch_einmal(konfiguration, stiller_bot):
    """``on_ready`` kommt nach jeder Wiederverbindung noch einmal — und findet nichts mehr."""
    verwaist = runden.erste(konfiguration.database_path)
    kanal = FakeKanal("300", "allgemein")
    gilde = FakeGilde(system=kanal)

    anmelden(stiller_bot, gilde)
    anmelden(stiller_bot, gilde)

    assert gilde_von(konfiguration, verwaist.id) == GILDE
    assert kanal.gesendet == [lebenszyklus.UEBERNOMMEN_GESAGT]


def test_ohne_kanal_zum_reden_steht_die_uebernahme_nur_im_log(konfiguration, stiller_bot, caplog):
    """Ungesagt ist schlecht, aber besser als eine Runde, an die niemand mehr herankommt."""
    verwaist = runden.erste(konfiguration.database_path)

    with caplog.at_level(logging.INFO):
        anmelden(stiller_bot, FakeGilde(kanaele=(FakeKanal("300", "regeln", darf=False),)))

    assert gilde_von(konfiguration, verwaist.id) == GILDE
    assert "Kein Kanal" in caplog.text


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
    # Adresse, Benutzer, Uhrzeit, Zone — das Modell gehört der Instanz und steht nicht hier,
    # die Quelle der Zahlen kommt als Menü darunter. Discord nimmt fünf Felder je Fenster.
    assert len(fenster.children) == 4
    assert "odell" not in beschriftet
    assert "uelle" not in beschriftet and "estwelt" not in beschriftet


def test_das_benutzerfeld_sagt_mit_wessen_augen_ich_sehe(bot):
    """Wer das Konto einträgt, entscheidet über die Sicht — das steht am Feld (#78)."""
    fenster = einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde()))

    _, benutzerfeld, _, _ = fenster.children
    assert "Augen" in benutzerfeld.label
    assert "Spielerkonto" in benutzerfeld.placeholder


def test_die_beschriftungen_bleiben_in_discords_grenzen():
    """45 Zeichen für die Beschriftung, 100 für den Hinweis — geprüft von py-cord selbst."""
    echtes_discord = pytest.importorskip("discord")

    felder = (
        (einrichten.FELD_ADRESSE, einrichten.HINWEIS_ADRESSE),
        (einrichten.FELD_BENUTZER, einrichten.HINWEIS_BENUTZER),
        (einrichten.FELD_UHRZEIT, einrichten.HINWEIS_UHRZEIT),
        (einrichten.FELD_ZONE, einrichten.HINWEIS_ZONE),
    )
    for beschriftung, hinweis in felder:
        echtes_discord.ui.InputText(label=beschriftung, placeholder=hinweis, required=False)


def test_das_fenster_bleibt_unter_discords_fuenf_feldern():
    """Die harte Grenze, an der jedes weitere Feld scheitert — py-cord wirft dafür.

    ``_ModalWeights`` führt fünf Reihen; ein sechstes ``InputText`` findet keine mehr. Das
    ist der Rahmen, in dem die Zone noch Platz hat (vier von fünf) und in dem die Quelle
    der Spieldaten nicht mehr als weiteres Feld untergebracht werden sollte — das letzte
    freie ist die Reserve für den nächsten runden-eigenen Wert.
    """
    echtes_discord = pytest.importorskip("discord")

    async def bauen(anzahl):
        return echtes_discord.ui.Modal(
            *(
                echtes_discord.ui.InputText(label=f"Feld {nummer}", required=False)
                for nummer in range(anzahl)
            ),
            title=einrichten.SETUP_TITEL,
        )

    assert len(asyncio.run(bauen(5)).children) == 5
    with pytest.raises(ValueError, match="open space"):
        asyncio.run(bauen(6))


def test_die_einrichtung_empfiehlt_das_spielerkonto_und_ein_eigenes_konto(bot):
    """Der ganze Satz passt in kein Feld und steht deshalb in der Antwort."""
    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronist"
    )

    gesagt = interaktion.response.gesendet[0]["text"]
    assert einrichten.AUGEN in gesagt
    assert "Spielerkonto" in gesagt
    assert "»Chronik«" in gesagt


def test_der_rat_sagt_auch_dass_die_rechte_je_figur_vergeben_werden(bot):
    """»Mit denselben Rechten« ist Handarbeit — sonst sieht »Chronik« fast nichts (#113).

    ``permissions.effective_level`` ist ``max(ownership.default, ownership[userId])``: ein
    frisch angelegtes Konto steht in keiner Karte und bekommt nur, was ``default`` ohnehin
    freigibt. Der Rat führte ohne diesen Zusatz in die schlechteste aller Varianten.
    """
    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronik"
    )

    gesagt = interaktion.response.gesendet[0]["text"]
    assert "je Figur" in gesagt
    assert "Beobachter" in gesagt


def test_die_antwort_der_einrichtung_bleibt_unter_discords_grenze(bot):
    """Der schlimmste Fall: neue Runde, Konto gesetzt, Uhrzeit unlesbar, Felder offen.

    Discord weist eine Interaktionsantwort über 2000 Zeichen ab — dann erführe der
    Einrichtende nicht einmal, ob seine Runde nun steht (#114).
    """
    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())),
        benutzer="Chronik",
        uhrzeit="viertel nach sieben",
    )

    assert len(interaktion.response.gesendet[0]["text"]) <= 2000


def test_eine_masslose_uhrzeit_wird_gekuerzt_zurueckgegeben(bot):
    """4000 Zeichen passen in ein ``InputText``, aber nicht in die Antwort darauf (#114)."""
    lang = "z" * 4000

    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), benutzer="Chronik", uhrzeit=lang
    )

    gesagt = interaktion.response.gesendet[0]["text"]
    assert len(gesagt) <= 2000
    assert lang not in gesagt
    assert einrichten.ECHO_GEKUERZT in gesagt


def test_wer_nur_die_uhrzeit_richtet_hoert_den_satz_nicht_noch_einmal(bot):
    ctx = FakeCtx(gilde=FakeGilde())
    ausfuellen(einrichtungsfenster(bot, ctx), benutzer="Chronist")

    nur_uhrzeit = ausfuellen(einrichtungsfenster(bot, ctx), uhrzeit="05:00")
    wieder_das_konto = ausfuellen(einrichtungsfenster(bot, ctx), benutzer="Chronistin")

    assert einrichten.AUGEN not in nur_uhrzeit.response.gesendet[0]["text"]
    assert einrichten.AUGEN in wieder_das_konto.response.gesendet[0]["text"]


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


def test_eine_adresse_aus_der_browserzeile_wird_abgewiesen_und_gesagt(konfiguration, bot):
    """#243: ohne Schema liest ``urlsplit`` den Hostnamen als Schema — und niemand merkt es.

    Genau diese Zeile stand auf der Box: kopiert, während der Anmeldebildschirm eines
    Moduls offen war. Angenommen wurde sie, gespielt wurde einen Abend lang, angekommen
    ist kein einziger Wurf.
    """
    aus_dem_browser = "danielfoundry.example:30000/modules/login.html?world=daggerheart"

    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), adresse=aus_dem_browser
    )

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.stored(unsere).get("foundry_url") is None
    gesagt = interaktion.response.gesendet[0]["text"]
    assert aus_dem_browser[: einrichten.ECHO_GRENZE] in gesagt
    assert "nicht** übernommen" in gesagt


def test_die_wurzel_mit_schema_und_port_geht_durch(konfiguration, bot):
    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())),
        adresse="http://foundry.example:30000/",
    )

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.effective(konfiguration, unsere).foundry_url == "http://foundry.example:30000/"
    assert einrichten.ADRESSE_UNBRAUCHBAR[:20] not in interaktion.response.gesendet[0]["text"]


def test_der_zustellkanal_wird_gewaehlt_und_wirkt_sofort(konfiguration, bot):
    kanal = FakeKanal("777", "chroniken")
    ctx = FakeCtx(gilde=FakeGilde(kanaele=(kanal,)))
    interaktion = ausfuellen(einrichtungsfenster(bot, ctx), benutzer="Chronist")

    menue, _quelle = menues(interaktion)
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
    menue, _quelle = menues(interaktion)

    menue.values = [einrichten.OHNE_KANAL]
    asyncio.run(menue.callback(FakeInteraction()))

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.effective(konfiguration, unsere).discord_recap_channel is None


# -- Die Zone, in der 04:00 auch 04:00 heißt ---------------------------------------------


def test_die_zeitzone_steht_im_fenster_neben_der_uhrzeit_und_wirkt(konfiguration, bot):
    """Bis hierher wirkte sie, ohne dass jemand sie ändern konnte (#110).

    Geprüft wird nicht die gespeicherte Zeichenkette, sondern die Nacht: 04:00 Auckland
    ist ein anderer Zeitpunkt als 04:00 Berlin, und genau das ist der ganze Sinn des Feldes.
    """
    ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())),
        benutzer="Chronist",
        zone="Pacific/Auckland",
    )
    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)

    assert settings.nightly_zone(unsere) == "Pacific/Auckland"
    assert not nightly.faellig(
        datetime(2026, 8, 6, 2, tzinfo=UTC), "04:00", None, settings.nightly_zone(unsere)
    )
    assert nightly.faellig(
        datetime(2026, 8, 5, 16, tzinfo=UTC), "04:00", None, settings.nightly_zone(unsere)
    )


def test_eine_unbekannte_zeitzone_wird_abgewiesen_statt_still_uebernommen(konfiguration, bot):
    """Gespeichert würde sie sonst und gelesen nicht — der Lauf verschöbe sich stumm.

    ``settings.nightly_zone`` fällt bei einem unbekannten Namen auf die Vorgabe zurück.
    Wer ihn stillschweigend annähme, ließe eine Runde in dem Glauben, ihr Lauf stehe auf
    ihrer Uhr, während er weiter nach Berlin ginge.
    """
    ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), zone="Pacific/Auckland")

    interaktion = ausfuellen(
        einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), zone="Europe/Wolkenkuckucksheim"
    )

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.nightly_zone(unsere) == "Pacific/Auckland"
    gesagt = interaktion.response.gesendet[0]["text"]
    assert "Europe/Wolkenkuckucksheim" in gesagt
    assert "Pacific/Auckland" in gesagt


def test_ein_leeres_zonenfeld_laesst_die_zone_stehen(konfiguration, bot):
    ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), zone="Pacific/Auckland")

    interaktion = ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())), uhrzeit="05:00")

    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert settings.nightly_zone(unsere) == "Pacific/Auckland"
    assert settings.nightly_time(unsere) == "05:00"
    assert "kenne ich als Zeitzone nicht" not in interaktion.response.gesendet[0]["text"]


# -- Woher die Zahlen kommen -------------------------------------------------------------


def test_die_quelle_der_spieldaten_wird_im_menue_gewaehlt_und_wirkt(konfiguration, bot):
    """Der zweite Wert ohne Bedienstelle aus #110 — und der Weg zurück gehört dazu.

    Eine Runde, die auf der Testwelt steht, kam ohne Datenbankzugriff nicht mehr davon los.
    Deshalb wird hier beides gefahren: hin und wieder her.
    """
    interaktion = ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())))
    _kanal, quelle = menues(interaktion)
    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)

    assert [option.value for option in quelle.options] == [settings.SERVER, settings.TESTWELT]
    assert [option.default for option in quelle.options] == [True, False]

    quelle.values = [settings.TESTWELT]
    hin = FakeInteraction()
    asyncio.run(quelle.callback(hin))

    assert settings.foundry_quelle(unsere) == settings.TESTWELT
    assert "erfunden" in hin.response.bearbeitet[0]["content"]

    # Die Ansicht bleibt stehen — sonst wäre die Testwelt eine Einbahnstraße, und genau
    # das war der gemeldete Zustand.
    zurueck_menue = hin.response.bearbeitet[0]["view"].items[1]
    assert [option.default for option in zurueck_menue.options] == [False, True]

    zurueck_menue.values = [settings.SERVER]
    her = FakeInteraction()
    asyncio.run(zurueck_menue.callback(her))

    assert settings.foundry_quelle(unsere) == settings.SERVER


def test_die_wahl_der_quelle_wirkt_auf_das_passwortfenster(konfiguration, bot):
    """Sie ist kein Etikett: auf der Testwelt fragt der Bot gar nicht erst nach Foundry."""
    settings.save(
        runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE),
        {"foundry_url": "https://foundry.example", "foundry_user": "chronist"},
    )
    unsere = runden.fuer_gilde(konfiguration.database_path, GILDE)
    assert chronik.foundry_im_spiel(konfiguration, unsere)

    einrichten.quelle_setzen(unsere, settings.TESTWELT)

    assert not chronik.foundry_im_spiel(konfiguration, unsere)


def test_ein_altes_quellenmenue_haengt_nicht_die_fremde_runde_auf_die_testwelt(konfiguration, bot):
    """Dasselbe wie am Kanalmenü, und teurer: die Nachbarrunde bekäme erfundene Zahlen."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    interaktion = ausfuellen(einrichtungsfenster(bot, FakeCtx(gilde=FakeGilde())))
    _kanal, quelle = menues(interaktion)

    lebenszyklus.loeschen(konfiguration, unsere)
    nachbar = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    assert nachbar.id == unsere.id
    quelle.values = [settings.TESTWELT]
    klick = FakeInteraction()
    asyncio.run(quelle.callback(klick))

    assert klick.response.bearbeitet[0]["content"] == chronik.VERALTET
    assert settings.foundry_quelle(nachbar) == settings.SERVER


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


def test_die_frist_ist_dreissig_tage():
    """Nach #90 der einzige Weg, auf dem eine Runde ohne Zutun der Gruppe verschwindet.

    Die Zeile darüber setzt die Konstante ein und hielte jeden Wert; hier steht die Zahl.
    """
    assert lebenszyklus.FRIST_TAGE == 30


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


# -- Eine einzelne Sitzung ----------------------------------------------------------------
#
# Der kleine Weg neben dem großen (#171). Er ist genauso endgültig und trägt dieselben
# Tondateien fort, also gilt hier dasselbe wie oben: die Frage sagt vorher, was verschwindet,
# der Klick prüft Runde und Recht noch einmal, und was danebensteht, bleibt unberührt.


def sitzungsbefehl(bot, ctx):
    asyncio.run(bot.gruppen[gateway.GRUPPE_CHRONIK].befehle["sitzung-loeschen"](ctx))
    return ctx


def sitzung_waehlen(ctx, session_id, *, wer=LEITUNG):
    """Die Wahl im Menü — sie zeigt die Rückfrage und löscht noch nichts.

    Gewählt wird über das, was im Menü steht, und nicht über eine hier gebaute Nummer: was
    der Bot mitgibt, ist die Marke der Sitzung, und ein Test, der sie sich selbst zimmert,
    prüfte den Weg nicht mehr.
    """
    (menue,) = ctx.ansichten[-1].items
    menue.values = [_menuewert(menue, session_id)]
    klick = FakeInteraction(wer=wer)
    asyncio.run(menue.callback(klick))
    return klick


def _menuewert(menue, session_id):
    for eintrag in menue.options:
        if eintrag.value.startswith(f"{session_id}:"):
            return eintrag.value
    raise AssertionError(f"Sitzung {session_id} steht nicht im Menü.")


def sitzung_bestaetigen(klick, *, wer=LEITUNG, ja=True):
    knopf = klick.response.bearbeitet[-1]["view"].items[0 if ja else 1]
    zweiter = FakeInteraction(wer=wer)
    asyncio.run(knopf.callback(zweiter))
    return zweiter


def zweiter_abend(config, runde, marke):
    """Eine zweite Sitzung derselben Runde — die, die stehen bleiben muss."""
    sitzung = notes.create_session(runde, played_on="2026-05-08", title=f"Zweiter Abend {marke}")
    szene = notes.session(runde, sitzung).scenes[0]
    notes.add_note(runde, szene.id, f"Am zweiten Abend ging es um {marke}.")
    spur = config.recordings_dir / f"sitzung{sitzung}-20260508T200000-Mira.wav"
    spur.write_bytes(b"ton")
    recordings.enqueue(runde, sitzung, spur.name, discord_user_id=MIRA.id)
    return {"sitzung": sitzung, "spur": spur}


def test_der_knopf_loescht_die_sitzung_mit_ihren_tondateien(konfiguration, bot):
    """Der ganze Weg: Befehl, Wahl, Rückfrage, Knopf — und daneben bleibt alles stehen.

    Zwei Sorten Tondatei gehen mit: die eingereihte mit ihrer Zeile und die, die es nie
    wurde. Letztere liegt die ganze Sitzung über auf der Platte und wird erst am Ende
    eingereiht; ohne den Griff ins Verzeichnis bliebe sie liegen, und niemand fände sie je.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    bleibt = zweiter_abend(konfiguration, unsere, "alpha")
    verwaist = konfiguration.recordings_dir / f"sitzung{ids['sitzung']}-20260501T200000-Mira.wav"
    verwaist.write_bytes(b"ton")
    nachbar = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    daneben = fuellen(konfiguration, nachbar, "beta")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    fertig = sitzung_bestaetigen(sitzung_waehlen(ctx, ids["sitzung"]))

    assert "ist fort" in fertig.response.bearbeitet[-1]["content"]
    assert notes.session(unsere, ids["sitzung"]) is None
    assert not ids["spur"].exists() and not verwaist.exists()
    # Der zweite Abend derselben Runde und die Nachbarrunde merken nichts davon.
    assert notes.session(unsere, bleibt["sitzung"]).title == "Zweiter Abend alpha"
    assert bleibt["spur"].exists()
    assert notes.session(nachbar, daneben["sitzung"]).title == "Sitzung beta"
    assert daneben["spur"].exists()


def test_die_frage_sagt_dass_die_tondateien_mitgehen(konfiguration, bot):
    """Das ist der Satz, um den es geht: an einer Sitzung hängen Stimmen echter Menschen.

    Ob welche da sind, sieht man ihr von außen nicht an — also steht es in der Frage, mit
    Zahl, und daneben, was **nicht** mitgeht.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    klick = sitzung_waehlen(ctx, ids["sitzung"])

    frage = klick.response.bearbeitet[-1]["content"]
    assert "Sitzung alpha" in frage
    assert "1 Aufnahme" in frage and "1 Tondatei" in frage
    assert "1 Szene mit 1 Notiz" in frage
    assert "keine Sicherung" in frage
    assert "Nachweis" in frage
    # Und die Wahl allein hat noch nichts angefasst.
    assert notes.session(unsere, ids["sitzung"]) is not None
    assert ids["spur"].exists()


def test_die_frage_nennt_auch_den_ton_ohne_zeile(konfiguration, bot):
    """Datei da, Zeile noch nicht: der Mitschnitt läuft oder ist abgestürzt.

    Genau dafür sucht das Löschen auch im Verzeichnis — und was es mitnimmt, muss in der
    Rückfrage stehen. Sonst bestätigt die Gruppe eine Sitzung ohne Ton und bekommt hinterher
    gesagt, dass eine Stunde Stimmen von der Platte ist.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    sitzung = notes.create_session(unsere, played_on="2026-05-01", title="Sitzung alpha")
    laufend = konfiguration.recordings_dir / f"sitzung{sitzung}-20260501T200000-Mira.wav"
    laufend.write_bytes(b"ton")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    klick = sitzung_waehlen(ctx, sitzung)

    frage = klick.response.bearbeitet[-1]["content"]
    assert "1 Tondatei" in frage
    fertig = sitzung_bestaetigen(klick).response.bearbeitet[-1]["content"]
    # Und die Meldung danach zählt richtig **und** sagt es in gutem Deutsch.
    assert "Von der Platte gelöscht: 1 Tondatei." in fertig
    assert not laufend.exists()


def test_ohne_tondatei_verspricht_die_frage_keine(konfiguration, bot):
    """Nach der Aufbewahrungsfrist bleibt die Zeile und der Ton ist fort — das ist ein
    anderer Satz, und ihn zu verwechseln hieße, eine Löschung zu versprechen, die längst
    geschehen ist."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    ids["spur"].unlink()

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    frage = sitzung_waehlen(ctx, ids["sitzung"]).response.bearbeitet[-1]["content"]

    assert "1 Aufnahme" in frage
    assert "Tondateien liegen dazu keine mehr" in frage


def test_abbrechen_laesst_die_sitzung_stehen(konfiguration, bot):
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    zurueck = sitzung_bestaetigen(sitzung_waehlen(ctx, ids["sitzung"]), ja=False)

    assert zurueck.response.bearbeitet[-1]["content"] == chronik.SITZUNG_ABGEBROCHEN
    assert notes.session(unsere, ids["sitzung"]) is not None
    assert ids["spur"].exists()


def test_der_nachweis_der_ansage_ueberlebt_seine_sitzung(konfiguration, bot):
    """Anders als beim Löschen der ganzen Runde: hier gibt es die Runde weiter.

    Was belegt, dass im Sprachkanal angesagt wurde, ist genau dann etwas wert, wenn das
    daraus Gemachte noch irgendwo liegt — und das tut es, in Discord. Wer den Nachweis
    nicht mehr will, löscht die Runde; dort geht er mit.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    sitzung_bestaetigen(sitzung_waehlen(ctx, ids["sitzung"]))

    assert zeilen(konfiguration, "consent_event", unsere.id) == 1
    assert zeilen(konfiguration, "consent_member", unsere.id) == 1
    connection = db.connect(konfiguration.database_path)
    try:
        offen = connection.execute(
            "SELECT session_id FROM consent_event WHERE runde_id = ?", (unsere.id,)
        ).fetchone()
    finally:
        connection.close()
    assert offen["session_id"] is None


def indexzeilen(config, runde_id):
    connection = db.connect(config.database_path)
    try:
        return [
            (zeile["kind"], zeile["session_id"], zeile["text"])
            for zeile in connection.execute(
                "SELECT kind, session_id, text FROM search_index WHERE runde_id = ?", (runde_id,)
            )
        ]
    finally:
        connection.close()


def register_erwaehnen(runde, name, sitzungen):
    """Ein Registereintrag, der in mehreren Sitzungen vorkommt — bestätigt wie im Betrieb.

    Der Weg über ``decide`` und nicht über ein INSERT von Hand: erst die Bestätigung
    schreibt den Eintrag in den Suchindex, und **dabei** merkt er sich die erste Sitzung, in
    der er vorkommt. Genau die wird gleich gelöscht.
    """
    scope = db.scoped(runde)
    try:
        with scope:
            eintrag = int(
                scope.execute(
                    "INSERT INTO register_entry (runde_id, kind, name, description, state, "
                    "suggested_at) VALUES (?, 'ort', ?, 'Ein tiefer Keller', 'vorschlag', "
                    "'2026-05-01T21:00:00') RETURNING id",
                    (scope.runde_id, name),
                ).fetchone()["id"]
            )
            for sitzung in sitzungen:
                scope.execute(
                    "INSERT INTO register_mention (runde_id, entry_id, session_id, scene_id) "
                    "VALUES (?, ?, ?, NULL)",
                    (scope.runde_id, eintrag, sitzung),
                )
    finally:
        scope.close()
    register.decide(runde, {eintrag: register.Entscheidung(ja=True)})
    return eintrag


def _index_arten(config, runde_id):
    return [(art, wo) for art, wo, _text in indexzeilen(config, runde_id)]


def test_die_geloeschte_sitzung_steht_nicht_mehr_im_suchindex(konfiguration, bot):
    """Der Index hängt an keinem Fremdschlüssel — eine fts5-Tabelle kennt keinen —, und die
    Löschtrigger der Quelltabellen feuern bei einer Kaskade nicht.

    Dass `/suche` nichts mehr findet, beweist das **nicht**: sie verbindet über die Sitzung
    und liefe schon deshalb ins Leere. Stehen bliebe trotzdem der Text — Notizen und Diktate
    eines Abends, der der Gruppe als gelöscht gemeldet wurde. Geprüft wird deshalb die
    Tabelle selbst.

    Das Register geht **nicht** mit: es gehört der Runde und überlebt jeden Abend. Im Index
    steht bei ihm die erste Sitzung, in der er vorkommt — ist genau die fort, bekommt er
    seine nächste, sonst wäre er von da an nicht mehr zu finden.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    bleibt = zweiter_abend(konfiguration, unsere, "alpha")
    register_erwaehnen(unsere, "Der Keller alpha", (ids["sitzung"], bleibt["sitzung"]))
    assert ("register", ids["sitzung"]) in _index_arten(konfiguration, unsere.id)
    assert ("notiz", ids["sitzung"]) in _index_arten(konfiguration, unsere.id)

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    sitzung_bestaetigen(sitzung_waehlen(ctx, ids["sitzung"]))

    uebrig = indexzeilen(konfiguration, unsere.id)
    assert [wo for _art, wo, _text in uebrig if wo == ids["sitzung"]] == []
    assert not [text for _art, _wo, text in uebrig if "Im Keller stand alpha" in text]
    # Der Registereintrag ist auf den zweiten Abend gerückt und bleibt damit auffindbar.
    assert ("register", bleibt["sitzung"]) in _index_arten(konfiguration, unsere.id)
    assert search.find(unsere, "Keller").groups
    assert notes.session(unsere, bleibt["sitzung"]) is not None


def test_eine_sitzung_zu_loeschen_ist_kein_befehl_fuer_jedes_mitglied(konfiguration, bot):
    """Gerechnet wird an allen drei Stellen: sonst stellt die Leitung die Frage und
    irgendwer klickt sie weg."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde(), autor=MITGLIED))
    assert ctx.antworten == [einrichten.NUR_ADMIN]
    assert ctx.ansichten == [None]

    erlaubt = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    am_menue = sitzung_waehlen(erlaubt, ids["sitzung"], wer=MITGLIED)
    assert am_menue.response.bearbeitet[-1]["content"] == einrichten.NUR_ADMIN

    klick = sitzung_waehlen(erlaubt, ids["sitzung"])
    am_knopf = sitzung_bestaetigen(klick, wer=MITGLIED)
    assert am_knopf.response.bearbeitet[-1]["content"] == einrichten.NUR_ADMIN
    assert notes.session(unsere, ids["sitzung"]) is not None
    assert ids["spur"].exists()


def test_auch_die_verwaltung_loescht_keine_einzelne_sitzung(konfiguration, bot):
    """Die Schranke ist die der Administration — dieselbe wie vor der ganzen Runde.

    Sie ist nicht die harmlosere Löschung, sondern die lautlose: alles hier läuft ephemer,
    der Thread bleibt stehen, und wer geklickt hat, steht mit Absicht in keinem Log. Ein
    Verwalter nähme so genau den einen unbequemen Abend samt seinen Aufnahmen fort, und
    niemand in der Gruppe erführe es. Fällt die Heimlichkeit — ein sichtbarer Vermerk im
    Kanal —, darf diese Schranke wieder sinken; bis dahin fällt sie hier durch.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde(), autor=VERWALTUNG))
    assert ctx.antworten == [einrichten.NUR_ADMIN]
    assert ctx.ansichten == [None]

    erlaubt = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    am_menue = sitzung_waehlen(erlaubt, ids["sitzung"], wer=VERWALTUNG)
    assert am_menue.response.bearbeitet[-1]["content"] == einrichten.NUR_ADMIN

    am_knopf = sitzung_bestaetigen(sitzung_waehlen(erlaubt, ids["sitzung"]), wer=VERWALTUNG)
    assert am_knopf.response.bearbeitet[-1]["content"] == einrichten.NUR_ADMIN
    assert notes.session(unsere, ids["sitzung"]) is not None
    assert ids["spur"].exists()


def test_ein_altes_menue_loescht_in_der_frischen_runde_nichts(konfiguration, bot):
    """``runde.id`` wird nach einer Löschung neu vergeben, die Ansicht lebt länger — und
    eine Sitzungskennung von vorhin träfe drüben irgendeinen Abend."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))

    lebenszyklus.loeschen(konfiguration, unsere)
    nachbar = runden.anlegen(konfiguration.database_path, "Nachbarn", guild_id=NACHBARGILDE)
    daneben = fuellen(konfiguration, nachbar, "beta")
    assert nachbar.id == unsere.id
    klick = sitzung_waehlen(ctx, ids["sitzung"])

    assert klick.response.bearbeitet[-1]["content"] == chronik.VERALTET
    assert notes.session(nachbar, daneben["sitzung"]) is not None


def test_ein_zweiter_knopf_loescht_nicht_noch_einmal(konfiguration, bot):
    """Zwei offene Rückfragen auf dieselbe Sitzung — die zweite sagt es, statt ein »fort«
    über etwas zu setzen, das schon fort war."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    erste = sitzung_waehlen(sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde())), ids["sitzung"])
    zweite = sitzung_waehlen(sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde())), ids["sitzung"])

    sitzung_bestaetigen(erste)
    nochmal = sitzung_bestaetigen(zweite)

    assert nochmal.response.bearbeitet[-1]["content"] == chronik.SITZUNG_SCHON_FORT


def neu_unter_derselben_nummer(config, runde, alte_nummer):
    """Dieselbe Nummer, ein anderer Abend — der Fall, für den die Marke gebaut ist.

    ``session.id`` ist ein ``INTEGER PRIMARY KEY`` ohne ``AUTOINCREMENT``; SQLite vergibt
    die Nummer der zuletzt gelöschten Zeile wieder. Genau das passiert im Betrieb: die
    gewählte Sitzung wird anderswo gelöscht, ``/chronik start`` beginnt den nächsten Abend.
    """
    notes.delete_session(config, runde, notes.sitzungsmarke(notes.session(runde, alte_nummer)))
    neu = notes.create_session(runde, played_on="2026-05-08", title="Der echte Abend")
    assert neu == alte_nummer, "Der Fall trägt nur, wenn SQLite die Nummer wirklich neu vergibt."
    spur = config.recordings_dir / f"sitzung{neu}-20260508T200000-Mira.wav"
    spur.write_bytes(b"ton")
    return {"sitzung": neu, "spur": spur}


def test_der_knopf_nimmt_nicht_den_frisch_begonnenen_abend(konfiguration, bot):
    """Der Ablauf, für den dieser Befehl gebaut wurde — und in dem er am teuersten irrt.

    Zwischen Rückfrage und Klick liegt eine Viertelstunde. Wird die gewählte Sitzung in der
    Zeit anderswo gelöscht und danach eine neue begonnen, steht unter der Nummer von vorhin
    ein anderer Abend. Ein Klick, der nur die Nummer trägt, nähme ihn samt seinen Aufnahmen
    fort — und meldete ihn unter dem Titel des alten als gelöscht.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    klick = sitzung_waehlen(sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde())), ids["sitzung"])

    echt = neu_unter_derselben_nummer(konfiguration, unsere, ids["sitzung"])
    fertig = sitzung_bestaetigen(klick)

    assert fertig.response.bearbeitet[-1]["content"] == chronik.SITZUNG_SCHON_FORT
    assert notes.session(unsere, echt["sitzung"]).title == "Der echte Abend"
    assert echt["spur"].exists()


def test_das_menue_zeigt_nicht_den_abend_unter_der_alten_nummer(konfiguration, bot):
    """Dieselbe Viertelstunde eine Stufe früher: schon die Wahl darf nichts Fremdes zeigen.

    Sonst stünde in der Rückfrage der neue Abend mit seinen Aufnahmen, und wer sie gestellt
    hat, hielte ihn für den, den er gewählt hat.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))

    echt = neu_unter_derselben_nummer(konfiguration, unsere, ids["sitzung"])
    klick = sitzung_waehlen(ctx, ids["sitzung"])

    assert klick.response.bearbeitet[-1]["content"] == chronik.SITZUNG_SCHON_FORT
    assert notes.session(unsere, echt["sitzung"]) is not None
    assert echt["spur"].exists()


def test_die_tondatei_die_bleibt_wird_ohne_ihren_namen_gemeldet(
    konfiguration, bot, caplog, monkeypatch
):
    """Der Stamm eines Aufnahmenamens ist der Sprechername — er gehört in keine Logzeile.

    Ein ungefangenes ``unlink`` schriebe ihn über den Traceback dorthin. Und die Meldung
    danach darf nicht mehr versprechen, als geschehen ist: was liegen blieb, wird gesagt.
    """
    caplog.set_level(logging.INFO)
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    sitzung = notes.create_session(unsere, played_on="2026-05-01", title="Sitzung alpha")
    spur = konfiguration.recordings_dir / f"sitzung{sitzung}-20260501T200000-{MIRA.name}.wav"
    spur.write_bytes(b"ton")
    recordings.enqueue(unsere, sitzung, spur.name, discord_user_id=MIRA.id)
    echt = Path.unlink

    def sperrt(self, *rest, **weiteres):
        if self == spur:
            raise PermissionError(13, "Permission denied", str(self))
        return echt(self, *rest, **weiteres)

    klick = sitzung_waehlen(sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde())), sitzung)
    monkeypatch.setattr(Path, "unlink", sperrt)
    antwort = sitzung_bestaetigen(klick)

    # Zuerst das Log: ein ungefangener Fehlschlag käme über den Traceback des Klicks hier
    # heraus, und dann stünde der Name da, bevor irgendeine Meldung geprüft wird.
    assert MIRA.name not in caplog.text
    assert ".wav" not in caplog.text
    assert "PermissionError" in caplog.text
    fertig = antwort.response.bearbeitet[-1]["content"]
    assert "1 Tondatei" in fertig and "Nicht gelöscht" in fertig
    assert spur.exists()
    assert notes.session(unsere, sitzung) is None


def test_eine_tondatei_ausserhalb_des_verzeichnisses_wird_nicht_geloescht(konfiguration, bot):
    """Der Name kommt aus einer Spalte, gelöscht wird eine Datei — ein ``../`` darin zeigte
    aus dem Aufnahmeverzeichnis heraus.

    Heute schreibt jeder Aufrufer einen bloßen Namen hinein; an einer Stelle, die *löscht*,
    ist das keine Zusage, auf die man sich verlässt.
    """
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    sitzung = notes.create_session(unsere, played_on="2026-05-01", title="Sitzung alpha")
    fremd = konfiguration.recordings_dir.parent / "nicht-unsere.wav"
    fremd.write_bytes(b"ton")
    recordings.enqueue(unsere, sitzung, f"../{fremd.name}", discord_user_id=MIRA.id)

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    sitzung_bestaetigen(sitzung_waehlen(ctx, sitzung))

    assert fremd.exists()
    assert notes.session(unsere, sitzung) is None


def test_im_log_der_sitzungsloeschung_steht_kein_name_und_kein_titel(konfiguration, bot, caplog):
    """Der Titel kommt von der Gruppe und trägt oft genug einen Klarnamen; wer geklickt hat,
    geht das Log des Betreibers nichts an — anders als beim Löschen der ganzen Runde bleibt
    die Runde ja stehen, in der es stünde."""
    caplog.set_level(logging.INFO)
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    sitzung_bestaetigen(sitzung_waehlen(ctx, ids["sitzung"]))

    assert "Sitzung gelöscht" in caplog.text
    assert "Sitzung alpha" not in caplog.text
    assert LEITUNG.display_name not in caplog.text
    assert str(LEITUNG.id) not in caplog.text
    assert MIRA.name not in caplog.text


def test_ohne_sitzung_gibt_es_nichts_zu_waehlen(konfiguration, bot):
    runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))

    assert ctx.antworten == [chronik.SITZUNG_KEINE]
    assert ctx.ansichten == [None]


def test_das_menue_zeigt_die_juengsten_und_sagt_es(konfiguration, bot):
    """Discord nimmt 25 Zeilen. Die älteren wegzulassen ist unvermeidlich — sie
    stillschweigend wegzulassen wäre die Auskunft, es gebe sie nicht."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    for tag in range(1, chronik.SITZUNG_ZUR_WAHL + 3):
        notes.create_session(unsere, played_on=f"2026-05-{tag:02d}", title=f"Abend {tag}")

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))

    (menue,) = ctx.ansichten[-1].items
    assert len(menue.options) == chronik.SITZUNG_ZUR_WAHL
    assert menue.options[0].label.startswith("2026-05-27")
    assert f"{chronik.SITZUNG_ZUR_WAHL} jüngsten" in ctx.antworten[-1]


def test_die_sitzung_wird_neben_der_ereignisschleife_geloescht(konfiguration, bot, monkeypatch):
    """Tondateien und Zeilen einer langen Sitzung dauern; solange die Schleife rechnet,
    antwortet der Bot niemandem."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    faeden = []
    echt = notes.delete_session

    def merken(config, runde, session_id):
        faeden.append(threading.get_ident())
        return echt(config, runde, session_id)

    monkeypatch.setattr(notes, "delete_session", merken)

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))
    sitzung_bestaetigen(sitzung_waehlen(ctx, ids["sitzung"]))

    assert len(faeden) == 1
    assert threading.get_ident() not in faeden


def test_die_ruhende_runde_loescht_keine_einzelne_sitzung(konfiguration, bot):
    """Sie ist verabschiedet und wartet auf ihre Frist. Wer sie ganz loswerden will, hat
    dafür `/chronik loeschen` — dort ist eine ruhende ausdrücklich zugelassen."""
    unsere = runden.anlegen(konfiguration.database_path, GILDENAME, guild_id=GILDE)
    ids = fuellen(konfiguration, unsere, "alpha")
    austritt(bot, FakeGilde())

    ctx = sitzungsbefehl(bot, FakeCtx(gilde=FakeGilde()))

    assert (
        lebenszyklus.frist_datum(runden.get(konfiguration.database_path, unsere.id))
        in (ctx.antworten[-1])
    )
    assert ctx.ansichten == [None]
    assert ids["spur"].exists()


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
    menue, _quelle = menues(interaktion)

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
PAKET = Path(lebenszyklus.__file__).parent
# ``scripts/`` zählt im Repo als Quelle (``pyproject.toml``, coverage) und ist der
# natürliche Ort für ein Betreiber-Werkzeug — genau das, was hier nicht entstehen soll.
QUELLEN = (PAKET, PAKET.parents[1] / "scripts")
ZIEL = "chronicle.lebenszyklus"
ZERSTOEREND = frozenset({"loeschen", "sperren"})
NUR_DIE_GRUPPE = {"chronicle/bot/einrichten.py"}


def _paket_der_datei(datei: Path) -> str:
    if not datei.is_relative_to(PAKET):
        return ""
    return ".".join(("chronicle", *datei.relative_to(PAKET).parts[:-1]))


def _absolut(knoten: ast.ImportFrom, paket: str) -> str:
    """Auch ``from . import lebenszyklus`` meint dasselbe Modul wie der volle Pfad."""
    if not knoten.level:
        return knoten.module or ""
    teile = paket.split(".") if paket else []
    basis = teile[: len(teile) - (knoten.level - 1)] if knoten.level > 1 else teile
    return ".".join((*basis, knoten.module)) if knoten.module else ".".join(basis)


def _punktname(knoten: ast.AST) -> str | None:
    """``chronicle.lebenszyklus`` aus einer Attribut-Kette zurückgewinnen."""
    teile: list[str] = []
    while isinstance(knoten, ast.Attribute):
        teile.append(knoten.attr)
        knoten = knoten.value
    if not isinstance(knoten, ast.Name):
        return None
    teile.append(knoten.id)
    return ".".join(reversed(teile))


def _bindungen(baum: ast.AST, paket: str) -> tuple[set[str], set[str]]:
    """Unter welchen Namen diese Datei das Modul kennt — und die Verben direkt."""
    module = {ZIEL}
    verben: set[str] = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            for name in knoten.names:
                if name.name == ZIEL and name.asname:
                    module.add(name.asname)
        elif isinstance(knoten, ast.ImportFrom):
            woher = _absolut(knoten, paket)
            for name in knoten.names:
                if woher == "chronicle" and name.name == "lebenszyklus":
                    module.add(name.asname or name.name)
                elif woher == ZIEL and name.name in ZERSTOEREND:
                    verben.add(name.asname or name.name)
    return module, verben


def _greift_zu(baum: ast.AST, module: set[str], verben: set[str]) -> bool:
    """Jede *Erwähnung* zählt, nicht nur der Aufruf.

    Wer ``_weg = lebenszyklus.loeschen`` schreibt und später ``_weg(…)`` ruft, hat
    denselben Weg gebaut — die Zuweisung ist die Stelle, an der er entsteht.
    """
    for knoten in ast.walk(baum):
        if (
            isinstance(knoten, ast.Attribute)
            and knoten.attr in ZERSTOEREND
            and _punktname(knoten.value) in module
        ):
            return True
        if isinstance(knoten, ast.Name) and knoten.id in verben:
            return True
        # ``getattr`` umgeht jede Namensprüfung — solange der Name dasteht, nicht diese.
        if (
            isinstance(knoten, ast.Call)
            and isinstance(knoten.func, ast.Name)
            and knoten.func.id == "getattr"
            and len(knoten.args) >= 2
            and _punktname(knoten.args[0]) in module
            and isinstance(knoten.args[1], ast.Constant)
            and knoten.args[1].value in ZERSTOEREND
        ):
            return True
    return False


def _aufrufer() -> set[str]:
    gefunden: set[str] = set()
    for wurzel in QUELLEN:
        if not wurzel.exists():
            continue
        for datei in sorted(wurzel.rglob("*.py")):
            if datei.samefile(lebenszyklus.__file__):
                continue
            baum = ast.parse(datei.read_text(encoding="utf-8"))
            paket = _paket_der_datei(datei)
            module, verben = _bindungen(baum, paket)
            if _greift_zu(baum, module, verben):
                gefunden.add(datei.relative_to(PAKET.parent).as_posix())
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
