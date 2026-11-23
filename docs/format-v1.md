# Nachrichtenformat v1: Android-Reiseanzeige → Ziele

Stand 17.11.2026. Diese Datei liegt gleichlautend in `Sky7677/ha-reiseanzeige` und `Sky7677/android-reiseanzeige` (jeweils `docs/format-v1.md`, Beispieldaten unter `docs/format-v1/`). Eine Änderung gilt erst, wenn beide Fassungen gleich sind und beide Repos gegen die Beispieldaten testen.

Die App gibt Reisen und auf Wunsch den Standort an Ziele weiter. Erstes Ziel ist die Integration `ha-reiseanzeige` (ab 0.8.0). Das Format kennt keine Home-Assistant-Begriffe, damit später auch andere Ziele (etwa ein allgemeiner Webhook) dieselben Nachrichten verstehen.

## Grundsätze

- Die App ist die Quelle ihrer Reisen. Das Ziel übernimmt, was sie schickt, und wendet keine eigene Ersetzungsregel an.
- Die App schickt die fertige Verbindung, nicht den geteilten Text. Beim Umplanen unterwegs setzt sie die Reise aus dem Anfang der alten und der neuen Verbindung zusammen (Zubringer); im Text allein fehlte dieser Teil.
- Halte, Strecke und Echtzeit rechnet das Ziel selbst. Daten der Bahn stehen nicht in den Nachrichten.
- Feldnamen nur in ASCII. Zeiten als ISO 8601 mit Zeitzone (`2026-10-08T05:57:00+02:00`).
- Unbekannte Felder werden ignoriert. Unbekannte Nachrichtenarten derselben Hauptversion auch: Das Ziel antwortet dann mit `ignored`.

## Transport

`POST` mit JSON (UTF-8) an die Adresse des Ziels, bei Home Assistant `<basis>/api/webhook/<id>`. Höchstens 256 KB je Anfrage, höchstens 50 Nachrichten. Ein Ziel darf mehr als 60 Anfragen pro Minute mit `429` ablehnen.

```json
{
  "format": 1,
  "sentAt": "2026-10-08T07:41:05+02:00",
  "app": {"name": "Reiseanzeige", "version": "0.8.0"},
  "messages": [ { "type": "…" } ]
}
```

Die Nachrichten werden der Reihe nach verarbeitet.

## Nachrichten

| `type` | Wann | Felder |
| --- | --- | --- |
| `link.hello` | erster Kontakt nach dem Verbinden, nach einem Update der App | `device` (Name des Handys), `appInstance` (feste Kennung der Installation) |
| `link.bye` | Verbindung in der App aufgehoben | – |
| `trip.upsert` | Teilen, Ersetzen, Zubringer, anderer Zug, Wagen oder Sitzplatz geändert; beim Verbinden für alle laufenden und kommenden Reisen | `tripId`, `revision`, `connection`, `seats` |
| `trip.delete` | Reise in der App gelöscht | `tripId` |
| `location` | während der Verfolgung, nur mit „Standort senden“ | `tripId`, `fixes`, optional `train` |
| `route.gps` | Umleitung per GPS erkannt, nachgerechnet oder beendet | `tripId`, `leg`, `legKey`, `gps` (oder `null`) |
| `sharing.stop` | Ankunft, „Senden stoppen“ | `tripId` |

### `trip.upsert`

- `tripId`: Kennung der Reise in der App, 1 bis 64 Zeichen aus `A–Z a–z 0–9 _ -`.
- `revision`: ganze Zahl, steigt mit jeder Änderung. Eine kleinere oder gleiche Revision als die gespeicherte ändert nichts.
- `seats`: je Abschnitt (Index als Text) `{"coach": "7", "seat": "45"}`, jeweils höchstens 12 Zeichen oder `null`. Bei Reisen aus der App hat sie Vorfahrt; das Ziel ändert Wagen und Sitzplatz nicht selbst.
- `connection`: die Verbindung im Format des ausgelesenen Textes:

```json
{
  "origin": "Augsburg Hbf",
  "destination": "Berlin Südkreuz",
  "day": "2026-10-08",
  "vbid": "00000000-0000-4000-8000-000000000001",
  "link": "https://www.bahn.de/buchung/start?vbid=00000000-0000-4000-8000-000000000001",
  "legs": [
    {
      "line": "RE16", "number": "59116", "category": "RE", "mode": "regional",
      "direction": "Nürnberg Hbf",
      "dep": {"name": "Augsburg Hbf", "time": "2026-10-08T05:57:00+02:00", "platform": "5", "platformNote": null},
      "arr": {"name": "Nürnberg Hbf", "time": "2026-10-08T07:30:00+02:00", "platform": "9", "platformNote": null},
      "extra": []
    }
  ]
}
```

`mode` ist eines von `longdistance`, `regional`, `suburban`, `subway`, `tram`, `bus`, `ship`, `other`. Die Abschnitte stehen in Reihenfolge; ihr Index ist die Position in `legs`. Höchstens 30 Abschnitte.

### `location`

```json
{"type": "location", "tripId": "…",
 "fixes": [{"lat": 49.4459, "lon": 11.0823, "accuracy": 12.0, "time": "2026-10-08T07:40:31+02:00", "speed": 33.4, "bearing": 12.0}],
 "train": {"leg": 1, "lat": 49.4471, "lon": 11.0850, "time": "2026-10-08T07:40:31+02:00", "gps": true,
           "speed": 33.4, "speedTime": "2026-10-08T07:40:31+02:00", "speedApprox": false}}
```

`fixes`: Messungen des Handys, älteste zuerst, höchstens 30. `accuracy` in Metern, `speed` in m/s, `bearing` in Grad; beide optional.

`train`: Ergebnis der App, optional – der Zugpunkt auf der Strecke zur Zeit `time` (`leg`, `lat`, `lon`; `gps`: per GPS korrigiert) und die Geschwindigkeit, wie die App sie anzeigt: `speed` in m/s, `speedTime` Zeit der Messung, `speedApprox` abgeleitet aus zwei Standorten auf der Strecke statt vom Handy gemessen (alle drei optional). Ein Ziel nutzt dieses Ergebnis vor seiner eigenen Auswertung der `fixes`; die bleibt Ersatz, etwa für Nachrichten ohne `train`. Ein Ziel zeigt nie den Standort selbst an, sondern nur den Punkt auf der Strecke.

### `route.gps`

Umleitung per GPS eines Abschnitts im Format `leg.diversion.gps` beider Fassungen: `after` (Halt, ab dem neu gezeichnet wird: `id`, `name`), `anchor` (`[lat, lon]`), `anchorAt`, `prefix` (Verlauf bis zum Halt, `[[lat, lon], …]`), `points` (aufs Gleis gesetzte Standorte, `[[lat, lon, zeit], …]`, höchstens 400). `gps: null` beendet die Umleitung. `legKey` (`line`, `dep` als Zeit der Abfahrt) sichert, dass `leg` denselben Abschnitt meint.

## Antwort

`200` mit JSON:

```json
{"format": 1, "ok": true, "trips": {"5f0c2a": "ok"}}
```

`trips` nennt für jede Reise der Anfrage ihren Zustand beim Ziel:

| Wert | Bedeutung für die App |
| --- | --- |
| `ok` | übernommen |
| `deleted` | beim Ziel gelöscht: diese Reise nicht mehr an dieses Ziel senden, in der App bleibt sie |
| `unknown` | Ziel kennt die Reise nicht (etwa `location` vor `trip.upsert`): die Reise mit `trip.upsert` neu senden |
| `ignored` | Nachricht nicht verstanden, Reise unverändert |

Auf `link.hello` an eine Einmal-Adresse kommt zusätzlich die dauerhafte Adresse:

```json
{"format": 1, "ok": true, "link": {"webhookId": "<64 Hex-Zeichen>", "instance": "Zuhause", "user": "Marlon", "localOnly": true}}
```

Fehler: `400` mit `{"format": 1, "ok": false, "error": {"code": "…", "message": "…"}}`, Codes `format_unsupported` (Hauptversion zu neu: Ziel aktualisieren), `bad_request`, `too_large`; `429` bei zu vielen Anfragen.

**Leere Antwort mit `200`:** Home Assistant antwortet so auf unbekannte Webhook-Adressen. Für die App heißt das: Verbindung beim Ziel aufgehoben.

## Verbinden

Verbunden wird mit einem Einmal-Schlüssel (64 Hex-Zeichen, 10 Minuten gültig). Die App meldet sich damit per `link.hello` an `<basis>/api/webhook/<einmal-schlüssel>` und bekommt in der Antwort die dauerhafte Adresse. Wiederholt die App `link.hello` mit demselben Schlüssel (Antwort verloren), bekommt sie dieselbe Adresse noch einmal. Die dauerhafte Adresse steht so nirgends außer in der Antwort.

**Link oder QR-Code aus dem Ziel:**

```
reiseanzeige://ziel/verbinden?v=1&url=<basis>&token=<einmal-schlüssel>&name=<instanz>&user=<benutzer>
```

Auf Android öffnet die Karte denselben Link als `intent://ziel/verbinden?…#Intent;scheme=reiseanzeige;package=de.sky7677.reiseanzeige;end`.

**Teilen an die Companion-App (ohne zweiten Bildschirm):** Die App erzeugt Einmal-Schlüssel und eine Kopplungskennung (16 Zeichen `a–z 0–9`) und teilt diesen Text an die Home-Assistant-App:

```
Reiseanzeige mit Home Assistant verbinden
reiseanzeige-kopplung v1 <kopplungskennung> <einmal-schlüssel>
```

Home Assistant kennt aus der Companion-App den Benutzer und das Handy, meldet den Einmal-Schlüssel an und schickt diesem Handy eine Benachrichtigung. Ein Tipp öffnet die App:

```
reiseanzeige://ziel/verbinden?v=1&url=<basis>&pair=<kopplungskennung>&name=<instanz>&user=<benutzer>
```

Die App nimmt den Link nur an, wenn sie die Kopplungskennung selbst erzeugt hat und sie höchstens 10 Minuten alt ist. In Benachrichtigung und Ereignisprotokoll steht so nie die dauerhafte Adresse.

## Beispieldaten

| Datei | Inhalt |
| --- | --- |
| `hello.json` | `link.hello` beim Verbinden |
| `hello-response.json` | Antwort darauf mit dauerhafter Adresse |
| `upsert-augsburg-berlin.json` | `trip.upsert`, Verbindung wie aus `teilen/augsburg-berlin-2026-10-08.txt` ausgelesen |
| `upsert-zubringer.json` | `trip.upsert` nach dem Umplanen unterwegs: ICE 1604 bis Erfurt aus der alten Reise, danach die neue Verbindung |
| `location.json` | `location` mit zwei Messungen, Zugpunkt und Geschwindigkeit der App |
| `route-gps.json` | `route.gps` mit Umleitung über Leipzig |
| `route-gps-ende.json` | `route.gps` mit `gps: null` |
| `delete-stop-bye.json` | `trip.delete`, `sharing.stop` und `link.bye` |
| `response-ok.json`, `response-deleted.json` | Antworten |
| `kopplung.txt` | geteilter Text der Kopplungsanfrage |
