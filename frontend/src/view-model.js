// Bereitet eine Reise für die Reiseinformationen auf. Reine Funktion: bekommt
// die Reise aus der App und die aktuelle Zeit, liefert, was angezeigt wird.

import {legStopovers} from './legs.js';

const ms = (iso) => (iso ? Date.parse(iso) : NaN);
const MIN = 60 * 1000;

// Echtzeit eines Halts zum Fahrplan-Halt finden (gleiche Station, in Reihenfolge).
// Neue Halte einer Umleitung haben keine eigene Echtzeit.
const alignRealtime = (stops, rtStops = []) => {
	let j = 0;
	return stops.map((s) => {
		if (s.added) return null;
		for (let k = j; k < rtStops.length; k++) {
			if (rtStops[k].stationId === s.station?.id) { j = k + 1; return rtStops[k]; }
		}
		return null;
	});
};

const timeInfo = (planned, rtPart, cancelled) => {
	if (!planned) return null;
	const delay = rtPart?.delay ?? null;
	return {
		planned,
		prognosed: cancelled ? null : rtPart?.prognosed ?? null,
		delayMin: delay === null || cancelled ? null : Math.round(delay / 60),
	};
};

const effective = (t) => (t ? ms(t.prognosed ?? t.planned) : NaN);

export const buildTripView = (trip, now = Date.now()) => {
	const failing = Boolean(trip.realtimeHealth?.failing);
	const rides = trip.snapshot.legs.filter(l => l.kind === 'ride');

	const legs = rides.map((leg, i) => {
		const rt = failing ? null : trip.realtime?.legs?.[leg.index] ?? null;
		const stopovers = legStopovers(leg);
		const aligned = alignRealtime(stopovers, rt?.stopovers);
		const last = stopovers.length - 1;
		const stops = stopovers.map((s, k) => {
			const r = aligned[k];
			const cancelled = Boolean(r?.cancelled || rt?.cancelled || s.cancelled);
			const arr = k === 0 ? null : timeInfo(s.plannedArrival, r?.arrival, cancelled);
			const dep = k === last ? null : timeInfo(s.plannedDeparture, r?.departure, cancelled);
			const plannedPlatform = r?.plannedPlatform ?? s.plannedPlatform ?? null;
			return {
				name: s.station?.name ?? '?',
				lat: s.station?.lat, lon: s.station?.lon,
				arr, dep,
				// Angezeigte Zeit: Abfahrt, am Ziel die Ankunft
				shown: dep ?? arr,
				platform: r?.platform ?? plannedPlatform,
				platformChanged: Boolean(r?.platformChanged),
				cancelled,
				added: Boolean(s.added),
				first: k === 0, last: k === last,
			};
		});
		const seat = trip.seats?.[leg.index] ?? null;
		return {
			index: leg.index,
			name: leg.line.name,
			product: leg.line.product ?? null,
			// Bahn-Abschnitt noch nicht im Fahrplan gefunden (ältere Reisen ohne Angabe: gefunden)
			pending: leg.matched === false && leg.source !== 'gtfs' && ['longdistance', 'regional', 'suburban'].includes(leg.line.product),
			// Zwischenhalte laut Fahrplan (GTFS), ohne Echtzeit
			planned: leg.source === 'gtfs',
			rail: ['longdistance', 'regional', 'suburban'].includes(leg.line.product),
			direction: leg.direction,
			replacement: Boolean(leg.replacementService),
			// Geänderter Laufweg laut Bahn: was entfällt, was dazukommt
			diversion: leg.diversion ? {
				cancelled: stops.filter(s => s.cancelled).map(s => s.name),
				added: stops.filter(s => s.added).map(s => s.name),
				endCancelled: stops.at(-1).cancelled,
			} : null,
			cancelled: Boolean(rt?.cancelled),
			hasRealtime: Boolean(rt?.hasRealtime),
			remarks: (rt?.remarks ?? leg.remarks ?? []).filter(m => m.type === 'warning' || m.type === 'status').map(m => m.text),
			seat: seat ? [seat.coach && `Wagen ${seat.coach}`, seat.seat && `Platz ${seat.seat}`].filter(Boolean).join(', ') : null,
			seatRaw: {coach: seat?.coach ?? '', seat: seat?.seat ?? ''},
			stops,
			position: i,
		};
	});

	// Aktueller bzw. nächster Halt: nur während der Fahrt
	let next = null;
	if (trip.status === 'running') {
		outer: for (const leg of legs) {
			for (const s of leg.stops) {
				if (s.cancelled) continue;
				const t = s.last ? effective(s.arr) : effective(s.dep);
				if (t >= now) { next = {leg: leg.position, stop: leg.stops.indexOf(s)}; break outer; }
			}
		}
	}
	for (const leg of legs) {
		leg.stops.forEach((s, k) => {
			const isNext = next && next.leg === leg.position && next.stop === k;
			const passed = trip.status === 'completed' || (trip.status === 'running' && next
				? leg.position < next.leg || (leg.position === next.leg && k < next.stop)
				: trip.status === 'running');
			s.state = isNext ? 'next' : passed ? 'past' : 'future';
		});
	}

	// Umstiege zwischen aufeinanderfolgenden Zügen
	for (let i = 0; i < legs.length - 1; i++) {
		const a = legs[i].stops.at(-1);
		const b = legs[i + 1].stops[0];
		const minutes = Math.round((effective(b.dep) - effective(a.arr)) / MIN);
		legs[i].transfer = {
			station: a.name === b.name ? a.name : `${a.name} → ${b.name}`,
			minutes: Number.isFinite(minutes) ? minutes : null,
			fromPlatform: a.platform,
			toPlatform: b.platform,
			risky: Number.isFinite(minutes) && minutes < 3,
		};
	}

	const lastRealtime = Object.values(trip.realtime?.legs ?? {}).map(l => l.fetchedAt).filter(Boolean).sort().at(-1) ?? null;
	return {
		id: trip.id,
		owner: trip.ownerName ?? 'Unbekannt',
		// Link zum Öffnen in DB Navigator bzw. auf bahn.de – nur mit gültiger vbid
		bahnLink: /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(trip.snapshot.vbid ?? '')
			? `https://www.bahn.de/buchung/start?vbid=${trip.snapshot.vbid}`
			: null,
		ownerId: trip.ownerId,
		origin: trip.snapshot.origin?.name,
		destination: trip.snapshot.destination?.name,
		departure: trip.departure,
		arrival: trip.arrival,
		status: trip.status,
		failing,
		updatedAt: trip.realtimeHealth?.lastSuccessAt ?? lastRealtime,
		legs,
	};
};
