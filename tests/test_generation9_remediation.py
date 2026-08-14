from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import appdock
from appdock import AppDockError, AppDockConfig
from scripts import update_helper


class Generation9RemediationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.install = self.root / "install"
        self.config = AppDockConfig.from_environment(data_dir=self.data)
        self.config.ensure()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def release_members(self, marker: bytes = b"release") -> dict[str, bytes]:
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
                {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
                for path, content in sorted(members.items())
            ],
        }
        members[appdock.RELEASE_MANIFEST_NAME] = json.dumps(manifest, sort_keys=True).encode()
        return members

    def write_release(self, root: Path, marker: bytes = b"release") -> None:
        for relative, content in self.release_members(marker).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def staged_record(self, staged: Path, version: str = "0.4.0") -> dict[str, object]:
        identity = appdock._staged_identity(staged, zip_sha256="a" * 64)
        return {
            "staged": True,
            "version": version,
            "path": str(staged),
            "digest": appdock._staged_record_digest(version, identity),
            "identity": identity,
        }

    def test_forged_journal_candidate_and_backup_cannot_delete_external_sentinels(self) -> None:
        self.write_release(self.install, b"new")
        external_candidate = self.root / "external-candidate"
        external_backup = self.root / "external-backup"
        self.write_release(external_candidate, b"new")
        external_backup.mkdir()
        (external_backup / "sentinel.txt").write_text("keep", encoding="utf-8")
        operation_id = "a" * 32
        tx_root = self.data / "updates" / "transactions" / operation_id
        tx_root.mkdir(parents=True)
        identity = appdock._staged_identity(self.install, zip_sha256="a" * 64)
        journal = {
            "schema_version": 2,
            "operation_id": operation_id,
            "install": str(self.install),
            "candidate": str(external_candidate),
            "backup": str(external_backup),
            "old_exists": True,
            "phase": "committed",
            "recovery": "finish-new",
            "files": sorted(identity_record["path"] for identity_record in identity["inventory"]),
            "preexisting": sorted(identity_record["path"] for identity_record in identity["inventory"]),
            "identity": identity,
        }
        appdock._durable_write_json(tx_root / "transaction.json", journal)

        with self.assertRaises(AppDockError):
            appdock.recover_update_transactions(self.data, expected_install=self.install)

        self.assertTrue(external_candidate.is_dir())
        self.assertEqual((external_backup / "sentinel.txt").read_text(encoding="utf-8"), "keep")

    def test_hardlinked_journal_is_rejected_before_recovery(self) -> None:
        self.write_release(self.install, b"new")
        operation_id = "b" * 32
        tx_root = self.data / "updates" / "transactions" / operation_id
        tx_root.mkdir(parents=True)
        journal_path = tx_root / "transaction.json"
        external_journal = self.root / "journal-outside.json"
        external_journal.write_text("{}", encoding="utf-8")
        os.link(external_journal, journal_path)

        with self.assertRaises(AppDockError):
            appdock.recover_update_transactions(self.data, expected_install=self.install)
        self.assertEqual(external_journal.read_text(encoding="utf-8"), "{}")

    def test_complete_false_installed_inventory_rejects_hardlinked_managed_member(self) -> None:
        self.write_release(self.install)
        sentinel = self.root / "external-app-js.txt"
        sentinel.write_bytes((self.install / "static" / "app.js").read_bytes())
        managed = self.install / "static" / "app.js"
        managed.unlink()
        os.link(sentinel, managed)

        with self.assertRaises(AppDockError):
            appdock._validate_installed_tree(self.install)
        self.assertEqual(sentinel.read_bytes(), b"js-release")

    def test_release_manifest_and_source_reader_reject_hardlink_member(self) -> None:
        staged = self.data / "updates" / "0.4.0"
        self.write_release(staged)
        sentinel = self.root / "external-helper.py"
        sentinel.write_bytes((staged / "scripts" / "update_helper.py").read_bytes())
        helper = staged / "scripts" / "update_helper.py"
        helper.unlink()
        os.link(sentinel, helper)

        with self.assertRaises(AppDockError):
            appdock._load_release_inventory(staged, complete=False)
        self.assertEqual(sentinel.read_bytes(), b"helper-release")

    def test_post_wait_identity_failure_restores_existing_service_and_retains_stage(self) -> None:
        self.write_release(self.install, b"old")
        staged = self.data / "updates" / "0.4.0"
        self.write_release(staged, b"new")
        receipt = self.staged_record(staged)
        appdock._write_staged_receipt(self.config, receipt)
        launched: list[tuple[Path, Path, list[str], Path | None]] = []

        def restore(restart_script, install, restart_args, *, startup_data=None, ready_token=None, use_startup_handoff=True):
            self.assertFalse(use_startup_handoff)
            launched.append((restart_script, install, restart_args, startup_data))
            return SimpleNamespace(poll=lambda: None)

        with patch("scripts.update_helper._alive", return_value=False), patch(
            "scripts.update_helper._verify_helper_identity",
            side_effect=AppDockError("tampered after parent exit"),
        ), patch("scripts.update_helper._launch_and_wait", side_effect=restore):
            result = update_helper.run(
                staged,
                self.install,
                self.data,
                123,
                self.install / "appdock.py",
                ["--host", "127.0.0.1", "--port", "8876", "--data-dir", str(self.data)],
                expected_version=receipt["version"],
                expected_digest=receipt["digest"],
                expected_zip_sha256=receipt["identity"]["zip_sha256"],
                expected_inventory_sha256=receipt["identity"]["inventory_sha256"],
                expected_helper_sha256=receipt["identity"]["helper_sha256"],
            )

        self.assertEqual(result, 1)
        self.assertEqual((self.install / "appdock.py").read_bytes(), b"old")
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0][1], self.install)
        self.assertEqual(launched[0][3], self.data)
        self.assertTrue(staged.is_dir())
        self.assertTrue(appdock._staged_receipt_path(self.config).is_file())

    def test_post_wait_identity_failure_reports_restore_launch_failure(self) -> None:
        self.write_release(self.install, b"old")
        staged = self.data / "updates" / "0.4.1"
        self.write_release(staged, b"new")
        receipt = self.staged_record(staged, version="0.4.1")
        appdock._write_staged_receipt(self.config, receipt)

        with patch("scripts.update_helper._alive", return_value=False), patch(
            "scripts.update_helper._verify_helper_identity",
            side_effect=AppDockError("tampered after parent exit"),
        ), patch(
            "scripts.update_helper._launch_and_wait",
            side_effect=RuntimeError("old service did not become ready"),
        ):
            result = update_helper.run(
                staged,
                self.install,
                self.data,
                123,
                self.install / "appdock.py",
                [],
                expected_version=receipt["version"],
                expected_digest=receipt["digest"],
                expected_zip_sha256=receipt["identity"]["zip_sha256"],
                expected_inventory_sha256=receipt["identity"]["inventory_sha256"],
                expected_helper_sha256=receipt["identity"]["helper_sha256"],
            )

        self.assertEqual(result, 1)
        self.assertTrue(staged.is_dir())
        log = (self.data / "runtime" / "update.log").read_text(encoding="utf-8")
        self.assertIn("existing AppDock could not be restored before apply", log)


if __name__ == "__main__":
    unittest.main()
