from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

import appdock
from scripts.build_private_package import build_private_archive
from scripts.build_portable import build_archive


class FixtureMixin:
    def make_private_package(self, root: Path, source_root: Path, count: int = 3) -> tuple[str, list[str]]:
        root.mkdir(parents=True, exist_ok=True)
        ids = [f"synthetic-{index}" for index in range(count)]
        registration_paths: list[str] = []
        normalized: dict[str, dict[str, object]] = {}
        for app_id in ids:
            path = root / "migration" / "registry" / app_id / "appdock.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = {
                "id": app_id,
                "name": app_id,
                "description": "synthetic fixture",
                "directory": str(source_root / app_id),
                "external": True,
                "command": [sys.executable, "-c", "pass"],
                "local_url": "http://127.0.0.1:19000",
                "private_url": "https://private.example.invalid/app",
            }
            path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            registration_paths.append(path.relative_to(root).as_posix())
            normalized[app_id] = appdock.normalize_manifest(
                raw, manifest_dir=path.parent, directory=source_root / app_id, external=True, allow_outside=True
            )
        order = ids
        extensions = {
            "schema_version": 1,
            "visibility": {"hidden_app_ids": [ids[-1]]},
            "providers": [],
            "widgets": [],
        }
        (root / "migration" / "app-order.json").write_text(json.dumps(order, indent=2) + "\n", encoding="utf-8")
        (root / "migration" / "extensions.json").write_text(json.dumps(extensions, indent=2) + "\n", encoding="utf-8")
        descriptor = {
            "schema_version": 1,
            "registrations": registration_paths,
            "order_path": "migration/app-order.json",
            "extension_config_path": "migration/extensions.json",
        }
        (root / appdock.PRIVATE_PACKAGE_MANIFEST).write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
        (root / "README.md").write_text("synthetic protected package\n", encoding="utf-8")
        migration_digest = appdock._digest({
            "schema_version": 1,
            "registrations": {app_id: normalized[app_id] for app_id in sorted(normalized)},
            "order": order,
            "extensions": extensions,
        })
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != appdock.PRIVATE_PACKAGE_HASH_MANIFEST:
                payload = path.read_bytes()
                files.append({
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                })
        manifest = {
            "schema_version": 1,
            "package": "AppDock private integration package",
            "migration_digest": migration_digest,
            "files": files,
        }
        (root / appdock.PRIVATE_PACKAGE_HASH_MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return migration_digest, ids

    def write_release_tree(self, root: Path, marker: bytes) -> None:
        members = {
            "appdock.py": marker,
            "static/app.js": b"js-" + marker,
            "static/app.css": b"css-" + marker,
            "scripts/update_helper.py": b"helper-" + marker,
            "scripts/path_safety.ps1": b"safety-" + marker,
            "scripts/install.ps1": b"install-" + marker,
            "scripts/uninstall.ps1": b"uninstall-" + marker,
        }
        manifest = {
            "schema_version": 2,
            "files": [
                {"path": name, "sha256": hashlib.sha256(payload).hexdigest()}
                for name, payload in sorted(members.items())
            ],
        }
        members[appdock.RELEASE_MANIFEST_NAME] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        for name, payload in members.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)


class PrivateManifestTests(FixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.source = self.root / "sources"
        self.digest, self.ids = self.make_private_package(self.package, self.source)
        self.config = appdock.AppDockConfig.from_environment(data_dir=self.root / "data")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_exact_manifest_digest_and_rootless_deterministic_archive(self) -> None:
        preview = appdock.preview_private_package(self.package)
        self.assertEqual(preview["digest"], self.digest)
        first, second = self.root / "one.zip", self.root / "two.zip"
        self.assertEqual(build_private_archive(self.package, first), build_private_archive(self.package, second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            infos = archive.infolist()
            expected = sorted(path.relative_to(self.package).as_posix() for path in self.package.rglob("*") if path.is_file())
            self.assertEqual([info.filename for info in infos], expected)
            self.assertTrue(all(not info.is_dir() and info.create_system == 0 for info in infos))

    def test_each_package_class_is_bound_by_size_hash_and_exact_set(self) -> None:
        classes = [
            "migration/registry/synthetic-0/appdock.json",
            "migration/app-order.json",
            "migration/extensions.json",
            "appdock-private-package.json",
            "README.md",
        ]
        for relative in classes:
            with self.subTest(relative=relative):
                original = (self.package / relative).read_bytes()
                (self.package / relative).write_bytes(original + b"x")
                with self.assertRaises(appdock.AppDockError):
                    appdock.preview_private_package(self.package)
                (self.package / relative).write_bytes(original)
        extra = self.package / "unexpected.txt"
        extra.write_text("extra", encoding="utf-8")
        with self.assertRaises(appdock.AppDockError):
            appdock.preview_private_package(self.package)
        extra.unlink()
        (self.package / "migration/app-order.json").write_text('{"x":1,"x":2}', encoding="utf-8")
        with self.assertRaises(appdock.AppDockError):
            appdock.preview_private_package(self.package)

    def test_import_requires_fresh_caller_confirmation_and_rechecks_toctou(self) -> None:
        with self.assertRaises(appdock.AppDockError):
            appdock.import_private_package(self.package, self.config)
        with self.assertRaises(appdock.AppDockError):
            appdock.import_private_package(self.package, self.config, expected_digest="0" * 64)
        original = appdock.preview_private_package
        calls = 0

        def changing(package_root):
            nonlocal calls
            calls += 1
            result = original(package_root)
            if calls == 1:
                path = self.package / "README.md"
                path.write_bytes(path.read_bytes() + b"changed")
            return result

        with mock.patch("appdock.preview_private_package", side_effect=changing):
            with self.assertRaises(appdock.AppDockError):
                appdock.import_private_package(self.package, self.config, expected_digest=self.digest)
        self.assertFalse(self.config.order_path.exists())


class ExtensionFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = appdock.AppDockConfig.from_environment(data_dir=self.root / "data")
        self.config.ensure()
        for app_id in ("visible", "hidden"):
            path = self.config.registry_root / app_id / "appdock.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "id": app_id, "name": app_id, "external": True,
                "directory": str(self.root / app_id), "command": [sys.executable, "-c", "pass"]
            }), encoding="utf-8")
        self.valid = {
            "schema_version": 1,
            "visibility": {"hidden_app_ids": ["hidden"]},
            "providers": [],
            "widgets": [],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_malformed_replacement_disables_stale_state_and_cache(self) -> None:
        self.config.extension_config_path.write_text(json.dumps(self.valid), encoding="utf-8")
        extensions = appdock.ExtensionManager(self.config)
        manager = appdock.AppManager(config=self.config, extensions=extensions)
        self.assertEqual([item["id"] for item in manager.all_status()], ["visible"])
        extensions._provider_cache[("stale",)] = (999999999.0, {"stale": True})
        self.config.extension_config_path.write_text("{", encoding="utf-8")
        self.assertEqual({item["id"] for item in manager.all_status()}, {"visible", "hidden"})
        snapshot = extensions.snapshot(manager.discover())
        self.assertFalse(snapshot["enabled"])
        self.assertTrue(snapshot["error"])
        self.assertEqual(extensions._provider_cache, {})

    def test_duplicate_keys_and_unknown_hidden_ids_fail_closed(self) -> None:
        self.config.extension_config_path.write_text(
            '{"schema_version":1,"schema_version":1,"visibility":{"hidden_app_ids":[]},"providers":[],"widgets":[]}',
            encoding="utf-8",
        )
        extensions = appdock.ExtensionManager(self.config)
        self.assertTrue(extensions.snapshot({"visible": object(), "hidden": object()})["error"])
        invalid = dict(self.valid)
        invalid["visibility"] = {"hidden_app_ids": ["unknown"]}
        self.config.extension_config_path.write_text(json.dumps(invalid), encoding="utf-8")
        self.assertEqual(extensions.hidden_app_ids({"visible": object(), "hidden": object()}), frozenset())
        self.assertTrue(extensions.snapshot({"visible": object(), "hidden": object()})["error"])


class CrashRecoveryTests(FixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_child(self, code: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.repo)
        return subprocess.run([sys.executable, "-c", code, *args], env=env, text=True, capture_output=True, check=False)

    def seed_old_migration_state(self, config: appdock.AppDockConfig, ids: list[str]) -> dict[str, bytes | None]:
        config.ensure()
        for app_id in ids:
            path = config.registry_root / app_id / "appdock.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"id": app_id, "old": True}), encoding="utf-8")
        config.order_path.write_text(json.dumps(list(reversed(ids))), encoding="utf-8")
        config.extension_config_path.write_text(json.dumps({
            "schema_version": 1, "visibility": {"hidden_app_ids": []}, "providers": [], "widgets": []
        }), encoding="utf-8")
        targets = [*(config.registry_root / app_id / "appdock.json" for app_id in ids), config.order_path, config.extension_config_path]
        return {str(path): path.read_bytes() if path.exists() else None for path in targets}

    def migration_snapshot(self, config: appdock.AppDockConfig, ids: list[str]) -> dict[str, bytes | None]:
        targets = [*(config.registry_root / app_id / "appdock.json" for app_id in ids), config.order_path, config.extension_config_path]
        return {str(path): path.read_bytes() if path.exists() else None for path in targets}

    def test_migration_abrupt_termination_exposes_only_complete_old_or_new_state(self) -> None:
        phases = ["after-journal", "after-staging", "after-replace:1", "after-replace:3", "before-commit", "after-commit"]
        for phase in phases:
            with self.subTest(phase=phase):
                case = self.root / phase.replace(":", "-")
                package = case / "package"
                digest, ids = self.make_private_package(package, case / "sources", count=4)
                config = appdock.AppDockConfig.from_environment(data_dir=case / "data")
                old = self.seed_old_migration_state(config, ids)
                preview = appdock.preview_private_package(package)
                new_targets = appdock._migration_targets(preview, config)
                new = {str(path): payload for path, payload in new_targets.items()}
                code = r'''
import os, sys
from pathlib import Path
import appdock
package, data, digest, wanted = sys.argv[1:]
seen = 0
def hook(phase):
    global seen
    if wanted.startswith("after-replace:") and phase.startswith("after-replace:"):
        seen += 1
        if seen == int(wanted.split(":")[1]): os._exit(77)
    elif phase == wanted: os._exit(77)
config = appdock.AppDockConfig.from_environment(data_dir=Path(data))
appdock.import_private_package(Path(package), config, expected_digest=digest, phase_hook=hook)
'''
                child = self.run_child(code, str(package), str(config.data_root), digest, phase)
                self.assertEqual(child.returncode, 77, child.stderr)
                appdock.AppManager(config=config)
                actual = self.migration_snapshot(config, ids)
                self.assertIn(actual, (old, new))
                self.assertEqual(actual, new if phase == "after-commit" else old)

    def test_recovery_itself_is_idempotent_after_process_death(self) -> None:
        package = self.root / "recovery-package"
        digest, ids = self.make_private_package(package, self.root / "sources", count=3)
        config = appdock.AppDockConfig.from_environment(data_dir=self.root / "recovery-data")
        old = self.seed_old_migration_state(config, ids)
        crash = r'''
import os, sys
from pathlib import Path
import appdock
package, data, digest = sys.argv[1:]
count = 0
def hook(phase):
    global count
    if phase.startswith("after-replace:"):
        count += 1
        if count == 1: os._exit(77)
appdock.import_private_package(Path(package), appdock.AppDockConfig.from_environment(data_dir=Path(data)), expected_digest=digest, phase_hook=hook)
'''
        self.assertEqual(self.run_child(crash, str(package), str(config.data_root), digest).returncode, 77)
        recover_crash = r'''
import os, sys
from pathlib import Path
import appdock
count = 0
def hook(phase):
    global count
    if phase.startswith("recovery:"):
        count += 1
        if count == 1: os._exit(78)
appdock.recover_private_migrations(appdock.AppDockConfig.from_environment(data_dir=Path(sys.argv[1])), phase_hook=hook)
'''
        self.assertEqual(self.run_child(recover_crash, str(config.data_root)).returncode, 78)
        appdock.recover_private_migrations(config)
        self.assertEqual(self.migration_snapshot(config, ids), old)

    def test_helper_death_matrix_recovers_whole_program_tree(self) -> None:
        phases = ["prepared", "after-backup", "after-activate", "before-commit", "after-commit"]
        for phase in phases:
            with self.subTest(phase=phase):
                case = self.root / f"update-{phase}"
                data = case / "data"
                install = case / "install"
                staged = data / "updates" / "0.2.0"
                self.write_release_tree(install, b"old")
                self.write_release_tree(staged, b"new")
                code = r'''
import os, sys
from pathlib import Path
from scripts import update_helper
wanted = sys.argv[4]
def hook(phase):
    if phase == wanted: os._exit(79)
update_helper.run(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), 0, Path(sys.argv[2]) / "appdock.py", [], phase_hook=hook)
'''
                child = self.run_child(code, str(staged), str(install), str(data), phase)
                self.assertEqual(child.returncode, 79, child.stderr)
                appdock.recover_update_transactions(data, expected_install=install)
                actual = (install / "appdock.py").read_bytes()
                self.assertEqual(actual, b"new" if phase == "after-commit" else b"old")
                self.assertFalse(any(path.name.endswith(".candidate") or path.name.endswith(".backup") for path in case.iterdir()))

    def test_public_archive_metadata_is_explicit_and_repeatable(self) -> None:
        first, second = self.root / "one.zip", self.root / "two.zip"
        self.assertEqual(build_archive(first, self.repo), build_archive(second, self.repo))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertTrue(all(info.create_system == 0 and info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))

    def test_uninstaller_has_no_get_file_hash_dependency(self) -> None:
        source = (self.repo / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Get-FileHash", source)
        self.assertIn("System.Security.Cryptography.SHA256", source)


if __name__ == "__main__":
    unittest.main()
