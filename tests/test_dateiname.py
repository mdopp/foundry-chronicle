"""Das Tor vor dem Dateisystem: was von außen kommt, wird ein Dateiname und sonst nichts.

Diese Datei ersetzt eine Zusicherung, die vorher eine fremde Bibliothek gab
(``werkzeug.utils.secure_filename``, siehe ``chronicle.dateiname``). Ein selbst
geschriebener Bereiniger ohne Tests wäre der schlechtere Tausch — beide Aufrufer
schreiben mit dem Ergebnis eine **Datei** an einen Pfad, und der Name kommt aus dem
Anzeigenamen eines Discord-Kontos oder aus dem Dateinamen eines Uploads. Deshalb steht
hier jede Klasse, die werkzeug abdeckte, einzeln: Pfadtrenner, Aufstieg, führender Punkt,
Unicode, leeres Ergebnis.
"""

from __future__ import annotations

import pytest

from chronicle.bot.recorder import _spurname
from chronicle.dateiname import sicherer_dateiname
from chronicle.recordings import target_path


@pytest.mark.parametrize(
    "eingabe",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "unterordner/datei.wav",
        "..",
        "../..",
        "....//....//etc/passwd",
        "a/b/c/d",
    ],
)
def test_kein_pfad_ueberlebt(eingabe: str) -> None:
    """Weder Trenner noch Aufstieg dürfen durchkommen — das ist der ganze Zweck."""
    ergebnis = sicherer_dateiname(eingabe)
    assert "/" not in ergebnis
    assert "\\" not in ergebnis
    assert ".." not in ergebnis
    assert ergebnis not in {".", ".."}


def test_pfad_wird_zum_namen_und_verliert_nichts_lesbares() -> None:
    assert sicherer_dateiname("../../../etc/passwd") == "etc_passwd"


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        (".bashrc", "bashrc"),
        ("...versteckt", "versteckt"),
        ("._.", ""),
        ("spur.wav.", "spur.wav"),
        ("_rand_", "rand"),
    ],
)
def test_punkte_und_striche_an_den_raendern_fallen(eingabe: str, erwartet: str) -> None:
    """Ein führender Punkt macht unter Unix eine versteckte Datei — nicht unsere Wahl."""
    assert sicherer_dateiname(eingabe) == erwartet


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("i contain cool \xfcml\xe4uts.txt", "i_contain_cool_umlauts.txt"),
        # NFKD zerlegt ü in u + Zeichen, das Zeichen fällt beim ASCII-Schritt.
        ("Grüße vom Würfel.wav", "Grue_vom_Wurfel.wav"),
        # ß, Æ und ø zerlegt NFKD dagegen *nicht* — sie fallen ganz weg. Unschön, aber
        # genau das tat werkzeug auch, und es ist der Grund, warum beide Aufrufer einen
        # Ersatznamen brauchen: ein Name kann fast oder ganz verschwinden.
        ("Ærø", "r"),
        ("My cool movie.mov", "My_cool_movie.mov"),
    ],
)
def test_unicode_wird_zu_ascii(eingabe: str, erwartet: str) -> None:
    """NFKD zerlegt, ASCII behält den Rest — ein Name, der jedes Dateisystem übersteht."""
    ergebnis = sicherer_dateiname(eingabe)
    assert ergebnis == erwartet
    assert ergebnis.isascii()


@pytest.mark.parametrize(
    "eingabe",
    ["", "   ", "🎲🎲🎲", "日本語", "...", "//", "\x00\x01", "?|<>*", "\t\n"],
)
def test_leeres_ergebnis_ist_erlaubt_und_bleibt_leer(eingabe: str) -> None:
    """Es *darf* nichts übrigbleiben — beide Aufrufer fangen genau diesen Fall ab."""
    assert sicherer_dateiname(eingabe) == ""


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("zwei   leerzeichen", "zwei_leerzeichen"),
        ("tab\tund\nzeile", "tab_und_zeile"),
        ("frage?zeichen", "fragezeichen"),
        ("null\x00byte", "nullbyte"),
        ("semi;kolon", "semikolon"),
    ],
)
def test_zwischenraum_wird_unterstrich_der_rest_faellt(eingabe: str, erwartet: str) -> None:
    assert sicherer_dateiname(eingabe) == erwartet


def test_nur_erlaubte_zeichen_kommen_heraus() -> None:
    wild = "".join(chr(n) for n in range(1, 0x2FF))
    assert set(sicherer_dateiname(wild)) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
    )


def test_spurname_faellt_auf_die_id_zurueck() -> None:
    """Ein Anzeigename aus lauter Emoji ergibt keinen Namen — dann zählt die Id."""

    class Sprecher:
        def __init__(self, name: str, id: int) -> None:
            self.name = name
            self.id = id

    assert _spurname(Sprecher("Kalja Ravenskye", 7)) == "Kalja_Ravenskye.wav"
    assert _spurname(Sprecher("🎲🎲", 7)) == "sprecher-7.wav"
    assert _spurname(Sprecher("../../etc/passwd", 7)) == "etc_passwd.wav"


def test_target_path_bleibt_im_aufnahmeverzeichnis(tmp_path) -> None:
    """Der Kern: ein Diktatname darf das Verzeichnis nicht verlassen."""
    ziel = target_path(tmp_path, 3, "../../../etc/passwd.m4a")
    assert ziel.parent == tmp_path
    assert ziel.name.endswith("-passwd.m4a")

    # ``Path.stem`` wirft das Verzeichnis schon weg; was es stehen lässt, fängt der
    # Bereiniger — ein Stamm, der nur aus ``..`` besteht, wird leer und damit »diktat«.
    aufstieg = target_path(tmp_path, 3, "..")
    assert aufstieg.parent == tmp_path
    assert aufstieg.name.endswith("-diktat")

    ohne_rest = target_path(tmp_path, 3, "🎲.m4a")
    assert ohne_rest.parent == tmp_path
    assert ohne_rest.name.endswith("-diktat.m4a")
