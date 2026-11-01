import {clock} from './format.js';

// Welche Reise zeigt die Karte? Reihenfolge aus dem Konzept:
//   1. manuell gewählte Reise, solange es sie gibt
//   2. eigene Reise: laufend, dann gerade abgeschlossen, dann die nächste anstehende
//   3. dasselbe für Reisen anderer Personen
//   4. sonst keine (Ruheansicht)
// Der Flurbildschirm hat keine eigenen Reisen und landet so direkt bei Schritt 3.

const RANK = {running: 0, completed: 1, upcoming: 2};

const byRelevance = (a, b) =>
	RANK[a.status] - RANK[b.status]
	|| (a.status === 'completed' ? Date.parse(b.arrival) - Date.parse(a.arrival) : Date.parse(a.departure) - Date.parse(b.departure));

export const selectTrip = (trips, {userId, manualId = null} = {}) => {
	if (manualId) {
		const manual = trips.find(t => t.id === manualId);
		if (manual) return manual;
	}
	const relevant = trips.filter(t => t.status in RANK);
	const own = relevant.filter(t => t.ownerId === userId).sort(byRelevance);
	if (own.length) return own[0];
	return relevant.filter(t => t.ownerId !== userId).sort(byRelevance)[0] ?? null;
};

// Umschalter: pro Person eine Gruppe, darin ihre Reisen in sinnvoller Reihenfolge
export const switcherGroups = (trips) => {
	const groups = new Map();
	for (const t of [...trips].sort(byRelevance)) {
		const key = t.ownerId;
		if (!groups.has(key)) groups.set(key, {ownerId: key, ownerName: t.ownerName ?? 'Unbekannt', trips: []});
		groups.get(key).trips.push(t);
	}
	return [...groups.values()];
};

const shortDay = (iso) => new Intl.DateTimeFormat('de-DE', {timeZone: 'Europe/Berlin', weekday: 'short', day: '2-digit', month: '2-digit'}).format(new Date(iso));

// Beschriftung im Umschalter: Name und Ziel; bei Verwechslungsgefahr Datum, notfalls Uhrzeit
export const switcherLabels = (trips) => {
	const key = (t) => `${t.ownerId}|${t.snapshot.destination?.name}`;
	const count = new Map();
	for (const t of trips) count.set(key(t), (count.get(key(t)) ?? 0) + 1);
	const dayCount = new Map();
	for (const t of trips) { const k = key(t) + shortDay(t.departure); dayCount.set(k, (dayCount.get(k) ?? 0) + 1); }
	return new Map(trips.map((t) => {
		let extra = '';
		if (count.get(key(t)) > 1) {
			extra = shortDay(t.departure);
			if (dayCount.get(key(t) + extra) > 1) extra += ` ${clock(t.departure)}`;
		}
		return [t.id, {name: t.ownerName ?? 'Unbekannt', destination: t.snapshot.destination?.name ?? '', extra}];
	}));
};
