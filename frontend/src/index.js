// Reiseanzeige – Dashboard-Karte für Home Assistant
//
// Wird von der Integration Reiseanzeige ausgeliefert und automatisch geladen;
// im Dashboard nur noch die Karte "type: custom:reiseanzeige-card" einfügen.
//
// Optionen: height (Höhe im Breitformat:
// Zahl in px oder CSS-Wert, Standard fast bildschirmhoch), theme (muted | gray),
// raster (true = sparsame Rasterkarte erzwingen), follow (false = auch während
// der Fahrt die ganze Strecke statt dem Zug zu folgen), mode (board = nur die
// Abfahrtstafel), station (Bahnhof der Tafel statt dem der Person)

import {createHaClient} from './ha-client.js';
import {selectTrip, switcherGroups, switcherLabels} from './select.js';
import {buildTripView} from './view-model.js';
import {buildGeometry, computePosition, splitRoute} from './position.js';
import {readFix, applyGps, fixSample, speedFromFixes, speedLabel} from './gps.js';
import {renderInfo, renderIdle} from './render-info.js';
import {renderBoard} from './render-board.js';
import {createTripMap} from './map.js';
import {STYLE} from './styles.js';
import {esc} from './format.js';

export const CARD_VERSION = '0.11.0';
const POLL_MS = 60 * 1000;
const SESSION_CHECK_MS = 60 * 1000;
const field = (form, name) => form.elements.namedItem(name);
const WIDE_FROM = 700; // px Kartenbreite für das Breitformat (wie die Container-Abfrage in styles.js)
const POSITION_MS = 15 * 1000;
// Knopf "Standort anfordern": so lange dreht das Symbol höchstens (das Handy antwortet
// meist binnen Sekunden), so lange ist er danach gesperrt (wie in der App)
const LOCATE_WAIT_MS = 20 * 1000;
const LOCATE_COOLDOWN_MS = 30 * 1000;
const BOARD_MS = 3 * 60 * 1000;   // Abfahrtstafel (die App speichert ebenso lange zwischen)   // Zugpunkt neu berechnen (lokal, ohne Netz)
const USER_SCROLL_HOLD_MS = 60 * 1000;
const DEFAULT_HEIGHT = 'max(480px, calc(100vh - 150px))';

class ReiseanzeigeCard extends HTMLElement {
	setConfig (config) {
		this._config = {height: 'auto', theme: 'muted', raster: false, follow: true, mode: 'trips', station: null, ...config};
		if (!['trips', 'board'].includes(this._config.mode)) throw new Error('mode muss "trips" oder "board" sein');
		this._manualId = null;
	}

	set hass (hass) {
		const first = !this._hass;
		this._hass = hass;
		if (first) this._start();
		this._map?.setDark(Boolean(hass.themes?.darkMode));
		const sig = Object.keys(hass.states).filter(k => k.startsWith('sensor.reiseanzeige_')).map(k => hass.states[k].last_updated).join('|');
		if (!first && sig !== this._sig) this._load();
		this._sig = sig;
		// Neuer Standort der Reisenden → Zugpunkt sofort neu berechnen
		const loc = this._current?.ownerLocation;
		const locStamp = loc ? hass.states[loc]?.last_updated : null;
		if (locStamp && locStamp !== this._locStamp) {
			this._locStamp = locStamp;
			if (this._locating && !this._locating.done) { this._locating.done = true; this._showLocate(); }
			this._updatePosition();
		}
	}

	connectedCallback () {
		if (this._hass && !this._timers) this._start();
	}

	disconnectedCallback () {
		for (const t of this._timers ?? []) clearInterval(t);
		this._timers = null;
		this._resize?.disconnect();
		this._map?.destroy();
		this._map = null;
		this._built = false;
	}

	_start () {
		this._client ??= createHaClient({hass: () => this._hass});
		this._timers = [
			setInterval(() => this._load(), POLL_MS),
			setInterval(() => this._client.keepAlive().catch(() => {}), SESSION_CHECK_MS),
			setInterval(() => this._updatePosition(), POSITION_MS),
		];
		this._build();
		this._load();
	}

	// Gerüst einmal anlegen; danach werden nur Teile ausgetauscht
	_build () {
		if (this._built) return;
		if (!this.shadowRoot) this.attachShadow({mode: 'open'});
		this.shadowRoot.innerHTML = `<style>${STYLE}</style>
			<ha-card>
				<div class="toolbar"><div class="switcher"></div><button type="button" class="btn add-toggle hidden">+ Reise</button></div>
				<form class="add-form hidden">
					<input name="link" placeholder="Geteilten Text aus dem DB Navigator einfügen" autocomplete="off">
					<button type="submit" class="btn primary">Speichern</button>
					<button type="button" class="btn" data-action="add-cancel">Abbrechen</button>
					<div class="add-msg"></div>
				</form>
				<div class="layout hidden"><div class="map"></div><div class="info"></div></div>
				<div class="status"><div class="idle"><div class="idle-sub">Verbinde mit der Reiseanzeige …</div></div></div>
				<div class="foot">Karte ${CARD_VERSION}</div>
			</ha-card>`;
		const $ = (s) => this.shadowRoot.querySelector(s);
		this._el = {card: $('ha-card'), switcher: $('.switcher'), layout: $('.layout'), map: $('.map'), info: $('.info'), status: $('.status'),
			addToggle: $('.add-toggle'), addForm: $('.add-form'), addMsg: $('.add-msg')};
		this._ui = {editLeg: null, confirmDelete: false, message: null};
		this._wireActions();
		const h = this._config.height;
		this._el.layout.style.setProperty('--ra-height', h === 'auto' ? DEFAULT_HEIGHT : typeof h === 'number' ? `${h}px` : h);
		// Wer in den Reiseinformationen scrollt, behält seine Stelle eine Weile
		for (const ev of ['wheel', 'touchstart', 'pointerdown']) {
			this._el.info.addEventListener(ev, () => { this._userScrollAt = Date.now(); }, {passive: true});
		}
		this._el.switcher.addEventListener('click', (e) => {
			const id = e.target.closest('button')?.dataset.id;
			if (!id) return;
			this._manualId = id;
			this._render();
		});
		// Nur noch für Leaflet: Kartengröße hat sich geändert. Breit/schmal entscheidet CSS.
		this._resize = new ResizeObserver(() => this._map?.resize());
		this._resize.observe(this._el.card);
		this._resize.observe(this._el.map); // Wechsel breit/schmal ändert vor allem die Kartenfläche
		this._built = true;
	}

	// Verwaltung: Reise hinzufügen, Wagen/Platz, löschen. Rechte prüft die App;
	// die Karte zeigt die Knöpfe nur, wo sie Sinn ergeben.
	_wireActions () {
		const {addToggle, addForm, addMsg, info} = this._el;
		const showAdd = (open) => {
			addForm.classList.toggle('hidden', !open);
			addToggle.classList.toggle('hidden', open || !this._me?.canAdd);
			if (open) { addMsg.textContent = ''; field(addForm, 'link').value = ''; field(addForm, 'link').focus(); }
		};
		addToggle.addEventListener('click', () => showAdd(true));
		addForm.addEventListener('click', (e) => { if (e.target.dataset.action === 'add-cancel') showAdd(false); });
		addForm.addEventListener('submit', async (e) => {
			e.preventDefault();
			const text = field(addForm, 'link').value.trim();
			if (!text) return;
			addMsg.className = 'add-msg';
			addMsg.textContent = 'Wird geladen …';
			try {
				const res = await this._client.request('api/trips', {method: 'POST', body: {text}});
				addMsg.textContent = res.message;
				this._manualId = res.trip.id;
				await this._load();
				setTimeout(() => showAdd(false), 2500);
			} catch (err) {
				addMsg.className = 'add-msg error';
				addMsg.textContent = err.message;
			}
		});

		info.addEventListener('click', async (e) => {
			const action = e.target.closest('[data-action]')?.dataset.action;
			const trip = this._shownTrip;
			if (!action || !trip) return;
			if (action === 'locate') {
				await this._requestLocation(trip);
			} else if (action === 'seat-edit') {
				this._ui = {...this._ui, editLeg: Number(e.target.dataset.leg), message: null};
				this._renderInfo(true);
				info.querySelector('.seat-form input')?.focus();
			} else if (action === 'seat-cancel') {
				this._ui.editLeg = null;
				this._renderInfo(true);
			} else if (action === 'delete') {
				if (!this._ui.confirmDelete) {
					// Zweiter Klick innerhalb von 5 s löscht wirklich
					this._ui.confirmDelete = true;
					this._renderInfo(true);
					clearTimeout(this._confirmTimer);
					this._confirmTimer = setTimeout(() => { this._ui.confirmDelete = false; this._renderInfo(true); }, 5000);
					return;
				}
				clearTimeout(this._confirmTimer);
				try {
					await this._client.request(`api/trips/${encodeURIComponent(trip.id)}`, {method: 'DELETE'});
					this._ui = {editLeg: null, confirmDelete: false, message: null};
					this._manualId = null;
					await this._load();
				} catch (err) {
					this._ui = {...this._ui, confirmDelete: false, message: {text: err.message, error: true}};
					this._renderInfo(true);
				}
			}
		});
		info.addEventListener('submit', async (e) => {
			const form = e.target.closest('.seat-form');
			if (!form) return;
			e.preventDefault();
			const trip = this._shownTrip;
			try {
				await this._client.request(`api/trips/${encodeURIComponent(trip.id)}/seats/${form.dataset.leg}`, {method: 'PUT', body: {coach: field(form, 'coach').value, seat: field(form, 'seat').value}});
				this._ui = {...this._ui, editLeg: null, message: null};
				await this._load();
			} catch (err) {
				this._ui.message = {text: err.message, error: true};
				this._renderInfo(true);
			}
		});
	}

	// Reiseinformationen zeichnen – nicht, solange jemand ein Formular ausfüllt (außer force)
	_renderInfo (force = false) {
		const trip = this._shownTrip;
		if (!trip) return;
		if (!force && this._ui.editLeg !== null) return;
		const info = this._el.info;
		const previousScroll = info.scrollTop;
		const sameTrip = this._shownId === trip.id;
		this._shownId = trip.id;
		// Bearbeiten und Löschen darf jeder Benutzer (die App lässt es zu)
		info.innerHTML = renderInfo(buildTripView(trip), {...this._ui, owned: true, canLocate: Boolean(trip.ownerCanLocate && trip.status === 'running'),
			// ohne Zugangsdaten der Bahn keine Echtzeit (ältere Integrationen melden nichts: mit)
			realtime: this._me?.realtime !== false});
		this._keepScroll(sameTrip ? previousScroll : 0, trip);
		this._showSpeed();
		this._showLocate();
	}

	get _boardOnly () { return this._config.mode === 'board'; }

	async _load () {
		// Nur Abfahrtstafel: keine Reisen laden, Tafel nach eigenem Takt
		if (this._boardOnly) {
			if (!this._board || Date.now() - this._board.at > BOARD_MS) await this._loadBoard();
			else this._render();
			return;
		}
		if (this._loading) return;
		this._loading = true;
		try {
			const [me, list] = await Promise.all([this._client.request('api/me'), this._client.request('api/trips')]);
			this._me = me;
			this._trips = list.trips;
			this._error = null;
		} catch (err) {
			this._error = err.message ?? String(err);
		} finally {
			this._loading = false;
			this._render();
		}
	}

	// Abfahrtstafel nur laden, solange die Ruheansicht zu sehen ist
	async _loadBoard () {
		if (this._boardLoading) return;
		this._boardLoading = true;
		try {
			const station = this._config.station;
			const data = await this._client.request(`api/board${station ? `?station=${encodeURIComponent(station)}` : ''}`);
			this._board = {at: Date.now(), data};
		} catch (err) {
			this._board = {at: Date.now(), data: {status: 'error', message: err.message}};
		} finally {
			this._boardLoading = false;
			if (!this._shownTrip || this._boardOnly) this._render();
		}
	}

	// Vollständige Reise (mit Streckenverläufen) nur für die angezeigte Reise holen – erneut,
	// sobald die Integration einen Verlauf neu berechnet hat (Gleisnetz geladen, Umleitung)
	async _fullTrip (trip) {
		const routes = trip.snapshot.legs.map(l => `${l.geometry?.key ?? ''}/${l.diversion?.geometry?.key ?? ''}`).join(',');
		const key = `${trip.id}|${trip.snapshot.createdAt}|${routes}`;
		if (this._full?.key !== key) {
			this._full = {key, trip: await this._client.request(`api/trips/${encodeURIComponent(trip.id)}`)};
		}
		return this._full.trip;
	}

	_render () {
		if (!this._built) return;
		const {switcher, layout, info, status} = this._el;
		if (this._boardOnly) {
			this._el.card.querySelector('.toolbar').classList.add('hidden');
			layout.classList.add('hidden');
			status.innerHTML = renderBoard(this._board?.data);
			return;
		}
		if (this._error && !this._trips) {
			layout.classList.add('hidden');
			status.innerHTML = `<div class="error">${esc(this._error)}</div>`;
			return;
		}
		if (!this._trips) return;
		if (this._el.addForm.classList.contains('hidden')) this._el.addToggle.classList.toggle('hidden', !this._me?.canAdd);

		const trips = this._trips;
		if (this._manualId && !trips.some(t => t.id === this._manualId)) this._manualId = null; // gewählte Reise entfernt
		const trip = selectTrip(trips, {userId: this._me?.id, manualId: this._manualId});

		// Umschalter: nur wenn es mehr als eine Reise gibt
		const labels = switcherLabels(trips);
		switcher.innerHTML = trips.length > 1
			? switcherGroups(trips).flatMap(g => g.trips.map(t => {
				const l = labels.get(t.id);
				return `<button data-id="${esc(t.id)}" class="${t.id === trip?.id ? 'active' : ''}">${esc(l.name)}<small>${esc(l.destination)}${l.extra ? ` · ${esc(l.extra)}` : ''}</small></button>`;
			})).join('')
			: '';

		if (!trip) {
			layout.classList.add('hidden');
			this._shownTrip = null;
			status.innerHTML = renderIdle() + renderBoard(this._board?.data);
			if (!this._board || Date.now() - this._board.at > BOARD_MS) this._loadBoard();
			return;
		}
		status.innerHTML = this._error ? `<div class="error">Aktualisierung fehlgeschlagen: ${esc(this._error)}</div>` : '';
		layout.classList.remove('hidden');
		if (this._shownTrip?.id !== trip.id) this._ui = {editLeg: null, confirmDelete: false, message: null};
		this._shownTrip = trip;
		this._renderInfo();

		if (!this._map) {
			this._map = createTripMap(this._el.map, {base: this._client.base, dark: Boolean(this._hass.themes?.darkMode), theme: this._config.theme, forceRaster: this._config.raster, follow: this._config.follow});
		}
		this._current = trip;
		this._fullTrip(trip).then((full) => {
			if (this._current?.id !== full.id || !this._map) return;
			this._map.showTrip(full);
			this._geometry = {key: this._full.key, geometry: buildGeometry(full)};
			this._updatePosition();
		}).catch(() => {});
	}

	// Scrollstelle halten; während der Fahrt (Breitformat) zum nächsten Halt, solange niemand selbst scrollt
	_keepScroll (previous, trip) {
		const info = this._el.info;
		const userBusy = Date.now() - (this._userScrollAt ?? 0) < USER_SCROLL_HOLD_MS;
		const next = info.querySelector('.stop.next');
		// Breitformat jetzt messen statt gemerkten Zustand zu nutzen
		const wide = this._el.card.clientWidth >= WIDE_FROM;
		if (wide && trip.status === 'running' && next && !userBusy) {
			info.scrollTop = Math.max(0, next.offsetTop - info.clientHeight / 3);
		} else {
			info.scrollTop = previous;
		}
	}

	// Zugposition aus Fahrplan und Echtzeit (aktuelle Daten aus der Liste, Strecke aus der vollen Reise)
	_updatePosition () {
		const trip = this._current;
		const g = this._geometry;
		if (!trip || !g || !this._map || !g.key.startsWith(`${trip.id}|`)) return;
		const now = Date.now();
		const view = buildTripView(trip, now);
		let pos = computePosition(view, g.geometry, now);
		// GPS-Korrektur, wenn für die Eigentümerin ein Standort eingetragen ist (gps.js)
		const fix = trip.ownerLocation ? readFix(this._hass?.states?.[trip.ownerLocation]) : null;
		if (pos && fix) {
			const computeAt = (t) => computePosition(buildTripView(trip, t), g.geometry, t);
			pos = applyGps({pos, fix, view, geometry: g.geometry, computeAt, now});
		}
		this._map.setPosition(pos, pos ? splitRoute(g.geometry, pos) : null);
		// Geschwindigkeit: nur bei einem neuen akzeptierten Standort neu bestimmen; gilt nur
		// für diese Reise und solange sie läuft. Unbekannt → letzter guter Wert bleibt.
		if (this._lastSpeed?.tripId !== trip.id || trip.status !== 'running') { this._lastSpeed = null; this._prevFix = null; }
		const sample = fixSample(pos);
		if (sample && trip.status === 'running' && sample.at !== this._prevFix?.at) {
			const speed = speedFromFixes({current: sample, previous: this._prevFix});
			if (speed) this._lastSpeed = {...speed, tripId: trip.id};
			this._prevFix = sample;
		}
		this._showSpeed();
	}

	// Handy der Reisenden um eine sofortige Standortmeldung bitten (über die App, die den
	// unsichtbaren Befehl der Companion-App schickt). Das Symbol dreht, bis ein neuer
	// Standort eintrifft; danach ist der Knopf bis zum Ende der Sperrzeit aus.
	async _requestLocation (trip) {
		const now = Date.now();
		if (this._locating?.tripId === trip.id && now - this._locating.since < LOCATE_COOLDOWN_MS) return;
		this._locating = {tripId: trip.id, since: now, done: false, error: null};
		const l = this._locating;
		this._showLocate();
		clearTimeout(this._locateWaitTimer);
		clearTimeout(this._locateCoolTimer);
		this._locateWaitTimer = setTimeout(() => this._showLocate(), LOCATE_WAIT_MS + 50);
		this._locateCoolTimer = setTimeout(() => this._showLocate(), LOCATE_COOLDOWN_MS + 50);
		try {
			await this._client.request(`api/trips/${encodeURIComponent(trip.id)}/locate`, {method: 'POST'});
		} catch (err) {
			if (this._locating !== l) return;
			l.done = true;
			l.error = err.message;
			const retry = Number(err.data?.retryInSec);
			if (err.code === 'COOLDOWN' && retry > 0) {
				// Sperrzeit der App übernehmen (z. B. jemand anderes hat gerade gedrückt)
				l.since = Date.now() - (LOCATE_COOLDOWN_MS - retry * 1000);
				clearTimeout(this._locateCoolTimer);
				this._locateCoolTimer = setTimeout(() => this._showLocate(), retry * 1000 + 50);
			} else {
				l.since = 0; // anderer Fehler: gleich wieder drückbar
			}
		}
		this._showLocate();
	}

	_showLocate () {
		const btn = this._el?.info.querySelector('.trip-head .locate');
		if (!btn) return;
		const l = this._locating?.tripId === this._shownTrip?.id ? this._locating : null;
		const age = l ? Date.now() - l.since : Infinity;
		const busy = Boolean(l && !l.done && age < LOCATE_WAIT_MS);
		btn.classList.toggle('busy', busy);
		btn.classList.toggle('failed', Boolean(l?.error));
		btn.disabled = age < LOCATE_COOLDOWN_MS;
		btn.title = busy ? 'Standort angefordert – warte auf das Handy …'
			: l?.error ? l.error
			: btn.disabled ? 'Standort gerade angefordert – gleich wieder möglich'
			: 'Standort und Geschwindigkeit jetzt vom Handy anfordern';
	}

	// Geschwindigkeit im Kopf der Reiseinformationen (ohne alles neu zu zeichnen)
	_showSpeed () {
		const el = this._el?.info.querySelector('.trip-head .speed');
		if (!el) return;
		const label = this._lastSpeed?.tripId === this._shownTrip?.id ? speedLabel(this._lastSpeed, Date.now()) : null;
		el.classList.toggle('hidden', !label);
		el.classList.toggle('stale', Boolean(label?.stale));
		el.textContent = label?.text ?? '';
		el.title = (label?.stale ? 'Letzte Geschwindigkeit' : 'Geschwindigkeit') + (this._lastSpeed?.approx ? ' – abgeleitet aus zwei Standorten auf der Strecke, weil das Handy gerade kein genaues GPS meldet' : ' laut GPS des Handys');
	}

	getCardSize () { return 8; }

	// Abschnitts-Ansicht (Sections): volle Breite des Abschnitts, Höhe bestimmt die Karte selbst
	getGridOptions () {
		return this._config?.mode === 'board'
			? {columns: 'full', rows: 'auto', min_columns: 3}
			: {columns: 'full', rows: 'auto', min_columns: 6};
	}
	static getStubConfig () { return {}; }
}

if (!customElements.get('reiseanzeige-card')) {
	customElements.define('reiseanzeige-card', ReiseanzeigeCard);
	window.customCards = window.customCards || [];
	window.customCards.push({type: 'reiseanzeige-card', name: 'Reiseanzeige', description: 'Geteilte Bahnreisen mit Karte und Echtzeit'});
	console.info(`%c REISEANZEIGE-KARTE %c ${CARD_VERSION} `, 'background:#ec0016;color:#fff', 'background:#333;color:#fff');
}
