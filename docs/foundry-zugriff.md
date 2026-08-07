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
Mensch. Ein eigenes Konto mit passenden Rechten ist sauberer als das eines Mitspielers.

**Das Passwort wird nirgends gespeichert** (#64): nicht in der SQLite, nicht in einer
Umgebungsvariable, nicht in einer Datei. Es wird gefragt, wenn der Abgleich es braucht,
lebt bis dahin im Arbeitsspeicher (`chronicle.zugang`) und wird vom Abgleich verbraucht —
auch vom gescheiterten. Hashen ginge nicht: wir müssen es vorzeigen, nicht prüfen. Und es
steht in keinem Aufrufargument (landet sonst in der Shell-History) und in keiner Logzeile.

## Was noch offen ist

- **Wie viel steht wirklich im Chat-Log?** Die beobachtete Welt hatte acht Nachrichten,
  davon sieben Würfe aus einem halbstündigen Fenster — kein Schaden, keine Beute. Ob
  eine volle Sitzung wesentlich mehr liefert, ist ungeprüft. Falls nicht, trägt die
  Chronik überwiegend auf Notizen und Transkript.
- **Wie lange hält der Handschlag?** Er ist aus dem Client nachgebaut. Ein
  Foundry-Hauptversionssprung kann ihn brechen; das ist eingeplantes Risiko, kein
  Versehen.
