# Reiseanzeige für Home Assistant

Bahnreisen aus dem DB Navigator an Home Assistant teilen und live verfolgen: eine Dashboard-Karte mit Landkarte und Zugposition (auf Wunsch per GPS des Handys korrigiert), Reiseinformationen mit Verspätungen, Gleisen und Umstiegen und eine Ruheansicht mit Abfahrtstafel.

Echtzeit und Abfahrtstafel brauchen kostenlose [Zugangsdaten der Bahn](#zugangsdaten-der-bahn-optional). Alles andere läuft auch ohne.

> Share train journeys from the DB Navigator app with Home Assistant and follow them live. The integration is currently German-only; an English README will follow.

## Datenquellen

- der Text, den der DB Navigator beim Teilen einer Verbindung mitschickt
- die offizielle [Timetables-API](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables) der Deutschen Bahn für Fahrplan und Echtzeit, Daten unter CC BY 4.0 (nur mit eigenen Zugangsdaten)
- für die Karte [OpenStreetMap](https://www.openstreetmap.org/copyright) (© OpenStreetMap-Mitwirkende)
- für Streckenverlauf und Lage der Halte das [Gleisnetz](https://github.com/Sky7677/reiseanzeige-gleisnetz), monatlich aus OpenStreetMap gebaut (ODbL 1.0, © OpenStreetMap-Mitwirkende)
- für Zwischenhalte vor dem Fahrplanabgleich und für Straßenbahn und U-Bahn der Fahrplan aus den GTFS-Feeds von [gtfs.de](https://gtfs.de), täglich verdichtet im selben Repository (Daten DELFI e.V., CC BY 4.0)

Nicht-öffentliche Schnittstellen der Bahn werden nicht genutzt.

## Installation

**Über HACS:** HACS → ⋮ → Benutzerdefinierte Repositories → `https://github.com/Sky7677/ha-reiseanzeige`, Typ „Integration“. Danach „Reiseanzeige“ installieren und Home Assistant neu starten.

**Von Hand:** Den Ordner `custom_components/reiseanzeige` nach `/config/custom_components/` kopieren und Home Assistant neu starten.

Anschließend unter Einstellungen → Geräte & Dienste → Integration hinzufügen → **Reiseanzeige** einrichten. Die Zugangsdaten der Bahn sind dabei optional; beide Felder leer lassen, um ohne Echtzeit zu starten.

Die Dashboard-Karte wird mitgeliefert und automatisch geladen, eine Ressource muss nicht angelegt werden:

```yaml
type: custom:reiseanzeige-card
```

Optionen der Karte: `height` (Höhe im Breitformat), `theme` (`muted` | `gray`), `raster` (sparsame Rasterkarte erzwingen), `follow` (`false` = ganze Strecke statt dem Zug folgen), `mode: board` (nur die Abfahrtstafel), `station` (Bahnhof der Tafel).

## Zugangsdaten der Bahn (optional)

Die Reiseanzeige läuft auch ohne Zugangsdaten, dann nach Fahrplan. Mit eigenen Zugangsdaten für die Timetables-API der Bahn kommt die Echtzeit dazu.

| | Ohne Zugangsdaten | Mit Zugangsdaten |
| --- | --- | --- |
| Reisen teilen, Strecke auf den Gleisen, Zugposition | ja, nach Fahrplan | ja, mit Verspätungen |
| Zwischenhalte mit Planzeiten (Züge bis 30 Tage voraus, Tram, U-Bahn) | ja | ja |
| Verspätungen, Gleiswechsel, Ausfälle, Störungsmeldungen | – | ja |
| Umleitungen laut Bahn (geänderte Halte, vorzeitiges Ende) | – | ja |
| Umleitungen ohne Halt per GPS (mit Handy und Gleisnetz) | ja | ja |
| Gleise an Zwischenhalten | – | ja |
| Abfahrtstafel | – | ja |
| Anfragen bei der Bahn | keine | nur während geteilter Reisen |

Die GPS-Korrektur der Zugposition über das Handy funktioniert in beiden Fällen.

### So bekommst du die Zugangsdaten

Stand: November 2026. Das Portal ändert sich gelegentlich; sieht ein Schritt anders aus, sind die Bezeichnungen meist ähnlich.

Konto und Zugangsdaten gehören dir. Die Reiseanzeige ist kein Angebot der Deutschen Bahn. Mit der Registrierung und dem Abonnement stimmst du den Bedingungen des Portals und der API zu – lies sie selbst, bevor du bestätigst. Diese Anleitung ersetzt das nicht.

1. **Anmelden:** Den [DB API Marketplace](https://developers.deutschebahn.com/db-api-marketplace/apis/) öffnen und oben rechts **Anmelden** wählen. Ohne Konto: **Registrieren** → **Weiter mit BahnID** und das Formular ausfüllen.
2. **Anwendung anlegen:** Oben **Anwendungen** → **Neue Anwendung erstellen**, einen Namen eingeben (zum Beispiel „Home Assistant“) und speichern. Das Portal zeigt danach die **Client ID** und das **Client Secret**. Das Client Secret erscheint nur dieses eine Mal: gleich kopieren und sicher aufbewahren, etwa im Passwortmanager.
3. **Timetables abonnieren:** Oben **Katalog** → nach „Timetables“ suchen → die [Timetables-API](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables) öffnen → unter **Nutzungspläne** beim kostenlosen Plan **Free** auf **Abonnieren** → deine Anwendung auswählen → **Weiter** und bestätigen. Auf dieser Seite stehen auch die Bedingungen der API.
4. **In Home Assistant eintragen:** Beim Einrichten der Reiseanzeige oder später unter Einstellungen → Geräte & Dienste → Reiseanzeige → ⋮ → **Neu konfigurieren**. Die Client ID kommt ins Feld „Client-ID“, das Client Secret ins Feld „API-Schlüssel / Client Secret“. Die Integration prüft beides mit einer einzigen Testabfrage.

Meldet die Prüfung, dass die Zugangsdaten nicht akzeptiert werden: Ist die Anwendung für Timetables abonniert, und ist das Abo schon aktiv? Wurde das Client Secret vollständig kopiert?

**Entfernen:** Neu konfigurieren → beide Felder leeren → Absenden. Die Reiseanzeige läuft dann wieder nach Fahrplan. Konto und Anwendung beim Portal verwaltest du dort.

### Was mit den Zugangsdaten passiert

- Sie bleiben in deiner Home-Assistant-Installation und gehen nur an die Timetables-API der Bahn. In der Diagnose zum Herunterladen sind sie entfernt. Gib sie nicht weiter und poste sie nicht, etwa in Fehlerberichten.
- Der kostenlose Plan erlaubt 60 Abfragen pro Minute. Die Integration bleibt mit höchstens 50 darunter und fragt nur während geteilter Reisen: rund 250 Abfragen für eine Tagesreise mit drei Zügen, an Tagen ohne Reise keine.
- Die Daten der API stehen unter CC BY 4.0 (Quelle: Deutsche Bahn AG). Die Karte nennt die Quelle.

**StaDa (optional):** Dieselbe Anwendung kann zusätzlich **StaDa – Station Data** (Plan „Free4All“) abonnieren. Die Integration holt daraus die Lage der Bahnhöfe, wenn das Gleisnetz abgeschaltet ist oder einen Bahnhof nicht kennt. Die Liste wird höchstens alle 7 Tage geholt, wie es deren Bedingungen verlangen.

## Benutzung

Im DB Navigator eine Verbindung öffnen → **Teilen** → **Home Assistant**. Die Integration liest den mitgeschickten Text (Datum, Züge, Zeiten, Gleise), speichert die Reise und bestätigt das per Push aufs teilende Handy.

- **Fahrplanabgleich (mit Zugangsdaten):** Die Integration sucht die Züge im Fahrplan der Timetables-API, um Zwischenhalte und Echtzeit zu bekommen. Weil die API ihren Fahrplan nur kurz im Voraus liefert, geschieht das ab etwa einem Tag vor der Abfahrt.
- **Zwischenhalte im Voraus:** Bis dahin – und ohne Zugangsdaten immer – kommen die Zwischenhalte mit Planzeiten aus dem GTFS-Fahrplan, für Reisen bis 30 Tage im Voraus. Die freien Daten haben keine Zugnummern; die Integration findet die Fahrt über Einstiegsbahnhof, Abfahrtsminute, Linie und Ausstieg.
- **Echtzeit (mit Zugangsdaten):** Abgefragt werden nur die nächsten Halte des laufenden Zuges, sein Ausstieg und der Einstieg des nächsten Zuges, eng getaktet nur kurz vor Abfahrt, Umstieg und Ankunft.
- **Umleitungen (mit Zugangsdaten):** Meldet die Bahn einen geänderten Laufweg, folgen Haltliste und Karte: Entfallene Halte sind durchgestrichen, neue Halte stehen mit Hinweis in der Liste, die Strecke führt über die angefahrenen Halte. Das gilt auch, wenn ein Zug früher endet. Die Bahn nennt nur Bahnhöfe, keine Gleise; den Weg dazwischen legt das Gleisnetz – und, wo die Bahn keine Halte nennt, der Standort des Handys (siehe nächster Punkt).
- **Umleitungen ohne Halt (GPS):** Fährt ein Zug eine andere Strecke, ohne dass die Bahn neue Halte meldet, folgt die Linie den Standorten des Handys der reisenden Person. Zwei Standorte in Folge deutlich neben der Strecke, jeweils auf einem erreichbaren Gleis, lösen das aus; danach wird mit jedem Standort nachgerechnet, und kehrt der Zug auf die geplante Strecke zurück, geht es von dort wie geplant weiter. Gespeichert werden nur aufs Gleis gesetzte Punkte, keine rohen Standorte, und sie bleiben in Home Assistant. Braucht das Gleisnetz und ein Handy mit Standort; mit Android (Standort alle 1–3 Minuten) gelingt das deutlich genauer als mit dem iPhone (etwa alle 15 Minuten). Funktioniert auch ohne Zugangsdaten der Bahn und lässt sich in den Optionen abschalten.
- **Tram und U-Bahn** kennt die Timetables-API nicht. Ihre Zwischenhalte kommen aus dem GTFS-Fahrplan, Echtzeit gibt es dafür nicht. **Busse** erscheinen nur mit Ein- und Ausstieg aus dem Text.
- **Streckenverlauf:** Die Integration lädt beim ersten Start das Gleisnetz (einige Dutzend MB, gespeichert in `.storage`) und prüft wöchentlich auf eine neue Fassung. Züge laufen damit auf den Gleisen von Halt zu Halt, ohne an Weichen umzukehren. Güterstrecken nimmt die Berechnung nur, wenn der Umweg sonst deutlich länger wäre. Straßenbahn und U-Bahn folgen der OSM-Linie mit derselben Nummer. Die Lage der Halte kommt über die Bahnhofsnummer aus OpenStreetMap. Fehlt die Nummer, sucht die Integration nach dem Namen und nimmt bei mehrdeutigen Namen den Halt nahe der übrigen Reise. In den Optionen lässt sich das Gleisnetz abschalten.
- **Ohne Einrichtung je Person:** Beim ersten Teilen erkennt die Integration das Handy, also Benachrichtigungsdienst und `device_tracker`. Darüber laufen Bestätigungen, die GPS-Korrektur der Zugposition und der Knopf „Standort anfordern“.
- **Neue Verbindung im selben Zeitraum:** Sie ersetzt die alte, zum Beispiel wenn man unterwegs umplant.
- **Sensoren:** je Person `sensor.reiseanzeige_<name>` mit dem Zustand der wichtigsten Reise. Fällt die Echtzeit aus, gibt es einmalig eine Nachricht und das Ereignis `reiseanzeige`.

Zur Fehlersuche: Geräte & Dienste → Reiseanzeige → ⋮ → **Diagnose herunterladen**. Die Datei enthält die gespeicherten Reisen, aber keine Zugangsdaten. Bevor du sie öffentlich teilst, etwa in einem Fehlerbericht: Die Reisen zeigen, wann du wo unterwegs bist.

## Grenzen und Pläne

- **Streckenverlauf:** Bahn-Abschnitte mehr als 30 Tage vor Abfahrt kennen nur Ein- und Ausstieg; bis dahin zeigt die Karte den kürzesten Weg auf den Gleisen. Umleitungen ohne Zwischenhalt auf der neuen Strecke erkennt die Karte nur mit GPS vom Handy (siehe oben).
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
