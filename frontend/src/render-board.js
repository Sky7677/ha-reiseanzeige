// Ruheansicht: "Keine anstehenden Reisen" und darunter die Abfahrtstafel

import {clock, esc} from './format.js';

const HINTS = {
	'no-station': 'Kein Bahnhof für die Abfahrtstafel eingestellt (Optionen der Integration oder „station“ an der Karte).',
	'no-credentials': 'Die Abfahrtstafel braucht Zugangsdaten der Bahn für die Timetables-API. Eintragen: Einstellungen → Geräte & Dienste → Reiseanzeige → ⋮ → Neu konfigurieren.',
};

// Farbe der Prognose: pünktlich grün, bis einschließlich 3 Minuten gelb, darüber rot
export const delayClass = (min) => (min <= 0 ? 'ok' : min <= 3 ? 'minor' : 'late');

const row = (d) => {
	const late = d.delayMin !== null && d.prognosed && !d.cancelled;
	return `
	<tr class="${d.cancelled ? 'cancelled' : ''}">
		<td class="time">${esc(clock(d.planned))}${late ? `<span class="rt ${delayClass(d.delayMin)}">${esc(clock(d.prognosed))}</span>` : ''}</td>
		<td><span class="linebadge">${esc(d.line)}</span></td>
		<td class="dest">${esc(d.destination ?? '')}${d.cancelled ? ' <em>fällt aus</em>' : ''}${d.added ? ' <em class="add">Zusatzzug</em>' : ''}
			${d.via.length ? `<div class="via">über ${d.via.map(esc).join(', ')}</div>` : ''}
			${d.messages.length && !d.cancelled ? `<div class="msg">${esc(d.messages[0])}</div>` : ''}</td>
		<td class="pf${d.platformChanged ? ' changed' : ''}">${d.platform ? esc(d.platform) : ''}</td>
	</tr>`;
};

export const renderBoard = (board) => {
	if (!board) return '<div class="board-note">Abfahrtstafel wird geladen …</div>';
	if (board.status !== 'ok') return `<div class="board-note">${esc(HINTS[board.status] ?? board.message ?? 'Abfahrtstafel nicht verfügbar')}</div>`;
	return `
	<section class="board">
		<div class="board-title">Abfahrten ${esc(board.station.name)}</div>
		${board.departures.length ? `
		<div class="board-wrap"><table>
			<thead><tr><th>Zeit</th><th>Zug</th><th>Ziel</th><th>Gl.</th></tr></thead>
			<tbody>${board.departures.map(row).join('')}</tbody>
		</table></div>` : '<div class="board-note">In den nächsten 90 Minuten keine Abfahrten.</div>'}
		<div class="board-foot">Stand ${esc(clock(board.fetchedAt))} · Daten: Deutsche Bahn, CC BY 4.0</div>
	</section>`;
};
