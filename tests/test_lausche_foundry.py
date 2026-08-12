"""Das Beobachtungswerkzeug aus #146, soweit es ohne laufenden Server prüfbar ist.

Was der Lauf mitschreibt, entscheidet erst ein echter Server. Prüfbar ist hier das
Drumherum, und das ist genau das Heikle: dass ohne Passwort nichts läuft, dass die
Mitschrift dieselben Rechte bekommt wie der Weltabzug, dass ein toter Server eine
Meldung gibt und keinen Traceback — und dass das Passwort nirgends auftaucht.
"""

from __future__ import annotations

import importlib.util
import json
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import PASSWORT

from chronicle.foundry.client import FoundryUnreachable

WURZEL = Path(__file__).resolve().parents[1]

SKRIPT = WURZEL / "scripts" / "lausche_foundry.py"

_spec = importlib.util.spec_from_file_location("lausche_foundry", SKRIPT)
lausche = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lausche)

EREIGNISSE = (
    ("session", {"userId": "u-chronist"}),
    ("modifyDocument", {"type": "ChatMessage", "action": "create"}),
)


class Leitung:
    """Nur, was das Skript benutzt — jeder Griff daneben fliegt auf."""

    def __init__(self, ereignisse=EREIGNISSE, fehler=None):
        self._ereignisse = ereignisse
        self._fehler = fehler
        self.getrennt = False

    @contextmanager
    def verbindung(self, *, mithoeren=None):
        if self._fehler is not None:
            raise self._fehler
        for name, nutzlast in self._ereignisse:
            mithoeren(name, nutzlast)
        yield self
        self.getrennt = True


def lauf(tmp_path, *, client=None, passwort=PASSWORT, argv=("--dauer", "0")):
    return lausche.main(
        list(argv),
        client=Leitung() if client is None else client,
        frage=lambda _: passwort,
        ordner=tmp_path / "dumps",
    )


def mitschriften(tmp_path) -> list[Path]:
    ordner = tmp_path / "dumps"
    return sorted(ordner.glob("*.jsonl")) if ordner.exists() else []


def test_ohne_passwort_wird_nichts_angelegt_und_nichts_verbunden(tmp_path, capsys):
    leitung = Leitung()

    assert lauf(tmp_path, client=leitung, passwort="") == 2

    assert "nirgends gespeichert" in capsys.readouterr().err
    assert mitschriften(tmp_path) == []
    assert not leitung.getrennt


def test_mit_passwort_laeuft_derselbe_aufruf_durch(tmp_path):
    """Gegenprobe: es ist wirklich das Passwort, das den Lauf aufhält."""
    assert lauf(tmp_path) == 0
    assert len(mitschriften(tmp_path)) == 1


def test_die_mitschrift_liegt_so_geschuetzt_wie_der_weltabzug(tmp_path):
    assert lauf(tmp_path) == 0

    datei = mitschriften(tmp_path)[0]
    assert stat.S_IMODE(datei.stat().st_mode) == 0o600
    assert stat.S_IMODE(datei.parent.stat().st_mode) == 0o700


def test_ein_offener_ordner_wird_zugezogen(tmp_path):
    """Gegenprobe: ein schon vorhandenes 0755 bliebe ohne das ``chmod`` offen."""
    ordner = tmp_path / "dumps"
    ordner.mkdir(mode=0o755)
    assert stat.S_IMODE(ordner.stat().st_mode) == 0o755

    assert lauf(tmp_path) == 0

    assert stat.S_IMODE(ordner.stat().st_mode) == 0o700


def test_ein_toter_server_meldet_sich_verstaendlich(tmp_path, capsys):
    fehler = FoundryUnreachable("Socket.io-Verbindung fehlgeschlagen: ConnectionError")

    assert lauf(tmp_path, client=Leitung(fehler=fehler)) == 1

    ausgabe = capsys.readouterr()
    assert "Keine Leitung zu Foundry" in ausgabe.err
    assert str(fehler) in ausgabe.err
    assert "Traceback" not in ausgabe.err


def test_ein_erreichbarer_server_meldet_keinen_fehler(tmp_path, capsys):
    """Gegenprobe zum toten Server: dann steht nichts auf der Fehlerausgabe."""
    assert lauf(tmp_path) == 0
    assert capsys.readouterr().err == ""


def test_jedes_ereignis_steht_mit_nutzlast_in_der_mitschrift(tmp_path):
    assert lauf(tmp_path) == 0

    zeilen = [json.loads(z) for z in mitschriften(tmp_path)[0].read_text("utf-8").splitlines()]
    assert [zeile["ereignis"] for zeile in zeilen] == ["session", "modifyDocument"]
    assert zeilen[1]["nutzlast"] == [{"type": "ChatMessage", "action": "create"}]
    assert all(zeile["zeit"] for zeile in zeilen)


def test_was_nicht_kam_steht_auch_nicht_drin(tmp_path):
    """Gegenprobe: die Mitschrift ist ein Protokoll, keine Vermutung."""
    assert lauf(tmp_path, client=Leitung(ereignisse=(("session", {}),))) == 0
    assert "modifyDocument" not in mitschriften(tmp_path)[0].read_text("utf-8")


def test_eine_stumme_leitung_wird_als_befund_gemeldet(tmp_path, capsys):
    assert lauf(tmp_path, client=Leitung(ereignisse=())) == 0
    assert "Kein einziges Ereignis" in capsys.readouterr().out


def test_der_lauf_sagt_vorher_was_er_anlegt(tmp_path, capsys):
    assert lauf(tmp_path) == 0

    ausgabe = capsys.readouterr().out
    assert str(mitschriften(tmp_path)[0]) in ausgabe
    assert "Klarnamen" in ausgabe
    assert "nie ins Repo" in ausgabe


def test_die_leitung_wird_am_ende_getrennt(tmp_path):
    leitung = Leitung()
    assert lauf(tmp_path, client=leitung) == 0
    assert leitung.getrennt


def test_das_passwort_steht_in_keiner_ausgabe_und_in_keiner_datei(tmp_path, capsys):
    assert lauf(tmp_path) == 0

    ausgabe = capsys.readouterr()
    assert PASSWORT not in ausgabe.out + ausgabe.err
    assert PASSWORT not in mitschriften(tmp_path)[0].read_text("utf-8")


def test_es_wird_nichts_neben_der_mitschrift_angelegt(tmp_path):
    """Keine Runde, keine Nachricht, keine Zeile in der Datenbank."""
    assert lauf(tmp_path) == 0
    assert [pfad.name for pfad in tmp_path.rglob("*") if pfad.is_file()] == [
        mitschriften(tmp_path)[0].name
    ]


@pytest.mark.parametrize("argument", ["--url", "--benutzer"])
def test_adresse_und_konto_lassen_sich_am_aufruf_setzen(tmp_path, argument):
    """Die Werte einer Runde liegen in der SQLite — und die bleibt hier zu."""
    assert lauf(tmp_path, argv=(argument, "wert", "--dauer", "0")) == 0


def test_fuer_das_passwort_gibt_es_kein_aufrufargument(tmp_path):
    with pytest.raises(SystemExit):
        lauf(tmp_path, argv=("--passwort", PASSWORT))
