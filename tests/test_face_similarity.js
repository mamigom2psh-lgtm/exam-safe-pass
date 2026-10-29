const test = require('node:test');
const assert = require('node:assert/strict');
const { evaluate, explainHumanDistance, summarizeScoreDistribution, evaluateLiveness, summarizeLivenessFrames, cameraErrorMessage } = require('../face_similarity.js');

const face = (embedding) => ({ embedding: Array.from({ length: 128 }, (_, i) => embedding + i / 1000) });
const compareScores = (score) => () => score;

test('one detected face reports an informational similarity score', () => {
	const result = evaluate([face(0)], [face(0)], compareScores(0.91));
	assert.equal(result.status, 'reference_only');
	assert.equal(result.score, 0.91);
	assert.equal(result.threshold, undefined);
});

test('typed-array model embeddings can be compared', () => {
	const result = evaluate(
		[{ embedding: new Float32Array(128).fill(0.1) }],
		[{ embedding: new Float32Array(128).fill(0.2) }],
		compareScores(0.7),
	);
	assert.equal(result.status, 'reference_only');
});

test('identical embeddings produce zero distance and perfect similarity', () => {
	const embedding = face(0).embedding;
	const result = evaluate(
		[{ embedding }],
		[{ embedding: embedding.slice() }],
		() => 1,
		() => 0,
	);
	assert.deepEqual(result, {
		status: 'reference_only',
		score: 1,
		distance: 0,
	});
});

test('invalid embedding values are rejected by face diagnostics', () => {
	const { inspectFace } = require('../face_similarity.js');
	assert.deepEqual(inspectFace({
		box: [1, 2, 30, 40],
		embedding: new Float32Array([0.1, Number.NaN]),
	}, 640, 480), {
		detected: true,
		box: [1, 2, 30, 40],
		inputWidth: 640,
		inputHeight: 480,
		embeddingAvailable: true,
		embeddingDimension: 2,
		embeddingValid: false,
	});
});

test('a different detected face remains an informational low-similarity result', () => {
	const result = evaluate([face(0)], [face(1)], compareScores(0.18));
	assert.equal(result.status, 'reference_only');
	assert.equal(result.score, 0.18);
});

test('a 40 percent score is not presented as a likely identity match', () => {
	const result = evaluate([face(0)], [face(1)], compareScores(0.4));
	assert.equal(result.status, 'reference_only');
	assert.equal(result.score, 0.4);
});

test('no detected face requests a new capture', () => {
	assert.equal(evaluate([face(0)], [], compareScores(0)).status, 'face_missing');
});

test('multiple detected faces are rejected for recapture', () => {
	assert.deepEqual(
		evaluate([face(0)], [face(0), face(1)], compareScores(0.9)),
		{ status: 'multiple_faces', count: 2 },
	);
});

test('missing or ambiguous registered photos are not compared', () => {
	assert.equal(evaluate([], [face(0)], compareScores(0.9)).status, 'reference_face_missing');
	assert.deepEqual(
		evaluate([face(0), face(1)], [face(0)], compareScores(0.9)),
		{ status: 'reference_face_count', count: 2 },
	);
});

test('missing embeddings and comparison errors fail closed', () => {
	assert.equal(evaluate([face(0)], [{ embedding: [1, 2] }], compareScores(1)).status, 'embedding_unavailable');
	assert.equal(evaluate([face(0)], [face(1)], compareScores(Number.NaN)).status, 'comparison_failed');
});

test('camera permission denial has explicit retry guidance', () => {
	assert.match(cameraErrorMessage('NotAllowedError'), /권한이 거부/);
});

test('invalid model output never produces a positive similarity', () => {
	assert.equal(evaluate([face(0)], [face(1)], compareScores(Number.NaN)).status, 'comparison_failed');
	assert.equal(evaluate([face(0)], [{ embedding: [1, 2] }], compareScores(1)).status, 'embedding_unavailable');
});

test('single-frame anti-spoof scores remain PoC candidates, not identity results', () => {
	assert.deepEqual(evaluateLiveness([{ real: 0.83 }]), {
		status: 'live_candidate',
		score: 0.83,
		threshold: 0.5,
	});
	assert.deepEqual(evaluateLiveness([{ real: 0.18 }]), {
		status: 'spoof_suspected',
		score: 0.18,
		threshold: 0.5,
	});
});

test('phone-screen photo failure case is recorded as a candidate, never authentication', () => {
	const result = evaluateLiveness([{ real: 0.84 }]);
	assert.equal(result.status, 'live_candidate');
	assert.notEqual(result.status, 'authenticated');
});

test('multi-frame summary records score range without producing an authentication verdict', () => {
	const summary = summarizeLivenessFrames([
		evaluateLiveness([{ real: 0.75 }]),
		evaluateLiveness([{ real: 0.84 }]),
		evaluateLiveness([{ real: 0.69 }]),
	]);
	assert.deepEqual(summary, {
		status: 'observed',
		frameCount: 3,
		validFrameCount: 3,
		statusCounts: { live_candidate: 3 },
		scoreMin: 0.69,
		scoreMax: 0.84,
		scoreRange: 0.15,
	});
	assert.notEqual(summary.status, 'authenticated');
});

test('multi-frame summary fails closed when no usable scores exist', () => {
	assert.deepEqual(summarizeLivenessFrames([
		{ status: 'liveness_unavailable' },
	]), {
		status: 'observed',
		frameCount: 1,
		validFrameCount: 0,
		statusCounts: { liveness_unavailable: 1 },
	});
});

test('anti-spoof model missing faces, multiple faces, and invalid scores fail closed', () => {
	assert.equal(evaluateLiveness([]).status, 'face_missing');
	assert.deepEqual(evaluateLiveness([{ real: 0.9 }, { real: 0.8 }]), {
		status: 'multiple_faces',
		count: 2,
	});
	assert.equal(evaluateLiveness([{}]).status, 'liveness_unavailable');
	assert.equal(evaluateLiveness([{ real: Number.NaN }]).status, 'liveness_unavailable');
	assert.equal(evaluateLiveness([{ real: 1.1 }]).status, 'liveness_unavailable');
});

test('distance explanation reproduces Human match scores and keeps self-distance at zero', () => {
	const explained = explainHumanDistance(2785.92);
	assert.equal(explained.similarity, 0.45);
	assert.ok(Math.abs(explained.sumSquaredDiff - 111.4368) < 1e-9);
	assert.ok(Math.abs(explained.euclidean - 10.5565) < 1e-3);
	assert.equal(explainHumanDistance(0).similarity, 1);
	assert.equal(explainHumanDistance(Number.NaN), undefined);
	assert.equal(explainHumanDistance(-1), undefined);
});

test('score distribution is descriptive only and ignores invalid values', () => {
	const out = summarizeScoreDistribution({
		same: [2700, 2600, Number.NaN, -1, 2800],
		other: [],
	});
	assert.deepEqual(out.same, { count: 3, min: 2600, median: 2700, max: 2800 });
	assert.deepEqual(out.other, { count: 0 });
	assert.equal(out.same.match, undefined);
	assert.equal(out.same.threshold, undefined);
});

test('evaluate never returns an authenticated status regardless of score', () => {
	const f = [face(0)];
	assert.equal(evaluate(f, f, () => 1, () => 0).status, 'reference_only');
});

const fs = require('node:fs');
const path = require('node:path');
const { browserVerificationState, evaluatePadEvidence, assessThresholdReadiness } = require('../face_similarity.js');
const recorded = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'recorded_field_cases.json'), 'utf8'));

test('recorded Anti-Spoof misclassifications show the model score cannot be trusted as proof', () => {
	for (const item of recorded.pad_misclassifications_human_antispoof_3_3_6) {
		const result = evaluateLiveness([{ real: item.score }]);
		assert.equal(result.status, item.model_status, item.scenario);
		assert.equal(item.truth, 'presentation_attack');
	}
	const wronglyLive = recorded.pad_misclassifications_human_antispoof_3_3_6
		.filter((item) => evaluateLiveness([{ real: item.score }]).status === 'live_candidate');
	assert.ok(wronglyLive.length >= 2);
});

test('PAD evidence stays reference-only even for perfect multi-frame scores', () => {
	const frames = Array.from({ length: 8 }, () => ({ status: 'live_candidate', score: 0.99 }));
	const pad = evaluatePadEvidence(summarizeLivenessFrames(frames));
	assert.equal(pad.status, 'reference_only');
	assert.equal(pad.verified, false);
	assert.ok(pad.reasons.includes('no_validated_pad_model_configured'));
});

test('browser verification state is never verified or rejected', () => {
	assert.equal(browserVerificationState(undefined, undefined).state, 'pending');
	const live = { status: 'live_candidate', score: 0.99 };
	for (const comparison of [{ status: 'reference_only', score: 1 }, { status: 'face_missing' }]) {
		const result = browserVerificationState(comparison, live);
		assert.equal(result.state, 'inconclusive');
		assert.equal(result.supervisorRequired, true);
	}
	assert.equal(browserVerificationState({ status: 'reference_only' }, { status: 'spoof_suspected' }).reason, 'spoof_suspected');
});

test('recorded similarity samples are too small to define a match threshold', () => {
	const groups = {
		same: recorded.face_similarity_percent_human_3_3_6.same_person_live_repeats
			.concat(recorded.face_similarity_percent_human_3_3_6.same_person_phone_photo),
		other: recorded.face_similarity_percent_human_3_3_6.other_person_phone_photo,
	};
	const readiness = assessThresholdReadiness(groups);
	assert.equal(readiness.ready, false);
	assert.equal(readiness.sampleSufficient, false);
	assert.deepEqual(readiness.counts, { same: 7, other: 1 });
	const enough = assessThresholdReadiness({ same: new Array(30).fill(1), other: new Array(30).fill(2) });
	assert.equal(enough.ready, false);
});

test('identical embeddings give distance zero, different faces give larger distances', () => {
	const vector = (seed) => Array.from({ length: 1024 }, (_, i) => Math.sin(seed * (i + 1)));
	const distance = (a, b) => Math.round(100 * 25 * a.reduce((sum, x, i) => sum + (x - b[i]) ** 2, 0)) / 100;
	const a = vector(1);
	const nearSame = a.map((x, i) => x + Math.sin(i) * 0.01);
	const other = vector(7);
	assert.equal(distance(a, a), 0);
	assert.ok(distance(a, nearSame) < distance(a, other));
});
