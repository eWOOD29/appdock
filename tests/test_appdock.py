from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
import threading
import tempfile
import unittest
import zipfile

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from appdock import (  # noqa: E402
    AppDockConfig,
    AppDockError,
    AppManager,
    Handler,
    HTML,
    GitHubOnboarding,
    LocalFolderOnboarding,
    ReleaseChecker,
    apply_update,
    canonical_github_url,
    compare_semver,
    parse_release,
    select_trusted_assets,
    stage_update,
    validate_zip,
    verify_sha256,
)


class Response:
    def __init__(self, data: bytes, status: int = 200):
        self.data = data
        self.status = status
        self.headers = {"Content-Length": str(len(data))}
    def read(self, limit: int = -1) -> bytes:
        return self.data if limit < 0 else self.data[:limit]
    def __enter__(self): return self
    def __exit__(self, *args): return None


class AppDockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppDockConfig.from_environment(data_dir=self.root / "data")
        self.config.ensure()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_manifest(self, directory: Path, app_id: str = "demo", **extra: object) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"id": app_id, "name": "Demo", "command": [sys.executable, "-c", "import time; time.sleep(30)"], **extra}
        (directory / "appdock.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_public_defaults_are_machine_neutral(self) -> None:
        config = AppDockConfig.from_environment(repo_root=self.root, platform="win32", local_app_data=self.root / "local")
        expected = self.root / "local" / "AppDock"
        self.assertEqual(os.path.normcase(os.path.realpath(config.data_root)), os.path.normcase(os.path.realpath(expected)))
        self.assertEqual(config.registry_root, config.data_root / "registry")
        self.assertEqual(config.install_root, config.data_root / "apps")
        self.assertNotIn("Ethan", str(config.data_root))
        self.assertNotIn("100.110", str(config.data_root))

    def test_data_dir_override_wins_and_state_is_outside_repo(self) -> None:
        with patch.dict(os.environ, {"APPDOCK_DATA_DIR": str(self.root / "override")}):
            config = AppDockConfig.from_environment(repo_root=self.root)
        expected = self.root / "override"
        self.assertEqual(os.path.normcase(os.path.realpath(config.data_root)), os.path.normcase(os.path.realpath(expected)))
        self.assertNotEqual(config.data_root, self.root)

    def test_discover_start_stop_and_shell_false(self) -> None:
        apps = self.root / "apps-direct"
        self.write_manifest(apps / "demo")
        manager = AppManager(apps)
        self.assertEqual(list(manager.discover()), ["demo"])
        with patch("appdock.subprocess.Popen", wraps=subprocess.Popen) as popen:
            started = manager.start("demo")
            self.assertIn(started["state"], {"running", "healthy"})
            self.assertTrue(popen.call_args.kwargs["shell"] is False)
        self.assertIsNotNone(started["pid"])
        self.assertEqual(manager.stop("demo")["state"], "stopped")

    def test_no_pid_low_level_stop_path(self) -> None:
        self.write_manifest(self.root / "apps-direct" / "demo")
        manager = AppManager(self.root / "apps-direct")
        self.assertEqual(manager.stop("demo")["state"], "stopped")

    def test_manifest_path_containment_and_safe_id(self) -> None:
        apps = self.root / "apps-direct"
        self.write_manifest(apps / "demo", cwd="..")
        self.write_manifest(apps / "other", app_id="../escape")
        self.assertEqual(AppManager(apps).discover(), {})

    def test_local_preview_register_requires_digest_and_does_not_start(self) -> None:
        source = self.root / "source"
        self.write_manifest(source, app_id="local_app")
        onboarding = LocalFolderOnboarding(self.config)
        preview = onboarding.preview(source)
        self.assertEqual(preview["app"]["id"], "local_app")
        with self.assertRaises(AppDockError): onboarding.register(source, "wrong", preview)
        with patch.object(AppManager, "start") as start:
            result = onboarding.register(source, preview["digest"], preview)
            start.assert_not_called()
        self.assertFalse(result["started"])
        registered = self.config.registry_root / "local_app" / "appdock.json"
        self.assertTrue(registered.is_file())
        self.assertTrue(json.loads(registered.read_text())["external"])

    def test_local_preview_digest_changes_when_manifest_changes(self) -> None:
        source = self.root / "source"
        self.write_manifest(source)
        onboarding = LocalFolderOnboarding(self.config)
        preview = onboarding.preview(source)
        self.write_manifest(source, description="changed")
        with self.assertRaises(AppDockError): onboarding.register(source, preview["digest"], preview)
        self.assertFalse((self.config.registry_root / "demo").exists())

    def test_github_url_validation(self) -> None:
        self.assertEqual(canonical_github_url("https://github.com/owner/repo"), "https://github.com/owner/repo.git")
        self.assertEqual(canonical_github_url("https://github.com/owner/repo.git"), "https://github.com/owner/repo.git")
        for value in ("http://github.com/owner/repo", "https://github.com/owner/repo/issues", "git@github.com:owner/repo.git", "https://evil.example/owner/repo", "https://user:pass@github.com/owner/repo", "https://github.com/owner/repo?x=1"):
            with self.subTest(value=value), self.assertRaises(AppDockError): canonical_github_url(value)

    def test_github_clone_is_argument_list_shell_false_and_registration_is_explicit(self) -> None:
        commands: list[list[str]] = []
        def runner(command, **kwargs):
            commands.append(command)
            stage = Path(command[-1]); self.write_manifest(stage, app_id="remote"); return SimpleNamespace(returncode=0)
        onboarding = GitHubOnboarding(self.config, runner=runner)
        preview = onboarding.preview("https://github.com/owner/repo")
        self.assertEqual(commands[0][:6], ["git", "-c", "core.hooksPath=", "clone", "--depth", "1"])
        self.assertIn("--single-branch", commands[0])
        self.assertIn("--no-tags", commands[0])
        self.assertFalse("shell" in commands[0])
        with self.assertRaises(AppDockError): onboarding.register(preview, "tampered")
        result = onboarding.register(preview, preview["digest"])
        self.assertEqual(result, {"registered": True, "id": "remote", "started": False})
        self.assertTrue((self.config.install_root / "remote" / "appdock.json").is_file())
        self.assertEqual(list(AppManager(config=self.config).discover()), ["remote"])

    def test_github_cleanup_removes_abandoned_staging(self) -> None:
        onboarding = GitHubOnboarding(self.config, runner=lambda command, **kwargs: SimpleNamespace(returncode=1))
        with self.assertRaises(AppDockError): onboarding.preview("https://github.com/owner/repo")
        self.assertTrue(list(self.config.staging_root.iterdir()) == [])

    def test_semver_and_release_parsing(self) -> None:
        self.assertLess(compare_semver("0.2.0", "0.3.0"), 0)
        self.assertGreater(compare_semver("v1.0.0", "0.9.9"), 0)
        release = parse_release({"tag_name": "v0.2.0", "html_url": "https://github.com/owner/repo/releases/tag/v0.2.0", "body": "notes", "assets": [{"name": "appdock-windows.zip", "browser_download_url": "https://github.com/owner/repo/releases/download/v0.2.0/appdock-windows.zip"}]})
        self.assertEqual(release["version"], "0.2.0")

    def test_release_checker_caches_and_reports_update(self) -> None:
        calls = []
        payload = json.dumps({"tag_name": "v0.2.0", "html_url": "https://github.com/owner/repo/releases/v0.2.0", "assets": []}).encode()
        checker = ReleaseChecker("owner/repo", opener=lambda *args, **kwargs: calls.append(1) or Response(payload), cache_ttl=60)
        self.assertTrue(checker.check()["update_available"])
        checker.check()
        self.assertEqual(len(calls), 1)

    def test_trusted_assets_require_both_named_github_assets(self) -> None:
        base = "https://github.com/owner/repo/releases/download/v0.2.0/"
        release = {"assets": [{"name": "appdock-windows.zip", "url": base + "appdock-windows.zip"}, {"name": "SHA256SUMS.txt", "url": base + "SHA256SUMS.txt"}]}
        self.assertEqual(set(select_trusted_assets(release, "owner/repo")), {"appdock-windows.zip", "SHA256SUMS.txt"})
        with self.assertRaises(AppDockError): select_trusted_assets({"assets": [{"name": "appdock-windows.zip", "url": "https://evil.example/file"}]}, "owner/repo")

    def make_zip(self, member: str = "appdock.py", content: bytes = b"new") -> bytes:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive: archive.writestr(member, content)
        return stream.getvalue()

    def make_release_zip(self, appdock_content: bytes = b"new") -> bytes:
        members = {
            "appdock.py": appdock_content,
            "static/app.js": b"js",
            "static/app.css": b"css",
            "scripts/update_helper.py": b"helper",
            "scripts/path_safety.ps1": b"safety",
            "scripts/install.ps1": b"install",
            "scripts/uninstall.ps1": b"uninstall",
        }
        manifest = {"schema_version": 2, "files": [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in sorted(members.items())
        ]}
        members["RELEASE-MANIFEST.json"] = json.dumps(manifest, sort_keys=True).encode()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for path, content in members.items():
                archive.writestr(path, content)
        return stream.getvalue()

    def test_checksum_and_zip_slip_symlink_rejection(self) -> None:
        data = self.make_release_zip(); checksum = hashlib.sha256(data).hexdigest()
        self.assertEqual(verify_sha256(data, f"{checksum}  appdock-windows.zip"), checksum)
        with self.assertRaises(AppDockError): verify_sha256(data, "0" * 64 + "  appdock-windows.zip")
        self.assertIn("appdock.py", validate_zip(data))
        with self.assertRaises(AppDockError): validate_zip(self.make_zip("../escape"))
        with self.assertRaises(AppDockError): validate_zip(self.make_zip("data/user.json"))

    def test_stage_update_uses_trusted_assets_checksum_and_preserves_data_on_apply(self) -> None:
        data = self.make_release_zip(b"new code"); sums = hashlib.sha256(data).hexdigest().encode() + b"  appdock-windows.zip\n"
        base = "https://github.com/owner/repo/releases/download/v0.2.0/"
        release = {"version": "0.2.0", "release_url": base, "assets": [{"name": "appdock-windows.zip", "url": base + "appdock-windows.zip"}, {"name": "SHA256SUMS.txt", "url": base + "SHA256SUMS.txt"}]}
        opener = lambda request, **kwargs: Response(data if request.full_url.endswith(".zip") else sums)
        staged = stage_update(release, self.config, opener=opener, repository="owner/repo")
        install = self.root / "install"; install.mkdir(); (install / "appdock.py").write_bytes(b"old"); (self.config.data_root / "keep.json").write_text("keep")
        result = apply_update(staged["path"], install, self.config.data_root)
        self.assertTrue(result["applied"]); self.assertEqual((install / "appdock.py").read_bytes(), b"new code"); self.assertEqual((self.config.data_root / "keep.json").read_text(), "keep")
    def test_http_post_requires_json_and_rejects_cross_origin(self) -> None:
        old_config, old_manager = Handler.config, Handler.manager
        Handler.config = self.config
        Handler.manager = AppManager(config=self.config)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            config_response = json.loads(urllib.request.urlopen(base + "/api/config").read())
            self.assertEqual(config_response["version"], "0.1.2")
            request = urllib.request.Request(base + "/api/apps/example/start", data=b"{}", method="POST", headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as cross_origin:
                urllib.request.urlopen(request)
            self.assertEqual(cross_origin.exception.code, 403)
            request = urllib.request.Request(base + "/api/apps/example/start", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as missing_type:
                urllib.request.urlopen(request)
            self.assertEqual(missing_type.exception.code, 400)
            self.assertIn("Add App", HTML)
            self.assertIn("Advanced GitHub", HTML)
            self.assertIn("Update now", HTML)
        finally:
            server.shutdown()
            server.server_close()
            Handler.config, Handler.manager = old_config, old_manager


if __name__ == "__main__": unittest.main()
