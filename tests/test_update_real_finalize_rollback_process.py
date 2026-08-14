from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import appdock
from scripts import build_portable, update_helper
from test_update_real_rollback_process import (
    V021_SHA256,
    V021_URL,
    _events,
    _extract_zip,
    _free_loopback_port,
    _listening_pids,
    _managed_snapshot,
    _source_version,
    _terminate_tree,
)


FINALIZE_WRAPPER_SOURCE = r'''from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVENTS = ROOT / "finalize-events.jsonl"
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


def pid_running(pid):
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0 and str(pid) in result.stdout and "No tasks are running" not in result.stdout


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

if phase == "restored-old" and EVENTS.is_file():
    candidate_pid = None
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("kind") == "child-started" and event.get("phase") == "candidate":
            candidate_pid = int(event["child_pid"])
    if candidate_pid is not None:
        emit("prior-candidate-state", candidate_pid=candidate_pid, running=pid_running(candidate_pid))

child_args = [sys.executable, "-B", str(INSTALL / "appdock.py"), *ARGS]
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


class RealFinalizeRollbackProcessProofTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "real finalize rollback proof is Windows-specific")
    def test_healthy_candidate_is_stopped_before_finalize_failure_rollback_and_old_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            events_path = root / "finalize-events.jsonl"
            wrapper = root / "restart_wrapper.py"
            wrapper.write_text(FINALIZE_WRAPPER_SOURCE, encoding="utf-8")

            request = urllib.request.Request(V021_URL, headers={"User-Agent": "AppDock-CI-Finalize-Rollback-Proof"})
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
            health_url = f"http://127.0.0.1:{port}/health"
            finalize_observation: dict[str, object] = {}
            wrapper_pids: set[int] = set()

            def fail_finalize(_result, _install, _data):
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                records = _events(events_path)
                candidate_started = next(
                    item for item in records
                    if item.get("kind") == "child-started" and item.get("phase") == "candidate"
                )
                finalize_observation.update(
                    health=health,
                    candidate_pid=int(candidate_started["child_pid"]),
                    listening=sorted(_listening_pids(port)),
                )
                raise RuntimeError("forced finalize failure after healthy candidate")

            try:
                with patch.object(update_helper, "finalize_update", side_effect=fail_finalize):
                    result = update_helper.run(
                        staged,
                        install,
                        data,
                        0,
                        wrapper,
                        restart_args,
                    )
                self.assertEqual(result, 1)

                candidate_health = dict(finalize_observation["health"])
                candidate_pid = int(finalize_observation["candidate_pid"])
                self.assertTrue(candidate_health["ok"])
                self.assertEqual(candidate_health["service"], "appdock")
                self.assertEqual(candidate_health["version"], "0.2.2-beta.1")
                self.assertEqual(finalize_observation["listening"], [candidate_pid])

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

                prior_candidate = next(item for item in records if item.get("kind") == "prior-candidate-state")
                self.assertEqual(int(prior_candidate["candidate_pid"]), candidate_pid)
                self.assertFalse(prior_candidate["running"], prior_candidate)

                old_started = next(
                    item for item in records
                    if item.get("kind") == "child-started" and item.get("phase") == "restored-old"
                )
                old_argv = list(old_started["argv"])
                self.assertFalse(any(str(argument).startswith("--update-helper-startup=") for argument in old_argv))
                ready_arguments = [str(argument) for argument in old_argv if str(argument).startswith("--ready-token=")]
                self.assertEqual(len(ready_arguments), 1)
                old_ready_token = ready_arguments[0].partition("=")[2]

                inventory, extras = appdock._validate_installed_tree(install)
                self.assertEqual(extras, [])
                self.assertEqual(inventory, old_snapshot)
                self.assertEqual(_source_version(install / "appdock.py"), "0.2.1")
                self.assertEqual(hashlib.sha256((install / "appdock.py").read_bytes()).hexdigest(), old_app_sha)

                with urllib.request.urlopen(health_url, timeout=5) as response:
                    old_health = json.loads(response.read().decode("utf-8"))
                self.assertTrue(old_health["ok"])
                self.assertEqual(old_health["service"], "appdock")
                self.assertEqual(old_health["version"], "0.2.1")
                self.assertEqual(old_health["ready_token"], old_ready_token)
                self.assertEqual(_listening_pids(port), {int(old_started["child_pid"])})
                self.assertNotIn(candidate_pid, _listening_pids(port))

                transactions = sorted((data / "updates" / "transactions").glob("*/transaction.json"))
                self.assertEqual(len(transactions), 1)
                journal = json.loads(transactions[0].read_text(encoding="utf-8"))
                self.assertEqual(journal["phase"], "rolled_back")
                self.assertEqual(journal["recovery"], "restore-old")
                self.assertFalse(staged.exists())
                self.assertEqual(list((data / "runtime").glob("update-startup-*.json")), [])

                update_log = (data / "runtime" / "update.log").read_text(encoding="utf-8")
                self.assertIn("update applied", update_log)
                self.assertIn("forced finalize failure after healthy candidate", update_log)
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
