from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import appdock  # noqa: E402
from appdock import AppDockConfig, apply_update  # noqa: E402


class UpdateLockAliasRegressionTests(unittest.TestCase):
    def _write_release_tree(self, root: Path) -> None:
        members = {
            "appdock.py": b"new app",
            "static/app.js": b"js",
            "static/app.css": b"css",
            "scripts/update_helper.py": b"helper",
            "scripts/path_safety.ps1": b"safety",
            "scripts/install.ps1": b"install",
            "scripts/uninstall.ps1": b"uninstall",
        }
        for relative, content in members.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        manifest = {
            "schema_version": 2,
            "files": [
                {"path": relative, "sha256": hashlib.sha256(content).hexdigest()}
                for relative, content in sorted(members.items())
            ],
        }
        (root / "RELEASE-MANIFEST.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )

    def test_apply_update_alias_reentrancy_releases_lock_for_fresh_process(self) -> None:
        repo_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchor = root / "anchor"
            anchor.mkdir()
            for attempt in range(3):
                data_alias = anchor / ".." / f"data-{attempt}"
                config = AppDockConfig.from_environment(data_dir=data_alias)
                config.ensure()
                staged = config.updates_root / "0.3.0"
                staged.mkdir()
                self._write_release_tree(staged)
                install = root / f"install-{attempt}"

                result = apply_update(staged, install, config.data_root)

                self.assertTrue(result["applied"])
                self.assertEqual(appdock._UPDATE_LOCK_STATES, {})
                environment = os.environ.copy()
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["PYTHONPATH"] = str(repo_root)
                probe = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        (
                            "import sys, appdock; "
                            "lock=appdock.acquire_update_lock(sys.argv[1]); "
                            "lock.release(); print('acquired')"
                        ),
                        str(config.data_root.resolve()),
                    ],
                    cwd=repo_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    shell=False,
                    close_fds=True,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertEqual(probe.stdout.strip(), "acquired")
                self.assertEqual(appdock._UPDATE_LOCK_STATES, {})


if __name__ == "__main__":
    unittest.main()
