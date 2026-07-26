from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import appdock


class CompleteKillMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_child(self, code: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.repo)
        return subprocess.run(
            [sys.executable, "-c", code, *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_private_package(self, root: Path, source_root: Path, count: int = 4) -> tuple[str, list[str]]:
        root.mkdir(parents=True, exist_ok=True)
        ids = [f"matrix-{index}" for index in range(count)]
        registration_paths: list[str] = []
        normalized: dict[str, dict[str, object]] = {}
        for app_id in ids:
            path = root / "migration" / "registry" / app_id / "appdock.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = {
                "id": app_id,
                "name": app_id,
                "directory": str(source_root / app_id),
                "external": True,
                "command": [sys.executable, "-c", "pass"],
            }
            path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            registration_paths.append(path.relative_to(root).as_posix())
            normalized[app_id] = appdock.normalize_manifest(
                raw,
                manifest_dir=path.parent,
                directory=source_root / app_id,
                external=True,
                allow_outside=True,
            )
        extensions = {
            "schema_version": 1,
            "visibility": {"hidden_app_ids": [ids[-1]]},
            "providers": [],
            "widgets": [],
        }
        (root / "migration" / "app-order.json").write_text(json.dumps(ids, indent=2) + "\n", encoding="utf-8")
        (root / "migration" / "extensions.json").write_text(json.dumps(extensions, indent=2) + "\n", encoding="utf-8")
        descriptor = {
            "schema_version": 1,
            "registrations": registration_paths,
            "order_path": "migration/app-order.json",
            "extension_config_path": "migration/extensions.json",
        }
        (root / appdock.PRIVATE_PACKAGE_MANIFEST).write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
        digest = appdock._digest(
            {
                "schema_version": 1,
                "registrations": {app_id: normalized[app_id] for app_id in sorted(normalized)},
                "order": ids,
                "extensions": extensions,
            }
        )
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != appdock.PRIVATE_PACKAGE_HASH_MANIFEST:
                payload = path.read_bytes()
                files.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                )
        (root / appdock.PRIVATE_PACKAGE_HASH_MANIFEST).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "package": "AppDock private integration package",
                    "migration_digest": digest,
                    "files": files,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return digest, ids

    @staticmethod
    def seed_old_migration_state(config: appdock.AppDockConfig, ids: list[str]) -> dict[str, bytes | None]:
        config.ensure()
        for app_id in ids:
            path = config.registry_root / app_id / "appdock.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"id": app_id, "old": True}), encoding="utf-8")
        config.order_path.write_text(json.dumps(list(reversed(ids))), encoding="utf-8")
        config.extension_config_path.write_text(
            json.dumps({"schema_version": 1, "visibility": {"hidden_app_ids": []}, "providers": [], "widgets": []}),
            encoding="utf-8",
        )
        targets = [
            *(config.registry_root / app_id / "appdock.json" for app_id in ids),
            config.order_path,
            config.extension_config_path,
        ]
        return {str(path): path.read_bytes() if path.exists() else None for path in targets}

    @staticmethod
    def migration_snapshot(config: appdock.AppDockConfig, ids: list[str]) -> dict[str, bytes | None]:
        targets = [
            *(config.registry_root / app_id / "appdock.json" for app_id in ids),
            config.order_path,
            config.extension_config_path,
        ]
        return {str(path): path.read_bytes() if path.exists() else None for path in targets}

    @staticmethod
    def write_release_tree(root: Path, marker: bytes) -> None:
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

    def test_migration_process_death_after_every_target_replacement(self) -> None:
        package = self.root / "package-template"
        digest, ids = self.make_private_package(package, self.root / "sources")
        preview = appdock.preview_private_package(package)
        target_count = len(appdock._migration_targets(preview, appdock.AppDockConfig.from_environment(data_dir=self.root / "count-data")))
        child_code = r'''
import os, sys
from pathlib import Path
import appdock
package, data, digest, wanted = sys.argv[1:]
seen = 0
def hook(phase):
    global seen
    if phase.startswith("after-replace:"):
        seen += 1
        if seen == int(wanted):
            os._exit(77)
appdock.import_private_package(
    Path(package),
    appdock.AppDockConfig.from_environment(data_dir=Path(data)),
    expected_digest=digest,
    phase_hook=hook,
)
'''
        for replacement_index in range(1, target_count + 1):
            with self.subTest(replacement_index=replacement_index):
                case = self.root / f"migration-{replacement_index}"
                case_package = case / "package"
                case_digest, case_ids = self.make_private_package(case_package, case / "sources")
                config = appdock.AppDockConfig.from_environment(data_dir=case / "data")
                old = self.seed_old_migration_state(config, case_ids)
                child = self.run_child(
                    child_code,
                    str(case_package),
                    str(config.data_root),
                    case_digest,
                    str(replacement_index),
                )
                self.assertEqual(child.returncode, 77, child.stderr)
                appdock.AppManager(config=config)
                self.assertEqual(self.migration_snapshot(config, case_ids), old)
                appdock.recover_private_migrations(config)
                self.assertEqual(self.migration_snapshot(config, case_ids), old)
        self.assertEqual(digest, preview["digest"])
        self.assertEqual(ids[-1], "matrix-3")

    def test_update_recovery_process_death_is_idempotent_in_both_directions(self) -> None:
        apply_code = r'''
import os, sys
from pathlib import Path
import appdock
staged, install, data, wanted = map(Path, sys.argv[1:4]) + [sys.argv[4]]
def hook(phase):
    if phase == wanted:
        os._exit(79)
appdock.apply_update(staged, install, data, phase_hook=hook)
'''
        # Use a syntax-neutral child body rather than relying on list arithmetic.
        apply_code = r'''
import os, sys
from pathlib import Path
import appdock
staged = Path(sys.argv[1]); install = Path(sys.argv[2]); data = Path(sys.argv[3]); wanted = sys.argv[4]
def hook(phase):
    if phase == wanted:
        os._exit(79)
appdock.apply_update(staged, install, data, phase_hook=hook)
'''
        recovery_code = r'''
import os, sys
from pathlib import Path
import appdock
data = Path(sys.argv[1]); install = Path(sys.argv[2]); wanted = sys.argv[3]
def hook(phase):
    if phase == wanted:
        os._exit(80)
appdock.recover_update_transactions(data, expected_install=install, phase_hook=hook)
'''
        for apply_phase, recovery_phase, expected in [
            ("after-activate", "recovery:restore-old", b"old"),
            ("after-commit", "recovery:finish-new", b"new"),
        ]:
            with self.subTest(apply_phase=apply_phase):
                case = self.root / apply_phase
                data = case / "data"
                install = case / "install"
                staged = data / "updates" / "0.2.0"
                self.write_release_tree(install, b"old")
                self.write_release_tree(staged, b"new")
                data.mkdir(parents=True, exist_ok=True)
                user_data = data / "user-state.json"
                user_data.write_bytes(b"preserve")
                first = self.run_child(apply_code, str(staged), str(install), str(data), apply_phase)
                self.assertEqual(first.returncode, 79, first.stderr)
                second = self.run_child(recovery_code, str(data), str(install), recovery_phase)
                self.assertEqual(second.returncode, 80, second.stderr)
                appdock.recover_update_transactions(data, expected_install=install)
                self.assertEqual((install / "appdock.py").read_bytes(), expected)
                self.assertEqual(user_data.read_bytes(), b"preserve")
                snapshot = {path.relative_to(install).as_posix(): path.read_bytes() for path in install.rglob("*") if path.is_file()}
                appdock.recover_update_transactions(data, expected_install=install)
                self.assertEqual(
                    {path.relative_to(install).as_posix(): path.read_bytes() for path in install.rglob("*") if path.is_file()},
                    snapshot,
                )
                self.assertFalse(any(path.name.endswith(".candidate") or path.name.endswith(".backup") for path in case.iterdir()))


if __name__ == "__main__":
    unittest.main()
