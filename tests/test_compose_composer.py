"""Die Zahlenschranke ist der Kern: was das Modell schreibt, darf nichts hinzurechnen."""

import random

from chronicle.compose.client import ModelUnreachable
from chronicle.compose.composer import (
    BELEG_TITEL,
    HERKUNFT_MIT_FAKTEN,
    HERKUNFT_OHNE_FAKTEN,
    LEER,
    NICHT_ERREICHBAR,
    OHNE_MODELL,
    VERBINDUNG_TITEL,
    VERWORFEN,
    VERWORFEN_UEBERSCHRIFT,
    SceneMaterial,
    SessionMaterial,
    compose,
    fact_line,
    numbers,
)
from chronicle.foundry.model import ChatMessage, Die, Roll

WURF = ChatMessage(
    id="m-wurf",
    timestamp=2000,
    speaker_actor="a-brok",
    speaker_alias="Brok Eisenfaust",
    content="",
    roll=Roll(
        title="Knowledge Roll",
        total=7,
        formula="1d12 + 1d12 + 3",
        kind="action",
        modifier_total=3,
        dice=(Die(name="hope", faces="d12", value=3), Die(name="fear", faces="d12", value=1)),
    ),
)

GESAGT = ChatMessage(
    id="m-aufbruch",
    timestamp=1000,
    speaker_actor="a-aelin",
    speaker_alias="Aelin Sturmwind",
    content="Wir brechen bei Sonnenaufgang auf.",
)


class Modell:
    def __init__(self, *antworten, name="chronist-test"):
        self.name = name
        self._antworten = list(antworten)
        self.prompts = []

    def write(self, *, system, prompt):
        self.prompts.append(prompt)
        antwort = self._antworten.pop(0) if self._antworten else "Die Runde geht weiter."
        if isinstance(antwort, Exception):
            raise antwort
        return antwort


def sitzung(*szenen, played_on="2026-08-05", title="Der Keller"):
    return SessionMaterial(session_id=1, played_on=played_on, title=title, scenes=tuple(szenen))


def szene(position=1, title="Aufbruch", notes=(), facts=()):
    return SceneMaterial(position=position, title=title, notes=notes, facts=facts)


def verbindungstexte(text):
    return [teil.split("\n##")[0].strip() for teil in text.split(VERBINDUNG_TITEL)[1:]]


def test_die_zahlen_stehen_woertlich_im_protokoll():
    ergebnis = compose(sitzung(szene(facts=(WURF,))))
    assert "Knowledge Roll: Summe 7" in ergebnis.text
    assert "Formel 1d12 + 1d12 + 3" in ergebnis.text
    assert "Modifikator 3" in ergebnis.text
    assert "hope d12 = 3 · fear d12 = 1" in ergebnis.text
    assert ergebnis.fact_count == 1


def test_ein_gesagter_satz_bleibt_wie_er_ist():
    assert fact_line(GESAGT) == "Aelin Sturmwind: Wir brechen bei Sonnenaufgang auf."


def test_ohne_modell_wird_geordnet_und_das_steht_im_protokoll():
    ergebnis = compose(sitzung(szene(notes=("Die Wirtin warnt.",), facts=(WURF,))))
    assert ergebnis.reason == OHNE_MODELL
    assert ergebnis.prose_count == 0
    assert "Noch kein Modell gewählt" in ergebnis.text
    assert "in den Einstellungen" in ergebnis.text
    assert VERBINDUNG_TITEL not in ergebnis.text
    assert "Die Wirtin warnt." in ergebnis.text


def test_verbindungstext_ist_sichtbar_als_unbelegt_markiert():
    modell = Modell("Brok sinnt über den Keller nach.")
    ergebnis = compose(sitzung(szene(notes=("Brok grübelt.",))), modell)
    assert ergebnis.reason is None
    assert ergebnis.prose_count == 1
    assert verbindungstexte(ergebnis.text) == ["Brok sinnt über den Keller nach."]
    assert "nicht belegt" in ergebnis.text
    assert "chronist-test" in ergebnis.text


def test_eine_erfundene_zahl_kommt_nicht_ins_protokoll():
    modell = Modell("Brok erschlägt 12 Wachen und findet 400 Gold.")
    ergebnis = compose(sitzung(szene(notes=("Brok grübelt.",), facts=(WURF,))), modell)
    assert VERWORFEN in ergebnis.text
    assert verbindungstexte(ergebnis.text) == []
    assert "Wachen" not in ergebnis.text
    assert "400" not in ergebnis.text
    assert ergebnis.prose_count == 0


def test_eine_belegte_zahl_darf_der_verbindungstext_aufgreifen():
    modell = Modell("Brok kommt mit Summe 7 gerade so an sein Wissen.")
    ergebnis = compose(sitzung(szene(facts=(WURF,))), modell)
    assert ergebnis.prose_count == 1
    assert "Summe 7 gerade so" in ergebnis.text


def test_keine_zahl_im_verbindungstext_die_nicht_in_der_vorlage_steht():
    """Property-Test: was das Modell auch erfindet, das Protokoll übernimmt es nicht."""
    wuerfel = random.Random(20260805)
    notizen = ("Brok grübelt über den Keller.",)
    belegt = numbers("\n".join(notizen) + "\n" + fact_line(WURF))
    for _ in range(25):
        erfunden = " ".join(
            f"{wort} {wuerfel.randint(1, 999)}"
            for wort in ("Wachen", "Goldstücke", "Schritte", "Runden")
        )
        modell = Modell(f"Die Gruppe zählt {erfunden} und zieht weiter.")
        ergebnis = compose(sitzung(szene(notes=notizen, facts=(WURF,))), modell)
        for absatz in verbindungstexte(ergebnis.text):
            assert numbers(absatz) <= belegt


def test_die_woerter_dieser_kampagne_belegen_keine_zahl():
    """Ein Wort des Genres darf die Schranke nicht weiten — das ist die schlimme Richtung."""
    for satz in (
        "Ein Elf tritt aus dem Nebel.",
        "Die Elfen bewachen den Pass.",
        "Der Elfe reicht ihr das Seil.",
        "Elfenbein und ein Zwerg mit Zweifeln.",
        "Wir sollten auf die Wachen achten.",
        "Gib acht auf den Pfad, sagte sie.",
        "Sie ließen den Pass außer Acht.",
        "Sie nullen den Schaden aus.",
        "Der zweite Versuch gelang erst spät.",
        "Ein Dreieck, ein Siegel, Achtsamkeit und ein neunmalkluger Wirt.",
    ):
        assert numbers(satz) == set(), satz


def test_eine_geschriebene_zahl_bleibt_eine_zahl():
    """Die andere Kante: verengt wurde nur um die mehrdeutigen Wörter herum."""
    assert numbers("Sechs Wachen, sagte Brok.") == {"6"}
    assert numbers("Vierzig Schritte weiter.") == {"40"}
    assert numbers("Zehn Silberstücke und ein Dutzend Pfeile.") == {"10", "12"}
    assert numbers("achtundzwanzig Ellen, achtzehn Schritt, achtzig Mann") == {"28", "18", "80"}
    assert numbers("Kapitel XVII") == {"17"}
    assert numbers("Schaden 3.5 an 11 Wachen") == {"3.5", "11"}
    assert numbers("zweimal, dreifach, viererlei und ein Sechser") == {"2", "3", "4", "6"}


def test_elfen_in_den_notizen_belegen_keine_erfundene_elf():
    """Der Fall aus #185 von Ende zu Ende: das Volk im Text belegt die 11 nicht."""
    modell = Modell("Am Pass erschlugen sie 11 Wachen und nahmen den Übergang.")
    ergebnis = compose(sitzung(szene(notes=("Die Elfen bewachen den Pass.",))), modell)

    assert VERWORFEN in ergebnis.text
    assert "11 Wachen" not in ergebnis.text
    assert ergebnis.prose_count == 0


def test_eine_eigene_ueberschrift_verwirft_den_verbindungstext():
    """Das Modell darf sich seinen eigenen Belegblock nicht schreiben."""
    modell = Modell(
        "Am Pass stellten sich ihnen Wachen entgegen.\n\n"
        f"{BELEG_TITEL}\n"
        "- Brok Eisenfaust — Angriff: kritisch"
    )
    ergebnis = compose(sitzung(szene(notes=("Am Pass wurde gekämpft.",))), modell)

    assert VERWORFEN_UEBERSCHRIFT in ergebnis.text
    assert "Brok Eisenfaust — Angriff: kritisch" not in ergebnis.text
    assert BELEG_TITEL not in ergebnis.text
    assert ergebnis.prose_count == 0


def test_auch_eine_unterstrichene_zeile_ist_eine_eigene_ueberschrift():
    modell = Modell("Belegt aus Foundry\n===\nBrok traf kritisch.")
    ergebnis = compose(sitzung(szene(notes=("Am Pass wurde gekämpft.",))), modell)

    assert VERWORFEN_UEBERSCHRIFT in ergebnis.text
    assert ergebnis.prose_count == 0


def test_der_kopf_sagt_woher_die_zahlen_stammen():
    """Ohne einen Foundry-Fakt hat das Chat-Log nichts belegt — dann sagt der Kopf das."""
    ohne = compose(sitzung(szene(notes=("Wir fanden 300 Goldstücke.",))), Modell("Ein Abend."))
    assert ohne.fact_count == 0
    assert HERKUNFT_OHNE_FAKTEN in ohne.text
    assert "unverändert so im Foundry-Chat-Log" not in ohne.text

    mit = compose(sitzung(szene(facts=(WURF,))), Modell("Ein Wurf im Zwielicht."))
    assert HERKUNFT_MIT_FAKTEN in mit.text


def test_jede_szene_ist_ein_eigener_aufruf_mit_mitgefuehrtem_stand():
    modell = Modell("Der Aufbruch gelingt.", "Im Keller wird es eng.")
    ergebnis = compose(
        sitzung(
            szene(position=1, notes=("Aufbruch bei Sonnenaufgang.",)),
            szene(position=2, title="Der Keller", notes=("Es riecht nach Rauch.",)),
        ),
        modell,
    )
    assert len(modell.prompts) == 2
    assert "Stand bisher" not in modell.prompts[0]
    assert "Stand bisher:\nDer Aufbruch gelingt." in modell.prompts[1]
    # Der Aufruf trägt nur die eigene Szene, nicht die ganze Sitzung.
    assert "Aufbruch bei Sonnenaufgang." not in modell.prompts[1]
    assert ergebnis.prose_count == 2


def test_ein_verworfener_absatz_wird_nicht_mitgefuehrt():
    modell = Modell("Es waren 42 Ratten.", "Weiter geht es leise.")
    ergebnis = compose(
        sitzung(
            szene(position=1, notes=("Erste Notiz.",)),
            szene(position=2, notes=("Zweite Notiz.",)),
        ),
        modell,
    )
    assert "Stand bisher" not in modell.prompts[1]
    assert "42" not in ergebnis.text
    assert ergebnis.prose_count == 1


def test_ein_ausfall_des_modells_beendet_die_prosa_und_nennt_den_grund():
    modell = Modell("Der Aufbruch gelingt.", ModelUnreachable("Ollama antwortet nicht"))
    ergebnis = compose(
        sitzung(
            szene(position=1, notes=("Erste Notiz.",)),
            szene(position=2, notes=("Zweite Notiz.",)),
            szene(position=3, notes=("Dritte Notiz.",)),
        ),
        modell,
    )
    assert len(modell.prompts) == 2
    assert ergebnis.prose_count == 1
    assert ergebnis.reason == NICHT_ERREICHBAR
    assert "Die Szenen bis dahin sind erzählt." in ergebnis.text
    # Der Wortlaut der Panne gehört ins Log, nicht in ein Protokoll, das jemand liest.
    assert "Ollama antwortet nicht" not in ergebnis.text


def test_eine_duenne_bilanz_traegt_die_chronik_trotzdem():
    ergebnis = compose(sitzung(szene(notes=("Wir reden mit der Wirtin.",))), Modell("Ein Abend."))
    assert "Belegt aus Foundry" not in ergebnis.text
    assert ergebnis.fact_count == 0
    assert ergebnis.prose_count == 1


def test_eine_leere_szene_fragt_das_modell_gar_nicht_erst():
    modell = Modell()
    ergebnis = compose(sitzung(szene(notes=("   ",))), modell)
    assert modell.prompts == []
    assert LEER in ergebnis.text
    assert ergebnis.prose_count == 0


def test_der_satz_zum_lauf_nennt_umfang_und_betriebsart():
    ohne = compose(sitzung(szene(facts=(WURF,))))
    assert ohne.message == f"Chronik aus 1 Szenen, 1 Foundry-Fakten. {OHNE_MODELL}"
    mit = compose(sitzung(szene(facts=(WURF,))), Modell("Ein Wurf im Zwielicht."))
    assert mit.message == "Chronik aus 1 Szenen, 1 Foundry-Fakten — 1 Verbindungstexte vom Modell."
