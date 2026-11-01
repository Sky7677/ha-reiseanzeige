// Zugposition auf der echten Strecke Augsburg → Nürnberg → Berlin (RE16 + ICE 1604, 08.10.2026)

import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {buildGeometry, computePosition, splitRoute} from '../src/position.js';
import {buildTripView} from '../src/view-model.js';

const snapshot = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixtures', 'augsburg-berlin.snapshot.json'), 'utf8'));
const trip = (realtime = null, status = 'running') => ({id: 't', status, snapshot, realtime, realtimeHealth: {failing: false}, departure: snapshot.plannedDeparture, arrival: snapshot.plannedArrival});
const at = (hhmm) => Date.parse(`2026-10-08T${hhmm}:00+02:00`);
const km = ([a, b], [c, d]) => Math.hypot((d - b) * 111 * Math.cos(a * Math.PI / 180), (c - a) * 111);

const geometry = buildGeometry(trip());
const posAt = (hhmm, t = trip()) => computePosition(buildTripView(t, at(hhmm)), geometry, at(hhmm));

test('Halte liegen auf dem Streckenverlauf, in Fahrtrichtung', () => {
	for (const g of geometry) {
		const idx = g.stopIdx.filter(i => i !== null);
		assert.deepEqual(idx, [...idx].sort((a, b) => a - b));
	}
	const re = snapshot.legs[0];
	re.stopovers.forEach((s, k) => assert.ok(km([s.station.lat, s.station.lon], geometry[0].coords[geometry[0].stopIdx[k]]) < 1, `${s.station.name} nahe der Linie`));
});

test('Am Bahnhof steht der Punkt bis zur Abfahrt', () => {
	const p = posAt('05:56');
	assert.equal(p.stopped, true);
	assert.ok(km(p.point, [48.365, 10.886]) < 0.5, 'Augsburg Hbf');
});

test('Zwischen zwei Halten: auf der Linie, anteilig zur Zeit', () => {
	const p = posAt('06:00'); // RE16 ab Augsburg 05:57, an Augsburg-Oberhausen kurz danach
	assert.equal(p.stopped, false);
	assert.equal(p.leg, 0);
	const line = geometry[0].coords;
	const nearest = Math.min(...line.map(c => km(c, p.point)));
	assert.ok(nearest < 0.5, 'Punkt liegt auf dem Streckenverlauf');
	assert.ok(p.prev && p.next, 'vorheriger und nächster Halt bekannt');
});

test('Umstieg: Punkt steht in Nürnberg', () => {
	const p = posAt('07:33');
	assert.equal(p.stopped, true);
	assert.ok(km(p.point, [snapshot.legs[2].stopovers[0].station.lat, snapshot.legs[2].stopovers[0].station.lon]) < 0.5);
});

test('Verspätung hält den Punkt zurück', () => {
	const plan = posAt('08:00');
	const ice = snapshot.legs[2];
	const late = {legs: {2: {hasRealtime: true, stopovers: ice.stopovers.map((s, k) => ({
		stationId: s.station.id,
		arrival: {planned: s.plannedArrival, prognosed: s.plannedArrival && new Date(Date.parse(s.plannedArrival) + 20 * 60000).toISOString(), delay: s.plannedArrival ? 1200 : null},
		departure: {planned: s.plannedDeparture, prognosed: s.plannedDeparture && new Date(Date.parse(s.plannedDeparture) + 20 * 60000).toISOString(), delay: s.plannedDeparture ? 1200 : null},
	}))}}};
	const delayed = posAt('08:00', trip(late));
	assert.equal(plan.stopped, false);
	assert.ok(delayed.vertex < plan.vertex, 'mit 20 min Verspätung weiter zurück');
});

test('Gefahren/kommend: Teilung am Punkt, vor der Fahrt alles kommend', () => {
	const p = posAt('08:00');
	const parts = splitRoute(geometry, p);
	assert.equal(parts[0].ahead.length, 0, 'RE16 schon gefahren');
	assert.deepEqual(parts[1].driven.at(-1), p.point);
	assert.deepEqual(parts[1].ahead[0], p.point);
	assert.equal(computePosition(buildTripView(trip(null, 'upcoming'), at('05:00')), geometry, at('05:00')), null);
	assert.ok(splitRoute(geometry, null).every(x => x.driven.length === 0));
});
