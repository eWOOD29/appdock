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
    _extract_zip,
    _free_loopback_port,
    _listening_pids,
    _managed_snapshot,
    _source_version,
    _terminate_tree,
)


def _probe_lock(data: Path, install: Path) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys, appdock; "
        "\ntry:\n lock=appdock.acquire_update_lock(sys.argv[1]); lock.release()"
        "\nexcept Exception as exc:\n print(type(exc).__name__ + ':' + str(exc)); raise SystemExit(3)"
        "\nprint('acquired')"
    )
    return subprocess.run(
        [sys.executable, "-B", "-c", code, str(data)],
        cwd=str(install),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


class RealFinalizeRollbackProcessProofTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "real finalize rollback proof is Windows-specific")
    def test_healthy_candidate_is_stopped_before_finalize_failure_rollback_and_old_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"

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
            real_launch = update_helper._launch_and_wait
            observations: list[dict[str, object]] = []
            candidate_process: subprocess.Popen[object] | None = None
            old_process: subprocess.Popen[object] | None = None
            finalize_observation: dict[str, object] = {}

            def observing_launch(restart_script, install_arg, restart_args_arg, **kwargs):
                nonlocal candidate_process, old_process
                use_handoff = bool(kwargs.get("use_startup_handoff", True))
                phase = "candidate" if use_handoff else "restored-old"
                active_version = _source_version(install / "appdock.py")
                active_sha = hashlib.sha256((install / "appdock.py").read_bytes()).hexdigest()
                lock_probe = _probe_lock(data, install)
                before_listeners = sorted(_listening_pids(port))
                candidate_running_before_old = None
                if phase == "restored-old":
                    self.assertIsNotNone(candidate_process)
                    candidate_running_before_old = candidate_process.poll() is None

                process = real_launch(
                    restart_script,
                    install_arg,
                    restart_args_arg,
                    **kwargs,
                )
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                observation = {
                    "phase": phase,
                    "version": active_version,
                    "app_sha256": active_sha,
                    "use_startup_handoff": use_handoff,
                    "lock_returncode": lock_probe.returncode,
                    "lock_stdout": lock_probe.stdout.strip(),
                    "lock_stderr": lock_probe.stderr.strip(),
                    "listeners_before_launch": before_listeners,
                    "candidate_running_before_old": candidate_running_before_old,
                    "pid": process.pid,
                    "health": health,
                    "listeners_after_ready": sorted(_listening_pids(port)),
                }
                observations.append(observation)
                if phase == "candidate":
                    candidate_process = process
                else:
                    old_process = process
                return process

            def fail_finalize(_result, _install, _data):
                self.assertIsNotNone(candidate_process)
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                finalize_observation.update(
                    health=health,
                    candidate_pid=candidate_process.pid,
                    candidate_poll=candidate_process.poll(),
                    listening=sorted(_listening_pids(port)),
                )
                raise RuntimeError("forced finalize failure after healthy candidate")

            try:
                with (
                    patch.object(update_helper, "_launch_and_wait", side_effect=observing_launch),
                    patch.object(update_helper, "finalize_update", side_effect=fail_finalize),
                ):
                    result = update_helper.run(
                        staged,
                        install,
                        data,
                        0,
                        install / "appdock.py",
                        restart_args,
                    )
                self.assertEqual(result, 1)
                self.assertEqual([item["phase"] for item in observations], ["candidate", "restored-old"])

                candidate = observations[0]
                restored = observations[1]
                candidate_pid = int(candidate["pid"])
                old_pid = int(restored["pid"])

                self.assertEqual(candidate["version"], "0.2.2-beta.1")
                self.assertEqual(candidate["app_sha256"], candidate_app_sha)
                self.assertTrue(candidate["use_startup_handoff"])
                self.assertNotEqual(candidate["lock_returncode"], 0)
                candidate_health = dict(candidate["health"])
                self.assertTrue(candidate_health["ok"])
                self.assertEqual(candidate_health["service"], "appdock")
                self.assertEqual(candidate_health["version"], "0.2.2-beta.1")
                self.assertEqual(candidate["listeners_after_ready"], [candidate_pid])

                self.assertEqual(finalize_observation["candidate_pid"], candidate_pid)
                self.assertIsNone(finalize_observation["candidate_poll"])
                self.assertEqual(finalize_observation["listening"], [candidate_pid])

                self.assertEqual(restored["version"], "0.2.1")
                self.assertEqual(restored["app_sha256"], old_app_sha)
                self.assertFalse(restored["use_startup_handoff"])
                self.assertFalse(restored["candidate_running_before_old"])
                self.assertNotIn(candidate_pid, restored["listeners_before_launch"])
                self.assertEqual(restored["lock_returncode"], 0, restored)
                self.assertEqual(restored["lock_stdout"], "acquired")
                old_health = dict(restored["health"])
                self.assertTrue(old_health["ok"])
                self.assertEqual(old_health["service"], "appdock")
                self.assertEqual(old_health["version"], "0.2.1")
                self.assertTrue(old_health.get("ready_token"))
                self.assertEqual(restored["listeners_after_ready"], [old_pid])
                self.assertNotEqual(candidate_health["ready_token"], old_health["ready_token"])

                self.assertIsNotNone(candidate_process)
                self.assertIsNotNone(old_process)
                self.assertIsNotNone(candidate_process.poll())
                self.assertIsNone(old_process.poll())
                self.assertEqual(_listening_pids(port), {old_pid})

                inventory, extras = appdock._validate_installed_tree(install)
                self.assertEqual(extras, [])
                self.assertEqual(inventory, old_snapshot)
                self.assertEqual(_source_version(install / "appdock.py"), "0.2.1")
                self.assertEqual(hashlib.sha256((install / "appdock.py").read_bytes()).hexdigest(), old_app_sha)

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
                if old_process is not None:
                    _terminate_tree(old_process.pid)
                if candidate_process is not None:
                    _terminate_tree(candidate_process.pid)


if __name__ == "__main__":
    unittest.main()
