// Reiseinformationen als HTML, aus dem aufbereiteten Modell (view-model.js)

import {clock, day, esc, statusText} from './format.js';

const time = (t) => {
	if (!t) return '<span class="t"></span>';
	let late = '';
	if (t.delayMin !== null && t.prognosed) {
		late = `<span class="rt ${t.delayMin >= 1 ? 'late' : 'ok'}">${esc(clock(t.prognosed))}</span>`;
	}
	return `<span class="t">${esc(clock(t.planned))}${late}</span>`;
};

const stopRow = (s) => `
	<li class="stop ${s.state}${s.first || s.last ? ' end' : ''}${s.cancelled ? ' cancelled' : ''}">
		${time(s.shown)}
		<span class="dot"></span>
		<span class="name">${esc(s.name)}${s.cancelled ? ' <em>entfällt</em>' : ''}</span>
		<span class="pf${s.platformChanged ? ' changed' : ''}">${s.platform ? `Gl. ${esc(s.platform)}` : ''}</span>
	</li>`;

// Wagen/Platz: Anzeige, Knopf für die Eigentümerin, Formular beim Bearbeiten
const seatPart = (leg, ui) => {
	if (ui.owned && ui.editLeg === leg.index) {
		return `
		<form class="seat-form" data-leg="${leg.index}">
			<input name="coach" placeholder="Wagen" maxlength="12" value="${esc(leg.seatRaw.coach)}" autocomplete="off">
			<input name="seat" placeholder="Platz" maxlength="12" value="${esc(leg.seatRaw.seat)}" autocomplete="off">
			<button type="submit" class="btn primary">Speichern</button>
			<button type="button" class="btn" data-action="seat-cancel">Abbrechen</button>
		</form>`;
	}
	// Wagen/Platz nur bei Zügen (nicht bei Tram, U-Bahn, Bus, Schiff)
	const edit = ui.owned && !['tram', 'subway', 'bus', 'ship'].includes(leg.product) ? `<button type="button" class="link" data-action="seat-edit" data-leg="${leg.index}">${leg.seat ? 'ändern' : 'Wagen/Platz eintragen'}</button>` : '';
	if (!leg.seat && !edit) return '';
	return `<div class="seat">${leg.seat ? esc(leg.seat) : ''}${leg.seat && edit ? ' · ' : ''}${edit}</div>`;
};

const legBlock = (leg, ui) => `
	<section class="leg">
		<div class="leg-head">
			<span class="line">${esc(leg.name)}</span>
			${leg.direction ? `<span class="dir">Richtung ${esc(leg.direction)}</span>` : ''}
			${leg.replacement ? '<span class="tag sev">Ersatzverkehr</span>' : ''}
			${leg.cancelled ? '<span class="tag bad">fällt aus</span>' : ''}
		</div>
		${seatPart(leg, ui)}
		${leg.pending ? '<div class="remark pending">Zwischenhalte und Echtzeit folgen, sobald der Fahrplan dafür bereitsteht (etwa einen Tag vorher).</div>' : ''}
		${leg.remarks.map(r => `<div class="remark">${esc(r)}</div>`).join('')}
		<ol class="stops">${leg.stops.map(stopRow).join('')}</ol>
		${leg.transfer ? `
		<div class="transfer${leg.transfer.risky ? ' risky' : ''}">
			Umstieg ${esc(leg.transfer.station)}${leg.transfer.minutes !== null ? ` · ${leg.transfer.minutes} min` : ''}${leg.transfer.fromPlatform || leg.transfer.toPlatform ? ` · Gl. ${esc(leg.transfer.fromPlatform ?? '?')} → ${esc(leg.transfer.toPlatform ?? '?')}` : ''}${leg.transfer.risky ? ' · Anschluss gefährdet' : ''}
		</div>` : ''}
	</section>`;

// ui: {owned, editLeg, confirmDelete, message, canLocate}
export const renderInfo = (v, ui = {}) => `
	<header class="trip-head">
		<div class="who">${esc(v.owner)} · ${esc(day(v.departure))}</div>
		<div class="route">${esc(v.origin)} → ${esc(v.destination)}</div>
		<div class="meta"><span class="badge ${v.status}">${esc(statusText(v.status))}</span> ${esc(clock(v.departure))} – ${esc(clock(v.arrival))}<span class="speed hidden" title="Geschwindigkeit laut GPS des Handys"></span>${ui.canLocate ? `<button type="button" class="locate" data-action="locate" title="Standort und Geschwindigkeit jetzt vom Handy anfordern" aria-label="Standort jetzt vom Handy anfordern"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg></button>` : ''}</div>
		${v.bahnLink ? `<a class="bahn-link" href="${esc(v.bahnLink)}" target="_blank" rel="noopener noreferrer">Im DB Navigator öffnen ↗</a>` : ''}
		${v.status === 'completed' ? `<div class="banner">Reise abgeschlossen · Ankunft ${esc(clock(v.arrival))}</div>` : ''}
		${v.failing ? '<div class="banner warn">Echtzeitdaten nicht verfügbar – Zeiten laut Fahrplan</div>' : ''}
	</header>
	${ui.message ? `<div class="banner ${ui.message.error ? 'warn' : ''}">${esc(ui.message.text)}</div>` : ''}
	${v.legs.map(leg => legBlock(leg, ui)).join('')}
	<footer class="updated">
		<span>${v.updatedAt ? `Echtzeit aktualisiert ${esc(clock(v.updatedAt))}` : 'Noch keine Echtzeitdaten'} · <a class="source" href="https://creativecommons.org/licenses/by/4.0/deed.de" target="_blank" rel="noopener noreferrer">Daten: Deutsche Bahn AG, CC BY 4.0</a></span>
		${ui.owned ? `<button type="button" class="link danger" data-action="delete">${ui.confirmDelete ? 'Wirklich löschen?' : 'Reise löschen'}</button>` : ''}
	</footer>`;

export const renderIdle = () => `
	<div class="idle">
		<div class="idle-title">Keine anstehenden Reisen</div>
	</div>`;
