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

	function cameraErrorMessage(name) {
		const messages = {
			NotAllowedError: '카메라 권한이 거부되었습니다. 사이트 설정에서 권한을 허용한 뒤 다시 시도하세요.',
			NotFoundError: '사용 가능한 카메라를 찾지 못했습니다.',
			NotReadableError: '카메라가 다른 앱에서 사용 중이거나 읽을 수 없습니다.',
			SecurityError: '카메라 접근에는 HTTPS 또는 localhost 주소가 필요합니다.',
		};
		return messages[name] || '카메라를 열지 못했습니다. 권한과 장치 상태를 확인하세요.';
	}

	const api = { evaluate, browserVerificationState, evaluatePadEvidence, assessThresholdReadiness, explainHumanDistance, summarizeScoreDistribution, inspectFace, evaluateLiveness, summarizeLivenessFrames, cameraErrorMessage };
	root.ExamFaceSimilarity = api;
	if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
