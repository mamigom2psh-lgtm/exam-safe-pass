"""The judge export and reset tools: whitelist-only copy, no history/photos/DB/.env, protected dev data."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = (".jpg", ".jpeg", ".sqlite3", ".db", ".npy")


def run(*args, env=None):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env=env if env is not None else os.environ)


class ExportToolTests(unittest.TestCase):
    def test_export_is_clean_single_commit_and_has_no_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "judge"
            result = run("tools/export_judge_repo.py", str(out), "--git-init")
            self.assertEqual(result.returncode, 0, result.stderr)
            files = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts]
            self.assertIn("render.yaml", files)
            self.assertIn("judge_mode.py", files)
            for name in files:
                lowered = name.lower()
                self.assertFalse(lowered.endswith(FORBIDDEN), name)
                self.assertFalse(name.startswith(("images/", "demo_photos/", "tests/")), name)
                self.assertNotEqual(name, ".env")
            self.assertFalse(any(Path(n).name.startswith("exam_safe_pass.sqlite") for n in files))
            self.assertFalse(any(n in ("demo_candidates.html", "admin_login.html") for n in files))
            log = subprocess.run(["git", "log", "--oneline"], cwd=out, capture_output=True, text=True).stdout.splitlines()
            self.assertEqual(len(log), 1)
            self.assertEqual(subprocess.run(["git", "remote"], cwd=out, capture_output=True, text=True).stdout.strip(), "")
            tracked = subprocess.run(["git", "ls-files"], cwd=out, capture_output=True, text=True).stdout.splitlines()
            self.assertFalse([t for t in tracked if t.lower().endswith(FORBIDDEN)])

    def test_export_refuses_paths_inside_repo_or_non_empty_folders(self):
        self.assertEqual(run("tools/export_judge_repo.py", str(ROOT / "judge-out")).returncode, 2)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "keep.txt").write_text("x")
            self.assertEqual(run("tools/export_judge_repo.py", tmp).returncode, 2)
            self.assertTrue((Path(tmp) / "keep.txt").exists())
        self.assertFalse((ROOT / "judge-out").exists())

    def test_render_yaml_has_no_secret_values_and_uses_health_check(self):
        text = (ROOT / "render.yaml").read_text(encoding="utf-8")
        self.assertIn("healthCheckPath: /healthz", text)
        self.assertIn("sync: false", text)
        self.assertIn("python3 server.py", text)
        block = text.split("EXAM_SAFE_JUDGE_PASSWORD")[1]
        self.assertNotIn("value:", block)


class ResetToolTests(unittest.TestCase):
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("EXAM_SAFE_")}
        env.update(extra)
        return env

    def test_refuses_without_judge_mode_or_confirmation_or_dev_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, photos = Path(tmp) / "judge.sqlite3", Path(tmp) / "photos"
            db.write_text("x")
            photos.mkdir()
            ok = self.env(EXAM_SAFE_JUDGE_MODE="1", EXAM_SAFE_DB=str(db), EXAM_SAFE_PHOTO_DIR=str(photos))
            self.assertEqual(run("tools/reset_judge_data.py", "--yes", env=self.env(EXAM_SAFE_DB=str(db))).returncode, 2)
            self.assertEqual(run("tools/reset_judge_data.py", env=ok).returncode, 1)
            self.assertTrue(db.exists() and photos.exists())
            for bad in ({"EXAM_SAFE_DB": str(ROOT / "exam_safe_pass.sqlite3")},
                        {"EXAM_SAFE_PHOTO_DIR": str(ROOT / "images")},
                        {"EXAM_SAFE_PHOTO_DIR": str(ROOT / "demo_photos")}):
                self.assertEqual(run("tools/reset_judge_data.py", "--yes", env={**ok, **bad}).returncode, 2, bad)
            self.assertEqual(run("tools/reset_judge_data.py", "--yes", env=ok).returncode, 0)
            self.assertFalse(db.exists() or photos.exists())


if __name__ == "__main__":
    unittest.main()
