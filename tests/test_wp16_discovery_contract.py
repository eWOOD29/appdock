from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import appdock
from scripts.emit_windows_path_fixture import build_fixture


class AppManagerDiscoveryPathContractTests(unittest.TestCase):
    def test_appmanager_discovery_preserves_windows_contract_for_all_eight_registrations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture"
            preview = build_fixture(fixture)
            config = appdock.AppDockConfig.from_environment(data_dir=root / "data")
            appdock.import_private_package(
                fixture / "source",
                config,
                expected_digest=preview["digest"],
            )
            manager = appdock.AppManager(config=config)
            with mock.patch("subprocess.Popen", side_effect=AssertionError("external application started")):
                specs = manager.discover()
            self.assertEqual(len(specs), 8)
            for index in range(8):
                app_id = f"synthetic-{index}" if index < 7 else "synthetic-backend"
                expected = rf"C:\Synthetic\Application{index}"
                self.assertEqual(str(specs[app_id].directory), expected)
                self.assertEqual(str(specs[app_id].cwd), expected)
                self.assertNotIn(str(config.registry_root), str(specs[app_id].directory))
                self.assertNotIn(str(config.registry_root), str(specs[app_id].cwd))


if __name__ == "__main__":
    unittest.main()
