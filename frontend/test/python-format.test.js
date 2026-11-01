// Die Karte rechnet mit Reisen, wie sie der Python-Teil der Integration erzeugt
// (scripts/make_card_fixture.py): Text + Fahrplan + Echtzeit, ohne Streckenverlauf.

import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {buildTripView} from '../src/view-model.js';
import {buildGeometry, computePosition} from '../src/position.js';
import {renderInfo} from '../src/render-info.js';

const {snapshot, realtime} = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixtures', 'moritzplatz-berlin.python.json'), 'utf8'));
const trip = (status) => ({id: 't', ownerId: 'm', ownerName: 'Max', status, departure: snapshot.plannedDeparture, arrival: snapshot.plannedArrival,
	snapshot, realtime, seats: {}, realtimeHealth: {failing: false}});
const at = (hhmm) => Date.parse(`2026-11-01T${hhmm}:00+01:00`);

test('Reiseinformationen aus dem Python-Format: alle Abschnitte, Verspätung, Gleiswechsel', () => {
	const v = buildTripView(trip('running'), at('17:30'));
	assert.deepEqual(v.legs.map(l => l.name), ['STR 1', 'RE1', 'ICE 1004', 'S41', 'U3']);
	const ice = v.legs[2];
	assert.deepEqual(ice.stops.map(s => s.name), ['Nürnberg Hbf', 'Erfurt Hbf', 'Halle (Saale) Hbf', 'Berlin Südkreuz']);
	const erfurt = ice.stops[1];
	assert.equal(erfurt.dep.delayMin, 5);
	assert.equal(erfurt.platform, '2');
	assert.ok(erfurt.platformChanged);
	assert.equal(erfurt.state, 'next');
	assert.equal(v.legs[1].transfer.minutes, 47, 'Umstieg Nürnberg 15:49 → 16:36');
	assert.equal(v.legs[0].transfer.station, 'Königsplatz, Augsburg (Bayern) → Augsburg Hbf', 'Fußweg zwischen verschiedenen Halten');
	assert.ok(v.bahnLink.endsWith('00000000-0000-4000-8000-000000000002'));
	assert.match(renderInfo(v, {owned: true}), /ICE 1004/);
});

test('Zugposition ohne Streckenverlauf: gerade Linien zwischen Halten mit Koordinaten', () => {
	const t = trip('running');
	const g = buildGeometry(t);
	assert.equal(g.length, 5);
	assert.equal(g[0].coords.length, 0, 'Tram ohne Koordinaten: keine Linie');
	assert.equal(g[2].coords.length, 4, 'ICE: vier Halte mit Koordinaten');
	// 17:30: zwischen Nürnberg (16:36) und Erfurt (an 17:42, Prognose 17:47)
	const pos = computePosition(buildTripView(t, at('17:30')), g, at('17:30'));
	assert.equal(pos.leg, 2);
	assert.ok(pos.point[0] > 49.4457 && pos.point[0] < 50.9725, `Breite ${pos.point[0]}`);
	// Während der Tramfahrt: kein Punkt, aber auch kein Absturz
	assert.doesNotThrow(() => computePosition(buildTripView(t, at('14:20')), g, at('14:20')));
});

test('Noch nicht im Fahrplan gefundener Bahn-Abschnitt: Hinweis statt leerer Zwischenhalte', () => {
	const t = structuredClone(trip('upcoming'));
	t.snapshot.legs[2].matched = false;
	const v = buildTripView(t, at('10:00'));
	assert.equal(v.legs[2].pending, true);
	assert.equal(v.legs[0].pending, false, 'Tram: kein Hinweis');
	assert.equal(v.legs[1].pending, false, 'gefunden');
	assert.match(renderInfo(v, {owned: true}), /folgen, sobald der Fahrplan/);
});
