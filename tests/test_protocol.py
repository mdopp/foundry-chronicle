"""Der kleinste Renderer: genau der Ausschnitt, den die Komposition schreibt."""

import pytest
from conftest import runde

from chronicle import db, protocol
from chronicle.compose.composer import BELEG_TITEL, NOTIZEN_TITEL, VERBINDUNG_TITEL
from chronicle.compose.recap import CHRONIK_TITEL, FAEDEN_TITEL, HERGANG_TITEL
from chronicle.compose.service import RUECKBLICK, save

STAND = "2026-08-05T20:00:00+00:00"

CHRONIK = "\n".join(
    [
        "# Chronik — Sitzung vom 2026-08-05: Der Keller",
        "",
        "_Verbindungstexte stammen vom Sprachmodell `chronist-test`._",
        "",
        "## Szene 1 — Aufbruch",
        "",
        NOTIZEN_TITEL,
        "- Wir brechen bei Sonnenaufgang auf.",
        "- Brok packt seine Axt ein.",
        "",
        BELEG_TITEL,
        "- Brok — Knowledge Roll: Summe 7 · Formel 1d12 + 1d12 + 3",
        "",
        VERBINDUNG_TITEL,
        "Die Runde tastet sich voran.",
        "",
    ]
)


RUECKBLICK_TEXT = "\n".join(
    [
        "# Rückblick — Sitzung vom 2026-08-05: Der Keller",
        "",
        HERGANG_TITEL,
        "Die Runde stieg in den Keller.",
        "",
        FAEDEN_TITEL,
        "- Die Wirtin wartet auf Antwort.",
        "",
        CHRONIK_TITEL,
        "- Brok — Knowledge Roll: Summe 7",
        "",
    ]
)


def gerendert(text=CHRONIK):
    return str(protocol.render(text))


@pytest.fixture
def scope(config):
    zugang = db.scoped(runde(config))
    yield zugang
    zugang.close()


def sitzung(scope, *, played_on="2026-08-05", title="Der Keller"):
    zeiger = scope.execute(
        "INSERT INTO session (runde_id, played_on, title, created_at) VALUES (?, ?, ?, ?)",
        (scope.runde_id, played_on, title, STAND),
    )
    scope.commit()
    return zeiger.lastrowid


def test_die_ueberschriften_beginnen_unter_der_seiten_h1():
    html = gerendert()
    assert "<h2>Chronik — Sitzung vom 2026-08-05: Der Keller</h2>" in html
    assert "<h3>Szene 1 — Aufbruch</h3>" in html
    assert "<h4>Notizen</h4>" in html


def test_belegtes_und_verbindungstext_bleiben_getrennt_sichtbar():
    html = gerendert()
    assert '<section class="abschnitt notizen">' in html
    assert '<section class="abschnitt belegt">' in html
    assert '<section class="abschnitt verbindung">' in html
    # Jeder Abschnitt wird auch wieder geschlossen, sonst verrutscht die Zuordnung.
    assert html.count("<section") == html.count("</section>") == 3


def test_der_verbindungstext_steht_in_seinem_eigenen_abschnitt():
    verbindung = gerendert().split('<section class="abschnitt verbindung">')[1]
    assert "<p>Die Runde tastet sich voran.</p>" in verbindung
    assert "Summe 7" not in verbindung


def test_aufzaehlungen_werden_zu_einer_liste():
    html = gerendert()
    assert "<ul>\n<li>Wir brechen bei Sonnenaufgang auf.</li>" in html
    assert html.count("<ul>") == html.count("</ul>") == 2


def test_ganzzeilige_kursivschrift_und_code_in_der_zeile():
    html = gerendert()
    assert '<p class="kursiv">Verbindungstexte stammen vom Sprachmodell ' in html
    assert "<code>chronist-test</code>" in html


def test_ein_hinweis_ohne_abschnitt_bleibt_ein_absatz():
    html = gerendert("## Szene 2\n\n_Weder Notizen noch Foundry-Fakten._\n")
    assert "<section" not in html
    assert '<p class="kursiv">Weder Notizen noch Foundry-Fakten.</p>' in html


def test_notizen_koennen_kein_html_einschleusen():
    html = gerendert("- <script>alert(1)</script>\n\nEin & ein <b>Wort</b>.\n")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Ein &amp; ein &lt;b&gt;Wort&lt;/b&gt;." in html


def test_ohne_lauf_gibt_es_kein_protokoll(config, scope):
    assert protocol.stored(runde(config), sitzung(scope)) is None


def test_das_abgelegte_protokoll_kommt_zurueck(config, scope):
    sitzung_id = sitzung(scope)
    save(scope, sitzung_id, CHRONIK, STAND)

    abgelegt = protocol.stored(runde(config), sitzung_id)

    assert abgelegt.text == CHRONIK
    assert abgelegt.created_at == STAND
    assert '<section class="abschnitt belegt">' in str(abgelegt.html)


def test_die_liste_zeigt_jede_sitzung_mit_und_ohne_chronik(config, scope):
    mit = sitzung(scope, played_on="2026-08-05", title="Der Keller")
    ohne = sitzung(scope, played_on="2026-07-01", title="Der Hafen")
    save(scope, mit, CHRONIK, STAND)

    eintraege = protocol.entries(runde(config))

    assert [e.session_id for e in eintraege] == [mit, ohne]
    assert eintraege[0].created_at == STAND
    assert eintraege[1].created_at is None


def test_ohne_sitzung_ist_die_liste_leer(config, scope):
    assert protocol.entries(runde(config)) == ()


def test_die_offenen_faeden_bekommen_einen_eigenen_abschnitt():
    html = gerendert(RUECKBLICK_TEXT)
    assert "<h2>Rückblick — Sitzung vom 2026-08-05: Der Keller</h2>" in html
    # Deutung, Verbindungstext und Belegtes dürfen nie gleich aussehen.
    assert '<section class="abschnitt deutung">' in html
    assert '<section class="abschnitt verbindung">' in html
    assert '<section class="abschnitt belegt">' in html


def test_chronik_und_rueckblick_liegen_unter_eigener_art(config, scope):
    sitzung_id = sitzung(scope)
    save(scope, sitzung_id, CHRONIK, STAND)
    save(scope, sitzung_id, RUECKBLICK_TEXT, STAND, kind=RUECKBLICK)

    assert protocol.stored(runde(config), sitzung_id).text == CHRONIK
    abgelegt = protocol.stored(runde(config), sitzung_id, RUECKBLICK)
    assert abgelegt.text == RUECKBLICK_TEXT


def test_ohne_rueckblick_kommt_nichts_zurueck(config, scope):
    sitzung_id = sitzung(scope)
    save(scope, sitzung_id, CHRONIK, STAND)

    assert protocol.stored(runde(config), sitzung_id, RUECKBLICK) is None
