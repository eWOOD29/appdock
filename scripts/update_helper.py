from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The helper is deliberately stdlib-only and imports the update primitive before
# waiting. The parent AppDock process can therefore exit and replace its files.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from appdock import _remove_tree, apply_update, finalize_update, recover_update_transactions, rollback_update  # noqa: E402

RESTART_READY_TIMEOUT_SECONDS = 20.0


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


def _launch_and_wait(restart_script: Path, install: Path, restart_args: list[str]) -> object:
    ready_token = secrets.token_urlsafe(32)
    command = [sys.executable, str(restart_script), *restart_args, "--ready-token", ready_token]
    restarted = subprocess.Popen(command, shell=False, cwd=str(install), close_fds=True)
    try:
        _wait_for_restart_ready(restarted, restart_args, ready_token)
    except Exception:
        _stop_restarted_process(restarted)
        raise
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
) -> int:
    log_path = data / "runtime" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    log(f"update helper started for pid {pid}")
    if handshake is not None and handshake_token is not None:
        temporary = handshake.with_suffix(handshake.suffix + ".tmp")
        temporary.write_text(handshake_token, encoding="utf-8")
        temporary.replace(handshake)
    while _alive(pid):
        time.sleep(0.2)
    recover_update_transactions(data, expected_install=install)
    service_restored = False
    try:
        result = apply_update(staged, install, data, phase_hook=phase_hook if callable(phase_hook) else None)
        log(f"update applied: {result['files']}")
        log("restarting AppDock with a fixed argument list")
        try:
            _launch_and_wait(restart_script, install, restart_args)
            finalize_update(result, install, data)
        except Exception as restart_exc:
            rollback_update(result, install, data)
            log("restart readiness failed; previous program files restored")
            try:
                _launch_and_wait(restart_script, install, restart_args)
                service_restored = True
                log("restored AppDock restarted successfully")
            except Exception as restore_exc:
                log(f"restored AppDock restart failed: {restore_exc}")
            raise restart_exc
    except Exception as exc:  # copy failures and restart-launch failures roll back
        if not service_restored:
            try:
                _launch_and_wait(restart_script, install, restart_args)
                log("existing AppDock restarted after update failure")
            except Exception as restore_exc:
                log(f"existing AppDock restart after update failure failed: {restore_exc}")
        _discard_stage(staged, data)
        log(f"update failed: {exc}")
        return 1
    _discard_stage(staged, data)
    log("update helper finished")
    return 0


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
    args = parser.parse_args()
    handshake = args.handshake.expanduser().absolute()
    runtime_root = (args.data.resolve() / "runtime").resolve()
    if handshake.parent.resolve() != runtime_root or not re.fullmatch(r"update-helper-[0-9a-f]{32}\.ready", handshake.name):
        raise SystemExit("invalid update helper handshake path")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", args.handshake_token):
        raise SystemExit("invalid update helper handshake token")
    handshake.parent.mkdir(parents=True, exist_ok=True)
    return run(
        args.staged.expanduser().absolute(),
        args.install.resolve(),
        args.data.resolve(),
        args.pid,
        args.restart_script.resolve(),
        list(args.restart_arg),
        handshake=handshake,
        handshake_token=args.handshake_token,
    )


if __name__ == "__main__":
    raise SystemExit(main())
