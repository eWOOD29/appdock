from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import appdock
from scripts import build_portable
from scripts import update_helper


REPO_ROOT = Path(__file__).parents[1].resolve()


LAUNCHER_SOURCE = r'''
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import appdock

staged, install, data, metadata, port = map(Path, sys.argv[1:])
process = appdock.launch_update_helper(
    staged,
    install,
    data,
    current_pid=os.getpid(),
    restart_args=[
        "--host=127.0.0.1",
        f"--port={port}",
        f"--data-dir={data}",
    ],
    expected_identity=appdock._read_staged_receipt(
        appdock.AppDockConfig.from_environment(data_dir=data)
    ),
)
Path(metadata).write_text(json.dumps({"helper_pid": process.pid}), encoding="utf-8")
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def _terminate_tree(pid: int) -> None:
    if pid > 0:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )


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
            pass
    return pids


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path.name}")


def _wait_for_health(port: int, timeout: float = 30.0) -> dict[str, object]:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok") is True:
                return payload
        except (OSError, ValueError, TypeError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for candidate health: {last_error}")


def _source_version_for_test(path: Path) -> str:
    marker = 'CURRENT_VERSION = "'
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(marker):
            return line[len(marker):].split('"', 1)[0]
    return "unknown"


def _extract_archive(payload: bytes, destination: Path) -> None:
    appdock.validate_zip(payload)
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(__import__("io").BytesIO(payload)) as archive:
        archive.extractall(destination)


def _make_old_install(candidate_zip: Path, install: Path) -> None:
    _extract_archive(candidate_zip.read_bytes(), install)
    source = install / "appdock.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            f'CURRENT_VERSION = "{appdock.CURRENT_VERSION}"',
            'CURRENT_VERSION = "0.2.1"',
        ),
        encoding="utf-8",
    )
    manifest_path = install / appdock.RELEASE_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["files"]:
        if item["path"] == "appdock.py":
            item["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _write_minimal_release_tree(root: Path, marker: bytes) -> None:
    members = {
        "appdock.py": marker,
        "static/app.js": b"js",
        "static/app.css": b"css",
        "scripts/update_helper.py": b"helper",
        "scripts/path_safety.ps1": b"safety",
        "scripts/install.ps1": b"install",
        "scripts/uninstall.ps1": b"uninstall",
    }
    for relative, content in members.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / appdock.RELEASE_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "files": [
                    {"path": relative, "sha256": hashlib.sha256(content).hexdigest()}
                    for relative, content in sorted(members.items())
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


class RealExternalHelperCwdRegressionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "external helper cwd regression is Windows-specific")
    def test_real_parent_helper_swap_candidate_finalize_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            candidate_zip = root / "candidate.zip"
            staged = data / "updates" / appdock.CURRENT_VERSION
            launcher = root / "launcher.py"
            metadata = root / "helper.json"
            build_portable.build_archive(candidate_zip)
            _make_old_install(candidate_zip, install)
            _extract_archive(candidate_zip.read_bytes(), staged)
            zip_sha256 = hashlib.sha256(candidate_zip.read_bytes()).hexdigest()
            staged_identity = appdock._staged_identity(staged, zip_sha256=zip_sha256)
            staged_result = {
                "staged": True,
                "version": appdock.CURRENT_VERSION,
                "path": str(staged),
                "identity": staged_identity,
                "digest": appdock._staged_record_digest(appdock.CURRENT_VERSION, staged_identity),
            }
            appdock._write_staged_receipt(config, staged_result)
            launcher.write_text(LAUNCHER_SOURCE, encoding="utf-8")
            port = _free_port()
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(REPO_ROOT)
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(launcher),
                    str(staged),
                    str(install),
                    str(data),
                    str(metadata),
                    str(port),
                ],
                cwd=str(install),
                env=environment,
                shell=False,
                close_fds=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            helper_pid = 0
            try:
                try:
                    _wait_for(metadata)
                except AssertionError:
                    parent.wait(timeout=10)
                    stdout = parent.stdout.read() if parent.stdout else ""
                    stderr = parent.stderr.read() if parent.stderr else ""
                    if parent.stdout:
                        parent.stdout.close()
                    if parent.stderr:
                        parent.stderr.close()
                    self.fail(f"launcher failed before handshake: rc={parent.returncode} stdout={stdout!r} stderr={stderr!r}")
                helper_pid = int(json.loads(metadata.read_text(encoding="utf-8"))["helper_pid"])
                parent.wait(timeout=15)
                parent_stdout = parent.stdout.read() if parent.stdout else ""
                parent_stderr = parent.stderr.read() if parent.stderr else ""
                if parent.stdout:
                    parent.stdout.close()
                if parent.stderr:
                    parent.stderr.close()
                self.assertEqual(parent.returncode, 0, parent_stderr or parent_stdout)
                try:
                    health = _wait_for_health(port)
                except AssertionError:
                    transaction_text = ""
                    for transaction_path in sorted((data / "updates" / "transactions").glob("*/transaction.json")):
                        transaction_text += transaction_path.read_text(encoding="utf-8")
                    update_log = (data / "runtime" / "update.log").read_text(encoding="utf-8") if (data / "runtime" / "update.log").is_file() else ""
                    stdout_path = data / "runtime" / "update-restart.stdout.log"
                    stderr_path = data / "runtime" / "update-restart.stderr.log"
                    restart_stdout = stdout_path.read_text(encoding="utf-8") if stdout_path.is_file() else ""
                    restart_stderr = stderr_path.read_text(encoding="utf-8") if stderr_path.is_file() else ""
                    self.fail(
                        "candidate did not become healthy; "
                        f"install_version={_source_version_for_test(install / 'appdock.py')!r} "
                        f"transactions={transaction_text!r} log={update_log!r} "
                        f"restart_stdout={restart_stdout!r} restart_stderr={restart_stderr!r}"
                    )
                self.assertEqual(health["service"], "appdock")
                self.assertEqual(health["version"], appdock.CURRENT_VERSION)
                self.assertEqual(json.loads((install / appdock.RELEASE_MANIFEST_NAME).read_text(encoding="utf-8"))["schema_version"], 2)
                transactions = sorted((data / "updates" / "transactions").glob("*/transaction.json"))
                self.assertEqual(len(transactions), 1)
                journal = json.loads(transactions[0].read_text(encoding="utf-8"))
                self.assertEqual(journal["phase"], "complete")
                self.assertEqual(journal["recovery"], "finish-new")
                self.assertFalse(staged.exists())
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    listing = _listening_pids(port)
                    if not listing:
                        break
                    time.sleep(0.2)
                for pid in _listening_pids(port):
                    _terminate_tree(pid)
                self.assertEqual(_listening_pids(port), set())
                self.assertTrue(helper_pid > 0)
                helper_listing = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {helper_pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertNotIn(str(helper_pid), helper_listing.stdout)
            finally:
                if parent.poll() is None:
                    _terminate_tree(parent.pid)
                if helper_pid:
                    _terminate_tree(helper_pid)
                for pid in _listening_pids(port):
                    _terminate_tree(pid)
                parent.wait(timeout=10)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
                    stream.bind(("127.0.0.1", port))

    def test_swap_failure_preserves_sanitized_causal_evidence_after_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")

            original_move = appdock._bound_directory_move
            failed = False

            def fail_install_swap(source: str | Path, destination: str | Path, **kwargs) -> None:
                nonlocal failed
                if not failed and Path(source).absolute() == install.absolute():
                    failed = True
                    raise PermissionError(13, "deliberate swap failure")
                original_move(source, destination, **kwargs)

            with patch.object(appdock, "_bound_directory_move", side_effect=fail_install_swap):
                with self.assertRaisesRegex(appdock.AppDockError, "^update failed and was rolled back$"):
                    appdock.apply_update(staged, install, data)

            transactions = sorted((data / "updates" / "transactions").glob("*/transaction.json"))
            self.assertEqual(len(transactions), 1)
            transaction = transactions[0]
            journal = json.loads(transaction.read_text(encoding="utf-8"))
            evidence_path = transaction.with_name("failure.json")
            self.assertTrue(evidence_path.is_file())
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["operation_id"], journal["operation_id"])
            self.assertEqual(evidence["phase"], "swapping")
            self.assertEqual(evidence["failing_operation"], "replace-install-with-backup")
            self.assertEqual(evidence["exception_type"], "PermissionError")
            self.assertEqual(evidence["errno"], 13)
            self.assertIsNone(evidence["winerror"])
            self.assertRegex(evidence["target_identity_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(evidence["rollback_phase"], "restore-old")
            self.assertEqual(evidence["rollback_outcome"], "rolled_back")
            self.assertNotIn(str(root), evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(journal["phase"], "rolled_back")
            self.assertEqual(journal["recovery"], "restore-old")

    def test_recovery_failure_raises_fixed_error_and_records_bounded_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")

            original_move = appdock._bound_directory_move
            failed = False

            def fail_install_swap(source: str | Path, destination: str | Path, **kwargs) -> None:
                nonlocal failed
                if not failed and Path(source).absolute() == install.absolute():
                    failed = True
                    raise PermissionError(13, "SECRET C:/private/path")
                original_move(source, destination, **kwargs)

            with (
                patch.object(appdock, "_bound_directory_move", side_effect=fail_install_swap),
                patch.object(appdock, "_recover_one_update", side_effect=RuntimeError("SECRET C:/private/path")),
            ):
                with self.assertRaisesRegex(appdock.AppDockError, "^update failed and recovery did not complete$") as raised:
                    appdock.apply_update(staged, install, data)

            self.assertEqual(str(raised.exception), "update failed and recovery did not complete")
            self.assertNotIn("SECRET", str(raised.exception))
            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            evidence_path = transaction.with_name("failure.json")
            evidence_text = evidence_path.read_text(encoding="utf-8")
            evidence = json.loads(evidence_text)
            self.assertEqual(evidence["phase"], "swapping")
            self.assertEqual(evidence["rollback_phase"], "restore-old")
            self.assertEqual(evidence["rollback_outcome"], "failed")
            self.assertEqual(evidence["rollback_failure_type"], "RuntimeError")
            self.assertIsNone(evidence["rollback_failure_errno"])
            self.assertIsNone(evidence["rollback_failure_winerror"])
            self.assertNotIn("SECRET", evidence_text)
            self.assertNotIn(str(root), evidence_text)

    def test_committed_recovery_failure_evidence_records_finish_new_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            appdock.AppDockConfig.from_environment(data_dir=data).ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")

            with (
                patch.object(appdock, "_recover_one_update", side_effect=RuntimeError("recovery failed")),
                self.assertRaisesRegex(appdock.AppDockError, "^update failed and recovery did not complete$"),
            ):
                appdock.apply_update(
                    staged,
                    install,
                    data,
                    restart=lambda: (_ for _ in ()).throw(RuntimeError("restart failed")),
                )

            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            evidence = json.loads(transaction.with_name("failure.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["phase"], "committed")
            self.assertEqual(evidence["rollback_phase"], "finish-new")
            self.assertEqual(evidence["rollback_outcome"], "failed")
            self.assertEqual(evidence["rollback_failure_type"], "RuntimeError")

    def test_restore_old_requires_backup_when_install_is_valid_target_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            appdock.AppDockConfig.from_environment(data_dir=data).ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"target app")
            _write_minimal_release_tree(staged, b"target app")
            paths = appdock._transaction_paths(install, data, "a" * 32)
            identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            paths["tx_root"].mkdir(parents=True)
            appdock._durable_write_json(
                paths["journal"],
                {
                    "schema_version": 2,
                    "operation_id": "a" * 32,
                    "install": str(install),
                    "candidate": str(paths["candidate"]),
                    "backup": str(paths["backup"]),
                    "old_exists": True,
                    "phase": "swapping",
                    "recovery": "restore-old",
                    "files": sorted(item["path"] for item in identity["inventory"]),
                    "preexisting": sorted(item["path"] for item in identity["inventory"]),
                    "identity": identity,
                },
            )

            with self.assertRaisesRegex(appdock.AppDockError, "^update backup is missing during recovery$"):
                appdock._recover_one_update(paths["tx_root"], install=install, data=data)

            self.assertTrue(install.is_dir())
            self.assertEqual((install / "appdock.py").read_bytes(), b"target app")
            persisted = json.loads(paths["journal"].read_text(encoding="utf-8"))
            self.assertEqual(persisted["phase"], "swapping")
            self.assertEqual(persisted["recovery"], "restore-old")

    def test_restore_old_validates_corrupt_backup_before_displacing_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            appdock.AppDockConfig.from_environment(data_dir=data).ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"target app")
            _write_minimal_release_tree(staged, b"target app")
            paths = appdock._transaction_paths(install, data, "b" * 32)
            _write_minimal_release_tree(paths["backup"], b"old app")
            (paths["backup"] / "appdock.py").write_bytes(b"corrupt old app")
            identity = appdock._staged_identity(staged, zip_sha256=None, complete=False)
            paths["tx_root"].mkdir(parents=True)
            appdock._durable_write_json(
                paths["journal"],
                {
                    "schema_version": 2,
                    "operation_id": "b" * 32,
                    "install": str(install),
                    "candidate": str(paths["candidate"]),
                    "backup": str(paths["backup"]),
                    "old_exists": True,
                    "phase": "swapping",
                    "recovery": "restore-old",
                    "files": sorted(item["path"] for item in identity["inventory"]),
                    "preexisting": sorted(item["path"] for item in identity["inventory"]),
                    "identity": identity,
                },
            )

            with self.assertRaisesRegex(appdock.AppDockError, "release file checksum does not match its inventory"):
                appdock._recover_one_update(paths["tx_root"], install=install, data=data)

            self.assertTrue(paths["backup"].is_dir())
            self.assertEqual((paths["backup"] / "appdock.py").read_bytes(), b"corrupt old app")
            self.assertTrue(install.is_dir())
            self.assertEqual((install / "appdock.py").read_bytes(), b"target app")
            persisted = json.loads(paths["journal"].read_text(encoding="utf-8"))
            self.assertEqual(persisted["phase"], "swapping")
            self.assertEqual(persisted["recovery"], "restore-old")

    def test_restart_callback_failure_records_committed_finish_new_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")

            with self.assertRaisesRegex(appdock.AppDockError, "^update failed after commit; recovery completed$"):
                appdock.apply_update(
                    staged,
                    install,
                    data,
                    restart=lambda: (_ for _ in ()).throw(RuntimeError("restart failed")),
                )

            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            evidence = json.loads(transaction.with_name("failure.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["phase"], "committed")
            self.assertEqual(evidence["failing_operation"], "restart-callback")
            self.assertEqual(evidence["rollback_phase"], "finish-new")
            self.assertEqual(evidence["rollback_outcome"], "complete")
            self.assertEqual(json.loads(transaction.read_text(encoding="utf-8"))["phase"], "complete")
            self.assertEqual((install / "appdock.py").read_bytes(), b"new app")

    def test_finalize_failure_records_committed_phase_and_finish_new_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")

            with (
                patch.object(appdock, "finalize_update", side_effect=RuntimeError("finalize failed")),
                self.assertRaisesRegex(appdock.AppDockError, "^update failed after commit; recovery completed$") as raised,
            ):
                appdock.apply_update(staged, install, data, restart=lambda: None)

            self.assertNotIn("finalize failed", str(raised.exception))
            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            evidence = json.loads(transaction.with_name("failure.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["phase"], "committed")
            self.assertEqual(evidence["failing_operation"], "finalize-update")
            self.assertEqual(evidence["rollback_phase"], "finish-new")
            self.assertEqual(evidence["rollback_outcome"], "complete")

    def test_keyboard_interrupt_is_reraised_after_successful_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            config = appdock.AppDockConfig.from_environment(data_dir=data)
            config.ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")

            with self.assertRaises(KeyboardInterrupt):
                appdock.apply_update(
                    staged,
                    install,
                    data,
                    restart=lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
                )

            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            self.assertEqual(json.loads(transaction.read_text(encoding="utf-8"))["phase"], "complete")

    def test_helper_rejects_effective_cwd_inside_mutable_tree_before_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            staged = root / "data" / "updates" / "0.3.0"
            data = root / "data"
            install.mkdir(parents=True)
            staged.mkdir(parents=True)
            with patch.object(update_helper.Path, "cwd", return_value=install):
                with self.assertRaisesRegex(appdock.AppDockError, "trusted updater working directory"):
                    update_helper._validate_effective_cwd(install, staged, data)


class Generation2ReviewBlockerTests(unittest.TestCase):
    def test_exception_metadata_is_fail_closed_for_hostile_exception_values(self) -> None:
        class HostileMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise RuntimeError("secret type metadata")
                return super().__getattribute__(name)

        class HostileFailure(Exception, metaclass=HostileMeta):
            @property
            def winerror(self):
                raise SystemExit("secret winerror")

        class HostileInt(int):
            def __le__(self, _other):
                raise RuntimeError("secret errno comparison")

        failure = HostileFailure()
        failure.errno = HostileInt(13)

        self.assertEqual(appdock._bounded_exception_metadata(failure), ("Exception", None, None))

    def test_evidence_persistence_failure_is_fixed_public_error_and_retains_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            appdock.AppDockConfig.from_environment(data_dir=data).ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")

            original_write = appdock._durable_write_json
            original_strict_write = appdock._durable_write_json_strict

            def fail_failure_evidence(path, payload):
                if Path(path).name == "failure.json":
                    raise OSError("SECRET C:/private/evidence-path")
                return original_write(path, payload)

            def fail_strict_failure_evidence(path, payload):
                if Path(path).name == "failure.json":
                    raise OSError("SECRET C:/private/evidence-path")
                return original_strict_write(path, payload)

            original_move = appdock._bound_directory_move
            failed = False

            def fail_install_swap(source, destination, **kwargs):
                nonlocal failed
                if not failed and Path(source).absolute() == install.absolute():
                    failed = True
                    raise PermissionError(13, "swap failed")
                return original_move(source, destination, **kwargs)

            with (
                patch.object(appdock, "_bound_directory_move", side_effect=fail_install_swap),
                patch.object(appdock, "_durable_write_json", side_effect=fail_failure_evidence),
                patch.object(appdock, "_durable_write_json_strict", side_effect=fail_strict_failure_evidence),
                self.assertRaisesRegex(
                    appdock.AppDockError,
                    "^update failed and failure evidence could not be persisted$",
                ) as raised,
            ):
                appdock.apply_update(staged, install, data)

            self.assertEqual(str(raised.exception), "update failed and failure evidence could not be persisted")
            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            self.assertEqual(json.loads(transaction.read_text(encoding="utf-8"))["phase"], "rolled_back")
            self.assertFalse(transaction.with_name("failure.json").exists())
            self.assertTrue(install.is_dir())

    def test_evidence_system_exit_survives_ordinary_primary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            appdock.AppDockConfig.from_environment(data_dir=data).ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")
            original_write = appdock._durable_write_json
            original_strict_write = appdock._durable_write_json_strict
            original_move = appdock._bound_directory_move
            failed = False

            def fail_failure_evidence(path, payload):
                if Path(path).name == "failure.json":
                    raise SystemExit("evidence control failure")
                return original_write(path, payload)

            def fail_strict_failure_evidence(path, payload):
                if Path(path).name == "failure.json":
                    raise SystemExit("evidence control failure")
                return original_strict_write(path, payload)

            def fail_install_swap(source, destination, **kwargs):
                nonlocal failed
                if not failed and Path(source).absolute() == install.absolute():
                    failed = True
                    raise PermissionError(13, "swap failed")
                return original_move(source, destination, **kwargs)

            with (
                patch.object(appdock, "_bound_directory_move", side_effect=fail_install_swap),
                patch.object(appdock, "_durable_write_json", side_effect=fail_failure_evidence),
                patch.object(appdock, "_durable_write_json_strict", side_effect=fail_strict_failure_evidence),
                self.assertRaises(SystemExit) as raised,
            ):
                appdock.apply_update(staged, install, data)

            self.assertEqual(str(raised.exception), "evidence control failure")

    def test_primary_system_exit_wins_over_evidence_control_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            appdock.AppDockConfig.from_environment(data_dir=data).ensure()
            install = root / "install"
            staged = data / "updates" / "0.3.0"
            _write_minimal_release_tree(install, b"old app")
            _write_minimal_release_tree(staged, b"new app")
            original_write = appdock._durable_write_json

            def fail_failure_evidence(path, payload):
                if Path(path).name == "failure.json":
                    raise SystemExit("evidence control failure")
                return original_write(path, payload)

            with patch.object(appdock, "_durable_write_json", side_effect=fail_failure_evidence):
                with self.assertRaises(SystemExit) as raised:
                    appdock.apply_update(
                        staged,
                        install,
                        data,
                        restart=lambda: (_ for _ in ()).throw(SystemExit("primary control failure")),
                    )

            self.assertEqual(str(raised.exception), "primary control failure")
            transaction = next((data / "updates" / "transactions").glob("*/transaction.json"))
            self.assertEqual(json.loads(transaction.read_text(encoding="utf-8"))["phase"], "complete")

    def test_committed_recovery_requires_verified_install_or_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            appdock.AppDockConfig.from_environment(data_dir=data).ensure()
            install = root / "install"
            operation_id = "c" * 32
            paths = appdock._transaction_paths(install, data, operation_id)
            _write_minimal_release_tree(paths["backup"], b"old app")
            paths["evidence_backup"].mkdir(parents=True)
            sentinel = paths["evidence_backup"] / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            identity = appdock._staged_identity(paths["backup"], zip_sha256=None, complete=False)
            journal = {
                "schema_version": 2,
                "operation_id": operation_id,
                "install": str(install),
                "candidate": str(paths["candidate"]),
                "backup": str(paths["backup"]),
                "old_exists": True,
                "phase": "committed",
                "recovery": "finish-new",
                "files": sorted(item["path"] for item in identity["inventory"]),
                "preexisting": sorted(item["path"] for item in identity["inventory"]),
                "identity": identity,
            }
            paths["tx_root"].mkdir(parents=True)
            appdock._durable_write_json(paths["journal"], journal)

            with self.assertRaisesRegex(appdock.AppDockError, "^update recovery cannot finish-new without a verified installation$"):
                appdock._recover_one_update(paths["tx_root"], install=install, data=data)

            self.assertFalse(install.exists())
            self.assertTrue(paths["backup"].is_dir())
            self.assertTrue(sentinel.is_file())
            persisted = json.loads(paths["journal"].read_text(encoding="utf-8"))
            self.assertEqual(persisted["phase"], "committed")
            self.assertEqual(persisted["recovery"], "finish-new")

    def test_main_rejects_unsafe_cwd_before_creating_data_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            install.mkdir()
            data = root / "absent-data"
            args = [
                "--staged", str(root / "staged"),
                "--install", str(install),
                "--data", str(data),
                "--pid", "0",
                "--restart-script", str(root / "restart.py"),
                "--handshake", str(data / "runtime" / ("update-helper-" + "a" * 32 + ".ready")),
                "--handshake-token", "helper-token-1234567890123456",
                "--expected-version", "0.2.2-beta.3",
                "--expected-digest", "a" * 64,
                "--expected-zip-sha256", "b" * 64,
                "--expected-inventory-sha256", "c" * 64,
                "--expected-helper-sha256", "d" * 64,
            ]
            with patch.object(update_helper.Path, "cwd", return_value=install):
                with self.assertRaisesRegex(appdock.AppDockError, "trusted updater working directory"):
                    update_helper.main(args)
            self.assertFalse(data.exists())
            self.assertFalse((data / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
