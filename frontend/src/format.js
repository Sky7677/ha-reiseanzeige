// Formatierung und kleine Helfer der Karte

export const TZ = 'Europe/Berlin';
const fmt = (opts) => new Intl.DateTimeFormat('de-DE', {timeZone: TZ, ...opts});

export const clock = (iso) => (iso ? fmt({hour: '2-digit', minute: '2-digit'}).format(new Date(iso)) : '–');
export const dayTime = (iso) => fmt({weekday: 'short', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}).format(new Date(iso));
export const day = (iso) => fmt({weekday: 'long', day: 'numeric', month: 'long'}).format(new Date(iso));
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

const STATUS = {upcoming: 'Anstehend', running: 'Unterwegs', completed: 'Abgeschlossen'};
export const statusText = (s) => STATUS[s] ?? s;
