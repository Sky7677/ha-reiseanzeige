# Reiseanzeige für Home Assistant

Bahnreisen aus dem DB Navigator an Home Assistant teilen und live verfolgen: eine Dashboard-Karte mit Landkarte und Zugposition (auf Wunsch per GPS des Handys korrigiert), Reiseinformationen mit Verspätungen, Gleisen und Umstiegen und eine Ruheansicht mit Abfahrtstafel.

> Share train journeys from the DB Navigator app with Home Assistant and follow them live. The integration is currently German-only; an English README will follow.

## Datenquellen

- der Text, den der DB Navigator beim Teilen einer Verbindung mitschickt
- die offizielle [Timetables-API](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables) der Deutschen Bahn für Fahrplan und Echtzeit, Daten unter CC BY 4.0
- für die Karte [OpenStreetMap](https://www.openstreetmap.org/copyright) (© OpenStreetMap-Mitwirkende)
- für Streckenverlauf und Lage der Halte das [Gleisnetz](https://github.com/Sky7677/reiseanzeige-gleisnetz), monatlich aus OpenStreetMap gebaut (ODbL 1.0, © OpenStreetMap-Mitwirkende)
- für Zwischenhalte vor dem Fahrplanabgleich und für Straßenbahn und U-Bahn der Fahrplan aus den GTFS-Feeds von [gtfs.de](https://gtfs.de), täglich verdichtet im selben Repository (Daten DELFI e.V., CC BY 4.0)

Nicht-öffentliche Schnittstellen der Bahn werden nicht genutzt.

## Installation

**Über HACS:** HACS → ⋮ → Benutzerdefinierte Repositories → `https://github.com/Sky7677/ha-reiseanzeige`, Typ „Integration“. Danach „Reiseanzeige“ installieren und Home Assistant neu starten.

**Von Hand:** Den Ordner `custom_components/reiseanzeige` nach `/config/custom_components/` kopieren und Home Assistant neu starten.

Anschließend unter Einstellungen → Geräte & Dienste → Integration hinzufügen → **Reiseanzeige** die Zugangsdaten eintragen.

Die Dashboard-Karte wird mitgeliefert und automatisch geladen, eine Ressource muss nicht angelegt werden:

```yaml
type: custom:reiseanzeige-card
```

Optionen der Karte: `height` (Höhe im Breitformat), `theme` (`muted` | `gray`), `raster` (sparsame Rasterkarte erzwingen), `follow` (`false` = ganze Strecke statt dem Zug folgen), `mode: board` (nur die Abfahrtstafel), `station` (Bahnhof der Tafel).

## Zugangsdaten

Im [DB API Marketplace](https://developers.deutschebahn.com/) eine Anwendung anlegen und den kostenlosen Plan von **Timetables** abonnieren. Jede Installation braucht eigene Zugangsdaten.

Optional kann dieselbe Anwendung zusätzlich **StaDa – Station Data** (Plan „Free4All“) abonnieren. Die Integration holt daraus die Lage der Bahnhöfe, wenn das Gleisnetz abgeschaltet ist oder einen Bahnhof nicht kennt. Die Liste wird höchstens alle 7 Tage geholt, wie es deren Bedingungen verlangen.

## Benutzung

Im DB Navigator eine Verbindung öffnen → **Teilen** → **Home Assistant**. Die Integration liest den mitgeschickten Text (Datum, Züge, Zeiten, Gleise), speichert die Reise und bestätigt das per Push aufs teilende Handy.

- **Fahrplanabgleich:** Die Integration sucht die Züge im Fahrplan der Timetables-API, um Zwischenhalte und Echtzeit zu bekommen. Weil die API ihren Fahrplan nur kurz im Voraus liefert, geschieht das ab etwa einem Tag vor der Abfahrt.
- **Zwischenhalte im Voraus:** Bis dahin kommen die Zwischenhalte mit Planzeiten aus dem GTFS-Fahrplan, für Reisen bis 30 Tage im Voraus. Die freien Daten haben keine Zugnummern; die Integration findet die Fahrt über Einstiegsbahnhof, Abfahrtsminute, Linie und Ausstieg.
- **Echtzeit:** Abgefragt werden nur die nächsten Halte des laufenden Zuges, sein Ausstieg und der Einstieg des nächsten Zuges, eng getaktet nur kurz vor Abfahrt, Umstieg und Ankunft.
- **Tram und U-Bahn** kennt die Timetables-API nicht. Ihre Zwischenhalte kommen aus dem GTFS-Fahrplan, Echtzeit gibt es dafür nicht. **Busse** erscheinen nur mit Ein- und Ausstieg aus dem Text.
- **Streckenverlauf:** Die Integration lädt beim ersten Start das Gleisnetz (einige Dutzend MB, gespeichert in `.storage`) und prüft wöchentlich auf eine neue Fassung. Züge laufen damit auf den Gleisen von Halt zu Halt, ohne an Weichen umzukehren. Güterstrecken nimmt die Berechnung nur, wenn der Umweg sonst deutlich länger wäre. Straßenbahn und U-Bahn folgen der OSM-Linie mit derselben Nummer. Die Lage der Halte kommt über die Bahnhofsnummer aus OpenStreetMap. Fehlt die Nummer, sucht die Integration nach dem Namen und nimmt bei mehrdeutigen Namen den Halt nahe der übrigen Reise. In den Optionen lässt sich das Gleisnetz abschalten.
- **Ohne Einrichtung je Person:** Beim ersten Teilen erkennt die Integration das Handy, also Benachrichtigungsdienst und `device_tracker`. Darüber laufen Bestätigungen, die GPS-Korrektur der Zugposition und der Knopf „Standort anfordern“.
- **Neue Verbindung im selben Zeitraum:** Sie ersetzt die alte, zum Beispiel wenn man unterwegs umplant.
- **Sensoren:** je Person `sensor.reiseanzeige_<name>` mit dem Zustand der wichtigsten Reise. Fällt die Echtzeit aus, gibt es einmalig eine Nachricht und das Ereignis `reiseanzeige`.

Zur Fehlersuche: Geräte & Dienste → Reiseanzeige → ⋮ → **Diagnose herunterladen**. Die Datei enthält die gespeicherten Reisen, aber keine Zugangsdaten.

## Grenzen und Pläne

- **Streckenverlauf:** Bahn-Abschnitte vor dem Fahrplanabgleich, also mehr als einen Tag vor Abfahrt, kennen nur Ein- und Ausstieg. Bis dahin zeigt die Karte den kürzesten Weg auf den Gleisen. Fährt ein Zug eine Umleitung, zeigt die Karte noch den geplanten Weg.
- **Gleisnetz:** nur Deutschland. Lücken oder Fehler in OpenStreetMap führen stellenweise zu geraden Linien.
- **Textformat:** Ändert die Bahn das Format des geteilten Textes, muss das Auslesen angepasst werden.
- **Ersatzverkehr:** Ausfälle sind sichtbar, Ersatzbusse kaum.

## Entwicklung

```bash
pip install -r requirements_test.txt && pytest      # Integration (Python)
npm ci && npm test                                  # Karte (JavaScript)
npm run build                                       # Kartendatei neu bauen und einchecken
```

## Lizenz

Code: MIT, siehe `LICENSE`. Enthaltene Bibliotheken der Karte und weitere Hinweise: siehe `THIRD_PARTY_NOTICES.md`. Das Gleisnetz, das die Integration herunterlädt, steht unter der ODbL 1.0 (© OpenStreetMap-Mitwirkende).
