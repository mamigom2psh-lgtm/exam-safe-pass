(function (root) {
	function isEmbedding(value) {
		return Array.isArray(value)
			|| (ArrayBuffer.isView(value) && !(value instanceof DataView));
	}

	function inspectFace(face, inputWidth, inputHeight) {
		const embedding = face?.embedding;
		const box = Array.isArray(face?.box) && face.box.length >= 4
			? face.box.slice(0, 4).map(Number)
			: null;
		const embeddingValid = isEmbedding(embedding)
			&& embedding.length >= 64
			&& Array.from(embedding).every(Number.isFinite);
		return {
			detected: Boolean(face),
			box,
			inputWidth: Number.isFinite(inputWidth) ? inputWidth : undefined,
			inputHeight: Number.isFinite(inputHeight) ? inputHeight : undefined,
			embeddingAvailable: isEmbedding(embedding),
			embeddingDimension: isEmbedding(embedding) ? embedding.length : undefined,
			embeddingValid,
		};
	}

	function evaluate(referenceFaces, liveFaces, compare, measure) {
		if (!Array.isArray(referenceFaces) || referenceFaces.length === 0) {
			return { status: 'reference_face_missing' };
		}
		if (referenceFaces.length !== 1) {
			return { status: 'reference_face_count', count: referenceFaces.length };
		}
		if (!Array.isArray(liveFaces) || liveFaces.length === 0) {
			return { status: 'face_missing' };
		}
		if (liveFaces.length !== 1) {
			return { status: 'multiple_faces', count: liveFaces.length };
		}
		const reference = referenceFaces[0]?.embedding;
		const live = liveFaces[0]?.embedding;
		if (!isEmbedding(reference) || !isEmbedding(live) || reference.length !== live.length || reference.length < 64) {
			return { status: 'embedding_unavailable' };
		}
		let score;
		let distance;
		try {
			score = compare(reference, live);
			distance = typeof measure === 'function' ? measure(reference, live) : undefined;
		} catch {
			return { status: 'comparison_failed' };
		}
		if (!Number.isFinite(score) || score < 0 || score > 1
			|| (distance !== undefined && (!Number.isFinite(distance) || distance < 0))) {
			return { status: 'comparison_failed' };
		}
		return {
			// No face-similarity threshold has been calibrated for identity
			// verification. Keep the score informational and fail closed.
			status: 'reference_only',
			score,
			...(distance === undefined ? {} : { distance }),
		};
	}

	// Mirrors Human 3.3.6 match.distance/similarity defaults (order 2, multiplier 25,
	// min 0.2, max 0.8) so diagnostics can show every intermediate value.
	function explainHumanDistance(distance) {
		if (!Number.isFinite(distance) || distance < 0) return undefined;
		const sumSquaredDiff = distance / 25;
		const euclidean = Math.sqrt(sumSquaredDiff);
		const scaledRoot = Math.sqrt(distance) / 100;
		const raw = distance === 0 ? 1 : (1 - scaledRoot - 0.2) / 0.6;
		return {
			sumSquaredDiff,
			euclidean,
			scaledRoot,
			rawSimilarity: raw,
			similarity: Math.round(100 * Math.max(Math.min(raw, 1), 0)) / 100,
		};
	}

	// Numeric-only descriptive statistics per label. It never derives a threshold or a match verdict.
	function summarizeScoreDistribution(groups) {
		const out = {};
		for (const [label, values] of Object.entries(groups || {})) {
			const nums = (Array.isArray(values) ? values : []).filter((v) => Number.isFinite(v) && v >= 0).sort((a, b) => a - b);
			if (!nums.length) { out[label] = { count: 0 }; continue; }
			const mid = nums.length >> 1;
			out[label] = {
				count: nums.length,
				min: nums[0],
				median: nums.length % 2 ? nums[mid] : (nums[mid - 1] + nums[mid]) / 2,
				max: nums[nums.length - 1],
			};
		}
		return out;
	}

	function evaluateLiveness(liveFaces, threshold = 0.5) {
		if (!Array.isArray(liveFaces) || liveFaces.length === 0) {
			return { status: 'face_missing' };
		}
		if (liveFaces.length !== 1) {
			return { status: 'multiple_faces', count: liveFaces.length };
		}
		const score = liveFaces[0]?.real;
		if (!Number.isFinite(score) || score < 0 || score > 1) {
			return { status: 'liveness_unavailable' };
		}
		return {
			status: score >= threshold ? 'live_candidate' : 'spoof_suspected',
			score,
			threshold,
		};
	}

	function summarizeLivenessFrames(frameResults) {
		if (!Array.isArray(frameResults) || frameResults.length === 0) {
			return {
				status: 'no_frames',
				frameCount: 0,
				validFrameCount: 0,
				statusCounts: {},
			};
		}
		const statusCounts = {};
		const scores = [];
		for (const result of frameResults) {
			const status = typeof result?.status === 'string' ? result.status : 'liveness_unavailable';
			statusCounts[status] = (statusCounts[status] || 0) + 1;
			if (Number.isFinite(result?.score) && result.score >= 0 && result.score <= 1) {
				scores.push(result.score);
			}
		}
		const scoreMin = scores.length ? Math.min(...scores) : undefined;
		const scoreMax = scores.length ? Math.max(...scores) : undefined;
		return {
			status: 'observed',
			frameCount: frameResults.length,
			validFrameCount: scores.length,
			statusCounts,
			...(scores.length ? { scoreMin, scoreMax, scoreRange: Number((scoreMax - scoreMin).toFixed(6)) } : {}),
		};
	}

	// Browser-side result mapping. A browser can never produce "verified" or "rejected":
	// those are decided only by the server's verification policy.
	function browserVerificationState(comparison, liveness) {
		const spoof = liveness?.status === 'spoof_suspected';
		if (!comparison && !liveness) {
			return { state: 'pending', supervisorRequired: false, reason: 'not_started' };
		}
		let reason = 'reference_only_unvalidated';
		if (spoof) reason = 'spoof_suspected';
		else if (comparison && comparison.status !== 'reference_only') reason = comparison.status;
		else if (liveness && liveness.status !== 'live_candidate') reason = liveness.status;
		return { state: 'inconclusive', supervisorRequired: true, reason };
	}

	// PAD (presentation attack detection) evidence is informational until a PAD
	// model has documented validation against print/replay/video attacks.
	// Averaging per-frame scores never upgrades the result.
	function evaluatePadEvidence(frameSummary, options = {}) {
		const reasons = ['pad_not_validated_against_print_replay_video'];
		if (!options.validatedPad) reasons.push('no_validated_pad_model_configured');
		if (!frameSummary || !frameSummary.validFrameCount) reasons.push('no_valid_frame_scores');
		return { status: 'reference_only', verified: false, reasons };
	}

	// Readiness gate for any similarity threshold: requires enough independent
	// samples per class. It never proposes a threshold value itself.
	function assessThresholdReadiness(groups, minPerClass = 30) {
		const counts = {};
		for (const label of ['same', 'other']) {
			const values = groups && Array.isArray(groups[label]) ? groups[label] : [];
			counts[label] = values.filter((v) => Number.isFinite(v) && v >= 0).length;
		}
		const ready = counts.same >= minPerClass && counts.other >= minPerClass;
		return {
			ready: false,
			sampleSufficient: ready,
			counts,
			minPerClass,
			reason: ready ? 'needs_independent_validation_review' : 'insufficient_samples',
		};
	}

	// Exploratory FAR/FRR at caller-supplied distance cut-offs. A "match" means distance <= cutoff.
	// It never picks a cutoff; error rates are only marked reportable when both classes are large enough.
	function computeErrorRates(groups, cutoffs, minPerClass = 30) {
		const clean = (v) => (Array.isArray(v) ? v : []).filter((x) => Number.isFinite(x) && x >= 0);
		const same = clean(groups && groups.same);
		const other = clean(groups && groups.other);
		const readiness = assessThresholdReadiness({ same, other }, minPerClass);
		const rows = (Array.isArray(cutoffs) ? cutoffs : []).filter(Number.isFinite).map((cutoff) => ({
			cutoff,
			frr: same.length ? same.filter((d) => d > cutoff).length / same.length : null,
			far: other.length ? other.filter((d) => d <= cutoff).length / other.length : null,
		}));
		return { exploratory: true, reportable: readiness.sampleSufficient, counts: readiness.counts, minPerClass, rows, recommendedThreshold: null };
	}

	// Compares paired baseline/aligned distances of the same image pairs.
	function summarizeAlignmentEffect(pairs) {
		const valid = (Array.isArray(pairs) ? pairs : []).filter((p) => Number.isFinite(p?.baseline) && Number.isFinite(p?.aligned));
		if (!valid.length) return { count: 0 };
		const deltas = valid.map((p) => p.aligned - p.baseline).sort((a, b) => a - b);
		const mid = deltas.length >> 1;
		const median = (values) => {
			const sorted = [...values].sort((a, b) => a - b);
			const m = sorted.length >> 1;
			return sorted.length % 2 ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2;
		};
		return {
			count: valid.length,
			medianBaseline: median(valid.map((p) => p.baseline)),
			medianAligned: median(valid.map((p) => p.aligned)),
			improved: deltas.filter((d) => d < 0).length,
			worsened: deltas.filter((d) => d > 0).length,
			medianDelta: deltas.length % 2 ? deltas[mid] : (deltas[mid - 1] + deltas[mid]) / 2,
			conclusion: 'descriptive_only',
		};
	}

	// Per-attack-type PAD error rates (ISO/IEC 30107-3 style). APCER = attacks accepted as live,
	// BPCER = bona fide rejected. Never returns a recommended threshold; attack types with
	// fewer than minPerClass samples are marked unreportable.
	function evaluatePadSamples(samples, threshold, minPerClass = 30) {
		const list = (Array.isArray(samples) ? samples : []).filter((s) => Number.isFinite(s?.score) && typeof s?.truth === 'string');
		const classes = {};
		for (const s of list) {
			const key = s.truth === 'bona_fide' ? 'bona_fide' : String(s.attack_type || 'unspecified_attack');
			(classes[key] = classes[key] || []).push(s.score);
		}
		const out = {};
		for (const [key, scores] of Object.entries(classes)) {
			const accepted = scores.filter((v) => v >= threshold).length;
			out[key] = {
				count: scores.length,
				acceptedAsLive: accepted,
				rate: accepted / scores.length,
				metric: key === 'bona_fide' ? 'BPCER=1-rate' : 'APCER=rate',
				reportable: scores.length >= minPerClass,
			};
		}
		const types = ['phone_screen', 'printed_photo', 'replay_video'];
		return {
			threshold,
			classes: out,
			untestedAttackTypes: types.filter((t) => !out[t]),
			reportable: Object.values(out).length > 0 && Object.values(out).every((c) => c.reportable),
			recommendedThreshold: null,
		};
	}

	function cameraErrorMessage(name) {
		const messages = {
			NotAllowedError: '카메라 권한이 거부되었습니다. 사이트 설정에서 권한을 허용한 뒤 다시 시도하세요.',
			NotFoundError: '사용 가능한 카메라를 찾지 못했습니다.',
			NotReadableError: '카메라가 다른 앱에서 사용 중이거나 읽을 수 없습니다.',
			SecurityError: '카메라 접근에는 HTTPS 또는 localhost 주소가 필요합니다.',
		};
		return messages[name] || '카메라를 열지 못했습니다. 권한과 장치 상태를 확인하세요.';
	}

	// Crop strategies for hairstyle-robustness experiments (measurement only, never a verdict path).
	// 'detector' keeps the detector box; 'face_core' trims hair-line/side-hair margins from the box.
	const CROP_STRATEGIES = {
		detector: { x: 0, y: 0, w: 1, h: 1 },
		face_core: { x: 0.12, y: 0.18, w: 0.76, h: 0.74 },
	};
	function cropRegion(box, imageWidth, imageHeight, strategy = 'face_core') {
		const f = CROP_STRATEGIES[strategy];
		if (!f || !Array.isArray(box) || box.length < 4 || box.some((v) => !Number.isFinite(v))) return undefined;
		if (!(imageWidth > 0) || !(imageHeight > 0) || !(box[2] > 0) || !(box[3] > 0)) return undefined;
		const x = Math.max(0, box[0] + box[2] * f.x);
		const y = Math.max(0, box[1] + box[3] * f.y);
		const w = Math.min(imageWidth - x, box[2] * f.w);
		const h = Math.min(imageHeight - y, box[3] * f.h);
		if (!(w >= 16) || !(h >= 16)) return undefined;
		return { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h) };
	}

	// Descriptive per-scenario/per-strategy distance stats for hairstyle-change evaluation.
	// Never derives a threshold; reportable only with >= minPerScenario pairs per scenario.
	const HAIR_SCENARIOS = ['identical', 'same_short_vs_long', 'same_tied', 'same_other_photo', 'other_person', 'phone_screen'];
	function summarizeHairScenarios(rows, minPerScenario = 30) {
		const buckets = {};
		for (const r of Array.isArray(rows) ? rows : []) {
			if (!HAIR_SCENARIOS.includes(r?.scenario) || typeof r?.strategy !== 'string' || !Number.isFinite(r?.distance) || r.distance < 0) continue;
			((buckets[r.strategy] ||= {})[r.scenario] ||= []).push(r.distance);
		}
		const strategies = {};
		let reportable = Object.keys(buckets).length > 0;
		for (const [strategy, scenarios] of Object.entries(buckets)) {
			strategies[strategy] = {};
			for (const [scenario, values] of Object.entries(scenarios)) {
				const sorted = [...values].sort((a, b) => a - b);
				const m = sorted.length >> 1;
				strategies[strategy][scenario] = {
					count: sorted.length,
					min: sorted[0],
					median: sorted.length % 2 ? sorted[m] : (sorted[m - 1] + sorted[m]) / 2,
					max: sorted[sorted.length - 1],
				};
				if (sorted.length < minPerScenario) reportable = false;
			}
		}
		return { strategies, missingScenarios: HAIR_SCENARIOS.filter((sc) => !Object.values(strategies).some((st) => st[sc])), reportable, recommendedThreshold: null, conclusion: 'descriptive_only' };
	}

	// Checks that a processing change did not make attack presentations (phone/print/replay)
	// more acceptable as live. Uses the same PAD threshold for both runs.
	function comparePadAcceptance(before, after, threshold) {
		const a = evaluatePadSamples(before, threshold);
		const b = evaluatePadSamples(after, threshold);
		const types = {};
		for (const key of Object.keys(b.classes)) {
			if (key === 'bona_fide') continue;
			const prev = a.classes[key]?.acceptedAsLive ?? 0;
			types[key] = { before: prev, after: b.classes[key].acceptedAsLive, worsened: b.classes[key].acceptedAsLive > prev, count: b.classes[key].count };
		}
		return { types, anyWorsened: Object.values(types).some((t) => t.worsened), reportable: a.reportable && b.reportable };
	}

	const api = { cropRegion, CROP_STRATEGIES, summarizeHairScenarios, comparePadAcceptance, evaluate, browserVerificationState, evaluatePadEvidence, assessThresholdReadiness, computeErrorRates, summarizeAlignmentEffect, evaluatePadSamples, explainHumanDistance, summarizeScoreDistribution, inspectFace, evaluateLiveness, summarizeLivenessFrames, cameraErrorMessage };
	root.ExamFaceSimilarity = api;
	if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
