from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import appdock
from scripts.emit_windows_path_fixture import build_fixture


class AppManagerDiscoveryPathContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture"
        self.preview = build_fixture(self.fixture)
        self.config = appdock.AppDockConfig.from_environment(data_dir=self.root / "data")
        self.receipt = appdock.import_private_package(self.fixture / "source", self.config, expected_digest=self.preview["digest"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_windows_discovery(self) -> dict[str, appdock.AppSpec]:
        manager = appdock.AppManager(config=self.config)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("external application started")):
            specs = manager.discover()
        self.assertEqual(len(specs), 8)
        for index in range(8):
            app_id = f"synthetic-{index}" if index < 7 else "synthetic-backend"
            directory = rf"C:\Synthetic\Application{index}"
            spec = specs[app_id]
            self.assertEqual(spec.path_flavor, "windows")
            self.assertIsInstance(spec.directory, str)
            self.assertIsInstance(spec.cwd, str)
            self.assertEqual(spec.directory, directory)
            self.assertEqual(spec.cwd, directory + r"\runtime\worker")
            self.assertNotIn(str(self.config.registry_root), spec.directory)
            self.assertNotIn(str(self.config.registry_root), spec.cwd)
        self.assertEqual([item["id"] for item in manager.all_status()], [f"synthetic-{index}" for index in range(7)])
        return specs

    def test_appmanager_discovery_preserves_windows_contract_for_all_eight_registrations(self) -> None:
        self.assert_windows_discovery()

    def test_idempotence_rollback_reimport_and_startup_recovery_preserve_provenance(self) -> None:
        second = appdock.import_private_package(self.fixture / "source", self.config, expected_digest=self.preview["digest"])
        self.assertFalse(second["changed"])
        self.assert_windows_discovery()
        self.assertTrue(appdock.rollback_private_package(self.receipt["receipt"], self.config)["rolled_back"])
        third = appdock.import_private_package(self.fixture / "source", self.config, expected_digest=self.preview["digest"])
        self.assertTrue(third["changed"])
        self.assert_windows_discovery()
        self.assert_windows_discovery()

    def test_imported_manifest_persists_bounded_provenance(self) -> None:
        for app_id in [f"synthetic-{index}" for index in range(7)] + ["synthetic-backend"]:
            raw = json.loads((self.config.registry_root / app_id / appdock.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertIs(raw.get("_appdock_private_import"), True)
            self.assertEqual(raw.get("path_flavor"), "windows")

    def test_missing_contradictory_or_unsupported_provenance_fails_closed(self) -> None:
        cases = {
            "synthetic-0": lambda raw: raw.pop("path_flavor"),
            "synthetic-1": lambda raw: raw.__setitem__("path_flavor", "native"),
            "synthetic-2": lambda raw: raw.__setitem__("_appdock_private_import", False),
            "synthetic-3": lambda raw: raw.__setitem__("external", False),
        }
        for app_id, mutate in cases.items():
            path = self.config.registry_root / app_id / appdock.MANIFEST_NAME
            raw = json.loads(path.read_text(encoding="utf-8"))
            mutate(raw)
            path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        specs = appdock.AppManager(config=self.config).discover()
        for app_id in cases:
            self.assertNotIn(app_id, specs)
        self.assertEqual(len(specs), 4)

    def test_runtime_boundary_is_mocked_and_unsupported_hosts_fail_closed(self) -> None:
        spec = self.assert_windows_discovery()["synthetic-0"]
        if os.name == "nt":
            native = appdock._native_runtime_cwd(spec)
            self.assertIsInstance(native, Path)
            self.assertEqual(str(native), str(spec.cwd))
            return
        manager = appdock.AppManager(config=self.config)
        with mock.patch.object(manager, "log_path", side_effect=AssertionError("log reached")) as log_path, mock.patch.object(manager, "_external_pids", side_effect=AssertionError("process reached")) as external_pids, mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess reached")) as popen:
            for operation in (manager.start, manager.stop, manager.restart):
                with self.assertRaisesRegex(appdock.AppDockError, "unsupported on this host"):
                    operation("synthetic-0")
        log_path.assert_not_called()
        external_pids.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
