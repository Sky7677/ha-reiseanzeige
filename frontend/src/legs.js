// Haltliste und Verlauf eines Fahrtabschnitts. Meldet die Bahn einen geänderten
// Laufweg (Umleitung, Zug endet früher), legt die Integration ihn in
// leg.diversion ab: geplante Halte (entfallene mit cancelled), neue Halte
// (added, ohne Zeiten) und den Verlauf über die angefahrenen Halte.

export const legStopovers = (leg) => (leg.diversion?.stopovers?.length >= 2 ? leg.diversion.stopovers : leg.stopovers);

const point = (s) => [s.station?.lat, s.station?.lon];

export const legCoords = (leg) => {
	const line = leg.diversion ? leg.diversion.polyline : leg.polyline;
	if (Array.isArray(line) && line.length >= 2) return line;
	// Rückfallebene ohne Streckenverlauf: gerade Linien zwischen den angefahrenen Halten
	return legStopovers(leg).filter(s => !s.cancelled).map(point).filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b));
};
