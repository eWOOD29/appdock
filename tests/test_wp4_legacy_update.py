from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import appdock


class LegacyUpdateCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.install = self.root / "install"
        self.staged = self.data / "updates" / "0.1.1"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_release_tree(self, root: Path, marker: bytes) -> None:
        members = {
            "appdock.py": marker,
            "static/app.js": b"js-" + marker,
            "static/app.css": b"css-" + marker,
            "scripts/update_helper.py": b"helper-" + marker,
            "scripts/path_safety.ps1": b"safety-" + marker,
            "scripts/install.ps1": b"install-" + marker,
            "scripts/uninstall.ps1": b"uninstall-" + marker,
        }
        manifest = {
            "schema_version": 2,
            "files": [
                {"path": name, "sha256": hashlib.sha256(payload).hexdigest()}
                for name, payload in sorted(members.items())
            ],
        }
        members[appdock.RELEASE_MANIFEST_NAME] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        for name, payload in members.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

    def test_bounded_legacy_root_updates_and_preserves_launcher_and_data(self) -> None:
        self.install.mkdir(parents=True)
        (self.install / "appdock.py").write_bytes(b"legacy-old")
        (self.install / "run-appdock.cmd").write_bytes(b"generated-launcher")
        self.write_release_tree(self.staged, b"new")
        self.data.mkdir(parents=True, exist_ok=True)
        user_data = self.data / "user-data.json"
        user_data.write_bytes(b"preserve-me")

        result = appdock.apply_update(self.staged, self.install, self.data)

        self.assertTrue(result["applied"])
        self.assertEqual((self.install / "appdock.py").read_bytes(), b"new")
        self.assertEqual((self.install / "run-appdock.cmd").read_bytes(), b"generated-launcher")
        self.assertEqual(user_data.read_bytes(), b"preserve-me")
        self.assertTrue((self.install / appdock.RELEASE_MANIFEST_NAME).is_file())

    def test_legacy_root_with_unknown_file_is_rejected_before_mutation(self) -> None:
        self.install.mkdir(parents=True)
        (self.install / "appdock.py").write_bytes(b"legacy-old")
        (self.install / "unknown.bin").write_bytes(b"must-not-be-owned")
        self.write_release_tree(self.staged, b"new")

        with self.assertRaises(appdock.AppDockError):
            appdock.apply_update(self.staged, self.install, self.data)

        self.assertEqual((self.install / "appdock.py").read_bytes(), b"legacy-old")
        self.assertEqual((self.install / "unknown.bin").read_bytes(), b"must-not-be-owned")


if __name__ == "__main__":
    unittest.main()
