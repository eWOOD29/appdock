from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import appdock  # noqa: E402
from appdock import AppDockConfig, apply_update  # noqa: E402
from scripts import update_helper  # noqa: E402


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

    def test_restart_diagnostic_log_rejects_hardlink_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            source = root / "source.txt"
            source.write_text("preserve\n", encoding="utf-8")
            target = runtime / update_helper.RESTART_STDERR_LOG_NAME
            try:
                os.link(source, target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            with self.assertRaises(appdock.AppDockError):
                update_helper._open_restart_log_stream(target)
            self.assertEqual(source.read_text(encoding="utf-8"), "preserve\n")

    def test_restart_log_existing_file_replacement_alias_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            path = runtime / update_helper.RESTART_STDOUT_LOG_NAME
            path.write_bytes(b"old-diagnostic\n")
            protected = root / "protected.txt"
            protected.write_bytes(b"protected-bytes")
            original_open = update_helper._open_existing_no_follow_descriptor
            raced = {"done": False}

            def replace_then_open(candidate: Path, label: str):
                if Path(candidate) == path and not raced["done"]:
                    path.unlink()
                    try:
                        path.symlink_to(protected)
                    except OSError as exc:
                        self.skipTest(f"file symlink creation unavailable: {exc}")
                    raced["done"] = True
                return original_open(candidate, label)

            with patch.object(
                update_helper,
                "_open_existing_no_follow_descriptor",
                side_effect=replace_then_open,
            ):
                with self.assertRaises(appdock.AppDockError):
                    update_helper._open_restart_log_stream(path)

            self.assertTrue(raced["done"])
            self.assertEqual(protected.read_bytes(), b"protected-bytes")

    def test_update_log_existing_file_replacement_alias_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            path = runtime / "update.log"
            path.write_bytes(b"old-update-log\n")
            protected = root / "protected.txt"
            protected.write_bytes(b"protected-bytes")
            original_open = update_helper._open_existing_no_follow_descriptor
            raced = {"done": False}

            def replace_then_open(candidate: Path, label: str):
                if Path(candidate) == path and not raced["done"]:
                    path.unlink()
                    try:
                        path.symlink_to(protected)
                    except OSError as exc:
                        self.skipTest(f"file symlink creation unavailable: {exc}")
                    raced["done"] = True
                return original_open(candidate, label)

            with patch.object(
                update_helper,
                "_open_existing_no_follow_descriptor",
                side_effect=replace_then_open,
            ):
                with self.assertRaises(appdock.AppDockError):
                    update_helper._safe_append_update_log(path, "must not reach protected target")

            self.assertTrue(raced["done"])
            self.assertEqual(protected.read_bytes(), b"protected-bytes")


if __name__ == "__main__":
    unittest.main()
