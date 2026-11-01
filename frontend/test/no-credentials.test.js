// Integration ohne Zugangsdaten der Bahn: Hinweise ohne Versprechen auf Echtzeit

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {buildTripView} from '../src/view-model.js';
import {renderInfo} from '../src/render-info.js';
import {renderBoard} from '../src/render-board.js';

const leg = (source, matched = false) => ({kind: 'ride', index: 0, matched, source, line: {name: 'RE16', product: 'regional'},
	stopovers: [
		{station: {id: null, name: 'Augsburg Hbf'}, plannedDeparture: '2026-10-08T05:57:00+02:00'},
		{station: {id: null, name: 'Donauwörth'}, plannedArrival: '2026-10-08T06:20:00+02:00', plannedDeparture: '2026-10-08T06:21:00+02:00'},
		{station: {id: null, name: 'Nürnberg Hbf'}, plannedArrival: '2026-10-08T07:05:00+02:00'}]});
const trip = (l) => ({id: 't', ownerId: 'm', ownerName: 'Max', status: 'upcoming', departure: '2026-10-08T05:57:00+02:00',
	arrival: '2026-10-08T07:05:00+02:00', snapshot: {origin: {name: 'Augsburg Hbf'}, destination: {name: 'Nürnberg Hbf'}, legs: [l]},
	realtime: null, seats: {}, realtimeHealth: {failing: false}});

test('Zwischenhalte laut Fahrplan: ohne Zugangsdaten kein „Echtzeit folgt“', () => {
	const v = buildTripView(trip(leg('gtfs')), Date.parse('2026-10-05T12:00:00+02:00'));
	const without = renderInfo(v, {realtime: false});
	assert.match(without, /Zwischenhalte laut Fahrplan, ohne Echtzeit\./);
	assert.doesNotMatch(without, /Echtzeit folgt/);
	assert.match(without, /Ohne Echtzeit \(keine Zugangsdaten der Bahn\) · <a[^>]*>Fahrplan: DELFI e\.V\.\/gtfs\.de, CC BY 4\.0<\/a>/);
	assert.doesNotMatch(without, /Deutsche Bahn AG/);
	const withKey = renderInfo(v, {});
	assert.match(withKey, /Echtzeit folgt etwa einen Tag vorher/);
	assert.match(withKey, /Daten: Deutsche Bahn AG, DELFI e\.V\.\/gtfs\.de, CC BY 4\.0/);
});

test('Noch ohne Zwischenhalte: Hinweis auf den Fahrplan statt auf die Bahn-API', () => {
	const text = leg('text');
	text.stopovers.splice(1, 1);
	const html = renderInfo(buildTripView(trip(text)), {realtime: false});
	assert.match(html, /Bisher nur Ein- und Ausstieg; Zwischenhalte folgen aus dem Fahrplan \(bis 30 Tage vorher\)\./);
});

test('Abfahrtstafel ohne Zugangsdaten sagt, wo man sie einträgt', () => {
	assert.match(renderBoard({status: 'no-credentials'}), /Neu konfigurieren/);
});
