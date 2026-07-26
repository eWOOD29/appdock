from __future__ import annotations

import subprocess
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}")
    target.write_text(source.replace(old, new, 1), encoding="utf-8")


replace_once(
    "appdock.py",
    '''@dataclass
class AppSpec:
    app_id: str
    name: str
    manifest_dir: Path
    directory: Path
    command: list[str]
    cwd: Path
    description: str = ""
    port: int | None = None
    health_url: str = ""
    local_url: str = ""
    private_url: str = ""
    env: dict[str, str | None] = field(default_factory=dict)
    process_name: str = ""
    stop_timeout: float = 3.0
''',
    '''@dataclass
class AppSpec:
    app_id: str
    name: str
    manifest_dir: Path
    directory: Path | str
    command: list[str]
    cwd: Path | str
    description: str = ""
    port: int | None = None
    health_url: str = ""
    local_url: str = ""
    private_url: str = ""
    env: dict[str, str | None] = field(default_factory=dict)
    process_name: str = ""
    stop_timeout: float = 3.0
    path_flavor: str = "native"


def _native_runtime_cwd(spec: AppSpec) -> Path:
    if spec.path_flavor == "native":
        return Path(spec.cwd)
    if spec.path_flavor == "windows":
        if os.name != "nt":
            raise AppDockError("application path flavor is unsupported on this host")
        return Path(str(spec.cwd))
    if spec.path_flavor == "posix":
        if os.name == "nt":
            raise AppDockError("application path flavor is unsupported on this host")
        return Path(str(spec.cwd))
    raise AppDockError("application path flavor is unsupported on this host")
''',
)

replace_once(
    "appdock.py",
    '''def _migration_targets(preview: dict[str, Any], config: AppDockConfig) -> dict[Path, bytes]:
    normalized = preview["normalized"]
    targets: dict[Path, bytes] = {}
    for app_id, manifest in normalized["registrations"].items():
        targets[config.registry_root / app_id / MANIFEST_NAME] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\\n"
    targets[config.order_path] = json.dumps(normalized["order"], indent=2).encode("utf-8") + b"\\n"
    targets[config.extension_config_path] = json.dumps(normalized["extensions"], indent=2, sort_keys=True).encode("utf-8") + b"\\n"
    return targets
''',
    '''def _migration_targets(preview: dict[str, Any], config: AppDockConfig) -> dict[Path, bytes]:
    normalized = preview["normalized"]
    path_flavor = preview.get("path_flavor")
    if path_flavor not in {"windows", "posix"}:
        raise AppDockError("private package path flavor is unsupported")
    targets: dict[Path, bytes] = {}
    for app_id, manifest in normalized["registrations"].items():
        persisted = dict(manifest)
        persisted["_appdock_private_import"] = True
        persisted["path_flavor"] = path_flavor
        targets[config.registry_root / app_id / MANIFEST_NAME] = json.dumps(persisted, indent=2, sort_keys=True).encode("utf-8") + b"\\n"
    targets[config.order_path] = json.dumps(normalized["order"], indent=2).encode("utf-8") + b"\\n"
    targets[config.extension_config_path] = json.dumps(normalized["extensions"], indent=2, sort_keys=True).encode("utf-8") + b"\\n"
    return targets
''',
)

replace_once(
    "appdock.py",
    '''            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                target_raw = raw.get("directory")
                if target_raw:
                    target = Path(str(target_raw)).expanduser()
                    if not target.is_absolute():
                        target = manifest_dir / target
                elif not self.legacy_direct_root:
                    target = self.config.install_root / str(raw.get("id") or manifest_dir.name)
                else:
                    target = manifest_dir
                normalized = normalize_manifest(raw, manifest_dir=manifest_dir, directory=target, allow_outside=not self.legacy_direct_root)
                app_id = normalized["id"]
                if app_id in specs:
                    continue
                target_dir = Path(normalized["directory"]).resolve()
                cwd = (target_dir / normalized["cwd"]).resolve()
                specs[app_id] = AppSpec(app_id, normalized["name"], manifest_dir.resolve(), target_dir, normalized["command"], cwd, normalized["description"], normalized["port"], normalized["health_url"], normalized["local_url"], normalized["private_url"], normalized["env"], normalized["process_name"], normalized["stop_timeout"])
            except (OSError, ValueError, TypeError, json.JSONDecodeError, ManifestError):
                continue
''',
    '''            try:
                raw = _loads_strict_json(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ManifestError("manifest must be a JSON object")
                provenance_present = "_appdock_private_import" in raw or "path_flavor" in raw
                if provenance_present:
                    if self.legacy_direct_root or raw.get("_appdock_private_import") is not True or raw.get("external") is not True:
                        raise ManifestError("imported registration provenance is invalid")
                    path_flavor = raw.get("path_flavor")
                    if path_flavor not in {"windows", "posix"}:
                        raise ManifestError("imported registration path flavor is invalid")
                    normalized = normalize_private_registration(raw, manifest_dir=manifest_dir, path_flavor=path_flavor)
                    app_id = normalized["id"]
                    if app_id in specs:
                        continue
                    directory = normalized["directory"]
                    relative_cwd = normalized["cwd"]
                    if path_flavor == "windows":
                        cwd = directory if relative_cwd == "." else directory + "\\\\" + relative_cwd
                    else:
                        cwd = directory if relative_cwd == "." else directory.rstrip("/") + "/" + relative_cwd
                    specs[app_id] = AppSpec(
                        app_id=app_id,
                        name=normalized["name"],
                        manifest_dir=manifest_dir.resolve(),
                        directory=directory,
                        command=normalized["command"],
                        cwd=cwd,
                        description=normalized["description"],
                        port=normalized["port"],
                        health_url=normalized["health_url"],
                        local_url=normalized["local_url"],
                        private_url=normalized["private_url"],
                        env=normalized["env"],
                        process_name=normalized["process_name"],
                        stop_timeout=normalized["stop_timeout"],
                        path_flavor=path_flavor,
                    )
                    continue

                target_raw = raw.get("directory")
                if target_raw:
                    target = Path(str(target_raw)).expanduser()
                    if not target.is_absolute():
                        target = manifest_dir / target
                elif not self.legacy_direct_root:
                    target = self.config.install_root / str(raw.get("id") or manifest_dir.name)
                else:
                    target = manifest_dir
                normalized = normalize_manifest(raw, manifest_dir=manifest_dir, directory=target, allow_outside=not self.legacy_direct_root)
                app_id = normalized["id"]
                if app_id in specs:
                    continue
                target_dir = Path(normalized["directory"]).resolve()
                cwd = (target_dir / normalized["cwd"]).resolve()
                specs[app_id] = AppSpec(app_id, normalized["name"], manifest_dir.resolve(), target_dir, normalized["command"], cwd, normalized["description"], normalized["port"], normalized["health_url"], normalized["local_url"], normalized["private_url"], normalized["env"], normalized["process_name"], normalized["stop_timeout"])
            except (OSError, ValueError, TypeError, json.JSONDecodeError, AppDockError):
                continue
''',
)

replace_once(
    "appdock.py",
    '''    def start(self, app_id: str) -> dict[str, Any]:
        spec = self.discover().get(app_id)
        if spec is None:
            raise KeyError(app_id)
        with self._lock:
''',
    '''    def start(self, app_id: str) -> dict[str, Any]:
        spec = self.discover().get(app_id)
        if spec is None:
            raise KeyError(app_id)
        runtime_cwd = _native_runtime_cwd(spec)
        with self._lock:
''',
)
replace_once("appdock.py", "                        cwd=spec.cwd,\n", "                        cwd=runtime_cwd,\n")
replace_once(
    "appdock.py",
    '''    def stop(self, app_id: str) -> dict[str, Any]:
        spec = self.discover().get(app_id)
        if spec is None:
            raise KeyError(app_id)
        with self._lock:
''',
    '''    def stop(self, app_id: str) -> dict[str, Any]:
        spec = self.discover().get(app_id)
        if spec is None:
            raise KeyError(app_id)
        _native_runtime_cwd(spec)
        with self._lock:
''',
)

replace_once("scripts/emit_windows_path_fixture.py", '            "cwd": ".",', '            "cwd": r"runtime\\\\worker",')
replace_once(
    "scripts/emit_windows_path_fixture.py",
    '''    archive = output / "AppDock-Private-Fixture.zip"
    archive_sha256 = build_private_archive(source, archive)
    print(json.dumps({
''',
    '''    archive = output / "AppDock-Private-Fixture.zip"
    archive_sha256 = build_private_archive(source, archive)
    runtime_config = appdock.AppDockConfig.from_environment(data_dir=output / "runtime-data")
    appdock.import_private_package(source, runtime_config, expected_digest=preview["digest"])
    manager = appdock.AppManager(config=runtime_config)
    specs = manager.discover()
    discovery = {
        "schema_version": 1,
        "path_flavor": "windows",
        "registrations": [
            {"id": app_id, "directory": str(specs[app_id].directory), "cwd": str(specs[app_id].cwd), "path_flavor": specs[app_id].path_flavor}
            for app_id in ids
        ],
        "visible_ids": [item["id"] for item in manager.all_status()],
    }
    write_json(output / "discovery.json", discovery)
    print(json.dumps({
''',
)

Path("tests/test_wp16_discovery_contract.py").write_text(r'''from __future__ import annotations

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
        with mock.patch.object(appdock.os, "name", "nt"):
            self.assertEqual(str(appdock._native_runtime_cwd(spec)), str(spec.cwd))
        if os.name == "nt":
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
''', encoding="utf-8")

base_ci = subprocess.run(
    ["git", "show", "fdd68188ed80b053be3725e018c0b1e96980a671:.github/workflows/ci.yml"],
    check=True,
    capture_output=True,
    text=True,
).stdout
old = "            private-fixture/preview.json\n            private-fixture/AppDock-Private-Fixture.zip\n"
new = "            private-fixture/preview.json\n            private-fixture/discovery.json\n            private-fixture/AppDock-Private-Fixture.zip\n"
if base_ci.count(old) != 1:
    raise SystemExit("CI artifact anchor failed")
base_ci = base_ci.replace(old, new)
old = "              ('preview.json', 'normalized preview'),\n              ('AppDock-Private-Fixture.zip', 'private ZIP'),\n"
new = "              ('preview.json', 'normalized preview'),\n              ('discovery.json', 'AppManager discovery'),\n              ('AppDock-Private-Fixture.zip', 'private ZIP'),\n"
if base_ci.count(old) != 1:
    raise SystemExit("CI deterministic anchor failed")
Path(".github/workflows/ci.yml").write_text(base_ci.replace(old, new), encoding="utf-8")
Path(".github/workflows/wp16-remediation.yml").unlink()
Path(__file__).unlink()
