// ---------- Verbindung zur Integration (WebSocket-Befehle von Home Assistant) ----------
// Gleiche Schnittstelle wie früher der Ingress-Client der App (request(path, {method, body})),
// damit der übrige Kartencode unverändert bleibt. Die Pfade werden auf die Befehle
// "reiseanzeige/…" abgebildet; angemeldet ist automatisch der Benutzer des Frontends.

export class CardError extends Error {
	constructor (code, message) { super(message); this.code = code; }
}

const ROUTES = [
	// [Methode, Muster, Befehl, Daten aus Treffer/Body/Query]
	['GET', /^api\/me$/, 'me', () => ({})],
	['GET', /^api\/trips$/, 'trips', () => ({})],
	['POST', /^api\/trips$/, 'add', (m, body) => ({text: String(body?.text ?? '')})],
	['GET', /^api\/trips\/([^/]+)$/, 'trip', (m) => ({trip_id: decodeURIComponent(m[1])})],
	['DELETE', /^api\/trips\/([^/]+)$/, 'delete', (m) => ({trip_id: decodeURIComponent(m[1])})],
	['PUT', /^api\/trips\/([^/]+)\/seats\/(\d+)$/, 'seat', (m, body) => ({trip_id: decodeURIComponent(m[1]), leg: Number(m[2]), coach: String(body?.coach ?? ''), seat: String(body?.seat ?? '')})],
	['POST', /^api\/trips\/([^/]+)\/locate$/, 'locate', (m) => ({trip_id: decodeURIComponent(m[1])})],
	['GET', /^api\/board$/, 'board', (m, body, q) => (q.get('station') ? {station: q.get('station')} : {})],
];

export const createHaClient = ({hass}) => {
	let base = null;

	const call = async (type, data = {}) => {
		try {
			return await hass().callWS({type: `reiseanzeige/${type}`, ...data});
		} catch (err) {
			if (err?.code === 'unknown_command') throw new CardError('not-installed', 'Die Integration „Reiseanzeige“ ist nicht installiert oder nicht geladen.');
			if (err?.code === 'not_configured') throw new CardError('not-configured', err.message);
			throw new CardError(String(err?.code ?? 'error').toUpperCase(), err?.message ?? String(err));
		}
	};

	// Basisadresse für Kartenkacheln (enthält einen Zufallsschlüssel der Integration)
	const connect = async () => {
		const r = await call('map_base');
		base = r.base;
	};

	const request = async (path, {method = 'GET', body} = {}) => {
		if (!base) await connect();
		const [p, query = ''] = path.split('?');
		const q = new URLSearchParams(query);
		for (const [m, re, type, data] of ROUTES) {
			const hit = m === method ? re.exec(p) : null;
			if (!hit) continue;
			const res = await call(type, data(hit, body, q));
			// Sperrzeit beim Standort-Knopf: wie früher als Fehler mit retryInSec
			if (type === 'locate' && res?.status === 'cooldown') {
				throw Object.assign(new CardError('COOLDOWN', `Gerade erst angefordert – in ${res.retryInSec} s wieder möglich`), {data: {retryInSec: res.retryInSec}});
			}
			return res;
		}
		throw new CardError('not-found', `Unbekannte Anfrage ${method} ${p}`);
	};

	return {connect, keepAlive: async () => {}, request, get connected () { return Boolean(base); }, get base () { return base; }};
};
