from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import appdock


class _ProviderHandler(BaseHTTPRequestHandler):
    status = 200
    content_type = "application/json"
    body = b"{}"
    headers_extra: dict[str, str] = {}

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        for key, value in self.headers_extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = appdock.AppDockConfig.from_environment(data_dir=self.root / "data")
        self.config.ensure()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def _raw_config(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "visibility": {"hidden_app_ids": ["synthetic-backend"]},
            "providers": [
                {
                    "id": "synthetic-provider",
                    "url": f"http://127.0.0.1:{self.port}/widgets",
                    "connect_timeout_ms": 200,
                    "read_timeout_ms": 400,
                    "cache_seconds": 0,
                }
            ],
            "widgets": [
                {
                    "id": "synthetic-metrics",
                    "type": "metrics",
                    "title": "Synthetic metrics",
                    "provider_id": "synthetic-provider",
                    "drill_down_url": "https://private.example.invalid/details",
                },
                {
                    "id": "synthetic-progress",
                    "type": "progress",
                    "title": "Synthetic progress",
                    "provider_id": "synthetic-provider",
                },
            ],
        }

    def _write_config(self, raw: dict[str, object] | None = None) -> None:
        self.config.extension_config_path.write_text(json.dumps(raw or self._raw_config()), encoding="utf-8")

    def test_extension_config_rejects_remote_dns_credentials_and_unknown_fields(self) -> None:
        raw = self._raw_config()
        raw["providers"][0]["url"] = "http://localhost:19091/widgets"  # type: ignore[index]
        with self.assertRaises(appdock.AppDockError):
            appdock.parse_extension_config(raw)
        raw = self._raw_config()
        raw["providers"][0]["url"] = "http://user:pass@127.0.0.1:19091/widgets"  # type: ignore[index]
        with self.assertRaises(appdock.AppDockError):
            appdock.parse_extension_config(raw)
        raw = self._raw_config()
        raw["widgets"][0]["html"] = "<b>unsafe</b>"  # type: ignore[index]
        with self.assertRaises(appdock.AppDockError):
            appdock.parse_extension_config(raw)

    def test_provider_payload_is_bounded_and_normalized(self) -> None:
        self._write_config()
        _ProviderHandler.status = 200
        _ProviderHandler.content_type = "application/json"
        _ProviderHandler.headers_extra = {}
        _ProviderHandler.body = json.dumps(
            {
                "schema_version": 1,
                "widgets": {
                    "synthetic-metrics": {
                        "status": "ok",
                        "metrics": [{"label": "CPU", "value": "42%"}],
                        "timestamp": "recent",
                    },
                    "synthetic-progress": {
                        "status": "warning",
                        "progress": [{"label": "Window", "value": 0.25, "reset_at": "later"}],
                    },
                },
            }
        ).encode()
        result = appdock.ExtensionManager(self.config).snapshot()
        self.assertTrue(result["enabled"])
        self.assertEqual(result["widgets"][0]["metrics"], [{"label": "CPU", "value": "42%"}])
        self.assertEqual(result["widgets"][1]["progress"][0]["value"], 0.25)
        self.assertEqual(result["widgets"][0]["drill_down_url"], "https://private.example.invalid/details")

    def test_provider_redirect_wrong_type_markup_and_oversize_fail_closed(self) -> None:
        self._write_config()
        manager = appdock.ExtensionManager(self.config)
        for status, content_type, body, extra in [
            (302, "application/json", b"{}", {"Location": "http://127.0.0.1:1/elsewhere"}),
            (200, "text/html", b"{}", {}),
            (200, "application/json", b"x" * (appdock.MAX_PROVIDER_RESPONSE_BYTES + 1), {}),
            (
                200,
                "application/json",
                json.dumps({"schema_version": 1, "widgets": {"synthetic-metrics": {"status": "ok", "metrics": [{"label": "<b>x</b>", "value": "1"}]}}}).encode(),
                {},
            ),
        ]:
            _ProviderHandler.status = status
            _ProviderHandler.content_type = content_type
            _ProviderHandler.body = body
            _ProviderHandler.headers_extra = extra
            manager._provider_cache.clear()
            result = manager.snapshot()
            self.assertTrue(all(widget["status"] == "unavailable" for widget in result["widgets"]))

    def test_visibility_filters_cards_without_changing_eight_id_order(self) -> None:
        self._write_config({
            **self._raw_config(),
            "visibility": {"hidden_app_ids": ["synthetic-backend"]},
            "providers": [],
            "widgets": [],
        })
        ids = [f"synthetic-{index}" for index in range(7)] + ["synthetic-backend"]
        for app_id in ids:
            manifest_dir = self.config.registry_root / app_id
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "appdock.json").write_text(json.dumps({
                "id": app_id,
                "name": app_id,
                "external": True,
                "directory": str(self.root / app_id),
                "command": ["python", "-V"],
            }), encoding="utf-8")
        self.config.order_path.write_text(json.dumps(ids), encoding="utf-8")
        manager = appdock.AppManager(config=self.config, extensions=appdock.ExtensionManager(self.config))
        self.assertEqual([item["id"] for item in manager.all_status()], ids[:7])
        self.assertEqual(json.loads(self.config.order_path.read_text()), ids)


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.package = self.root / "private-package"
        self.package.mkdir()
        self.config = appdock.AppDockConfig.from_environment(data_dir=self.data)
        self.ids = [f"synthetic-{index}" for index in range(7)] + ["synthetic-backend"]
        registration_paths = []
        for app_id in self.ids:
            path = self.package / "migration" / "registry" / app_id / "appdock.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "id": app_id,
                "name": app_id,
                "description": "synthetic fixture",
                "directory": str(self.root / "sources" / app_id),
                "external": True,
                "command": ["python", "-m", "synthetic"],
                "local_url": "http://127.0.0.1:19000",
                "private_url": "https://private.example.invalid/app",
            }), encoding="utf-8")
            registration_paths.append(path.relative_to(self.package).as_posix())
        (self.package / "migration" / "app-order.json").write_text(json.dumps(self.ids), encoding="utf-8")
        (self.package / "migration" / "extensions.json").write_text(json.dumps({
            "schema_version": 1,
            "visibility": {"hidden_app_ids": ["synthetic-backend"]},
            "providers": [],
            "widgets": [],
        }), encoding="utf-8")
        (self.package / appdock.PRIVATE_PACKAGE_MANIFEST).write_text(json.dumps({
            "schema_version": 1,
            "registrations": registration_paths,
            "order_path": "migration/app-order.json",
            "extension_config_path": "migration/extensions.json",
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_import_restart_idempotence_and_rollback(self) -> None:
        preview = appdock.preview_private_package(self.package)
        self.assertEqual(preview["registration_count"], 8)
        self.assertEqual(preview["visible_count"], 7)
        self.assertFalse(self.data.exists())
        first = appdock.import_private_package(self.package, self.config)
        self.assertTrue(first["changed"])
        manager = appdock.AppManager(config=self.config, extensions=appdock.ExtensionManager(self.config))
        self.assertEqual(len(manager.discover()), 8)
        self.assertEqual(len(manager.all_status()), 7)
        self.assertEqual(json.loads(self.config.order_path.read_text()), self.ids)
        second = appdock.import_private_package(self.package, self.config)
        self.assertFalse(second["changed"])
        rolled_back = appdock.rollback_private_package(first["receipt"], self.config)
        self.assertTrue(rolled_back["rolled_back"])
        self.assertFalse(self.config.order_path.exists())
        self.assertFalse(self.config.extension_config_path.exists())

    def test_failure_injection_restores_previous_state(self) -> None:
        self.config.ensure()
        self.config.order_path.write_text(json.dumps(["existing"]), encoding="utf-8")
        count = 0

        def fail_after_two(_target: str) -> None:
            nonlocal count
            count += 1
            if count == 3:
                raise RuntimeError("synthetic failure")

        with self.assertRaises(RuntimeError):
            appdock.import_private_package(self.package, self.config, failure_hook=fail_after_two)
        self.assertEqual(json.loads(self.config.order_path.read_text()), ["existing"])
        self.assertFalse(self.config.extension_config_path.exists())

    def test_rejects_duplicate_order_and_relative_source(self) -> None:
        order_path = self.package / "migration" / "app-order.json"
        order_path.write_text(json.dumps([self.ids[0]] * 8), encoding="utf-8")
        with self.assertRaises(appdock.AppDockError):
            appdock.preview_private_package(self.package)
        order_path.write_text(json.dumps(self.ids), encoding="utf-8")
        manifest_path = self.package / "migration" / "registry" / self.ids[0] / "appdock.json"
        raw = json.loads(manifest_path.read_text())
        raw["directory"] = "relative/source"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(appdock.AppDockError):
            appdock.preview_private_package(self.package)


if __name__ == "__main__":
    unittest.main()
