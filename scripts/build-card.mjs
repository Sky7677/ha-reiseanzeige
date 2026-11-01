// Baut die Dashboard-Karte zu einer einzigen Datei (custom_components/reiseanzeige/frontend/reiseanzeige-card.js),
// inklusive Leaflet, MapLibre und deren CSS. Aufruf: npm run build:card

import * as esbuild from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';

const root = path.join(import.meta.dirname, '..');
const outfile = path.join(root, 'custom_components', 'reiseanzeige', 'frontend', 'reiseanzeige-card.js');

// MapLibre 6 lädt seinen Hintergrundprozess als eigene Datei, die wiederum
// eine weitere lädt. Wir bündeln beide zu einem eigenständigen Skript und
// geben es als Text in die Karte; sie startet es per Blob-Adresse.
const maplibreWorker = {
	name: 'maplibre-worker',
	setup (build) {
		build.onResolve({filter: /^virtual:maplibre-worker$/}, (a) => ({path: a.path, namespace: 'mlw'}));
		build.onLoad({filter: /.*/, namespace: 'mlw'}, async () => {
			const r = await esbuild.build({
				entryPoints: [path.join(root, 'node_modules', 'maplibre-gl', 'dist', 'maplibre-gl-worker.mjs')],
				bundle: true, format: 'iife', minify: true, target: ['es2020'], write: false, logLevel: 'warning', legalComments: 'none',
			});
			return {contents: r.outputFiles[0].text, loader: 'text'};
		});
	},
};

await esbuild.build({
	entryPoints: [path.join(root, 'frontend', 'src', 'index.js')],
	bundle: true,
	format: 'esm',
	minify: true,
	target: ['es2020'],
	loader: {'.css': 'text'},
	legalComments: 'eof',
	outfile,
	logLevel: 'warning',
	plugins: [maplibreWorker],
});
console.log(`${path.relative(root, outfile)}: ${Math.round(fs.statSync(outfile).size / 1024)} KB`);
