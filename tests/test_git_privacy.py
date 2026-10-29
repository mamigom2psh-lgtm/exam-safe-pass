import unittest
from tools import git_privacy_audit as audit


class GitPrivacyTests(unittest.TestCase):
    def test_tracked_files_and_branches_contain_no_photos_or_databases(self):
        report = audit.audit()
        self.assertEqual(report["tracked"], [])
        exposed = {r: v for r, v in report["refs"].items()
                   if r.startswith(("refs/heads/", "refs/remotes/"))}
        self.assertEqual(exposed, {})

    def test_gitignore_blocks_sensitive_extensions(self):
        text = open(".gitignore", encoding="utf8").read()
        for needle in ("images/", "*.jpg", "*.sqlite3", "*.embedding", "*.npy"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
