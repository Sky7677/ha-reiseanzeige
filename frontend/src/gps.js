// GPS-Korrektur der berechneten Zugposition (Konzept: "GPS-Position des Handys").
//
// Ein Standort zählt nur, wenn er
//   1. genau genug ist (höchstens MAX_ACCURACY_M),
//   2. nah an der Strecke des aktuellen Abschnitts liegt
//      (höchstens so weit wie seine eigene Ungenauigkeit, mindestens MIN_TOLERANCE_M –
//      der Streckenverlauf der Bahn ist vereinfacht und liegt in Kurven neben dem Gleis),
//   3. zeitlich passt: höchstens MAX_DEVIATION_MIN Fahrminuten von der berechneten
//      Position entfernt (gegen parallele Autobahnen, Wohnen an der Strecke usw.),
//   4. nicht "zurückgelassen" aussieht: Handy steht am letzten Halt still, obwohl die
//      Abfahrt dort schon LEFT_BEHIND_MIN zurückliegt (Zug verpasst).
//
// Er ersetzt die Berechnung nicht, sondern korrigiert sie: Der Vorsprung bzw.
// Rückstand in km entlang der Strecke wird auf die laufende Berechnung aufgeschlagen
// und klingt mit dem Alter des Standorts ab. Ohne neue Standorte landet der Punkt
// so ohne Sprung wieder bei der reinen Berechnung.
//
// Den Zeitpunkt der Messung liefert die Companion-App nicht; maßgeblich ist, wann
// Home Assistant den Standort erhalten hat (last_updated).

import {dist, pointAtAlong} from './position.js';

export const GPS_RULES = Object.freeze({
	MAX_ACCURACY_M: 250,
	MIN_TOLERANCE_M: 175,
	MAX_DEVIATION_MIN: 20,
	MAX_AGE_MS: 10 * 60 * 1000,      // danach wird der Standort gar nicht mehr betrachtet
	DECAY_MS: 3 * 60 * 1000,         // Zeitkonstante des Abklingens
	STILL_MS: 1,                     // m/s: darunter gilt das Handy als stillstehend
	LEFT_BEHIND_KM: 0.4,             // so nah am letzten Halt = "noch am Bahnsteig"
	LEFT_BEHIND_MIN: 3,
	SPEED_MAX_AGE_MS: 2 * 60 * 1000, // bis dahin gilt die Geschwindigkeit als aktuell, danach "zuletzt …"
	// Geschwindigkeit vom Handy nur bei echtem GPS: Standorte aus Mobilfunk/WLAN (≈100 m)
	// melden 0 statt "unbekannt" (beobachtet 08.10.2026 im RE16 und ICE 1604)
	SPEED_MAX_ACCURACY_M: 75,
	// Abgeleitete Geschwindigkeit aus zwei Standorten: nur bei diesem Abstand
	DERIVED_MIN_MS: 30 * 1000,
	DERIVED_MAX_MS: 5 * 60 * 1000,
});

// Standort aus einer device_tracker-Entität lesen
export const readFix = (state) => {
	const a = state?.attributes;
	if (!a || state.state === 'unavailable' || state.state === 'unknown') return null;
	const lat = Number(a.latitude), lon = Number(a.longitude);
	if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
	return {
		lat, lon,
		accuracy: Number.isFinite(Number(a.gps_accuracy)) ? Number(a.gps_accuracy) : Infinity,
		speed: Number.isFinite(Number(a.speed)) ? Number(a.speed) : null, // m/s
		at: Date.parse(state.last_updated),
	};
};

// Kürzester Abstand eines Punkts zur Linie: Lage entlang der Strecke (km) und Abstand (m)
export const projectOnto = (g, [lat, lon]) => {
	let best = null;
	const k = Math.cos(lat * Math.PI / 180);
	for (let i = 0; i < g.coords.length - 1; i++) {
		const [ay, ax] = g.coords[i], [by, bx] = g.coords[i + 1];
		// lokale Ebene in Grad, Länge mit cos(Breite) gestaucht
		const dx = (bx - ax) * k, dy = by - ay;
		const px = (lon - ax) * k, py = lat - ay;
		const len2 = dx * dx + dy * dy;
		const t = len2 > 0 ? Math.min(1, Math.max(0, (px * dx + py * dy) / len2)) : 0;
		const proj = [ay + dy * t, ax + (bx - ax) * t];
		const d = dist([lat, lon], proj) * 1000;
		if (!best || d < best.distM) best = {distM: d, along: g.cum[i] + dist(g.coords[i], proj), point: proj, vertex: i};
	}
	return best;
};

// Mittlere Geschwindigkeit des Abschnitts in km/min (für den zeitlichen Test)
const legSpeed = (g, leg) => {
	const first = leg.stops[0], last = leg.stops.at(-1);
	const t0 = Date.parse(first?.dep?.prognosed ?? first?.dep?.planned);
	const t1 = Date.parse(last?.arr?.prognosed ?? last?.arr?.planned);
	const km = g.cum.at(-1) ?? 0;
	const min = (t1 - t0) / 60000;
	return min > 0 && km > 0 ? km / min : 2; // Notwert: 120 km/h
};

// Abfahrtszeit (Prognose, sonst Plan) des Halts vor der Position
const prevStopDeparture = (leg, g, along) => {
	let prev = null;
	leg.stops.forEach((s, k) => {
		const i = g.stopIdx[k];
		if (i !== null && i !== undefined && g.cum[i] <= along + 1e-6 && s.dep) prev = {along: g.cum[i], dep: Date.parse(s.dep.prognosed ?? s.dep.planned)};
	});
	return prev;
};

// Prüft einen Standort gegen die Regeln. Ergebnis: {ok, reason, offsetKm}
export const evaluateFix = ({fix, view, geometry, computeAt, now, rules = GPS_RULES}) => {
	if (!fix) return {ok: false, reason: 'none'};
	const age = now - fix.at;
	if (!(age >= 0) || age > rules.MAX_AGE_MS) return {ok: false, reason: 'stale'};
	if (fix.accuracy > rules.MAX_ACCURACY_M) return {ok: false, reason: 'inaccurate'};

	const then = computeAt(fix.at);
	if (!then) return {ok: false, reason: 'not-running'};
	const g = geometry[then.leg];
	const proj = projectOnto(g, [fix.lat, fix.lon]);
	if (!proj) return {ok: false, reason: 'no-route'};
	if (proj.distM > Math.max(rules.MIN_TOLERANCE_M, fix.accuracy)) return {ok: false, reason: 'off-route'};

	const offsetKm = proj.along - then.along;
	const leg = view.legs[then.leg];
	if (Math.abs(offsetKm) / legSpeed(g, leg) > rules.MAX_DEVIATION_MIN) return {ok: false, reason: 'implausible-time'};

	// Zug verpasst? Handy steht am letzten Halt, dessen Abfahrt schon eine Weile her ist
	if (fix.speed !== null && fix.speed < rules.STILL_MS && fix.accuracy <= rules.SPEED_MAX_ACCURACY_M) {
		const prev = prevStopDeparture(leg, g, then.along);
		if (prev && Math.abs(proj.along - prev.along) < rules.LEFT_BEHIND_KM && fix.at - prev.dep > rules.LEFT_BEHIND_MIN * 60000) {
			return {ok: false, reason: 'left-behind'};
		}
	}
	return {ok: true, offsetKm, leg: then.leg, age, alongAtFix: proj.along};
};

// Berechnete Position (jetzt) mit GPS-Korrektur. Liefert die Position im Format
// von computePosition plus source: 'gps' | 'computed' und den Grund bei Ablehnung.
export const applyGps = ({pos, fix, view, geometry, computeAt, now, rules = GPS_RULES}) => {
	if (!pos) return null;
	const r = evaluateFix({fix, view, geometry, computeAt, now, rules});
	if (!r.ok || r.leg !== pos.leg) return {...pos, source: 'computed', gpsReason: r.ok ? 'other-leg' : r.reason};
	// Abklingen: exponentiell, zusätzlich linear auf genau 0 bei MAX_AGE – kein Sprung am Ende
	const weight = Math.exp(-r.age / rules.DECAY_MS) * Math.max(0, 1 - r.age / rules.MAX_AGE_MS);
	const g = geometry[pos.leg];
	const along = Math.min(Math.max(pos.along + r.offsetKm * weight, 0), g.cum.at(-1) ?? 0);
	const at = pointAtAlong(g, along);
	return {...pos, point: at.point, vertex: at.vertex, along, stopped: pos.stopped && Math.abs(r.offsetKm * weight) < 0.2, source: 'gps', gpsAge: r.age, gpsWeight: weight, gpsSpeed: fix.speed, gpsAccuracy: fix.accuracy, gpsAlongAtFix: r.alongAtFix, gpsAt: fix.at};
};

// Daten eines akzeptierten Standorts für die Geschwindigkeit (aus applyGps), sonst null
export const fixSample = (pos) => (pos?.source === 'gps'
	? {leg: pos.leg, along: pos.gpsAlongAtFix, at: pos.gpsAt, speed: pos.gpsSpeed, accuracy: pos.gpsAccuracy}
	: null);

// Geschwindigkeit zum aktuellen Standort: {kmh, at, approx} oder null (= unbekannt).
//   1. vom Handy, wenn die Genauigkeit für echtes GPS spricht (auch eine echte 0)
//   2. sonst abgeleitet aus dem Weg entlang der Strecke seit dem vorigen Standort
//   3. sonst unbekannt – die Anzeige behält dann den letzten guten Wert
const round = (kmh) => (kmh < 3 ? 0 : Math.round(kmh));
export const speedFromFixes = ({current, previous, rules = GPS_RULES}) => {
	if (!current) return null;
	if (current.speed !== null && current.speed !== undefined && current.accuracy <= rules.SPEED_MAX_ACCURACY_M) {
		return {kmh: round(current.speed * 3.6), at: current.at, approx: false};
	}
	if (previous && previous.leg === current.leg) {
		const dt = current.at - previous.at;
		if (dt >= rules.DERIVED_MIN_MS && dt <= rules.DERIVED_MAX_MS) {
			const kmh = Math.abs(current.along - previous.along) / (dt / 3600000);
			return {kmh: round(kmh), at: current.at, approx: true};
		}
	}
	return null;
};

// Anzeige: frisch kräftig ("298 km/h"), danach als letzter Wert mit Alter
// ("zuletzt 298 km/h · vor 5 min"). null, wenn es keinen Wert gibt.
export const speedLabel = (last, now, rules = GPS_RULES) => {
	if (!last) return null;
	const age = Math.max(0, now - last.at);
	const value = `${last.approx ? '≈ ' : ''}${last.kmh} km/h`;
	if (age <= rules.SPEED_MAX_AGE_MS) return {text: value, stale: false};
	return {text: `zuletzt ${value} · vor ${Math.round(age / 60000)} min`, stale: true};
};
