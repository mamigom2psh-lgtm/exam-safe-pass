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
