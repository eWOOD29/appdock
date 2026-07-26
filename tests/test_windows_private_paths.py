from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import appdock
from scripts.build_private_package import build_private_archive


class WindowsPrivatePathContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    def _write_hash_manifest(self, package: Path, migration_digest: str) -> None:
        files = []
        for path in sorted(package.rglob("*")):
            if not path.is_file() or path.name == appdock.PRIVATE_PACKAGE_HASH_MANIFEST:
                continue
            payload = path.read_bytes()
            files.append({
                "path": path.relative_to(package).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
        self._write_json(package / appdock.PRIVATE_PACKAGE_HASH_MANIFEST, {
            "schema_version": 1,
            "package": "AppDock private integration package",
            "migration_digest": migration_digest,
            "files": files,
        })

    def _base_registration(self, app_id: str, directory: str) -> dict[str, object]:
        return {
            "id": app_id,
            "name": app_id,
            "description": "synthetic fixture",
            "directory": directory,
            "external": True,
            "command": ["python", "-c", "raise SystemExit('must never execute')"],
            "cwd": ".",
            "port": None,
            "health_url": "",
            "local_url": "http://127.0.0.1:19000",
            "private_url": "https://private.example.invalid/app",
            "env": {},
            "process_name": "",
            "stop_timeout": 3.0,
        }

    def _write_schema2_package(self, package: Path, *, registration_count: int = 8) -> tuple[list[str], str]:
        ids = [f"synthetic-{index}" for index in range(registration_count - 1)] + ["synthetic-backend"]
        registration_paths: list[str] = []
        normalized: dict[str, dict[str, object]] = {}
        for index, app_id in enumerate(ids):
            path = package / "migration" / "registry" / app_id / "appdock.json"
            raw = self._base_registration(app_id, rf"C:\Synthetic\Application{index}")
            self._write_json(path, raw)
            registration_paths.append(path.relative_to(package).as_posix())
            normalized[app_id] = appdock.normalize_private_registration(
                raw,
                manifest_dir=path.parent,
                path_flavor="windows",
            )
        order = list(ids)
        extensions = {
            "schema_version": 1,
            "visibility": {"hidden_app_ids": ["synthetic-backend"]},
            "providers": [],
            "widgets": [],
        }
        self._write_json(package / "migration" / "app-order.json", order)
        self._write_json(package / "migration" / "extensions.json", extensions)
        self._write_json(package / appdock.PRIVATE_PACKAGE_MANIFEST, {
            "schema_version": 2,
            "path_flavor": "windows",
            "registrations": registration_paths,
            "order_path": "migration/app-order.json",
            "extension_config_path": "migration/extensions.json",
        })
        normalized_payload = {
            "schema_version": 2,
            "path_flavor": "windows",
            "registrations": {app_id: normalized[app_id] for app_id in sorted(normalized)},
            "order": order,
            "extensions": extensions,
        }
        digest = appdock._digest(normalized_payload)
        self._write_hash_manifest(package, digest)
        return ids, digest

    def _write_malformed_schema1_package(self, package: Path) -> None:
        app_id = "synthetic-app"
        path = package / "migration" / "registry" / app_id / "appdock.json"
        raw = self._base_registration(app_id, r"/C:\Synthetic\Application")
        self._write_json(path, raw)
        self._write_json(package / "migration" / "app-order.json", [app_id])
        extensions = {"schema_version": 1, "visibility": {"hidden_app_ids": []}, "providers": [], "widgets": []}
        self._write_json(package / "migration" / "extensions.json", extensions)
        self._write_json(package / appdock.PRIVATE_PACKAGE_MANIFEST, {
            "schema_version": 1,
            "registrations": [path.relative_to(package).as_posix()],
            "order_path": "migration/app-order.json",
            "extension_config_path": "migration/extensions.json",
        })
        # The malformed path must be rejected before digest validation.
        self._write_hash_manifest(package, "0" * 64)

    def test_malformed_leading_slash_drive_path_is_rejected_on_every_host(self) -> None:
        package = self.root / "malformed"
        package.mkdir()
        self._write_malformed_schema1_package(package)
        with self.assertRaisesRegex(appdock.AppDockError, "absolute external directory"):
            appdock.preview_private_package(package)

    def test_windows_path_matrix_is_explicit_and_host_independent(self) -> None:
        self.assertEqual(
            appdock.normalize_windows_external_directory(r"C:\Synthetic\Application"),
            r"C:\Synthetic\Application",
        )
        self.assertEqual(
            appdock.normalize_windows_external_directory("c:\\Synthetic\\Application\\"),
            r"C:\Synthetic\Application",
        )
        invalid = [
            r"/C:\Synthetic\Application",
            r"C:Synthetic\Application",
            r"\Synthetic\Application",
            r"Synthetic\Application",
            "",
            "C:/Synthetic/Application",
            r"C:\Synthetic/Application",
            r"\\?\C:\Synthetic\Application",
            r"\\.\C:\Synthetic\Application",
            r"\\server\share\Application",
            "C:\\Synthetic\x00Application".replace("x00", "\x00"),
            r"C:\Synthetic\\Application",
            r"C:\Synthetic\..\Application",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(appdock.AppDockError):
                    appdock.normalize_windows_external_directory(value)

    def test_schema2_preview_import_idempotence_rollback_and_reimport(self) -> None:
        package = self.root / "schema2"
        package.mkdir()
        ids, expected_digest = self._write_schema2_package(package)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("external process started")):
            preview = appdock.preview_private_package(package)
        self.assertEqual(preview["schema_version"], 2)
        self.assertEqual(preview["path_flavor"], "windows")
        self.assertEqual(preview["registration_count"], 8)
        self.assertEqual(preview["visible_count"], 7)
        self.assertEqual(preview["order_count"], 8)
        self.assertEqual(preview["digest"], expected_digest)
        self.assertEqual(preview["normalized"]["order"], ids)
        self.assertTrue(all(
            item["directory"].startswith("C:\\Synthetic\\")
            for item in preview["normalized"]["registrations"].values()
        ))

        data = self.root / "data"
        config = appdock.AppDockConfig.from_environment(data_dir=data)
        first = appdock.import_private_package(package, config, expected_digest=expected_digest)
        self.assertTrue(first["changed"])
        second = appdock.import_private_package(package, config, expected_digest=expected_digest)
        self.assertFalse(second["changed"])
        self.assertEqual(json.loads(config.order_path.read_text(encoding="utf-8")), ids)
        rollback = appdock.rollback_private_package(first["receipt"], config)
        self.assertTrue(rollback["rolled_back"])
        third = appdock.import_private_package(package, config, expected_digest=expected_digest)
        self.assertTrue(third["changed"])

    def test_schema2_builder_is_deterministic_and_rootless(self) -> None:
        package = self.root / "builder"
        package.mkdir()
        self._write_schema2_package(package)
        first = self.root / "first.zip"
        second = self.root / "second.zip"
        self.assertEqual(build_private_archive(package, first), build_private_archive(package, second))
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_schema2_requires_declared_windows_path_flavor(self) -> None:
        package = self.root / "missing-flavor"
        package.mkdir()
        self._write_schema2_package(package)
        descriptor_path = package / appdock.PRIVATE_PACKAGE_MANIFEST
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor.pop("path_flavor")
        self._write_json(descriptor_path, descriptor)
        self._write_hash_manifest(package, "0" * 64)
        with self.assertRaisesRegex(appdock.AppDockError, "manifest is invalid"):
            appdock.preview_private_package(package)

    def test_schema1_valid_windows_paths_remain_compatible(self) -> None:
        package = self.root / "schema1"
        package.mkdir()
        app_id = "synthetic-app"
        path = package / "migration" / "registry" / app_id / "appdock.json"
        raw = self._base_registration(app_id, r"C:\Synthetic\Application")
        self._write_json(path, raw)
        order = [app_id]
        extensions = {"schema_version": 1, "visibility": {"hidden_app_ids": []}, "providers": [], "widgets": []}
        self._write_json(package / "migration" / "app-order.json", order)
        self._write_json(package / "migration" / "extensions.json", extensions)
        self._write_json(package / appdock.PRIVATE_PACKAGE_MANIFEST, {
            "schema_version": 1,
            "registrations": [path.relative_to(package).as_posix()],
            "order_path": "migration/app-order.json",
            "extension_config_path": "migration/extensions.json",
        })
        normalized = appdock.normalize_private_registration(raw, manifest_dir=path.parent, path_flavor="windows")
        digest = appdock._digest({
            "schema_version": 1,
            "registrations": {app_id: normalized},
            "order": order,
            "extensions": extensions,
        })
        self._write_hash_manifest(package, digest)
        preview = appdock.preview_private_package(package)
        self.assertEqual(preview["schema_version"], 1)
        self.assertEqual(preview["path_flavor"], "windows")
        self.assertEqual(preview["digest"], digest)


if __name__ == "__main__":
    unittest.main()
