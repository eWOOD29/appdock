from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import appdock


class _ProviderHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).requests += 1
        widget_id = "status-b" if self.path.endswith("/bravo") else "status-a"
        payload = {
            "schema_version": 1,
            "widgets": {
                widget_id: {
                    "status": "ok",
                    "metrics": [{"label": "Requests", "value": str(type(self).requests)}],
                    "timestamp": "2026-07-25T00:00:00Z",
                }
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ExtensionContentIdentityTests(unittest.TestCase):
    TARGET_SIZE = 2048
    REQUESTED_MTIME_NS = 1_700_000_000_000_000_000

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = appdock.AppDockConfig.from_environment(data_dir=self.root / "data")
        self.config.ensure()
        for app_id in ("visible", "hidden-a", "hidden-b"):
            manifest = self.config.registry_root / app_id / appdock.MANIFEST_NAME
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {
                        "id": app_id,
                        "name": app_id,
                        "external": True,
                        "directory": str(self.root / app_id),
                        "command": [sys.executable, "-c", "pass"],
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )

        _ProviderHandler.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.port = self.server.server_address[1]
        self.valid_a = self._valid_config("a", "hidden-a", "alpha")
        self.valid_b = self._valid_config("b", "hidden-b", "bravo")
        self.mtime_ns: int | None = None

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp.cleanup()

    def _valid_config(self, suffix: str, hidden_id: str, path: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "visibility": {"hidden_app_ids": [hidden_id]},
            "providers": [
                {
                    "id": f"provider-{suffix}",
                    "url": f"http://127.0.0.1:{self.port}/{path}",
                    "connect_timeout_ms": 500,
                    "read_timeout_ms": 500,
                    "cache_seconds": 30,
                }
            ],
            "widgets": [
                {
                    "id": f"status-{suffix}",
                    "type": "metrics",
                    "title": f"Status {suffix.upper()}",
                    "provider_id": f"provider-{suffix}",
                    "drill_down_url": f"https://example.invalid/{path}",
                }
            ],
        }

    def _json_bytes(self, value: object) -> bytes:
        return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _fixed_size(self, payload: bytes) -> bytes:
        self.assertLessEqual(len(payload), self.TARGET_SIZE)
        return payload + (b" " * (self.TARGET_SIZE - len(payload)))

    def _replace_preserving_signature(self, payload: bytes) -> tuple[int, int]:
        path = self.config.extension_config_path
        path.write_bytes(self._fixed_size(payload))
        requested = self.REQUESTED_MTIME_NS if self.mtime_ns is None else self.mtime_ns
        os.utime(path, ns=(requested, requested))
        signature = (path.stat().st_size, path.stat().st_mtime_ns)
        if self.mtime_ns is None:
            self.mtime_ns = signature[1]
        self.assertEqual(signature, (self.TARGET_SIZE, self.mtime_ns))
        return signature

    def _new_production_manager(self) -> tuple[appdock.ExtensionManager, appdock.AppManager]:
        extensions = appdock.ExtensionManager(self.config)
        return extensions, appdock.AppManager(config=self.config, extensions=extensions)

    def _assert_valid_a_active(self, extensions: appdock.ExtensionManager, manager: appdock.AppManager) -> None:
        self.assertEqual({item["id"] for item in manager.all_status()}, {"visible", "hidden-b"})
        before = _ProviderHandler.requests
        snapshot = extensions.snapshot(manager.discover())
        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["error"], "")
        self.assertEqual([item["id"] for item in snapshot["widgets"]], ["status-a"])
        self.assertEqual(snapshot["widgets"][0]["drill_down_url"], "https://example.invalid/alpha")
        self.assertEqual(_ProviderHandler.requests, before + 1)
        self.assertTrue(extensions._provider_cache)

    def _assert_invalid_state(self, extensions: appdock.ExtensionManager, manager: appdock.AppManager) -> None:
        self.assertEqual({item["id"] for item in manager.all_status()}, {"visible", "hidden-a", "hidden-b"})
        snapshot = extensions.snapshot(manager.discover())
        self.assertFalse(snapshot["enabled"])
        self.assertEqual(snapshot["widgets"], [])
        self.assertTrue(snapshot["error"])
        self.assertEqual(extensions._active.hidden_app_ids, frozenset())
        self.assertEqual(extensions._active.providers, ())
        self.assertEqual(extensions._active.widgets, ())
        self.assertEqual(extensions._provider_cache, {})

    def test_same_metadata_invalid_replacements_disable_every_derived_state_and_recover(self) -> None:
        duplicate_keys = (
            '{"providers":[],"schema_version":1,"schema_version":1,'
            '"visibility":{"hidden_app_ids":[]},"widgets":[]}'
        ).encode("utf-8")
        unsupported = dict(self.valid_a)
        unsupported["schema_version"] = 2
        unknown_field = dict(self.valid_a)
        unknown_field["unexpected"] = True
        unknown_hidden = dict(self.valid_a)
        unknown_hidden["visibility"] = {"hidden_app_ids": ["unknown-id"]}
        invalid_reference = json.loads(json.dumps(self.valid_a))
        invalid_reference["widgets"][0]["provider_id"] = "missing-provider"
        cases = {
            "malformed JSON": b"{",
            "duplicate JSON keys": duplicate_keys,
            "unsupported version": self._json_bytes(unsupported),
            "unknown field": self._json_bytes(unknown_field),
            "unknown hidden registration": self._json_bytes(unknown_hidden),
            "invalid provider reference": self._json_bytes(invalid_reference),
        }

        for label, invalid_payload in cases.items():
            with self.subTest(label=label):
                self.mtime_ns = None
                self._replace_preserving_signature(self._json_bytes(self.valid_a))
                extensions, manager = self._new_production_manager()
                self._assert_valid_a_active(extensions, manager)
                active_request_count = _ProviderHandler.requests
                metadata = self._replace_preserving_signature(invalid_payload)

                self._assert_invalid_state(extensions, manager)
                self.assertEqual(_ProviderHandler.requests, active_request_count)
                self.assertEqual(
                    (self.config.extension_config_path.stat().st_size, self.config.extension_config_path.stat().st_mtime_ns),
                    metadata,
                )

                # Repeated invalid loads remain disabled and do not resurrect or refetch stale state.
                self._assert_invalid_state(extensions, manager)
                self.assertEqual(_ProviderHandler.requests, active_request_count)

                # Restoring the original valid bytes with the colliding metadata recovers deterministically.
                self._replace_preserving_signature(self._json_bytes(self.valid_a))
                self._assert_valid_a_active(extensions, manager)
                recovered_request_count = _ProviderHandler.requests
                self.assertEqual({item["id"] for item in manager.all_status()}, {"visible", "hidden-b"})
                repeated = extensions.snapshot(manager.discover())
                self.assertTrue(repeated["enabled"])
                self.assertEqual(repeated["error"], "")
                self.assertEqual(_ProviderHandler.requests, recovered_request_count)

    def test_same_metadata_different_valid_content_replaces_cached_configuration(self) -> None:
        metadata = self._replace_preserving_signature(self._json_bytes(self.valid_a))
        extensions, manager = self._new_production_manager()
        self._assert_valid_a_active(extensions, manager)
        first_request_count = _ProviderHandler.requests

        self.assertEqual(self._replace_preserving_signature(self._json_bytes(self.valid_b)), metadata)
        self.assertEqual({item["id"] for item in manager.all_status()}, {"visible", "hidden-a"})
        self.assertEqual(extensions._provider_cache, {})
        snapshot = extensions.snapshot(manager.discover())
        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["error"], "")
        self.assertEqual([item["id"] for item in snapshot["widgets"]], ["status-b"])
        self.assertEqual(snapshot["widgets"][0]["drill_down_url"], "https://example.invalid/bravo")
        self.assertEqual(_ProviderHandler.requests, first_request_count + 1)

        repeated_request_count = _ProviderHandler.requests
        self.assertEqual({item["id"] for item in manager.all_status()}, {"visible", "hidden-a"})
        self.assertEqual(extensions.snapshot(manager.discover()), snapshot)
        self.assertEqual(_ProviderHandler.requests, repeated_request_count)


if __name__ == "__main__":
    unittest.main()
