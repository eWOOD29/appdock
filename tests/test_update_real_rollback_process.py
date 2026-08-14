from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path

import appdock
from scripts import build_portable, update_helper


V021_URL = "https://github.com/eWOOD29/appdock/releases/download/v0.2.1/appdock-windows.zip"
V021_SHA256 = "166fb1211b46d5e253e499df6f873e0425f8a71be24c6b87740ed7082ac46e49"


def _extract_zip(payload: bytes, destination: Path) -> None:
    appdock.validate_zip(payload)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(destination)


def _managed_snapshot(root: Path) -> dict[str, str]:
    manifest = json.loads((root / appdock.RELEASE_MANIFEST_NAME).read_text(encoding="utf-8"))
    return {
        item["path"]: hashlib.sha256((root / Path(*item["path"].split("/"))).read_bytes()).hexdigest()
        for item in manifest["files"]
    }


def _source_version(path: Path) -> str:
    match = re.search(r'^CURRENT_VERSION\s*=\s*["\']([^"\']+)["\']', path.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise AssertionError(f"CURRENT_VERSION was not found in {path}")
    return match.group(1)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def _events(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _listening_pids(port: int) -> set[int]:
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        if not fields[1].endswith(f":{port}"):
            continue
        try:
            pids.add(int(fields[4]))
        except ValueError:
            continue
    return pids


def _terminate_tree(pid: int) -> None:
    if pid <= 0:
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )


WRAPPER_SOURCE = r'''from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVENTS = ROOT / "rollback-events.jsonl"
INSTALL = Path.cwd().resolve()
ARGS = sys.argv[1:]
DATA = None
for index, argument in enumerate(ARGS):
    if argument == "--data-dir" and index + 1 < len(ARGS):
        DATA = Path(ARGS[index + 1]).resolve()
    elif argument.startswith("--data-dir="):
        DATA = Path(argument.partition("=")[2]).resolve()
if DATA is None:
    raise SystemExit("missing data root")


def emit(kind, **fields):
    payload = {"kind": kind, "wrapper_pid": os.getpid(), **fields}
    with EVENTS.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True) + "\n")
        stream.flush()


source = (INSTALL / "appdock.py").read_text(encoding="utf-8")
match = re.search(r'^CURRENT_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
version = match.group(1) if match else ""
app_sha256 = hashlib.sha256((INSTALL / "appdock.py").read_bytes()).hexdigest()
handoff = any(argument.startswith("--update-helper-startup=") for argument in ARGS)
phase = "candidate" if handoff else "restored-old"
emit("launch-boundary", phase=phase, version=version, app_sha256=app_sha256, argv=ARGS)

probe_code = (
    "import sys, appdock; "
    "\ntry:\n lock=appdock.acquire_update_lock(sys.argv[1]); lock.release()"
    "\nexcept Exception as exc:\n print(type(exc).__name__ + ':' + str(exc)); raise SystemExit(3)"
    "\nprint('acquired')"
)
probe = subprocess.run(
    [sys.executable, "-B", "-c", probe_code, str(DATA)],
    cwd=str(INSTALL),
    capture_output=True,
    text=True,
    timeout=10,
    shell=False,
    close_fds=True,
)
emit(
    "lock-probe",
    phase=phase,
    returncode=probe.returncode,
    stdout=probe.stdout.strip(),
    stderr=probe.stderr.strip(),
)

child_args = [sys.executable, "-B", str(INSTALL / "appdock.py"), *ARGS]
if handoff:
    child_args.append("--appdock-probe-force-candidate-failure")
child = subprocess.Popen(
    child_args,
    cwd=str(INSTALL),
    shell=False,
    close_fds=True,
)
emit("child-started", phase=phase, child_pid=child.pid, argv=child_args[2:])
returncode = child.wait()
emit("child-exited", phase=phase, child_pid=child.pid, returncode=returncode)
raise SystemExit(returncode)
'''


class RealRollbackProcessProofTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "real rollback process proof is Windows-specific")
    def test_candidate_process_failure_rolls_back_exact_v021_and_restarts_after_lock_release(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            events_path = root / "rollback-events.jsonl"
            wrapper = root / "restart_wrapper.py"
            wrapper.write_text(WRAPPER_SOURCE, encoding="utf-8")

            request = urllib.request.Request(V021_URL, headers={"User-Agent": "AppDock-CI-Rollback-Proof"})
            with urllib.request.urlopen(request, timeout=30) as response:
                old_zip = response.read(appdock.MAX_UPDATE_ASSET_BYTES + 1)
            self.assertLessEqual(len(old_zip), appdock.MAX_UPDATE_ASSET_BYTES)
            self.assertEqual(hashlib.sha256(old_zip).hexdigest(), V021_SHA256)
            _extract_zip(old_zip, install)
            self.assertEqual(_source_version(install / "appdock.py"), "0.2.1")
            old_snapshot = _managed_snapshot(install)
            old_app_sha = hashlib.sha256((install / "appdock.py").read_bytes()).hexdigest()

            candidate_zip = root / "candidate.zip"
            build_portable.build_archive(candidate_zip)
            candidate_bytes = candidate_zip.read_bytes()
            appdock.validate_zip(candidate_bytes)
            staged = data / "updates" / "0.2.2-beta.1"
            _extract_zip(candidate_bytes, staged)
            self.assertEqual(_source_version(staged / "appdock.py"), "0.2.2-beta.1")
            candidate_app_sha = hashlib.sha256((staged / "appdock.py").read_bytes()).hexdigest()
            self.assertNotEqual(candidate_app_sha, old_app_sha)

            port = _free_loopback_port()
            restart_args = [
                "--host", "127.0.0.1",
                "--port", str(port),
                "--data-dir", str(data),
            ]
            wrapper_pids: set[int] = set()
            try:
                result = update_helper.run(
                    staged,
                    install,
                    data,
                    0,
                    wrapper,
                    restart_args,
                )
                self.assertEqual(result, 1)

                records = _events(events_path)
                wrapper_pids.update(int(item["wrapper_pid"]) for item in records if isinstance(item.get("wrapper_pid"), int))
                candidate_boundary = next(
                    item for item in records
                    if item.get("kind") == "launch-boundary" and item.get("phase") == "candidate"
                )
                old_boundary = next(
                    item for item in records
                    if item.get("kind") == "launch-boundary" and item.get("phase") == "restored-old"
                )
                self.assertEqual(candidate_boundary["version"], "0.2.2-beta.1")
                self.assertEqual(candidate_boundary["app_sha256"], candidate_app_sha)
                self.assertEqual(old_boundary["version"], "0.2.1")
                self.assertEqual(old_boundary["app_sha256"], old_app_sha)

                candidate_lock = next(
                    item for item in records
                    if item.get("kind") == "lock-probe" and item.get("phase") == "candidate"
                )
                old_lock = next(
                    item for item in records
                    if item.get("kind") == "lock-probe" and item.get("phase") == "restored-old"
                )
                self.assertNotEqual(candidate_lock["returncode"], 0)
                self.assertEqual(old_lock["returncode"], 0, old_lock)
                self.assertEqual(old_lock["stdout"], "acquired")

                candidate_started = next(
                    item for item in records
                    if item.get("kind") == "child-started" and item.get("phase") == "candidate"
                )
                candidate_exited = next(
                    item for item in records
                    if item.get("kind") == "child-exited" and item.get("phase") == "candidate"
                )
                self.assertIn("--appdock-probe-force-candidate-failure", candidate_started["argv"])
                self.assertNotEqual(candidate_exited["returncode"], 0)

                old_started = next(
                    item for item in records
                    if item.get("kind") == "child-started" and item.get("phase") == "restored-old"
                )
                old_argv = list(old_started["argv"])
                self.assertFalse(any(str(argument).startswith("--update-helper-startup=") for argument in old_argv))
                ready_arguments = [str(argument) for argument in old_argv if str(argument).startswith("--ready-token=")]
                self.assertEqual(len(ready_arguments), 1)
                ready_token = ready_arguments[0].partition("=")[2]

                inventory, extras = appdock._validate_installed_tree(install)
                self.assertEqual(extras, [])
                self.assertEqual(inventory, old_snapshot)
                self.assertEqual(_source_version(install / "appdock.py"), "0.2.1")
                self.assertEqual(hashlib.sha256((install / "appdock.py").read_bytes()).hexdigest(), old_app_sha)

                health_url = f"http://127.0.0.1:{port}/health"
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                self.assertTrue(health["ok"])
                self.assertEqual(health["service"], "appdock")
                self.assertEqual(health["version"], "0.2.1")
                self.assertEqual(health["ready_token"], ready_token)

                listening = _listening_pids(port)
                self.assertEqual(listening, {int(old_started["child_pid"])})

                transactions = sorted((data / "updates" / "transactions").glob("*/transaction.json"))
                self.assertEqual(len(transactions), 1)
                journal = json.loads(transactions[0].read_text(encoding="utf-8"))
                self.assertEqual(journal["phase"], "rolled_back")
                self.assertEqual(journal["recovery"], "restore-old")
                self.assertFalse(staged.exists())
                self.assertEqual(list((data / "runtime").glob("update-startup-*.json")), [])

                update_log = (data / "runtime" / "update.log").read_text(encoding="utf-8")
                self.assertIn("update applied", update_log)
                self.assertIn("restart readiness failed; previous program files restored", update_log)
                self.assertIn("restored AppDock restarted successfully after updater lock release", update_log)
            finally:
                for pid in sorted(wrapper_pids, reverse=True):
                    _terminate_tree(pid)
                if events_path.is_file():
                    for item in _events(events_path):
                        pid = item.get("wrapper_pid")
                        if isinstance(pid, int):
                            _terminate_tree(pid)
                        child_pid = item.get("child_pid")
                        if isinstance(child_pid, int):
                            _terminate_tree(child_pid)


if __name__ == "__main__":
    unittest.main()
