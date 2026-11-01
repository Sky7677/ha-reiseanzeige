// Logik der Dashboard-Karte: Auswahl der Reise, Reiseinformationen, Ingress-Anmeldung.

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {selectTrip, switcherGroups} from '../src/select.js';
import {buildTripView} from '../src/view-model.js';
import {renderInfo} from '../src/render-info.js';
import {createHaClient, CardError} from '../src/ha-client.js';

const T = (h, m = 0) => `2026-10-08T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00+02:00`;
const trip = (id, ownerId, status, dep, arr = dep) => ({id, ownerId, ownerName: ownerId, status, departure: dep, arrival: arr, snapshot: {destination: {name: 'Z'}}});

test('Auswahl: manuell, dann eigene (läuft > gerade abgeschlossen > anstehend), dann andere', () => {
	const trips = [
		trip('a-later', 'anna', 'upcoming', T(12)), trip('a-next', 'anna', 'upcoming', T(9)),
		trip('b-run', 'ben', 'running', T(7)), trip('a-done', 'anna', 'completed', T(5), T(6)),
	];
	assert.equal(selectTrip(trips, {userId: 'anna'}).id, 'a-done', 'gerade angekommen vor nächster Reise');
	assert.equal(selectTrip(trips.filter(t => t.id !== 'a-done'), {userId: 'anna'}).id, 'a-next');
	assert.equal(selectTrip(trips, {userId: 'flur'}).id, 'b-run', 'Flur: laufende Reise einer Person');
	assert.equal(selectTrip(trips, {userId: 'anna', manualId: 'b-run'}).id, 'b-run');
	assert.equal(selectTrip(trips, {userId: 'anna', manualId: 'weg'}).id, 'a-done', 'entfernte Wahl fällt zurück');
	assert.equal(selectTrip([], {userId: 'anna'}), null, 'Ruheansicht');
	assert.deepEqual(switcherGroups(trips).map(g => [g.ownerId, g.trips.map(t => t.id)]), [['ben', ['b-run']], ['anna', ['a-done', 'a-next', 'a-later']]]);
});

// Reise mit zwei Zügen und Umstieg, Echtzeit wie sie die App liefert
const st = (id, name, arr, dep, pf) => ({station: {id, name, lat: 48, lon: 11}, plannedArrival: arr, plannedDeparture: dep, plannedPlatform: pf});
const rtStop = (id, arr, dep, {da = null, dd = null, pf = null, changed = false, cancelled = false} = {}) => ({
	stationId: id, cancelled,
	arrival: {planned: arr, prognosed: da === null ? null : new Date(Date.parse(arr) + da * 1000).toISOString(), delay: da},
	departure: {planned: dep, prognosed: dd === null ? null : new Date(Date.parse(dep) + dd * 1000).toISOString(), delay: dd},
	platform: pf, plannedPlatform: pf, platformChanged: changed,
});
const fullTrip = (status, realtime) => ({
	id: 't', ownerName: 'Max', status, departure: T(6), arrival: T(10, 17),
	snapshot: {origin: {name: 'Augsburg Hbf'}, destination: {name: 'Berlin Südkreuz'}, legs: [
		{kind: 'ride', index: 0, line: {name: 'RE16'}, direction: 'Nürnberg Hbf', stopovers: [st('A', 'Augsburg Hbf', null, T(6), '5'), st('D', 'Donauwörth', T(6, 30), T(6, 31), '3'), st('N', 'Nürnberg Hbf', T(7, 30), null, '9')]},
		{kind: 'walk', index: 1},
		{kind: 'ride', index: 2, line: {name: 'ICE 1604'}, direction: 'Berlin Hbf', stopovers: [st('N', 'Nürnberg Hbf', null, T(7, 36), '6'), st('E', 'Erfurt Hbf', T(8, 40), T(8, 42), '2'), st('S', 'Berlin Südkreuz', T(10, 17), null, '7')]},
	]},
	seats: {2: {coach: '9', seat: '64'}},
	realtime, realtimeHealth: {failing: false, lastSuccessAt: T(6, 40)},
});

test('Reiseinformationen: Zeiten, Verspätung, Gleiswechsel, nächster Halt, Umstieg, Sitzplatz', () => {
	const rt = {legs: {
		0: {hasRealtime: true, stopovers: [rtStop('A', null, T(6), {dd: 0, pf: '5'}), rtStop('D', T(6, 30), T(6, 31), {da: 240, dd: 240, pf: '3'}), rtStop('N', T(7, 30), null, {da: 240, pf: '10', changed: true})]},
		2: {hasRealtime: false, stopovers: [rtStop('N', null, T(7, 36)), rtStop('E', T(8, 40), T(8, 42)), rtStop('S', T(10, 17), null)]},
	}};
	const v = buildTripView(fullTrip('running', rt), Date.parse(T(6, 33)));
	const [re, ice] = v.legs;
	assert.deepEqual(re.stops.map(s => s.state), ['past', 'next', 'future'], 'Donauwörth: Abfahrt 06:35 noch nicht erreicht');
	assert.equal(re.stops[1].dep.delayMin, 4);
	assert.equal(re.stops[0].dep.delayMin, 0, 'pünktlich = 0, nicht leer');
	assert.equal(ice.stops[1].dep.delayMin, null, 'keine Echtzeit = leer');
	assert.equal(re.stops[2].platform, '10');
	assert.equal(re.stops[2].platformChanged, true);
	assert.deepEqual(re.transfer, {station: 'Nürnberg Hbf', minutes: 2, fromPlatform: '10', toPlatform: '6', risky: true}, 'Verspätung frisst Umstiegszeit');
	assert.equal(ice.seat, 'Wagen 9, Platz 64');
	const html = renderInfo(v);
	assert.match(html, /Anschluss gefährdet/);
	assert.match(html, /Richtung Berlin Hbf/);
});

test('Reiseinformationen: ausgefallener Halt, Ausfall-Hinweis, Echtzeit-Störung', () => {
	const rt = {legs: {0: {hasRealtime: true, stopovers: [rtStop('A', null, T(6), {dd: 0}), rtStop('D', T(6, 30), T(6, 31), {cancelled: true}), rtStop('N', T(7, 30), null, {da: 0})]}}};
	const v = buildTripView(fullTrip('upcoming', rt), Date.parse(T(5)));
	assert.equal(v.legs[0].stops[1].cancelled, true);
	assert.equal(v.legs[0].stops[1].dep.prognosed, null);
	assert.deepEqual(v.legs[0].stops.map(s => s.state), ['future', 'future', 'future'], 'vor der Abfahrt nichts hervorgehoben');
	assert.match(renderInfo(v), /entfällt/);

	const failing = {...fullTrip('running', rt), realtimeHealth: {failing: true}};
	const vf = buildTripView(failing, Date.parse(T(6, 10)));
	assert.equal(vf.legs[0].stops[1].cancelled, false, 'bei Störung Fahrplan statt alter Echtzeit');
	assert.match(renderInfo(vf), /Echtzeitdaten nicht verfügbar/);
});

test('Reiseinformationen: abgeschlossen, Text wird maskiert', () => {
	const t = fullTrip('completed', null);
	t.snapshot.legs[0].line.name = '<b>RE</b>';
	const v = buildTripView(t, Date.parse(T(11)));
	assert.ok(v.legs.every(l => l.stops.every(s => s.state === 'past')));
	const html = renderInfo(v);
	assert.match(html, /Reise abgeschlossen/);
	assert.ok(!html.includes('<b>RE</b>') && html.includes('&lt;b&gt;RE&lt;/b&gt;'));
});

// Verbindung zur Integration über die WebSocket-Befehle von Home Assistant
const fakeHass = (handlers) => {
	const calls = [];
	return {calls, callWS: async (msg) => {
		calls.push(msg);
		const h = handlers[msg.type];
		if (!h) throw {code: 'unknown_command', message: 'Unknown command.'};
		return h(msg);
	}};
};

test('Integration: Pfade werden auf WebSocket-Befehle abgebildet, Fehler verständlich', async () => {
	const hass = fakeHass({
		'reiseanzeige/map_base': () => ({base: '/api/reiseanzeige/TOKEN/'}),
		'reiseanzeige/me': () => ({id: 'u'}),
		'reiseanzeige/trips': () => ({trips: []}),
		'reiseanzeige/add': (m) => ({ok: true, message: m.text}),
		'reiseanzeige/trip': (m) => ({id: m.trip_id}),
		'reiseanzeige/delete': (m) => ({id: m.trip_id}),
		'reiseanzeige/seat': (m) => m,
		'reiseanzeige/locate': () => ({status: 'cooldown', retryInSec: 12}),
		'reiseanzeige/board': (m) => ({status: 'ok', station: m.station ?? null}),
	});
	const client = createHaClient({hass: () => hass});
	assert.deepEqual(await client.request('api/me'), {id: 'u'});
	assert.equal(client.base, '/api/reiseanzeige/TOKEN/');
	assert.deepEqual(await client.request('api/trips', {method: 'POST', body: {text: 'A → B'}}), {ok: true, message: 'A → B'});
	assert.deepEqual(await client.request('api/trips/a%20b'), {id: 'a b'});
	assert.deepEqual(await client.request('api/trips/x', {method: 'DELETE'}), {id: 'x'});
	const seat = await client.request('api/trips/x/seats/2', {method: 'PUT', body: {coach: '9', seat: '64'}});
	assert.deepEqual([seat.trip_id, seat.leg, seat.coach, seat.seat], ['x', 2, '9', '64']);
	assert.deepEqual(await client.request('api/board?station=Ulm%20Hbf'), {status: 'ok', station: 'Ulm Hbf'});
	assert.deepEqual(await client.request('api/board'), {status: 'ok', station: null});
	await assert.rejects(client.request('api/trips/x/locate', {method: 'POST'}), (e) => e.code === 'COOLDOWN' && e.data.retryInSec === 12);

	await assert.rejects(createHaClient({hass: () => fakeHass({})}).request('api/me'), (e) => e instanceof CardError && e.code === 'not-installed');
	const failing = fakeHass({'reiseanzeige/map_base': () => ({base: '/b/'}), 'reiseanzeige/trip': () => { throw {code: 'not_found', message: 'Reise nicht gefunden'}; }});
	await assert.rejects(createHaClient({hass: () => failing}).request('api/trips/x'), {code: 'NOT_FOUND', message: 'Reise nicht gefunden'});
});

import {switcherLabels} from '../src/select.js';

test('Umschalter: Datum nur bei Verwechslungsgefahr, Uhrzeit nur am selben Tag', () => {
	const t = (id, owner, dest, dep) => ({id, ownerId: owner, ownerName: owner, departure: dep, snapshot: {destination: {name: dest}}});
	const l = switcherLabels([
		t('a', 'Max', 'Berlin', '2026-10-02T21:17:00+02:00'), t('b', 'Max', 'Berlin', '2026-10-08T06:10:00+02:00'),
		t('c', 'Anna', 'Berlin', '2026-10-08T06:10:00+02:00'),
		t('d', 'Ben', 'Hamburg', '2026-10-09T08:00:00+02:00'), t('e', 'Ben', 'Hamburg', '2026-10-09T17:00:00+02:00'),
	]);
	assert.equal(l.get('a').extra, 'Fr., 02.10.');
	assert.equal(l.get('b').extra, 'Do., 08.10.');
	assert.equal(l.get('c').extra, '', 'andere Person: kein Datum nötig');
	assert.equal(l.get('d').extra, 'Fr., 09.10. 08:00');
});

test('Link zum DB Navigator: nur mit gültiger vbid, sonst kein Link', () => {
	const t = fullTrip('upcoming', null);
	t.snapshot.vbid = '00000000-0000-4000-8000-000000000001';
	const html = renderInfo(buildTripView(t));
	assert.match(html, /href="https:\/\/www\.bahn\.de\/buchung\/start\?vbid=00000000-0000-4000-8000-000000000001"/);
	assert.match(html, /target="_blank" rel="noopener noreferrer"/);
	t.snapshot.vbid = null;
	assert.ok(!renderInfo(buildTripView(t)).includes('bahn-link'));
	t.snapshot.vbid = '"><script>x</script>';
	assert.ok(!renderInfo(buildTripView(t)).includes('bahn-link'), 'manipulierte vbid wird ignoriert');
});

import {delayClass, renderBoard} from '../src/render-board.js';

test('Abfahrtstafel: pünktlich grün, bis 3 Minuten gelb, ab 4 Minuten rot', () => {
	assert.deepEqual([0, 1, 3, 4, 12].map(delayClass), ['ok', 'minor', 'minor', 'late', 'late']);
	const dep = (delayMin) => ({id: String(delayMin), line: 'RE9', destination: 'Ulm Hbf', via: [], planned: '2026-10-08T05:50:00Z', prognosed: new Date(Date.parse('2026-10-08T05:50:00Z') + delayMin * 60000).toISOString(), delayMin, platform: '7', platformChanged: false, cancelled: false, added: false, messages: []});
	const html = renderBoard({status: 'ok', station: {name: 'Augsburg Hbf'}, fetchedAt: '2026-10-08T05:40:00Z', departures: [dep(0), dep(3), dep(4)]});
	assert.deepEqual([...html.matchAll(/class="rt (\w+)"/g)].map(m => m[1]), ['ok', 'minor', 'late']);
});

test('Reiseinformationen: Zwischenhalte aus dem Fahrplan (GTFS) mit eigenem Hinweis und Quelle', () => {
	const trip = fullTrip('upcoming', null);
	trip.snapshot.legs[0].matched = false;
	trip.snapshot.legs[0].source = 'gtfs';
	trip.snapshot.legs[0].line.product = 'regional';
	trip.snapshot.legs[2].matched = false;
	trip.snapshot.legs[2].line.product = 'longdistance';
	const v = buildTripView(trip, Date.parse(T(5)));
	assert.equal(v.legs[0].planned, true);
	assert.equal(v.legs[0].pending, false, 'kein „folgen“-Hinweis, die Halte sind ja da');
	assert.equal(v.legs[1].pending, true, 'ohne Fahrplan wie bisher');
	const html = renderInfo(v);
	assert.match(html, /Zwischenhalte laut Fahrplan; Echtzeit folgt etwa einen Tag vorher/);
	assert.match(html, /DELFI e\.V\.\/gtfs\.de, CC BY 4\.0/);
});

test('Umstieg: Gleis nur, wenn bekannt – kein „?“ beim Bus', () => {
	const trip = fullTrip('upcoming', null);
	const v = buildTripView(trip, Date.parse(T(5)));
	const t = v.legs[0].transfer;
	const html = renderInfo({...v, legs: [{...v.legs[0], transfer: {...t, toPlatform: null}}]});
	assert.doesNotMatch(html, /→ \?/);
	assert.match(html, new RegExp(`Gl\\. ${t.fromPlatform}(?! →)`));
	assert.match(renderInfo({...v, legs: [{...v.legs[0], transfer: {...t, fromPlatform: null}}]}), /→ Gl\. 6/);
	assert.match(renderInfo({...v, legs: [{...v.legs[0], transfer: {...t, fromPlatform: null, toPlatform: null}}]}), /Umstieg Nürnberg Hbf · 6 min\s*</);
});
