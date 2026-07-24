from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

MANIFEST_NAME = "appdock.json"
CURRENT_VERSION = "0.1.1"
DEFAULT_UPDATE_REPOSITORY = "eWOOD29/appdock"
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
MAX_JSON_BYTES = 128 * 1024
MAX_UPDATE_ASSET_BYTES = 100 * 1024 * 1024
MAX_UPDATE_FILE_COUNT = 4096
MAX_UPDATE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_GITHUB_STAGE_BYTES = 250 * 1024 * 1024
MAX_GITHUB_STAGE_FILES = 20_000
MAX_GITHUB_STAGING_TOTAL_BYTES = 500 * 1024 * 1024
MAX_GITHUB_STAGING_TOTAL_FILES = 40_000
GITHUB_STAGE_TTL_SECONDS = 24 * 60 * 60
RELEASE_MANIFEST_NAME = "RELEASE-MANIFEST.json"
REQUIRED_RELEASE_FILES = {
    "appdock.py",
    "static/app.js",
    "static/app.css",
    "scripts/update_helper.py",
    "scripts/path_safety.ps1",
    "scripts/install.ps1",
    "scripts/uninstall.ps1",
}


class AppDockError(Exception):
    """Expected, user-facing AppDock error."""


class ManifestError(AppDockError, ValueError):
    pass


class PreviewError(AppDockError, ValueError):
    pass


@dataclass(frozen=True)
class AppDockConfig:
    data_root: Path
    registry_root: Path
    install_root: Path
    order_path: Path
    runtime_root: Path
    logs_root: Path
    staging_root: Path
    updates_root: Path
    update_repository: str = DEFAULT_UPDATE_REPOSITORY

    @classmethod
    def from_environment(
        cls,
        *,
        repo_root: Path | None = None,
        data_dir: str | Path | None = None,
        platform: str | None = None,
        local_app_data: str | Path | None = None,
    ) -> "AppDockConfig":
        platform = platform or os.sys.platform
        override = data_dir or os.environ.get("APPDOCK_DATA_DIR")
        if override:
            root = Path(override).expanduser()
        elif platform == "win32":
            root = Path(local_app_data or os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "AppDock"
        else:
            root = Path.home() / ".local" / "share" / "appdock"
        root = root.expanduser().absolute()
        return cls(
            data_root=root,
            registry_root=root / "registry",
            install_root=root / "apps",
            order_path=root / "app-order.json",
            runtime_root=root / "runtime",
            logs_root=root / "runtime" / "logs",
            staging_root=root / "staging",
            updates_root=root / "updates",
            update_repository=os.environ.get("APPDOCK_UPDATE_REPOSITORY", DEFAULT_UPDATE_REPOSITORY),
        )

    def ensure(self) -> None:
        _assert_no_link_or_reparse_ancestor(self.data_root)
        for path in (self.registry_root, self.install_root, self.runtime_root, self.logs_root, self.staging_root, self.updates_root):
            _assert_no_link_or_reparse_ancestor(path)
            path.mkdir(parents=True, exist_ok=True)


@dataclass
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


@dataclass
class AppRuntime:
    process: subprocess.Popen[str] | None = None
    started_at: float | None = None
    last_exit_code: int | None = None
    last_error: str = ""
    intentional_stop: bool = False


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    if _is_link_or_reparse(path):
        try:
            if path.is_dir() and not path.is_symlink():
                os.rmdir(path)
            else:
                path.unlink()
        except OSError:
            if not ignore_errors:
                raise
        return

    def make_writable_and_retry(function: Callable[..., Any], raw_path: str, _exc_info: Any) -> None:
        os.chmod(raw_path, stat.S_IWRITE)
        function(raw_path)

    try:
        shutil.rmtree(path, onerror=make_writable_and_retry)
    except FileNotFoundError:
        return
    except OSError:
        if not ignore_errors:
            raise


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _assert_no_link_or_reparse_ancestor(path: Path) -> None:
    current = path.expanduser().absolute()
    while True:
        if _is_link_or_reparse(current):
            raise AppDockError("AppDock data root is beneath a symlink or reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _safe_child(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name or not APP_ID_RE.fullmatch(name):
        raise ManifestError("unsafe app id")
    root = root.expanduser().absolute()
    _assert_no_link_or_reparse_ancestor(root)
    child = root / name
    if _is_link_or_reparse(child):
        raise ManifestError("AppDock data path is a symlink or reparse point")
    if not _inside(child.resolve(), root):
        raise ManifestError("path escapes AppDock data directory")
    return child


def _safe_version_child(root: Path, version: str) -> Path:
    if not SEMVER_RE.fullmatch(version):
        raise AppDockError("invalid update version")
    root = root.expanduser().absolute()
    _assert_no_link_or_reparse_ancestor(root)
    child = root / version
    if _is_link_or_reparse(child):
        raise AppDockError("update path is a symlink or reparse point")
    if not _inside(child.resolve(), root):
        raise AppDockError("update path escapes data directory")
    return child


def _assert_tree_safe(root: Path, containment_root: Path) -> None:
    _assert_no_link_or_reparse_ancestor(root)
    root = root.resolve()
    if not _inside(root, containment_root):
        raise AppDockError("staging path escapes data directory")
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current).resolve()
        if not _inside(current_path, root):
            raise AppDockError("staging path escapes its root")
        for name in [*dirs, *files]:
            path = Path(current) / name
            if path.is_symlink() or not _inside(path.resolve(), root):
                raise AppDockError("staging contains an unsafe symlink or path")


def _validate_url(value: Any, field_name: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 2048:
        raise ManifestError(f"{field_name} must be a URL")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.netloc:
        raise ManifestError(f"{field_name} must be an http(s) URL without credentials")
    return value


def _validate_health_url(value: Any) -> str:
    value = _validate_url(value, "health_url")
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ManifestError("health_url must use a literal loopback host")
    return value


def validate_bind_host(host: str) -> str:
    if not isinstance(host, str) or host not in {"127.0.0.1", "localhost", "::1"}:
        raise AppDockError("AppDock may bind only to a loopback host")
    return host


def _process_group_options(platform: str | None = None) -> dict[str, Any]:
    if (platform or os.name) == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _relative_path(raw: Any, base: Path, *, field_name: str) -> Path:
    if raw in (None, ""):
        return base.resolve()
    if not isinstance(raw, str) or "\x00" in raw:
        raise ManifestError(f"{field_name} must be a safe path")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ManifestError(f"{field_name} must be relative")
    result = (base / candidate).resolve()
    if not _inside(result, base):
        raise ManifestError(f"{field_name} escapes the app directory")
    return result


def normalize_manifest(raw: dict[str, Any], *, manifest_dir: Path, directory: Path | None = None, external: bool = False, allow_outside: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")
    app_id = raw.get("id")
    if not isinstance(app_id, str) or not APP_ID_RE.fullmatch(app_id):
        raise ManifestError("id must match [a-z0-9][a-z0-9_-]{0,63}")
    command = raw.get("command")
    if not isinstance(command, list) or not command or len(command) > 128 or not all(isinstance(item, str) and item and "\x00" not in item for item in command):
        raise ManifestError("command must be a non-empty list of strings")
    target = directory or manifest_dir
    target = target.expanduser().resolve()
    is_external = bool(raw.get("external", external))
    if not is_external and not allow_outside and not _inside(target, manifest_dir):
        raise ManifestError("directory escapes the manifest directory")
    cwd = _relative_path(raw.get("cwd", "."), target, field_name="cwd")
    if not cwd.is_dir() and not cwd.exists():
        # A manifest may be registered before a generated working directory exists.
        pass
    try:
        port = int(raw["port"]) if raw.get("port") is not None else None
    except (TypeError, ValueError):
        raise ManifestError("port must be an integer") from None
    if port is not None and not 1 <= port <= 65535:
        raise ManifestError("port must be between 1 and 65535")
    env = raw.get("env", {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or (v is not None and not isinstance(v, (str, int, float, bool))) for k, v in env.items()):
        raise ManifestError("env must be a simple object")
    private_url = raw.get("private_url")
    if private_url in (None, ""):
        private_url = raw.get("tailscale_url")
    normalized: dict[str, Any] = {
        "id": app_id,
        "name": str(raw.get("name") or app_id)[:200],
        "description": str(raw.get("description") or "")[:2000],
        "external": is_external,
        "directory": str(target),
        "command": command,
        "cwd": str(cwd.relative_to(target)) if _inside(cwd, target) else str(cwd),
        "port": port,
        "health_url": _validate_health_url(raw.get("health_url")),
        "local_url": _validate_url(raw.get("local_url"), "local_url"),
        "private_url": _validate_url(private_url, "private_url"),
        "env": {str(k): (None if v is None else str(v)) for k, v in env.items()},
        "process_name": str(raw.get("process_name") or "")[:200],
        "stop_timeout": max(0.1, min(float(raw.get("stop_timeout", 3.0)), 60.0)),
    }
    return normalized


def validate_manifest(raw: dict[str, Any], manifest_dir: Path, *, directory: Path | None = None) -> dict[str, Any]:
    return normalize_manifest(raw, manifest_dir=manifest_dir, directory=directory)


class AppManager:
    def __init__(self, apps_root: Path | None = None, config: AppDockConfig | None = None):
        self.config = config or AppDockConfig.from_environment()
        self.legacy_direct_root = apps_root is not None
        self.apps_root = Path(apps_root).expanduser().resolve() if apps_root is not None else self.config.registry_root
        self.order_path = (self.apps_root / ".appdock-order.json") if self.legacy_direct_root else self.config.order_path
        self._runtimes: dict[str, AppRuntime] = {}
        self._lock = threading.RLock()

    def discover(self) -> dict[str, AppSpec]:
        specs: dict[str, AppSpec] = {}
        if not self.apps_root.is_dir():
            return specs
        try:
            entries = sorted(self.apps_root.iterdir(), key=lambda p: p.name)
        except OSError:
            return specs
        for manifest_dir in entries:
            if not manifest_dir.is_dir() or manifest_dir.is_symlink():
                continue
            manifest_path = manifest_dir / MANIFEST_NAME
            if not manifest_path.is_file() or manifest_path.is_symlink():
                continue
            try:
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
        return specs

    def _runtime(self, app_id: str) -> AppRuntime:
        return self._runtimes.setdefault(app_id, AppRuntime())

    def _read_order(self) -> list[str]:
        try:
            raw = json.loads(self.order_path.read_text(encoding="utf-8"))
            return [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []

    def _write_order(self, app_ids: list[str]) -> None:
        self.order_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.order_path.with_name(self.order_path.name + ".tmp")
        temp.write_text(json.dumps(app_ids, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.order_path)

    def _ordered_specs(self, specs: dict[str, AppSpec]) -> list[AppSpec]:
        saved = self._read_order()
        ordered = [item for item in saved if item in specs]
        ordered.extend(item for item in sorted(specs) if item not in ordered)
        return [specs[item] for item in ordered]

    def _refresh_process(self, app_id: str) -> AppRuntime:
        runtime = self._runtime(app_id)
        if runtime.process is not None and runtime.process.poll() is not None:
            runtime.last_exit_code = runtime.process.returncode
            runtime.process = None
        return runtime

    def _listening_pids(self, port: int | None) -> list[int]:
        if not port or os.name != "nt":
            return []
        try:
            result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=1.5, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        pids: list[int] = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
                continue
            address = fields[1]
            if not address.endswith(f":{port}") or address.rsplit(":", 1)[0].strip("[]") not in {"0.0.0.0", "127.0.0.1", "::"}:
                continue
            try:
                pid = int(fields[4])
            except ValueError:
                continue
            if pid > 4 and pid not in pids:
                pids.append(pid)
        return pids

    def _process_name_pids(self, process_name: str) -> list[int]:
        if not process_name or os.name != "nt":
            return []
        try:
            result = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=1.5, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        pids: list[int] = []
        for line in result.stdout.splitlines():
            fields = line.replace('"', "").split(",")
            if len(fields) < 2 or fields[0].strip().lower() != process_name.lower():
                continue
            try:
                pid = int(fields[1].strip())
            except ValueError:
                continue
            if pid > 4 and pid not in pids:
                pids.append(pid)
        return pids

    def _external_pids(self, spec: AppSpec) -> list[int]:
        pids = self._listening_pids(spec.port)
        for pid in self._process_name_pids(spec.process_name):
            if pid not in pids:
                pids.append(pid)
        return pids

    def _health(self, spec: AppSpec) -> tuple[bool | None, str]:
        if not spec.health_url:
            return None, "not configured"
        try:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(spec.health_url, timeout=0.8) as response:
                return 200 <= response.status < 400, f"HTTP {response.status}"
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return False, str(exc.reason if isinstance(exc, urllib.error.URLError) and exc.reason else exc)

    def status(self, spec: AppSpec) -> dict[str, Any]:
        with self._lock:
            runtime = self._refresh_process(spec.app_id)
            process = runtime.process
            managed = process is not None and process.poll() is None
            external_pids = [] if managed else self._external_pids(spec)
            running = managed or bool(external_pids)
            health, detail = self._health(spec) if running else (None, "not running")
            state = "healthy" if running and health is True else "unhealthy" if running and health is False else "running" if running else "crashed" if runtime.last_exit_code not in (None, 0) and not runtime.intentional_stop else "stopped"
            return {"id": spec.app_id, "name": spec.name, "description": spec.description, "state": state, "pid": process.pid if managed else (external_pids[0] if external_pids else None), "managed": managed, "started_at": runtime.started_at, "last_exit_code": runtime.last_exit_code, "health": health, "health_detail": detail, "port": spec.port, "local_url": spec.local_url, "private_url": spec.private_url}

    def all_status(self) -> list[dict[str, Any]]:
        return [self.status(spec) for spec in self._ordered_specs(self.discover())]

    def move(self, app_id: str, direction: str) -> dict[str, Any]:
        specs = self.discover()
        if app_id not in specs:
            raise KeyError(app_id)
        ids = [spec.app_id for spec in self._ordered_specs(specs)]
        index = ids.index(app_id)
        target = index - 1 if direction == "up" else index + 1 if direction == "down" else index
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            self._write_order(ids)
        return self.status(specs[app_id])

    def log_path(self, spec: AppSpec) -> Path:
        root = self.config.logs_root if not self.legacy_direct_root else spec.manifest_dir / "runtime" / "logs"
        root.mkdir(parents=True, exist_ok=True)
        path = (root / f"{spec.app_id}.log").resolve()
        if not _inside(path, root):
            raise AppDockError("unsafe log path")
        return path

    def logs(self, spec: AppSpec, lines: int = 100) -> list[str]:
        try:
            return self.log_path(spec).read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 200)) :]
        except OSError:
            return []

    def start(self, app_id: str) -> dict[str, Any]:
        spec = self.discover().get(app_id)
        if spec is None:
            raise KeyError(app_id)
        with self._lock:
            runtime = self._refresh_process(app_id)
            if runtime.process is None and self._external_pids(spec):
                return self.status(spec)
            if runtime.process is None:
                log = self.log_path(spec).open("a", encoding="utf-8", buffering=1)
                log.write(f"\n--- AppDock start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                try:
                    runtime.process = subprocess.Popen(
                        spec.command,
                        cwd=spec.cwd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        shell=False,
                        env=self._environment(spec),
                        **_process_group_options(),
                    )
                    runtime.started_at = time.time()
                    runtime.last_exit_code = None
                    runtime.intentional_stop = False
                except OSError as exc:
                    runtime.last_error = str(exc)
                    log.write(f"AppDock launch error: {exc}\n")
                    raise AppDockError("could not start app") from exc
                finally:
                    log.close()
            return self.status(spec)

    def _environment(self, spec: AppSpec) -> dict[str, str]:
        environment = os.environ.copy()
        for key, value in spec.env.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return environment

    def stop(self, app_id: str) -> dict[str, Any]:
        spec = self.discover().get(app_id)
        if spec is None:
            raise KeyError(app_id)
        with self._lock:
            runtime = self._refresh_process(app_id)
            process = runtime.process
            if process is not None and process.poll() is None:
                runtime.intentional_stop = True
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True, check=False)
                else:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=spec.stop_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                runtime.last_exit_code = process.returncode
                runtime.process = None
            return self.status(spec)

    def restart(self, app_id: str) -> dict[str, Any]:
        self.stop(app_id)
        return self.start(app_id)


class LocalFolderOnboarding:
    def __init__(self, config: AppDockConfig):
        self.config = config

    def preview(self, folder: str | Path) -> dict[str, Any]:
        source = Path(folder).expanduser().resolve()
        manifest_path = source / MANIFEST_NAME
        if not source.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
            raise PreviewError("folder must contain a root appdock.json")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            normalized = normalize_manifest(raw, manifest_dir=source, directory=source, external=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, ManifestError) as exc:
            raise PreviewError(str(exc)) from exc
        public = {k: v for k, v in normalized.items() if k not in {"env"}}
        result = {"kind": "local", "app": public, "source_name": source.name, "digest": _digest({"source": str(source), "manifest": normalized}), "confirmation_required": True}
        return result

    def register(self, folder: str | Path, confirmation: str, preview: dict[str, Any] | None = None) -> dict[str, Any]:
        source = Path(folder).expanduser().resolve()
        current = self.preview(source)
        expected = current["digest"]
        supplied = preview.get("digest") if isinstance(preview, dict) else confirmation
        if supplied != expected or confirmation != expected:
            raise PreviewError("preview confirmation is stale or invalid")
        app_id = current["app"]["id"]
        self.config.ensure()
        registry_dir = _safe_child(self.config.registry_root, app_id)
        registry_manifest = registry_dir / MANIFEST_NAME
        if registry_manifest.exists() or registry_dir.exists():
            raise PreviewError("an app with this id is already registered")
        registry_dir.mkdir(parents=True)
        manifest_path = source / MANIFEST_NAME
        try:
            source_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            normalized = normalize_manifest(source_raw, manifest_dir=source, directory=source, external=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, ManifestError) as exc:
            raise PreviewError(str(exc)) from exc
        normalized["external"] = True
        normalized["directory"] = str(source)
        if _digest({"source": str(source), "manifest": normalized}) != expected:
            raise PreviewError("source manifest changed during registration")
        try:
            _atomic_json(registry_manifest, normalized)
        except Exception:
            _remove_tree(registry_dir, ignore_errors=True)
            raise
        return {"registered": True, "id": app_id, "started": False}


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    temp.write_bytes(_canonical_json(payload) + b"\n")
    temp.replace(path)


def _tree_usage(root: Path) -> tuple[int, int]:
    files = 0
    total_bytes = 0
    if not root.exists():
        return files, total_bytes
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PreviewError("GitHub checkout contains a symlink")
        if not path.is_file():
            continue
        files += 1
        try:
            total_bytes += path.stat().st_size
        except OSError as exc:
            raise PreviewError("could not inspect GitHub checkout") from exc
    return files, total_bytes


def _assert_staging_quota(stage: Path, staging_root: Path) -> None:
    files, total_bytes = _tree_usage(stage)
    if files > MAX_GITHUB_STAGE_FILES or total_bytes > MAX_GITHUB_STAGE_BYTES:
        raise PreviewError("GitHub checkout exceeds the staging quota")
    all_files, all_bytes = _tree_usage(staging_root)
    if all_files > MAX_GITHUB_STAGING_TOTAL_FILES or all_bytes > MAX_GITHUB_STAGING_TOTAL_BYTES:
        raise PreviewError("GitHub staging area exceeds the total quota")


def _terminate_process_tree(process: Any) -> None:
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 4:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    try:
        process.terminate()
        process.wait(timeout=1)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=1)
        except Exception:
            pass


def _run_bounded_clone(
    command: list[str],
    stage: Path,
    *,
    staging_root: Path | None = None,
    popen: Callable[..., Any] | None = None,
    timeout: float = 120,
    poll_interval: float = 0.05,
) -> Any:
    runner = popen or subprocess.Popen
    environment = os.environ.copy()
    environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    process = runner(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        shell=False,
        env=environment,
        **_process_group_options(),
    )
    deadline = time.monotonic() + timeout
    root = staging_root or stage.parent
    while process.poll() is None:
        try:
            _assert_staging_quota(stage, root)
        except PreviewError:
            _terminate_process_tree(process)
            raise
        if time.monotonic() >= deadline:
            _terminate_process_tree(process)
            raise PreviewError("GitHub clone timed out")
        if poll_interval:
            time.sleep(poll_interval)
    stdout, stderr = process.communicate()
    if getattr(process, "returncode", 0) != 0:
        raise PreviewError("GitHub clone failed")
    _assert_staging_quota(stage, root)
    return subprocess.CompletedProcess(command, 0, stdout, stderr)


class GitHubOnboarding:
    _clone_lock = threading.Lock()

    def __init__(self, config: AppDockConfig, runner: Callable[..., Any] | None = None):
        self.config = config
        self.runner = runner

    @staticmethod
    def canonical_url(url: str) -> str:
        if not isinstance(url, str) or len(url) > 500:
            raise PreviewError("GitHub URL is invalid")
        try:
            parsed = urllib.parse.urlsplit(url)
            explicit_port = parsed.port
        except ValueError as exc:
            raise PreviewError("GitHub URL is invalid") from exc
        if parsed.scheme != "https" or parsed.hostname != "github.com" or explicit_port is not None or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PreviewError("only canonical public GitHub HTTPS URLs are accepted")
        path = parsed.path
        if not path.startswith("/") or path.endswith("/") or path.count("/") != 2:
            raise PreviewError("GitHub URL must be https://github.com/<owner>/<repo>")
        parts = path[1:].split("/")
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in parts):
            raise PreviewError("GitHub URL must be https://github.com/<owner>/<repo>")
        owner, repo = parts
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not repo or repo in {".", ".."}:
            raise PreviewError("GitHub repository is invalid")
        return f"https://github.com/{owner}/{repo}.git"

    def preview(self, url: str) -> dict[str, Any]:
        canonical = self.canonical_url(url)
        self.config.ensure()
        self.cleanup_stale()
        stage = Path(tempfile.mkdtemp(prefix="repo-", dir=self.config.staging_root)).resolve()
        command = ["git", "-c", "core.hooksPath=", "clone", "--depth", "1", "--single-branch", "--no-tags", canonical, str(stage)]
        try:
            with self._clone_lock:
                if self.runner is None:
                    result = _run_bounded_clone(command, stage, staging_root=self.config.staging_root)
                else:
                    try:
                        result = self.runner(command, capture_output=True, text=True, timeout=120, check=False, shell=False)
                    except TypeError:
                        result = self.runner(command)
            returncode = getattr(result, "returncode", 0)
            if returncode != 0:
                raise PreviewError("GitHub clone failed")
            manifest = stage / MANIFEST_NAME
            if not manifest.is_file() or manifest.is_symlink():
                raise PreviewError("repository root must contain appdock.json")
            _assert_tree_safe(stage, self.config.data_root)
            _assert_staging_quota(stage, self.config.staging_root)
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            normalized = normalize_manifest(raw, manifest_dir=stage, directory=stage)
            public = {k: v for k, v in normalized.items() if k not in {"env"}}
            digest = _digest({"canonical_url": canonical, "stage": stage.name, "manifest": normalized})
            return {"kind": "github", "url": canonical, "app": public, "staging_id": stage.name, "digest": digest, "confirmation_required": True}
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired, json.JSONDecodeError, ManifestError) as exc:
            _remove_tree(stage, ignore_errors=True)
            if isinstance(exc, PreviewError):
                raise
            raise PreviewError(str(exc)) from exc

    def register(self, preview: dict[str, Any], confirmation: str) -> dict[str, Any]:
        if not isinstance(preview, dict) or confirmation != preview.get("digest"):
            raise PreviewError("preview confirmation is stale or invalid")
        app = preview.get("app")
        stage_name = preview.get("staging_id")
        if not isinstance(app, dict) or not isinstance(stage_name, str):
            raise PreviewError("invalid GitHub preview")
        app_id = app.get("id")
        stage = _safe_child(self.config.staging_root, stage_name)
        if not stage.is_dir() or not (stage / MANIFEST_NAME).is_file():
            raise PreviewError("staging area no longer exists")
        _assert_tree_safe(stage, self.config.data_root)
        normalized = normalize_manifest(json.loads((stage / MANIFEST_NAME).read_text(encoding="utf-8")), manifest_dir=stage, directory=stage)
        if normalized["id"] != app_id:
            raise PreviewError("preview changed")
        current_digest = _digest({"canonical_url": preview.get("url"), "stage": stage.name, "manifest": normalized})
        if current_digest != preview.get("digest"):
            raise PreviewError("preview is stale or tampered")
        self.config.ensure()
        destination = _safe_child(self.config.install_root, app_id)
        registry = _safe_child(self.config.registry_root, app_id)
        if destination.exists() or registry.exists():
            raise PreviewError("an app with this id is already registered")
        try:
            stage.replace(destination)
            normalized["external"] = False
            normalized["directory"] = str(destination)
            _atomic_json(registry / MANIFEST_NAME, normalized)
        except Exception:
            if destination.exists() and not stage.exists():
                destination.replace(stage)
            _remove_tree(registry, ignore_errors=True)
            raise PreviewError("could not register repository")
        return {"registered": True, "id": app_id, "started": False}

    def cleanup(self, staging_id: str) -> bool:
        stage = _safe_child(self.config.staging_root, staging_id)
        if stage.exists():
            _remove_tree(stage)
            return True
        return False

    def cleanup_stale(self, *, now: float | None = None, ttl_seconds: float = GITHUB_STAGE_TTL_SECONDS) -> int:
        self.config.ensure()
        cutoff = (time.time() if now is None else now) - ttl_seconds
        removed = 0
        for stage in self.config.staging_root.iterdir():
            if not stage.name.startswith("repo-") or not stage.is_dir() or stage.is_symlink():
                continue
            try:
                modified = stage.stat().st_mtime
            except OSError:
                continue
            if modified < cutoff:
                _remove_tree(stage, ignore_errors=True)
                removed += 1
        return removed


def _staging_cleanup_loop(
    onboarding: GitHubOnboarding,
    stop_event: threading.Event,
    *,
    interval_seconds: float = 300,
    ttl_seconds: float = GITHUB_STAGE_TTL_SECONDS,
) -> None:
    while not stop_event.is_set():
        try:
            onboarding.cleanup_stale(ttl_seconds=ttl_seconds)
        except OSError:
            pass
        if stop_event.wait(interval_seconds):
            return


def canonical_github_url(url: str) -> str:
    return GitHubOnboarding.canonical_url(url)


def preview_local_folder(folder: str | Path, config: AppDockConfig) -> dict[str, Any]:
    return LocalFolderOnboarding(config).preview(folder)


def register_local_folder(folder: str | Path, confirmation: str, config: AppDockConfig, preview: dict[str, Any] | None = None) -> dict[str, Any]:
    return LocalFolderOnboarding(config).register(folder, confirmation, preview)


def compare_semver(left: str, right: str) -> int:
    def parse(value: str) -> tuple[int, int, int, tuple[str, ...]]:
        text = str(value)
        text = text[1:] if text.startswith("v") else text
        match = SEMVER_RE.fullmatch(text)
        if not match:
            raise ValueError("invalid semantic version")
        pre = tuple(match.group(4).split(".")) if match.group(4) else ()
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), pre
    a, b = parse(left), parse(right)
    if a[:3] != b[:3]:
        return (a[:3] > b[:3]) - (a[:3] < b[:3])
    if not a[3] and b[3]:
        return 1
    if a[3] and not b[3]:
        return -1
    for left_part, right_part in zip(a[3], b[3]):
        if left_part == right_part:
            continue
        left_number, right_number = left_part.isdigit(), right_part.isdigit()
        if left_number and right_number:
            return (int(left_part) > int(right_part)) - (int(left_part) < int(right_part))
        if left_number != right_number:
            return -1 if left_number else 1
        return (left_part > right_part) - (left_part < right_part)
    return (len(a[3]) > len(b[3])) - (len(a[3]) < len(b[3]))


def parse_release(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AppDockError("release response is invalid")
    if payload.get("draft") or payload.get("prerelease"):
        raise AppDockError("release is not a stable public release")
    tag = str(payload.get("tag_name") or "")
    tag = tag[1:] if tag.startswith("v") else tag
    compare_semver(tag, "0.0.0")
    assets = []
    for asset in payload.get("assets") or []:
        if isinstance(asset, dict) and isinstance(asset.get("name"), str) and isinstance(asset.get("browser_download_url"), str):
            assets.append({"name": asset["name"], "url": asset["browser_download_url"], "size": asset.get("size")})
    return {"version": tag, "latest": tag, "release_url": str(payload.get("html_url") or ""), "notes": str(payload.get("body") or ""), "assets": assets}


class ReleaseChecker:
    def __init__(self, repository: str = DEFAULT_UPDATE_REPOSITORY, opener: Callable[..., Any] | None = None, cache_ttl: float = 300.0, current: str = CURRENT_VERSION):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid update repository")
        self.repository, self.opener, self.cache_ttl, self.current = repository, opener or urllib.request.urlopen, cache_ttl, current
        self._cache: tuple[float, dict[str, Any]] | None = None

    def check(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self.cache_ttl:
            return self._cache[1]
        endpoint = f"https://api.github.com/repos/{self.repository}/releases/latest"
        request = urllib.request.Request(endpoint, headers={"Accept": "application/vnd.github+json", "User-Agent": "AppDock"})
        try:
            try:
                response = self.opener(request, timeout=5)
            except TypeError:
                response = self.opener(request)
            if hasattr(response, "__enter__"):
                with response as stream:
                    payload = json.loads(stream.read().decode("utf-8"))
            else:
                payload = json.loads(response.read().decode("utf-8"))
            release = parse_release(payload)
            release["current"] = self.current
            release["update_available"] = compare_semver(release["version"], self.current) > 0
            self._cache = (now, release)
            return release
        except (OSError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
            raise AppDockError("could not check GitHub releases") from exc


def select_trusted_assets(release: dict[str, Any], repository: str = DEFAULT_UPDATE_REPOSITORY) -> dict[str, dict[str, Any]]:
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        raise AppDockError("release assets are missing")
    found: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") not in {"appdock-windows.zip", "SHA256SUMS.txt"}:
            continue
        url = str(asset.get("url") or asset.get("browser_download_url") or "")
        parsed = urllib.parse.urlsplit(url)
        repository_parts = repository.strip("/").split("/")
        path_parts = parsed.path.strip("/").split("/")
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.query or parsed.fragment or parsed.username or parsed.password or len(repository_parts) != 2 or path_parts[:2] != repository_parts or len(path_parts) < 3:
            raise AppDockError("release asset is not trusted")
        if asset.get("size") is not None:
            try:
                if int(asset["size"]) > MAX_UPDATE_ASSET_BYTES:
                    raise AppDockError("release asset is too large")
            except (TypeError, ValueError):
                raise AppDockError("release asset size is invalid") from None
        if asset["name"] in found:
            raise AppDockError("duplicate release asset")
        found[asset["name"]] = {"name": asset["name"], "url": url, "size": asset.get("size")}
    if set(found) != {"appdock-windows.zip", "SHA256SUMS.txt"}:
        raise AppDockError("release must provide appdock-windows.zip and SHA256SUMS.txt")
    return found


def _read_asset(opener: Callable[..., Any], url: str, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AppDock"})
    try:
        response = opener(request, timeout=15)
    except TypeError:
        response = opener(request)
    def read_stream(stream: Any) -> bytes:
        final_url = stream.geturl() if callable(getattr(stream, "geturl", None)) else url
        parsed = urllib.parse.urlsplit(final_url)
        host = (parsed.hostname or "").lower()
        allowed_host = host == "github.com" or host == "objects.githubusercontent.com" or host.endswith(".githubusercontent.com")
        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise AppDockError("update asset redirected to an invalid URL") from exc
        if parsed.scheme != "https" or explicit_port is not None or not allowed_host or parsed.username or parsed.password:
            raise AppDockError("update asset redirected to an untrusted host")
        length = stream.headers.get("Content-Length") if getattr(stream, "headers", None) else None
        if length is not None and int(length) > max_bytes:
            raise AppDockError("update asset is too large")
        data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise AppDockError("update asset is too large")
        return data
    if hasattr(response, "__enter__"):
        with response as stream:
            return read_stream(stream)
    return read_stream(response)


def verify_sha256(data: bytes, sums_text: str, filename: str = "appdock-windows.zip") -> str:
    expected = None
    for line in sums_text.splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]{64})\s+[* ]?(.+?)\s*$", line)
        if match and Path(match.group(2)).name == filename:
            expected = match.group(1).lower()
            break
    if expected is None:
        raise AppDockError("checksum entry is missing")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise AppDockError("update checksum verification failed")
    return actual


def _assert_zip_member(name: str, info: zipfile.ZipInfo) -> None:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("//") or re.match(r"^[A-Za-z]:", normalized):
        raise AppDockError("update ZIP contains an absolute path")
    if ":" in normalized:
        raise AppDockError("update ZIP contains an alternate data stream")
    if normalized.endswith("/") and info.is_dir():
        normalized = normalized.rstrip("/")
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise AppDockError("update ZIP contains an unsafe path")
    for part in parts:
        if part.endswith((".", " ")):
            raise AppDockError("update ZIP contains a Windows-unsafe path")
        device = part.split(".", 1)[0].upper()
        if device in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}} or re.fullmatch(r"(?:COM|LPT)[¹²³]", device):
            raise AppDockError("update ZIP contains a Windows device name")
    reserved = {"data", "registry", "apps", "runtime", "staging", "updates", "user-data"}
    if any(part.lower() in reserved for part in parts):
        raise AppDockError("update ZIP contains a reserved or escaping path")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise AppDockError("update ZIP contains a symlink")


def _parse_release_manifest(data: bytes) -> dict[str, str]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppDockError("release inventory is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2} or not isinstance(payload.get("files"), list):
        raise AppDockError("release inventory is invalid")
    inventory: dict[str, str] = {}
    for item in payload["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise AppDockError("release inventory entry is invalid")
        path, digest = item["path"], item["sha256"]
        if not isinstance(path, str) or "\\" in path or path == RELEASE_MANIFEST_NAME:
            raise AppDockError("release inventory path is invalid")
        pure = PurePosixPath(path)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise AppDockError("release inventory path is invalid")
        _assert_zip_member(path, zipfile.ZipInfo(path))
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AppDockError("release inventory checksum is invalid")
        if path in inventory:
            raise AppDockError("release inventory contains duplicate paths")
        inventory[path] = digest
    if not REQUIRED_RELEASE_FILES.issubset(inventory):
        raise AppDockError("release inventory is missing required AppDock files")
    return inventory


def _release_path(root: Path, relative: str) -> Path:
    target = (root / Path(*PurePosixPath(relative).parts)).resolve()
    if not _inside(target, root):
        raise AppDockError("release inventory path escapes its root")
    return target


def _load_release_inventory(root: Path, *, complete: bool) -> dict[str, str]:
    manifest_path = root / RELEASE_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        if complete:
            raise AppDockError("release inventory is missing")
        return {}
    inventory = _parse_release_manifest(manifest_path.read_bytes())
    if complete:
        actual: set[str] = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise AppDockError("release tree contains a symlink")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        expected = {*inventory, RELEASE_MANIFEST_NAME}
        if actual != expected:
            raise AppDockError("release tree does not match its inventory")
        for relative, expected_digest in inventory.items():
            actual_digest = hashlib.sha256(_release_path(root, relative).read_bytes()).hexdigest()
            if actual_digest != expected_digest:
                raise AppDockError("release file checksum does not match its inventory")
    return inventory


def validate_zip(data: bytes) -> list[str]:
    names: list[str] = []
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_UPDATE_FILE_COUNT:
                raise AppDockError("update ZIP contains too many files")
            aggregate_size = 0
            for info in infos:
                _assert_zip_member(info.filename, info)
                if info.file_size < 0:
                    raise AppDockError("update ZIP contains an invalid file size")
                aggregate_size += info.file_size
                if aggregate_size > MAX_UPDATE_UNCOMPRESSED_BYTES:
                    raise AppDockError("update ZIP expands beyond the permitted size")
                names.append(info.filename)
            root_entries = [info for info in infos if info.filename == "appdock.py"]
            if len(root_entries) != 1 or root_entries[0].is_dir():
                raise AppDockError("update ZIP must contain root appdock.py")
            manifest_entries = [info for info in infos if info.filename == RELEASE_MANIFEST_NAME]
            if len(manifest_entries) != 1 or manifest_entries[0].is_dir():
                raise AppDockError("update ZIP must contain a release inventory")
            file_infos = [info for info in infos if not info.is_dir()]
            if len({info.filename for info in file_infos}) != len(file_infos):
                raise AppDockError("update ZIP contains duplicate paths")
            inventory = _parse_release_manifest(archive.read(RELEASE_MANIFEST_NAME))
            archive_files = {info.filename for info in file_infos}
            if archive_files != {*inventory, RELEASE_MANIFEST_NAME}:
                raise AppDockError("update ZIP does not match its release inventory")
            for relative, expected_digest in inventory.items():
                if hashlib.sha256(archive.read(relative)).hexdigest() != expected_digest:
                    raise AppDockError("update ZIP file checksum does not match its inventory")
    except (zipfile.BadZipFile, OSError, AppDockError) as exc:
        if isinstance(exc, AppDockError):
            raise
        raise AppDockError("update ZIP is invalid") from exc
    return names


def stage_update(release: dict[str, Any], config: AppDockConfig, *, opener: Callable[..., Any] | None = None, repository: str = DEFAULT_UPDATE_REPOSITORY) -> dict[str, Any]:
    version = str(release.get("version") or release.get("latest") or "")
    version = version[1:] if version.startswith("v") else version
    if compare_semver(version, CURRENT_VERSION) <= 0:
        raise AppDockError("release is not newer than the current version")
    assets = select_trusted_assets(release, repository)
    opener = opener or urllib.request.urlopen
    zip_bytes = _read_asset(opener, assets["appdock-windows.zip"]["url"], MAX_UPDATE_ASSET_BYTES)
    sums = _read_asset(opener, assets["SHA256SUMS.txt"]["url"], 1024 * 1024).decode("utf-8", "replace")
    verify_sha256(zip_bytes, sums)
    validate_zip(zip_bytes)
    config.ensure()
    destination = _safe_version_child(config.updates_root, version)
    if destination.exists():
        raise AppDockError("update version is already staged")
    temporary = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=config.updates_root))
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as archive:
            for info in archive.infolist():
                _assert_zip_member(info.filename, info)
                archive.extract(info, temporary)
        temporary.replace(destination)
    except Exception:
        _remove_tree(temporary, ignore_errors=True)
        raise
    return {"staged": True, "version": version, "path": str(destination), "digest": _digest({"version": version, "release_url": release.get("release_url"), "assets": assets})}


def apply_update(staged_dir: str | Path, install_dir: str | Path, data_dir: str | Path, *, restart: Callable[[], Any] | None = None) -> dict[str, Any]:
    staged_lexical = Path(staged_dir).expanduser().absolute()
    install, data = Path(install_dir).resolve(), Path(data_dir).resolve()
    if _is_link_or_reparse(staged_lexical):
        raise AppDockError("staged update root is a symlink or reparse point")
    staged = staged_lexical.resolve()
    if _inside(install, data) or _inside(data, install):
        raise AppDockError("installation and data roots must not overlap")
    if not staged.is_dir() or not _inside(staged, data / "updates"):
        raise AppDockError("staged update path is invalid")
    _assert_tree_safe(staged_lexical, data / "updates")
    target_inventory = _load_release_inventory(staged, complete=True)
    current_inventory = _load_release_inventory(install, complete=False) if install.exists() else {}
    target_files = {*target_inventory, RELEASE_MANIFEST_NAME}
    current_files = set(current_inventory)
    if current_inventory:
        current_files.add(RELEASE_MANIFEST_NAME)
    affected = sorted(target_files | current_files)
    backup = data / "updates" / "backups" / uuid.uuid4().hex
    backup.mkdir(parents=True, exist_ok=False)
    preexisting: list[str] = []
    result = {"applied": True, "backup": str(backup), "files": affected, "preexisting": preexisting}
    try:
        install.mkdir(parents=True, exist_ok=True)
        for relative in affected:
            old = _release_path(install, relative)
            if old.is_symlink() or (old.exists() and not old.is_file()):
                raise AppDockError("managed installation path is not a regular file")
            if old.is_file():
                backup_target = _release_path(backup, relative)
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, backup_target)
                preexisting.append(relative)
    except Exception as exc:
        _remove_tree(backup, ignore_errors=True)
        raise AppDockError("update backup failed; installation was not changed") from exc
    try:
        for relative in sorted(target_files):
            source = _release_path(staged, relative)
            destination = _release_path(install, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        for relative in sorted(current_files - target_files):
            _release_path(install, relative).unlink(missing_ok=True)
        if restart:
            restart()
    except Exception as exc:
        rollback_update(result, install, data)
        raise AppDockError("update failed and was rolled back") from exc
    return result


def rollback_update(applied: dict[str, Any], install_dir: str | Path, data_dir: str | Path) -> None:
    """Restore files recorded by ``apply_update`` after a restart launch failure."""
    install, data = Path(install_dir).resolve(), Path(data_dir).resolve()
    if _inside(install, data) or _inside(data, install):
        raise AppDockError("installation and data roots must not overlap")
    backup = Path(str(applied.get("backup") or "")).resolve()
    backup_root = (data / "updates" / "backups").resolve()
    if not backup.is_dir() or not _inside(backup, backup_root):
        raise AppDockError("update backup path is invalid")
    files = applied.get("files")
    preexisting = applied.get("preexisting")
    if not isinstance(files, list) or not isinstance(preexisting, list) or not all(isinstance(item, str) for item in preexisting):
        raise AppDockError("update rollback file list is invalid")
    preexisting_set = set(preexisting)
    for raw_relative in files:
        if not isinstance(raw_relative, str):
            raise AppDockError("update rollback path is invalid")
        relative = Path(raw_relative)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise AppDockError("update rollback path is invalid")
        target = (install / relative).resolve()
        saved = (backup / relative).resolve()
        if not _inside(target, install) or not _inside(saved, backup):
            raise AppDockError("update rollback path escapes its root")
        if raw_relative in preexisting_set:
            if not saved.is_file():
                raise AppDockError("update rollback backup is incomplete")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        else:
            target.unlink(missing_ok=True)


def _is_development_checkout(install_dir: Path) -> bool:
    root = install_dir.expanduser().resolve()
    return any((candidate / ".git").exists() for candidate in (root, *root.parents))


def launch_update_helper(
    staged_dir: str | Path,
    install_dir: str | Path,
    data_dir: str | Path,
    *,
    current_pid: int | None = None,
    restart_command: list[str] | None = None,
    helper_path: str | Path | None = None,
    popen: Callable[..., Any] | None = None,
    restart_args: Iterable[str] = (),
) -> Any:
    """Start the stdlib updater outside the AppDock process.

    ``restart_command`` and ``popen`` are injection points for tests and trusted
    callers only; the HTTP API never takes either value from a request body.
    """
    staged = Path(staged_dir).expanduser().absolute()
    install = Path(install_dir).expanduser().absolute()
    data = Path(data_dir).expanduser().absolute()
    for root in (staged, install, data):
        _assert_no_link_or_reparse_ancestor(root)
    if _is_development_checkout(install):
        raise AppDockError("one-click updates are disabled in a .git checkout; use git pull")
    helper = Path(helper_path).expanduser().resolve() if helper_path else staged / "scripts" / "update_helper.py"
    if not _inside(staged, data / "updates"):
        raise AppDockError("staged update path is invalid")
    if not helper.is_file() or helper.is_symlink() or not _inside(helper, staged):
        raise AppDockError("verified staged update helper is not installed")
    command = [sys.executable, str(install / "appdock.py"), *list(restart_args)] if restart_command is None else restart_command
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise AppDockError("restart command is invalid")
    helper_command = [
        sys.executable,
        "-B",
        str(helper),
        "--staged", str(staged),
        "--install", str(install),
        "--data", str(data),
        "--pid", str(current_pid if current_pid is not None else os.getpid()),
        "--restart-script", command[1] if len(command) > 1 else str(install / "appdock.py"),
    ]
    handshake = data / "runtime" / f"update-helper-{uuid.uuid4().hex}.ready"
    handshake_token = secrets.token_urlsafe(32)
    handshake.parent.mkdir(parents=True, exist_ok=True)
    handshake.unlink(missing_ok=True)
    helper_command.extend(["--handshake", str(handshake), "--handshake-token", handshake_token])
    if command and command[0] != sys.executable:
        raise AppDockError("restart command must use the configured Python executable")
    for argument in command[2:]:
        helper_command.append(f"--restart-arg={argument}")
    runner = popen or subprocess.Popen
    try:
        process = runner(helper_command, shell=False, close_fds=True)
    except OSError as exc:
        raise AppDockError("could not launch update helper") from exc
    deadline = time.monotonic() + 5
    confirmed = False
    try:
        while time.monotonic() < deadline:
            if handshake.is_file():
                if not secrets.compare_digest(handshake.read_text(encoding="utf-8"), handshake_token):
                    raise AppDockError("update helper startup handshake is invalid")
                if callable(getattr(process, "poll", None)) and process.poll() is not None:
                    raise AppDockError("update helper exited during startup")
                confirmed = True
                return process
            if callable(getattr(process, "poll", None)) and process.poll() is not None:
                raise AppDockError("update helper exited before startup handshake")
            time.sleep(0.05)
        raise AppDockError("update helper did not confirm startup")
    finally:
        handshake.unlink(missing_ok=True)
        if not confirmed and callable(getattr(process, "poll", None)) and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                if callable(getattr(process, "kill", None)):
                    process.kill()


def restart_appdock() -> None:
    """Replace the current process with the installed AppDock entry point."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>AppDock</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/app.js" defer></script>
</head>
<body>
  <header class="topbar">
    <div class="brand">App<span>Dock</span></div>
    <nav class="actions" aria-label="AppDock actions">
      <button id="addButton" type="button" class="primary">Add App</button>
      <button id="settingsButton" type="button">Settings / Updates</button>
    </nav>
  </header>

  <main class="shell">
    <div class="toolbar">
      <div>
        <h1>Your apps</h1>
        <p class="muted">Register explicit manifests, then control each process locally.</p>
      </div>
      <button id="refreshButton" type="button">Refresh</button>
    </div>
    <section id="apps" class="apps" aria-live="polite"></section>

    <section id="updatesPanel" class="panel" hidden>
      <h2>AppDock updates</h2>
      <p class="muted">Release assets are downloaded from the configured GitHub repository, checksum-verified, staged, and applied with rollback.</p>
      <div class="actions">
        <button id="checkUpdateButton" type="button">Check for updates</button>
        <button id="updateButton" type="button" class="primary" hidden>Update now</button>
      </div>
      <p id="updateResult" class="status" role="status"></p>
      <pre id="releaseNotes" class="release-notes"></pre>
    </section>
  </main>

  <div id="addModal" class="modal" hidden>
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="addTitle">
      <h2 id="addTitle">Add an app</h2>
      <p class="warning">Apps are code. AppDock previews and registers manifests, but never starts an imported app automatically.</p>

      <label for="localFolder">Local app folder</label>
      <input id="localFolder" autocomplete="off" placeholder="C:\path\to\your-app">
      <button id="previewLocalButton" type="button" class="primary">Preview local app</button>

      <details class="advanced">
        <summary>Advanced GitHub import</summary>
        <p class="muted">Only canonical public github.com repository URLs are accepted. AppDock clones into private staging and previews the root manifest.</p>
        <label for="githubUrl">Repository URL</label>
        <input id="githubUrl" type="url" autocomplete="off" placeholder="https://github.com/owner/repository">
        <button id="previewGithubButton" type="button" class="primary">Preview GitHub app</button>
      </details>

      <section id="previewPanel" hidden>
        <h3>Manifest preview</h3>
        <pre id="previewOutput" class="preview-output"></pre>
        <button id="registerButton" type="button" class="primary" hidden>Register app</button>
      </section>

      <div class="actions">
        <button id="closeAddButton" type="button">Close</button>
      </div>
    </section>
  </div>
</body>
</html>'''


class UpdateCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._staged: dict[str, Any] | None = None
        self._staging = False
        self._applying = False

    def begin_stage(self) -> None:
        with self._lock:
            if self._applying:
                raise AppDockError("an update is already being applied")
            if self._staged is not None or self._staging:
                raise AppDockError("an update is already staged or staging")
            self._staging = True

    def finish_stage(self, staged: dict[str, Any]) -> None:
        if not isinstance(staged, dict) or not isinstance(staged.get("digest"), str):
            raise AppDockError("staged update is invalid")
        with self._lock:
            if not self._staging or self._applying or self._staged is not None:
                raise AppDockError("update staging reservation is invalid")
            self._staged = staged
            self._staging = False

    def cancel_stage(self) -> None:
        with self._lock:
            self._staging = False

    def store(self, staged: dict[str, Any]) -> None:
        if not isinstance(staged, dict) or not isinstance(staged.get("digest"), str):
            raise AppDockError("staged update is invalid")
        with self._lock:
            if self._applying:
                raise AppDockError("an update is already being applied")
            if self._staged is not None or self._staging:
                raise AppDockError("an update is already staged")
            self._staged = staged

    def claim(self, confirmation: str) -> dict[str, Any]:
        with self._lock:
            if self._applying:
                raise AppDockError("an update is already being applied")
            if self._staging:
                raise AppDockError("an update is still staging")
            if not self._staged or confirmation != self._staged.get("digest"):
                raise AppDockError("staged update confirmation is stale or invalid")
            staged = self._staged
            self._staged = None
            self._applying = True
            return staged

    def restore(self, staged: dict[str, Any]) -> None:
        with self._lock:
            self._applying = False
            self._staged = staged


def stage_coordinated_update(
    release: dict[str, Any],
    config: AppDockConfig,
    coordinator: UpdateCoordinator,
    *,
    repository: str,
    stager: Callable[..., dict[str, Any]] = stage_update,
) -> dict[str, Any]:
    coordinator.begin_stage()
    staged: dict[str, Any] | None = None
    try:
        staged = stager(release, config, repository=repository)
        coordinator.finish_stage(staged)
    except Exception:
        if staged is not None:
            raw_path = staged.get("path")
            if isinstance(raw_path, str):
                staged_path = Path(raw_path).resolve()
                if staged_path != config.updates_root.resolve() and _inside(staged_path, config.updates_root):
                    _remove_tree(staged_path, ignore_errors=True)
        coordinator.cancel_stage()
        raise
    return staged


class Handler(BaseHTTPRequestHandler):
    manager: AppManager = AppManager()
    config: AppDockConfig = manager.config
    local: LocalFolderOnboarding = LocalFolderOnboarding(config)
    github: GitHubOnboarding = GitHubOnboarding(config)
    checker: ReleaseChecker = ReleaseChecker(config.update_repository)
    coordinator: UpdateCoordinator = UpdateCoordinator()
    ready_token: str | None = None
    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'none'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'",
        )

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _static(self, filename: str, content_type: str) -> None:
        body = (Path(__file__).resolve().parent / "static" / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host", "")

    def _approved_host(self) -> bool:
        raw_host = self.headers.get("Host", "")
        try:
            host = (urllib.parse.urlsplit(f"//{raw_host}").hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        configured = {
            item.strip().lower().rstrip(".")
            for item in os.environ.get("APPDOCK_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        return host in {"127.0.0.1", "localhost", "::1", *configured}

    def _body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise AppDockError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_JSON_BYTES:
            raise AppDockError("request body is too large")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise AppDockError("JSON body must be an object")
        return value

    def do_GET(self) -> None:
        if not self._approved_host():
            self._json({"error": "unapproved Host header"}, 421)
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/":
                self._html(HTML)
            elif path == "/static/app.css": self._static("app.css", "text/css; charset=utf-8")
            elif path == "/static/app.js": self._static("app.js", "text/javascript; charset=utf-8")
            elif path == "/health":
                health = {"ok": True, "service": "appdock", "version": CURRENT_VERSION}
                if self.ready_token is not None:
                    health["ready_token"] = self.ready_token
                self._json(health)
            elif path == "/api/apps": self._json(self.manager.all_status())
            elif path == "/api/config": self._json({"version": CURRENT_VERSION, "update_repository": self.config.update_repository})
            elif path == "/api/updates/check":
                release = self.checker.check()
                self._json({**release, "confirmation_digest": _digest(release)})
            elif path.startswith("/api/apps/") and path.endswith("/logs"):
                app_id = urllib.parse.unquote(path.removeprefix("/api/apps/").removesuffix("/logs").strip("/")); spec = self.manager.discover().get(app_id)
                if spec is None: self._json({"error": "app not found"}, 404)
                else: self._json({"lines": self.manager.logs(spec)})
            else: self._json({"error": "not found"}, 404)
        except AppDockError as exc: self._json({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        if not self._approved_host():
            self._json({"error": "unapproved Host header"}, 421)
            return
        if not self._same_origin(): self._json({"error": "cross-origin requests are not allowed"}, 403); return
        try: body = self._body()
        except (AppDockError, ValueError, TypeError) as exc: self._json({"error": str(exc)}, 400); return
        path = urllib.parse.urlsplit(self.path).path; parts = path.strip("/").split("/")
        try:
            if len(parts) == 4 and parts[:2] == ["api", "apps"] and parts[3] in {"start", "stop", "restart"}: self._json(getattr(self.manager, parts[3])(urllib.parse.unquote(parts[2]))); return
            if len(parts) == 5 and parts[:2] == ["api", "apps"] and parts[3] == "move" and parts[4] in {"up", "down"}: self._json(self.manager.move(urllib.parse.unquote(parts[2]), parts[4])); return
            if path == "/api/onboarding/local/preview": self._json(self.local.preview(body.get("folder"))); return
            if path == "/api/onboarding/local/register": self._json(self.local.register(body.get("folder"), body.get("confirmation", ""), body.get("preview"))); return
            if path == "/api/onboarding/github/preview": self._json(self.github.preview(body.get("url"))); return
            if path == "/api/onboarding/github/register": self._json(self.github.register(body.get("preview"), body.get("confirmation", ""))); return
            if path == "/api/onboarding/github/cleanup": self._json({"cleaned": self.github.cleanup(body.get("staging_id", ""))}); return
            if path == "/api/updates/stage":
                release = self.checker.check()
                if not release.get("update_available") or body.get("confirmation") != _digest(release):
                    raise AppDockError("update confirmation is stale or invalid")
                staged = stage_coordinated_update(release, self.config, Handler.coordinator, repository=self.config.update_repository)
                self._json({"staged": True, "version": staged["version"], "confirmation_digest": staged["digest"]}); return
            if path == "/api/updates/apply":
                staged = Handler.coordinator.claim(body.get("confirmation", ""))
                install_dir = Path(__file__).resolve().parent
                restart_args = ["--host", str(self.server.server_address[0]), "--port", str(self.server.server_address[1]), "--data-dir", str(self.config.data_root)]
                try:
                    launch_update_helper(staged["path"], install_dir, self.config.data_root, current_pid=os.getpid(), restart_args=restart_args)
                except Exception:
                    Handler.coordinator.restore(staged)
                    raise
                self._json({"restart_pending": True, "version": staged.get("version")}, 202)
                threading.Thread(target=self.server.shutdown, daemon=True).start(); return
            self._json({"error": "not found"}, 404)
        except KeyError: self._json({"error": "app not found"}, 404)
        except (AppDockError, OSError, ValueError, TypeError) as exc: self._json({"error": str(exc)}, 400)

    def log_message(self, fmt: str, *args: Any) -> None: return


def main() -> None:
    parser = argparse.ArgumentParser(description="AppDock local app dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--ready-token", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    validate_bind_host(args.host)
    if args.ready_token is not None and not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", args.ready_token):
        parser.error("invalid readiness token")
    config = AppDockConfig.from_environment(data_dir=args.data_dir)
    config.ensure(); Handler.config = config; Handler.manager = AppManager(config=config); Handler.local = LocalFolderOnboarding(config); Handler.github = GitHubOnboarding(config); Handler.checker = ReleaseChecker(config.update_repository); Handler.coordinator = UpdateCoordinator(); Handler.ready_token = args.ready_token
    cleanup_stop = threading.Event()
    cleanup_thread = threading.Thread(target=_staging_cleanup_loop, args=(Handler.github, cleanup_stop), name="appdock-staging-cleanup", daemon=True)
    cleanup_thread.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AppDock listening at http://{args.host}:{args.port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        cleanup_stop.set()
        server.server_close()


if __name__ == "__main__": main()
