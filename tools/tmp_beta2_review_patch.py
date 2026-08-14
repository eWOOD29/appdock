from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly one replacement in {path}, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "appdock.py",
    '''def _process_exists(pid: int) -> bool:\n    if pid <= 0 or pid == os.getpid():\n        return False\n    if os.name == "nt":\n        import ctypes\n        from ctypes import wintypes\n\n        process_query_limited_information = 0x1000\n        error_access_denied = 5\n        error_invalid_parameter = 87\n        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)\n        open_process = kernel32.OpenProcess\n        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]\n        open_process.restype = wintypes.HANDLE\n        close_handle = kernel32.CloseHandle\n        close_handle.argtypes = [wintypes.HANDLE]\n        close_handle.restype = wintypes.BOOL\n        handle = open_process(process_query_limited_information, False, pid)\n        if handle:\n            close_handle(handle)\n            return True\n        error = ctypes.get_last_error()\n        if error == error_access_denied:\n            return True\n        if error == error_invalid_parameter:\n            return False\n        return False\n    try:\n        os.kill(pid, 0)\n    except ProcessLookupError:\n        return False\n    except PermissionError:\n        return True\n    except OSError:\n        return False\n    return True\n''',
    '''def _process_exists(pid: int) -> bool:\n    if pid <= 0 or pid == os.getpid():\n        return False\n    if os.name == "nt":\n        import ctypes\n        from ctypes import wintypes\n\n        synchronize = 0x00100000\n        wait_object_0 = 0x00000000\n        wait_timeout = 0x00000102\n        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)\n        open_process = kernel32.OpenProcess\n        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]\n        open_process.restype = wintypes.HANDLE\n        wait_for_single_object = kernel32.WaitForSingleObject\n        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]\n        wait_for_single_object.restype = wintypes.DWORD\n        close_handle = kernel32.CloseHandle\n        close_handle.argtypes = [wintypes.HANDLE]\n        close_handle.restype = wintypes.BOOL\n        handle = open_process(synchronize, False, pid)\n        if not handle:\n            return False\n        try:\n            wait_result = wait_for_single_object(handle, 0)\n        finally:\n            close_handle(handle)\n        if wait_result == wait_timeout:\n            return True\n        if wait_result == wait_object_0:\n            return False\n        return False\n    try:\n        os.kill(pid, 0)\n    except ProcessLookupError:\n        return False\n    except PermissionError:\n        return True\n    except OSError:\n        return False\n    return True\n''',
)

replace_once(
    "scripts/update_helper.py",
    '''            except Exception as exc:\n                failure = exc\n                restore_after_unlock = True\n                restore_success_message = "existing AppDock restarted after updater lock release"\n                restore_failure_prefix = "existing AppDock restart after updater lock release failed"\n                log(f"update recovery failed before apply: {exc}")\n''',
    '''            except Exception as exc:\n                failure = exc\n                log(\n                    "update recovery failed before apply; existing AppDock will not be restarted "\n                    f"because installation state is untrusted: {exc}"\n                )\n''',
)

replace_once(
    "scripts/update_helper.py",
    '''    if restore_after_unlock:\n        try:\n            _launch_and_wait(\n                restart_script,\n                install,\n                restart_args,\n                startup_data=data,\n                use_startup_handoff=False,\n            )\n            log(restore_success_message)\n        except Exception as restore_exc:\n            log(f"{restore_failure_prefix}: {restore_exc}")\n    return 1\n''',
    '''    if restore_after_unlock:\n        try:\n            _validate_installed_tree(install)\n        except Exception as validation_exc:\n            log(\n                f"{restore_failure_prefix}: restored installation did not pass validation: "\n                f"{validation_exc}"\n            )\n            return 1\n        try:\n            _launch_and_wait(\n                restart_script,\n                install,\n                restart_args,\n                startup_data=data,\n                use_startup_handoff=False,\n            )\n            log(restore_success_message)\n        except Exception as restore_exc:\n            log(f"{restore_failure_prefix}: {restore_exc}")\n    return 1\n''',
)

path = Path("tests/test_update_restart_handoff_regression.py")
text = path.read_text(encoding="utf-8")
marker = '\n\nif __name__ == "__main__":\n    unittest.main()\n'
if text.count(marker) != 1:
    raise SystemExit("unexpected test-file trailer")
addition = r'''

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
'''
path.write_text(text.replace(marker, addition + marker, 1), encoding="utf-8")
