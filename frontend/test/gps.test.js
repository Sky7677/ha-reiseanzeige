// GPS-Korrektur auf der echten Strecke Augsburg → Nürnberg → Berlin (RE16 + ICE 1604)

import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {buildGeometry, computePosition, pointAtAlong} from '../src/position.js';
import {buildTripView} from '../src/view-model.js';
import {applyGps, applyAppPoint, evaluateFix, readFix, readAppResult, projectOnto, fixSample, speedFromFixes, speedLabel, GPS_RULES} from '../src/gps.js';

const snapshot = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixtures', 'augsburg-berlin.snapshot.json'), 'utf8'));
const trip = {id: 't', status: 'running', snapshot, realtime: null, realtimeHealth: {failing: false}};
const geometry = buildGeometry(trip);
const at = (hhmm) => Date.parse(`2026-10-08T${hhmm}:00+02:00`);
const MIN = 60000;
const view = buildTripView(trip, at('08:00'));
const computeAt = (t) => computePosition(buildTripView(trip, t), geometry, t);
const ICE = 1;

// Standort genau auf der Strecke bzw. seitlich daneben
const onRoute = (along, leg = ICE) => pointAtAlong(geometry[leg], along).point;
const besideRoute = (along, meters, leg = ICE) => {
	const g = geometry[leg];
	const a = pointAtAlong(g, along), b = pointAtAlong(g, along + 0.5);
	const k = Math.cos(a.point[0] * Math.PI / 180);
	const dy = b.point[0] - a.point[0], dx = (b.point[1] - a.point[1]) * k;
	const len = Math.hypot(dx, dy);
	const deg = meters / 111320;
	return [a.point[0] + (dx / len) * deg, a.point[1] - (dy / len) * deg / k];
};
const fix = (point, {ageMin = 0, accuracy = 11, speed = 70, now = at('08:00')} = {}) => ({lat: point[0], lon: point[1], accuracy, speed, at: now - ageMin * MIN});
const apply = (f, now = at('08:00')) => applyGps({pos: computeAt(now), fix: f, view, geometry, computeAt, now});

test('Standort lesen: Attribute der Companion-App, unbrauchbare Zustände', () => {
	const f = readFix({state: 'not_home', last_updated: '2026-10-08T06:00:00Z', attributes: {latitude: 50.1, longitude: 10.9, gps_accuracy: 11, speed: 0, source_type: 'gps'}});
	assert.deepEqual(f, {lat: 50.1, lon: 10.9, accuracy: 11, speed: 0, at: Date.parse('2026-10-08T06:00:00Z')});
	// Android-Reiseanzeige: Messzeit statt Eingangszeit (im Hintergrund gesammelt geschickt)
	assert.equal(readFix({state: 'not_home', last_updated: '2026-10-08T06:01:30Z', attributes: {latitude: 50.1, longitude: 10.9, gps_accuracy: 8, fix_time: '2026-10-08T08:00:31+02:00'}}).at, Date.parse('2026-10-08T06:00:31Z'));
	assert.equal(readFix({state: 'unavailable', attributes: {}}), null);
	assert.equal(readFix({state: 'home', attributes: {}}), null);
});

test('Projektion: Punkt auf der Linie hat Abstand ~0, seitlich versetzt den Versatz', () => {
	const p = projectOnto(geometry[ICE], onRoute(80));
	assert.ok(p.distM < 5);
	assert.ok(Math.abs(p.along - 80) < 0.05);
	const q = projectOnto(geometry[ICE], besideRoute(80, 500));
	assert.ok(q.distM > 400 && q.distM < 600, `seitlich ${Math.round(q.distM)} m`);
});

test('Frischer Standort 2 km vor der Berechnung: Punkt folgt dem GPS', () => {
	const pos = computeAt(at('08:00'));
	const r = apply(fix(onRoute(pos.along + 2)));
	assert.equal(r.source, 'gps');
	assert.ok(Math.abs(r.along - (pos.along + 2)) < 0.05);
});

test('Zug steht auf freier Strecke: Handy steht still, 3 km hinter der Berechnung – zählt', () => {
	const pos = computeAt(at('08:00'));
	const r = apply(fix(onRoute(pos.along - 3), {speed: 0}));
	assert.equal(r.source, 'gps');
	assert.ok(r.along < pos.along - 2.9);
});

test('Abklingen: 3 Minuten alter Standort wirkt nur noch zum Teil, ohne Sprung am Ende', () => {
	const now = at('08:00');
	const then = computeAt(now - 3 * MIN);
	const r = apply(fix(onRoute(then.along + 2), {ageMin: 3}));
	const pos = computeAt(now);
	const expected = 2 * Math.exp(-1) * (1 - 3 / 10);
	assert.equal(r.source, 'gps');
	assert.ok(Math.abs((r.along - pos.along) - expected) < 0.05);
	// kurz vor dem Ende kaum noch Wirkung, danach gar keine
	const late = computeAt(now - 9.9 * MIN);
	const r2 = apply(fix(onRoute(late.along + 2), {ageMin: 9.9}));
	assert.ok(Math.abs(r2.along - pos.along) < 0.01, 'kurz vor 10 min praktisch null');
	assert.equal(apply(fix(onRoute(pos.along + 2), {ageMin: 11})).source, 'computed');
});

test('Abgelehnt: zu ungenau, neben der Strecke, zeitlich unplausibel, veraltet', () => {
	const pos = computeAt(at('08:00'));
	const reason = (f) => evaluateFix({fix: f, view, geometry, computeAt, now: at('08:00')}).reason;
	assert.equal(reason(fix(onRoute(pos.along), {accuracy: 400})), 'inaccurate');
	assert.equal(reason(fix(besideRoute(pos.along, 1000))), 'off-route', 'parallele Autobahn 1 km daneben');
	assert.equal(reason(fix(besideRoute(pos.along, 150))), undefined, '150 m daneben ist noch auf der Strecke');
	assert.equal(reason(fix(besideRoute(pos.along, 300), {accuracy: 200})), 'off-route');
	assert.equal(reason(fix(besideRoute(pos.along, 300), {accuracy: 240})), 'off-route', 'Toleranz = eigene Ungenauigkeit');
	assert.equal(reason(fix(onRoute(Math.min(pos.along + 120, geometry[ICE].cum.at(-1) - 1)))), 'implausible-time', 'weit voraus');
	assert.equal(reason(fix(onRoute(pos.along), {ageMin: 12})), 'stale');
	assert.equal(apply(fix(besideRoute(pos.along, 1000))).source, 'computed');
});

test('Zug verpasst: Handy steht in Nürnberg still, Abfahrt 07:36 ist 9 Minuten her', () => {
	const now = at('07:45');
	const nbg = geometry[ICE].coords[0];
	const r = evaluateFix({fix: fix(nbg, {speed: 0, now}), view, geometry, computeAt, now});
	assert.equal(r.reason, 'left-behind');
	// Kurz nach der Abfahrt (1 min) noch nicht – Zug kann ja verspätet stehen
	const early = at('07:37');
	assert.equal(evaluateFix({fix: fix(nbg, {speed: 0, now: early}), view, geometry, computeAt, now: early}).ok, true);
	// Stillstand aus ungenauem Standort (100 m, Mobilfunk/WLAN) ist kein Beweis
	assert.equal(evaluateFix({fix: fix(nbg, {speed: 0, accuracy: 100, now}), view, geometry, computeAt, now}).ok, true);
});

test('Regeln sind an einer Stelle festgelegt', () => {
	assert.equal(GPS_RULES.MAX_ACCURACY_M, 250);
	assert.equal(GPS_RULES.MIN_TOLERANCE_M, 175);
});

const speedOf = (f, now, previous = null) => speedFromFixes({current: fixSample(apply(f, now)), previous});

test('Geschwindigkeit vom Handy: nur bei akzeptiertem, genauem Standort (≤ 75 m), km/h gerundet', () => {
	const now = at('08:00');
	const pos = computeAt(now);
	assert.deepEqual(speedOf(fix(onRoute(pos.along), {speed: 82.8}), now), {kmh: 298, at: now, approx: false});
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 41}), now).kmh, 148, '41 m/s = 148 km/h (Attribut in m/s)');
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 0.5, accuracy: 25}), now).kmh, 0, 'echter Stillstand bei 25 m');
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 30, accuracy: 75}), now).kmh, 108, '75 m zählt noch');
	assert.equal(speedOf(fix(besideRoute(pos.along, 1000), {speed: 33}), now), null, 'Auto daneben: nichts merken');
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: null}), now), null);
	const older = speedOf(fix(onRoute(computeAt(now - 4 * MIN).along), {speed: 50, ageMin: 4}), now);
	assert.equal(older.at, now - 4 * MIN, 'Zeitpunkt = Eingang des Standorts');
});

test('Ungenauer Standort (100 m) mit speed 0: keine falsche 0, sondern abgeleitet oder unbekannt', () => {
	const now = at('08:00');
	const pos = computeAt(now);
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 0, accuracy: 100}), now), null, 'ohne vorigen Standort: unbekannt');
	// voriger Standort 60 s früher, 2,5 km zurück → 150 km/h, als "≈" gekennzeichnet
	const previous = {leg: ICE, along: pos.along - 2.5, at: now - 60 * 1000, speed: 41, accuracy: 25};
	assert.deepEqual(speedOf(fix(onRoute(pos.along), {speed: 0, accuracy: 100}), now, previous), {kmh: 150, at: now, approx: true});
	// zu kurzer oder zu langer Abstand, anderer Abschnitt → unbekannt
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 0, accuracy: 100}), now, {...previous, at: now - 10 * 1000}), null);
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 0, accuracy: 100}), now, {...previous, at: now - 6 * MIN}), null);
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 0, accuracy: 100}), now, {...previous, leg: 0}), null);
	// genaues GPS hat Vorrang vor der Ableitung
	assert.equal(speedOf(fix(onRoute(pos.along), {speed: 40, accuracy: 25}), now, previous).approx, false);
});

test('Geschwindigkeitsanzeige mit Uhrzeit der Messung, nach 2 Minuten blasser', () => {
	const now = at('08:00');
	assert.deepEqual(speedLabel({kmh: 298, at: now - 90 * 1000}, now), {text: '298 km/h · 07:58', stale: false});
	assert.deepEqual(speedLabel({kmh: 298, at: now - 5 * MIN}, now), {text: '298 km/h · 07:55', stale: true});
	assert.deepEqual(speedLabel({kmh: 150, at: now - 30 * 1000, approx: true}, now), {text: '≈ 150 km/h · 07:59', stale: false});
	assert.deepEqual(speedLabel({kmh: 150, at: now - 3 * MIN, approx: true}, now), {text: '≈ 150 km/h · 07:57', stale: true});
	assert.equal(speedLabel(null, now), null);
});

// Tracker der Android-App mit ihrem Ergebnis (Attribute train_*)
const appState = (along, {tMin = 0, leg = ICE, speed = 39.72, approx = true, trip_id = 't', gps = true} = {}) => {
	const [lat, lon] = onRoute(along, leg);
	const t = new Date(at('08:00') + tMin * MIN).toISOString();
	return {state: 'not_home', attributes: {latitude: lat + 0.001, longitude: lon, gps_accuracy: 64, speed: null, fix_time: t, trip_id,
		train_leg: leg, train_latitude: lat, train_longitude: lon, train_time: t, train_gps: gps,
		train_speed: speed, train_speed_time: speed == null ? null : t, train_speed_approx: approx}};
};

test('Ergebnis der App lesen: Zugpunkt und Geschwindigkeit, nur für die eigene Reise', () => {
	const now = at('08:00');
	const r = readAppResult(appState(computeAt(now).along), 't');
	assert.equal(r.leg, ICE);
	assert.equal(r.gps, true);
	assert.deepEqual(r.speed, {kmh: 143, at: now, approx: true}, '39,72 m/s = 143 km/h, wie die App');
	assert.equal(readAppResult(appState(80), 'andere'), null, 'Messung einer anderen Reise');
	assert.equal(readAppResult(appState(80, {speed: null}), 't').speed, null, 'App kennt keine Geschwindigkeit');
	// Ältere App ohne Ergebnis: nur die Messung
	assert.equal(readAppResult({state: 'not_home', attributes: {latitude: 50, longitude: 10, trip_id: 't'}}, 't'), null);
	assert.equal(readAppResult(null, 't'), null);
});

test('Zugpunkt der App hat Vorrang: kein Prüfen wie beim GPS, Abklingen mit dem Alter', () => {
	const now = at('08:00');
	const pos = computeAt(now);
	// App sieht den Zug 3 km weiter (etwa ohne Echtzeit hier): Punkt folgt der App
	const ahead = readAppResult(appState(pos.along + 3), 't');
	const p = applyAppPoint({pos, point: ahead, geometry, computeAt, now});
	assert.equal(p.source, 'app');
	assert.ok(Math.abs(p.along - (pos.along + 3)) < 0.05, `along ${p.along}`);
	// mit dem Alter schwächer, nach 10 Minuten gar nicht mehr
	const old = readAppResult(appState(computeAt(now - 5 * MIN).along + 3, {tMin: -5}), 't');
	const q = applyAppPoint({pos, point: old, geometry, computeAt, now});
	assert.ok(q.gpsWeight < 0.5 && q.along > pos.along && q.along < pos.along + 3);
	assert.equal(applyAppPoint({pos, point: readAppResult(appState(80, {tMin: -11}), 't'), geometry, computeAt, now}), null);
	// anderer Abschnitt als die eigene Rechnung: nicht verwendet (dann eigene Auswertung)
	assert.equal(applyAppPoint({pos, point: {...ahead, leg: 0}, geometry, computeAt, now}), null);
	// Uhr des Handys etwas vor: zählt wie frisch
	const early = applyAppPoint({pos, point: {...ahead, at: now + 20 * 1000}, geometry, computeAt, now});
	assert.equal(early.gpsWeight, 1);
});

test('Doppelt befahrenes Stück (Umkehr): der GPS-Abgleich nimmt die Stelle, die zur Uhrzeit passt', async () => {
	const {projectOnto} = await import('../src/gps.js');
	const {dist} = await import('../src/position.js');
	// Hin nach Norden, Umkehr, zurück nach Süden auf denselben Gleisen
	const coords = [[48.70, 10.80], [48.80, 10.80], [48.90, 10.80], [48.80, 10.80], [48.70, 10.80]];
	const cum = [0];
	for (let i = 1; i < coords.length; i++) cum.push(cum[i - 1] + dist(coords[i - 1], coords[i]));
	const g = {coords, cum};
	const p = [48.75, 10.8002];
	assert.ok(projectOnto(g, p).along < 10, 'ohne Zeitbezug: der Hinweg');
	const back = projectOnto(g, p, {along: cum[3] + 4, tolM: 175});
	assert.ok(Math.abs(back.along - (cum[3] + dist(coords[3], [48.75, 10.80]))) < 0.5, 'mit Zeitbezug: die Rückfahrt');
});
