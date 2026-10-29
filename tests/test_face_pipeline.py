import re
import hashlib
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "vendor/human/models"
PINNED = {
    "facemesh.bin": "3826da640b0a3021161605369ee6af293f75d518040355b960ec71a3390c1c0b",
    "facemesh.json": "b60ca26f404724f43bd2b1575761d8265180e1f053cc0731caa68462927309e7",
}


class FaceMeshPipelineTests(unittest.TestCase):
    def test_facemesh_model_files_match_pinned_hashes_and_are_documented(self):
        sources = (MODELS / "MODEL-SOURCES.txt").read_text(encoding="utf-8")
        for name, digest in PINNED.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((MODELS / name).read_bytes()).hexdigest(), digest)
                self.assertIn(digest, sources)

    def test_server_allowlists_facemesh_files(self):
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('"human/models/facemesh.json"', server)
        self.assertIn('"human/models/facemesh.bin"', server)

    def test_alignment_uses_isolated_instance_and_same_config_for_reference_and_frames(self):
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        self.assertIn("async function ensureAlignModel()", kiosk)
        self.assertIn("detectAligned(reference,deadline)", kiosk)
        self.assertIn("detectAligned(frame,deadline)", kiosk)
        # The baseline instance must keep mesh disabled so a facemesh failure cannot break it.
        baseline = kiosk[kiosk.index("faceModel=new HumanBundle.Human"):kiosk.index("faceModelPromise=faceModel.load()")]
        self.assertIn("mesh:{enabled:false}", baseline)
        self.assertNotIn("facemesh", baseline)
        self.assertIn("facemesh 모델이 로드되지 않았습니다", kiosk)
        self.assertIn("기본 참고 분석으로 계속 진행", kiosk)
        self.assertIn("판정 아님", kiosk)
        self.assertNotIn("alignmentSummary.improved>", kiosk)  # alignment never drives a verdict

    def test_alignment_results_do_not_reach_server_requests(self):
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        for forbidden in ("alignmentPairs", "alignmentSummary", "alignedReferenceFace"):
            self.assertNotIn(f"{forbidden}}}", kiosk)
            self.assertNotRegex(kiosk, rf"JSON\.stringify\([^)]*{forbidden}")

    def test_offline_evaluator_refuses_datasets_inside_the_repository(self):
        result = subprocess.run(
            ["node", "tools/evaluate_faces.js", str(ROOT / "tests")],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("outside the repository", result.stderr)


if __name__ == "__main__":
    unittest.main()


class HairRobustnessExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kiosk = (ROOT / "kiosk.html").read_text(encoding="utf8")

    def test_diagnostic_rows_required_by_spec_exist(self):
        for element in ("diagFacemesh", "diagAlignApplied", "diagAlignment", "diagCrop", "diagReason", "diagReferenceBox", "diagLiveBox"):
            self.assertIn(f'id="{element}"', self.kiosk)
        self.assertIn("정렬 전/후 원시 거리", self.kiosk)
        self.assertIn("판정 불가 사유", self.kiosk)

    def test_similarity_percent_is_labelled_not_a_probability(self):
        self.assertIn("동일인일 확률이 아닙니다", self.kiosk)
        self.assertIn("확률 아님", self.kiosk)

    def test_crop_and_alignment_experiments_never_feed_verdict_or_server(self):
        for forbidden in ("refCore", "liveCore", "cropText", "alignmentPairs"):
            for request in re.findall(r"api\('/api/[^']+',\{[^}]*\}", self.kiosk):
                self.assertNotIn(forbidden, request)
        analysis = self.kiosk[self.kiosk.index("$('cameraContinue').onclick"):self.kiosk.index("$('completeSelfDemo').onclick")]
        self.assertIn("ExamFaceSimilarity.evaluate(\n\t\t\treferenceResult.face,\n\t\t\tdetectedFaces", analysis)
        self.assertIn("evaluateLiveness(detectedFaces", analysis)
        # PAD input is the baseline capture only: the crop/alignment results are not passed to it.
        self.assertNotIn("evaluateLiveness(refCore", analysis)
        self.assertNotIn("evaluateLiveness(alignedFrame", analysis)

    def test_crop_experiment_is_safe_when_it_fails(self):
        self.assertIn("catch(error){cropText=`측정 실패", self.kiosk)
        self.assertIn("results.push(result)", self.kiosk)  # embeddings are zeroed with the other results

    def test_facemesh_failure_text_and_fallback(self):
        self.assertIn("기본 참고 분석으로 전환", self.kiosk)

    def test_uncertain_result_offers_supervisor_path_in_ui(self):
        self.assertIn('id="faceHelp"', self.kiosk)
        self.assertIn("/api/kiosk/self-demo/request-review", self.kiosk)
