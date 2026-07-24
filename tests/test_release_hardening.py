from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
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

    def test_archive_rejects_device_names_ads_and_expansion_limit(self) -> None:
        for member in ("CON/appdock.py", "folder/NUL.txt", "appdock.py:stream"):
            with self.subTest(member=member), self.assertRaises(AppDockError):
                validate_zip(self.make_zip({member: b"x"}))
        with patch.object(appdock, "MAX_UPDATE_UNCOMPRESSED_BYTES", 4, create=True):
            with self.assertRaises(AppDockError):
                validate_zip(self.make_zip({"appdock.py": b"12345"}))

    def test_update_archive_requires_appdock_entry_point(self) -> None:
        with self.assertRaises(AppDockError):
            validate_zip(self.make_zip({"README.md": b"not an application"}))

    def test_trusted_asset_path_must_start_with_exact_repository(self) -> None:
        deceptive = "https://github.com/other/project/owner/repo/releases/download/v1.0.0/"
        release = {"assets": [
            {"name": "appdock-windows.zip", "url": deceptive + "appdock-windows.zip"},
            {"name": "SHA256SUMS.txt", "url": deceptive + "SHA256SUMS.txt"},
        ]}
        with self.assertRaises(AppDockError):
            select_trusted_assets(release, "owner/repo")

    def test_semver_numeric_prerelease_identifiers_compare_numerically(self) -> None:
        self.assertGreater(compare_semver("1.0.0-10", "1.0.0-2"), 0)
        self.assertLess(compare_semver("1.0.0-alpha", "1.0.0-alpha.1"), 0)

    def test_apply_update_rejects_install_data_overlap(self) -> None:
        staged = self.config.updates_root / "0.2.0"
        staged.mkdir(parents=True)
        (staged / "appdock.py").write_text("new", encoding="utf-8")
        with self.assertRaises(AppDockError):
            apply_update(staged, self.config.data_root, self.config.data_root)

    def test_updater_has_external_restart_helper_boundary(self) -> None:
        self.assertTrue(callable(getattr(appdock, "launch_update_helper", None)))

    def test_update_helper_preserves_option_like_restart_arguments(self) -> None:
        install = self.root / "install"
        install.mkdir()
        (install / "appdock.py").write_text("print('ok')", encoding="utf-8")
        staged = self.config.updates_root / "0.2.0"
        staged.mkdir(parents=True)
        captured: list[list[str]] = []

        launch_update_helper(
            staged,
            install,
            self.config.data_root,
            helper_path=Path(appdock.__file__).resolve().parent / "scripts" / "update_helper.py",
            restart_args=["--host", "127.0.0.1", "--port", "8876"],
            popen=lambda command, **kwargs: captured.append(command) or SimpleNamespace(pid=123),
        )

        self.assertIn("--restart-arg=--host", captured[0])
        self.assertIn("--restart-arg=--port", captured[0])

    def test_update_helper_rolls_back_if_restart_launch_fails(self) -> None:
        install = self.root / "install"
        install.mkdir()
        installed_entry = install / "appdock.py"
        installed_entry.write_text("old", encoding="utf-8")
        staged = self.config.updates_root / "0.2.0"
        staged.mkdir(parents=True)
        (staged / "appdock.py").write_text("new", encoding="utf-8")

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
