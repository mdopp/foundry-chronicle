# Wie Foundry ausgelesen wird

Kern-Foundry bringt **keine** Query-API mit. Der Zugriff läuft über denselben Weg, den
der Browser-Client nimmt: Login per HTTP, dann Socket.io. Das ist kein dokumentiertes
API, sondern aus dem Client-Bundle nachgebaut — entsprechend vorsichtig behandeln.

Beobachtet an einer laufenden Welt am 2026-05-06: **Foundry 13.351**, System
**Daggerheart 1.9.5**. Grundlage ist eine funktionierende Referenz-Implementierung
(`tools/foundry-pull` in Daniels Daggerheart-Repo, Node.js) — nicht Teil dieses Repos.

## Der Handschlag

Vier Schritte, alle nötig, in dieser Reihenfolge:

1. `GET /join` → Foundry setzt einen `session`-Cookie, auch ohne Anmeldung.
2. Socket.io mit diesem Cookie verbinden und `getJoinData` senden → liefert die
   **Benutzerliste mit IDs**, ohne Anmeldung. Der nächste Schritt braucht die ID, nicht
   den Namen.
3. `POST /join` mit `{action: "join", userid, password}` als JSON, den Cookie im Header
   → bindet die Sitzung an den Benutzer. Antwort setzt einen neuen Cookie.
4. Socket.io **neu** verbinden, mit neuem Cookie *und* `?session=<id>` in der Query. Der
   Server schickt ein `session`-Event mit `userId`; stimmt die nicht mit der erwarteten
   überein, hat die Anmeldung nicht durchgereicht. Dann `socket.emit("world", cb)`.

Der Rückruf liefert die komplette Welt in einem Stück. In der beobachteten Welt waren
das 15,7 MB.

## Was zurückkommt

Ein Objekt mit diesen Sammlungen:

```
actors  items  journal  scenes  macros  playlists
tables  cards  folders  users   messages  combats  settings
```

Für die Chronik zählen **`messages`** (das Chat-Log), **`actors`** (Spielfiguren und
NSCs) und **`combats`** (Kampfzustand). Der Rest ist für uns Beifang.

Daneben stehen zwei Kopfblöcke, die keine Sammlung sind: **`system`** (`{id, ...}` —
welches Regelwerk) und **`world`** (`{id, title, ...}` — welche Welt gerade offen ist).

## Drei Fallen

### Der Server filtert nicht

Foundry filtert `world` **nicht** serverseitig. Jeder angemeldete Benutzer bekommt die
vollständigen Weltdaten; die Filterung passiert erst im Client beim Rendern. Wer den
Rohdump speichert, speichert also GM-Inhalte, ungespielte Handlungsstränge und die
Klarnamen aller Beteiligten.

Die Filterung muss deshalb bei uns nachgebaut werden. Foundry-Berechtigungsstufen:

| Stufe | Bedeutung | Was wir übernehmen |
|---|---|---|
| 0 `NONE` | unsichtbar | nichts |
| 1 `LIMITED` | kennt Name und Existenz | höchstens den Namen |
| 2 `OBSERVER` | darf einsehen | alles |
| 3 `OWNER` | gehört ihm | alles |

Die wirksame Stufe ist `max(ownership.default, ownership[userId])`. Bei Journalen erbt
die Seite vom Dokument: `max(Dokument, Seite)`.

### Bei Würfen ist `content` leer

Die naheliegende Annahme — im Chat-Log steht der Text, den man im Spiel gelesen hat —
ist falsch. Bei Wurf-Nachrichten ist `content` ein leerer String. Foundry rendert diesen
Text erst im Client aus System-Vorlagen; serverseitig existiert er nicht.

Die Zahlen stehen stattdessen fertig aufbereitet in `message.system.roll`:

```json
{ "title": "Knowledge Roll", "total": 7, "formula": "1d12 + 1d12 + 3",
  "type": "action", "isCritical": false, "modifierTotal": 3,
  "hope": { "dice": "d12", "value": 3 }, "fear": { "dice": "d12", "value": 1 } }
```

Wer stattdessen `rolls[]` auswertet, bekommt dieselben Zahlen roh in `terms[]`, verpackt
in Würfel-Darstellungsdaten. Der `system`-Block ist der bessere Einstieg.

> **Zurückgezogen — der Nachtrag vom 2026-08-07 galt nie.** Er hielt fest,
> `message.system.roll` sei in einer beobachteten Welt »kein einziges Mal« vorgekommen,
> und nannte `read_roll` deshalb eine offene Lücke. Beides stimmte nicht: die Welt, an
> der das gemessen wurde, war eine nachgebaute Fixture, die den Block schlicht nicht
> trug. In der Auszählung eines echten Abzugs tragen **sieben von acht** Nachrichten
> einen vollständigen `system.roll`. Der Satz darüber gilt unverändert; der Adapter
> braucht keinen zweiten Einstieg.
>
> Stehen bleibt eine Beobachtung daraus, weil sie unabhängig davon zutrifft: `rolls[0]`
> ist ein JSON-**String** und trägt unter `options.roll` dieselben Felder noch einmal,
> die Wurfklasse steht darin in `class` (`DualityRoll`, `D20Roll`, `DamageRoll`, …).
> Die eingebaute Testwelt führt beide Ablagen, damit ein zweiter Einstieg — falls ihn
> je eine Welt nötig macht — Material hätte.

Wer die Nachricht einer Figur zuordnen will, nimmt `speaker.actor` und `speaker.alias` —
nicht `author`, das ist der Benutzer hinter der Figur.

### Der Wurf-Block ist regelwerksspezifisch

`hope` und `fear` sind die Daggerheart-Dualitätsmechanik, `type: "dualityRoll"` ebenso.
D&D 5e und PF2e legen dort etwas anderes ab. An die Zahlen kommt man **nur** durch die
System-Interna — die Anbindung kann an dieser Stelle nicht regelwerksneutral bleiben.

Die Trennlinie liegt deshalb nicht davor, sondern dahinter: ein **dünner Adapter je
System** bildet auf ein gemeinsames Modell ab, und ab dort weiß nichts mehr, welches
Regelwerk gespielt wurde.

### Ein Server hostet nur eine Welt

Foundry hat immer genau eine Welt offen. Wer mehrere Runden auf einem Server fährt,
wechselt sie zwischen den Abenden — und ein Abgleich zöge dann das Chat-Log der falschen
Kampagne in die falsche Chronik. Die Runde merkt sich deshalb beim ersten gelungenen
Abgleich `world.id` und `world.title`; jeder spätere vergleicht und **verweigert sich bei
Nichtübereinstimmung**. Umgehängt wird nur ausdrücklich (`sync(..., umhaengen=True)`).

Ein Dump ohne `world`-Block lässt sich nicht vergleichen und wird nicht als Wechsel
gewertet — eine Verweigerung ohne Beleg brächte den Abgleich zum Erliegen, statt vor
etwas zu schützen.

## Anmeldung ist Benutzer und Passwort

Es gibt keinen API-Token. Der Zugang ist das Passwort eines echten Foundry-Kontos, und
er trägt dessen Berechtigungen — das Protokoll sieht damit genau so viel wie dieser
Mensch. Ein eigenes Konto mit passenden Rechten — etwa »Chronik« — ist sauberer als das
eines Mitspielers.

**Welches Konto es ist, ist eine Entscheidung der Gruppe** (#78), keine Nebenwirkung
dessen, wer zufällig sein Passwort hergibt: ein Spielerkonto zeigt, was die Runde erlebt
hat, ein Konto der Spielleitung auch ungespielte Handlungsstränge, verdeckte NSCs und
Fallen. Für ein Sitzungsprotokoll ist das Spielerkonto fast immer richtig. Deshalb sagt
die Einrichtung das am Benutzerfeld, und nicht nur hier.

### »Chronik« mit denselben Rechten — das ist Handarbeit je Figur

Ein neu angelegtes Konto erbt **nichts**. Foundry hängt die Sicht auf ein Dokument an
dessen `ownership`-Karte, und die wirksame Stufe ist `max(ownership.default,
ownership[userId])` — steht das neue Konto in keiner Karte, bleibt ihm nur, was `default`
ohnehin für alle offen lässt. Das ist in einer gewachsenen Welt fast nichts: **ausgeführt
gegen die Testwelt sieht ein solches Konto 29 von 91 Figuren, davon 20 nur namentlich —
also 9 echte.** Ein gewöhnliches Spielerkonto sieht 33–52, ein GM-Konto alle 91. Wer
»Chronik« naiv anlegt, bekommt also **weniger** als hätte er das Konto eines Mitspielers
genommen.

Die Rechte werden deshalb **je Figur** nachgetragen, in Foundry unter *Actors* →
Rechtsklick auf die Figur → *Configure Ownership*: dort für »Chronik« mindestens
**Observer** setzen. `Limited` genügt nicht — daraus übernehmen wir nach der Tabelle oben
nur den Namen, und eine Figur ohne Werte trägt keine Chronik. Ebenso wirkt es, `default`
der Figur anzuheben; das gibt sie dann allerdings **allen** Konten frei, auch denen, die
sie bisher nicht sahen.

Für das Chat-Log gilt das nicht: Geflüstertes und blinde Würfe hängen an `whisper` und
`blind` der Nachricht, nicht an einer Figur. Was »Chronik« davon zu sehen bekommt,
entscheidet sich damit nachrichtenweise und lässt sich nicht nachträglich freigeben.

**Das Passwort wird nirgends gespeichert** (#64): nicht in der SQLite, nicht in einer
Umgebungsvariable, nicht in einer Datei. Gefragt wird beim **Sitzungsstart**, damit
Foundry den Abend über offen steht (#96) — freiwillig; ohne es läuft die Sitzung ohne
Zahlen weiter, und der Abschluss fragt noch einmal. Es lebt bis dahin im Arbeitsspeicher
(`chronicle.zugang`), verfällt dort nach zwölf Stunden und wird vom Abgleich verbraucht —
auch vom gescheiterten und auch von dem, der auf der Testwelt oder an einer ruhenden Runde
abbricht, bevor er einen Server sieht. Hashen ginge nicht: wir müssen es vorzeigen, nicht
prüfen. Und es steht in keinem Aufrufargument (landet sonst in der Shell-History) und in
keiner Logzeile.

**Gefragt wird nur, wo es auch vorgezeigt würde.** Eine Runde auf der Testwelt oder ohne
eingetragenen Zugang bekommt kein Fenster: ein Geheimnis einzusammeln, das nirgends
hingeht, ist der schlechteste Tausch — es läge nur bis zur Frist im Speicher. »Zugang«
heißt dabei dasselbe wie im Client (`config.foundry_configured`): **Adresse und Konto**.
Eine Adresse ohne Konto ergäbe sonst ein Fenster, dessen Eingabe ein `FoundryError`
verbraucht, bevor der erste Byte über die Leitung geht.

**Und nur, wer es hinterlegt hat, überspringt die Frage.** Seit das Passwort beim Start
kommt, sind Hinterlegen und Verbrauchen zwei Handlungen, und `/chronik start` steht jedem
Mitglied offen. Neben dem Passwort liegt deshalb die Discord-Kennung dessen, der es gab
(`zugang.gemerkt_von`); `/chronik fertig` nimmt den kurzen Weg nur für genau diese Person.
Jeder andere bekommt das Fenster — und damit sofort den Weg zum eigenen Passwort, ohne die
zwölf Stunden abzuwarten. Verworfen wird nichts: eine spätere Eingabe überschreibt die
frühere, sie wird nur nie *ungefragt* dem Foundry-Konto der Runde vorgezeigt. Eine
Discord-Kennung ist im Kanal ohnehin sichtbar und damit selbst kein Geheimnis.

**Und geprüft wird, was auch benutzt wird.** Der Befehl liest das Passwort in einem Zug
mit der Kennung (`zugang.passwort_von`) und reicht den gelesenen Wert bis zum Abgleich
durch. Der Auftrag läuft in einem eigenen Faden; sähe er dort selbst im Merkzettel nach,
könnte zwischen Prüfen und Benutzen ein zweites Fenster genau die Zeichenkette
hineingeschoben haben, die der Befehl gerade abgelehnt hat. Nur wo nichts mitgebracht
wurde, liest der Abgleich den Merkzettel wie eh und je — der nächtliche Lauf etwa.

## Die Testwelt: erzeugen, abgreifen, anonymisieren, abspielen

Der Server gehört der Spielleitung und ist zwischen den Sitzungen meistens aus. Damit
Filter, Adapter und Zwischenspeicher trotzdem gegen eine Welt in echtem Umfang geprüft
werden können, liegt eine im Paket. Sie ist **erzeugt, nicht abgegriffen** — und daneben
steht der Weg für eine eigene Welt, der nie im Repo endet.

### 1. Die eingecheckte Testwelt erzeugen

```
python scripts/erzeuge_testwelt.py
```

`src/chronicle/foundry/testwelt.json` fällt Zeichen für Zeichen aus diesem Skript heraus,
und `tests/test_testwelt.py` hält das als Dauergate fest. Kein Wert darin kommt aus einer
echten Welt: keine Kennung, kein Zeitstempel, kein Name, kein Welttitel. Geteilt wird nur
die **Form** — Schlüssel je Ebene, Figurentypen, die Verteilung der `ownership`-Stufen,
die vier Rollenstufen, Nachrichten mit und ohne `system.roll`, eine geflüsterte und eine
blinde, und Würfe mit Hoffnung, Furcht und Kritischem in beiden Ablagen.

> **Warum nicht einfach eine echte Welt pseudonymisieren?** Weil auch eine
> pseudonymisierte Welt die Welt einer echten, privaten Gruppe bleibt: Konten-Graph,
> Berechtigungsverteilung, `users[].character` und die Zeitstempel sagen weiterhin, wer
> wann wie lange mit wem spielt. Das ist pseudonymes Verhaltensdatum und gehört in kein
> veröffentlichtes Image.

### 2. Eine eigene Welt abgreifen

```
python -m chronicle.foundry --dump            # eine Instanz mit einer Runde
python -m chronicle.foundry --runde 2 --dump  # eine Instanz mit mehreren
```

Derselbe Handschlag wie ein Abgleich, aber die **rohe** Antwort geht in eine Datei: kein
Berechtigungsfilter, keine Feldauswahl, nichts in der SQLite. Das Ziel ist nicht wählbar
— es ist `dumps/welt-dump.json`, mit Rechten `0600` in einem Ordner mit `0700`. Ein frei
angegebener Pfad legte den Abzug bei einem Tippfehler neben den Quelltext, wo ihn keine
Ignore-Regel mehr fängt.

Der Lauf schreibt in seine erste Zeile, aus **welcher** Runde er zieht, und verlangt
`--runde`, sobald die Instanz mehr als eine trägt. Stillschweigend die erste zu nehmen
hieße auf einer Instanz mit mehreren Gruppen, den ungefilterten Abzug einer fremden
Gruppe zu ziehen.

> **Diese Datei ist personenbezogen.** Sie trägt die Klarnamen aller Konten der Welt,
> dazu Journale, Charakterbiografien und den Wortlaut jeder Chat-Nachricht. Sie gehört
> **nie** ins Repo, nie in ein Issue und nie in einen Anhang. `welt-dump*.json` und
> `dumps/` stehen deshalb in `.gitignore` — das ist ein Netz, keine Erlaubnis.

### 3. Sie anonymisieren — für den eigenen Rechner, nicht fürs Repo

```
python scripts/anonymisiere_welt.py dumps/welt-dump.json meine-testwelt.json
```

Wer seine eigene Welt lokal durchspielen will, macht daraus eine Fixture derselben Form.
Eingecheckt wird sie trotzdem nicht — dafür ist Schritt 1 da. Das Skript trägt zwei
Regeln, und beide sind nötig:

- **Behalten wird nur, was es ausdrücklich aufzählt — bis ganz nach unten.** Kopfblöcke
  `world`/`system` (ohne den frei vergebenen `title`), Ids, `ownership`, Rollen,
  Sprecherbezüge und die Zahlen eines Wurfs, letztere über einen benannten Bauplan je
  Ebene, auch im eingebetteten JSON von `rolls[]`. Journale, Ordner, Makros, Gegenstände,
  Einstellungen, Module und Charakterbiografien fallen weg. Eine Ausschlussliste wäre nach
  dem nächsten Foundry-Update unvollständig, ohne dass es jemandem auffällt.
- **Was bleibt, wird nachgeprüft — auf Personendaten, nicht nur auf Namen.** Nach dem
  Umschreiben läuft die Ausgabe noch einmal Zeichenkette für Zeichenkette durch. Findet
  sie einen Namen aus der Eingabe, eine E-Mail, eine Adresse oder einen Rechnernamen, eine
  IP, eine telefonnummernförmige Ziffernfolge oder einen Heimatverzeichnis-Pfad, bricht
  der Lauf mit Exit 2 ab und schreibt **nichts**. Der Fund nennt den JSON-Pfad und die
  Art, nie den Wert.

Die Pseudonyme sind bewusst kein Namensvorrat, sondern erfundene Silben mit laufender
Nummer (`Baba-001`). Der erste Anlauf nahm hübsche Fantasienamen — und die Selbstprüfung
hat ihn überführt: hieß eine Figur wie ein Bestandteil eines Pseudonyms, brachte das
Pseudonym sie zurück. Ein Pseudonymraum, der Bruchstücke der Eingabe enthalten *kann*, ist
untauglich.

### 4. Abspielen

Die Quelle der Spieldaten ist eine Einstellung **je Runde**: „Echter Server" oder
„Eingebaute Testwelt". Bei der Testwelt liest der Abgleich die mitgelieferte Fixture und
schickt sie durch **dieselbe** Strecke — Berechtigungsfilter → System-Adapter →
Zwischenspeicher. Kein Netz, kein zweiter Prozess, kein zweiter Weg; sonst prüfte die
Testwelt etwas anderes als den Betrieb.

Zwei Eigenschaften hängen daran:

- **Unübersehbar ehrlich.** Solange die Testwelt aktiv ist, sagen das Band auf jeder
  Seite und die Foundry-Karte: *Testwelt aktiv — das sind keine echten Kampagnendaten.*
  Erfundene Zahlen für echte zu halten ist das Hauptrisiko dieses Schalters.
- **Keine Weltbindung.** Die Runde wird an die Testwelt **nicht** gebunden. Sonst gälte
  das Zurückschalten auf den echten Server als Weltwechsel und der Abgleich müsste sich
  verweigern.

Umschalten ersetzt beim nächsten Abgleich den Zwischenspeicher im Ganzen. Szenen, die auf
Nachrichten der anderen Welt verweisen, laufen dann ins Leere — harmlos, und mit dem
Rück-Abgleich wieder da.

## Was Foundry von sich aus schickt — recherchiert, noch nicht gemessen

Der Handschlag baut eine socket.io-Leitung auf und benutzt sie für **eine** Frage
(`world`). Dieselbe Leitung schickt aber auch von sich aus. Wie diese Ereignisse heißen,
stand hier nicht (#146) — und PR #128 baute darüber einen Takt alle zwei Minuten, also
Polling über eine Push-Leitung.

**Was hier steht, ist recherchiert und nicht an unserer Welt gemessen.** Der Beleg pro
Aussage steht dabei; gemessen wird mit `scripts/lausche_foundry.py` (unten). Bis dahin
gilt es als gut belegte Erwartung, nicht als Beobachtung.

### Ein Ereignis für alles: `modifyDocument`

Jede Änderung an einem Dokument — auch eine neue Chat-Nachricht — kommt als
**`modifyDocument`**. Es gibt kein `createChatMessage` auf dem Socket; was so heißt, ist
ein *Hook* im Browser-Client und nie etwas auf der Leitung. Belegt durch die
API-Referenz zu `ClientDatabaseBackend#onModifyDocument` („Handle a socket response
broadcast back from the server", <https://foundryvtt.com/api/v12/classes/foundry.data.ClientDatabaseBackend.html>)
und durch einen fremden Client, der genau das von außen tut:
<https://github.com/this-gavagai/foundryvtt-tm> hört `modifyDocument`, `userActivity`
und `progress` und führt `ChatMessage` als Dokumenttyp.

Die Nutzlast ist eine `DocumentSocketResponse`
(<https://foundryvtt.com/api/v12/interfaces/foundry.abstract.types.DocumentSocketRequest.html>,
Typdefinition in <https://github.com/League-of-Foundry-Developers/foundry-vtt-types>):

```
type       "ChatMessage" | "Actor" | …      welches Dokument
action     "create" | "update" | "delete"   erzeugt, geändert, gelöscht
operation  {parentUuid, pack, render, …}    seit v12 gebündelt
userId     wer es ausgelöst hat
result     die Nutzdaten — Form je nach action
```

**Erzeugt, geändert und gelöscht unterscheiden sich nur in `action` und in der Form von
`result`:** bei `create` stehen dort die vollständigen Objekte, bei `update` nur die
geänderten Felder samt `_id`, bei `delete` bloße Ids. Ein Zuhörer, der Würfe sammelt,
braucht also `action == "create"` und `type == "ChatMessage"` — und muss für `update`
und `delete` entscheiden, ob er nachträgt.

Zwei Eigenschaften, die den Bau bestimmen:

- **Der Server schickt an alle außer den Auslöser.** Wer selbst nichts schreibt, sieht
  jede fremde Änderung; auf die eigene bekäme er eine Antwort auf seinen Aufruf. Für uns
  ist das der Normalfall — die Chronik schreibt nichts nach Foundry zurück.
- **Er filtert dabei nicht**, so wenig wie bei `world`. Geflüstertes und Blindwürfe
  kommen mit. Der Berechtigungsfilter gehört deshalb auch hier **vor** den Speicher.

### Der Wurf steckt in `rolls[]` — als JSON-**String**

`ChatMessage.rolls` ist ein Feld aus JSON-Strings, nicht aus Objekten; der Server sendet
die serialisierte Form. Ein Python-Zuhörer muss `json.loads(nachricht["rolls"][0])`
machen, sonst greift er ins Leere. Ein serialisierter Wurf trägt `class`, `formula`,
`total`, `terms[]`, `options` und `evaluated`.

> **Widerspruch, den erst der Lauf auflöst.** Oben steht — ausgezählt an einem **echten**
> Abzug —, dass sieben von acht Nachrichten einen vollständigen `system.roll` tragen.
> Der Quelltext des heutigen Daggerheart-Systems
> (<https://github.com/Foundryborne/daggerheart>, `module/data/chat-message/actorRoll.mjs`)
> sagt dagegen, `roll` sei dort ein **Getter** über `rolls[]` — und Getter werden nicht
> serialisiert, stünden also in keiner Nutzlast. Beides kann stimmen, wenn die
> Systemfassung sich geändert hat; unsere Beobachtung stammt von Daggerheart 1.9.5.
> Aufgelöst wird das an einer echten Nachricht, nicht hier. Bis dahin bleibt der
> `system`-Block der dokumentierte Einstieg und `rolls[]` der belegte zweite.

### Was sonst noch von selbst kommt

Neben `session` (direkt nach dem Verbinden, mit `sessionId` und `userId`) und
`modifyDocument` schickt der Server unter anderem `userActivity`, `pause`, `chatBubble`,
`showEntry`/`shareImage`, `playAudio`, `pullToScene`, `resetFog`, `shutdown`, `reload`
und die freien Paketkanäle `module.<id>`/`system.<id>`; **`userQuery`** kam mit v13 dazu.
`time` ist dagegen Frage und Antwort, kein Push. Diese Liste stammt aus dem
ausgelieferten Bundle und ist der schwächste Beleg auf dieser Seite — für uns zählt
davon ohnehin nur `modifyDocument`.

### Welche Foundry-Fassung das voraussetzt

Unser Handschlag ist gegen **13.351** beobachtet. Für `modifyDocument` gilt:

| Fassung | Was sich änderte | Beleg |
|---|---|---|
| ≤ 0.5.3 | 156 einzeln benannte Socket-Ereignisse | [#2454](https://github.com/foundryvtt/foundryvtt/issues/2454) |
| 0.5.4 | Zusammenlegung auf `modifyDocument` | [#2454](https://github.com/foundryvtt/foundryvtt/issues/2454) |
| v12 | **Bruch** in der Form: alles wandert in ein `operation`-Objekt | [#10214](https://github.com/foundryvtt/foundryvtt/issues/10214) |
| v13 | `modifyDocument` unverändert; neu ist `userQuery` | [api/v13 `User#query`](https://foundryvtt.com/api/v13/classes/foundry.documents.User.html) |

Also: v12 und v13 sprechen dasselbe, v11 und älter nicht. Ein Zuhörer, der auf v11
laufen soll, bräuchte einen zweiten Pfad — das lohnt sich vermutlich nicht.

### Nachsehen statt raten: `scripts/lausche_foundry.py`

```
PYTHONPATH=src python3 scripts/lausche_foundry.py --dauer 180
```

Derselbe Handschlag wie ein Abgleich (`FoundryClient.verbindung`), aber die Leitung
bleibt offen: der Lauf schreibt jedes eingehende Ereignis samt Nutzlast weg und legt
danach auf. Währenddessen in Foundry einmal würfeln, einmal etwas in den Chat schreiben,
eine Nachricht ändern und eine löschen — danach steht in der Datei, was oben nur
recherchiert ist.

Das Passwort wird gefragt und nirgends abgelegt. Die Mitschrift geht als
`dumps/lauschen-<Zeit>.jsonl` in denselben gitignorierten Ordner wie der Weltabzug, mit
denselben Rechten, und ist aus demselben Grund **personenbezogen**: der Server filtert
nicht. In die Datenbank geht nichts — sie wird nicht einmal geöffnet.

## Was noch offen ist

- **Wie heißen die Ereignisse wirklich?** Der Abschnitt oben ist recherchiert, nicht
  gemessen. Offen bleiben insbesondere: ob ein Nicht-GM-Konto fremdes Geflüster wirklich
  mitbekommt, ob der Daggerheart-Wurf auf der Leitung einen `system.roll` trägt oder nur
  `rolls[]`, und ob während einer Sitzung Ereignisse auftreten, die in keiner Quelle
  stehen. Ein Lauf von `scripts/lausche_foundry.py` beantwortet das.
- **Wie viel steht wirklich im Chat-Log?** Die beobachtete Welt hatte acht Nachrichten,
  davon sieben Würfe aus einem halbstündigen Fenster — kein Schaden, keine Beute. Ob
  eine volle Sitzung wesentlich mehr liefert, ist ungeprüft. Falls nicht, trägt die
  Chronik überwiegend auf Notizen und Transkript.
- **Wie lange hält der Handschlag?** Er ist aus dem Client nachgebaut. Ein
  Foundry-Hauptversionssprung kann ihn brechen; das ist eingeplantes Risiko, kein
  Versehen.
