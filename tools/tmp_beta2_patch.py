from pathlib import Path


def replace_between(path: Path, start_marker: str, end_marker: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    updated = text[:start] + replacement.rstrip() + "\n\n" + text[end:]
    path.write_text(updated, encoding="utf-8")


appdock = Path("appdock.py")
replace_between(
    appdock,
    "def _process_exists(pid: int) -> bool:\n",
    "def _consume_update_startup_handoff",
    r'''def _process_exists(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(process_query_limited_information, False, pid)
        if handle:
            close_handle(handle)
            return True
        error = ctypes.get_last_error()
        if error == error_access_denied:
            return True
        if error == error_invalid_parameter:
            return False
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True''',
)

helper = Path("scripts/update_helper.py")
replace_between(
    helper,
    "def _restore_existing_service_after_preapply_failure(\n",
    "def _launch_and_wait(\n",
    r'''def _restore_existing_service_after_preapply_failure(
    restart_script: Path,
    install: Path,
    data: Path,
    restart_args: list[str],
    log: Callable[[str], None],
) -> bool:
    """Restore the old service normally when no updater lock is held."""
    try:
        _launch_and_wait(
            restart_script,
            install,
            restart_args,
            startup_data=data,
            use_startup_handoff=False,
        )
    except Exception as exc:
        log(f"existing AppDock could not be restored before apply: {exc}")
        return False
    log("existing AppDock restored and readiness verified before apply")
    return True''',
)

replace_between(
    helper,
    "def _launch_and_wait(\n",
    "def run(\n",
    r'''def _launch_and_wait(
    restart_script: Path,
    install: Path,
    restart_args: list[str],
    *,
    startup_data: Path | None = None,
    ready_token: str | None = None,
    use_startup_handoff: bool = True,
) -> object:
    ready_token = ready_token or secrets.token_urlsafe(32)
    data = startup_data or _restart_data_dir(restart_args)
    startup_receipt = None
    stdout_stream = None
    stderr_stream = None
    stdout_target: object = subprocess.DEVNULL
    stderr_target: object = subprocess.DEVNULL
    try:
        if data is not None:
            runtime = data.expanduser().absolute() / "runtime"
            stdout_stream = _open_restart_log_stream(runtime / RESTART_STDOUT_LOG_NAME)
            stderr_stream = _open_restart_log_stream(runtime / RESTART_STDERR_LOG_NAME)
            stdout_target = stdout_stream
            stderr_target = stderr_stream
        if use_startup_handoff and data is not None:
            startup_receipt = _write_update_startup_handoff(data, install, ready_token)
        command = [
            sys.executable,
            str(restart_script),
            *restart_args,
            f"--ready-token={ready_token}",
        ]
        if use_startup_handoff:
            command.append(f"--update-helper-startup={ready_token}")
        restarted = subprocess.Popen(
            command,
            shell=False,
            cwd=str(install),
            close_fds=True,
            stdout=stdout_target,
            stderr=stderr_target,
            **_process_group_options(),
        )
    except Exception:
        if stdout_stream is not None:
            stdout_stream.close()
        if stderr_stream is not None:
            stderr_stream.close()
        if startup_receipt is not None:
            startup_receipt.unlink(missing_ok=True)
        raise
    else:
        if stdout_stream is not None:
            stdout_stream.close()
        if stderr_stream is not None:
            stderr_stream.close()
    try:
        _wait_for_restart_ready(restarted, restart_args, ready_token)
    except Exception as exc:
        _stop_restarted_process(restarted)
        if data is not None:
            raise RuntimeError(
                f"{exc}; restart diagnostics were captured in runtime/{RESTART_STDOUT_LOG_NAME} and runtime/{RESTART_STDERR_LOG_NAME}"
            ) from exc
        raise
    finally:
        if startup_receipt is not None:
            startup_receipt.unlink(missing_ok=True)
    return restarted''',
)

replace_between(
    helper,
    "def run(\n",
    "_HELPER_OPTIONS = frozenset({\n",
    r'''def run(
    staged: Path,
    install: Path,
    data: Path,
    pid: int,
    restart_script: Path,
    restart_args: list[str],
    *,
    handshake: Path | None = None,
    handshake_token: str | None = None,
    phase_hook: object | None = None,
    expected_version: str | None = None,
    expected_digest: str | None = None,
    expected_zip_sha256: str | None = None,
    expected_inventory_sha256: str | None = None,
    expected_helper_sha256: str | None = None,
) -> int:
    data = data.expanduser().absolute()
    _update_lock_path(data)
    log_path = data / "runtime" / "update.log"

    def log(message: str) -> None:
        _safe_append_update_log(log_path, message)

    log(f"update helper started for pid {pid}")
    try:
        _validate_installed_tree(install)
    except Exception as exc:
        log(f"update preflight rejected current installation; AppDock was left running: {exc}")
        return 1
    if handshake is not None and handshake_token is not None:
        temporary = handshake.with_suffix(handshake.suffix + ".tmp")
        temporary.write_text(handshake_token, encoding="utf-8")
        temporary.replace(handshake)
    while _alive(pid):
        time.sleep(0.2)
    try:
        claimed_identity = _verify_helper_identity(
            staged,
            data,
            expected_version=expected_version,
            expected_digest=expected_digest,
            expected_zip_sha256=expected_zip_sha256,
            expected_inventory_sha256=expected_inventory_sha256,
            expected_helper_sha256=expected_helper_sha256,
        )
    except Exception as exc:
        log(f"update helper identity verification failed: {exc}")
        _restore_existing_service_after_preapply_failure(restart_script, install, data, restart_args, log)
        return 1

    failure: Exception | None = None
    restore_after_unlock = False
    restore_success_message = ""
    restore_failure_prefix = ""
    discard_failed_stage = False
    try:
        with acquire_update_lock(data):
            try:
                recover_update_transactions(data, expected_install=install)
            except Exception as exc:
                failure = exc
                restore_after_unlock = True
                restore_success_message = "existing AppDock restarted after updater lock release"
                restore_failure_prefix = "existing AppDock restart after updater lock release failed"
                log(f"update recovery failed before apply: {exc}")
            else:
                try:
                    result = apply_update(
                        staged,
                        install,
                        data,
                        phase_hook=phase_hook if callable(phase_hook) else None,
                        expected_identity=claimed_identity,
                    )
                except Exception as exc:
                    failure = exc
                    restore_after_unlock = True
                    restore_success_message = "existing AppDock restarted after updater lock release"
                    restore_failure_prefix = "existing AppDock restart after updater lock release failed"
                    discard_failed_stage = True
                else:
                    log(f"update applied: {result['files']}")
                    log("restarting AppDock with a fixed argument list")
                    restarted = None
                    try:
                        restarted = _launch_and_wait(restart_script, install, restart_args, startup_data=data)
                        finalize_update(result, install, data)
                    except Exception as restart_exc:
                        if restarted is not None:
                            _stop_restarted_process(restarted)
                        try:
                            rollback_update(result, install, data)
                        except Exception as rollback_exc:
                            failure = rollback_exc
                            log(f"restart readiness failed and rollback failed: {rollback_exc}")
                        else:
                            log("restart readiness failed; previous program files restored")
                            failure = restart_exc
                            restore_after_unlock = True
                            restore_success_message = "restored AppDock restarted successfully after updater lock release"
                            restore_failure_prefix = "restored AppDock restart after updater lock release failed"
                            discard_failed_stage = True
                    else:
                        _discard_stage(staged, data)
                        log("update helper finished")
                        return 0

                if discard_failed_stage:
                    _discard_stage(staged, data)
                if failure is not None:
                    log(f"update failed: {failure}")
    except Exception as exc:
        log(f"update helper could not acquire updater lock or recover transactions: {exc}")
        return 1

    if restore_after_unlock:
        try:
            _launch_and_wait(
                restart_script,
                install,
                restart_args,
                startup_data=data,
                use_startup_handoff=False,
            )
            log(restore_success_message)
        except Exception as restore_exc:
            log(f"{restore_failure_prefix}: {restore_exc}")
    return 1''',
)


generation9 = Path("tests/test_generation9_remediation.py")
generation9_text = generation9.read_text(encoding="utf-8")
old_restore = '''        def restore(restart_script, install, restart_args, *, startup_data=None, ready_token=None):\n            launched.append((restart_script, install, restart_args, startup_data))\n            return SimpleNamespace(poll=lambda: None)\n'''
new_restore = '''        def restore(restart_script, install, restart_args, *, startup_data=None, ready_token=None, use_startup_handoff=True):\n            self.assertFalse(use_startup_handoff)\n            launched.append((restart_script, install, restart_args, startup_data))\n            return SimpleNamespace(poll=lambda: None)\n'''
if old_restore not in generation9_text:
    raise RuntimeError("generation9 restore seam was not found")
generation9.write_text(generation9_text.replace(old_restore, new_restore, 1), encoding="utf-8")


test_path = Path("tests/test_update_restart_handoff_regression.py")
test_path.write_text(r'''from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
''', encoding="utf-8")

print("patched appdock.py, scripts/update_helper.py, and restart-handoff regressions")
