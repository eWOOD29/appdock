from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import appdock
from scripts import update_helper
from appdock import (
    AppDockConfig,
    AppDockError,
    AppManager,
    GitHubOnboarding,
    Handler,
    LocalFolderOnboarding,
    ThreadingHTTPServer,
    apply_update,
    compare_semver,
    launch_update_helper,
    normalize_manifest,
    select_trusted_assets,
    validate_bind_host,
    validate_zip,
)


class ReleaseHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppDockConfig.from_environment(data_dir=self.root / "data")
        self.config.ensure()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_manifest(self, directory: Path, *, app_id: str = "demo", cwd: str = ".", **extra: object) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / cwd).mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "id": app_id,
            "name": "Demo",
            "command": [sys.executable, "-c", "import time; time.sleep(30)"],
            "cwd": cwd,
            **extra,
        }
        (directory / "appdock.json").write_text(json.dumps(payload), encoding="utf-8")

    def make_zip(self, members: dict[str, bytes]) -> bytes:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return stream.getvalue()

    def release_members(self, overrides: dict[str, bytes] | None = None) -> dict[str, bytes]:
        members = {
            "appdock.py": b"app",
            "static/app.js": b"js",
            "static/app.css": b"css",
            "scripts/update_helper.py": b"helper",
            "scripts/path_safety.ps1": b"safety",
            "scripts/install.ps1": b"install",
            "scripts/uninstall.ps1": b"uninstall",
        }
        members.update(overrides or {})
        manifest = {
            "schema_version": 2,
            "files": [
                {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
                for path, content in sorted(members.items())
            ],
        }
        members["RELEASE-MANIFEST.json"] = json.dumps(manifest, sort_keys=True).encode("utf-8")
        return members

    def write_release_tree(self, root: Path, overrides: dict[str, bytes] | None = None) -> None:
        for relative, content in self.release_members(overrides).items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def test_local_registration_preserves_cwd_and_private_env(self) -> None:
        source = self.root / "source"
        self.write_manifest(source, cwd="server", env={"APP_MODE": "local"})
        onboarding = LocalFolderOnboarding(self.config)
        preview = onboarding.preview(source)
        self.assertNotIn("env", preview["app"])
        onboarding.register(source, preview["digest"], preview)
        registered = json.loads((self.config.registry_root / "demo" / "appdock.json").read_text(encoding="utf-8"))
        self.assertEqual(registered["cwd"], "server")
        self.assertEqual(registered["env"], {"APP_MODE": "local"})

    def test_github_registration_preserves_manifest_cwd(self) -> None:
        def runner(command, **kwargs):
            stage = Path(command[-1])
            self.write_manifest(stage, app_id="remote", cwd="web")
            return SimpleNamespace(returncode=0)

        onboarding = GitHubOnboarding(self.config, runner=runner)
        preview = onboarding.preview("https://github.com/owner/repo")
        onboarding.register(preview, preview["digest"])
        registered = json.loads((self.config.registry_root / "remote" / "appdock.json").read_text(encoding="utf-8"))
        self.assertEqual(registered["cwd"], "web")

    def test_github_url_rejects_explicit_ports(self) -> None:
        for url in ("https://github.com:443/owner/repo", "https://github.com:444/owner/repo"):
            with self.subTest(url=url), self.assertRaises(AppDockError):
                appdock.canonical_github_url(url)

    def test_github_clone_timeout_is_user_facing_and_cleans_staging(self) -> None:
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired("git clone", 120)

        onboarding = GitHubOnboarding(self.config, runner=runner)
        with self.assertRaises(AppDockError):
            onboarding.preview("https://github.com/owner/repo")
        self.assertEqual(list(self.config.staging_root.iterdir()), [])

    def test_staging_cleanup_removes_read_only_git_artifacts(self) -> None:
        stage = self.config.staging_root / "repo-read-only"
        pack = stage / ".git" / "objects" / "pack" / "pack-test.pack"
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b"pack")
        pack.chmod(stat.S_IREAD)
        appdock._remove_tree(stage)
        self.assertFalse(stage.exists())

    def test_staging_alias_is_rejected_without_deleting_its_target(self) -> None:
        target = self.config.staging_root / "repo-target"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        alias = self.config.staging_root / "repo-alias"

        original = appdock._is_link_or_reparse
        with patch("appdock._is_link_or_reparse", side_effect=lambda path: Path(path) == alias or original(Path(path))):
            with self.assertRaises(AppDockError):
                GitHubOnboarding(self.config).cleanup("repo-alias")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_symlinked_staging_root_is_rejected_before_child_resolution(self) -> None:
        staging_root = self.config.staging_root
        child = staging_root / "repo-child"
        original = appdock._is_link_or_reparse
        with patch("appdock._is_link_or_reparse", side_effect=lambda path: Path(path) == staging_root or original(Path(path))):
            with self.assertRaises(AppDockError):
                GitHubOnboarding(self.config).cleanup("repo-child")

    def test_update_version_alias_is_rejected_lexically(self) -> None:
        alias = self.config.updates_root / "0.2.7"
        with patch("appdock._is_link_or_reparse", side_effect=lambda path: Path(path) == alias):
            with self.assertRaises(AppDockError):
                appdock._safe_version_child(self.config.updates_root, "0.2.7")

    def test_github_staging_enforces_ttl_size_and_ui_cleanup(self) -> None:
        stale = self.config.staging_root / "repo-stale"
        stale.mkdir()
        old = time.time() - 100
        os.utime(stale, (old, old))
        onboarding = GitHubOnboarding(self.config)
        self.assertEqual(onboarding.cleanup_stale(now=time.time(), ttl_seconds=10), 1)
        self.assertFalse(stale.exists())

        idle_stale = self.config.staging_root / "repo-idle-stale"
        idle_stale.mkdir()
        os.utime(idle_stale, (old, old))
        stop = threading.Event()
        cleanup_thread = threading.Thread(
            target=appdock._staging_cleanup_loop,
            args=(onboarding, stop),
            kwargs={"interval_seconds": 0.01, "ttl_seconds": 10},
        )
        cleanup_thread.start()
        cleanup_thread.join(timeout=0.2)
        stop.set()
        cleanup_thread.join(timeout=1)
        self.assertFalse(idle_stale.exists(), "idle staging cleanup did not run independently")

        def runner(command, **kwargs):
            stage = Path(command[-1])
            self.write_manifest(stage, app_id="oversized")
            (stage / "payload.bin").write_bytes(b"12345")
            return SimpleNamespace(returncode=0)

        with patch.object(appdock, "MAX_GITHUB_STAGE_BYTES", 4):
            with self.assertRaises(AppDockError):
                GitHubOnboarding(self.config, runner=runner).preview("https://github.com/owner/repo")
        self.assertEqual(list(self.config.staging_root.iterdir()), [])
        ui = (Path(appdock.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/onboarding/github/cleanup", ui)

    def test_default_clone_runner_stops_the_process_while_quota_is_exceeded(self) -> None:
        stage = self.config.staging_root / "repo-growing"
        stage.mkdir()

        class GrowingProcess:
            returncode: int | None = None

            def __init__(self) -> None:
                self.polls = 0
                self.stopped = False

            def poll(self):
                self.polls += 1
                (stage / f"chunk-{self.polls}.bin").write_bytes(b"123")
                return self.returncode

            def communicate(self, timeout=None):
                return "", ""

            def terminate(self):
                self.stopped = True
                self.returncode = 1

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.stopped = True
                self.returncode = 1

        process = GrowingProcess()
        with patch.object(appdock, "MAX_GITHUB_STAGE_BYTES", 4):
            with self.assertRaises(AppDockError):
                appdock._run_bounded_clone(
                    ["git", "clone", "https://github.com/owner/repo.git", str(stage)],
                    stage,
                    popen=lambda command, **kwargs: process,
                    poll_interval=0,
                )
        self.assertTrue(process.stopped)
        self.assertGreaterEqual(process.polls, 2)

    def test_health_checks_are_loopback_only(self) -> None:
        app_dir = self.root / "app"
        app_dir.mkdir()
        safe = normalize_manifest(
            {"id": "safe", "command": ["python"], "health_url": "http://127.0.0.1:8000/health"},
            manifest_dir=app_dir,
        )
        self.assertEqual(safe["health_url"], "http://127.0.0.1:8000/health")
        for unsafe in ("http://169.254.169.254/latest/meta-data", "https://example.com/health", "http://[::ffff:169.254.169.254]/"):
            with self.subTest(unsafe=unsafe), self.assertRaises(AppDockError):
                normalize_manifest({"id": "unsafe", "command": ["python"], "health_url": unsafe}, manifest_dir=app_dir)

    def test_private_url_is_supported_without_publishing_machine_values(self) -> None:
        app_dir = self.root / "app"
        app_dir.mkdir()
        normalized = normalize_manifest(
            {"id": "demo", "command": ["python"], "private_url": "https://private-host.example/app"},
            manifest_dir=app_dir,
        )
        self.assertEqual(normalized["private_url"], "https://private-host.example/app")

    def test_detected_external_listener_prevents_duplicate_start(self) -> None:
        apps = self.root / "apps"
        self.write_manifest(apps / "demo", port=8765)
        manager = AppManager(apps)
        with patch.object(AppManager, "_external_pids", return_value=[9999], create=True), patch("appdock.subprocess.Popen") as popen:
            status = manager.start("demo")
        popen.assert_not_called()
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["pid"], 9999)
        self.assertFalse(status["managed"])

    def test_external_listener_is_never_killed_by_port_only(self) -> None:
        apps = self.root / "apps"
        self.write_manifest(apps / "demo", port=8765)
        manager = AppManager(apps)
        with patch.object(AppManager, "_external_pids", return_value=[9999], create=True), patch("appdock.subprocess.run") as run:
            status = manager.stop("demo")
        run.assert_not_called()
        self.assertEqual(status["pid"], 9999)
        self.assertFalse(status["managed"])

    def test_posix_apps_launch_in_an_isolated_process_group(self) -> None:
        self.assertEqual(appdock._process_group_options("posix"), {"start_new_session": True})
        windows = appdock._process_group_options("nt")
        self.assertIn("creationflags", windows)
        self.assertNotIn("start_new_session", windows)

    def test_archive_rejects_device_names_ads_and_expansion_limit(self) -> None:
        for member in ("CON/appdock.py", "folder/NUL.txt", "appdock.py:stream"):
            with self.subTest(member=member), self.assertRaises(AppDockError):
                validate_zip(self.make_zip({member: b"x"}))
        with patch.object(appdock, "MAX_UPDATE_UNCOMPRESSED_BYTES", 4, create=True):
            with self.assertRaises(AppDockError):
                validate_zip(self.make_zip({"appdock.py": b"12345"}))

    def test_archive_rejects_unicode_windows_device_aliases(self) -> None:
        for name in ("COM¹.txt", "COM²", "COM³.log", "LPT¹.txt", "LPT²", "LPT³.log"):
            with self.subTest(name=name), self.assertRaises(AppDockError):
                appdock._assert_zip_member(name, zipfile.ZipInfo(name))

    def test_update_archive_requires_appdock_entry_point(self) -> None:
        with self.assertRaises(AppDockError):
            validate_zip(self.make_zip({"README.md": b"not an application"}))

    def test_update_archive_requires_a_complete_verified_inventory(self) -> None:
        complete = self.release_members()
        self.assertIn("RELEASE-MANIFEST.json", validate_zip(self.make_zip(complete)))

        missing = dict(complete)
        missing.pop("static/app.js")
        with self.assertRaises(AppDockError):
            validate_zip(self.make_zip(missing))

        missing_safety = dict(complete)
        missing_safety.pop("scripts/path_safety.ps1")
        safety_manifest = json.loads(missing_safety["RELEASE-MANIFEST.json"])
        safety_manifest["files"] = [entry for entry in safety_manifest["files"] if entry["path"] != "scripts/path_safety.ps1"]
        missing_safety["RELEASE-MANIFEST.json"] = json.dumps(safety_manifest, sort_keys=True).encode("utf-8")
        with self.assertRaises(AppDockError):
            validate_zip(self.make_zip(missing_safety))

        extra = dict(complete)
        extra["unexpected.py"] = b"surprise"
        with self.assertRaises(AppDockError):
            validate_zip(self.make_zip(extra))

    def test_trusted_asset_path_must_start_with_exact_repository(self) -> None:
        deceptive = "https://github.com/other/project/owner/repo/releases/download/v1.0.0/"
        release = {"assets": [
            {"name": "appdock-windows.zip", "url": deceptive + "appdock-windows.zip"},
            {"name": "SHA256SUMS.txt", "url": deceptive + "SHA256SUMS.txt"},
        ]}
        with self.assertRaises(AppDockError):
            select_trusted_assets(release, "owner/repo")

    def test_update_download_validates_the_final_redirect_host(self) -> None:
        class RedirectedResponse:
            headers: dict[str, str] = {}

            def __init__(self, final_url: str):
                self.final_url = final_url

            def read(self, limit: int) -> bytes:
                return b"asset"

            def geturl(self) -> str:
                return self.final_url

        evil = lambda request, **kwargs: RedirectedResponse("https://evil.example/asset.zip")
        with self.assertRaises(AppDockError):
            appdock._read_asset(evil, "https://github.com/owner/repo/releases/download/v1/a.zip", 100)
        wrong_port = lambda request, **kwargs: RedirectedResponse("https://release-assets.githubusercontent.com:444/asset.zip")
        with self.assertRaises(AppDockError):
            appdock._read_asset(wrong_port, "https://github.com/owner/repo/releases/download/v1/a.zip", 100)

        allowed = lambda request, **kwargs: RedirectedResponse("https://release-assets.githubusercontent.com/github-production-release-asset/file")
        self.assertEqual(appdock._read_asset(allowed, "https://github.com/owner/repo/releases/download/v1/a.zip", 100), b"asset")

    def test_semver_numeric_prerelease_identifiers_compare_numerically(self) -> None:
        self.assertGreater(compare_semver("1.0.0-10", "1.0.0-2"), 0)
        self.assertLess(compare_semver("1.0.0-alpha", "1.0.0-alpha.1"), 0)

    def test_apply_update_rejects_install_data_overlap(self) -> None:
        staged = self.config.updates_root / "0.2.0"
        staged.mkdir(parents=True)
        (staged / "appdock.py").write_text("new", encoding="utf-8")
        with self.assertRaises(AppDockError):
            apply_update(staged, self.config.data_root, self.config.data_root)

    def test_apply_update_rejects_a_staging_root_alias(self) -> None:
        alias = self.config.updates_root / "0.2.8"
        with patch("appdock._is_link_or_reparse", side_effect=lambda path: Path(path) == alias):
            with self.assertRaises(AppDockError):
                apply_update(alias, self.root / "install-alias", self.config.data_root)

    def test_apply_update_removes_obsolete_managed_files_and_rollback_restores_them(self) -> None:
        install = self.root / "install"
        install.mkdir()
        self.write_release_tree(install, {"obsolete.py": b"old obsolete", "appdock.py": b"old app"})
        staged = self.config.updates_root / "0.2.0"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new app"})

        result = apply_update(staged, install, self.config.data_root)
        self.assertFalse((install / "obsolete.py").exists())
        self.assertEqual((install / "appdock.py").read_bytes(), b"new app")

        appdock.rollback_update(result, install, self.config.data_root)
        self.assertEqual((install / "obsolete.py").read_bytes(), b"old obsolete")
        self.assertEqual((install / "appdock.py").read_bytes(), b"old app")

    def test_backup_failure_leaves_existing_installation_untouched(self) -> None:
        install = self.root / "install-backup-failure"
        install.mkdir()
        self.write_release_tree(install, {"appdock.py": b"old app", "old.txt": b"keep me"})
        staged = self.config.updates_root / "0.2.5"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new app"})

        real_copy = shutil.copy2
        backup_root = (self.config.data_root / "updates" / "backups").resolve()

        def fail_during_backup(source, destination, *args, **kwargs):
            if appdock._inside(Path(destination).resolve(), backup_root) and Path(source).name == "old.txt":
                raise OSError("simulated backup failure")
            return real_copy(source, destination, *args, **kwargs)

        with patch("appdock.shutil.copy2", side_effect=fail_during_backup):
            with self.assertRaises(AppDockError):
                apply_update(staged, install, self.config.data_root)

        self.assertEqual((install / "appdock.py").read_bytes(), b"old app")
        self.assertEqual((install / "old.txt").read_bytes(), b"keep me")

    def test_updater_has_external_restart_helper_boundary(self) -> None:
        self.assertTrue(callable(getattr(appdock, "launch_update_helper", None)))

    def test_update_coordinator_atomically_claims_one_apply(self) -> None:
        coordinator = appdock.UpdateCoordinator()
        coordinator.store({"digest": "confirmed", "version": "0.2.0"})
        with self.assertRaises(AppDockError):
            coordinator.store({"digest": "other"})
        claimed = coordinator.claim("confirmed")
        self.assertEqual(claimed["version"], "0.2.0")
        with self.assertRaises(AppDockError):
            coordinator.claim("confirmed")
        with self.assertRaises(AppDockError):
            coordinator.store({"digest": "other"})

    def test_staged_directory_is_removed_when_coordinator_rejects_it(self) -> None:
        coordinator = appdock.UpdateCoordinator()
        coordinator.store({"digest": "already-staged"})
        staged_path = self.config.updates_root / "0.2.0"

        def fake_stager(release, config, **kwargs):
            staged_path.mkdir(parents=True)
            (staged_path / "partial.zip").write_bytes(b"partial")
            return {"digest": "new-stage", "path": str(staged_path), "version": "0.2.0"}

        with self.assertRaises(AppDockError):
            appdock.stage_coordinated_update(
                {"version": "0.2.0"},
                self.config,
                coordinator,
                repository="owner/repo",
                stager=fake_stager,
            )
        self.assertFalse(staged_path.exists())

    def test_concurrent_stage_requests_cannot_delete_the_winning_stage(self) -> None:
        coordinator = appdock.UpdateCoordinator()
        staged_path = self.config.updates_root / "0.2.3"
        first_entered = threading.Event()
        release_first = threading.Event()
        results: list[dict[str, object]] = []
        errors: list[Exception] = []
        stager_calls = 0
        stager_lock = threading.Lock()

        def first_stager(release, config, **kwargs):
            nonlocal stager_calls
            with stager_lock:
                stager_calls += 1
            staged_path.mkdir(parents=True, exist_ok=True)
            (staged_path / "owner.txt").write_text("first", encoding="utf-8")
            first_entered.set()
            self.assertTrue(release_first.wait(timeout=3))
            return {"digest": "first", "path": str(staged_path), "version": "0.2.3"}

        def run_first():
            try:
                results.append(
                    appdock.stage_coordinated_update(
                        {"version": "0.2.3"}, self.config, coordinator,
                        repository="owner/repo", stager=first_stager,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_first)
        thread.start()
        self.assertTrue(first_entered.wait(timeout=3))

        def second_stager(release, config, **kwargs):
            nonlocal stager_calls
            with stager_lock:
                stager_calls += 1
            staged_path.mkdir(parents=True, exist_ok=True)
            (staged_path / "owner.txt").write_text("second", encoding="utf-8")
            return {"digest": "second", "path": str(staged_path), "version": "0.2.3"}

        try:
            results.append(
                appdock.stage_coordinated_update(
                    {"version": "0.2.3"}, self.config, coordinator,
                    repository="owner/repo", stager=second_stager,
                )
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            release_first.set()
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(stager_calls, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertTrue(staged_path.is_dir())
        claimed = coordinator.claim(str(results[0]["digest"]))
        self.assertTrue(Path(str(claimed["path"])).is_dir())

    def test_update_helper_preserves_option_like_restart_arguments(self) -> None:
        install = self.root / "install"
        install.mkdir()
        (install / "appdock.py").write_text("print('ok')", encoding="utf-8")
        staged = self.config.updates_root / "0.2.0"
        staged.mkdir(parents=True)
        self.write_release_tree(staged)
        captured: list[list[str]] = []

        def ready_popen(command, **kwargs):
            captured.append(command)
            handshake = Path(command[command.index("--handshake") + 1])
            token = command[command.index("--handshake-token") + 1]
            handshake.write_text(token, encoding="utf-8")
            return SimpleNamespace(pid=123, poll=lambda: None)

        launch_update_helper(
            staged,
            install,
            self.config.data_root,
            restart_args=["--host", "127.0.0.1", "--port", "8876"],
            popen=ready_popen,
        )

        self.assertEqual(captured[0][1], "-B")
        self.assertEqual(Path(captured[0][2]), staged / "scripts" / "update_helper.py")
        self.assertIn("--restart-arg=--port", captured[0])

    def test_update_helper_launch_requires_transaction_handshake(self) -> None:
        install = self.root / "install-helper-handshake"
        install.mkdir()
        (install / "appdock.py").write_text("print('ok')", encoding="utf-8")
        staged = self.config.updates_root / "0.3.0"
        staged.mkdir(parents=True)
        self.write_release_tree(staged)

        exited = SimpleNamespace(poll=lambda: 1)
        with self.assertRaises(AppDockError):
            launch_update_helper(
                staged,
                install,
                self.config.data_root,
                restart_args=["--host", "127.0.0.1"],
                popen=lambda command, **kwargs: exited,
            )

    def test_update_helper_rolls_back_if_restart_launch_fails(self) -> None:
        install = self.root / "install"
        install.mkdir()
        self.write_release_tree(install, {"appdock.py": b"old"})
        installed_entry = install / "appdock.py"
        staged = self.config.updates_root / "0.2.0"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new"})

        with patch("scripts.update_helper.subprocess.Popen", side_effect=OSError("restart failed")):
            result = update_helper.run(
                staged,
                install,
                self.config.data_root,
                0,
                installed_entry,
                [],
            )

        self.assertEqual(result, 1)
        self.assertEqual(installed_entry.read_text(encoding="utf-8"), "old")

    def test_update_helper_rolls_back_if_restarted_process_exits_before_ready(self) -> None:
        install = self.root / "install-early-exit"
        install.mkdir()
        self.write_release_tree(install, {"appdock.py": b"old"})
        installed_entry = install / "appdock.py"
        staged = self.config.updates_root / "0.2.1"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new"})

        child = SimpleNamespace(poll=lambda: 1)
        with patch("scripts.update_helper.subprocess.Popen", return_value=child):
            result = update_helper.run(
                staged,
                install,
                self.config.data_root,
                0,
                installed_entry,
                ["--host", "127.0.0.1", "--port", "8876"],
            )

        self.assertEqual(result, 1)
        self.assertEqual(installed_entry.read_text(encoding="utf-8"), "old")
        self.assertFalse(staged.exists(), "failed update left a version stage that blocks retry")

    def test_update_helper_restarts_restored_version_after_failed_new_version(self) -> None:
        install = self.root / "install-restored-restart"
        install.mkdir()
        self.write_release_tree(install, {"appdock.py": b"old"})
        installed_entry = install / "appdock.py"
        staged = self.config.updates_root / "0.2.6"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new"})

        failed_child = SimpleNamespace(poll=lambda: 1)
        restored_child = SimpleNamespace(poll=lambda: None)
        children = iter([failed_child, restored_child])
        readiness_calls: list[tuple[object, str]] = []

        def readiness(process, restart_args, token, timeout=20):
            readiness_calls.append((process, token))
            if process is failed_child:
                raise RuntimeError("new version failed")

        with patch("scripts.update_helper.subprocess.Popen", side_effect=lambda *args, **kwargs: next(children)), patch(
            "scripts.update_helper._wait_for_restart_ready", side_effect=readiness
        ):
            result = update_helper.run(staged, install, self.config.data_root, 0, installed_entry, [])

        self.assertEqual(result, 1)
        self.assertEqual(installed_entry.read_text(encoding="utf-8"), "old")
        self.assertEqual([call[0] for call in readiness_calls], [failed_child, restored_child])

    def test_update_helper_restarts_existing_version_after_backup_failure(self) -> None:
        install = self.root / "install-backup-service"
        install.mkdir()
        self.write_release_tree(install, {"appdock.py": b"old", "old.txt": b"keep"})
        installed_entry = install / "appdock.py"
        staged = self.config.updates_root / "0.2.9"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new"})

        real_copy = shutil.copy2
        backup_root = (self.config.data_root / "updates" / "backups").resolve()

        def fail_backup(source, destination, *args, **kwargs):
            if appdock._inside(Path(destination).resolve(), backup_root) and Path(source).name == "old.txt":
                raise OSError("simulated backup failure")
            return real_copy(source, destination, *args, **kwargs)

        restored_child = SimpleNamespace(poll=lambda: None)
        readiness: list[object] = []
        with patch("appdock.shutil.copy2", side_effect=fail_backup), patch(
            "scripts.update_helper.subprocess.Popen", return_value=restored_child
        ), patch(
            "scripts.update_helper._wait_for_restart_ready", side_effect=lambda process, *args, **kwargs: readiness.append(process)
        ):
            result = update_helper.run(staged, install, self.config.data_root, 0, installed_entry, [])

        self.assertEqual(result, 1)
        self.assertEqual(installed_entry.read_text(encoding="utf-8"), "old")
        self.assertEqual(readiness, [restored_child])

    def test_update_helper_accepts_restart_only_after_appdock_health_is_ready(self) -> None:
        install = self.root / "install-ready"
        install.mkdir()
        self.write_release_tree(install, {"appdock.py": b"old"})
        installed_entry = install / "appdock.py"
        staged = self.config.updates_root / "0.2.2"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new"})

        child = SimpleNamespace(poll=lambda: None)

        ready_token = "ready-token-1234567890"

        class HealthyResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"ok": True, "service": "appdock", "ready_token": ready_token}).encode("utf-8")

            def geturl(self):
                return "http://127.0.0.1:8876/health"

        with patch("scripts.update_helper.secrets.token_urlsafe", return_value=ready_token), patch(
            "scripts.update_helper.subprocess.Popen", return_value=child
        ), patch(
            "scripts.update_helper.urllib.request.urlopen", return_value=HealthyResponse()
        ) as health:
            result = update_helper.run(
                staged,
                install,
                self.config.data_root,
                0,
                installed_entry,
                ["--host", "127.0.0.1", "--port", "8876"],
            )

        self.assertEqual(result, 0)
        self.assertEqual(installed_entry.read_text(encoding="utf-8"), "new")
        self.assertFalse(staged.exists())
        self.assertIn("127.0.0.1:8876/health", health.call_args.args[0].full_url)

    def test_update_helper_rejects_health_from_an_unrelated_appdock_instance(self) -> None:
        install = self.root / "install-unrelated-health"
        install.mkdir()
        self.write_release_tree(install, {"appdock.py": b"old"})
        installed_entry = install / "appdock.py"
        staged = self.config.updates_root / "0.2.4"
        staged.mkdir(parents=True)
        self.write_release_tree(staged, {"appdock.py": b"new"})

        class RunningChild:
            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        class UnrelatedHealthyResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true, "service": "appdock"}'

            def geturl(self):
                return "http://127.0.0.1:8876/health"

        with patch("scripts.update_helper.subprocess.Popen", return_value=RunningChild()), patch(
            "scripts.update_helper.urllib.request.urlopen", return_value=UnrelatedHealthyResponse()
        ), patch("scripts.update_helper.time.monotonic", side_effect=[0, 0, 21]), patch(
            "scripts.update_helper.time.sleep", return_value=None
        ):
            result = update_helper.run(
                staged,
                install,
                self.config.data_root,
                0,
                installed_entry,
                ["--host", "127.0.0.1", "--port", "8876"],
            )

        self.assertEqual(result, 1)
        self.assertEqual(installed_entry.read_text(encoding="utf-8"), "old")
        self.assertFalse(staged.exists())

    def test_update_helper_rejects_redirected_health_even_with_the_right_token(self) -> None:
        ready_token = "ready-token-1234567890"

        class RunningChild:
            def poll(self):
                return None

        class RedirectedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"ok": True, "service": "appdock", "ready_token": ready_token}).encode("utf-8")

            def geturl(self):
                return "http://127.0.0.1:9999/health"

        with patch("scripts.update_helper.urllib.request.urlopen", return_value=RedirectedResponse()):
            with self.assertRaises(RuntimeError):
                update_helper._wait_for_restart_ready(
                    RunningChild(),
                    ["--host", "127.0.0.1", "--port", "8876"],
                    ready_token,
                    timeout=0.1,
                )

    def test_public_server_refuses_non_loopback_bind_by_default(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertEqual(validate_bind_host(host), host)
        for host in ("0.0.0.0", "192.0.2.10", "example.com"):
            with self.subTest(host=host), self.assertRaises(AppDockError):
                validate_bind_host(host)

    def test_http_rejects_unapproved_host_header(self) -> None:
        class TestHandler(Handler):
            pass

        TestHandler.config = self.config
        TestHandler.manager = AppManager(config=self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/health",
                headers={"Host": "attacker.example"},
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 421)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_allows_explicit_private_proxy_host(self) -> None:
        class TestHandler(Handler):
            pass

        TestHandler.config = self.config
        TestHandler.manager = AppManager(config=self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/health",
                headers={"Host": "appdock.private.example"},
            )
            with patch.dict("os.environ", {"APPDOCK_ALLOWED_HOSTS": "appdock.private.example"}):
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_health_echoes_only_the_running_instance_readiness_token(self) -> None:
        class TokenHandler(Handler):
            pass

        TokenHandler.config = self.config
        TokenHandler.manager = AppManager(config=self.config)
        TokenHandler.ready_token = "ready-token-1234567890"
        server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=3) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["ready_token"], TokenHandler.ready_token)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_responses_include_local_control_plane_security_headers(self) -> None:
        class TestHandler(Handler):
            pass

        TestHandler.config = self.config
        TestHandler.manager = AppManager(config=self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=3) as response:
                headers = response.headers
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
                self.assertEqual(headers.get("Cache-Control"), "no-store")
                self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_routine_api_responses_do_not_expose_absolute_paths(self) -> None:
        self.write_manifest(self.config.registry_root / "demo")

        class TestHandler(Handler):
            pass

        TestHandler.config = self.config
        TestHandler.manager = AppManager(config=self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            apps = json.loads(urllib.request.urlopen(base + "/api/apps", timeout=3).read())
            config = json.loads(urllib.request.urlopen(base + "/api/config", timeout=3).read())
            self.assertNotIn("directory", apps[0])
            self.assertNotIn("log_path", apps[0])
            self.assertNotIn("data_root", config)
            self.assertNotIn("registry_root", config)
            self.assertNotIn("install_root", config)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_dashboard_keeps_links_ordering_and_log_access(self) -> None:
        source = appdock.HTML + (Path(appdock.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("local_url", source)
        self.assertIn("private_url", source)
        self.assertIn("move-up", source)
        self.assertIn("/logs", source)

    def test_dashboard_can_complete_both_registration_flows(self) -> None:
        source = appdock.HTML + (Path(appdock.__file__).resolve().parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("[truncated]", source)
        self.assertIn("/api/onboarding/local/register", source)
        self.assertIn("/api/onboarding/github/register", source)
        self.assertIn("Register app", appdock.HTML)
        self.assertIn("<details", appdock.HTML)
        self.assertIn('src="/static/app.js"', appdock.HTML)
        self.assertIn('href="/static/app.css"', appdock.HTML)


if __name__ == "__main__":
    unittest.main()
