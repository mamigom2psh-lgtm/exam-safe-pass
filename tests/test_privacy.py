import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".js", ".html", ".md", ".json", ".txt"}
SKIP_PARTS = {"node_modules", "vendor", "__pycache__", ".git"}


def source_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES and not (SKIP_PARTS & set(path.parts)):
            yield path


class PrivacyGuardTests(unittest.TestCase):
    def test_gitignore_blocks_photos_embeddings_and_databases(self):
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for rule in ("images/", "*.sqlite3", "*.embedding"):
            self.assertIn(rule, rules)

    def test_sources_contain_no_resident_numbers_or_embedded_images(self):
        rrn = re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)")
        inline_image = re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/]{200,}")
        for path in source_files():
            if "test_privacy" in path.name or "test_server_hardening" in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertIsNone(rrn.search(text))
                self.assertIsNone(inline_image.search(text))

    def test_server_never_persists_face_images_or_embeddings(self):
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        schema = server[server.index("CREATE TABLE IF NOT EXISTS tokens"):server.index("event_columns")]
        for column_hint in ("embedding", "descriptor", "face_image", "face_photo", "biometric"):
            self.assertNotIn(column_hint, schema.lower())

    def test_kiosk_clears_embeddings_and_does_not_post_face_data(self):
        kiosk = (ROOT / "kiosk.html").read_text(encoding="utf-8")
        self.assertIn("embedding?.fill(0)", kiosk)
        for field in ("toDataURL", "toBlob"):
            self.assertNotIn(field, kiosk)


if __name__ == "__main__":
    unittest.main()
