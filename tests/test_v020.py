from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import appdock
from appdock import (
    CURRENT_VERSION,
    HTML,
    Handler,
    LMStudioAdapter,
    ReleaseChecker,
    ThreadingHTTPServer,
    build_lm_load_args,
    build_lm_unload_args,
    discover_lms,
)


class V020Tests(unittest.TestCase):
    def test_update_check_reports_unavailable_current_and_newer(self) -> None:
        class Response:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = json.dumps(payload).encode()
            def read(self) -> bytes:
                return self.payload
            def __enter__(self) -> "Response":
                return self
            def __exit__(self, *_args: object) -> None:
                return None

        def check(tag: str) -> dict[str, object]:
            return ReleaseChecker(
                "owner/repo",
                opener=lambda *_args, **_kwargs: Response({"tag_name": tag, "assets": []}),
                current=CURRENT_VERSION,
            ).check()

        self.assertFalse(check("v0.2.0")["update_available"])
        self.assertTrue(check("v0.3.0")["update_available"])
        with self.assertRaises(appdock.AppDockError):
            ReleaseChecker("owner/repo", opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")), current=CURRENT_VERSION).check()

    def test_version_consistency(self) -> None:
        self.assertEqual(CURRENT_VERSION, "0.2.1")
        pyproject = Path(__file__).parents[1] / "pyproject.toml"
        self.assertIn('version = "0.2.1"', pyproject.read_text(encoding="utf-8"))

    def test_lms_discovery_honors_override_then_user_bin_then_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            override = root / "custom" / "lms.exe"
            override.parent.mkdir()
            override.write_text("fake", encoding="utf-8")
            profile_bin = root / "profile" / ".lmstudio" / "bin"
            profile_bin.mkdir(parents=True)
            (profile_bin / "lms.cmd").write_text("fake", encoding="utf-8")
            path_bin = root / "path"
            path_bin.mkdir()
            (path_bin / "lms").write_text("fake", encoding="utf-8")
            environment = {
                "APPDOCK_LMS_PATH": str(override),
                "USERPROFILE": str(root / "profile"),
                "PATH": str(path_bin),
            }
            discovered = discover_lms(environment)
            self.assertTrue(os.path.samefile(discovered, override))
            environment.pop("APPDOCK_LMS_PATH")
            discovered = discover_lms(environment)
            self.assertTrue(os.path.samefile(discovered, profile_bin / "lms.cmd"))
            (profile_bin / "lms.cmd").unlink()
            discovered = discover_lms(environment)
            self.assertTrue(os.path.samefile(discovered, path_bin / "lms"))

    def test_lms_absent_state_is_useful_and_never_exposes_executable(self) -> None:
        with patch("appdock.discover_lms", return_value=None):
            payload = LMStudioAdapter().snapshot()
        self.assertEqual(payload["status"], "absent")
        self.assertFalse(payload["available"])
        self.assertIn("optional", payload["error"].lower())
        self.assertNotIn("executable", payload)
        self.assertNotIn("path", json.dumps(payload).lower())

    def test_lms_normalizes_running_partial_malformed_and_timeout_states(self) -> None:
        installed = [{
            "modelKey": "org-a/shared",
            "displayName": "Shared <model>",
            "quantization": {"name": "Q4"},
            "variants": ["org-a/shared@q4"],
        }, {"modelKey": "org-b/shared"}, {"unexpected": True}, "bad"]
        loaded = [{"modelKey": "org-a/shared@q4", "identifier": "worker-1", "contextLength": 4096}]

        def fake_running(args: list[str], **_: object) -> object:
            value = installed if args[1] == "ls" else loaded
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        with patch("appdock.subprocess.run", side_effect=fake_running):
            running = LMStudioAdapter(executable="C:\\internal\\lms.exe").snapshot()
        self.assertEqual(running["status"], "running")
        self.assertTrue(running["running"])
        self.assertEqual([item["key"] for item in running["installed_models"]], ["org-a/shared", "org-b/shared"])
        self.assertTrue(running["installed_models"][0]["loaded"])
        self.assertFalse(running["installed_models"][1]["loaded"])

        def fake_partial(args: list[str], **_: object) -> object:
            if args[1] == "ls":
                return SimpleNamespace(returncode=0, stdout=json.dumps(installed), stderr="")
            return SimpleNamespace(returncode=0, stdout="not json", stderr="internal path")

        with patch("appdock.subprocess.run", side_effect=fake_partial):
            partial = LMStudioAdapter(executable="C:\\internal\\lms.exe").snapshot()
        self.assertEqual(partial["status"], "partial")
        self.assertIn("malformed", partial["warning"].lower())
        self.assertNotIn("internal path", json.dumps(partial))

        with patch("appdock.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="lms", timeout=1)):
            timed_out = LMStudioAdapter(executable="C:\\internal\\lms.exe").snapshot()
        self.assertEqual(timed_out["status"], "timeout")
        self.assertNotIn("internal", json.dumps(timed_out).lower())

    def test_lms_cli_calls_are_fixed_arrays_and_shell_false(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(args: list[str], **kwargs: object) -> object:
            calls.append((args, kwargs))
            payload = [] if args[1] in {"ls", "ps"} else None
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with patch("appdock.subprocess.run", side_effect=fake_run):
            adapter = LMStudioAdapter(executable="lms")
            adapter.snapshot()
            adapter.execute(["unload", "worker-1"])
        self.assertTrue(calls)
        self.assertTrue(all(kwargs.get("shell") is False for _args, kwargs in calls))
        self.assertEqual(calls[-1][0], ["lms", "unload", "worker-1"])

    def test_lms_load_args_are_strict_and_use_fresh_canonical_model_key(self) -> None:
        installed = [{"key": "org-a/shared"}, {"key": "org-b/shared"}]
        request = {
            "model": "org-a/shared",
            "gpu": 0.5,
            "context_length": 8192,
            "parallel": 2,
            "ttl": 3600,
            "identifier": "worker-1",
            "speculative_draft_mtp": "disable",
            "speculative_draft_simple": "enable",
            "speculative_draft_model": "org-a/draft",
            "speculative_draft_max_tokens": 8,
            "speculative_draft_min_tokens": 2,
            "speculative_draft_min_continue_probability": 0.7,
        }
        self.assertEqual(build_lm_load_args(request, installed), [
            "load", "--gpu", "0.5", "--context-length", "8192", "--parallel", "2", "--ttl", "3600",
            "--identifier", "worker-1", "--no-speculative-draft-mtp", "--speculative-draft-simple",
            "--speculative-draft-model", "org-a/draft", "--speculative-draft-max-tokens", "8",
            "--speculative-draft-min-tokens", "2", "--speculative-draft-min-continue-probability", "0.7",
            "-y", "org-a/shared",
        ])
        self.assertEqual(build_lm_load_args({"model": "org-a/shared", "gpu": "auto"}, installed), ["load", "-y", "org-a/shared"])
        for invalid in (
            {"model": "org-a/shared", "unknown": 1},
            {"model": "org-a/shared", "parallel": True},
            {"model": "org-a/shared", "gpu": 1.1},
            {"model": "org-a/shared", "identifier": "bad\nname"},
            {"model": "shared"},
            {"model": "org-a/shared", "speculative_draft_max_tokens": 0},
        ):
            with self.assertRaises(ValueError):
                build_lm_load_args(invalid, installed)

    def test_lms_rejects_option_shaped_operands_before_building_argv(self) -> None:
        for request in (
            {"model": "--all"},
            {"model": "--help"},
            {"model_key": "-y"},
            {"model": "org-a/shared", "identifier": "--all"},
            {"model": "org-a/shared", "speculative_draft_simple": "enable", "speculative_draft_model": "--all"},
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                build_lm_load_args(request, [{"key": "org-a/shared"}, {"key": "--all"}])
        with self.assertRaises(ValueError):
            build_lm_load_args({"model": "org-a/shared"}, [{"key": "--all"}])

    def test_lms_unload_requires_exact_fresh_identifier_and_never_unloads_all(self) -> None:
        loaded = [{"identifier": "worker-1", "key": "org-a/shared"}]
        self.assertEqual(build_lm_unload_args({"identifier": "worker-1"}, loaded), ["unload", "worker-1"])
        for invalid in ({"identifier": "worker"}, {"identifier": "worker-1", "all": True}, {"identifier": "worker-1\n"}):
            with self.assertRaises(ValueError):
                build_lm_unload_args(invalid, loaded)
        for invalid in ("--all", "--help", "-y"):
            with self.subTest(identifier=invalid), self.assertRaises(ValueError):
                build_lm_unload_args({"identifier": invalid}, [{"identifier": invalid}])

    def test_lms_mutations_are_fake_only_locked_and_same_origin_protected(self) -> None:
        class FakeAdapter:
            def __init__(self) -> None:
                self.commands: list[list[str]] = []
                self.started = threading.Event()
                self.release = threading.Event()

            def snapshot(self) -> dict[str, object]:
                return {
                    "available": True, "reachable": True, "running": True, "status": "running",
                    "installed_models": [{"key": "org-a/shared"}],
                    "loaded_instances": [{"identifier": "worker-1", "key": "org-a/shared"}],
                    "error": None, "warning": None,
                }

            def execute(self, args: list[str]) -> tuple[bool, bool, str]:
                self.commands.append(args)
                self.started.set()
                self.release.wait(2)
                return True, False, ""

        class TestHandler(Handler):
            pass

        fake = FakeAdapter()
        TestHandler.lm_adapter = fake
        TestHandler.lm_mutation_lock = threading.Lock()
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path: str, body: dict[str, object], origin: str | None = None) -> object:
            request_obj = urllib.request.Request(
                base + path, data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json"},
            )
            if origin:
                request_obj.add_header("Origin", origin)
            return urllib.request.urlopen(request_obj, timeout=3)

        result: list[object] = []
        def first_request() -> None:
            result.append(request("/api/lm-studio/load", {"model": "org-a/shared"}))

        worker = threading.Thread(target=first_request)
        worker.start()
        self.assertTrue(fake.started.wait(1))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            request("/api/lm-studio/unload", {"identifier": "worker-1"})
        self.assertEqual(raised.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            request("/api/lm-studio/unload", {"identifier": "worker-1"}, "https://evil.example")
        self.assertEqual(raised.exception.code, 403)
        fake.release.set()
        worker.join(3)
        self.assertEqual(len(result), 1)
        self.assertEqual(fake.commands, [["load", "-y", "org-a/shared"]])
        server.shutdown()
        server.server_close()
        thread.join(3)

    def test_public_ui_has_drawer_updates_notification_lm_and_no_inline_handlers(self) -> None:
        for marker in (
            "menuButton", "drawer", "dashboardLink", "lmStudioLink", "updatesLink", "updatesBadge",
            "updateBanner", "lmStudioView", "/api/lm-studio", "Check for updates", "Update now",
            "waitForHealthyVersion", "AUTO_UPDATE_CHECK_INTERVAL_MS",
        ):
            self.assertIn(marker, HTML + (Path(__file__).parents[1] / "static/app.js").read_text(encoding="utf-8"))
        self.assertNotRegex(HTML, r"\bon(?:click|change|submit|input)=")
        script = (Path(__file__).parents[1] / "static/app.js").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"\bon(?:click|change|submit|input)\s*=")
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)
        css = (Path(__file__).parents[1] / "static/app.css").read_text(encoding="utf-8")
        self.assertIn(".update-banner[hidden]", css)

    def test_public_docs_describe_optional_lm_updates_and_separate_data(self) -> None:
        root = Path(__file__).parents[1]
        text = "\n".join((root / name).read_text(encoding="utf-8") for name in (
            "README.md", "docs/USAGE.md", "docs/UPDATES.md", "docs/ARCHITECTURE.md",
            "docs/PRIVACY.md", "docs/TROUBLESHOOTING.md",
        ))
        for phrase in ("LM Studio is optional", "GitHub Releases", "connection metadata", "development clone", "mutable user data"):
            self.assertIn(phrase.lower(), text.lower())


if __name__ == "__main__":
    unittest.main()
