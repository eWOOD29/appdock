from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import privacy_scan


class PrivacyScanTests(unittest.TestCase):
    def test_decoded_structured_values_and_archive_bytes_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            encoded = "C%3A%5C" + "Users%5C" + "PrivatePerson%5Cproject"
            (root / "config.json").write_text('{"path":"' + encoded + '"}', encoding="utf-8")
            findings = privacy_scan.scan(root)
            self.assertTrue(any("absolute Windows user path" in item for item in findings))

            archive_path = root / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("config.json", '{"url":"' + 'https://' + 'drive.google.com/file/example' + '"}')
            findings = privacy_scan.scan_archive(archive_path)
            self.assertTrue(any("Google Drive URL" in item for item in findings))

    def test_synthetic_public_values_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config.json").write_text(
                '{"provider":"http://127.0.0.1:19091/widgets","link":"https://private.example.invalid"}',
                encoding="utf-8",
            )
            self.assertEqual(privacy_scan.scan(root), [])


if __name__ == "__main__":
    unittest.main()
