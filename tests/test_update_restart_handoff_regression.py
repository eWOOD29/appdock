from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import appdock
from scripts import update_helper


class UpdateRestartHandoffRegressionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process probing regression")
    def test_windows_process_exists_does_not_use_os_kill(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            with patch.object(appdock.os, "kill", side_effect=AssertionError("os.kill must not be used on Windows")):
                self.assertTrue(appdock._process_exists(process.pid))
        finally:
            process.terminate()
            process.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows startup handoff regression")
    def test_windows_startup_handoff_accepts_live_helper_owner(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                data = root / "data"
                install = root / "install"
                install.mkdir()
                token = "A" * 32
                receipt = appdock._update_startup_receipt_path(data, token)
                appdock._durable_write_json(
                    receipt,
                    {
                        "schema_version": 1,
                        "token": token,
                        "owner_pid": process.pid,
                        "install": str(install.resolve()),
                        "data": str(data.resolve()),
                    },
                )
                with patch.object(appdock.os, "kill", side_effect=AssertionError("os.kill must not be used on Windows")):
                    appdock._consume_update_startup_handoff(data, install, token)
                self.assertFalse(receipt.exists())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_rollback_restores_old_service_only_after_lock_release_without_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / "staged"
            install = root / "install"
            data = root / "data"
            restart_script = install / "appdock.py"
            for path in (staged, install, data / "runtime"):
                path.mkdir(parents=True, exist_ok=True)

            lock_state = {"active": False}
            launches: list[tuple[bool, bool]] = []

            class FakeLock:
                def __enter__(self):
                    if lock_state["active"]:
                        raise AssertionError("fake lock is already active")
                    lock_state["active"] = True
                    return self

                def __exit__(self, *_args):
                    lock_state["active"] = False

            def fake_launch(*_args, use_startup_handoff=True, **_kwargs):
                launches.append((use_startup_handoff, lock_state["active"]))
                if len(launches) == 1:
                    raise RuntimeError("candidate restart failed")
                return object()

            logs: list[str] = []
            with (
                patch.object(update_helper, "_update_lock_path", return_value=data / "runtime" / "update.lock"),
                patch.object(update_helper, "_safe_append_update_log", side_effect=lambda _path, message: logs.append(message)),
                patch.object(update_helper, "_validate_installed_tree", return_value=({}, [])),
                patch.object(update_helper, "_alive", return_value=False),
                patch.object(update_helper, "_verify_helper_identity", return_value={"identity": "ok"}),
                patch.object(update_helper, "acquire_update_lock", return_value=FakeLock()),
                patch.object(update_helper, "recover_update_transactions", return_value=[]),
                patch.object(update_helper, "apply_update", return_value={"files": ["appdock.py"], "transaction": "tx"}),
                patch.object(update_helper, "finalize_update"),
                patch.object(update_helper, "rollback_update") as rollback,
                patch.object(update_helper, "_discard_stage"),
                patch.object(update_helper, "_launch_and_wait", side_effect=fake_launch),
            ):
                result = update_helper.run(
                    staged,
                    install,
                    data,
                    12345,
                    restart_script,
                    ["--host", "127.0.0.1", "--port", "8765", "--data-dir", str(data)],
                )

            self.assertEqual(result, 1)
            rollback.assert_called_once()
            self.assertEqual(launches, [(True, True), (False, False)])
            self.assertTrue(any("previous program files restored" in item for item in logs))
            self.assertTrue(any("after updater lock release" in item for item in logs))

    def test_preapply_restore_uses_normal_startup_without_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(update_helper, "_launch_and_wait", return_value=object()) as launch:
                self.assertTrue(
                    update_helper._restore_existing_service_after_preapply_failure(
                        root / "appdock.py",
                        root,
                        root / "data",
                        ["--host", "127.0.0.1", "--port", "8765"],
                        lambda _message: None,
                    )
                )
            self.assertFalse(launch.call_args.kwargs["use_startup_handoff"])


    @unittest.skipUnless(os.name == "nt", "Windows retained process-handle regression")
    def test_windows_process_exists_rejects_exited_process_with_retained_handle(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        process.wait(timeout=5)
        self.assertFalse(appdock._process_exists(process.pid))

    @unittest.skipUnless(os.name == "nt", "Windows retained process-handle regression")
    def test_windows_startup_handoff_rejects_exited_owner_with_retained_handle(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        process.wait(timeout=5)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            install = root / "install"
            install.mkdir()
            token = "B" * 32
            receipt = appdock._update_startup_receipt_path(data, token)
            appdock._durable_write_json(
                receipt,
                {
                    "schema_version": 1,
                    "token": token,
                    "owner_pid": process.pid,
                    "install": str(install.resolve()),
                    "data": str(data.resolve()),
                },
            )
            with self.assertRaises(appdock.AppDockError):
                appdock._consume_update_startup_handoff(data, install, token)
            self.assertTrue(receipt.exists())

    def test_recovery_failure_never_restarts_untrusted_installation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / "staged"
            install = root / "install"
            data = root / "data"
            restart_script = install / "appdock.py"
            for item in (staged, install, data / "runtime"):
                item.mkdir(parents=True, exist_ok=True)

            class FakeLock:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

            logs: list[str] = []
            with (
                patch.object(update_helper, "_update_lock_path", return_value=data / "runtime" / "update.lock"),
                patch.object(update_helper, "_safe_append_update_log", side_effect=lambda _path, message: logs.append(message)),
                patch.object(update_helper, "_validate_installed_tree", return_value=({}, [])),
                patch.object(update_helper, "_alive", return_value=False),
                patch.object(update_helper, "_verify_helper_identity", return_value={"identity": "ok"}),
                patch.object(update_helper, "acquire_update_lock", return_value=FakeLock()),
                patch.object(update_helper, "recover_update_transactions", side_effect=appdock.AppDockError("recovery failed")),
                patch.object(update_helper, "_launch_and_wait") as launch,
            ):
                result = update_helper.run(
                    staged,
                    install,
                    data,
                    12345,
                    restart_script,
                    ["--host", "127.0.0.1", "--port", "8765", "--data-dir", str(data)],
                )

            self.assertEqual(result, 1)
            launch.assert_not_called()
            self.assertTrue(any("will not be restarted" in item for item in logs))
            self.assertTrue(any("installation state is untrusted" in item for item in logs))

    def test_post_lock_restore_revalidates_installation_before_launch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / "staged"
            install = root / "install"
            data = root / "data"
            restart_script = install / "appdock.py"
            for item in (staged, install, data / "runtime"):
                item.mkdir(parents=True, exist_ok=True)

            class FakeLock:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

            logs: list[str] = []
            with (
                patch.object(update_helper, "_update_lock_path", return_value=data / "runtime" / "update.lock"),
                patch.object(
                    update_helper,
                    "_validate_installed_tree",
                    side_effect=[({}, []), appdock.AppDockError("restored tree is unsafe")],
                ) as validate,
                patch.object(update_helper, "_safe_append_update_log", side_effect=lambda _path, message: logs.append(message)),
                patch.object(update_helper, "_alive", return_value=False),
                patch.object(update_helper, "_verify_helper_identity", return_value={"identity": "ok"}),
                patch.object(update_helper, "acquire_update_lock", return_value=FakeLock()),
                patch.object(update_helper, "recover_update_transactions", return_value=[]),
                patch.object(update_helper, "apply_update", side_effect=RuntimeError("apply failed")),
                patch.object(update_helper, "_discard_stage"),
                patch.object(update_helper, "_launch_and_wait") as launch,
            ):
                result = update_helper.run(
                    staged,
                    install,
                    data,
                    12345,
                    restart_script,
                    ["--host", "127.0.0.1", "--port", "8765", "--data-dir", str(data)],
                )

            self.assertEqual(result, 1)
            self.assertEqual(validate.call_count, 2)
            launch.assert_not_called()
            self.assertTrue(any("restored installation did not pass validation" in item for item in logs))


if __name__ == "__main__":
    unittest.main()
