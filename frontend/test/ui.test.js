// Bedienung der fertig gebauten Karte in einem nachgebauten Browser (jsdom):
// Reise per Link hinzufügen, Wagen/Platz eintragen, löschen mit Nachfrage.

import {test, before, after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {JSDOM} from 'jsdom';

const root = path.join(import.meta.dirname, '..', '..');
const snap = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixtures', 'augsburg-berlin.snapshot.json'), 'utf8'));
const tick = (ms = 30) => new Promise(r => setTimeout(r, ms));

// Attrappe der Schnittstelle mit Zustand (als HTTP-Pfade; callWS unten bildet die WebSocket-Befehle darauf ab)
const api = {trips: [], calls: []};
const makeTrip = (id, ownerId, ownerName) => ({id, ownerId, ownerName, status: 'upcoming', departure: snap.plannedDeparture, arrival: snap.plannedArrival, snapshot: snap, seats: {}, realtime: null, realtimeHealth: {failing: false}});
const reply = (status, body) => ({ok: status < 300, status, json: async () => body});
const fakeFetch = async (url, opts = {}) => {
	const pathName = url.replace('/api/hassio_ingress/T/', '');
	const method = opts.method ?? 'GET';
	const body = opts.body ? JSON.parse(opts.body) : null;
	api.calls.push(`${method} ${pathName}`);
	if (pathName === 'api/me') return reply(200, {id: 'max', name: 'Max', configured: true, canAdd: true, station: 'Augsburg Hbf'});
	if (pathName.startsWith('api/board')) return reply(200, {status: 'ok', station: {eva: '8000013', name: 'Augsburg Hbf'}, fetchedAt: '2026-10-08T05:40:00Z', departures: [
		{id: '1', line: 'ICE 1291', destination: 'München Hbf', via: ['München-Pasing'], planned: '2026-10-08T05:42:00Z', prognosed: '2026-10-08T05:48:00Z', delayMin: 6, platform: '5', platformChanged: true, cancelled: false, added: false, messages: ['Verspätung aus vorheriger Fahrt']},
		{id: '2', line: 'RB87', destination: 'Mindelheim', via: [], planned: '2026-10-08T05:58:00Z', prognosed: null, delayMin: null, platform: '2', platformChanged: false, cancelled: true, added: false, messages: []},
	]});
	if (pathName === 'api/trips' && method === 'GET') return reply(200, {trips: api.trips});
	if (pathName === 'api/trips' && method === 'POST') {
		if (!body.text.includes('vbid')) return reply(400, {error: 'INVALID_LINK', message: 'Das ist kein Reise-Link.'});
		const t = makeTrip('neu', 'max', 'Max');
		api.trips.push(t);
		return reply(201, {status: 'saved', trip: t, replaced: [], message: 'Reise gespeichert.'});
	}
	if (/^api\/trips\/[^/]+\/locate$/.test(pathName) && method === 'POST') return api.locateReply ?? reply(202, {status: 'sent'});
	const m = /^api\/trips\/([^/]+)(?:\/seats\/(\d+))?$/.exec(pathName);
	const trip = api.trips.find(t => t.id === m?.[1]);
	if (m && method === 'PUT') { trip.seats[m[2]] = {coach: body.coach || null, seat: body.seat || null}; return reply(200, trip); }
	if (m && method === 'DELETE') { api.trips = api.trips.filter(t => t !== trip); return reply(200, {}); }
	if (m && method === 'GET') return reply(200, trip);
	return reply(404, {});
};

// WebSocket-Befehle der Integration → Pfade der Attrappe
const wsRoute = (m) => ({
	'reiseanzeige/me': ['GET', 'api/me'],
	'reiseanzeige/trips': ['GET', 'api/trips'],
	'reiseanzeige/add': ['POST', 'api/trips', {text: m.text}],
	'reiseanzeige/trip': ['GET', `api/trips/${m.trip_id}`],
	'reiseanzeige/delete': ['DELETE', `api/trips/${m.trip_id}`],
	'reiseanzeige/seat': ['PUT', `api/trips/${m.trip_id}/seats/${m.leg}`, {coach: m.coach, seat: m.seat}],
	'reiseanzeige/locate': ['POST', `api/trips/${m.trip_id}/locate`],
	'reiseanzeige/board': ['GET', `api/board${m.station ? `?station=${encodeURIComponent(m.station)}` : ''}`],
})[m.type];
const callWS = async (m) => {
	if (m.type === 'reiseanzeige/map_base') return {base: '/api/reiseanzeige/T/'};
	const route = wsRoute(m);
	if (!route) throw {code: 'unknown_command', message: 'Unknown command.'};
	const [method, p, body] = route;
	const res = await fakeFetch('/api/hassio_ingress/T/' + p, {method, body: body ? JSON.stringify(body) : undefined});
	const data = await res.json();
	if (!res.ok && data.error === 'COOLDOWN') return {status: 'cooldown', retryInSec: data.retryInSec};
	if (!res.ok) throw {code: String(data.error ?? 'error').toLowerCase(), message: data.message};
	return data;
};

let card, $, $$;
before(async () => {
	await import('../../scripts/build-card.mjs'); // baut custom_components/reiseanzeige/frontend/reiseanzeige-card.js
	const dom = new JSDOM('<!doctype html><body></body>', {url: 'https://ha.local/lovelace/0', pretendToBeVisual: true});
	const w = dom.window;
	Object.defineProperty(globalThis, 'navigator', {value: w.navigator, configurable: true});
	for (const k of ['window', 'document', 'HTMLElement', 'customElements', 'location', 'Node', 'Element', 'getComputedStyle', 'requestAnimationFrame', 'cancelAnimationFrame']) globalThis[k] = w[k];
	w.SVGSVGElement.prototype.createSVGRect = () => ({});
	w.HTMLCanvasElement.prototype.getContext = () => null; // kein WebGL → Rasterkarte
	w.ResizeObserver = globalThis.ResizeObserver = class { constructor (cb) { this.cb = cb; } observe () { setTimeout(() => this.cb([]), 5); } disconnect () {} };
	globalThis.fetch = w.fetch = fakeFetch;
	await import(path.join(root, 'custom_components', 'reiseanzeige', 'frontend', 'reiseanzeige-card.js'));
	card = document.createElement('reiseanzeige-card');
	card.setConfig({});
	document.body.append(card);
	card.hass = {states: {}, themes: {darkMode: false}, callWS};
	await tick(100);
	$ = (s) => card.shadowRoot.querySelector(s);
	$$ = (s) => [...card.shadowRoot.querySelectorAll(s)];
});

// Karte entfernen: beendet ihre Zeitgeber, sonst läuft der Test nie zu Ende
after(() => { card?.remove(); });

const submit = (form) => form.dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true}));

test('Ohne Reisen: Ruheansicht mit Abfahrtstafel und „+ Reise“', async () => {
	await tick(50);
	assert.match($('.status').textContent, /Keine anstehenden Reisen/);
	assert.equal($('.board-title').textContent, 'Abfahrten Augsburg Hbf');
	const rows = $$('.board tbody tr');
	assert.equal(rows.length, 2);
	assert.match(rows[0].textContent, /ICE 1291[\s\S]*München Hbf/);
	assert.ok(rows[0].querySelector('.pf.changed'), 'Gleiswechsel markiert');
	assert.ok(rows[1].classList.contains('cancelled'));
	assert.match($('.board-foot').textContent, /CC BY 4\.0/);
	assert.equal($('.add-toggle').classList.contains('hidden'), false);
});

test('Reise per Link hinzufügen; Fehler werden angezeigt', async () => {
	$('.add-toggle').click();
	const form = $('.add-form');
	assert.equal(form.classList.contains('hidden'), false);
	form.elements.namedItem('link').value = 'Hallo';
	submit(form);
	await tick();
	assert.match($('.add-msg').textContent, /kein Reise-Link/);
	form.elements.namedItem('link').value = 'https://www.bahn.de/buchung/start?vbid=x';
	submit(form);
	await tick(100);
	assert.match($('.add-msg').textContent, /gespeichert/);
	assert.match($('.trip-head .route').textContent, /Augsburg Hbf → Berlin Südkreuz/);
});

test('Wagen/Platz eintragen; Formular übersteht eine Aktualisierung', async () => {
	$('[data-action="seat-edit"][data-leg="2"]').click();
	const form = $('.seat-form');
	form.elements.namedItem('coach').value = '9';
	form.elements.namedItem('seat').value = '64';
	card._render(); // minütliche Aktualisierung während der Eingabe
	assert.equal($('.seat-form').elements.namedItem('coach').value, '9', 'Eingabe bleibt erhalten');
	submit($('.seat-form'));
	await tick(100);
	assert.ok(api.calls.includes('PUT api/trips/neu/seats/2'));
	assert.match($('.info').textContent, /Wagen 9, Platz 64/);
	assert.equal($('.seat-form'), null);
});

test('Löschen erst nach Nachfrage', async () => {
	$('[data-action="delete"]').click();
	assert.equal($('[data-action="delete"]').textContent, 'Wirklich löschen?');
	assert.ok(!api.calls.some(c => c.startsWith('DELETE')));
	$('[data-action="delete"]').click();
	await tick(100);
	assert.ok(api.calls.includes('DELETE api/trips/neu'));
	assert.match($('.status').textContent, /Keine anstehenden Reisen/);
});

test('Fremde Reise: Bearbeiten und Löschen ebenfalls möglich', async () => {
	api.trips.push(makeTrip('anna1', 'anna', 'Anna'));
	await card._load();
	assert.match($('.trip-head .who').textContent, /Anna/);
	assert.ok($('.info [data-action="delete"]'));
	assert.ok($('.info [data-action="seat-edit"]'));
});

test('Standort anfordern: Knopf nur während der Fahrt, dreht bis zum neuen Standort, dann kurz gesperrt', async () => {
	const anna = api.trips.find(t => t.id === 'anna1');
	await card._load();
	assert.equal($('.trip-head .locate'), null, 'vor der Abfahrt kein Knopf');
	Object.assign(anna, {status: 'running', ownerCanLocate: true, ownerLocation: 'device_tracker.annas_handy'});
	const hass = card._hass;
	card.hass = {...hass, states: {...hass.states, 'device_tracker.annas_handy': {state: 'not_home', last_updated: '2026-10-08T06:00:00Z', attributes: {}}}};
	await card._load();
	const btn = () => $('.trip-head .locate');
	assert.ok(btn(), 'Knopf da');
	btn().click();
	await tick(50);
	assert.ok(api.calls.includes('POST api/trips/anna1/locate'));
	assert.ok(btn().classList.contains('busy'), 'Symbol dreht');
	assert.equal(btn().disabled, true);
	await card._load(); // Aktualisierung zeichnet neu – Zustand bleibt
	assert.ok(btn().classList.contains('busy'));
	// neuer Standort trifft ein
	card.hass = {...card._hass, states: {...card._hass.states, 'device_tracker.annas_handy': {state: 'not_home', last_updated: '2026-10-08T06:00:09Z', attributes: {}}}};
	assert.equal(btn().classList.contains('busy'), false, 'dreht nicht mehr');
	assert.equal(btn().disabled, true, 'aber noch gesperrt');
	const n = api.calls.filter(c => c === 'POST api/trips/anna1/locate').length;
	btn().click();
	await tick(20);
	assert.equal(api.calls.filter(c => c === 'POST api/trips/anna1/locate').length, n, 'kein zweiter Aufruf in der Sperrzeit');
	// Sperrzeit der App wird übernommen und angezeigt
	card._locating = null;
	api.locateReply = reply(429, {error: 'COOLDOWN', retryInSec: 12, message: 'Gerade erst angefordert – in 12 s wieder möglich'});
	card._showLocate();
	btn().click();
	await tick(50);
	assert.equal(btn().disabled, true);
	assert.match(btn().title, /in 12 s/);
	api.locateReply = null;
	anna.status = 'upcoming';
});

test('mode: board – nur Abfahrtstafel, eigener Bahnhof, keine Reisen-Anfragen', async () => {
	const before = api.calls.length;
	const board = document.createElement('reiseanzeige-card');
	board.setConfig({mode: 'board', station: 'Ulm Hbf'});
	document.body.append(board);
	board.hass = card._hass;
	await tick(100);
	const q = (s) => board.shadowRoot.querySelector(s);
	assert.ok(q('.board-title'));
	assert.ok(q('.toolbar').classList.contains('hidden'));
	assert.ok(q('.layout').classList.contains('hidden'));
	const newCalls = api.calls.slice(before);
	assert.ok(newCalls.includes('GET api/board?station=Ulm%20Hbf'));
	assert.ok(!newCalls.some(c => c === 'GET api/trips'), 'keine Reisen geladen');
	assert.deepEqual(board.getGridOptions(), {columns: 'full', rows: 'auto', min_columns: 3});
	assert.throws(() => board.setConfig({mode: 'quatsch'}));
	board.remove();
});

test('Laufende Reise, nur Bahnhöfe haben Koordinaten (Tram/U-Bahn nicht): Karte zeichnet ohne Fehler', async () => {
	const py = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixtures', 'moritzplatz-berlin.python.json'), 'utf8'));
	const realNow = Date.now;
	try {
		// Mit Koordinaten nur an Bahnhöfen und ganz ohne Koordinaten (kein StaDa-Abo)
		const bare = structuredClone(py.snapshot);
		for (const leg of bare.legs) for (const st of [leg.origin, leg.destination, ...leg.stopovers.map(x => x.station)]) { st.lat = null; st.lon = null; }
		for (const [snap, hhmm] of [['14:20', '15:55', '16:00', '17:30', '19:45', '20:05'].map(t => [py.snapshot, t]), ['14:20', '17:30', '20:05'].map(t => [bare, t])].flat()) {
			Date.now = () => Date.parse(`2026-11-01T${hhmm}:00+01:00`);
			api.trips = [{id: 'py', ownerId: 'max', ownerName: 'Max', status: 'running', departure: snap.plannedDeparture,
				arrival: snap.plannedArrival, snapshot: {...snap, createdAt: snap === py.snapshot ? 'a' : 'b'}, realtime: py.realtime, seats: {}, realtimeHealth: {failing: false}}];
			card._manualId = 'py';
			await card._load();
			await tick(60);
			assert.doesNotThrow(() => card._updatePosition(), `Zeitpunkt ${hhmm}`);
		}
	} finally {
		Date.now = realNow;
		api.trips = [];
	}
});
