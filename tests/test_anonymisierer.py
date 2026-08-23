"""Der Anonymisierer und seine Selbstprüfung.

Die Eingabe hier ist **erfunden** — sie hat nur die Form eines Weltabzugs. Ein echter
trägt die Klarnamen aller Beteiligten und gehört nie ins Repo; genau deshalb gibt es das
Skript, das hier geprüft wird.

Die wichtigste Prüfung ist die letzte: das Skript darf nicht bloß ersetzen, es muss
merken, wenn das Ersetzen misslungen ist. Ein stiller Rest wäre schlimmer als kein
Skript, weil ihn danach niemand mehr sucht.
"""

from __future__ import annotations

import json
import random
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import anonymisiere_welt as anonym  # noqa: E402

LEITUNG_NAME = "Spielleitung Talmar"
FIGUR_NAME = "Nuala Abendrot"
ALIAS = "Nuala"
KONTO_NAME = "Chronistin Wolke"
SZENE_NAME = "Der Schankraum"
SZENE_NAV = "Schankstube"
JOURNAL_NAME = "Was Talmar verschweigt"

# Der erste Anlauf des Skripts vergab hübsche Fantasienamen — und stolperte über eine
# Figur, deren Name in einem davon steckte. Diese Figur hält den Fall fest: sie heißt wie
# die erste Silbe des Pseudonymraums.
STOLPERFIGUR = "Baba"


def kennung(rng: random.Random) -> str:
    return "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(16))


@pytest.fixture
def roh() -> dict:
    rng = random.Random(58)
    konto, leitung, spielerin = kennung(rng), kennung(rng), kennung(rng)
    figur, wirtin, wurm = kennung(rng), kennung(rng), kennung(rng)
    return {
        "userId": konto,
        "world": {
            "id": "halber-mond",
            "title": "Chronik vom Halben Mond",
            "coreVersion": "13.341",
            "description": f"Eine Kampagne von {LEITUNG_NAME}.",
            "authors": [{"name": LEITUNG_NAME}],
        },
        "system": {"id": "daggerheart", "title": "Daggerheart", "version": "1.2", "packs": [1, 2]},
        "users": [
            {"_id": konto, "name": KONTO_NAME, "role": 1, "character": figur, "avatar": "/a.png"},
            {"_id": leitung, "name": LEITUNG_NAME, "role": 4, "hotbar": {}},
            {"_id": spielerin, "name": "Taru", "role": 1},
        ],
        "actors": [
            {
                "_id": figur,
                "name": FIGUR_NAME,
                "type": "character",
                "ownership": {"default": 2, spielerin: 3},
                "system": {"biography": f"Aufgewachsen bei {KONTO_NAME}."},
                "items": [{"name": "Schwert"}],
            },
            {"_id": wirtin, "name": STOLPERFIGUR, "type": "npc", "ownership": {"default": 1}},
            {"_id": wurm, "name": "Der Wurm", "type": "npc", "ownership": {"default": 0}},
        ],
        "messages": [
            {
                "_id": kennung(rng),
                "timestamp": 1000,
                "author": spielerin,
                "content": f"{ALIAS} tritt ein.",
                "speaker": {"scene": "s1", "actor": figur, "token": "t1", "alias": ALIAS},
                "whisper": [],
                "blind": False,
                "flavor": f"{LEITUNG_NAME} wirft",
                "rolls": [
                    json.dumps(
                        {
                            "class": "DualityRoll",
                            "formula": "1d12 + 1d12 + 2",
                            "total": 14,
                            "terms": [{"faces": 12, "results": [{"result": 9}]}],
                            "options": {
                                "title": "Agility",
                                "roll": {
                                    "total": 14,
                                    "isCritical": False,
                                    "modifierTotal": 2,
                                    "hope": {"dice": "d12", "value": 9},
                                    "fear": {"dice": "d12", "value": 3},
                                },
                                "data": {"notiz": KONTO_NAME},
                            },
                        }
                    )
                ],
            },
            {
                "_id": kennung(rng),
                "timestamp": 2000,
                "author": leitung,
                "content": "",
                "whisper": [leitung],
                "blind": True,
                "speaker": {"actor": wurm},
                "system": {"roll": {"title": "Verborgen", "total": 20}, "beschreibung": "geheim"},
            },
        ],
        "journal": [{"_id": "j1", "name": JOURNAL_NAME, "pages": [{"text": LEITUNG_NAME}]}],
        "folders": [{"_id": "f1", "name": "Charaktere"}],
        "macros": [{"_id": "k1", "name": "Wurf"}],
        "scenes": [
            {
                "_id": "s1",
                "name": SZENE_NAME,
                "navName": SZENE_NAV,
                "active": True,
                "ownership": {"default": 0},
                "walls": [1],
                "tokens": [{"name": FIGUR_NAME}],
            }
        ],
    }


def sauber(roh: dict) -> dict:
    return anonym.anonymisiere(roh)


def texte(wert) -> list[str]:
    return [text for _pfad, text in anonym._zeichenketten(wert)]


def test_kein_name_aus_der_eingabe_ueberlebt(roh):
    ersatz = anonym.Ersatz(roh)
    assert anonym.pruefe(anonym.anonymisiere(roh, ersatz), ersatz.gefahren) == []


def test_der_pseudonymraum_beruehrt_die_eingabe_nicht(roh):
    """Der Fall, an dem die Selbstprüfung den Anonymisierer selbst überführt hat."""
    ersatz = anonym.Ersatz(roh)
    assert STOLPERFIGUR in ersatz.zuordnung
    for pseudo in ersatz.zuordnung.values():
        assert STOLPERFIGUR.casefold() not in pseudo.casefold()


def test_die_zuordnung_ist_im_lauf_stabil(roh):
    ergebnis = sauber(roh)
    figur = next(f for f in ergebnis["actors"] if f["_id"] == ergebnis["users"][0]["character"])
    # Der Alias ist der Vorname derselben Figur — und bleibt es auch danach.
    assert ergebnis["messages"][0]["speaker"]["alias"] == figur["name"]
    assert json.dumps(sauber(roh), sort_keys=True) == json.dumps(ergebnis, sort_keys=True)


def test_die_struktur_bleibt_stehen(roh):
    ergebnis = sauber(roh)
    assert ergebnis["world"]["id"] == "halber-mond"
    assert ergebnis["system"]["id"] == "daggerheart"
    assert [b["role"] for b in ergebnis["users"]] == [1, 4, 1]
    assert [f["type"] for f in ergebnis["actors"]] == ["character", "npc", "npc"]
    assert [f["ownership"] for f in ergebnis["actors"]] == [f["ownership"] for f in roh["actors"]]
    assert [b["_id"] for b in ergebnis["users"]] == [b["_id"] for b in roh["users"]]
    zweite = ergebnis["messages"][1]
    assert zweite["blind"] is True
    assert zweite["whisper"] == roh["messages"][1]["whisper"]
    assert zweite["system"] == {"roll": {"title": "Verborgen", "total": 20}}


def test_die_zeitstempel_verlieren_ihren_ursprung_und_behalten_ihre_abstaende(roh):
    """#255: der Weltabzug behielt das Datum, der Mitschnitt warf den Zeitpunkt weg.

    Jetzt tun beide dasselbe. Ersatzlos streichen wäre die dritte Antwort gewesen und die
    schlechteste: die Szenenzuordnung rechnet mit den Abständen, und ohne sie prüfte
    ``tests/test_foundry_echtwelt.py`` seine eigene Vorlage statt der Zuordnung.
    """
    stempel = [nachricht["timestamp"] for nachricht in sauber(roh)["messages"]]
    assert stempel == [0, 1000]
    roher_abstand = roh["messages"][1]["timestamp"] - roh["messages"][0]["timestamp"]
    assert stempel[1] - stempel[0] == roher_abstand


def test_ein_unlesbarer_zeitstempel_faellt_weg_statt_unverschoben_dazubleiben(roh):
    """Was das Skript nicht verschieben kann, darf es auch nicht veröffentlichen."""
    roh["messages"][0]["timestamp"] = "irgendwann am Dienstag"
    ergebnis = sauber(roh)
    assert "timestamp" not in ergebnis["messages"][0]
    # Der Ursprung kommt dann von der Nachricht, die noch einen lesbaren trägt.
    assert ergebnis["messages"][1]["timestamp"] == 0


def test_die_zahlen_eines_wurfs_bleiben_unangetastet(roh):
    wurf = json.loads(sauber(roh)["messages"][0]["rolls"][0])
    assert wurf["total"] == 14
    assert wurf["formula"] == "1d12 + 1d12 + 2"
    assert wurf["terms"] == [{"faces": 12, "results": [{"result": 9}]}]
    assert wurf["options"]["roll"]["hope"] == {"dice": "d12", "value": 9}
    assert wurf["options"]["roll"]["fear"] == {"dice": "d12", "value": 3}
    # Alles neben der Rechnung ist Freitext und fällt weg — auch wenn es harmlos aussieht.
    assert "data" not in wurf["options"]


def test_freitext_faellt_weg(roh):
    ergebnis = sauber(roh)
    assert ergebnis["messages"][0]["content"] == anonym.ERSATZTEXT
    assert ergebnis["messages"][1]["content"] == ""
    assert "flavor" not in ergebnis["messages"][0]
    assert "journal" not in ergebnis and "folders" not in ergebnis and "macros" not in ergebnis
    assert "system" not in ergebnis["actors"][0] and "items" not in ergebnis["actors"][0]
    assert "description" not in ergebnis["world"] and "authors" not in ergebnis["world"]
    assert JOURNAL_NAME not in " ".join(texte(ergebnis))


def test_szenen_behalten_ihre_kennung_und_verlieren_ihren_namen(roh):
    szene = sauber(roh)["scenes"][0]
    assert szene["_id"] == "s1"
    assert szene["name"] != SZENE_NAME
    # Auch der Name in der Kartenleiste ist ein Name — und das Karten-Innenleben bleibt
    # ganz draußen: es sagt nichts über den Abend und kann Freitext tragen.
    assert szene["navName"] not in ("", SZENE_NAV)
    assert "walls" not in szene and "tokens" not in szene


def test_szenen_behalten_was_der_adapter_liest(roh):
    """``active`` und ein leeres ``navName`` — daran entscheidet der Adapter den Ortsnamen."""
    assert sauber(roh)["scenes"][0]["active"] is True

    roh["scenes"][0]["navName"] = ""
    assert sauber(roh)["scenes"][0]["navName"] == ""

    del roh["scenes"][0]["navName"]
    assert "navName" not in sauber(roh)["scenes"][0]


def test_ein_misslungenes_ersetzen_bricht_den_lauf_ab(roh, tmp_path, monkeypatch):
    """Die Prüfung ist das Dauergate — sie muss auch einen kaputten Ersetzer fangen."""
    monkeypatch.setattr(anonym.Ersatz, "text", lambda self, original: str(original or ""))
    monkeypatch.setattr(anonym.Ersatz, "name", lambda self, original: str(original or ""))
    eingabe = tmp_path / "roh.json"
    eingabe.write_text(json.dumps(roh), encoding="utf-8")
    ziel = tmp_path / "sauber.json"
    with pytest.raises(anonym.Anonymisierung) as fehler:
        anonym.lauf(eingabe, ziel)
    assert not ziel.exists()
    # Der Name selbst steht nicht in der Meldung: sie landet leicht in einem Log. Auch
    # nicht in Bruchstücken — die frühere Fassung nannte die ersten zwei Zeichen und die
    # genaue Länge, und das ist zusammen mit dem Pfad schon eine Spur.
    assert FIGUR_NAME not in str(fehler.value)
    assert KONTO_NAME not in str(fehler.value)
    assert FIGUR_NAME[:2] not in str(fehler.value)
    assert "Zeichen" not in str(fehler.value)


def test_der_lauf_schreibt_und_berichtet(roh, tmp_path):
    eingabe = tmp_path / "roh.json"
    eingabe.write_text(json.dumps(roh), encoding="utf-8")
    ziel = tmp_path / "unterordner" / "sauber.json"
    bericht = anonym.lauf(eingabe, ziel)
    assert "3 Konten, 3 Figuren, 2 Nachrichten" in bericht
    assert json.loads(ziel.read_text(encoding="utf-8"))["world"]["id"] == "halber-mond"


def test_die_kommandozeile_meldet_den_fehlschlag(tmp_path, capsys):
    eingabe = tmp_path / "roh.json"
    eingabe.write_text(json.dumps(["kein Weltabzug"]), encoding="utf-8")
    assert anonym.main([str(eingabe), str(tmp_path / "sauber.json")]) == 2
    assert "enthält keinen Weltabzug" in capsys.readouterr().err


# Ab hier: die vier Teilbäume, die früher ungefiltert durchliefen, und die Personendaten
# ohne Namen, die früher mit Exit 0 durchkamen. Alle Werte sind erfunden; `.invalid` ist
# die TLD, die es per Norm nie geben wird.
KLARNAME = "Reinhold Beispiel"
RECHNER = "foundry.beispiel.invalid"
MAIL = "post@beispiel.invalid"
RUFNUMMER = "089 1234567"
HEIMPFAD = "C:/Users/rbeispiel/FoundryVTT"
NETZADRESSE = "https://10.0.0.7:30000/join"

LECKPFADE = (KLARNAME, RECHNER, MAIL, RUFNUMMER, HEIMPFAD, NETZADRESSE)


@pytest.fixture
def probe() -> dict:
    """Ein Abzug, der genau dort Personendaten trägt, wo die Erlaubnisliste früher endete."""
    return {
        "userId": "k1",
        "world": {"id": "probe", "title": f"Kampagne von {KLARNAME}, Server {RECHNER}"},
        "system": {"id": "daggerheart", "title": f"Daggerheart auf {RECHNER}"},
        "users": [{"_id": "k1", "name": KLARNAME, "role": 4}],
        "actors": [{"_id": "a1", "name": "Wurm", "type": "npc", "ownership": {"default": 0}}],
        "scenes": [{"_id": "s1", "name": "Schankraum", "ownership": {"default": 0}}],
        "messages": [
            {
                "_id": "m1",
                "timestamp": 1000,
                "author": "k1",
                "whisper": [],
                "blind": False,
                "content": "",
                "speaker": {"actor": "a1", "alias": "Wurm"},
                "system": {"roll": {"total": 7, "flavor": RUFNUMMER, "notiz": HEIMPFAD}},
                "rolls": [
                    json.dumps(
                        {
                            "class": "DualityRoll",
                            "total": 7,
                            "terms": [
                                {
                                    "faces": 12,
                                    "results": [{"result": 5}],
                                    "options": {"flavor": f"{MAIL} auf {RECHNER}"},
                                }
                            ],
                            "options": {
                                "title": "Agility",
                                "roll": {"total": 7, "notiz": f"Treffen, Tel {RUFNUMMER}"},
                                "quelle": NETZADRESSE,
                            },
                        }
                    )
                ],
            }
        ],
    }


def test_die_vier_teilbaeume_lecken_nicht(probe):
    """Welttitel, ``system.roll``, ``terms[].options`` und ``options.roll`` — alle vier.

    Der Reviewer hat das Skript über einen Prüfabzug laufen lassen und in genau diesen
    vier Teilbäumen einen Klarnamen, eine Foundry-Adresse, einen Windows-Heimatpfad, eine
    E-Mail und Telefonnummern gefunden. Die Erlaubnisliste war nur auf den obersten zwei
    Ebenen eine.
    """
    ergebnis = sauber(probe)
    inhalt = " ".join(texte(ergebnis))
    for spur in LECKPFADE:
        assert spur not in inhalt, spur

    assert "title" not in ergebnis["world"] and "title" not in ergebnis["system"]
    wurf = ergebnis["messages"][0]["system"]["roll"]
    assert set(wurf) == {"total"}
    roh = json.loads(ergebnis["messages"][0]["rolls"][0])
    assert "options" not in roh["terms"][0]
    assert set(roh["options"]) == {"title", "roll"}
    assert set(roh["options"]["roll"]) == {"total"}


def test_die_pruefung_findet_personendaten_ohne_namen():
    """Die Zusage lautet »keine Personendaten«, nicht »keine Namen«."""
    ausgabe = {
        "world": {"id": RECHNER},
        "users": [{"name": MAIL}, {"name": RUFNUMMER}, {"name": HEIMPFAD}],
        "messages": [{"content": NETZADRESSE}, {"content": "Server 192.168.178.42"}],
    }
    funde = anonym.pruefe(ausgabe, ())
    arten = {fund.rsplit(": ", 1)[-1] for fund in funde}
    assert {"E-Mail", "Adresse", "Rechnername", "IP-Adresse", "Telefonnummer"} <= arten
    assert any("Heimatverzeichnis" in fund for fund in funde)
    # Der Wert selbst steht in keiner Meldung: diese Zeile landet leicht in einem Log.
    assert not any(spur in " ".join(funde) for spur in LECKPFADE)


def test_die_pruefung_haelt_die_zahlen_eines_wurfs_fuer_harmlos(probe):
    """Ein Fehlalarm auf einer Würfelformel machte das Werkzeug unbrauchbar."""
    assert anonym.pruefe(sauber(probe), ()) == []


FREIE_SCHLUESSEL = {
    "bob@beispiel.invalid": 2,
    "C:/Users/rbeispiel": 1,
    "Familie Beispiel 089/1234567": 2,
}


def test_ein_ownership_schluessel_ist_keine_freie_zeichenkette(roh):
    """Der eine Plan mit freien Schlüsseln — sie liefen ungeprüft in die Ausgabe.

    Genau diese drei Schlüssel kamen mit Exit 0 und Erfolgsmeldung durch: die Prüfung sah
    nur Werte. Was kein ``default`` und keine Konto-Id aus ``users[]`` ist, fällt weg.
    """
    bekannt = roh["users"][2]["_id"]
    roh["actors"][0]["ownership"] = FREIE_SCHLUESSEL | {"default": 2, bekannt: 3}
    ersatz = anonym.Ersatz(roh)
    ergebnis = anonym.anonymisiere(roh, ersatz)
    assert ergebnis["actors"][0]["ownership"] == {"default": 2, bekannt: 3}
    assert anonym.pruefe(ergebnis, ersatz.gefahren) == []


def test_ein_eigener_feldname_ist_kein_fund(roh):
    """Am echten Abzug brach der Lauf an ``world.<Schlüssel>`` ab — an unserem eigenen Wort.

    Eine Figur hieß wie ein Stück eines Feldnamens aus diesem Skript. Der Schlüssel steht
    aber in keiner Eingabe: er kommt aus dem Bauplan. Ein Abbruch, den niemand beheben
    kann, ohne die Ausgabe von Hand anzufassen, ist schlimmer als kein Abbruch.
    """
    roh["actors"][1]["name"] = "Core Sentinel"
    ersatz = anonym.Ersatz(roh)
    # Die Falle ist wirklich gestellt: »core« steckt in ``coreVersion``.
    assert "Core" in ersatz.gefahren
    assert "core" in "coreVersion".casefold()
    assert "coreVersion" in anonym.EIGENE_SCHLUESSEL

    ergebnis = anonym.anonymisiere(roh, ersatz)
    assert "coreVersion" in ergebnis["world"]
    assert anonym.pruefe(ergebnis, ersatz.gefahren) == []


def test_ein_system_block_ohne_wurf_bleibt_als_leerer_block_stehen(roh):
    """Der Befund aus #242 in der Fixture: ``system`` da, ``system.roll`` nicht.

    Fiele der leere Block weg, sähe eine anonymisierte Welt aus, als hätte sie gar keine
    Regelwerksdaten — und der Unterschied, an dem ``read_roll`` abzweigt, wäre nicht mehr
    zu sehen.
    """
    roh["messages"][0]["system"] = {"title": "Necrotic Ray", "hasRoll": True}
    ergebnis = sauber(roh)
    assert ergebnis["messages"][0]["system"] == {}
    assert ergebnis["messages"][1]["system"] == {"roll": {"title": "Verborgen", "total": 20}}


def test_die_pruefung_liest_auch_schluessel(roh, tmp_path, monkeypatch, capsys):
    """Der zweite Halt: fiele die Schlüsselfilterung weg, hielte der Zaun trotzdem.

    Eine ganze Fläche unbeobachtet zu lassen ist der Fehler, der sich hier wiederholen
    könnte — deshalb sieht die Prüfung jetzt Schlüssel wie Werte.
    """
    monkeypatch.setattr(
        anonym,
        "anonymisiere",
        lambda raw, ersatz=None: {"actors": [{"ownership": dict(FREIE_SCHLUESSEL)}]},
    )
    eingabe = tmp_path / "roh.json"
    eingabe.write_text(json.dumps(roh), encoding="utf-8")
    ziel = tmp_path / "sauber.json"
    assert anonym.main([str(eingabe), str(ziel)]) == 2
    assert not ziel.exists()
    fehler = capsys.readouterr().err
    arten = {fund.rsplit(": ", 1)[-1] for fund in fehler.splitlines()[1:]}
    assert {"E-Mail", "Heimatverzeichnis", "Telefonnummer"} <= arten
    # Der Schlüssel ist hier selbst der Wert — er steht deshalb auch nicht im Pfad.
    assert not any(spur in fehler for spur in FREIE_SCHLUESSEL)


def test_personendaten_ohne_namen_brechen_den_lauf_ab(probe, tmp_path, monkeypatch, capsys):
    """Früher lief so etwas mit Exit 0 und Erfolgsmeldung durch — jetzt scheitert es."""
    monkeypatch.setattr(anonym, "anonymisiere", lambda raw, ersatz=None: {"world": {"id": RECHNER}})
    eingabe = tmp_path / "roh.json"
    eingabe.write_text(json.dumps(probe), encoding="utf-8")
    ziel = tmp_path / "sauber.json"
    assert anonym.main([str(eingabe), str(ziel)]) == 2
    assert not ziel.exists()
    assert ".world.id: Rechnername" in capsys.readouterr().err


# -- Ein Mitschnitt: derselbe Abzug mehrmals, eine Pseudonymtabelle (#242) ---------------
#
# Ein Mitschnitt ist der Rohstoff, aus dem ein Abend ohne Foundry noch einmal läuft. Er
# ist damit genauso personenbezogen wie ein einzelner Abzug — und stellt eine Bedingung
# mehr: die Pseudonyme müssen über alle Bilder halten, sonst spricht im zweiten Bild
# jemand anderes als im ersten.

NACHZUEGLER = "Ferun Talbrecht"

# Zwei Blicke im Abstand von fünf Minuten. Der Abstand ist der Prüfwert: er muss die
# Anonymisierung überleben, das Datum darf es nicht.
BILDZEITEN = ("2026-08-22T20:00:00+00:00", "2026-08-22T20:05:00+00:00")
BILDABSTAND_MS = 300_000


@pytest.fixture
def mitschnitt(roh, tmp_path) -> Path:
    """Zwei Bilder eines Abends — im zweiten kommt jemand dazu, den das erste nicht kennt."""
    spaeter = json.loads(json.dumps(roh))
    spaeter["users"].append({"_id": "u-spaet", "name": NACHZUEGLER, "role": 1})
    spaeter["messages"].append(
        {
            "_id": "m-spaet",
            "timestamp": 3000,
            "author": "u-spaet",
            "content": f"{NACHZUEGLER} setzt sich dazu.",
            "speaker": {"alias": NACHZUEGLER},
            "whisper": [],
            "blind": False,
        }
    )
    datei = tmp_path / "mitschnitt-2026-08-22.jsonl"
    datei.write_text(
        "".join(
            json.dumps(
                {"zeit": zeit, "userId": roh["userId"], "welt": welt},
                ensure_ascii=False,
            )
            + "\n"
            for zeit, welt in zip(BILDZEITEN, (roh, spaeter), strict=True)
        ),
        encoding="utf-8",
    )
    return datei


def bilder(datei: Path) -> list[dict]:
    return [json.loads(zeile) for zeile in datei.read_text(encoding="utf-8").splitlines()]


def test_ein_mitschnitt_wird_bildweise_anonymisiert(mitschnitt, tmp_path):
    ziel = tmp_path / "abend.jsonl"
    meldung = anonym.lauf(mitschnitt, ziel)
    gelesen = bilder(ziel)
    assert len(gelesen) == 2
    assert "2 Bilder" in meldung
    assert all(set(bild["welt"]) >= {"users", "actors", "messages", "scenes"} for bild in gelesen)
    assert LEITUNG_NAME not in ziel.read_text(encoding="utf-8")
    assert NACHZUEGLER not in ziel.read_text(encoding="utf-8")


def test_dieselbe_person_traegt_in_jedem_bild_dasselbe_pseudonym(mitschnitt, tmp_path):
    """Sonst ließe sich der Abend nicht mehr nachspielen — es spräche jedes Mal jemand anderes."""
    ziel = tmp_path / "abend.jsonl"
    anonym.lauf(mitschnitt, ziel)
    erstes, zweites = (bild["welt"] for bild in bilder(ziel))
    assert [k["name"] for k in erstes["users"]] == [k["name"] for k in zweites["users"][:-1]]
    assert erstes["messages"][0]["speaker"]["alias"] == zweites["messages"][0]["speaker"]["alias"]


def test_ein_mitschnitt_zaehlt_ab_einem_ursprung_ueber_alle_bilder(mitschnitt, tmp_path):
    """Wann diese Gruppe gespielt hat, gehört ihr — wie weit die Bilder auseinanderliegen,
    gehört zum Material (#255).

    Der Ursprung ist einer über alle Bilder. Rechnete jedes für sich, stünde in beiden
    dieselbe Null und der Fünf-Minuten-Abstand wäre weg.
    """
    ziel = tmp_path / "abend.jsonl"
    anonym.lauf(mitschnitt, ziel)
    erstes, zweites = bilder(ziel)

    assert zweites["zeit"] - erstes["zeit"] == BILDABSTAND_MS
    assert erstes["welt"]["messages"][0]["timestamp"] == 0
    assert zweites["welt"]["messages"][-1]["timestamp"] == 2000
    assert "2026-08" not in ziel.read_text(encoding="utf-8")


def test_ein_anonymisierter_mitschnitt_bleibt_abspielbar(mitschnitt, tmp_path):
    """Die Probe aufs Ganze: nach dem Anonymisieren ist er noch ein Server mit Zahlen."""
    from chronicle.foundry import systems
    from chronicle.foundry.mitschnitt import Wiedergabe

    ziel = tmp_path / "abend.jsonl"
    anonym.lauf(mitschnitt, ziel)
    gespielt = Wiedergabe.aus_datei(ziel)
    assert len(gespielt) == 2
    _konto, welt = gespielt.fetch_world()
    wurf = systems.read_roll(systems.DAGGERHEART, welt["messages"][0])
    assert (wurf.total, wurf.formula) == (14, "1d12 + 1d12 + 2")


def test_eine_zeile_ohne_bild_bricht_den_lauf_ab(tmp_path, capsys):
    eingabe = tmp_path / "kaputt.jsonl"
    eingabe.write_text('{"welt": {}}\n[1, 2]\n', encoding="utf-8")
    ziel = tmp_path / "abend.jsonl"
    assert anonym.main([str(eingabe), str(ziel)]) == 2
    assert not ziel.exists()
    assert "Zeile 2" in capsys.readouterr().err


def test_ein_mitschnitt_ohne_bild_bricht_den_lauf_ab(tmp_path, capsys):
    eingabe = tmp_path / "leer.jsonl"
    eingabe.write_text("\n\n", encoding="utf-8")
    assert anonym.main([str(eingabe), str(tmp_path / "abend.jsonl")]) == 2
    assert "kein Bild" in capsys.readouterr().err
