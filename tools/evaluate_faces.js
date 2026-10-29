#!/usr/bin/env node
// Offline same/other-person evaluation of Human FaceRes, baseline vs Facemesh-aligned.
// Usage: NODE_PATH=<dir with @tensorflow/* + jpeg-js + pngjs> node tools/evaluate_faces.js <dataset-dir> [report.json]
// Dataset layout (keep OUTSIDE git): <dataset-dir>/<person>/<image>.jpg|png
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
	const embed = async (file, aligned) => {
		human.config.face.mesh.enabled = aligned;
		human.config.face.detector.rotation = aligned;
		const img = decode(file);
		const rgb = new Uint8Array(img.width * img.height * 3);
		for (let i = 0, o = 0; i < img.data.length; i += 4) { rgb[o++] = img.data[i]; rgb[o++] = img.data[i + 1]; rgb[o++] = img.data[i + 2]; }
		const tensor = human.tf.tensor3d(rgb, [img.height, img.width, 3], 'int32');
		const result = await human.detect(tensor);
		tensor.dispose();
		return result.face.length === 1 ? Array.from(result.face[0].embedding) : null;
	};

	const people = fs.readdirSync(dir).filter((n) => fs.statSync(path.join(dir, n)).isDirectory());
	const files = [];
	for (const person of people) {
		for (const name of fs.readdirSync(path.join(dir, person)).filter((n) => /\.(jpe?g|png)$/i.test(n)).sort()) {
			files.push({ person, file: path.join(dir, person, name) });
		}
	}
	const vectors = { baseline: [], aligned: [] };
	let undetected = 0;
	for (const item of files) {
		for (const mode of ['baseline', 'aligned']) vectors[mode].push(await embed(item.file, mode === 'aligned'));
		if (!vectors.baseline.at(-1) || !vectors.aligned.at(-1)) undetected++;
	}
	const groups = { baseline: { same: [], other: [], self: [] }, aligned: { same: [], other: [], self: [] } };
	const pairs = [];
	for (const mode of ['baseline', 'aligned']) {
		for (let i = 0; i < files.length; i++) {
			if (vectors[mode][i]) groups[mode].self.push(human.match.distance(vectors[mode][i], vectors[mode][i]));
			for (let j = i + 1; j < files.length; j++) {
				if (!vectors[mode][i] || !vectors[mode][j]) continue;
				const d = human.match.distance(vectors[mode][i], vectors[mode][j]);
				(files[i].person === files[j].person ? groups[mode].same : groups[mode].other).push(d);
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
	};
	for (const mode of ['baseline', 'aligned']) {
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
