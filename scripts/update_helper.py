from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# The helper is deliberately stdlib-only and imports the update primitive before
# waiting. The parent AppDock process can therefore exit and replace its files.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from appdock import AppDockError, AppDockConfig, _assert_no_link_or_reparse_ancestor, _clear_staged_receipt, _is_link_or_reparse, _read_staged_receipt, _remove_tree, _update_lock_path, _validate_installed_tree, _write_update_startup_handoff, acquire_update_lock, apply_update, finalize_update, recover_update_transactions, rollback_update  # noqa: E402

RESTART_READY_TIMEOUT_SECONDS = 20.0
RESTART_STDOUT_LOG_NAME = "update-restart.stdout.log"
RESTART_STDERR_LOG_NAME = "update-restart.stderr.log"


def _process_group_options() -> dict[str, object]:
    """Keep the restarted service out of the helper/test console process group."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _validate_windows_handle_safety(handle: int, label: str) -> None:
    """Reject a Windows handle that itself names a reparse object."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    info = _FileAttributeTagInfo()
    if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise AppDockError(f"{label} handle attributes could not be verified") from ctypes.WinError(ctypes.get_last_error())
    if info.FileAttributes & 0x400:
        raise AppDockError(f"{label} opened as a reparse point")


def _open_existing_no_follow_descriptor(path: Path, label: str) -> int:
    """Open an existing append target without following its final alias."""
    if os.name != "nt":
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise AppDockError(f"{label} cannot be opened safely on this platform")
        flags = os.O_WRONLY | os.O_APPEND | nofollow | getattr(os, "O_BINARY", 0)
        try:
            return os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise AppDockError(f"{label} could not be opened without following aliases") from exc

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    file_append_data = 0x00000004
    file_read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    handle = create_file(
        str(path),
        file_append_data | file_read_attributes,
        share_all,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, f"{label} disappeared while opening", str(path))
        raise AppDockError(f"{label} could not be opened safely") from ctypes.WinError(error)
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0),
        )
    except Exception:
        close_handle(handle)
        raise
    return descriptor


def _validate_open_append_descriptor(path: Path, descriptor: int, label: str) -> None:
    _assert_no_link_or_reparse_ancestor(path.parent)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise AppDockError(f"{label} opened as an unsafe file")
    if os.name == "nt":
        import msvcrt

        _validate_windows_handle_safety(msvcrt.get_osfhandle(descriptor), label)
    if _is_link_or_reparse(path):
        raise AppDockError(f"{label} path changed to an unsafe alias while opening")
    try:
        current = path.stat()
    except FileNotFoundError as exc:
        raise AppDockError(f"{label} path disappeared while opening") from exc
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise AppDockError(f"{label} path identity changed while opening")


def _open_safe_append_descriptor(path: Path, label: str) -> int:
    """Create or open one append-only log without a check-then-follow boundary."""
    path = path.expanduser().absolute()
    _assert_no_link_or_reparse_ancestor(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_or_reparse_ancestor(path.parent)
    create_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise AppDockError(f"{label} cannot be created safely on this platform")
        create_flags |= nofollow
    for _attempt in range(3):
        _assert_no_link_or_reparse_ancestor(path.parent)
        if _is_link_or_reparse(path):
            raise AppDockError(f"{label} is unsafe")
        descriptor = -1
        if path.exists():
            try:
                descriptor = _open_existing_no_follow_descriptor(path, label)
            except FileNotFoundError:
                continue
        else:
            try:
                descriptor = os.open(path, create_flags, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                raise AppDockError(f"{label} could not be created safely") from exc
        try:
            _validate_open_append_descriptor(path, descriptor, label)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    raise AppDockError(f"{label} could not be opened safely")


def _safe_append_update_log(path: Path, message: str) -> None:
    """Append one durable line through the same no-follow primitive as restart logs."""
    descriptor = _open_safe_append_descriptor(path, "update log")
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
            stream.flush()
            os.fsync(stream.fileno())
            after = os.fstat(stream.fileno())
            if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1:
                raise AppDockError("update log changed link identity while writing")
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _open_restart_log_stream(path: Path):
    """Open a durable append-only restart log without following unsafe aliases."""
    descriptor = _open_safe_append_descriptor(path, "restart diagnostic log")
    try:
        stream = os.fdopen(descriptor, "a", encoding="utf-8", newline="")
        descriptor = -1
        return stream
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _restart_health_url(restart_args: list[str]) -> str:
    host = "127.0.0.1"
    port = 8765
    for index, argument in enumerate(restart_args):
        if argument == "--host" and index + 1 < len(restart_args):
            host = restart_args[index + 1]
        elif argument.startswith("--host="):
            host = argument.partition("=")[2]
        elif argument == "--port" and index + 1 < len(restart_args):
            port = int(restart_args[index + 1])
        elif argument.startswith("--port="):
            port = int(argument.partition("=")[2])
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535:
        raise RuntimeError("restart health endpoint is invalid")
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}/health"


def _stop_restarted_process(process: object) -> None:
    if getattr(process, "poll")() is not None:
        return
    try:
        getattr(process, "terminate")()
        getattr(process, "wait")(timeout=3)
    except Exception:
        try:
            getattr(process, "kill")()
            getattr(process, "wait")(timeout=3)
        except Exception:
            pass


def _wait_for_restart_ready(
    process: object,
    restart_args: list[str],
    ready_token: str,
    timeout: float = RESTART_READY_TIMEOUT_SECONDS,
) -> None:
    health_url = _restart_health_url(restart_args)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        returncode = getattr(process, "poll")()
        if returncode is not None:
            raise RuntimeError(f"restarted AppDock exited before readiness with status {returncode}")
        try:
            request = urllib.request.Request(health_url, headers={"User-Agent": "AppDock-Updater"})
            with urllib.request.urlopen(request, timeout=1) as response:
                if callable(getattr(response, "geturl", None)) and response.geturl() != health_url:
                    raise RuntimeError("restart health check was redirected")
                payload = json.loads(response.read().decode("utf-8"))
            response_token = payload.get("ready_token")
            if (
                payload.get("ok") is True
                and payload.get("service") == "appdock"
                and isinstance(response_token, str)
                and secrets.compare_digest(response_token, ready_token)
            ):
                return
        except (OSError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise RuntimeError("restarted AppDock did not become healthy before the timeout")


def _discard_stage(staged: Path, data: Path) -> None:
    candidate = staged.expanduser().absolute()
    updates_root = (data / "updates").resolve()
    try:
        candidate.parent.resolve().relative_to(updates_root)
    except ValueError:
        return
    if candidate.resolve() == updates_root:
        return
    _remove_tree(candidate, ignore_errors=True)
    _clear_staged_receipt(data, candidate)


def _restart_data_dir(restart_args: list[str]) -> Path | None:
    for index, argument in enumerate(restart_args):
        if argument == "--data-dir" and index + 1 < len(restart_args):
            return Path(restart_args[index + 1])
        if argument.startswith("--data-dir="):
            return Path(argument.partition("=")[2])
    return None


def _verify_helper_identity(
    staged: Path,
    data: Path,
    *,
    expected_version: str | None,
    expected_digest: str | None,
    expected_zip_sha256: str | None,
    expected_inventory_sha256: str | None,
    expected_helper_sha256: str | None,
) -> dict[str, object] | None:
    config = AppDockConfig.from_environment(data_dir=data)
    receipt = _read_staged_receipt(config)
    expected = (expected_version, expected_digest, expected_zip_sha256, expected_inventory_sha256, expected_helper_sha256)
    if any(value is not None for value in expected):
        if receipt is None or any(not isinstance(value, str) for value in expected):
            raise AppDockError("helper update identity arguments are incomplete")
        if receipt["version"] != expected_version or receipt["digest"] != expected_digest:
            raise AppDockError("helper update receipt claim does not match")
        identity = receipt["identity"]
        if (
            identity["zip_sha256"] != expected_zip_sha256
            or identity["inventory_sha256"] != expected_inventory_sha256
            or identity["helper_sha256"] != expected_helper_sha256
        ):
            raise AppDockError("helper update identity claim does not match")
    if receipt is not None and Path(receipt["path"]).expanduser().resolve() != staged.expanduser().resolve():
        raise AppDockError("helper staged path does not match its receipt")
    return receipt


def _restore_existing_service_after_preapply_failure(
    restart_script: Path,
    install: Path,
    data: Path,
    restart_args: list[str],
    log: Callable[[str], None],
) -> bool:
    """Restore the old serving process after the parent has handed off."""
    try:
        _launch_and_wait(restart_script, install, restart_args, startup_data=data)
    except Exception as exc:
        log(f"existing AppDock could not be restored before apply: {exc}")
        return False
    log("existing AppDock restored and readiness verified before apply")
    return True


def _launch_and_wait(
    restart_script: Path,
    install: Path,
    restart_args: list[str],
    *,
    startup_data: Path | None = None,
    ready_token: str | None = None,
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
        startup_receipt = _write_update_startup_handoff(data, install, ready_token) if data is not None else None
        command = [
            sys.executable,
            str(restart_script),
            *restart_args,
            f"--ready-token={ready_token}",
            f"--update-helper-startup={ready_token}",
        ]
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
    return restarted


def run(
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
    try:
        with acquire_update_lock(data):
            try:
                recover_update_transactions(data, expected_install=install)
            except Exception as exc:
                _restore_existing_service_after_preapply_failure(restart_script, install, data, restart_args, log)
                log(f"update recovery failed before apply: {exc}")
                return 1
            service_restored = False
            try:
                result = apply_update(
                    staged,
                    install,
                    data,
                    phase_hook=phase_hook if callable(phase_hook) else None,
                    expected_identity=claimed_identity,
                )
                log(f"update applied: {result['files']}")
                log("restarting AppDock with a fixed argument list")
                try:
                    _launch_and_wait(restart_script, install, restart_args, startup_data=data)
                    finalize_update(result, install, data)
                except Exception as restart_exc:
                    rollback_update(result, install, data)
                    log("restart readiness failed; previous program files restored")
                    try:
                        _launch_and_wait(restart_script, install, restart_args, startup_data=data)
                        service_restored = True
                        log("restored AppDock restarted successfully")
                    except Exception as restore_exc:
                        log(f"restored AppDock restart failed: {restore_exc}")
                    raise restart_exc
            except Exception as exc:  # copy failures and restart-launch failures roll back
                if not service_restored:
                    try:
                        _launch_and_wait(restart_script, install, restart_args, startup_data=data)
                        log("existing AppDock restarted after update failure")
                    except Exception as restore_exc:
                        log(f"existing AppDock restart after update failure failed: {restore_exc}")
                _discard_stage(staged, data)
                log(f"update failed: {exc}")
                return 1
            _discard_stage(staged, data)
            log("update helper finished")
            return 0
    except Exception as exc:
        log(f"update helper could not acquire updater lock or recover transactions: {exc}")
        return 1


_HELPER_OPTIONS = frozenset({
    "--staged",
    "--install",
    "--data",
    "--pid",
    "--restart-script",
    "--restart-arg",
    "--handshake",
    "--handshake-token",
    "--expected-version",
    "--expected-digest",
    "--expected-zip-sha256",
    "--expected-inventory-sha256",
    "--expected-helper-sha256",
})


def _normalize_helper_cli_args(argv: list[str] | None = None) -> list[str]:
    """Bind a legacy split option-like handshake token before argparse sees it.

    AppDock v0.2.1 passes ``--handshake-token`` and its random token as two
    argv entries. ``secrets.token_urlsafe(32)`` may begin with ``-``, which
    argparse otherwise treats as another option. Keep the compatibility shim
    limited to a syntactically valid token and never consume a known option.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    for index, argument in enumerate(arguments[:-1]):
        if argument != "--handshake-token":
            continue
        candidate = arguments[index + 1]
        if (
            candidate.startswith("-")
            and candidate not in _HELPER_OPTIONS
            and re.fullmatch(r"[A-Za-z0-9_-]{20,128}", candidate)
        ):
            arguments[index:index + 2] = [f"--handshake-token={candidate}"]
        break
    return arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AppDock external update helper")
    parser.add_argument("--staged", type=Path, required=True)
    parser.add_argument("--install", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--restart-script", type=Path, required=True)
    parser.add_argument("--restart-arg", action="append", default=[])
    parser.add_argument("--handshake", type=Path, required=True)
    parser.add_argument("--handshake-token", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--expected-zip-sha256", required=True)
    parser.add_argument("--expected-inventory-sha256", required=True)
    parser.add_argument("--expected-helper-sha256", required=True)
    args = parser.parse_args(_normalize_helper_cli_args(argv))
    data = args.data.expanduser().absolute()
    try:
        runtime_root = _update_lock_path(data).parent
    except Exception as exc:
        raise SystemExit(f"unsafe update data root: {exc}") from exc
    handshake = args.handshake.expanduser().absolute()
    staged = args.staged.expanduser().absolute()
    install = args.install.expanduser().absolute()
    restart_script = args.restart_script.expanduser().absolute()
    for path in (handshake, staged, install, restart_script):
        _assert_no_link_or_reparse_ancestor(path)
    if _is_link_or_reparse(handshake) or handshake.parent != runtime_root or not re.fullmatch(r"update-helper-[0-9a-f]{32}\.ready", handshake.name):
        raise SystemExit("invalid update helper handshake path")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", args.handshake_token):
        raise SystemExit("invalid update helper handshake token")
    handshake.parent.mkdir(parents=True, exist_ok=True)
    return run(
        staged,
        install,
        data,
        args.pid,
        restart_script,
        list(args.restart_arg),
        handshake=handshake,
        handshake_token=args.handshake_token,
        expected_version=args.expected_version,
        expected_digest=args.expected_digest,
        expected_zip_sha256=args.expected_zip_sha256,
        expected_inventory_sha256=args.expected_inventory_sha256,
        expected_helper_sha256=args.expected_helper_sha256,
    )


if __name__ == "__main__":
    raise SystemExit(main())