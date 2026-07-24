from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# The helper is deliberately stdlib-only and imports the update primitive before
# waiting. The parent AppDock process can therefore exit and replace its files.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from appdock import apply_update, rollback_update  # noqa: E402


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


def run(staged: Path, install: Path, data: Path, pid: int, restart_script: Path, restart_args: list[str]) -> int:
    log_path = data / "runtime" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    log(f"update helper started for pid {pid}")
    while _alive(pid):
        time.sleep(0.2)
    try:
        result = apply_update(staged, install, data)
        log(f"update applied: {result['files']}")
        command = [sys.executable, str(restart_script), *restart_args]
        log(f"restarting AppDock with argument list: {command!r}")
        try:
            subprocess.Popen(command, shell=False, cwd=str(install), close_fds=True)
        except Exception:
            rollback_update(result, install, data)
            log("restart launch failed; previous program files restored")
            raise
    except Exception as exc:  # copy failures and restart-launch failures roll back
        log(f"update failed: {exc}")
        return 1
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
    args = parser.parse_args()
    return run(args.staged.resolve(), args.install.resolve(), args.data.resolve(), args.pid, args.restart_script.resolve(), list(args.restart_arg))


if __name__ == "__main__":
    raise SystemExit(main())
