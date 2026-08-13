"""Der abgeleitete Image-Tag für den Rollout.

Die Behauptung, die hier gehalten wird: der Tag zeigt auf einen Stand, den es in GHCR
wirklich gibt — die Registry wird gefragt, nicht die Historie geraten. Und ``--zurueck 1``
nennt den veröffentlichten Stand davor, den Weg zurück.

Der Fall, an dem die erste Fassung zerbrach (#176), steht als eigene Fixture da: ein Push
landet mehrere Commits auf einmal, der letzte fasst keine Bau-Pfade an — getaggt wird
trotzdem er.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import urllib.error
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "bestimme_image_tag", WURZEL / "scripts" / "bestimme_image_tag.py"
)
tagskript = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tagskript)

# Dieselben Präfixe wie der Pfadfilter in build-images.yml — mehr braucht das Modell der
# Fixture nicht.
BAUPFADE = ("src/", "pyproject.toml", "Dockerfile", ".dockerignore")


def _git(verzeichnis: Path, *argumente: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *argumente],
        cwd=verzeichnis,
        check=True,
        capture_output=True,
    )


class Wegwerfrepo:
    """Ein Repo plus die Tags, die dazu in GHCR lägen — gefüllt nach derselben Regel wie
    GitHub sie anwendet: ein *Push* löst den Bau aus, wenn irgendeine seiner Dateien im
    Pfadfilter liegt, und ``type=sha`` taggt dann seinen **letzten** Commit."""

    def __init__(self, pfad: Path) -> None:
        self.pfad = pfad
        self.tags_in_ghcr: set[str] = set()

    def commit(self, datei: str) -> str:
        ziel = self.pfad / datei
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_text(datei, encoding="utf-8")
        _git(self.pfad, "add", datei)
        _git(self.pfad, "commit", "-q", "-m", f"chore: {datei}")
        return self.sha(0)

    def push(self, *dateien: str) -> str:
        """Ein Push mit beliebig vielen Commits — der Batch-Merge aus CLAUDE.md."""
        for datei in dateien:
            kopf = self.commit(datei)
        if any(datei.startswith(BAUPFADE) for datei in dateien):
            self.tags_in_ghcr.add(tagskript.tag(kopf))
        return kopf

    def sha(self, tiefe: int) -> str:
        ergebnis = subprocess.run(
            ["git", "log", "--format=%H", "HEAD"],
            cwd=self.pfad,
            check=True,
            capture_output=True,
            text=True,
        )
        return ergebnis.stdout.split()[tiefe]


class _Antwort:
    def __init__(self, koerper: bytes = b"") -> None:
        self._koerper = koerper

    def __enter__(self) -> _Antwort:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self, *_: object) -> bytes:
        return self._koerper


@pytest.fixture(autouse=True)
def _kein_token_leck():
    tagskript._pull_token.cache_clear()
    yield
    tagskript._pull_token.cache_clear()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wegwerfrepo:
    _git(tmp_path, "init", "-q", "-b", "main")
    monkeypatch.setattr(tagskript, "WURZEL", tmp_path)
    wegwerf = Wegwerfrepo(tmp_path)
    monkeypatch.setattr(
        tagskript,
        "existiert_in_der_registry",
        lambda kennung: kennung in wegwerf.tags_in_ghcr,
    )
    return wegwerf


def test_bildpfade_stehen_im_workflow() -> None:
    pfade = tagskript.bildpfade(tagskript.WORKFLOW.read_text(encoding="utf-8"))
    assert ":(glob)src/**" in pfade
    assert ":(glob)pyproject.toml" in pfade
    assert ":(glob)Dockerfile" in pfade


def test_der_tag_hat_die_form_aus_der_tag_matrix() -> None:
    # type=sha,prefix=sha-,format=short — sieben Zeichen, nicht die wachsende Kurzform
    # von git.
    assert tagskript.tag("0123456789abcdef0123456789abcdef01234567") == "sha-0123456"


def test_der_batch_kopf_traegt_den_tag_auch_ohne_baupfad(repo: Wegwerfrepo, capsys) -> None:
    # Der Fall aus #176: ein Rebase-Merge landet ddf8821..7e758a1 in *einem* Push. Der
    # Bau-Pfad-Commit steckt in der Mitte, getaggt wird der Doku-Commit am Ende. Wer die
    # Historie nach Bau-Pfaden absucht, nennt hier den mittleren — und der hat in GHCR
    # nichts.
    repo.push("src/eins.py")
    mitte_und_kopf = repo.push("src/zwei.py", "README.md")
    assert tagskript.tag(repo.sha(1)) not in repo.tags_in_ghcr

    assert tagskript.main(["--ref", "main"]) == 0
    ausgabe = capsys.readouterr()
    assert ausgabe.out.strip() == tagskript.tag(mitte_und_kopf)
    assert ausgabe.err == ""


def test_zurueck_nennt_den_vorherigen_veroeffentlichten_stand(repo: Wegwerfrepo, capsys) -> None:
    aelter = repo.push("src/eins.py")
    repo.push("src/zwei.py", "README.md")
    assert tagskript.main(["--ref", "main", "--zurueck", "1"]) == 0
    assert capsys.readouterr().out.strip() == tagskript.tag(aelter)


def test_ein_kopf_ohne_eigenes_image_wird_genannt_nicht_verschwiegen(
    repo: Wegwerfrepo, capsys
) -> None:
    # Reiner Doku-Push nach dem Bau: für main gibt es kein eigenes Image. Der ältere Tag
    # ist inhaltlich derselbe Stand — er wird genannt, aber nicht stillschweigend.
    gebaut = repo.push("src/eins.py")
    repo.push("docs.md")
    assert tagskript.main(["--ref", "main"]) == 0
    ausgabe = capsys.readouterr()
    assert ausgabe.out.strip() == tagskript.tag(gebaut)
    assert "kein eigenes Image" in ausgabe.err


def test_ein_fehlendes_image_wird_verweigert_statt_alt_beantwortet(
    repo: Wegwerfrepo, capsys
) -> None:
    # Bau-Pfade geändert, aber kein Tag in GHCR: der Bau ist rot oder fehlt. Ein älterer
    # Tag wäre hier ein anderer Stand — also lieber kein Tag.
    repo.push("src/eins.py")
    kopf = repo.push("src/drei.py")
    repo.tags_in_ghcr.discard(tagskript.tag(kopf))

    assert tagskript.main(["--ref", "main"]) == 1
    ausgabe = capsys.readouterr()
    assert ausgabe.out.strip() == ""
    assert "Bau fehlt oder ist rot" in ausgabe.err


def test_voll_liefert_die_ganze_referenz(repo: Wegwerfrepo, capsys) -> None:
    kopf = repo.push("src/eins.py")
    assert tagskript.main(["--ref", "main", "--voll"]) == 0
    assert capsys.readouterr().out.strip() == f"{tagskript.IMAGE}:{tagskript.tag(kopf)}"


def test_zu_weit_zurueck_meldet_sich_statt_zu_raten(repo: Wegwerfrepo, capsys) -> None:
    repo.push("src/eins.py")
    assert tagskript.main(["--ref", "main", "--zurueck", "5"]) == 1
    assert "veröffentlichte Stände" in capsys.readouterr().err


def test_die_registry_wird_gefragt_statt_geraten(monkeypatch: pytest.MonkeyPatch) -> None:
    gefragt: list[tuple[str, str, str | None]] = []

    def falsches_urlopen(anfrage, timeout=None):
        if isinstance(anfrage, str):
            assert f"repository:{tagskript.PAKET}:pull" in anfrage
            return _Antwort(json.dumps({"token": "T"}).encode())
        gefragt.append(
            (anfrage.full_url, anfrage.get_method(), anfrage.get_header("Authorization"))
        )
        if anfrage.full_url.endswith("sha-vorhanden"):
            return _Antwort()
        raise urllib.error.HTTPError(anfrage.full_url, 404, "MANIFEST_UNKNOWN", {}, None)

    monkeypatch.setattr(tagskript.urllib.request, "urlopen", falsches_urlopen)

    assert tagskript.existiert_in_der_registry("sha-vorhanden") is True
    assert tagskript.existiert_in_der_registry("sha-fehlt") is False
    assert gefragt[0] == (
        f"https://{tagskript.REGISTRY}/v2/{tagskript.PAKET}/manifests/sha-vorhanden",
        "HEAD",
        "Bearer T",
    )


def test_ein_kaputter_bau_bricht_nicht_als_404_durch(monkeypatch: pytest.MonkeyPatch) -> None:
    # 500 heißt nicht 'gibt es nicht' — das darf nicht als 'nicht veröffentlicht'
    # durchgehen, sonst nennt das Skript wieder einen älteren Tag als den gemeinten.
    def falsches_urlopen(anfrage, timeout=None):
        if isinstance(anfrage, str):
            return _Antwort(json.dumps({"token": "T"}).encode())
        raise urllib.error.HTTPError(anfrage.full_url, 500, "kaputt", {}, None)

    monkeypatch.setattr(tagskript.urllib.request, "urlopen", falsches_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        tagskript.existiert_in_der_registry("sha-egal")


def test_ohne_registry_wird_kein_tag_geraten(repo: Wegwerfrepo, monkeypatch, capsys) -> None:
    repo.push("src/eins.py")

    def kein_netz(_kennung: str) -> bool:
        raise urllib.error.URLError("Name oder Dienst nicht bekannt")

    monkeypatch.setattr(tagskript, "existiert_in_der_registry", kein_netz)
    assert tagskript.main(["--ref", "main"]) == 2
    ausgabe = capsys.readouterr()
    assert ausgabe.out.strip() == ""
    assert "GHCR nicht erreichbar" in ausgabe.err


def test_die_registry_adresse_stimmt_mit_dem_template_ueberein() -> None:
    # Zwei Stellen, ein Image: sonst pinnt das Skript einen Tag, den das Manifest gar
    # nicht zieht.
    manifest = (WURZEL / "templates" / "daggerheart-chronik" / "template.yml").read_text(
        encoding="utf-8"
    )
    assert re.findall(r"^\s*image: (\S+):", manifest, re.MULTILINE) == [tagskript.IMAGE] * 2


def test_die_variable_verweist_auf_das_skript() -> None:
    # Der Assistent zeigt diese Beschreibung beim Ausrollen an; steht der Weg zum Tag
    # nicht dort, wird wieder 'latest' eingetragen.
    variablen = json.loads(
        (WURZEL / "templates" / "daggerheart-chronik" / "variables.json").read_text(
            encoding="utf-8"
        )
    )
    assert "bestimme_image_tag.py" in variablen["CHRONICLE_IMAGE_TAG"]["description"]
