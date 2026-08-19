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
    _terminate_and_reap,
)
from windows_process_tree import (
    descendants_or_self,
    identities_subset,
    identities_for_pids,
    process_identity,
    running_identities,
    snapshot_processes,
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
            staged = data / "updates" / "0.2.2-beta.2"
            _extract_zip(candidate_bytes, staged)
            self.assertEqual(_source_version(staged / "appdock.py"), "0.2.2-beta.2")
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
            real_stop = update_helper._stop_restarted_process
            observations: list[dict[str, object]] = []
            candidate_process: subprocess.Popen[object] | None = None
            old_process: subprocess.Popen[object] | None = None
            finalize_observation: dict[str, object] = {}
            stop_observation: dict[str, object] = {}

            def observing_launch(restart_script, install_arg, restart_args_arg, **kwargs):
                nonlocal candidate_process, old_process
                use_handoff = bool(kwargs.get("use_startup_handoff", True))
                phase = "candidate" if use_handoff else "restored-old"
                active_version = _source_version(install / "appdock.py")
                active_sha = hashlib.sha256((install / "appdock.py").read_bytes()).hexdigest()
                lock_probe = _probe_lock(data, install)
                before_snapshot = snapshot_processes()
                before_listeners = sorted(_listening_pids(port))
                candidate_running_before_old = None
                candidate_survivors_before_old = None
                if phase == "restored-old":
                    self.assertIsNotNone(candidate_process)
                    candidate_running_before_old = candidate_process.poll() is None
                    candidate_tree = list(stop_observation["candidate_tree_before_stop"])
                    candidate_survivors_before_old = running_identities(candidate_tree, before_snapshot)

                process = real_launch(
                    restart_script,
                    install_arg,
                    restart_args_arg,
                    **kwargs,
                )
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                ready_snapshot = snapshot_processes()
                launch_identity = process_identity(process.pid, ready_snapshot)
                process_tree = list(descendants_or_self(launch_identity).values())
                ready_listeners = _listening_pids(port)
                listener_identities = identities_for_pids(ready_listeners, ready_snapshot)
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
                    "candidate_survivors_before_old": candidate_survivors_before_old,
                    "pid": process.pid,
                    "launch_identity": launch_identity,
                    "process_tree": process_tree,
                    "health": health,
                    "listeners_after_ready": sorted(ready_listeners),
                    "listener_identities_after_ready": listener_identities,
                }
                observations.append(observation)
                if phase == "candidate":
                    candidate_process = process
                else:
                    old_process = process
                return process

            def observing_stop(process):
                self.assertIs(process, candidate_process)
                before_snapshot = snapshot_processes()
                launch_identity = observations[0]["launch_identity"]
                candidate_tree = list(descendants_or_self(launch_identity).values())
                listeners_before = _listening_pids(port)
                listener_identities_before = identities_for_pids(listeners_before, before_snapshot)
                self.assertEqual(len(listener_identities_before), 1)
                self.assertTrue(identities_subset(listener_identities_before, candidate_tree))
                real_stop(process)
                after_snapshot = snapshot_processes()
                stop_observation.update(
                    candidate_tree_before_stop=candidate_tree,
                    survivors_after_stop=running_identities(candidate_tree, after_snapshot),
                    listeners_after_stop=sorted(_listening_pids(port)),
                )

            def fail_finalize(_result, _install, _data):
                self.assertIsNotNone(candidate_process)
                with urllib.request.urlopen(health_url, timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                finalize_snapshot = snapshot_processes()
                candidate_tree = list(observations[0]["process_tree"])
                finalize_listeners = _listening_pids(port)
                finalize_observation.update(
                    health=health,
                    candidate_pid=candidate_process.pid,
                    candidate_poll=candidate_process.poll(),
                    candidate_survivors=running_identities(candidate_tree, finalize_snapshot),
                    listening=sorted(finalize_listeners),
                    listener_identities=identities_for_pids(finalize_listeners, finalize_snapshot),
                )
                raise RuntimeError("forced finalize failure after healthy candidate")

            try:
                with (
                    patch.object(update_helper, "_launch_and_wait", side_effect=observing_launch),
                    patch.object(update_helper, "_stop_restarted_process", side_effect=observing_stop),
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
                self.assertEqual(
                    [item["phase"] for item in observations],
                    ["candidate", "restored-old"],
                    (data / "runtime" / "update.log").read_text(encoding="utf-8"),
                )

                candidate = observations[0]
                restored = observations[1]
                candidate_pid = int(candidate["pid"])

                self.assertEqual(candidate["version"], "0.2.2-beta.2")
                self.assertEqual(candidate["app_sha256"], candidate_app_sha)
                self.assertTrue(candidate["use_startup_handoff"])
                self.assertNotEqual(candidate["lock_returncode"], 0)
                candidate_health = dict(candidate["health"])
                self.assertTrue(candidate_health["ok"])
                self.assertEqual(candidate_health["service"], "appdock")
                self.assertEqual(candidate_health["version"], "0.2.2-beta.2")
                candidate_listeners = set(candidate["listeners_after_ready"])
                candidate_listener_identities = set(candidate["listener_identities_after_ready"])
                candidate_tree = list(candidate["process_tree"])
                self.assertEqual(len(candidate_listeners), 1)
                self.assertEqual(len(candidate_listener_identities), 1)
                self.assertTrue(identities_subset(candidate_listener_identities, candidate_tree))

                self.assertEqual(finalize_observation["candidate_pid"], candidate_pid)
                self.assertIsNone(finalize_observation["candidate_poll"])
                finalize_listeners = set(finalize_observation["listening"])
                finalize_listener_identities = set(finalize_observation["listener_identities"])
                finalize_survivors = list(finalize_observation["candidate_survivors"])
                self.assertEqual(len(finalize_listeners), 1)
                self.assertEqual(len(finalize_listener_identities), 1)
                self.assertTrue(identities_subset(finalize_listener_identities, finalize_survivors))
                self.assertTrue(stop_observation["candidate_tree_before_stop"])
                self.assertEqual(stop_observation["survivors_after_stop"], [])
                self.assertEqual(stop_observation["listeners_after_stop"], [])

                self.assertEqual(restored["version"], "0.2.1")
                self.assertEqual(restored["app_sha256"], old_app_sha)
                self.assertFalse(restored["use_startup_handoff"])
                self.assertFalse(restored["candidate_running_before_old"])
                self.assertEqual(restored["candidate_survivors_before_old"], [])
                self.assertEqual(restored["listeners_before_launch"], [])
                self.assertEqual(restored["lock_returncode"], 0, restored)
                self.assertEqual(restored["lock_stdout"], "acquired")
                old_health = dict(restored["health"])
                self.assertTrue(old_health["ok"])
                self.assertEqual(old_health["service"], "appdock")
                self.assertEqual(old_health["version"], "0.2.1")
                self.assertTrue(old_health.get("ready_token"))
                restored_listeners = set(restored["listeners_after_ready"])
                restored_listener_identities = set(restored["listener_identities_after_ready"])
                restored_tree = list(restored["process_tree"])
                self.assertEqual(len(restored_listeners), 1)
                self.assertEqual(len(restored_listener_identities), 1)
                self.assertTrue(identities_subset(restored_listener_identities, restored_tree))
                self.assertNotEqual(candidate_health["ready_token"], old_health["ready_token"])

                self.assertIsNotNone(candidate_process)
                self.assertIsNotNone(old_process)
                self.assertIsNotNone(candidate_process.poll())
                self.assertIsNone(old_process.poll())
                final_listeners = _listening_pids(port)
                final_snapshot = snapshot_processes()
                final_listener_identities = identities_for_pids(final_listeners, final_snapshot)
                final_old_tree = descendants_or_self(restored["launch_identity"])
                self.assertEqual(len(final_listeners), 1)
                self.assertEqual(len(final_listener_identities), 1)
                self.assertTrue(identities_subset(final_listener_identities, final_old_tree.values()))
                shutdown_candidate_tree = list(stop_observation["candidate_tree_before_stop"])
                self.assertEqual(running_identities(shutdown_candidate_tree, final_snapshot), [])

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
                    _terminate_and_reap(old_process)
                if candidate_process is not None:
                    _terminate_and_reap(candidate_process)


if __name__ == "__main__":
    unittest.main()
