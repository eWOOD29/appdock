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
from appdock import AppDockError, AppDockConfig, _assert_no_link_or_reparse_ancestor, _clear_staged_receipt, _is_link_or_reparse, _read_staged_receipt, _remove_tree, _update_lock_path, _write_update_startup_handoff, acquire_update_lock, apply_update, finalize_update, recover_update_transactions, rollback_update  # noqa: E402

RESTART_READY_TIMEOUT_SECONDS = 20.0


def _process_group_options() -> dict[str, object]:
    """Keep the restarted service out of the helper/test console process group."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _safe_append_update_log(path: Path, message: str) -> None:
    """Append one line without following an unsafe or multiply-linked log file."""
    path = path.expanduser().absolute()
    _assert_no_link_or_reparse_ancestor(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_or_reparse_ancestor(path.parent)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    binary_flag = getattr(os, "O_BINARY", 0)
    flags |= binary_flag
    for attempt in range(2):
        existed = path.exists()
        before = path.stat() if existed else None
        if existed:
            if _is_link_or_reparse(path) or before is None or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise AppDockError("update log is not a regular single-link file")
        else:
            flags |= os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            flags &= ~os.O_EXCL
            continue
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise AppDockError("update log opened as an unsafe file")
            if before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise AppDockError("update log changed while opening")
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
        return
    raise AppDockError("update log could not be created safely")


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
    startup_receipt = _write_update_startup_handoff(data, install, ready_token) if data is not None else None
    command = [
        sys.executable,
        str(restart_script),
        *restart_args,
        f"--ready-token={ready_token}",
        f"--update-helper-startup={ready_token}",
    ]
    try:
        restarted = subprocess.Popen(
            command,
            shell=False,
            cwd=str(install),
            close_fds=True,
            **_process_group_options(),
        )
    except OSError:
        if startup_receipt is not None:
            startup_receipt.unlink(missing_ok=True)
        raise
    try:
        _wait_for_restart_ready(restarted, restart_args, ready_token)
    except Exception:
        _stop_restarted_process(restarted)
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


def main() -> int:
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
    args = parser.parse_args()
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
