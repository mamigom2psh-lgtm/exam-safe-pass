#!/usr/bin/env node
// Offline same/other-person evaluation of Human FaceRes, baseline vs Facemesh-aligned.
// Usage: NODE_PATH=<dir with @tensorflow/* + jpeg-js + pngjs> node tools/evaluate_faces.js <dataset-dir> [report.json]
// Dataset layout (keep OUTSIDE git): <dataset-dir>/<person>/<image>.jpg|png
// Optional scenario tags in file names: name__short.jpg, __long, __tied, __phone (hairstyle/phone-screen scenarios).
// Modes: baseline, aligned (Facemesh+rotation), core (hair-trimmed face-core crop re-embedded).
// Output: distances and error-rate summary only. Images and embeddings are never written.
const fs = require('fs');
const path = require('path');
const sim = require('../face_similarity.js');

const ROOT = path.resolve(__dirname, '..');
const MODEL_DIR = path.join(ROOT, 'vendor/human/models') + '/';
const CUTOFFS = [1500, 2000, 2500, 3000, 3500]; // exploratory grid, not a recommendation

function decode(file) {
	const buf = fs.readFileSync(file);
	if (/\.png$/i.test(file)) {
		const { PNG } = require('pngjs');
		const p = PNG.sync.read(buf);
		return { width: p.width, height: p.height, data: p.data };
	}
	const jpeg = require('jpeg-js');
	const j = jpeg.decode(buf, { useTArray: true });
	return { width: j.width, height: j.height, data: j.data };
}

async function main() {
	const [dir, out] = process.argv.slice(2);
	if (!dir) { console.error('usage: evaluate_faces.js <dataset-dir> [report.json]'); process.exit(2); }
	const rel = path.relative(ROOT, path.resolve(dir));
	if (!rel.startsWith('..') && !path.isAbsolute(rel)) {
		console.error('Dataset must be outside the repository so face images cannot be committed.');
		process.exit(2);
	}
	// Node's fetch cannot read file://, so serve the vendored models on loopback only.
	const http = require('http');
	const server = http.createServer((req, res) => {
		const name = path.basename(decodeURIComponent(req.url.split('?')[0]));
		const file = path.join(MODEL_DIR, name);
		if (!fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
		res.writeHead(200); res.end(fs.readFileSync(file));
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const modelBase = `http://127.0.0.1:${server.address().port}/`;
	const Human = require(path.join(ROOT, 'node_modules/@vladmandic/human/dist/human.node-wasm.js'));
		const human = new (Human.Human || Human.default)({
		backend: 'wasm', modelBasePath: modelBase, debug: false, warmup: 'none',
		wasmPath: path.join(path.dirname(require.resolve('@tensorflow/tfjs-backend-wasm')), '/'),
		face: {
			enabled: true,
			detector: { modelPath: 'blazeface.json', rotation: false, maxDetected: 5, skipFrames: 0, skipTime: 0, minConfidence: 0.2 },
			mesh: { enabled: true }, attention: { enabled: false }, iris: { enabled: false },
			description: { enabled: true, modelPath: 'faceres.json', skipFrames: 0, skipTime: 0, minConfidence: 0.1 },
			emotion: { enabled: false }, antispoof: { enabled: false }, liveness: { enabled: false },
		},
		body: { enabled: false }, hand: { enabled: false }, gesture: { enabled: false }, object: { enabled: false }, segmentation: { enabled: false },
	});
	await human.load();
	const toRgb = (img) => {
		const rgb = new Uint8Array(img.width * img.height * 3);
		for (let i = 0, o = 0; i < img.data.length; i += 4) { rgb[o++] = img.data[i]; rgb[o++] = img.data[i + 1]; rgb[o++] = img.data[i + 2]; }
		return rgb;
	};
	const detectRgb = async (rgb, width, height) => {
		const tensor = human.tf.tensor3d(rgb, [height, width, 3], 'int32');
		const result = await human.detect(tensor);
		tensor.dispose();
		return result.face.length === 1 ? result.face[0] : null;
	};
	const embed = async (file, mode) => {
		const aligned = mode === 'aligned';
		human.config.face.mesh.enabled = aligned;
		human.config.face.detector.rotation = aligned;
		const img = decode(file);
		const rgb = toRgb(img);
		const face = await detectRgb(rgb, img.width, img.height);
		if (!face || mode !== 'core') return face ? Array.from(face.embedding) : null;
		const region = sim.cropRegion(face.box, img.width, img.height, 'face_core');
		if (!region) return null;
		const crop = new Uint8Array(region.w * region.h * 3);
		for (let y = 0; y < region.h; y++) {
			const from = ((region.y + y) * img.width + region.x) * 3;
			crop.set(rgb.subarray(from, from + region.w * 3), y * region.w * 3);
		}
		const coreFace = await detectRgb(crop, region.w, region.h);
		return coreFace ? Array.from(coreFace.embedding) : null;
	};
	const tagOf = (file) => (path.basename(file).match(/__(short|long|tied|phone)\./i) || [])[1]?.toLowerCase();

	const people = fs.readdirSync(dir).filter((n) => fs.statSync(path.join(dir, n)).isDirectory());
	const files = [];
	for (const person of people) {
		for (const name of fs.readdirSync(path.join(dir, person)).filter((n) => /\.(jpe?g|png)$/i.test(n)).sort()) {
			files.push({ person, file: path.join(dir, person, name) });
		}
	}
	const MODES = ['baseline', 'aligned', 'core'];
	const vectors = { baseline: [], aligned: [], core: [] };
	let undetected = 0;
	for (const item of files) {
		for (const mode of MODES) vectors[mode].push(await embed(item.file, mode));
		if (!vectors.baseline.at(-1) || !vectors.aligned.at(-1)) undetected++;
	}
	const groups = { baseline: { same: [], other: [], self: [] }, aligned: { same: [], other: [], self: [] }, core: { same: [], other: [], self: [] } };
	const scenarioRows = [];
	const scenarioOf = (a, b) => {
		const tags = [tagOf(a.file), tagOf(b.file)];
		if (tags.includes('phone')) return 'phone_screen';
		if (a.person !== b.person) return 'other_person';
		if (tags.includes('short') && tags.includes('long')) return 'same_short_vs_long';
		if (tags.includes('tied')) return 'same_tied';
		return 'same_other_photo';
	};
	const pairs = [];
	for (const mode of MODES) {
		for (let i = 0; i < files.length; i++) {
			if (vectors[mode][i]) {
				const self = human.match.distance(vectors[mode][i], vectors[mode][i]);
				groups[mode].self.push(self);
				scenarioRows.push({ scenario: 'identical', strategy: mode, distance: self });
			}
			for (let j = i + 1; j < files.length; j++) {
				if (!vectors[mode][i] || !vectors[mode][j]) continue;
				const d = human.match.distance(vectors[mode][i], vectors[mode][j]);
				(files[i].person === files[j].person ? groups[mode].same : groups[mode].other).push(d);
				scenarioRows.push({ scenario: scenarioOf(files[i], files[j]), strategy: mode, distance: d });
			}
		}
	}
	for (let i = 0; i < files.length; i++) for (let j = i + 1; j < files.length; j++) {
		if (vectors.baseline[i] && vectors.baseline[j] && vectors.aligned[i] && vectors.aligned[j]) {
			pairs.push({ baseline: human.match.distance(vectors.baseline[i], vectors.baseline[j]), aligned: human.match.distance(vectors.aligned[i], vectors.aligned[j]) });
		}
	}
	const report = {
		note: 'Exploratory. Distances only; no images or embeddings. Not a verified match threshold.',
		humanVersion: human.version, images: files.length, people: people.length, undetectedImages: undetected,
		modes: {},
		alignmentEffect: sim.summarizeAlignmentEffect(pairs),
		hairScenarios: sim.summarizeHairScenarios(scenarioRows),
	};
	for (const mode of MODES) {
		report.modes[mode] = {
			distribution: sim.summarizeScoreDistribution(groups[mode]),
			errorRates: sim.computeErrorRates(groups[mode], CUTOFFS),
		};
	}
	const text = JSON.stringify(report, null, 2);
	if (out) fs.writeFileSync(out, text);
	console.log(text);
	server.close();
}
main().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });
