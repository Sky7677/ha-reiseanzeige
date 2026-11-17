# Hinweise für Claude

Dieses Repo (`Sky7677/ha-reiseanzeige`, Integration für Home Assistant) gehört mit `Sky7677/android-reiseanzeige` (Android- und Wear-OS-App) zusammen. Seit 17.11.2026 entstehen beide in einem gemeinsamen Chat im Projekt „Home Assistant Addons“.

- Gemeinsam: das Format der Reisen (Snapshot Version 2), die geteilten Testtexte und erwarteten Ergebnisse (die App rechnet Feld für Feld gegen diese Integration) und ab 0.8.0 das Nachrichtenformat v1 (`docs/format-v1.md` mit Beispieldaten in `docs/format-v1/`, gleichlautend in beiden Repos).
- Jede Änderung an einem Teil prüft den anderen mit. Änderungen am Format v1 gelten erst, wenn beide Fassungen gleich sind und beide Repos gegen die Beispieldaten testen.
- Konzepte: „Konzept: Live-Reiseanzeige für Home Assistant“ und „Konzept: Android-Reiseanzeige“ (lebende Dokumente, Kurzfassungen im Projekt).
- Arbeitsweise: Claude arbeitet bis zum Pull Request (Zweig `claude/<thema>`), Marlon prüft, mergt und erstellt die Releases (zuerst als Vorabversion). Nie direkt auf `main` pushen.
- Bauen und testen: `pip install -r requirements_test.txt && pytest`; Karte: `npm ci && npm test`, nach Änderungen an `frontend/src` `npm run build` und die gebaute Datei einchecken.
- Namen im Code (Variablen, Felder, Dateien) nur in ASCII, keine Umlaute.
