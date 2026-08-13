"""Der Ablauf einer Aufnahme: ansagen, protokollieren, je Sprecher eine Spur.

Die Reihenfolge ist der Kern und keine Höflichkeit: **erst ist die Ansage zu Ende
gespielt, dann beginnt der Mitschnitt.** ``schreiben`` weigert sich, bevor die Ansage
protokolliert ist — ein Aufrufer, der die Reihenfolge dreht, bekommt einen Fehler und
keine Datei.

Discord trennt die Audiodaten ohnehin je Client; damit entfällt die Sprechertrennung
nicht bloß billiger, sondern exakt. Geschrieben wird **eine Datei je Sprecher für die
ganze Sitzung**, im Strom auf die Platte. Kein Puffer im Speicher: vier Stunden Runde
sind je Spur ein knappes Gigabyte, und in kleine Schnipsel geschnitten verlöre die
Erkennung genau den Kontext, von dem sie lebt.

Die fertige Spur reiht sich in dieselbe Warteschlange ein wie ein Diktat-Upload — einen
zweiten Verarbeitungsweg gibt es nicht. Der Empfangstest ist davon keine Ausnahme: er
schneidet mit wie jeder andere Lauf, Ansage und Protokoll eingeschlossen, und **wirft**
danach weg, statt einzureihen. Gelöscht wird eine Aufnahme nur auf ausdrückliches
Verlangen (``python -m chronicle.transcribe --loeschen``).

Diese Datei kennt Discord nicht. Sie spricht mit einer ``Stimme``; wer die ist, entscheidet
``gateway.py``, und in den Tests ist es eine Attrappe.
"""

from __future__ import annotations

import asyncio
import logging
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from werkzeug.utils import secure_filename

from chronicle import consent, lebenszyklus, notes, recordings
from chronicle.bot import BotFehler, ansage
from chronicle.config import Config
from chronicle.runde import Runde

logger = logging.getLogger(__name__)

OHNE_SITZUNG = "Noch keine Sitzung angelegt — leg eine an, dann schneide ich mit."
FREMDE_RUNDE = (
    "Dieser Sprachkanal gehört zu einem anderen Server als die Runde, für die ich "
    "schreiben soll — hier nehme ich nichts auf."
)
NICHT_ANGESAGT = "Es wurde noch nichts angesagt — ohne Ansage wird nichts geschrieben."

# Die Ansage lief, aber nicht dort, wo sie gemeint war. Wer im Ursprungskanal saß, hat
# nichts gehört; wer im neuen sitzt, wurde nie gefragt. Ein Eintrag daraus behauptete eine
# Zustimmung, die niemand geben konnte — also entsteht keiner und es wird nicht gestartet.
VERSCHOBEN_BEIM_START = (
    "Während der Ansage hat mich jemand in einen anderen Sprachkanal gezogen — gehört hat "
    "sie damit niemand, der gemeint war. Ich habe nichts protokolliert und schneide nicht "
    "mit. Gebt `/aufnahme start` noch einmal, in dem Kanal, in dem aufgenommen werden soll."
)
GESTARTET = (
    "Die Ansage ist durch, ich schneide jetzt mit — je Sprecherin und Sprecher eine "
    "eigene Spur. Wer nicht aufgezeichnet werden möchte, verlässt den Sprachkanal; "
    "außerhalb nehme ich nichts auf. Beendet wird mit `/aufnahme stop`, und "
    "`/aufnahme hilfe` sagt den Rest."
)
NICHTS_GESPROCHEN = "Es hat niemand gesprochen — keine Spur abgelegt."
EINGEREIHT = "Spur »{spur}« → Sitzung {sitzung}, wartet auf den Stapel."

# Eine Spur ohne Zeile in der Warteschlange ist für jeden Aufräumweg unsichtbar: sie wird
# weder verschriftet noch nach der zugesagten Frist gelöscht. Deshalb wird gesagt, welche
# es traf — eine Stimme, die nur im Log fehlt, fehlt niemandem auf.
NICHT_EINGEREIHT = (
    "Diese Spuren sind nicht eingereiht: {spuren}. Sie liegen noch da — bitte einmal "
    "`/aufnahme stop` geben, das holt genau sie nach; die übrigen sind schon durch."
)

# Eine Aufnahme läuft Stunden; in der Zeit kann ihre Runde gelöscht und ihre Kennung an
# eine fremde Gilde neu vergeben worden sein. Was dann geschrieben würde, wären
# Einwilligungsprotokolle mit den Anzeigenamen dieser Gruppe und ihre Tonspuren — in der
# Kampagne einer anderen. Deshalb wird vor jedem Schreiben nachgesehen.
RUNDE_FORT = (
    "Die Runde, für die ich mitgeschnitten habe, gibt es nicht mehr — die Spuren sind "
    "gelöscht, und abgelegt wurde nichts."
)

# Hier bleiben die Namen weg, anders als bei NICHT_EINGEREIHT: die Runde ist fort, ihre
# Kennung kann längst an eine fremde Gilde vergeben sein — die Anzeigenamen dieser Gruppe
# gingen dann an eine andere.
RUNDE_FORT_REST = (
    "Die Runde, für die ich mitgeschnitten habe, gibt es nicht mehr — abgelegt wurde "
    "nichts. Einige Spuren ließen sich aber nicht löschen und liegen noch da; bitte "
    "einmal `/aufnahme stop` geben, das versucht es erneut."
)


# Wie lange der Empfangstest lauscht. Zehn Sekunden, aus drei Gründen: es reicht für zwei
# gesprochene Sätze, also dafür, dass »null Pakete« wirklich »null Pakete« heißt und nicht
# bloß eine Sprechpause; py-cord ordnet die SSRC eines Sprechers erst mit dem ersten
# Sprech-Ereignis einem Konto zu, und das braucht nach dem ersten Wort einen Moment; und es
# bleibt an der Grenze, ab der ein Lauf ein eigener, beobachtbarer Job sein müsste — der
# Aufrufer wartet in Discords »denkt nach …« und bekommt danach seine Antwort.
PROBE_DAUER = 10

PROBE_KOPF = "**Empfangstest in #{kanal} — {dauer} Sekunden gelauscht**"
PROBE_ZAHLEN = "Pakete: {pakete} · Spuren: {spuren}"
PROBE_SPUR = "• {name}: {bytes} Bytes"

PROBE_TRAEGT = (
    "**Der Empfang trägt.** Der Ton kommt an, lässt sich dekodieren und landet je Sprecher "
    "in einer eigenen Spur — `/aufnahme start` schneidet hier wirklich mit."
)

# Der Fall, für den es diesen Befehl gibt: der Empfänger hört in seinem eigenen Faden auf
# und beendet den Mitschnitt von selbst. In Discord sieht das aus wie eine laufende
# Aufnahme — bis Stunden später die Chronik fehlt. Warum er aufhörte, sagt py-cord uns
# nicht mehr: seit dem festgenagelten Stand bekommt der Abschluss-Rückruf den Fehler nicht
# mehr gereicht, und ohne Zusatzargumente ruft py-cord ihn gar nicht erst auf. Was hier
# steht, ist deshalb das, was wir wirklich wissen — dass aufgehört wurde, nicht warum.
PROBE_ABGEBROCHEN = (
    "**Es kommt nichts Lesbares an.** Der Mitschnitt hat von selbst aufgehört, bevor die "
    "{dauer} Sekunden um waren; was danach gesprochen wurde, stünde in keiner Spur. Den "
    "Grund nennt py-cord uns nicht — er steht im Log des Bots."
)

# Keine Null an dieser Stelle: auf dem festgenagelten Stand fängt py-cord einen
# Dekodierfehler an Ort und Stelle ab und füllt die Lücke mit Paketverlust-Verschleierung.
# Gezählt wird dabei nichts, was hier ankäme — eine »0« hieße also nicht »nichts verloren«,
# sondern nur »wir sehen es nicht«.
PROBE_RAHMENVERLUST = (
    "Verlorene Rahmen zählt dieser Test nicht mit: py-cord fängt einen Dekodierfehler an "
    "Ort und Stelle ab und füllt die Lücke — ein Schlüsselwechsel kostet dann Millisekunden "
    "statt der ganzen Aufnahme. Sichtbar wird das allein als Warnung im Log des Bots."
)

PROBE_STILL = (
    "**Es kam kein einziges Paket an.** Entweder hat in den {dauer} Sekunden niemand "
    "gesprochen — dann noch einmal, und dabei reden —, oder Discord schickt keinen Ton "
    "hierher."
)

PROBE_AUFGERAEUMT = (
    "Die Probespuren sind gelöscht, eingereiht wurde nichts, und in die Chronik geht davon "
    "nichts. Protokolliert ist allein die Ansage — dass hier {dauer} Sekunden aufgezeichnet "
    "wurde, bleibt nachweisbar."
)

PROBE_AUFGERAEUMT_REST = (
    "Eingereiht wurde nichts, und in die Chronik geht davon nichts. Diese Probespuren "
    "ließen sich aber nicht löschen: {spuren}. Sie liegen noch im Aufnahmeordner und "
    "gehören dort nicht hin — der Grund steht im Log des Bots."
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Kanal:
    guild_id: str
    id: str
    name: str


@dataclass(frozen=True)
class Spurbericht:
    name: str
    bytes: int
    geloescht: bool


@dataclass(frozen=True)
class Probe:
    """Was in der Probezeit wirklich ankam — Zahlen, keine Vermutung."""

    kanal: str
    dauer: int
    pakete: int
    abgebrochen: bool
    spuren: tuple[Spurbericht, ...]


class AufnahmeFehler(BotFehler):
    """Was Start oder Abschluss verhindert — eine fehlende Sitzung etwa."""


class NichtAngesagt(BotFehler):
    """Der Versuch, vor der Ansage zu schreiben."""


class Stimme(Protocol):
    """Die ganze Abhängigkeit dieser Stufe zu Discord."""

    kanal: Kanal

    def mitglieder(self) -> tuple[consent.Member, ...]: ...

    def im_kanal(self) -> bool: ...

    async def ansagen(self, datei: Path) -> None: ...

    def mitschneiden(self, aufnahme: Aufnahme) -> None: ...

    def mitschnitt_beenden(self) -> bool: ...

    async def trennen(self) -> None: ...


class _Spur:
    """Ein Sprecher, eine Datei, im Strom geschrieben.

    ``writeframes`` schreibt den WAV-Kopf bei jedem Aufruf mit fort. Stirbt der Prozess
    mitten in der Sitzung, liegt trotzdem eine abspielbare Spur da statt eines Rumpfes.
    """

    def __init__(self, pfad: Path, sprecher: str) -> None:
        self.pfad = pfad
        # Der Anzeigename, nicht der Dateiname: der Empfangstest sagt dem Aufrufer, von wem
        # etwas ankam, und ``sitzung3-20260811T2030-Mira`` beantwortet das schlechter.
        self.sprecher = sprecher
        self.bytes = 0
        self._datei = wave.open(str(pfad), "wb")
        self._datei.setnchannels(ansage.KANAELE)
        self._datei.setsampwidth(ansage.BREITE)
        self._datei.setframerate(ansage.RATE)

    def schreiben(self, pcm: bytes) -> None:
        self._datei.writeframes(pcm)
        self.bytes += len(pcm)

    def schliessen(self) -> None:
        self._datei.close()


def _loeschen(spur: _Spur) -> bool:
    """Löscht die Spur und sagt, ob es gelang — der Fehlschlag kommt ohne Traceback ins Log.

    ``unlink`` hängt den Pfad an seinen ``OSError``, und der Pfad trägt den Anzeigenamen des
    Sprechers. Ein ``logger.exception`` schriebe ihn mitsamt Traceback ins Log; ``strerror``
    nennt allein den Grund.
    """
    try:
        spur.pfad.unlink(missing_ok=True)
    except OSError as fehler:
        logger.warning("Eine Spur ließ sich nicht löschen: %s", fehler.strerror)
        return False
    return True


def _spurname(sprecher: consent.Member) -> str:
    # Der Anzeigename kann aus Zeichen bestehen, die kein Dateiname sein können; dann
    # bleibt die Id. Wer wirklich gesprochen hat, steht ohnehin im Einwilligungsprotokoll.
    return f"{secure_filename(sprecher.name) or f'sprecher-{sprecher.id}'}.wav"


class Aufnahme:
    def __init__(self, config: Config, runde: Runde, session_id: int, kanal: Kanal) -> None:
        self.session_id = session_id
        self.kanal = kanal
        self.runde = runde
        self._config = config
        self._spuren: dict[str, _Spur] = {}
        self._angesagt = False
        self.begonnen_at: str | None = None
        # Nur gezählt, nicht gedeutet: wie oft überhaupt etwas zu schreiben ankam. Das ist
        # die Frage, die der Empfangstest beantwortet — und die einzige, die man von außen
        # nicht sehen kann.
        self.pakete = 0

    @property
    def laeuft(self) -> bool:
        return self._angesagt

    def _gemeint(self) -> Runde:
        gemeint = lebenszyklus.dieselbe(self.runde)
        if gemeint is None:
            raise AufnahmeFehler(RUNDE_FORT)
        return gemeint

    def ansage_protokollieren(
        self, mitglieder: tuple[consent.Member, ...], *, art: str = consent.ANSAGE
    ) -> int:
        kennung = consent.record(
            self._gemeint(),
            session_id=self.session_id,
            kind=art,
            guild_id=self.kanal.guild_id,
            channel_id=self.kanal.id,
            channel_name=self.kanal.name,
            text=ansage.PROTOKOLL,
            members=mitglieder,
        )
        if art == consent.ANSAGE:
            self._angesagt = True
        logger.info(
            "Einwilligung %s in #%s protokolliert: %s Anwesende",
            art,
            self.kanal.name,
            len(mitglieder),
        )
        return kennung

    def mitschnitt_begonnen(self) -> None:
        """Hält den Nullpunkt der Sitzungsuhr fest — den Moment, ab dem Millisekunde 0 zählt.

        Er wird hier festgehalten und nirgends später hergeleitet: ``uploaded_at`` ist der
        Zeitpunkt des Einreihens und damit das *Ende* der Aufnahme, und die Ansage davor
        dauert je nach Text unterschiedlich lang. Ein aus einem der beiden gerechneter
        Beginn wäre eine geratene Zeitrechnung, und aus ihr fiele jede Äußerung in die
        falsche Szene.
        """
        self.begonnen_at = _now()

    def schreiben(self, sprecher: consent.Member, pcm: bytes) -> None:
        if not self._angesagt:
            raise NichtAngesagt(NICHT_ANGESAGT)
        spur = self._spuren.get(sprecher.id)
        if spur is None:
            self._config.recordings_dir.mkdir(parents=True, exist_ok=True)
            ziel = recordings.target_path(
                self._config.recordings_dir, self.session_id, _spurname(sprecher)
            )
            spur = _Spur(ziel, sprecher.name)
            self._spuren[sprecher.id] = spur
            # Gezählt statt benannt: der Anzeigename ist es, der hier spricht, und der
            # Dateiname trägt ihn im Stamm (#194). Was der Betreiber an dieser Zeile
            # braucht, ist, dass eine weitere Spur aufgemacht wurde und zu welchem Abend —
            # beides sagen zwei Zahlen, die außerhalb dieser Datenbank nichts bedeuten.
            logger.info("Sitzung %s: %s. Spur begonnen", self.session_id, len(self._spuren))
        spur.schreiben(pcm)
        self.pakete += 1

    def beenden(self) -> tuple[str, ...]:
        """Schließt die Spuren und reiht sie ein; leere Spuren bleiben nicht liegen.

        Ist die Runde fort, werden die Spuren geschlossen und **gelöscht**: sie in die
        Runde einzureihen, die inzwischen unter derselben Kennung steht, hieße die Stimmen
        dieser Gruppe in eine fremde Kampagne zu legen. Liegen bleiben dürfen sie auch
        nicht — es gäbe keinen Eintrag mehr, der sie je aufräumt. Auch dort steht jede Spur
        für sich: was sich nicht schließen lässt, wird trotzdem gelöscht, und was sich nicht
        löschen lässt, hält die übrigen nicht auf. Bleibt eine liegen, wird das gesagt statt
        »die Spuren sind gelöscht« zu behaupten, und die Aufnahme behält alle Spuren — ein
        zweites ``/aufnahme stop`` versucht es erneut.

        Jede Spur steht dabei für sich, vom Schließen an: was scheitert, hält die übrigen
        nicht auf, und was ein voriger Anlauf schon eingereiht hat, wird übersprungen statt
        erneut versucht. Bleibt eine liegen, wird sie beim Namen genannt und die Aufnahme
        behält alle Spuren — ein zweites ``/aufnahme stop`` holt dann genau die fehlende
        nach.

        Scheitert das **Schließen**, wird die Spur im selben Durchgang nicht eingereiht:
        was ``close`` nachträgt — das Auffüllbyte bei ungerader Länge und die endgültigen
        Längen im Kopf — fehlt dann, und eine womöglich unvollständige Spur in die
        Verschriftung zu geben, verdeckt den Fehler. Gelöscht wird sie deshalb aber nicht —
        es ist die Stimme eines Menschen, und was zu retten ist, entscheidet nicht diese
        Zeile. Sie wird genannt, und der zweite Anlauf reiht sie ein: ``close`` gibt die
        Datei auch im Fehlerfall frei, ist beim zweiten Mal also still erfolgreich. Damit
        steht sie in der Warteschlange und ist für ``recordings.sweep`` sichtbar — eine
        Spur, deren Verschriftung scheitern mag, ist besser als eine, die niemand findet
        und niemand löscht.
        """
        gemeint = lebenszyklus.dieselbe(self.runde)
        if gemeint is None:
            geblieben = 0
            for spur in self._spuren.values():
                try:
                    spur.schliessen()
                except Exception:
                    logger.exception("Eine Spur ließ sich nicht abschließen")
                # Auch die nicht geschlossene Spur wird gelöscht: ``close`` gibt die Datei
                # im ``finally`` frei, und hier ist Löschen das Ziel, nicht Einreihen.
                if not _loeschen(spur):
                    geblieben += 1
            logger.warning(
                "Aufnahme ohne Runde beendet: %s Spuren gelöscht, %s liegengeblieben",
                len(self._spuren) - geblieben,
                geblieben,
            )
            if geblieben:
                raise AufnahmeFehler(RUNDE_FORT_REST)
            self._spuren.clear()
            self._angesagt = False
            return (RUNDE_FORT,)
        meldungen = []
        liegengeblieben = []
        for user_id, spur in self._spuren.items():
            try:
                spur.schliessen()
                if not spur.bytes:
                    # Ein Fehlschlag wird hier nicht der Runde gemeldet: eine leere Spur
                    # wird nie eingereiht, ein zweiter Anlauf scheiterte identisch weiter —
                    # die Zusage aus NICHT_EINGEREIHT wäre eine falsche.
                    _loeschen(spur)
                    continue
                try:
                    recordings.enqueue(
                        gemeint,
                        self.session_id,
                        spur.pfad.name,
                        discord_user_id=user_id,
                        started_at=self.begonnen_at,
                    )
                except recordings.BereitsEingereiht:
                    pass
                meldungen.append(EINGEREIHT.format(spur=spur.pfad.stem, sitzung=self.session_id))
            except Exception:
                # Ohne den Dateinamen: er trägt den Anzeigenamen des Sprechers, und für den
                # Grund braucht ihn der Traceback nicht. Welche Spur es traf, sagt die
                # Meldung an die Runde.
                logger.exception("Eine Spur ließ sich nicht abschließen oder einreihen")
                liegengeblieben.append(spur.pfad.stem)
        if liegengeblieben:
            raise AufnahmeFehler(NICHT_EINGEREIHT.format(spuren=", ".join(liegengeblieben)))
        self._spuren.clear()
        self._angesagt = False
        return tuple(meldungen) or (NICHTS_GESPROCHEN,)

    def verwerfen(self) -> tuple[Spurbericht, ...]:
        """Schließt die Spuren, sagt was ankam — und löscht sie. Eingereiht wird nichts.

        Der Ausgang des Empfangstests, und der einzige Weg hier heraus, der **nicht** in
        die Warteschlange führt: was in der Probezeit ankam, war nie für die Chronik
        gedacht. Es sind trotzdem Stimmen echter Menschen, also verschwinden sie sofort und
        nicht erst mit der Frist — auch die leeren, auch die, deren Schließen scheiterte.

        Wie beim Beenden steht jede Spur für sich, und was liegen bleibt, wird gesagt: eine
        Probespur ohne Zeile in der Warteschlange ist für jeden Aufräumweg unsichtbar.
        """
        berichte = []
        for spur in self._spuren.values():
            try:
                spur.schliessen()
            except Exception:
                # Ohne den Dateinamen: er trägt den Anzeigenamen des Sprechers. Gelöscht
                # wird die Spur trotzdem — ``close`` gibt die Datei im ``finally`` frei.
                logger.exception("Eine Probespur ließ sich nicht abschließen")
            berichte.append(
                Spurbericht(name=spur.sprecher, bytes=spur.bytes, geloescht=_loeschen(spur))
            )
        logger.info("Empfangstest beendet: %s Spuren verworfen", len(berichte))
        self._spuren.clear()
        self._angesagt = False
        return tuple(berichte)


async def starten(config: Config, stimme: Stimme, runde: Runde) -> Aufnahme:
    """Ansage spielen, Einwilligung protokollieren, dann erst mitschneiden.

    Die Runde kommt vom Befehl und gehört der Gilde, in der er gegeben wurde; hier wird
    nur noch nachgesehen, ob sie auch die des Sprachkanals ist. Einen Rückfall auf »die
    erste Runde« gibt es nicht mehr: er ließe Ansage, Einwilligungsprotokoll samt
    Anzeigenamen, Tonspuren und Transkripte in einer fremden Kampagne landen.

    Zwischen Ansage und Eintrag wird nachgesehen, ob der Bot noch dort sitzt, wo er
    angefangen hat — dieselbe Bedingung wie beim Nachzügler, und hier trägt sie alles:
    der Eintrag nennt Kanal und Besetzung des Ursprungs, und beides wäre gelogen, hätte
    die Ansage nebenan gespielt. Dann bricht der Start ab, statt einen Lauf zu
    hinterlassen, der weder schreibt noch etwas sagt.
    """
    if runde.guild_id != stimme.kanal.guild_id:
        raise AufnahmeFehler(FREMDE_RUNDE)
    sitzung = notes.latest_session(runde)
    if sitzung is None:
        raise AufnahmeFehler(OHNE_SITZUNG)
    gesprochen = ansage.datei(config.recordings_dir, tts_url=config.tts_url)

    aufnahme = Aufnahme(config, runde, sitzung.id, stimme.kanal)
    await stimme.ansagen(gesprochen)
    if not stimme.im_kanal():
        raise AufnahmeFehler(VERSCHOBEN_BEIM_START)
    aufnahme.ansage_protokollieren(stimme.mitglieder())
    stimme.mitschneiden(aufnahme)
    aufnahme.mitschnitt_begonnen()
    return aufnahme


async def nachzuegler(
    config: Config, stimme: Stimme, aufnahme: Aufnahme, wer: consent.Member
) -> int | None:
    """Wer später dazukommt, hört dieselbe Ansage noch einmal — und steht im Protokoll.

    Das ist die ehrlichere Hälfte der Wahl: bloß zu vermerken, dass jemand die Ansage
    verpasst hat, hielte fest, dass er nicht eingewilligt hat, statt ihn zu fragen.

    Erst gespielt, dann protokolliert, und dazwischen nachgesehen, ob der Bot noch dort
    ist, wo die Aufnahme läuft: der Eintrag ist der Nachweis einer *gehörten* Ansage.
    Hat sie in einem anderen Kanal gespielt — weil jemand den Bot inzwischen gezogen hat
    —, entsteht keiner. Ein Protokoll, das eine Zustimmung behauptet, die niemand geben
    konnte, ist schlimmer als eine Lücke.
    """
    await stimme.ansagen(ansage.datei(config.recordings_dir, tts_url=config.tts_url))
    if not stimme.im_kanal():
        logger.warning("Die Ansage für einen Nachzügler lief woanders — kein Eintrag.")
        return None
    return aufnahme.ansage_protokollieren((wer,), art=consent.NACHZUEGLER)


async def stoppen(stimme: Stimme, aufnahme: Aufnahme) -> tuple[str, ...]:
    stimme.mitschnitt_beenden()
    await stimme.trennen()
    return aufnahme.beenden()


async def pruefen(config: Config, stimme: Stimme, runde: Runde) -> Probe:
    """Kurz lauschen und sagen, was wirklich ankam — ohne eine Zeile für die Chronik.

    Denselben Weg wie ``starten``, und keinen leiseren: erst die hörbare Ansage, dann das
    Einwilligungsprotokoll, dann der Mitschnitt. Ein Testmodus, der still mitschnitte, wäre
    eine Straftat (§201 StGB) — dass die Spuren zehn Sekunden später gelöscht werden, ändert
    daran nichts, denn aufgezeichnet wurde trotzdem.

    Aufgeräumt wird auch der gescheiterte Lauf: getrennt und verworfen wird im ``finally``,
    weil sonst genau dann Spuren liegenblieben, wenn niemand hinsieht.

    Ob der Empfang durchgehalten hat, sagt ``mitschnitt_beenden``: lief er nicht mehr, hat
    py-cord ihn selbst beendet. Das ist das einzige Signal, das der festgenagelte Stand
    dafür noch hergibt — und es kommt vor dem Trennen, weil das Trennen ihn ohnehin beendet.
    """
    aufnahme = await starten(config, stimme, runde)
    logger.info("Empfangstest in #%s: %s Sekunden", stimme.kanal.name, PROBE_DAUER)
    try:
        await asyncio.sleep(PROBE_DAUER)
    finally:
        try:
            lief_noch = stimme.mitschnitt_beenden()
            await stimme.trennen()
        finally:
            spuren = aufnahme.verwerfen()
    return Probe(
        kanal=stimme.kanal.name,
        dauer=PROBE_DAUER,
        pakete=aufnahme.pakete,
        abgebrochen=not lief_noch,
        spuren=spuren,
    )


def _urteil(probe: Probe) -> str:
    # Der Abbruch steht vor der Paketzahl: hört der Empfang nach der Hälfte auf, sind die
    # Zahlen davor sämtlich in Ordnung — und ein »trägt« daneben wäre die eine Auskunft,
    # die dieser Befehl niemals geben darf.
    if probe.abgebrochen:
        return PROBE_ABGEBROCHEN.format(dauer=probe.dauer)
    if not probe.pakete:
        return PROBE_STILL.format(dauer=probe.dauer)
    return PROBE_TRAEGT


def bericht(probe: Probe) -> str:
    """Die Zahlen, das Urteil und der Verbleib der Spuren — in dieser Reihenfolge.

    Die Zahlen stehen vor dem Urteil, damit nachprüfbar bleibt, worauf es sich stützt.
    """
    zeilen = [
        PROBE_KOPF.format(kanal=probe.kanal, dauer=probe.dauer),
        PROBE_ZAHLEN.format(pakete=probe.pakete, spuren=len(probe.spuren)),
    ]
    zeilen.extend(PROBE_SPUR.format(name=spur.name, bytes=spur.bytes) for spur in probe.spuren)
    zeilen.append(_urteil(probe))
    zeilen.append(PROBE_RAHMENVERLUST)
    geblieben = [spur.name for spur in probe.spuren if not spur.geloescht]
    if geblieben:
        zeilen.append(PROBE_AUFGERAEUMT_REST.format(spuren=", ".join(geblieben)))
    else:
        zeilen.append(PROBE_AUFGERAEUMT.format(dauer=probe.dauer))
    return "\n".join(zeilen)
