// Berechnete Zugposition (Konzept: "Positionsberechnung").
// Zwischen zwei Halten anteilig zur Zeit entlang des Streckenverlaufs, mit
// Ist-/Prognose-Abfahrt am letzten und Prognose-Ankunft am nächsten Halt.
// Am Bahnhof steht der Punkt, bis die Abfahrt erreicht ist; beim Umstieg am
// Umstiegsbahnhof. Entfallene Halte werden übersprungen.
// Reine Funktionen: Strecke + aufbereitete Reise (view-model) + Uhrzeit.
// Bei einer Umleitung gelten deren Haltliste und Verlauf (legs.js).

import {legCoords, legStopovers} from './legs.js';

const toRad = Math.PI / 180;
// Abstand in km; für Strecken innerhalb Europas genau genug
export const dist = ([la1, lo1], [la2, lo2]) => {
	const x = (lo2 - lo1) * toRad * Math.cos(((la1 + la2) / 2) * toRad);
	const y = (la2 - la1) * toRad;
	return Math.hypot(x, y) * 6371;
};

// Pro Fahrtabschnitt: Linie, Lage jedes Halts auf der Linie, aufsummierte Länge
export const buildGeometry = (trip) => trip.snapshot.legs.filter(l => l.kind === 'ride').map((leg) => {
	const coords = legCoords(leg);
	const cum = [0];
	for (let i = 1; i < coords.length; i++) cum.push(cum[i - 1] + dist(coords[i - 1], coords[i]));
	let from = 0;
	const stopIdx = legStopovers(leg).map((s) => {
		const p = [s.station?.lat, s.station?.lon];
		// entfallene Halte liegen nicht auf dem neuen Verlauf
		if (s.cancelled || !Number.isFinite(p[0]) || coords.length === 0) return null;
		// nächster Linienpunkt, nur vorwärts suchen (Halte kommen in Fahrtrichtung)
		let best = from, bestD = Infinity;
		for (let i = from; i < coords.length; i++) {
			const d = dist(p, coords[i]);
			if (d < bestD) { bestD = d; best = i; }
		}
		from = best;
		return best;
	});
	return {coords, cum, stopIdx};
});

const eff = (t) => (t ? Date.parse(t.prognosed ?? t.planned) : NaN);

// Punkt bei Anteil f zwischen den Halten kA und kB eines Abschnitts
const interpolate = (g, stops, kA, kB, f) => {
	const iA = g.stopIdx[kA], iB = g.stopIdx[kB];
	if (iA === null || iB === null || iB <= iA) {
		const a = [stops[kA].lat, stops[kA].lon], b = [stops[kB].lat, stops[kB].lon];
		return {point: [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f], vertex: iA ?? 0};
	}
	const target = g.cum[iA] + f * (g.cum[iB] - g.cum[iA]);
	let i = iA;
	while (i < iB - 1 && g.cum[i + 1] < target) i++;
	const seg = g.cum[i + 1] - g.cum[i];
	const t = seg > 0 ? (target - g.cum[i]) / seg : 0;
	const a = g.coords[i], b = g.coords[i + 1];
	return {point: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], vertex: i};
};

const stopPoint = (g, stops, k) => ({point: [stops[k].lat, stops[k].lon], vertex: g.stopIdx[k] ?? 0, atStop: true});

// Kilometer entlang des Abschnitts für einen Punkt auf (oder nahe) Linienpunkt `vertex`
export const alongOf = (g, vertex, point) => {
	if (!g.coords.length) return 0;
	const v = Math.min(Math.max(vertex, 0), g.coords.length - 1);
	return g.cum[v] + dist(g.coords[v], point);
};

// Punkt bei `along` km entlang des Abschnitts
export const pointAtAlong = (g, along) => {
	const total = g.cum.at(-1) ?? 0;
	const a = Math.min(Math.max(along, 0), total);
	let i = 0;
	while (i < g.coords.length - 2 && g.cum[i + 1] < a) i++;
	const seg = (g.cum[i + 1] ?? g.cum[i]) - g.cum[i];
	const t = seg > 0 ? (a - g.cum[i]) / seg : 0;
	const p = g.coords[i], q = g.coords[i + 1] ?? g.coords[i];
	return {point: [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t], vertex: i};
};

// Position während der Fahrt; null vor bzw. nach der Reise.
// Ergebnis: {leg, point:[lat,lon], vertex, stopped, prev, next} – prev/next als Koordinaten
const validPoint = (p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]);

export const computePosition = (view, geometry, now) => {
	const pos = computeRaw(view, geometry, now);
	if (!pos) return null;
	if (!validPoint(pos.prev)) pos.prev = null;
	if (!validPoint(pos.next)) pos.next = null;
	if (!validPoint(pos.point)) {
		// Abschnitt ohne Koordinaten (Tram, U-Bahn, Bus ohne Lagedaten): Zug bleibt am
		// letzten bekannten Punkt stehen, z. B. am Umstiegsbahnhof
		if (!pos.prev) return null;
		Object.assign(pos, {point: pos.prev, vertex: 0, stopped: true});
	}
	const g = geometry[pos.leg];
	pos.along = g && g.coords.length ? alongOf(g, pos.vertex, pos.point) : 0;
	return pos;
};

const computeRaw = (view, geometry, now) => {
	if (view.status !== 'running') return null;
	let lastStop = null;
	for (let li = 0; li < view.legs.length; li++) {
		const g = geometry[li];
		const stops = view.legs[li].stops;
		// angefahrene Halte mit Lage und Zeit (neue Halte einer Umleitung haben keine Zeit)
		const usable = stops.map((s, k) => ({k, s})).filter(({s}) => !s.cancelled && Number.isFinite(s.lat) && (s.arr || s.dep));
		for (let j = 0; j < usable.length; j++) {
			const {k, s} = usable[j];
			const arr = Number.isNaN(eff(s.arr)) ? eff(s.dep) : eff(s.arr);
			const dep = Number.isNaN(eff(s.dep)) ? arr : eff(s.dep);
			const next = usable[j + 1]?.s ?? view.legs[li + 1]?.stops[0] ?? null;
			if (now < arr) {
				const p = usable[j - 1];
				if (!p) {
					// vor dem ersten Halt dieses Abschnitts: Umstieg bzw. Abfahrt steht bevor
					return {leg: li, ...stopPoint(g, stops, k), stopped: true, prev: lastStop, next: [s.lat, s.lon]};
				}
				const pdep = Number.isNaN(eff(p.s.dep)) ? eff(p.s.arr) : eff(p.s.dep);
				const f = Math.min(1, Math.max(0, (now - pdep) / Math.max(1, arr - pdep)));
				return {leg: li, ...interpolate(g, stops, p.k, k, f), stopped: false, prev: [p.s.lat, p.s.lon], next: [s.lat, s.lon]};
			}
			if (now < dep) {
				return {leg: li, ...stopPoint(g, stops, k), stopped: true, prev: lastStop, next: next ? [next.lat, next.lon] : null};
			}
			lastStop = [s.lat, s.lon];
		}
	}
	// Reise läuft laut Status noch, alle Zeiten vorbei: am Ziel
	const li = view.legs.length - 1;
	const stops = view.legs[li].stops;
	return {leg: li, ...stopPoint(geometry[li], stops, stops.length - 1), stopped: true, prev: lastStop, next: null};
};

// Teilt die Strecke in bereits gefahren und noch kommend
export const splitRoute = (geometry, pos) => geometry.map((g, li) => {
	if (!pos || li > pos.leg) return {driven: [], ahead: g.coords};
	if (li < pos.leg) return {driven: g.coords, ahead: []};
	const v = Math.min(pos.vertex, g.coords.length - 1);
	return {
		driven: [...g.coords.slice(0, v + 1), pos.point],
		ahead: [pos.point, ...g.coords.slice(v + 1)],
	};
});
