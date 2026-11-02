// Umleitung: ICE 1004 fährt ab Erfurt über Leipzig statt Halle. Reise und Verlauf
// stammen aus dem Python-Teil (scripts/make_card_fixture.py, Test-Gleisnetz).

import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {buildTripView} from '../src/view-model.js';
import {buildGeometry, computePosition, dist} from '../src/position.js';
import {legCoords, legStopovers} from '../src/legs.js';
import {renderInfo} from '../src/render-info.js';

const {snapshot, realtime} = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixtures', 'umleitung.python.json'), 'utf8'));
const trip = (snap = snapshot) => ({id: 't', ownerId: 'm', ownerName: 'Max', status: 'running', departure: snap.plannedDeparture,
	arrival: snap.plannedArrival, snapshot: snap, realtime, seats: {}, realtimeHealth: {failing: false}});
const at = (hhmm) => Date.parse(`2026-11-01T${hhmm}:00+01:00`);
const LEIPZIG = [51.3455, 12.3821];
const HALLE = [51.4776, 11.9870];

test('Haltliste folgt dem neuen Laufweg: Halle entfällt, Leipzig kommt dazu', () => {
	const ice = buildTripView(trip(), at('17:30')).legs[2];
	assert.deepEqual(ice.stops.map(s => s.name), ['Nürnberg Hbf', 'Erfurt Hbf', 'Halle (Saale) Hbf', 'Leipzig Hbf', 'Berlin Südkreuz']);
	assert.deepEqual(ice.stops.map(s => s.cancelled), [false, false, true, false, false]);
	assert.deepEqual(ice.stops.map(s => s.added), [false, false, false, true, false]);
	assert.deepEqual(ice.diversion, {cancelled: ['Halle (Saale) Hbf'], added: ['Leipzig Hbf'], endCancelled: false, reported: true, gps: false});
	assert.equal(ice.stops[1].dep.delayMin, 5, 'Echtzeit bleibt beim richtigen Halt');
	assert.equal(ice.stops[3].shown, null, 'neuer Halt ohne Zeit');
	assert.equal(ice.stops[4].arr.delayMin, 5, 'Südkreuz bekommt seine Echtzeit, nicht der neue Halt davor');
	const html = renderInfo(buildTripView(trip(), at('17:30')), {});
	assert.match(html, /Laufweg geändert/);
	assert.match(html, /Geänderter Laufweg\. Halt entfällt: Halle \(Saale\) Hbf; zusätzlich: Leipzig Hbf\./);
	assert.match(html, /Leipzig Hbf <em>zusätzlicher Halt<\/em>/);
});

test('Zug fährt auf dem neuen Verlauf, entfallene Halte liegen nicht darauf', () => {
	const geometry = buildGeometry(trip());
	const ice = snapshot.legs[2];
	assert.deepEqual(geometry[2].coords, ice.diversion.polyline);
	assert.equal(geometry[2].stopIdx[2], null, 'Halle');
	const idx = geometry[2].stopIdx.filter(i => i !== null);
	assert.deepEqual(idx, [...idx].sort((a, b) => a - b));
	const p = computePosition(buildTripView(trip(), at('18:30')), geometry, at('18:30'));
	assert.equal(p.leg, 2);
	assert.equal(p.vertex, 1, 'auf dem Stück Erfurt – Leipzig');
	assert.ok(dist(p.point, LEIPZIG) < 20 && dist(p.point, HALLE) > 20, 'vor Leipzig, nicht über Halle');
});

test('Ohne Verlauf (kurze Liste): gerade Linien über die angefahrenen Halte', () => {
	const light = structuredClone(snapshot);
	light.legs[2].diversion.polyline = null;
	const coords = legCoords(light.legs[2]);
	assert.equal(coords.length, 4, 'ohne Halle');
	assert.ok(coords.some(c => dist(c, LEIPZIG) < 1));
	assert.equal(legStopovers(light.legs[1]), light.legs[1].stopovers, 'Abschnitte ohne Umleitung unverändert');
});

test('Zug endet vorzeitig: Hinweis nennt den nicht erreichten Ausstieg', () => {
	const snap = structuredClone(snapshot);
	const ice = snap.legs[2];
	ice.diversion.stopovers = ice.stopovers.map((s, k) => (k >= 2 ? {...s, cancelled: true} : s));
	ice.diversion.polyline = null;
	const html = renderInfo(buildTripView(trip(snap), at('17:30')), {});
	assert.match(html, /Halte entfallen: Halle \(Saale\) Hbf, Berlin Südkreuz\. Der Zug fährt nicht bis Berlin Südkreuz\./);
});

test('Strecke laut GPS: eigener Hinweis, Haltliste wie geplant', () => {
	const snap = structuredClone(snapshot);
	const ice = snap.legs[2];
	ice.diversion = {path: null, stops: null, gps: {active: true}, stopovers: structuredClone(ice.stopovers),
		polyline: ice.diversion.polyline, geometry: {key: 'g', gps: true}};
	const view = buildTripView(trip(snap), at('17:30')).legs[2];
	assert.equal(view.diversion.gps, true);
	assert.equal(view.diversion.reported, false);
	assert.deepEqual(view.stops.map(s => s.name), ['Nürnberg Hbf', 'Erfurt Hbf', 'Halle (Saale) Hbf', 'Berlin Südkreuz']);
	const html = renderInfo(buildTripView(trip(snap), at('17:30')), {});
	assert.match(html, /Strecke laut GPS/);
	assert.match(html, /Der Zug fährt eine andere Strecke als geplant; die Karte folgt dem Standort des Handys\./);
	assert.doesNotMatch(html, /Geänderter Laufweg/);
});
