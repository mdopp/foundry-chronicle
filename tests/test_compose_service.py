"""Vom Speicher bis zum abgelegten Protokoll — der Stapellauf am Stück."""

import pytest
import requests
from conftest import UNSER_KONTO, deutsche_runde, laufender_job, runde

import chronicle.compose.__main__ as entry
from chronicle import db, jobs, lebenszyklus, settings
from chronicle import runde as runden
from chronicle import sprache as sprachen
from chronicle.compose import client as ollama
from chronicle.compose.service import KIND, RUECKBLICK, compose_session, recap_session
from chronicle.discord import rueckblick
from chronicle.foundry import store
from chronicle.foundry.model import NICHT_MEHR_VORHANDEN, WorldSnapshot
from chronicle.foundry.world import project

# Das Material dieser Datei ist deutsch; seit #268 folgt der Text der Sprache seiner Runde
# (Vorgabe Englisch). Geprüft wird hier deshalb gegen die deutschen Texte.
_CHRONIK = sprachen.chronik(sprachen.DEUTSCH)
_RUECKBLICK = sprachen.rueckblick(sprachen.DEUTSCH)
_ERZAEHLUNG = sprachen.erzaehlung(sprachen.DEUTSCH)

VERBINDUNG_TITEL = _CHRONIK.verbindung_titel

STAND = "2026-08-05T20:00:00+00:00"
SPAETER = "2026-08-06T20:00:00+00:00"


class Modell:
    def __init__(
        self,
        antwort="Die Runde tastet sich voran.",
        faeden="- Die Wirtin wartet auf Antwort.",
        name="chronist-test",
    ):
        self.name = name
        self.antwort = antwort
        self.faeden = faeden
        self.prompts = []

    def write(self, *, system, prompt):
        self.prompts.append(prompt)
        return self.faeden if "Fäden" in system else self.antwort


@pytest.fixture
def scope(config):
    zugang = db.scoped(deutsche_runde(config))
    yield zugang
    zugang.close()


def sitzung(scope, *, title="Der Keller", played_on="2026-08-05"):
    zeiger = scope.execute(
        "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
        (scope.runde_id, played_on, title, STAND),
    )
    return zeiger.lastrowid


def szene(scope, sitzung_id, *, position=1, title="Aufbruch"):
    zeiger = scope.execute(
        "INSERT INTO scene (runde_id, session_id, position, title, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, sitzung_id, position, title, STAND),
    )
    return zeiger.lastrowid


def notiz(scope, szene_id, text):
    scope.execute(
        "INSERT INTO note (runde_id, scene_id, text, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (scope.runde_id, szene_id, text, STAND, STAND),
    )


def fakt(scope, szene_id, message_id="m-wurf"):
    scope.execute(
        "INSERT INTO scene_foundry_message (runde_id, scene_id, message_id) VALUES (?, ?, ?)",
        (scope.runde_id, szene_id, message_id),
    )


def _gilde_setzen(config, guild_id):
    connection = db.connect(config.database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE runde SET guild_id = ? WHERE id = ?", (guild_id, runde(config).id)
            )
    finally:
        connection.close()


def protokolle(scope, sitzung_id):
    return scope.execute(
        "SELECT kind, text, created_at FROM protocol WHERE runde_id = ? AND session_id = ?",
        (scope.runde_id, sitzung_id),
    ).fetchall()


def eine_runde(scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(scope)
    erste = szene(scope, sitzung_id, position=1, title="Aufbruch")
    zweite = szene(scope, sitzung_id, position=2, title="Der Keller")
    notiz(scope, erste, "Wir brechen bei Sonnenaufgang auf.")
    notiz(scope, zweite, "Brok prüft, was er über den Keller weiß.")
    fakt(scope, zweite)
    scope.commit()
    return sitzung_id


def test_die_foundry_zahl_landet_unveraendert_im_protokoll(config, scope, welt):
    sitzung_id = eine_runde(scope, welt)

    ergebnis = compose_session(config, runde(config), sitzung_id, model=Modell())

    assert ergebnis.fact_count == 1
    zeilen = protokolle(scope, sitzung_id)
    assert [z["kind"] for z in zeilen] == [KIND]
    text = zeilen[0]["text"]
    assert text == ergebnis.text
    assert "Knowledge Roll: Summe 7" in text
    assert "hope d12 = 3 · fear d12 = 1" in text
    assert "Wir brechen bei Sonnenaufgang auf." in text
    assert VERBINDUNG_TITEL in text


def test_die_zuordnung_ueberlebt_ein_geleertes_chat_log(config, scope, welt):
    """#61: der Wurf ist in Foundry weg, die Szene behält ihn — mit Herkunftsvermerk."""
    sitzung_id = eine_runde(scope, welt)
    store.save(scope, WorldSnapshot(system="daggerheart", fetched_at=SPAETER))
    scope.commit()

    ergebnis = compose_session(config, runde(config), sitzung_id, model=Modell())

    assert ergebnis.fact_count == 1
    assert "Knowledge Roll: Summe 7" in ergebnis.text
    assert NICHT_MEHR_VORHANDEN in ergebnis.text
    assert SPAETER not in ergebnis.text


def test_ein_zweiter_lauf_ersetzt_die_chronik(config, scope, welt):
    sitzung_id = eine_runde(scope, welt)
    compose_session(config, runde(config), sitzung_id, model=Modell("Erster Anlauf."))

    compose_session(config, runde(config), sitzung_id, model=Modell("Zweiter Anlauf."))

    zeilen = protokolle(scope, sitzung_id)
    assert len(zeilen) == 1
    assert "Zweiter Anlauf." in zeilen[0]["text"]
    assert "Erster Anlauf." not in zeilen[0]["text"]


def test_ohne_sprachmodell_entsteht_die_chronik_trotzdem(config, scope, welt):
    sitzung_id = eine_runde(scope, welt)

    ergebnis = compose_session(config, runde(config), sitzung_id)

    assert not config.ollama_configured
    assert ergebnis.reason is not None
    assert VERBINDUNG_TITEL not in ergebnis.text
    assert "Knowledge Roll: Summe 7" in ergebnis.text
    assert protokolle(scope, sitzung_id)[0]["text"] == ergebnis.text


def test_ohne_zuordnung_traegt_die_chronik_allein_die_notizen(config, scope, welt):
    store.save(scope, project(welt, UNSER_KONTO, fetched_at=STAND))
    sitzung_id = sitzung(scope)
    notiz(scope, szene(scope, sitzung_id), "Wir reden mit der Wirtin.")
    scope.commit()

    ergebnis = compose_session(config, runde(config), sitzung_id, model=Modell())

    assert ergebnis.fact_count == 0
    assert "Wir reden mit der Wirtin." in ergebnis.text
    assert "Belegt aus Foundry" not in ergebnis.text


def test_eine_unbekannte_sitzung_gibt_es_nicht(config, scope):
    assert compose_session(config, runde(config), 999) is None


def test_jede_szene_bekommt_ihren_eigenen_aufruf(config, scope, welt):
    sitzung_id = eine_runde(scope, welt)
    modell = Modell()

    compose_session(config, runde(config), sitzung_id, model=modell)

    assert len(modell.prompts) == 2
    assert "Belegte Fakten aus Foundry" in modell.prompts[1]
    assert "Belegte Fakten aus Foundry" not in modell.prompts[0]


def test_der_rueckblick_liegt_neben_der_chronik(config, scope, welt):
    sitzung_id = eine_runde(scope, welt)
    compose_session(config, runde(config), sitzung_id, model=Modell())

    ergebnis = recap_session(config, runde(config), sitzung_id, model=Modell())

    zeilen = protokolle(scope, sitzung_id)
    assert {z["kind"] for z in zeilen} == {KIND, RUECKBLICK}
    abgelegt = next(z["text"] for z in zeilen if z["kind"] == RUECKBLICK)
    assert abgelegt == ergebnis.text
    assert ergebnis.scene_count == 2
    # Die Zahl steht unverändert in der Chronik und wird von dort übernommen.
    assert "Knowledge Roll: Summe 7" in abgelegt


def test_ohne_chronik_gibt_es_keinen_rueckblick(config, scope, welt):
    sitzung_id = eine_runde(scope, welt)

    assert recap_session(config, runde(config), sitzung_id, model=Modell()) is None
    assert protokolle(scope, sitzung_id) == []


def test_ein_zweiter_lauf_ersetzt_den_rueckblick(config, scope, welt):
    sitzung_id = eine_runde(scope, welt)
    compose_session(config, runde(config), sitzung_id, model=Modell())
    recap_session(config, runde(config), sitzung_id, model=Modell("Erster Anlauf."))

    recap_session(config, runde(config), sitzung_id, model=Modell("Zweiter Anlauf."))

    zeilen = [z for z in protokolle(scope, sitzung_id) if z["kind"] == RUECKBLICK]
    assert len(zeilen) == 1
    assert "Zweiter Anlauf." in zeilen[0]["text"]
    assert "Erster Anlauf." not in zeilen[0]["text"]


def test_der_rueckblick_bekommt_die_vorigen_rueckblicke_mit(config, scope, welt):
    alt = eine_runde(scope, welt)
    compose_session(config, runde(config), alt, model=Modell())
    recap_session(config, runde(config), alt, model=Modell("Der Hafen lag hinter uns."))
    neu = sitzung(scope, title="Der Turm", played_on="2026-08-12")
    notiz(scope, szene(scope, neu), "Wir steigen zum Turm.")
    scope.commit()
    compose_session(config, runde(config), neu, model=Modell())
    modell = Modell()

    recap_session(config, runde(config), neu, model=modell)

    assert "Der Hafen lag hinter uns." in modell.prompts[0]
    assert "Wir steigen zum Turm." in modell.prompts[0]


def test_der_stapelaufruf_meldet_die_betriebsart(config, scope, welt, monkeypatch, capsys):
    sitzung_id = eine_runde(scope, welt)
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    assert entry.main([str(sitzung_id)]) == 1
    assert "geordnet, nicht erzählt" in capsys.readouterr().out


def test_der_stapelaufruf_schreibt_beides(config, scope, welt, monkeypatch, capsys):
    sitzung_id = eine_runde(scope, welt)
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    entry.main([str(sitzung_id)])

    ausgabe = capsys.readouterr().out
    assert "Chronik aus" in ausgabe
    assert "Rückblick aus" in ausgabe
    assert {z["kind"] for z in protokolle(scope, sitzung_id)} == {KIND, RUECKBLICK}


def test_der_stapelaufruf_stellt_den_rueckblick_genau_einmal_zu(
    config, scope, welt, monkeypatch, capsys
):
    sitzung_id = eine_runde(scope, welt)
    settings.save(runde(config), {"discord_recap_channel": "chronik"})
    # Der Stapelaufruf nimmt die erste Runde; die trägt seit dem Runden-Modell eine Gilde,
    # und ohne sie wüsste die Zustellung nicht, in welcher sie den Kanal suchen soll.
    _gilde_setzen(config, "g-runde")
    gepostet = []

    class Briefkasten:
        def guild_channel_id(self, gilde, kanal):
            return f"{gilde}/{kanal}"

        def post_embed(self, kanal, eingebettet):
            gepostet.append((kanal, eingebettet))

    monkeypatch.setattr(rueckblick, "DiscordClient", lambda zugang: Briefkasten())
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    # Der Token kommt seit #230 aus der Umgebung und nirgends sonst her.
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token-nur-fuer-den-test")
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    entry.main([str(sitzung_id)])
    assert "delivered" in capsys.readouterr().out

    # Ein zweiter Lauf komponiert neu — gepostet wird deshalb nicht noch einmal.
    entry.main([str(sitzung_id)])

    zeilen = protokolle(scope, sitzung_id)
    abgelegt = next(z["text"] for z in zeilen if z["kind"] == RUECKBLICK)
    assert gepostet == [("g-runde/chronik", rueckblick.embed(abgelegt))]
    assert "had already been delivered" in capsys.readouterr().out


def test_der_stapelaufruf_weist_falsche_argumente_ab(config, monkeypatch, capsys):
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    assert entry.main([]) == 2
    assert entry.main(["keine-zahl"]) == 2
    assert entry.main(["1", "keine-zahl"]) == 2
    assert entry.main(["1", "2", "3"]) == 2
    assert entry.main(["999"]) == 2
    assert entry.main(["999", "4711"]) == 2
    ausgabe = capsys.readouterr().out
    assert "no round" in ausgabe.casefold()
    assert "4711" in ausgabe


def _zweite_runde(config, welt):
    """Eine zweite Runde mit eigener Sitzung — dieselbe Instanz, andere Kampagne."""
    zweite = runden.anlegen(config.database_path, "Zanarkand", guild_id="g-zweite")
    fremd = db.scoped(zweite)
    try:
        return zweite, eine_runde(fremd, welt)
    finally:
        fremd.close()


def _fremde_protokolle(runde_der_anderen, sitzung_id):
    fremd = db.scoped(runde_der_anderen)
    try:
        return protokolle(fremd, sitzung_id)
    finally:
        fremd.close()


def _stapel_umgebung(config, monkeypatch):
    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)


def test_der_stapelaufruf_erreicht_die_sitzung_der_zweiten_runde(
    config, scope, welt, monkeypatch, capsys
):
    """#245: genannt wird die Runde, und dann ist auch die zweite erreichbar."""
    zweite, sitzung_id = _zweite_runde(config, welt)
    _stapel_umgebung(config, monkeypatch)

    assert entry.main([str(sitzung_id), str(zweite.id)]) == 1

    # Die zweite Runde ist frisch und damit englisch — die Vorgabe seit #268.
    assert sprachen.chronik(sprachen.ENGLISCH).fertig.split()[0] in capsys.readouterr().out
    assert {z["kind"] for z in _fremde_protokolle(zweite, sitzung_id)} == {KIND, RUECKBLICK}


def test_der_stapelaufruf_verlangt_bei_mehreren_runden_eine_wahl(
    config, scope, welt, monkeypatch, capsys
):
    """Ohne genannte Runde wird nicht geraten — auch nicht über die eindeutige Sitzungs-Id.

    Die stillschweigende erste Runde war der Fehler aus #245; sie durch ein Suchen über
    alle Runden zu ersetzen wäre der teurere: ein Aufruf käme dann an eine Kampagne, die
    niemand benannt hat.
    """
    zweite, sitzung_id = _zweite_runde(config, welt)
    _stapel_umgebung(config, monkeypatch)

    assert entry.main([str(sitzung_id)]) == 2

    ausgabe = capsys.readouterr().out
    assert "Zanarkand" in ausgabe
    assert str(zweite.id) in ausgabe
    assert _fremde_protokolle(zweite, sitzung_id) == []


def test_der_stapelaufruf_greift_nicht_in_die_nicht_genannte_runde(
    config, scope, welt, monkeypatch, capsys
):
    """Genannt wird Runde eins, gemeint ist eine Sitzung von nebenan — und nichts geschieht."""
    zweite, sitzung_id = _zweite_runde(config, welt)
    _stapel_umgebung(config, monkeypatch)

    assert entry.main([str(sitzung_id), str(runde(config).id)]) == 2

    assert "no longer exists" in capsys.readouterr().out
    assert protokolle(scope, sitzung_id) == []
    assert _fremde_protokolle(zweite, sitzung_id) == []


# --- Was der Stapellauf hinterlässt: eine Zeile und eine freie Karte -------------------


class Antwort:
    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": "Die Runde tastet sich voran."}}


class Ollama:
    """Ein Ollama am Draht — was der Stapellauf wirklich hinausschickt, steht in ``rumpf``."""

    def __init__(self, fehler=None):
        self.rumpf = []
        self._fehler = fehler

    def post(self, url, **kwargs):
        self.rumpf.append(kwargs["json"])
        if self._fehler is not None:
            raise self._fehler
        return Antwort()


@pytest.fixture
def am_draht(config, monkeypatch):
    """Der Stapellauf mit hinterlegtem Modell und einer Gegenstelle auf dem Draht."""

    def gegenstelle(fehler=None):
        eines = Ollama(fehler)
        # An ``requests.Session`` und nicht an ``_http_session``: dessen Funktionsobjekt
        # steckt als Vorgabewert in den Signaturen und lässt sich nicht mehr austauschen.
        monkeypatch.setattr(ollama.requests, "Session", lambda: eines)
        return eines

    monkeypatch.setenv("CHRONICLE_DATA_DIR", str(config.data_dir))
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.example:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "chronist-test")
    return gegenstelle


def test_der_stapelaufruf_gibt_die_modellhaltung_hinterher_wieder_her(
    config, scope, welt, am_draht
):
    """#300: der Aufschrieb neben ``/session done`` besetzte das große Modell bis zur Frist.

    Freigegeben wurde bis dahin allein in ``jobs.abschluss``; dieser Weg geht dort nicht
    vorbei. Der Beleg steht auf dem Draht und nicht in einer Merkliste: die letzte Anfrage
    an Ollama trägt ``keep_alive: 0``.
    """
    sitzung_id = eine_runde(scope, welt)
    gegenstelle = am_draht()

    entry.main([str(sitzung_id)])

    assert gegenstelle.rumpf[-1]["keep_alive"] == ollama.FREIGABE
    assert ollama.FREIGABE == 0


def test_der_stapelaufruf_hinterlaesst_eine_zeile_zu_seiner_sitzung(config, scope, welt, am_draht):
    """#301: bis dahin wusste der Dienst hinterher nicht, dass dieser Lauf stattfand."""
    sitzung_id = eine_runde(scope, welt)
    am_draht()

    assert entry.main([str(sitzung_id)]) == 0

    zeile = jobs.latest(runde(config), jobs.CHRONIK, sitzung_id)
    assert zeile is not None
    assert zeile.session_id == sitzung_id
    assert zeile.fertig
    assert zeile.result


def test_ein_gescheiterter_stapelaufruf_steht_ebenfalls_in_der_zeile(
    config, scope, welt, am_draht, capsys
):
    """Der Fehlschlag ist der Fall, um den es ging — er verschwand am 22.08. spurlos.

    Die Sitzung gibt es, die Runde ruht seit dem Rauswurf: aus der Kette kommt nichts
    zurück. Vorher endete das in einer gedruckten Zeile und sonst nirgends.
    """
    sitzung_id = eine_runde(scope, welt)
    _gilde_setzen(config, "g-ruhend")
    lebenszyklus.sperren(config.database_path, "g-ruhend")
    am_draht()

    assert entry.main([str(sitzung_id)]) == 2

    zeile = jobs.latest(runde(config), jobs.CHRONIK, sitzung_id)
    assert zeile is not None
    assert zeile.gescheitert
    assert lebenszyklus.RUHT == zeile.error
    assert lebenszyklus.RUHT in capsys.readouterr().out


def test_ein_modell_an_der_zeitgrenze_verschwindet_nicht_mehr_spurlos(
    config, scope, welt, am_draht
):
    """Genau der Lauf vom 22.08.: Ollama antwortet nicht, und niemand erfuhr davon.

    Die Chronik entsteht trotzdem — geordnet statt formuliert —, und **das** steht jetzt
    in der Zeile. Vorher stand dort nichts, und wiederholt wurde der Lauf erst einen Tag
    später.
    """
    sitzung_id = eine_runde(scope, welt)
    am_draht(requests.ReadTimeout("zu lange"))

    assert entry.main([str(sitzung_id)]) == 1

    zeile = jobs.latest(runde(config), jobs.CHRONIK, sitzung_id)
    assert zeile is not None and zeile.fertig
    assert "without a language model" in zeile.result


def test_der_stapelaufruf_beginnt_nicht_neben_einem_laufenden_lauf(
    config, scope, welt, am_draht, capsys
):
    """Eine CPU, ein Ollama — dieselbe Schranke wie hinter jedem Knopf, jetzt auch hier."""
    sitzung_id = eine_runde(scope, welt)
    am_draht()
    laufender_job(config, kind=jobs.ABGLEICH)

    assert entry.main([str(sitzung_id)]) == 2

    assert "already going" in capsys.readouterr().out
    assert protokolle(scope, sitzung_id) == []
