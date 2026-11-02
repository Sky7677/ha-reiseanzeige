// Aussehen der Karte. Farben aus dem HA-Theme, Akzent DB-Rot.

import leafletCss from 'leaflet/dist/leaflet.css';
import maplibreCss from 'maplibre-gl/dist/maplibre-gl.css';

const CARD_CSS = `
:host { --ra-accent: #ec0016; --ra-line: var(--primary-text-color); --ra-ok: #2e9e48; --ra-late: #e3001b; }
/* Layout nach Breite per Container-Abfrage: entscheidet der Browser bei jedem Zeichnen
   neu – auch nach Dashboard-Wechseln, ohne einen in JavaScript gemerkten Zustand */
ha-card { overflow: hidden; container-type: inline-size; container-name: ra; }
.toolbar { display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px 0; }
.toolbar:has(.switcher:empty):has(.add-toggle.hidden) { display: none; }
.switcher { display: flex; flex-wrap: wrap; gap: 6px; flex: 1; }
.add-toggle { margin-left: auto; white-space: nowrap; }
.hidden { display: none !important; }
.btn {
	font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 16px; cursor: pointer;
	border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
}
.btn.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
.remark.pending { color: var(--secondary-text-color); background: none; padding-left: 0; font-style: italic; }
.updated .source { color: inherit; text-decoration: none; opacity: 0.8; }
.link { font: inherit; font-size: 12.5px; background: none; border: 0; padding: 0; cursor: pointer; color: var(--primary-color); }
.link.danger { color: var(--ra-late); }
input {
	font: inherit; font-size: 14px; padding: 6px 10px; border-radius: 8px; min-width: 0;
	border: 1px solid var(--divider-color); background: var(--card-background-color, transparent); color: var(--primary-text-color);
}
.add-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 12px 0; }
.add-form input { flex: 1 1 260px; }
.add-msg { flex-basis: 100%; font-size: 13px; color: var(--secondary-text-color); }
.add-msg:empty { display: none; }
.add-msg.error { color: var(--ra-late); }
.seat-form { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.seat-form input { width: 76px; }
.switcher button {
	font: inherit; font-size: 13px; padding: 5px 12px; border-radius: 16px; cursor: pointer;
	border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
}
.switcher button.active { background: var(--ra-accent); border-color: var(--ra-accent); color: #fff; }
.switcher button small { opacity: .75; margin-left: 4px; }

.layout { display: grid; grid-template-columns: 1fr; }
.map { height: 220px; position: relative; background: var(--secondary-background-color); }
.info { padding: 12px 16px 14px; overflow-y: auto; position: relative; }
.layout.hidden { display: none; }
@container ra (min-width: 700px) {
	.layout { grid-template-columns: 2fr 1fr; height: var(--ra-height, 480px); }
	.layout .map { height: 100%; }
	.layout .info { height: 100%; box-sizing: border-box; border-left: 1px solid var(--divider-color); }
}

.trip-head .who { color: var(--secondary-text-color); font-size: 13px; }
.trip-head .route { font-size: 19px; font-weight: 600; margin: 2px 0 4px; line-height: 1.25; }
.trip-head .meta { font-size: 14px; color: var(--secondary-text-color); }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: var(--secondary-background-color); color: var(--primary-text-color); margin-right: 4px; }
.badge.running { background: var(--ra-accent); color: #fff; }
.bahn-link { display: inline-block; margin-top: 6px; font-size: 13px; color: var(--primary-color); text-decoration: none; }
.bahn-link:hover { text-decoration: underline; }
.trip-head .speed { margin-left: 10px; font-weight: 600; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
.trip-head .speed.stale { font-weight: 400; color: var(--secondary-text-color); }
.trip-head .locate { display: inline-flex; align-items: center; justify-content: center; vertical-align: middle; width: 28px; height: 28px; margin: -6px 0 -6px 4px; padding: 0; border: 0; border-radius: 50%; background: none; color: var(--secondary-text-color); cursor: pointer; }
.trip-head .locate svg { width: 18px; height: 18px; fill: currentColor; }
.trip-head .locate:hover:not(:disabled) { color: var(--primary-color); background: rgba(127, 127, 127, 0.12); }
.trip-head .locate:disabled { cursor: default; opacity: 0.45; }
.trip-head .locate.busy { opacity: 1; color: var(--primary-color); }
.trip-head .locate.busy svg { animation: ra-spin 1s linear infinite; }
.trip-head .locate.failed { color: var(--ra-late); }
@keyframes ra-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .trip-head .locate.busy svg { animation: none; } }
.banner { margin-top: 8px; padding: 6px 10px; border-radius: 6px; font-size: 13px; background: var(--secondary-background-color); }
.banner.warn { background: rgba(227, 0, 27, .12); color: var(--ra-late); }

.leg { margin-top: 14px; }
.leg-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 8px; }
.line { font-weight: 600; font-size: 15px; }
.dir { color: var(--secondary-text-color); font-size: 13px; }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 9px; }
.tag.sev { background: rgba(255, 152, 0, .18); color: #e68a00; }
.tag.bad { background: rgba(227, 0, 27, .14); color: var(--ra-late); }
.seat, .remark { font-size: 12.5px; margin-top: 3px; color: var(--secondary-text-color); }
.remark { color: var(--ra-late); }

.stops { list-style: none; margin: 8px 0 0; padding: 0; }
.stop { display: grid; grid-template-columns: 74px 14px 1fr auto; align-items: center; column-gap: 8px; font-size: 13.5px; min-height: 24px; position: relative; }
.stop .t { font-variant-numeric: tabular-nums; white-space: nowrap; }
.stop .rt { margin-left: 5px; font-size: 12.5px; }
.stop .rt.ok { color: var(--ra-ok); }
.stop .rt.late { color: var(--ra-late); font-weight: 600; }
.stop .dot { width: 8px; height: 8px; border-radius: 50%; border: 2px solid var(--ra-line); background: var(--card-background-color, #fff); justify-self: center; z-index: 1; }
.stop::before { content: ''; position: absolute; left: 87px; top: 0; bottom: 0; width: 2px; background: var(--ra-line); opacity: .35; }
.stop:first-child::before { top: 50%; }
.stop:last-child::before { bottom: 50%; }
.stop.end .name { font-weight: 600; }
.stop .pf { font-size: 12.5px; color: var(--secondary-text-color); white-space: nowrap; }
.stop .pf.changed { color: var(--ra-late); font-weight: 600; }
.stop.past { opacity: .5; }
.stop.next .dot { background: var(--ra-accent); border-color: var(--ra-accent); width: 10px; height: 10px; }
.stop.next .name { font-weight: 600; }
.stop.cancelled .name { text-decoration: line-through; }
.stop.cancelled em { font-style: normal; text-decoration: none; color: var(--ra-late); font-size: 12px; }
.stop.added em { font-style: normal; color: var(--ra-late); font-size: 12px; }

.transfer { margin: 8px 0 0 82px; font-size: 12.5px; padding: 5px 9px; border-radius: 6px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
.transfer.risky { color: var(--ra-late); background: rgba(227, 0, 27, .1); }
.updated { margin-top: 14px; font-size: 11.5px; color: var(--secondary-text-color); display: flex; justify-content: space-between; align-items: center; gap: 8px; }

.idle { padding: 20px 16px 6px; text-align: center; }
.board { padding: 6px 16px 14px; container-type: inline-size; }
.board-title { font-size: 15px; font-weight: 600; margin: 8px 0 6px; }
.board-wrap { overflow-x: hidden; }
.board table { width: 100%; border-collapse: collapse; font-size: 14px; }
.board th { text-align: left; font-size: 11.5px; font-weight: 500; color: var(--secondary-text-color); padding: 4px 6px; border-bottom: 1px solid var(--divider-color); }
.board td { padding: 6px; border-bottom: 1px solid var(--divider-color); vertical-align: top; }
.board td.dest { width: 100%; overflow-wrap: anywhere; }
.board td.time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.board td.time .rt { display: block; font-size: 12.5px; }
.board .rt.ok { color: var(--ra-ok); } .board .rt.minor { color: var(--warning-color, #ffa600); font-weight: 600; } .board .rt.late { color: var(--ra-late); font-weight: 600; }
.board td.pf { text-align: right; white-space: nowrap; }
.board td.pf.changed { color: var(--ra-late); font-weight: 600; }
.board .via, .board .msg { font-size: 12px; color: var(--secondary-text-color); margin-top: 1px; overflow-wrap: anywhere;
	display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
/* Sehr schmale Karte: "über …" weglassen, enger setzen */
@container (max-width: 340px) {
	.board .via { display: none; }
	.board td, .board th { padding: 5px 3px; }
	.board table { font-size: 13px; }
}
.board .msg { color: var(--ra-late); }
.board tr.cancelled .dest { text-decoration: line-through; }
.board em { font-style: normal; text-decoration: none; display: inline-block; font-size: 12px; color: var(--ra-late); }
.board em.add { color: var(--ra-ok); }
.board em.sev { color: var(--warning-color, #e68a00); }
.linebadge { display: inline-block; font-size: 12px; font-weight: 600; padding: 1px 7px; border-radius: 4px; border: 1px solid var(--divider-color); white-space: nowrap; }
.board-note { padding: 8px 16px 16px; text-align: center; font-size: 13px; color: var(--secondary-text-color); }
.board-foot { font-size: 11px; color: var(--secondary-text-color); margin-top: 8px; }
.idle-title { font-size: 18px; font-weight: 500; }
.idle-sub { color: var(--secondary-text-color); font-size: 13px; margin-top: 4px; }
.error { padding: 16px; color: var(--ra-late); }
.foot { font-size: 10.5px; color: var(--secondary-text-color); text-align: right; padding: 0 10px 6px; opacity: .7; }

/* Karte */
.ra-reset {
	font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 16px; cursor: pointer; border: 0;
	background: var(--card-background-color, #fff); color: var(--primary-text-color); box-shadow: 0 1px 4px rgba(0,0,0,.35);
}
.ra-reset span { margin-right: 4px; }
.ra-gps { font-size: 11.5px; padding: 3px 8px; border-radius: 10px; background: var(--card-background-color, #fff); color: var(--primary-text-color); box-shadow: 0 1px 3px rgba(0,0,0,.3); }
.ra-label { background: var(--card-background-color, #fff); color: var(--primary-text-color); border: 0; box-shadow: 0 1px 3px rgba(0,0,0,.3); font-size: 12px; font-weight: 600; padding: 2px 6px; }
.ra-label::before { display: none; }
.ra-dark-tiles { filter: invert(1) hue-rotate(180deg) brightness(.85) contrast(.9) saturate(.6); }
.leaflet-container { font: inherit; background: var(--secondary-background-color); }
.leaflet-control-attribution { font-size: 10px; background: rgba(255,255,255,.7) !important; }
`;

export const STYLE = `${leafletCss}\n${maplibreCss}\n${CARD_CSS}`;
