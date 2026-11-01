// Landkarte der Reise: Leaflet als Gerüst, Vektorkarte über MapLibre (wie
// Home Assistant selbst), ohne WebGL 2 Rasterkacheln. Alle Kartendaten kommen
// über den Kartenproxy der App (Ingress), nie direkt aus dem Browser.

import L from 'leaflet';
import * as maplibregl from 'maplibre-gl';
import '@maplibre/maplibre-gl-leaflet';
// MapLibres Hintergrundprozess, beim Bauen zu einem Stück gebündelt (scripts/build-card.mjs),
// damit die Karte eine einzige Datei bleibt
import workerCode from 'virtual:maplibre-worker';

let workerReady = false;
const ensureWorker = () => {
	if (workerReady) return;
	maplibregl.setWorkerUrl(URL.createObjectURL(new Blob([workerCode], {type: 'text/javascript'})));
	workerReady = true;
};

const ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap-Mitwirkende</a>';

export const hasWebGL2 = () => {
	try {
		return Boolean(document.createElement('canvas').getContext('webgl2'));
	} catch {
		return false;
	}
};

// Strecke dezent (weiß bzw. dunkelgrau), Zug in Rot – Rot nur für das, was zählt
const COLORS = {
	light: {route: '#3d4148', stopFill: '#ffffff', train: '#ec0016', trainRing: '#ffffff'},
	dark: {route: '#f1f1f1', stopFill: '#1c1c1c', train: '#ff2d3f', trainRing: '#ffffff'},
};
const DRIVEN_OPACITY = 0.3;
const IDLE_RESET_MS = 90 * 1000;   // nach Berührung: so lange bis zur automatischen Ansicht
const FOLLOW_MAX_ZOOM = 11;

const coordsOf = (leg) => {
	if (Array.isArray(leg.polyline) && leg.polyline.length >= 2) return leg.polyline;
	// Rückfallebene ohne Streckenverlauf: gerade Linien zwischen den Halten
	return leg.stopovers.map(s => [s.station?.lat, s.station?.lon]).filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b));
};

export const createTripMap = (container, {base, dark = false, theme = 'muted', forceRaster = false, follow = true}) => {
	const abs = new URL(base, location.origin).href;
	const map = L.map(container, {zoomControl: false, attributionControl: true, zoomSnap: 0.25});
	map.attributionControl.setPrefix(false);
	map.setView([51, 10], 5);

	let baseLayer = null;
	let vector = !forceRaster && hasWebGL2();

	const rasterLayer = (isDark) => L.tileLayer(`${abs}map/raster/{z}/{x}/{y}.png`, {maxZoom: 19, attribution: ATTRIBUTION, className: isDark ? 'ra-dark-tiles' : ''});

	const setBase = async (isDark) => {
		if (baseLayer) map.removeLayer(baseLayer);
		baseLayer = null;
		if (vector) {
			try {
				ensureWorker();
				const res = await fetch(`${abs}map/style/${theme}-${isDark ? 'dark' : 'light'}.json`, {credentials: 'same-origin'});
				if (!res.ok) throw new Error(`Kartenstil: HTTP ${res.status}`);
				const style = JSON.parse((await res.text()).replaceAll('{BASE}', abs));
				baseLayer = L.maplibreGL({style, attribution: ATTRIBUTION, interactive: false});
				baseLayer.addTo(map);
			} catch (err) {
				// Vektorkarte klappt hier nicht → dauerhaft die sparsame Rasterkarte
				console.warn('Reiseanzeige: Vektorkarte nicht möglich, nutze Rasterkarte', err);
				if (baseLayer) map.removeLayer(baseLayer);
				vector = false;
				baseLayer = rasterLayer(isDark).addTo(map);
			}
		} else {
			baseLayer = rasterLayer(isDark).addTo(map);
		}
		baseLayer.bringToBack?.();
	};

	let currentDark = dark;
	const ready = setBase(dark);
	const routeLayer = L.layerGroup().addTo(map);
	const trainLayer = L.layerGroup().addTo(map);
	let current = null;      // {key, trip, lines:[{driven, ahead}], bounds}
	let train = null;
	let pos = null;
	let userMovedAt = 0;
	let resetTimer = null;

	// Knopf "zur Reise": erscheint, sobald jemand selbst zoomt oder verschiebt
	const ResetControl = L.Control.extend({
		options: {position: 'topright'},
		onAdd () {
			const b = L.DomUtil.create('button', 'ra-reset');
			b.type = 'button';
			b.title = 'Ansicht zurücksetzen';
			b.innerHTML = '<span aria-hidden="true">⤢</span> Zur Reise';
			L.DomEvent.disableClickPropagation(b);
			L.DomEvent.on(b, 'click', () => resetView());
			return b;
		},
	});
	const resetControl = new ResetControl();

	// Hinweis, wenn der Zugpunkt gerade auf GPS beruht statt auf der Berechnung
	const GpsBadge = L.Control.extend({
		options: {position: 'bottomleft'},
		onAdd () {
			const d = L.DomUtil.create('div', 'ra-gps');
			d.textContent = 'Position per GPS';
			d.title = 'Zugposition anhand des Handy-Standorts korrigiert';
			return d;
		},
	});
	const gpsBadge = new GpsBadge();

	const userInteracted = () => {
		userMovedAt = Date.now();
		if (!resetControl._map) resetControl.addTo(map);
		clearTimeout(resetTimer);
		resetTimer = setTimeout(resetView, IDLE_RESET_MS);
	};
	for (const ev of ['pointerdown', 'wheel', 'touchstart', 'dblclick']) {
		container.addEventListener(ev, (e) => { if (!e.target.closest?.('.ra-reset')) userInteracted(); }, {passive: true});
	}

	// Automatische Ansicht: während der Fahrt um den Zug, sonst die ganze Strecke
	const applyView = (animate) => {
		if (!current || userMovedAt) return;
		const c = current;
		if (follow && pos) {
			const pts = [pos.point, pos.prev, pos.next].filter(p => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]));
			if (pts.length) map.fitBounds(L.latLngBounds(pts), {padding: [48, 48], maxZoom: FOLLOW_MAX_ZOOM, animate});
			else if (c.bounds.isValid()) map.fitBounds(c.bounds, {padding: [28, 28], animate});
		} else if (c.bounds.isValid()) {
			map.fitBounds(c.bounds, {padding: [28, 28], animate});
		}
	};

	const resetView = () => {
		clearTimeout(resetTimer);
		userMovedAt = 0;
		resetControl.remove();
		applyView(true);
	};

	const draw = () => {
		routeLayer.clearLayers();
		trainLayer.clearLayers();
		train = null;
		if (!current) return;
		const c = COLORS[currentDark ? 'dark' : 'light'];
		const rides = current.trip.snapshot.legs.filter(l => l.kind === 'ride');
		current.lines = rides.map((leg) => {
			const dash = leg.replacementService ? '8 6' : null;
			return {
				coords: coordsOf(leg),
				driven: L.polyline([], {color: c.route, weight: 4, opacity: DRIVEN_OPACITY, dashArray: dash, interactive: false}).addTo(routeLayer),
				ahead: L.polyline(coordsOf(leg), {color: c.route, weight: 4, opacity: 0.95, dashArray: dash, interactive: false}).addTo(routeLayer),
			};
		});
		for (const leg of rides) {
			for (const s of leg.stopovers.slice(1, -1)) {
				if (!Number.isFinite(s.station?.lat)) continue;
				L.circleMarker([s.station.lat, s.station.lon], {radius: 3, color: c.route, weight: 2, fillColor: c.stopFill, fillOpacity: 1, interactive: false}).addTo(routeLayer);
			}
		}
		// Start, Umstiege, Ziel: größer und beschriftet
		const points = [];
		rides.forEach((leg, i) => {
			if (i === 0 && leg.stopovers[0]?.station) points.push(leg.stopovers[0].station);
			if (leg.stopovers.at(-1)?.station) points.push(leg.stopovers.at(-1).station);
		});
		const seen = new Set();
		for (const p of points) {
			if (!Number.isFinite(p.lat) || seen.has(p.name)) continue;
			seen.add(p.name);
			L.circleMarker([p.lat, p.lon], {radius: 6, color: c.route, weight: 3, fillColor: c.stopFill, fillOpacity: 1, interactive: false})
				.bindTooltip(p.name, {permanent: true, direction: 'right', offset: [8, 0], className: 'ra-label'})
				.addTo(routeLayer);
		}
		updateTrain();
	};

	const valid = (p) => Array.isArray(p) ? Number.isFinite(p[0]) && Number.isFinite(p[1]) : Boolean(p && Number.isFinite(p.lat));
	const clean = (list) => (list ?? []).filter(valid);

	const updateTrain = () => {
		if (!current?.lines) return;
		const c = COLORS[currentDark ? 'dark' : 'light'];
		// Ohne Position (vor/nach der Fahrt): ganze Strecke ungeteilt
		const parts = current.split ?? current.lines.map(l => ({driven: [], ahead: l.coords}));
		current.lines.forEach((l, i) => {
			if (!parts[i]) return;
			l.driven.setLatLngs(clean(parts[i].driven));
			l.ahead.setLatLngs(clean(parts[i].ahead));
		});
		if (pos && valid(pos.point)) {
			if (!train) train = L.circleMarker(pos.point, {radius: 8, color: c.trainRing, weight: 3, fillColor: c.train, fillOpacity: 1, interactive: false}).addTo(trainLayer);
			else train.setLatLng(pos.point);
			train.bringToFront();
		} else if (train) {
			trainLayer.clearLayers();
			train = null;
		}
	};

	return {
		ready,
		get vector () { return vector; },

		setDark (isDark) {
			if (isDark === currentDark) return;
			currentDark = isDark;
			setBase(isDark);
			draw();
		},

		// Neue bzw. andere Reise: alles neu zeichnen, automatische Ansicht
		showTrip (trip) {
			const key = `${trip.id}|${trip.snapshot.createdAt}`;
			if (current?.key === key) return;
			const bounds = L.latLngBounds([]);
			for (const leg of trip.snapshot.legs) if (leg.kind === 'ride') coordsOf(leg).forEach(p => bounds.extend(p));
			const isNewTrip = current?.trip.id !== trip.id;
			current = {key, trip, bounds, split: current?.split};
			if (isNewTrip) { pos = null; current.split = null; resetView(); }
			draw();
			applyView(false);
		},

		// Zugposition und Teilung gefahren/kommend (aus position.js)
		setPosition (newPos, split) {
			pos = newPos;
			if (pos?.source === 'gps' && pos.gpsWeight > 0.2) { if (!gpsBadge._map) gpsBadge.addTo(map); } else gpsBadge.remove();
			if (current) current.split = split;
			updateTrain();
			applyView(true);
		},

		resize () {
			map.invalidateSize();
			applyView(false);
		},

		destroy () {
			clearTimeout(resetTimer);
			map.remove();
		},
	};
};
