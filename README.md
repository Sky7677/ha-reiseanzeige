# Reiseanzeige für Home Assistant

Bahnreisen aus dem DB Navigator an Home Assistant teilen und live verfolgen: eine Dashboard-Karte mit Landkarte und Zugposition (auf Wunsch per GPS des Handys korrigiert), Reiseinformationen mit Verspätungen, Gleisen und Umstiegen und eine Ruheansicht mit Abfahrtstafel.

Echtzeit und Abfahrtstafel brauchen kostenlose [Zugangsdaten der Bahn](#zugangsdaten-der-bahn-optional). Alles andere läuft auch ohne.

> Share train journeys from the DB Navigator app with Home Assistant and follow them live. The integration is currently German-only; an English README will follow.

## Datenquellen

- der Text, den der DB Navigator beim Teilen einer Verbindung mitschickt
- [OpenBoards](https://developers.deutschebahn.com/db-api-marketplace/apis/product/open-boards) der DB InfraGO (DB API Marketplace): Abfahrts- und Ankunftstafeln mit Plan und Echtzeit, zu den Bedingungen der API (nur mit eigenen Zugangsdaten). OpenBoards löst die Timetables-API ab, die die Integration bis 0.8 nutzte.
- für die Bahnhofsnummern die [Bahnhofsliste](https://github.com/Sky7677/reiseanzeige-gleisnetz/releases/tag/bahnhofsnummern) aus demselben Repository wie das Gleisnetz, wöchentlich neu: Nummern und Namen aus OpenStation (DB InfraGO, CC0), Lage und weitere Schreibweisen aus trainline-eu/stations und OpenStreetMap (ODbL 1.0) – dieselbe Liste wie in der Android-App
- für die Karte [OpenStreetMap](https://www.openstreetmap.org/copyright) (© OpenStreetMap-Mitwirkende)
- für Streckenverlauf und Lage der Halte das [Gleisnetz](https://github.com/Sky7677/reiseanzeige-gleisnetz), monatlich aus OpenStreetMap gebaut (ODbL 1.0, © OpenStreetMap-Mitwirkende)
- für Zwischenhalte ohne Zugangsdaten, bis zur Zuordnung in OpenBoards und für Straßenbahn und U-Bahn der Fahrplan aus den GTFS-Feeds von [gtfs.de](https://gtfs.de), täglich verdichtet im selben Repository (Daten DELFI e.V., CC BY 4.0)

Nicht-öffentliche Schnittstellen der Bahn werden nicht genutzt.

## Installation

**Über HACS:** HACS → ⋮ → Benutzerdefinierte Repositories → `https://github.com/Sky7677/ha-reiseanzeige`, Typ „Integration“. Danach „Reiseanzeige“ installieren und Home Assistant neu starten.

**Von Hand:** Den Ordner `custom_components/reiseanzeige` nach `/config/custom_components/` kopieren und Home Assistant neu starten.

Anschließend unter Einstellungen → Geräte & Dienste → Integration hinzufügen → **Reiseanzeige** einrichten. Die Zugangsdaten der Bahn sind dabei optional; beide Felder leer lassen, um ohne Echtzeit zu starten.

Die Dashboard-Karte wird mitgeliefert und automatisch geladen, eine Ressource muss nicht angelegt werden:

```yaml
type: custom:reiseanzeige-card
```

Optionen der Karte: `height` (Höhe im Breitformat), `theme` (`muted` | `gray`), `raster` (sparsame Rasterkarte erzwingen), `follow` (`false` = ganze Strecke statt dem Zug folgen), `mode: board` (nur die Abfahrtstafel), `station` (Bahnhof der Tafel), `app` (`false` = Knopf „Android-App“ ausblenden, etwa auf dem Flurbildschirm).

## Zugangsdaten der Bahn (optional)

Die Reiseanzeige läuft auch ohne Zugangsdaten, dann nach Fahrplan. Mit eigenen Zugangsdaten für OpenBoards der Bahn kommt die Echtzeit dazu.

**Umstieg von 0.8:** Seit 0.9.0 holt die Integration Zuordnung, Echtzeit und Abfahrtstafel aus OpenBoards statt aus der Timetables-API. Die eingetragenen Zugangsdaten bleiben; im Portal muss dieselbe Anwendung nur zusätzlich OpenBoards abonnieren (Schritt 3 unten). Lehnt OpenBoards die Zugangsdaten ab, meldet Home Assistant das einmal. Gespeicherte Reisen ordnet die Integration nach dem Update von selbst neu zu.

| | Ohne Zugangsdaten | Mit Zugangsdaten |
| --- | --- | --- |
| Reisen teilen, Strecke auf den Gleisen, Zugposition | ja, nach Fahrplan | ja, mit Verspätungen |
| Zwischenhalte mit Planzeiten (Züge bis 30 Tage voraus, Tram, U-Bahn) | ja | ja |
| Verspätungen, Gleiswechsel, Ausfälle, Störungsmeldungen | – | ja |
| Umleitungen laut Bahn (geänderte Halte, vorzeitiges Ende) | – | ja |
| Umleitungen ohne Halt per GPS (mit Handy und Gleisnetz) | ja | ja |
| Gleise an Zwischenhalten | – | ja |
| Abfahrtstafel | – | ja |
| Anfragen bei der Bahn | keine | beim Teilen und während geteilter Reisen |

Die GPS-Korrektur der Zugposition über das Handy funktioniert in beiden Fällen.

### So bekommst du die Zugangsdaten

Stand: November 2026. Das Portal ändert sich gelegentlich; sieht ein Schritt anders aus, sind die Bezeichnungen meist ähnlich.

Konto und Zugangsdaten gehören dir. Die Reiseanzeige ist kein Angebot der Deutschen Bahn. Mit der Registrierung und dem Abonnement stimmst du den Bedingungen des Portals und der API zu – lies sie selbst, bevor du bestätigst. Diese Anleitung ersetzt das nicht.

1. **Anmelden:** Den [DB API Marketplace](https://developers.deutschebahn.com/db-api-marketplace/apis/) öffnen und oben rechts **Anmelden** wählen. Ohne Konto: **Registrieren** → **Weiter mit BahnID** und das Formular ausfüllen.
2. **Anwendung anlegen:** Oben **Anwendungen** → **Neue Anwendung erstellen**, einen Namen eingeben (zum Beispiel „Home Assistant“) und speichern. Das Portal zeigt danach die **Client ID** und das **Client Secret**. Das Client Secret erscheint nur dieses eine Mal: gleich kopieren und sicher aufbewahren, etwa im Passwortmanager.
3. **OpenBoards abonnieren:** Oben **Katalog** → nach „OpenBoards“ suchen → [OpenBoards](https://developers.deutschebahn.com/db-api-marketplace/apis/product/open-boards) öffnen → unter **Nutzungspläne** beim kostenlosen Plan **Free** auf **Abonnieren** → deine Anwendung auswählen → **Weiter** und bestätigen. Auf dieser Seite stehen auch die Bedingungen der API. Dieselbe Anwendung kann die Android-Reiseanzeige nutzen.
4. **In Home Assistant eintragen:** Beim Einrichten der Reiseanzeige oder später unter Einstellungen → Geräte & Dienste → Reiseanzeige → ⋮ → **Neu konfigurieren**. Die Client ID kommt ins Feld „Client-ID“, das Client Secret ins Feld „API-Schlüssel / Client Secret“. Die Integration prüft beides mit einer einzigen Testabfrage.

Meldet die Prüfung, dass die Zugangsdaten nicht akzeptiert werden: Ist die Anwendung für OpenBoards abonniert, und ist das Abo schon aktiv? Wurde das Client Secret vollständig kopiert?

**Entfernen:** Neu konfigurieren → beide Felder leeren → Absenden. Die Reiseanzeige läuft dann wieder nach Fahrplan. Konto und Anwendung beim Portal verwaltest du dort.

### Was mit den Zugangsdaten passiert

- Sie bleiben in deiner Home-Assistant-Installation und gehen nur an OpenBoards der Bahn. In der Diagnose zum Herunterladen sind sie entfernt. Gib sie nicht weiter und poste sie nicht, etwa in Fehlerberichten.
- Die Integration fragt höchstens 4-mal pro Sekunde und 60-mal pro Minute (erlaubt sind 10 Abfragen pro Sekunde; so bleibt auch Platz für die Android-App mit eigener Anwendung) und nur für geteilte Reisen: beim Teilen, um die Züge zuzuordnen, danach erst ab 3 Stunden vor der Abfahrt. Gefragt wird nach Bahnhofsnummern und Zeitfenstern, nie nach der ganzen Reise; an Tagen ohne Reise gibt es keine Abfragen (außer der Abfahrtstafel, solange eine Karte sie zeigt).
- Die Tafeln stammen von der DB InfraGO AG; es gelten die Bedingungen der API im Portal. Die Karte nennt die Quelle.

## Benutzung

Im DB Navigator eine Verbindung öffnen → **Teilen** → **Home Assistant**. Die Integration liest den mitgeschickten Text (Datum, Züge, Zeiten, Gleise), speichert die Reise und bestätigt das per Push aufs teilende Handy.

- **Zuordnung in OpenBoards (mit Zugangsdaten):** Gleich nach dem Teilen sucht die Integration jeden Zug in der Abfahrtstafel seines Einstiegs – über die Zugnummer aus dem Text, sonst über Linie und Minute. Die Tafel nennt den weiteren Laufweg; die Zeiten der Zwischenhalte kommen aus dem GTFS-Fahrplan, sonst aus der Tafel des Halts, Ankunft und Gleis am Ausstieg aus dessen Ankunftstafel. OpenBoards kennt den Plan Tage im Voraus. Findet sich ein Zug nicht, folgt ein neuer Versuch: mehr als einen Tag vorher alle 12 Stunden, danach alle 2 Stunden, ab 3 Stunden vorher alle 20 Minuten, in der letzten Stunde und unterwegs alle 5 Minuten. Meldet OpenBoards „zu viele Anfragen“ oder ist nicht erreichbar, pausiert die Integration eine Minute und versucht es nach 2 Minuten erneut (danach 4, 8 … Minuten); das zählt nicht als Ausfall der Echtzeit. Die Bahnhofsnummern kommen aus der Bahnhofsliste (siehe Datenquellen) und dem Gleisnetz.
- **Zwischenhalte im Voraus:** Ohne Zugangsdaten – und bis zur Zuordnung – kommen die Zwischenhalte mit Planzeiten aus dem GTFS-Fahrplan, für Reisen bis 30 Tage im Voraus. Die freien Daten haben keine Zugnummern; die Integration findet die Fahrt über Einstiegsbahnhof, Abfahrtsminute, Linie und Ausstieg.
- **Echtzeit (mit Zugangsdaten):** Abgefragt werden nur die Tafeln der nächsten Halte des laufenden Zuges, seines Ausstiegs und des Einstiegs des nächsten Zuges, eng getaktet nur kurz vor Abfahrt, Umstieg und Ankunft. Halte dazwischen bekommen die zuletzt bekannte Verspätung, passierte Halte behalten ihre tatsächlichen Zeiten. Meldungen der Bahn erscheinen beim Abschnitt, Verspätungsgründe bleiben über die Fahrt erhalten (wie im DB Navigator).
- **Umleitungen (mit Zugangsdaten):** Meldet die Bahn einen geänderten Laufweg, folgen Haltliste und Karte: Entfallene Halte sind durchgestrichen, neue Halte stehen mit Hinweis in der Liste, die Strecke führt über die angefahrenen Halte. Das gilt auch, wenn ein Zug früher endet. Die Bahn nennt nur Bahnhöfe, keine Gleise; den Weg dazwischen legt das Gleisnetz – und, wo die Bahn keine Halte nennt, der Standort des Handys (siehe nächster Punkt).
- **Umleitungen ohne Halt (GPS):** Fährt ein Zug eine andere Strecke, ohne dass die Bahn neue Halte meldet, folgt die Linie den Standorten des Handys der reisenden Person. Zwei Standorte in Folge deutlich neben der Strecke, jeweils auf einem erreichbaren Gleis, lösen das aus; danach wird mit jedem Standort nachgerechnet, und kehrt der Zug auf die geplante Strecke zurück, geht es von dort wie geplant weiter. Gespeichert werden nur aufs Gleis gesetzte Punkte, keine rohen Standorte, und sie bleiben in Home Assistant. Braucht das Gleisnetz und ein Handy mit Standort; mit Android (Standort alle 1–3 Minuten) gelingt das deutlich genauer als mit dem iPhone (etwa alle 15 Minuten). Funktioniert auch ohne Zugangsdaten der Bahn und lässt sich in den Optionen abschalten.
- **Tram und U-Bahn** führt OpenBoards nicht. Ihre Zwischenhalte kommen aus dem GTFS-Fahrplan, Echtzeit gibt es dafür nicht. **Ersatzbusse** (Schienenersatzverkehr, im DB Navigator etwa „Bus RE80“) stehen ebenfalls im Fahrplan und bekommen so ihre Zwischenhalte; andere **Busse** erscheinen nur mit Ein- und Ausstieg aus dem Text.
- **Ersatzbusse auf der Abfahrtstafel:** Geplanten Ersatzverkehr eines anderen Betreibers kennt OpenBoards oft nicht. Die Tafel ergänzt ihn deshalb aus dem Fahrplan, gekennzeichnet als „Ersatzbus, laut Fahrplan“ und mit der Haltestelle, an der er abfährt. Meldet die Bahn den Zug derselben Linie zur selben Zeit in dieselbe Richtung als fahrend, entfällt der Ersatzbus; führt die Bahn den Bus selbst, gilt ihre Fassung mit Echtzeit. Kurzfristig eingerichteter Ersatzverkehr steht noch nicht im Fahrplan – dann zeigt die Tafel den Ausfall des Zuges mit dem Hinweis der Bahn.
- **Streckenverlauf:** Die Integration lädt beim ersten Start das Gleisnetz (einige Dutzend MB, gespeichert in `.storage`) und prüft wöchentlich auf eine neue Fassung. Züge laufen damit auf den Gleisen von Halt zu Halt, ohne an Weichen umzukehren. Güterstrecken nimmt die Berechnung nur, wenn der Umweg sonst deutlich länger wäre. Straßenbahn und U-Bahn folgen der OSM-Linie mit derselben Nummer. Die Lage der Halte kommt über die Bahnhofsnummer aus OpenStreetMap. Fehlt die Nummer, sucht die Integration nach dem Namen und nimmt bei mehrdeutigen Namen den Halt nahe der übrigen Reise; was das Gleisnetz nicht kennt, kommt aus der Bahnhofsliste. In den Optionen lässt sich das Gleisnetz abschalten.
- **Ohne Einrichtung je Person:** Beim ersten Teilen erkennt die Integration das Handy, also Benachrichtigungsdienst und `device_tracker`. Darüber laufen Bestätigungen, die GPS-Korrektur der Zugposition und der Knopf „Standort anfordern“ (nicht für Personen mit verbundener Android-App, siehe unten).
- **Neue Verbindung im selben Zeitraum:** Sie ersetzt die alte, zum Beispiel wenn man unterwegs umplant.
- **Sensoren:** je Person `sensor.reiseanzeige_<name>` mit dem Zustand der wichtigsten Reise. Fällt die Echtzeit aus, gibt es einmalig eine Nachricht und das Ereignis `reiseanzeige`.

Zur Fehlersuche: Geräte & Dienste → Reiseanzeige → ⋮ → **Diagnose herunterladen**. Die Datei enthält die gespeicherten Reisen, aber keine Zugangsdaten. Bevor du sie öffentlich teilst, etwa in einem Fehlerbericht: Die Reisen zeigen, wann du wo unterwegs bist.

## Mit der Android-Reiseanzeige

Die [Android-Reiseanzeige](https://github.com/Sky7677/android-reiseanzeige) läuft eigenständig auf dem Handy und kann ihre Reisen an Home Assistant weitergeben (ab 0.8.0). Wer sie nutzt, teilt dort; Home Assistant übernimmt die Reisen von selbst und zeigt sie wie gewohnt in der Karte, auf dem Flurbildschirm und im Sensor. Für alle anderen bleibt das Teilen über die Companion-App.

**Verbinden** (einmal je Person und Handy):

- In der Karte **Android-App** → **Neue Verbindung**: am Handy „In der App öffnen“, am PC den QR-Code mit der App scannen (Einstellungen → Ziele → Home Assistant).
- Ohne zweiten Bildschirm: in der App „Über die Companion-App verbinden“ und an „Home Assistant“ teilen. Home Assistant schickt eine Benachrichtigung; ein Tipp darauf schließt das Verbinden ab.

Der Schlüssel zum Verbinden gilt 10 Minuten und einmal. Die dauerhafte Adresse bekommt die App erst in der Antwort darauf; sie steht weder in einer Benachrichtigung noch im Protokoll.

**Danach gilt für die verbundene Person:**

- Neue, ersetzte und gelöschte Reisen kommen aus der App. Halte, Strecke und Echtzeit rechnet Home Assistant weiter selbst.
- Mit „Standort senden“ in der App kommen die Standorte während der Fahrt in einen eigenen Tracker (`device_tracker.reiseanzeige_<person>_<handy>`), ohne Verlauf in Home Assistant. Die Karte zeigt wie bisher nur den Punkt auf der Strecke. Den Standort der Companion-App braucht die Reiseanzeige dann nicht mehr.
- Umleitungen per GPS wertet die App aus; Home Assistant legt nur die Linie auf seine Gleise.
- Wagen und Sitzplatz kommen aus der App; die Karte zeigt sie nur an.
- Push-Nachrichten kommen von der App, nicht von Home Assistant. Das Ereignis `reiseanzeige` für Automationen bleibt.
- Reisen aus der App löscht man in der App; die Karte bietet dafür keinen Knopf. Wird die Verbindung aufgehoben (in der Karte oder in der App), entfernt Home Assistant die Reisen dieser App sofort – so auch, wenn das Handy kaputt oder weg ist. Administratoren sehen in der Karte unter „Android-App“ die Verbindungen aller Personen und können jede aufheben.

**Erreichbarkeit:** Die App schickt an die interne Adresse von Home Assistant, etwa über ein VPN wie WireGuard (die Reiseanzeige muss dort zu den eingeschlossenen Apps gehören). Die Adresse lässt sich in den Optionen ändern („Adresse für die Android-App“). Standardmäßig nimmt Home Assistant Nachrichten der App nur aus privaten Netzen an; Adressen von Tailscale (100.x) zählen nicht dazu – dann die Option „Android-App nur aus dem lokalen Netz“ abschalten.

Das Nachrichtenformat steht in [`docs/format-v1.md`](docs/format-v1.md), gleichlautend in beiden Repos.

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
