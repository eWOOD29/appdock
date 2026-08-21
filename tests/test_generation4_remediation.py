from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import appdock
from scripts import update_helper


class _FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _write_release(root: Path, marker: bytes) -> None:
    members = {
        "appdock.py": marker,
        "static/app.js": b"js" + marker,
        "static/app.css": b"css" + marker,
        "scripts/update_helper.py": b"helper" + marker,
        "scripts/path_safety.ps1": b"safety" + marker,
        "scripts/install.ps1": b"install" + marker,
        "scripts/uninstall.ps1": b"uninstall" + marker,
    }
    for relative, payload in members.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    manifest = {
        "schema_version": 2,
        "files": [
            {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
            for relative, payload in sorted(members.items())
        ],
    }
    (root / appdock.RELEASE_MANIFEST_NAME).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _same_path(left: str | Path, right: str | Path) -> bool:
    return appdock._lexical_path_key(left) == appdock._lexical_path_key(right)


def _rollback_fixture(root: Path, operation_id: str, *, legacy: bool = False) -> tuple[Path, Path, Path, dict[str, Path], appdock.UpdateApplyError]:
    data = root / "data"
    install = root / "install"
    staged = data / "updates" / "0.4.0"
    data.mkdir(parents=True)
    (data / "runtime").mkdir()
    _write_release(install, b"old")
    _write_release(staged, b"new")
    paths = appdock._transaction_paths(install, data, operation_id)
    paths["tx_root"].mkdir(parents=True)
    old_identity = appdock._installed_tree_identity(install)
    target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
    journal = {
        "schema_version": 2, "operation_id": operation_id,
        "install": str(install), "candidate": str(paths["candidate"]),
        "backup": str(paths["backup"]), "old_exists": True,
        "phase": "rolled_back", "recovery": "restore-old", "files": [],
        "preexisting": [], "identity": target_identity,
    }
    if not legacy:
        journal["old_identity"] = old_identity
    appdock._durable_write_json(paths["journal"], journal)
    failure = appdock.UpdateApplyError("update failed and was rolled back", operation_id=operation_id, recovery_outcome="rolled_back")
    return data, install, staged, paths, failure


class Generation4RecoveryRemediationTests(unittest.TestCase):
    def test_windows_short_and_long_aliases_share_transaction_path_key(self):
        if os.name != "nt":
            self.skipTest("Windows path alias semantics")
        self.assertEqual(
            appdock._lexical_path_key(r"C:\\Program Files"),
            appdock._lexical_path_key(r"C:\\PROGRA~1"),
        )

    def test_restore_promotion_flush_failure_leaves_retryable_install_quarantine_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"new")
            _write_release(staged, b"new")
            operation_id = "e" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            _write_release(paths["backup"], b"old")
            old_identity = appdock._installed_tree_identity(paths["backup"])
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2, "operation_id": operation_id,
                "install": str(install), "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]), "old_exists": True,
                "phase": "swapping", "recovery": "restore-old", "files": [],
                "preexisting": [], "identity": target_identity, "old_identity": old_identity,
            })
            calls = {"install_parent": 0}

            def fail_after_promotion(path, *, strict=False):
                path = Path(path)
                if _same_path(path, install.parent):
                    calls["install_parent"] += 1
                    if calls["install_parent"] >= 2:
                        raise OSError("restore promotion parent flush failed")

            with patch.object(appdock, "_fsync_directory", side_effect=fail_after_promotion):
                with self.assertRaisesRegex(OSError, "restore promotion parent flush failed"):
                    appdock._recover_one_update(paths["tx_root"], install=install, data=data)
            self.assertTrue(install.is_dir())
            self.assertTrue(paths["restore_quarantine"].is_dir())
            self.assertEqual((install / "appdock.py").read_bytes(), b"old")
            self.assertEqual(json.loads(paths["journal"].read_text())["phase"], "swapping")

            self.assertEqual(appdock._recover_one_update(paths["tx_root"], install=install, data=data), "rolled_back")
            self.assertEqual((install / "appdock.py").read_bytes(), b"old")
            self.assertFalse(paths["restore_quarantine"].exists())

    def test_terminal_journal_replace_flush_failure_remains_retryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"new")
            _write_release(staged, b"new")
            operation_id = "f" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            _write_release(paths["backup"], b"old")
            old_identity = appdock._installed_tree_identity(paths["backup"])
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2, "operation_id": operation_id,
                "install": str(install), "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]), "old_exists": True,
                "phase": "swapping", "recovery": "restore-old", "files": [],
                "preexisting": [], "identity": target_identity, "old_identity": old_identity,
            })
            original_strict = appdock._durable_write_json_strict
            def replace_then_flush_fail(path, payload):
                if payload.get("phase") == "rolled_back":
                    original_fsync = appdock._fsync_directory
                    with patch.object(appdock, "_fsync_directory", side_effect=lambda target, *, strict=False: (_ for _ in ()).throw(OSError("journal parent flush failed")) if Path(target) == paths["tx_root"] else original_fsync(target, strict=strict)):
                        return original_strict(path, payload)
                return original_strict(path, payload)

            with patch.object(appdock, "_durable_write_json_strict", side_effect=replace_then_flush_fail):
                with self.assertRaisesRegex(OSError, "journal parent flush failed"):
                    appdock._recover_one_update(paths["tx_root"], install=install, data=data)
            self.assertEqual(json.loads(paths["journal"].read_text())["phase"], "swapping")
            self.assertTrue(paths["backup"].is_dir())
            self.assertTrue(paths["restore_quarantine"].is_dir())

            self.assertEqual(appdock._recover_one_update(paths["tx_root"], install=install, data=data), "rolled_back")
            self.assertEqual((install / "appdock.py").read_bytes(), b"old")

    def test_parent_rejects_arbitrary_restart_script_before_handshake_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            staged = data / "updates" / "0.4.0"
            install = root / "install"
            data.mkdir(parents=True)
            _write_release(staged, b"new")
            _write_release(install, b"old")
            arbitrary = root / "arbitrary.py"
            arbitrary.write_text("raise SystemExit(0)\n", encoding="utf-8")
            with self.assertRaisesRegex(appdock.AppDockError, "restart script"):
                appdock.launch_update_helper(
                    staged, install, data,
                    restart_command=[os.fspath(__import__("sys").executable), str(arbitrary)],
                    popen=lambda *_args, **_kwargs: self.fail("Popen must not be reached"),
                )
            self.assertFalse((data / "runtime").exists())

    def test_staging_publishes_directory_through_bound_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            members = {name: name.encode() for name in sorted(appdock.REQUIRED_RELEASE_FILES)}
            manifest = {"schema_version": 2, "files": [{"path": name, "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in sorted(members.items())]}
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, value in members.items():
                    archive.writestr(name, value)
                archive.writestr(appdock.RELEASE_MANIFEST_NAME, json.dumps(manifest, sort_keys=True))
            zip_bytes = payload.getvalue()
            sums = f"{hashlib.sha256(zip_bytes).hexdigest()}  appdock-windows.zip\n"
            class Response:
                def __init__(self, body): self.body = body
                def read(self, *_args): return self.body
                def __enter__(self): return self
                def __exit__(self, *_args): return None
            def opener(request, timeout=0):
                return Response(zip_bytes if request.full_url.endswith(".zip") else sums.encode())
            with patch.object(appdock, "_bound_directory_move", wraps=appdock._bound_directory_move) as move:
                appdock.stage_update({"version": "0.4.0", "assets": [{"name": "appdock-windows.zip", "browser_download_url": "https://github.com/eWOOD29/appdock/releases/download/v0.4.0/appdock-windows.zip"}, {"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/eWOOD29/appdock/releases/download/v0.4.0/SHA256SUMS.txt"}]}, config, opener=opener)
            move.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows directory flush contract")
    def test_strict_directory_flush_requests_write_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel32 = MagicMock()
            kernel32.CreateFileW.return_value = 123
            kernel32.FlushFileBuffers.return_value = 1
            kernel32.CloseHandle.return_value = 1
            with patch("ctypes.WinDLL", return_value=kernel32):
                appdock._fsync_directory(root, strict=True)
            self.assertEqual(kernel32.CreateFileW.call_args.args[1], 0x40000000)

    @unittest.skipUnless(os.name == "nt", "Windows directory flush contract")
    def test_strict_directory_flush_failure_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kernel32 = MagicMock()
            kernel32.CreateFileW.return_value = 123
            kernel32.FlushFileBuffers.return_value = 0
            kernel32.CloseHandle.return_value = 1
            with (
                patch("ctypes.WinDLL", return_value=kernel32),
                patch("ctypes.get_last_error", return_value=5),
                self.assertRaises(OSError),
            ):
                appdock._fsync_directory(root, strict=True)

    def test_restore_old_rejects_valid_backup_with_wrong_recorded_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            recorded_old = root / "recorded-old"
            data.mkdir(parents=True)
            _write_release(install, b"new")
            _write_release(staged, b"new")
            _write_release(recorded_old, b"old-a")
            operation_id = "f" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            _write_release(paths["backup"], b"old-b")
            paths["tx_root"].mkdir(parents=True, exist_ok=True)
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            old_identity = appdock._installed_tree_identity(recorded_old)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2,
                "operation_id": operation_id,
                "install": str(install),
                "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]),
                "old_exists": True,
                "phase": "swapping",
                "recovery": "restore-old",
                "files": sorted(item["path"] for item in target_identity["inventory"]),
                "preexisting": sorted(item["path"] for item in old_identity["inventory"]),
                "identity": target_identity,
                "old_identity": old_identity,
            })
            with self.assertRaisesRegex(appdock.AppDockError, "recorded rollback identity"):
                appdock._recover_one_update(paths["tx_root"], install=install, data=data)
            self.assertEqual((install / "appdock.py").read_bytes(), b"new")
            self.assertEqual((paths["backup"] / "appdock.py").read_bytes(), b"old-b")
            self.assertEqual(json.loads(paths["journal"].read_text())["phase"], "swapping")

    def test_terminal_rolled_back_rejects_mismatched_install_without_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"wrong")
            _write_release(staged, b"new")
            operation_id = "d" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            _write_release(paths["backup"], b"old")
            paths["tx_root"].mkdir(parents=True, exist_ok=True)
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            old_identity = appdock._installed_tree_identity(paths["backup"])
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2,
                "operation_id": operation_id,
                "install": str(install),
                "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]),
                "old_exists": True,
                "phase": "rolled_back",
                "recovery": "restore-old",
                "files": sorted(item["path"] for item in target_identity["inventory"]),
                "preexisting": sorted(item["path"] for item in old_identity["inventory"]),
                "identity": target_identity,
                "old_identity": old_identity,
            })
            with self.assertRaisesRegex(appdock.AppDockError, "recorded rollback identity"):
                appdock._recover_one_update(paths["tx_root"], install=install, data=data)
            self.assertTrue(paths["backup"].is_dir())
            self.assertEqual((paths["backup"] / "appdock.py").read_bytes(), b"old")
            self.assertEqual((install / "appdock.py").read_bytes(), b"wrong")

    def test_legacy_terminal_rolled_back_preserves_unverifiable_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"old")
            _write_release(staged, b"new")
            operation_id = "e" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            _write_release(paths["backup"], b"old")
            paths["tx_root"].mkdir(parents=True, exist_ok=True)
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2,
                "operation_id": operation_id,
                "install": str(install),
                "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]),
                "old_exists": True,
                "phase": "rolled_back",
                "recovery": "restore-old",
                "files": sorted(item["path"] for item in target_identity["inventory"]),
                "preexisting": [],
                "identity": target_identity,
            })
            self.assertEqual(
                appdock._recover_one_update(paths["tx_root"], install=install, data=data),
                "rolled_back",
            )
            self.assertTrue(paths["backup"].is_dir())
            self.assertTrue(paths["tx_root"].is_dir())

    def test_phase_write_failure_does_not_mutate_journal_in_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tx_root = root / "tx"
            tx_root.mkdir()
            journal = {"phase": "swapping", "recovery": "restore-old"}
            with patch.object(appdock, "_durable_write_json", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    appdock._set_update_phase(tx_root, journal, "committed", "finish-new")
            self.assertEqual(journal, {"phase": "swapping", "recovery": "restore-old"})

    def test_terminal_phase_requests_strict_journal_directory_durability(self):
        with tempfile.TemporaryDirectory() as temporary:
            tx_root = Path(temporary) / "tx"
            tx_root.mkdir()
            journal = {"phase": "swapping", "recovery": "restore-old"}
            with patch.object(appdock, "_durable_write_json_strict") as write_json:
                appdock._set_update_phase(tx_root, journal, "rolled_back", "restore-old", strict=True)
            write_json.assert_called_once_with(tx_root / "transaction.json", {**journal, "phase": "rolled_back", "recovery": "restore-old"})

    def test_directory_flush_failure_through_restore_preserves_retry_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"old")
            _write_release(staged, b"new")
            operation_id = "1" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            old_identity = appdock._installed_tree_identity(install)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2, "operation_id": operation_id,
                "install": str(install), "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]), "old_exists": True,
                "phase": "swapping", "recovery": "restore-old", "files": [],
                "preexisting": [], "identity": identity, "old_identity": old_identity,
            })
            _write_release(paths["backup"], b"old")
            original = appdock._fsync_directory

            def fail_strict(path, *, strict=False):
                if strict:
                    raise OSError("directory flush failed")
                return original(path, strict=strict)

            with patch.object(appdock, "_fsync_directory", side_effect=fail_strict):
                with self.assertRaises(OSError):
                    appdock._recover_one_update(paths["tx_root"], install=install, data=data)
            self.assertTrue(paths["backup"].is_dir())
            self.assertTrue(paths["tx_root"].is_dir())
            self.assertIn(json.loads(paths["journal"].read_text())["phase"], {"swapping", "rolled_back"})

    @unittest.skipUnless(os.name == "nt", "Windows directory ancestry lease")
    def test_windows_ancestry_lease_closes_each_native_handle_once(self):
        import ctypes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "parent" / "child"
            child.mkdir(parents=True)
            kernel32 = MagicMock()
            kernel32.CreateFileW.side_effect = list(range(100, 100 + len(child.parents)))
            kernel32.CloseHandle.return_value = 1

            def mark_directory(_handle, _info_class, info_pointer, _size):
                info_pointer._obj.FileAttributes = 0x10
                info_pointer._obj.ReparseTag = 0
                return 1

            kernel32.GetFileInformationByHandleEx.side_effect = mark_directory
            with patch.object(ctypes, "WinDLL", return_value=kernel32):
                with appdock._directory_ancestry_lease(child):
                    pass

            opened = [call.args[0] for call in kernel32.CreateFileW.call_args_list]
            closed = [call.args[0] for call in kernel32.CloseHandle.call_args_list]
            self.assertEqual(len(closed), len(opened))
            self.assertEqual(closed, list(reversed(list(range(100, 100 + len(opened))))))

    @unittest.skipUnless(os.name == "nt", "Windows directory ancestry lease")
    def test_windows_ancestry_lease_denies_parent_rebind_but_allows_child_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "parent"
            source = parent / "source"
            destination = parent / "destination"
            source.mkdir(parents=True)
            destination.mkdir()
            (source / "child.txt").write_bytes(b"child")
            with appdock._directory_ancestry_lease(source, destination):
                with self.assertRaises(OSError) as denied:
                    os.replace(parent, root / "rebound")
                self.assertEqual(getattr(denied.exception, "winerror", None), 32)
                os.replace(source / "child.txt", destination / "child.txt")
                self.assertEqual((destination / "child.txt").read_bytes(), b"child")

    @unittest.skipUnless(os.name == "nt", "Windows handle-bound directory rename")
    def test_windows_bound_directory_move_blocks_source_rebind_but_renames_exact_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            rebound = root / "rebound"
            source.mkdir()
            destination.parent.mkdir(exist_ok=True)
            (source / "marker.txt").write_bytes(b"exact-source")
            rebind_error = []

            def validate_after_open():
                try:
                    os.replace(source, rebound)
                except OSError as exc:
                    rebind_error.append(exc)

            appdock._bound_directory_move(source, destination, validator=validate_after_open)
            self.assertEqual([getattr(rebind_error[0], "winerror", None)], [32])
            self.assertFalse(source.exists())
            self.assertEqual((destination / "marker.txt").read_bytes(), b"exact-source")
            self.assertFalse(rebound.exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle-bound directory rename")
    def test_windows_bound_directory_move_blocks_destination_parent_rebind(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_parent = root / "source-parent"
            destination_parent = root / "destination-parent"
            source = source_parent / "source"
            destination = destination_parent / "destination"
            rebound = root / "rebound-destination-parent"
            source.mkdir(parents=True)
            destination_parent.mkdir()
            (source / "marker.txt").write_bytes(b"exact-source")
            rebind_error = []

            def validate_after_open():
                try:
                    os.replace(destination_parent, rebound)
                except OSError as exc:
                    rebind_error.append(exc)

            appdock._bound_directory_move(source, destination, validator=validate_after_open)
            self.assertEqual([getattr(rebind_error[0], "winerror", None)], [32])
            self.assertEqual((destination / "marker.txt").read_bytes(), b"exact-source")
            self.assertFalse(rebound.exists())

    @unittest.skipUnless(os.name == "nt", "Windows retirement handle binding")
    def test_windows_retirement_binds_source_at_final_validation_seam(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, install, staged, paths, _failure = _rollback_fixture(Path(temporary), "3" * 32)
            rebound = paths["tx_root"].parent / "rebound-tx"
            rebind_error = []

            def final_validation_seam():
                try:
                    os.replace(paths["tx_root"], rebound)
                except OSError as exc:
                    rebind_error.append(exc)

            self.assertTrue(
                appdock._retire_authorized_rollback_for_restart(
                    install,
                    data,
                    "3" * 32,
                    "rolled_back",
                    validation_hook=final_validation_seam,
                )
            )
            self.assertEqual([getattr(rebind_error[0], "winerror", None)], [32])
            self.assertFalse(paths["tx_root"].exists())
            archive = data / "updates" / "history" / ("3" * 32)
            self.assertTrue((archive / "transaction.json").is_file())
            self.assertEqual(json.loads((archive / "transaction.json").read_text())["phase"], "rolled_back")
            self.assertFalse(rebound.exists())

    def test_apply_swaps_install_and_candidate_through_bound_directory_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"old")
            _write_release(staged, b"new")
            with patch.object(appdock, "_bound_directory_move", wraps=appdock._bound_directory_move) as move:
                result = appdock.apply_update(staged, install, data)
            self.assertEqual(
                [(Path(call.args[0]).name, Path(call.args[1]).name) for call in move.call_args_list],
                [("install", Path(result["backup"]).name), (Path(result["backup"]).with_suffix(".candidate").name, "install")],
            )
            self.assertEqual((install / "appdock.py").read_bytes(), b"new")
            self.assertEqual((Path(result["backup"]) / "appdock.py").read_bytes(), b"old")

    def test_finish_new_promotes_bound_candidate_and_commits_before_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(staged, b"new")
            operation_id = "4" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            _write_release(paths["candidate"], b"new")
            _write_release(paths["backup"], b"old")
            paths["evidence_backup"].mkdir(parents=True)
            (paths["evidence_backup"] / "sentinel.txt").write_text("keep", encoding="utf-8")
            identity = appdock._staged_identity(paths["candidate"], zip_sha256=None, complete=False)
            journal = {
                "schema_version": 2, "operation_id": operation_id,
                "install": str(install), "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]), "old_exists": True,
                "phase": "committed", "recovery": "finish-new", "files": [],
                "preexisting": [], "identity": identity,
            }
            appdock._durable_write_json(paths["journal"], journal)
            observed = {}
            original_set_phase = appdock._set_update_phase

            def record_terminal_state(tx_root, current_journal, phase, recovery, **kwargs):
                observed.update({
                    "backup": paths["backup"].exists(),
                    "candidate": paths["candidate"].exists(),
                    "evidence": paths["evidence_backup"].exists(),
                    "strict": kwargs.get("strict"),
                })
                return original_set_phase(tx_root, current_journal, phase, recovery, **kwargs)

            with patch.object(appdock, "_set_update_phase", side_effect=record_terminal_state) as set_phase, patch.object(
                appdock, "_bound_directory_move", wraps=appdock._bound_directory_move
            ) as move:
                self.assertEqual(
                    appdock._recover_one_update(paths["tx_root"], install=install, data=data),
                    "complete",
                )
            self.assertEqual(
                [(appdock._lexical_path_key(call.args[0]), appdock._lexical_path_key(call.args[1])) for call in move.call_args_list],
                [(appdock._lexical_path_key(paths["candidate"]), appdock._lexical_path_key(install))],
            )
            self.assertEqual(observed, {"backup": True, "candidate": False, "evidence": True, "strict": True})
            set_phase.assert_called_once()
            self.assertTrue(install.is_dir())
            self.assertFalse(paths["backup"].exists())
            self.assertFalse(paths["evidence_backup"].exists())

    def test_finish_new_strict_terminal_failure_preserves_sources_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(staged, b"new")
            operation_id = "5" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            _write_release(paths["candidate"], b"new")
            _write_release(paths["backup"], b"old")
            paths["evidence_backup"].mkdir(parents=True)
            (paths["evidence_backup"] / "sentinel.txt").write_text("keep", encoding="utf-8")
            identity = appdock._staged_identity(paths["candidate"], zip_sha256=None, complete=False)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2, "operation_id": operation_id,
                "install": str(install), "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]), "old_exists": True,
                "phase": "committed", "recovery": "finish-new", "files": [],
                "preexisting": [], "identity": identity,
            })
            with patch.object(appdock, "_durable_write_json_strict", side_effect=OSError("complete flush failed")):
                with self.assertRaisesRegex(OSError, "complete flush failed"):
                    appdock._recover_one_update(paths["tx_root"], install=install, data=data)
            self.assertTrue(install.is_dir())
            self.assertTrue(paths["backup"].is_dir())
            self.assertTrue(paths["evidence_backup"].is_dir())
            self.assertFalse(paths["candidate"].exists())
            self.assertEqual(json.loads(paths["journal"].read_text())["phase"], "committed")
            self.assertEqual(appdock._recover_one_update(paths["tx_root"], install=install, data=data), "complete")
            self.assertFalse(paths["backup"].exists())
            self.assertFalse(paths["evidence_backup"].exists())

    def test_direct_helper_rejects_arbitrary_restart_before_handshake_or_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            install.mkdir()
            (install / "appdock.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            arbitrary = root / "restart.py"
            arbitrary.write_text("raise SystemExit(0)\n", encoding="utf-8")
            data = root / "data"
            staged = root / "staged"
            staged.mkdir()
            handshake = data / "runtime" / ("update-helper-" + "a" * 32 + ".ready")
            args = [
                "--staged", str(staged), "--install", str(install), "--data", str(data),
                "--pid", "0", "--restart-script", str(arbitrary), "--handshake", str(handshake),
                "--handshake-token", "helper-token-1234567890123456",
                "--expected-version", "0.2.2-beta.3", "--expected-digest", "a" * 64,
                "--expected-zip-sha256", "b" * 64, "--expected-inventory-sha256", "c" * 64,
                "--expected-helper-sha256", "d" * 64,
            ]
            with patch.object(update_helper, "run") as run:
                with self.assertRaisesRegex(appdock.AppDockError, "restart script"):
                    update_helper.main(args)
            run.assert_not_called()
            self.assertFalse(data.exists())

    def test_direct_helper_accepts_exact_install_appdock_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            install.mkdir()
            (install / "appdock.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            data = root / "data"
            staged = root / "staged"
            staged.mkdir()
            handshake = data / "runtime" / ("update-helper-" + "b" * 32 + ".ready")
            args = [
                "--staged", str(staged), "--install", str(install), "--data", str(data),
                "--pid", "0", "--restart-script", str(install / "appdock.py"), "--handshake", str(handshake),
                "--handshake-token", "helper-token-1234567890123456",
                "--expected-version", "0.2.2-beta.3", "--expected-digest", "a" * 64,
                "--expected-zip-sha256", "b" * 64, "--expected-inventory-sha256", "c" * 64,
                "--expected-helper-sha256", "d" * 64,
            ]
            with patch.object(update_helper, "run", return_value=0) as run:
                self.assertEqual(update_helper.main(args), 0)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[4], install / "appdock.py")

    def test_bound_directory_move_flushes_both_parents_before_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "marker.txt").write_bytes(b"exact-source")
            flushes = []
            with patch.object(appdock, "_fsync_directory", side_effect=lambda path, *, strict=False: flushes.append((Path(path), strict))):
                appdock._bound_directory_move(source, destination)
            self.assertEqual(
                flushes,
                [(source.parent, True), (destination.parent, True)],
            )

    def test_bound_directory_move_strict_flush_failure_is_not_authorized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "marker.txt").write_bytes(b"exact-source")
            with patch.object(appdock, "_fsync_directory", side_effect=OSError("parent flush failed")):
                with self.assertRaisesRegex(OSError, "parent flush failed"):
                    appdock._bound_directory_move(source, destination)
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_dir())

    def test_restore_old_promotion_failure_preserves_quarantine_and_retries_exact_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"new")
            _write_release(staged, b"new")
            operation_id = "2" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            _write_release(paths["backup"], b"old")
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            old_identity = appdock._installed_tree_identity(paths["backup"])
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2, "operation_id": operation_id,
                "install": str(install), "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]), "old_exists": True,
                "phase": "swapping", "recovery": "restore-old", "files": [],
                "preexisting": [], "identity": target_identity, "old_identity": old_identity,
            })

            def fail_promotion(phase):
                if phase == "before-promote-restore":
                    raise RuntimeError("injected restore promotion failure")

            with self.assertRaisesRegex(RuntimeError, "promotion failure"):
                appdock._recover_one_update(paths["tx_root"], install=install, data=data, phase_hook=fail_promotion)
            self.assertFalse(install.exists())
            self.assertTrue(paths["restore_quarantine"].is_dir())
            self.assertTrue(paths["restore_temp"].is_dir())
            self.assertTrue(paths["backup"].is_dir())
            self.assertEqual(json.loads(paths["journal"].read_text())["phase"], "swapping")

            self.assertEqual(
                appdock._recover_one_update(paths["tx_root"], install=install, data=data),
                "rolled_back",
            )
            self.assertTrue(install.is_dir())
            self.assertEqual((install / "appdock.py").read_bytes(), b"old")
            self.assertFalse(paths["restore_quarantine"].exists())
            self.assertEqual(json.loads(paths["journal"].read_text())["phase"], "rolled_back")

    def test_empty_backup_is_rejected_before_install_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"new")
            _write_release(staged, b"new")
            operation_id = "a" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["backup"].mkdir(parents=True)
            paths["tx_root"].mkdir(parents=True)
            identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            journal = {
                "schema_version": 2,
                "operation_id": operation_id,
                "install": str(install),
                "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]),
                "old_exists": True,
                "phase": "swapping",
                "recovery": "restore-old",
                "files": sorted(item["path"] for item in identity["inventory"]),
                "preexisting": sorted(item["path"] for item in identity["inventory"]),
                "identity": identity,
            }
            appdock._durable_write_json(paths["journal"], journal)
            with self.assertRaises(appdock.AppDockError):
                appdock._recover_one_update(paths["tx_root"], install=install, data=data)
            self.assertTrue(paths["backup"].is_dir())
            self.assertEqual((install / "appdock.py").read_bytes(), b"new")
            self.assertEqual(json.loads(paths["journal"].read_text())["phase"], "swapping")

    def test_rollback_journal_is_durable_before_cleanup_and_retry_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"old")
            _write_release(staged, b"new")
            original_write = appdock._durable_write_json_strict
            failed = {"done": False}

            def fail_rollback_commit(path, payload):
                if Path(path).name == "transaction.json" and payload.get("phase") == "rolled_back" and not failed["done"]:
                    failed["done"] = True
                    raise OSError("final journal write failed")
                return original_write(path, payload)

            with patch.object(appdock, "_durable_write_json_strict", side_effect=fail_rollback_commit):
                with self.assertRaisesRegex(appdock.AppDockError, "recovery did not complete"):
                    appdock.apply_update(staged, install, data, phase_hook=lambda phase: (_ for _ in ()).throw(RuntimeError("activate failed")) if phase == "before-commit" else None)
            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            paths = appdock._transaction_paths(install, data, json.loads(transaction.read_text())["operation_id"])
            self.assertEqual(json.loads(transaction.read_text())["phase"], "swapping")
            self.assertTrue(paths["backup"].is_dir())
            self.assertTrue(paths["evidence_backup"].is_dir())
            self.assertFalse(install.exists())
            self.assertTrue(paths["restore_quarantine"].is_dir())
            self.assertTrue(paths["restore_temp"].is_dir())
            self.assertEqual(json.loads(transaction.read_text())["phase"], "swapping")
            self.assertEqual(appdock._recover_one_update(paths["tx_root"], install=install, data=data), "rolled_back")
            self.assertEqual((install / "appdock.py").read_bytes(), b"old")

    def test_finalize_failure_evidence_does_not_claim_rollback_before_recovery_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"old")
            _write_release(staged, b"new")
            applied = appdock.apply_update(staged, install, data)
            transaction = Path(applied["transaction"])

            with (
                patch.object(appdock, "_recover_one_update", side_effect=appdock.AppDockError("rollback failed")),
                self.assertRaisesRegex(appdock.AppDockError, "rollback failed"),
            ):
                appdock.rollback_update(
                    applied,
                    install,
                    data,
                    failure=RuntimeError("forced finalize failure"),
                )

            self.assertFalse(transaction.with_name("failure.json").exists())

    def test_failure_evidence_uses_last_known_durable_phase_when_commit_reread_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            _write_release(install, b"old")
            _write_release(staged, b"new")
            original_write = appdock._durable_write_json

            def committed_write_then_fail(path, payload):
                result = original_write(path, payload)
                if Path(path).name == "transaction.json" and payload.get("phase") == "committed":
                    raise OSError("commit acknowledgement lost")
                return result

            with (
                patch.object(appdock, "_durable_write_json", side_effect=committed_write_then_fail),
                patch.object(appdock, "_update_journal", side_effect=OSError("journal reread lost")),
                self.assertRaisesRegex(appdock.AppDockError, "recovery did not complete"),
            ):
                appdock.apply_update(staged, install, data)
            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            evidence = json.loads(transaction.with_name("failure.json").read_text())
            self.assertEqual(evidence["phase"], "swapping")

    def test_helper_does_not_restart_for_unstructured_apply_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            (data / "runtime").mkdir()
            _write_release(install, b"old")
            _write_release(staged, b"new")
            with (
                patch.object(update_helper, "_alive", return_value=False),
                patch.object(update_helper, "_verify_helper_identity", return_value={"identity": "ok"}),
                patch.object(update_helper, "acquire_update_lock", return_value=_FakeLock()),
                patch.object(update_helper, "recover_update_transactions", return_value=[]),
                patch.object(update_helper, "apply_update", side_effect=RuntimeError("recovery failed")),
                patch.object(update_helper, "_launch_and_wait") as launch,
            ):
                result = update_helper.run(staged, install, data, 123, install / "appdock.py", [])
            self.assertEqual(result, 1)
            launch.assert_not_called()

    def test_helper_restarts_only_for_exact_durable_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            (data / "runtime").mkdir()
            _write_release(install, b"old")
            _write_release(staged, b"new")
            operation_id = "b" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            old_identity = appdock._installed_tree_identity(install)
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2,
                "operation_id": operation_id,
                "install": str(install),
                "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]),
                "old_exists": True,
                "phase": "rolled_back",
                "recovery": "restore-old",
                "files": sorted(item["path"] for item in target_identity["inventory"]),
                "preexisting": sorted(item["path"] for item in old_identity["inventory"]),
                "identity": target_identity,
                "old_identity": old_identity,
            })
            appdock._durable_write_json(paths["tx_root"] / "failure.json", {"schema_version": 1, "operation_id": operation_id, "evidence": "preserve"})
            failure = appdock.UpdateApplyError("update failed and was rolled back", operation_id=operation_id, recovery_outcome="rolled_back")
            with (
                patch.object(update_helper, "_alive", return_value=False),
                patch.object(update_helper, "_verify_helper_identity", return_value={"identity": "ok"}),
                patch.object(update_helper, "acquire_update_lock", return_value=_FakeLock()),
                patch.object(update_helper, "recover_update_transactions", return_value=[]),
                patch.object(update_helper, "apply_update", side_effect=failure),
                patch.object(update_helper, "_launch_and_wait", return_value=object()) as launch,
            ):
                result = update_helper.run(staged, install, data, 123, install / "appdock.py", [])
            self.assertEqual(result, 1)
            launch.assert_called_once()
            history_root = data / "updates" / "history" / operation_id
            self.assertFalse(paths["tx_root"].exists())
            self.assertTrue((history_root / "transaction.json").is_file())
            self.assertTrue((history_root / "failure.json").is_file())

    def test_helper_does_not_restart_when_rollback_retirement_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            (data / "runtime").mkdir()
            _write_release(install, b"old")
            _write_release(staged, b"new")
            operation_id = "c" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            paths["tx_root"].mkdir(parents=True)
            old_identity = appdock._installed_tree_identity(install)
            target_identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            appdock._durable_write_json(paths["journal"], {
                "schema_version": 2,
                "operation_id": operation_id,
                "install": str(install),
                "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]),
                "old_exists": True,
                "phase": "rolled_back",
                "recovery": "restore-old",
                "files": sorted(item["path"] for item in target_identity["inventory"]),
                "preexisting": sorted(item["path"] for item in old_identity["inventory"]),
                "identity": target_identity,
                "old_identity": old_identity,
            })
            failure = appdock.UpdateApplyError("update failed and was rolled back", operation_id=operation_id, recovery_outcome="rolled_back")
            with (
                patch.object(update_helper, "_alive", return_value=False),
                patch.object(update_helper, "_verify_helper_identity", return_value={"identity": "ok"}),
                patch.object(update_helper, "acquire_update_lock", return_value=_FakeLock()),
                patch.object(update_helper, "recover_update_transactions", return_value=[]),
                patch.object(update_helper, "apply_update", side_effect=failure),
                patch.object(appdock, "_bound_directory_move", side_effect=OSError("archive move failed")),
                patch.object(update_helper, "_launch_and_wait") as launch,
            ):
                result = update_helper.run(staged, install, data, 123, install / "appdock.py", [])
            self.assertEqual(result, 1)
            launch.assert_not_called()
            self.assertTrue(paths["tx_root"].exists())
            self.assertFalse((data / "updates" / "history" / operation_id).exists())

    def _run_retirement_case(self, data, install, staged, failure, *, extra=None, flush_failure=False):
        if extra is not None:
            extra()
        patches = [
            patch.object(update_helper, "_alive", return_value=False),
            patch.object(update_helper, "_verify_helper_identity", return_value={"identity": "ok"}),
            patch.object(update_helper, "acquire_update_lock", return_value=_FakeLock()),
            patch.object(update_helper, "recover_update_transactions", return_value=[]),
            patch.object(update_helper, "apply_update", side_effect=failure),
            patch.object(update_helper, "_launch_and_wait"),
        ]
        if flush_failure:
            patches.append(patch.object(appdock, "_fsync_directory", side_effect=OSError("history parent flush failed")))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as launch:
            if flush_failure:
                with patches[6]:
                    result = update_helper.run(staged, install, data, 123, install / "appdock.py", [])
            else:
                result = update_helper.run(staged, install, data, 123, install / "appdock.py", [])
        self.assertEqual(result, 1)
        launch.assert_not_called()

    def test_retirement_strict_parent_flush_failure_does_not_relaunch(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, install, staged, paths, failure = _rollback_fixture(Path(temporary), "8" * 32)
            self._run_retirement_case(data, install, staged, failure, flush_failure=True)
            self.assertTrue((data / "updates" / "history" / ("8" * 32)).is_dir())
            self.assertTrue((data / "updates" / "history" / ("8" * 32) / "transaction.json").is_file())

    def test_retirement_existing_history_destination_does_not_relaunch(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, install, staged, paths, failure = _rollback_fixture(Path(temporary), "9" * 32)
            destination = data / "updates" / "history" / ("9" * 32)
            destination.mkdir(parents=True)
            self._run_retirement_case(data, install, staged, failure)
            self.assertTrue(paths["tx_root"].is_dir())

    def test_stale_history_without_active_transaction_does_not_relaunch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            install = root / "install"
            staged = data / "updates" / "0.4.0"
            data.mkdir(parents=True)
            (data / "runtime").mkdir()
            _write_release(install, b"old")
            _write_release(staged, b"new")
            operation_id = "a" * 32
            archive = data / "updates" / "history" / operation_id
            archive.mkdir(parents=True)
            _write_release(archive, b"old")
            failure = appdock.UpdateApplyError("update failed and was rolled back", operation_id=operation_id, recovery_outcome="rolled_back")
            self._run_retirement_case(data, install, staged, failure)

    def test_legacy_rolled_back_journal_without_old_identity_does_not_relaunch(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, install, staged, paths, failure = _rollback_fixture(Path(temporary), "b" * 32, legacy=True)
            self._run_retirement_case(data, install, staged, failure)
            self.assertTrue(paths["tx_root"].is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows history reparse handling")
    def test_aliased_history_root_does_not_relaunch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, install, staged, paths, failure = _rollback_fixture(root, "c" * 32)
            real_history = root / "real-history"
            real_history.mkdir()
            history = data / "updates" / "history"
            try:
                os.symlink(real_history, history, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            self._run_retirement_case(data, install, staged, failure)
            self.assertTrue(paths["tx_root"].is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows history destination reparse handling")
    def test_aliased_history_destination_does_not_relaunch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, install, staged, paths, failure = _rollback_fixture(root, "d" * 32)
            real_destination = root / "real-destination"
            real_destination.mkdir()
            destination = data / "updates" / "history" / ("d" * 32)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(real_destination, destination, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            self._run_retirement_case(data, install, staged, failure)
            self.assertTrue(paths["tx_root"].is_dir())


if __name__ == "__main__":
    unittest.main()
