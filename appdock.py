from __future__ import annotations

import argparse
from functools import wraps
import hashlib
import http.client
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

if os.name == "nt":
    import msvcrt
else:
    import fcntl

MANIFEST_NAME = "appdock.json"
CURRENT_VERSION = "0.2.2-beta.1"
DEFAULT_UPDATE_REPOSITORY = "eWOOD29/appdock"
DEFAULT_UPDATE_CHANNEL = "stable"
UPDATE_CHANNELS = frozenset({"stable", "beta"})
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
BETA_TAG_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-beta\.(0|[1-9]\d*)$")
MAX_JSON_BYTES = 128 * 1024
MAX_UPDATE_ASSET_BYTES = 100 * 1024 * 1024
MAX_UPDATE_FILE_COUNT = 4096
MAX_UPDATE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_GITHUB_STAGE_BYTES = 250 * 1024 * 1024
MAX_GITHUB_STAGE_FILES = 20_000
MAX_GITHUB_STAGING_TOTAL_BYTES = 500 * 1024 * 1024
MAX_GITHUB_STAGING_TOTAL_FILES = 40_000
GITHUB_STAGE_TTL_SECONDS = 24 * 60 * 60
MAX_EXTENSION_CONFIG_BYTES = 64 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 64 * 1024
MAX_EXTENSION_PROVIDERS = 8
MAX_EXTENSION_WIDGETS = 16
MAX_WIDGET_METRICS = 12
MAX_WIDGET_PROGRESS = 6
UPDATE_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PRIVATE_PACKAGE_MANIFEST = "appdock-private-package.json"
PRIVATE_PACKAGE_HASH_MANIFEST = "PACKAGE-MANIFEST.json"
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
LM_STUDIO_TIMEOUT = 8.0
LM_STUDIO_MAX_ERROR = 240
LM_STUDIO_DOCS_URL = "https://lmstudio.ai/docs/cli"
LM_LOAD_FIELDS = {
    "model", "model_key", "gpu", "context_length", "parallel", "ttl", "identifier",
    "speculative_draft_mtp", "speculative_draft_simple", "speculative_draft_model",
    "speculative_draft_max_tokens", "speculative_draft_min_tokens",
    "speculative_draft_min_continue_probability",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


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
    private_root: Path
    extension_config_path: Path
    migration_root: Path
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
            private_root=root / "private",
            extension_config_path=root / "private" / "extensions.json",
            migration_root=root / "migrations",
            update_repository=os.environ.get("APPDOCK_UPDATE_REPOSITORY", DEFAULT_UPDATE_REPOSITORY),
        )

    def ensure(self) -> None:
        _assert_no_link_or_reparse_ancestor(self.data_root)
        for path in (
            self.registry_root,
            self.install_root,
            self.runtime_root,
            self.logs_root,
            self.staging_root,
            self.updates_root,
            self.private_root,
            self.migration_root,
        ):
            _assert_no_link_or_reparse_ancestor(path)
            path.mkdir(parents=True, exist_ok=True)


@dataclass
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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AppDockError(f"JSON contains duplicate key: {key}")
        result[key] = value
    return result


def _loads_strict_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except AppDockError:
        raise
    except json.JSONDecodeError as exc:
        raise AppDockError("JSON file is invalid") from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _durable_write_bytes(path: Path, payload: bytes) -> None:
    path = Path(path).expanduser().absolute()
    _assert_safe_directory_ancestors(path.parent)
    if _is_link_or_reparse(path):
        raise AppDockError("update output path is unsafe")
    if path.exists():
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AppDockError("update output is not a regular single-link file")
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_directory_ancestors(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _assert_safe_directory_ancestors(temporary.parent)
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _assert_safe_directory_ancestors(path.parent)
    if _is_link_or_reparse(path) or (path.exists() and path.stat().st_nlink != 1):
        temporary.unlink(missing_ok=True)
        raise AppDockError("update output path changed while writing")
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _durable_write_json(path: Path, payload: Any) -> None:
    _durable_write_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _durable_copy(source: Path, destination: Path) -> None:
    _durable_write_bytes(destination, _read_regular_single_link(source, MAX_UPDATE_UNCOMPRESSED_BYTES))


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


def _assert_safe_directory_ancestors(path: Path) -> None:
    current = path.expanduser().absolute()
    while True:
        if current.exists() or current.is_symlink():
            if _is_link_or_reparse(current) or not current.is_dir():
                raise AppDockError("updater path has an unsafe or non-directory ancestor")
        parent = current.parent
        if parent == current:
            return
        current = parent


@dataclass
class _UpdateLockState:
    path: Path
    stream: Any
    owner_thread: int
    references: int = 1


class UpdateLock:
    def __init__(self, key: str, state: _UpdateLockState):
        self._key = key
        self._state = state
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        with _UPDATE_LOCK_GUARD:
            if self._released:
                return
            self._released = True
            state = _UPDATE_LOCK_STATES.get(self._key)
            if state is None:
                return
            state.references -= 1
            if state.references:
                return
            try:
                if os.name == "nt":
                    state.stream.seek(0)
                    msvcrt.locking(state.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(state.stream.fileno(), fcntl.LOCK_UN)
            finally:
                state.stream.close()
                _UPDATE_LOCK_STATES.pop(self._key, None)

    def __enter__(self) -> "UpdateLock":
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.release()


_UPDATE_LOCK_GUARD = threading.Lock()
_UPDATE_LOCK_STATES: dict[str, _UpdateLockState] = {}


def _update_lock_path(data_dir: str | Path) -> Path:
    data = Path(data_dir).expanduser().absolute()
    _assert_safe_directory_ancestors(data)
    runtime = data / "runtime"
    _assert_safe_directory_ancestors(runtime)
    try:
        runtime.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AppDockError("could not prepare updater runtime directory") from exc
    _assert_safe_directory_ancestors(data)
    _assert_safe_directory_ancestors(runtime)
    canonical_data = data.resolve()
    canonical_runtime = runtime.resolve()
    if not _inside(canonical_runtime, canonical_data):
        raise AppDockError("updater runtime directory escapes the data root")
    lock_path = runtime / "update.lock"
    if _is_link_or_reparse(lock_path):
        raise AppDockError("updater lock path is unsafe")
    return lock_path


def acquire_update_lock(data_dir: str | Path) -> UpdateLock:
    lock_path = _update_lock_path(data_dir)
    key = os.path.normcase(str(lock_path.resolve()))
    with _UPDATE_LOCK_GUARD:
        existing = _UPDATE_LOCK_STATES.get(key)
        if existing is not None:
            if existing.owner_thread != threading.get_ident():
                raise AppDockError("could not acquire updater lock; another update may be in progress")
            existing.references += 1
            return UpdateLock(key, existing)
        stream = None
        try:
            stream = lock_path.open("a+b")
            if os.name == "nt":
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if stream is not None:
                stream.close()
            raise AppDockError("could not acquire updater lock; another update may be in progress") from exc
        state = _UpdateLockState(lock_path, stream, threading.get_ident())
        _UPDATE_LOCK_STATES[key] = state
        return UpdateLock(key, state)


def _locked_transaction(data_index: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            data_dir = args[data_index] if len(args) > data_index else kwargs.get("data_dir")
            if data_dir is None:
                raise AppDockError("update data directory is required")
            with acquire_update_lock(data_dir):
                return function(*args, **kwargs)
        return wrapped
    return decorate


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
        if _is_link_or_reparse(Path(current)) or not _inside(current_path, root):
            raise AppDockError("staging path escapes its root")
        for name in [*dirs, *files]:
            path = Path(current) / name
            if path.is_symlink() or _is_link_or_reparse(path) or not _inside(path.resolve(), root):
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


def discover_lms(env: dict[str, str] | None = None) -> str | None:
    """Find the optional LM Studio CLI without exposing its location to clients."""
    environment = env if env is not None else os.environ
    candidates: list[Path] = []
    override = environment.get("APPDOCK_LMS_PATH", "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    profile = Path(environment.get("USERPROFILE") or str(Path.home())).expanduser()
    local_app_data = Path(environment.get("LOCALAPPDATA") or (profile / "AppData" / "Local"))
    for directory in (
        profile / ".lmstudio" / "bin",
        local_app_data / "LM Studio" / "bin",
        profile / "AppData" / "Local" / "LM Studio" / "bin",
    ):
        for name in ("lms.exe", "lms.cmd", "lms"):
            candidates.append(directory / name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    path_entries = [Path(entry) for entry in environment.get("PATH", "").split(os.pathsep) if entry]
    for directory in path_entries:
        for name in ("lms.exe", "lms.cmd", "lms"):
            candidate = directory / name
            try:
                if candidate.is_file():
                    return str(candidate.resolve())
            except OSError:
                continue
    for name in ("lms.exe", "lms.cmd", "lms"):
        found = shutil.which(name, path=environment.get("PATH"))
        if found:
            return found
    return None


def _bounded_cli_error(_text: str, fallback: str) -> str:
    """Return a bounded generic CLI message; stderr may contain local paths."""
    return fallback[:LM_STUDIO_MAX_ERROR]


def _as_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _first_text(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _as_text(raw.get(key))
        if value is not None:
            return value
    return None


def _safe_model_token(value: Any) -> str | None:
    text = _as_text(value)
    if text is None or len(text) > 256 or _CONTROL_RE.search(text):
        return None
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return None
    return text


def _normalize_model(raw: Any, *, loaded: bool = False) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    key = next((_safe_model_token(raw.get(name)) for name in ("modelKey", "model_key", "key", "model", "id") if _safe_model_token(raw.get(name)) is not None), None)
    identifier = next((_safe_model_token(raw.get(name)) for name in ("identifier", "loadedIdentifier") if _safe_model_token(raw.get(name)) is not None), None)
    if key is None and identifier is None:
        return None
    variants = raw.get("variants")
    normalized_variants = [token for item in variants if (token := _safe_model_token(item)) is not None] if isinstance(variants, list) else []
    quantization = raw.get("quantization")
    if isinstance(quantization, dict):
        quantization = _first_text(quantization, "name", "label")
    else:
        quantization = _as_text(quantization)
    item: dict[str, Any] = {
        "key": key or identifier,
        "display_name": _first_text(raw, "displayName", "display_name", "name") or key or identifier or "Unknown model",
        "type": _first_text(raw, "type", "modelType"),
        "format": _first_text(raw, "format"),
        "publisher": _first_text(raw, "publisher"),
        "params": _first_text(raw, "paramsString", "params", "parameterCount"),
        "architecture": _first_text(raw, "architecture", "architectureName"),
        "quantization": quantization,
        "size_bytes": _as_number(raw.get("sizeBytes", raw.get("size_bytes"))),
        "vision": raw.get("vision") if isinstance(raw.get("vision"), bool) else None,
        "tool_capability": raw.get("trainedForToolUse") if isinstance(raw.get("trainedForToolUse"), bool) else None,
        "max_context": _as_number(raw.get("maxContextLength", raw.get("max_context"))),
        "variants": normalized_variants,
    }
    if loaded:
        ttl_ms = _as_number(raw.get("ttlMs"))
        item.update({
            "identifier": identifier or key,
            "context_length": _as_number(raw.get("contextLength", raw.get("context_length"))),
            "status": _first_text(raw, "status"),
            "parallel": _as_number(raw.get("parallel")),
            "ttl_seconds": ttl_ms / 1000 if ttl_ms is not None else _as_number(raw.get("ttl_seconds")),
        })
    return item


def _identity_tokens(model: dict[str, Any]) -> set[str]:
    values = [model.get("key"), *(model.get("variants") or [])]
    return {
        value.strip().replace("\\", "/").lower().rstrip("/")
        for value in values
        if isinstance(value, str) and value.strip()
    }


class LMStudioAdapter:
    """Defensive, local-only adapter for the optional JSON-producing lms CLI."""

    def __init__(self, executable: str | None = None, timeout: float = LM_STUDIO_TIMEOUT):
        self.executable = executable if executable is not None else discover_lms()
        self.timeout = max(0.1, min(float(timeout), LM_STUDIO_TIMEOUT))

    def _json_command(self, args: list[str]) -> tuple[bool, Any, str, bool]:
        if not self.executable:
            return False, None, "LM Studio CLI was not found", False
        try:
            result = subprocess.run(
                [self.executable, *args], shell=False, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return False, None, "LM Studio CLI timed out", True
        except (OSError, subprocess.SubprocessError):
            return False, None, "LM Studio CLI could not be started", False
        if result.returncode != 0:
            return False, None, _bounded_cli_error(result.stderr, "LM Studio application/server is unavailable"), False
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False, None, f"lms {' '.join(args)} returned malformed JSON", False
        if not isinstance(value, list):
            return False, None, f"lms {' '.join(args)} returned an unexpected JSON shape", False
        return True, value, "", False

    def execute(self, args: list[str]) -> tuple[bool, bool, str]:
        if not self.executable:
            return False, False, "LM Studio CLI was not found"
        try:
            result = subprocess.run(
                [self.executable, *args], shell=False, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return False, True, "LM Studio CLI timed out"
        except (OSError, subprocess.SubprocessError):
            return False, False, "LM Studio CLI could not be started"
        if result.returncode != 0:
            return False, False, "LM Studio rejected the requested operation"
        return True, False, ""

    def snapshot(self) -> dict[str, Any]:
        base = {
            "available": bool(self.executable), "reachable": False, "running": False,
            "status": "absent" if not self.executable else "unavailable",
            "installed_models": [], "loaded_instances": [], "error": None, "warning": None,
            "docs_url": LM_STUDIO_DOCS_URL,
        }
        if not self.executable:
            base["error"] = "LM Studio is unavailable. This optional integration needs the lms CLI."
            return base
        ls_ok, ls_raw, ls_error, ls_timeout = self._json_command(["ls", "--json"])
        ps_ok, ps_raw, ps_error, ps_timeout = self._json_command(["ps", "--json"])
        installed = [model for raw in (ls_raw if ls_ok else []) if (model := _normalize_model(raw)) is not None]
        loaded = [model for raw in (ps_raw if ps_ok else []) if (model := _normalize_model(raw, loaded=True)) is not None]
        loaded_tokens = set().union(*(_identity_tokens(item) for item in loaded)) if loaded else set()
        for model in installed:
            model["loaded"] = bool(_identity_tokens(model) & loaded_tokens)
        base.update({
            "reachable": ls_ok or ps_ok,
            "running": ps_ok,
            "installed_models": installed,
            "loaded_instances": loaded,
        })
        if ls_ok and ps_ok:
            base["status"] = "empty" if not installed and not loaded else "running"
        elif ls_timeout or ps_timeout:
            base["status"] = "timeout"
            base["warning"] = "LM Studio status check timed out."
        elif ls_ok or ps_ok:
            base["status"] = "partial"
            failed = ls_error if not ls_ok else ps_error
            base["warning"] = "LM Studio returned malformed status data." if "malformed" in failed or "unexpected JSON" in failed else ("Installed model list unavailable: lms ls failed." if not ls_ok else "Loaded model status unavailable: lms ps failed.")
        else:
            malformed = any("malformed" in error or "unexpected JSON" in error for error in (ls_error, ps_error))
            if malformed:
                base["status"] = "malformed"
                base["error"] = "LM Studio returned malformed status data."
            else:
                base["error"] = "LM Studio CLI was found, but the application/server is unavailable."
        return base


def _validated_string(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or _CONTROL_RE.search(value):
        raise ValueError(f"{field} must be a non-empty safe string of at most {maximum} characters")
    return value


def _validated_cli_operand(value: Any, field: str, maximum: int) -> str:
    operand = _validated_string(value, field, maximum)
    if operand.startswith("-"):
        raise ValueError(f"{field} must not begin with '-'")
    return operand


def _validated_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _validated_float(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be a number from {minimum} to {maximum}")
    return float(value)


def _installed_model_key(model: Any) -> str | None:
    if not isinstance(model, dict) or not isinstance(model.get("key"), str) or not model["key"].strip():
        return None
    return _validated_cli_operand(model["key"].strip(), "installed model key", 256)


def build_lm_load_args(request: dict[str, Any], installed_models: list[dict[str, Any]]) -> list[str]:
    if not isinstance(request, dict):
        raise ValueError("JSON body must be an object")
    unknown = set(request) - LM_LOAD_FIELDS
    if unknown:
        raise ValueError("unknown load setting: " + sorted(unknown)[0])
    if "model" in request and "model_key" in request:
        raise ValueError("specify only one model key")
    requested = _validated_cli_operand(request.get("model", request.get("model_key")), "model", 256)
    canonical = next((key for item in installed_models if (key := _installed_model_key(item)) and key.lower().replace("\\", "/") == requested.lower().replace("\\", "/")), None)
    if canonical is None:
        raise ValueError("model must be one of the installed model keys")
    args = ["load"]
    gpu = request.get("gpu")
    if gpu not in (None, "auto"):
        if gpu in ("off", "max"):
            args += ["--gpu", gpu]
        else:
            ratio = _validated_float(gpu, "gpu", 0.0, 1.0)
            args += ["--gpu", str(int(ratio)) if ratio.is_integer() else str(ratio)]
    if request.get("context_length") is not None:
        args += ["--context-length", str(_validated_int(request["context_length"], "context_length", 1, 1_048_576))]
    if request.get("parallel") is not None:
        args += ["--parallel", str(_validated_int(request["parallel"], "parallel", 1, 128))]
    if request.get("ttl") is not None:
        args += ["--ttl", str(_validated_int(request["ttl"], "ttl", 1, 604800))]
    if request.get("identifier") is not None:
        args += ["--identifier", _validated_cli_operand(request["identifier"], "identifier", 128)]
    mtp = request.get("speculative_draft_mtp", "default")
    simple = request.get("speculative_draft_simple", "default")
    if mtp not in {"default", "enable", "disable"}:
        raise ValueError("speculative_draft_mtp must be enable, disable, or default")
    if simple not in {"default", "enable", "disable"}:
        raise ValueError("speculative_draft_simple must be enable, disable, or default")
    if mtp == "enable" and simple == "enable":
        raise ValueError("MTP and Simple speculative decoding cannot both be enabled")
    draft_fields = ("speculative_draft_model", "speculative_draft_max_tokens", "speculative_draft_min_tokens", "speculative_draft_min_continue_probability")
    draft_set = any(request.get(field) is not None for field in draft_fields)
    if simple == "enable" and request.get("speculative_draft_model") is None:
        raise ValueError("speculative_draft_model is required when Simple speculative decoding is enabled")
    if simple != "enable" and request.get("speculative_draft_model") is not None:
        raise ValueError("speculative_draft_model requires Simple speculative decoding")
    if draft_set and simple != "enable" and mtp != "enable":
        raise ValueError("draft settings require speculative decoding to be enabled")
    if mtp == "enable":
        args.append("--speculative-draft-mtp")
    elif mtp == "disable":
        args.append("--no-speculative-draft-mtp")
    if simple == "enable":
        args.append("--speculative-draft-simple")
    if request.get("speculative_draft_model") is not None:
        args += ["--speculative-draft-model", _validated_cli_operand(request["speculative_draft_model"], "speculative_draft_model", 256)]
    for field, option, minimum, maximum in (
        ("speculative_draft_max_tokens", "--speculative-draft-max-tokens", 1, 512),
        ("speculative_draft_min_tokens", "--speculative-draft-min-tokens", 0, 512),
    ):
        if request.get(field) is not None:
            args += [option, str(_validated_int(request[field], field, minimum, maximum))]
    if request.get("speculative_draft_min_continue_probability") is not None:
        probability = _validated_float(request["speculative_draft_min_continue_probability"], "speculative_draft_min_continue_probability", 0.0, 1.0)
        args += ["--speculative-draft-min-continue-probability", str(probability)]
    if request.get("speculative_draft_min_tokens") is not None and request.get("speculative_draft_max_tokens") is not None and request["speculative_draft_min_tokens"] > request["speculative_draft_max_tokens"]:
        raise ValueError("speculative_draft_min_tokens cannot exceed speculative_draft_max_tokens")
    return [*args, "-y", canonical]


def build_lm_unload_args(request: dict[str, Any], loaded_instances: list[dict[str, Any]]) -> list[str]:
    if not isinstance(request, dict) or set(request) != {"identifier"}:
        raise ValueError("unload requires only an identifier")
    identifier = _validated_cli_operand(request.get("identifier"), "identifier", 128)
    loaded_identifiers = {
        item.get("identifier") for item in loaded_instances
        if isinstance(item, dict) and isinstance(item.get("identifier"), str)
    }
    if identifier not in loaded_identifiers:
        raise ValueError("identifier is not currently loaded")
    return ["unload", identifier]


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


_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"^([A-Za-z]):\\(.+)$")
_WINDOWS_RESERVED_COMPONENT_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


def normalize_windows_external_directory(value: Any) -> str:
    """Validate and canonicalize a Windows external directory without host pathlib semantics.

    AppDock v0.1.2 intentionally supports drive-qualified paths only. UNC and device
    namespaces are rejected until they have a separately reviewed package contract.
    """
    if not isinstance(value, str) or not value or len(value) > 32767 or "\x00" in value:
        raise AppDockError("private registration must preserve an absolute external directory")
    if "/" in value or value.startswith("\\\\"):
        raise AppDockError("private registration must preserve an absolute external directory")
    match = _WINDOWS_DRIVE_ABSOLUTE_RE.fullmatch(value)
    if match is None:
        raise AppDockError("private registration must preserve an absolute external directory")
    drive, tail = match.groups()
    tail = tail.rstrip("\\")
    if not tail:
        raise AppDockError("private registration must preserve an absolute external directory")
    components = tail.split("\\")
    if any(
        not component
        or component in {".", ".."}
        or component[-1] in {" ", "."}
        or any(ord(character) < 32 or character in '<>:"|?*' for character in component)
        or _WINDOWS_RESERVED_COMPONENT_RE.fullmatch(component)
        for component in components
    ):
        raise AppDockError("private registration must preserve an absolute external directory")
    return f"{drive.upper()}:\\" + "\\".join(components)


def _normalize_windows_relative_path(value: Any, *, field_name: str) -> str:
    if value in (None, "", "."):
        return "."
    if not isinstance(value, str) or len(value) > 32767 or "\x00" in value or "/" in value:
        raise ManifestError(f"{field_name} must be a safe relative Windows path")
    if value.startswith("\\") or re.match(r"^[A-Za-z]:", value):
        raise ManifestError(f"{field_name} must be a safe relative Windows path")
    value = value.rstrip("\\")
    components = value.split("\\")
    if any(
        not component
        or component in {".", ".."}
        or component[-1] in {" ", "."}
        or any(ord(character) < 32 or character in '<>:"|?*' for character in component)
        or _WINDOWS_RESERVED_COMPONENT_RE.fullmatch(component)
        for component in components
    ):
        raise ManifestError(f"{field_name} must be a safe relative Windows path")
    return "\\".join(components)


def _normalize_posix_external_directory(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 32767 or "\x00" in value:
        raise AppDockError("private registration must preserve an absolute external directory")
    if not value.startswith("/") or "\\" in value or ":" in value or value.startswith("//"):
        raise AppDockError("private registration must preserve an absolute external directory")
    components = value.split("/")[1:]
    while components and components[-1] == "":
        components.pop()
    if not components or any(not component or component in {".", ".."} for component in components):
        raise AppDockError("private registration must preserve an absolute external directory")
    return "/" + "/".join(components)


def _normalize_posix_relative_path(value: Any, *, field_name: str) -> str:
    if value in (None, "", "."):
        return "."
    if not isinstance(value, str) or len(value) > 32767 or "\x00" in value or "\\" in value:
        raise ManifestError(f"{field_name} must be a safe relative POSIX path")
    if value.startswith("/"):
        raise ManifestError(f"{field_name} must be a safe relative POSIX path")
    value = value.rstrip("/")
    components = value.split("/")
    if any(not component or component in {".", ".."} for component in components):
        raise ManifestError(f"{field_name} must be a safe relative POSIX path")
    return "/".join(components)


def _private_path_flavor(value: Any) -> str | None:
    try:
        normalize_windows_external_directory(value)
        return "windows"
    except AppDockError:
        pass
    try:
        _normalize_posix_external_directory(value)
        return "posix"
    except AppDockError:
        return None


def normalize_private_registration(raw: dict[str, Any], *, manifest_dir: Path, path_flavor: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("external") is not True:
        raise AppDockError("private registration must preserve an absolute external directory")
    if path_flavor == "windows":
        directory = normalize_windows_external_directory(raw.get("directory"))
        cwd = _normalize_windows_relative_path(raw.get("cwd", "."), field_name="cwd")
    elif path_flavor == "posix":
        directory = _normalize_posix_external_directory(raw.get("directory"))
        cwd = _normalize_posix_relative_path(raw.get("cwd", "."), field_name="cwd")
    else:
        raise AppDockError("private package path flavor is unsupported")

    # Reuse the ordinary manifest validator for every non-path field, but feed it a
    # package-local placeholder so validation never resolves or probes the external source.
    validation_raw = dict(raw)
    validation_raw["directory"] = str(manifest_dir)
    validation_raw["cwd"] = "."
    normalized = normalize_manifest(
        validation_raw,
        manifest_dir=manifest_dir,
        directory=manifest_dir,
        external=True,
        allow_outside=True,
    )
    normalized["directory"] = directory
    normalized["cwd"] = cwd
    return normalized


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


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    url: str
    connect_timeout: float
    read_timeout: float
    cache_seconds: float


@dataclass(frozen=True)
class WidgetSpec:
    widget_id: str
    widget_type: str
    title: str
    provider_id: str
    drill_down_url: str = ""


@dataclass(frozen=True)
class ExtensionConfig:
    hidden_app_ids: frozenset[str] = frozenset()
    providers: tuple[ProviderSpec, ...] = ()
    widgets: tuple[WidgetSpec, ...] = ()


def _bounded_text(value: Any, field_name: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AppDockError(f"{field_name} must be text")
    text = value.strip()
    if (not text and not allow_empty) or len(text) > maximum:
        raise AppDockError(f"{field_name} is invalid")
    if any(ord(char) < 32 and char not in "\t" for char in text) or "<" in text or ">" in text:
        raise AppDockError(f"{field_name} contains unsupported markup or control characters")
    return text


def _literal_loopback_provider_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise AppDockError("provider URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AppDockError("provider URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.netloc
        or port is None
    ):
        raise AppDockError("provider URL must be an explicit literal-loopback http URL with a port")
    return urllib.parse.urlunsplit(parsed)


def _normalized_extension_payload(config: ExtensionConfig) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "visibility": {"hidden_app_ids": sorted(config.hidden_app_ids)},
        "providers": [
            {
                "id": item.provider_id,
                "url": item.url,
                "connect_timeout_ms": int(item.connect_timeout * 1000),
                "read_timeout_ms": int(item.read_timeout * 1000),
                "cache_seconds": item.cache_seconds,
            }
            for item in config.providers
        ],
        "widgets": [
            {
                "id": item.widget_id,
                "type": item.widget_type,
                "title": item.title,
                "provider_id": item.provider_id,
                **({"drill_down_url": item.drill_down_url} if item.drill_down_url else {}),
            }
            for item in config.widgets
        ],
    }


def parse_extension_config(raw: Any) -> ExtensionConfig:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "visibility", "providers", "widgets"}:
        raise AppDockError("private extension configuration is invalid")
    if raw.get("schema_version") != 1:
        raise AppDockError("unsupported private extension configuration schema")
    visibility = raw.get("visibility")
    if not isinstance(visibility, dict) or set(visibility) != {"hidden_app_ids"}:
        raise AppDockError("visibility configuration is invalid")
    hidden = visibility.get("hidden_app_ids")
    if not isinstance(hidden, list) or len(hidden) > 256:
        raise AppDockError("hidden app IDs are invalid")
    hidden_ids: list[str] = []
    for item in hidden:
        if not isinstance(item, str) or not APP_ID_RE.fullmatch(item) or item in hidden_ids:
            raise AppDockError("hidden app IDs are invalid")
        hidden_ids.append(item)

    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, list) or len(providers_raw) > MAX_EXTENSION_PROVIDERS:
        raise AppDockError("provider configuration is invalid")
    providers: list[ProviderSpec] = []
    provider_ids: set[str] = set()
    for item in providers_raw:
        required = {"id", "url", "connect_timeout_ms", "read_timeout_ms", "cache_seconds"}
        if not isinstance(item, dict) or set(item) != required:
            raise AppDockError("provider configuration is invalid")
        provider_id = item.get("id")
        if not isinstance(provider_id, str) or not APP_ID_RE.fullmatch(provider_id) or provider_id in provider_ids:
            raise AppDockError("provider ID is invalid")
        try:
            connect_ms = int(item.get("connect_timeout_ms"))
            read_ms = int(item.get("read_timeout_ms"))
            cache_seconds = float(item.get("cache_seconds"))
        except (TypeError, ValueError):
            raise AppDockError("provider bounds are invalid") from None
        if not 50 <= connect_ms <= 2000 or not 50 <= read_ms <= 5000 or not 0 <= cache_seconds <= 30:
            raise AppDockError("provider bounds are invalid")
        providers.append(
            ProviderSpec(
                provider_id=provider_id,
                url=_literal_loopback_provider_url(item.get("url")),
                connect_timeout=connect_ms / 1000,
                read_timeout=read_ms / 1000,
                cache_seconds=cache_seconds,
            )
        )
        provider_ids.add(provider_id)

    widgets_raw = raw.get("widgets")
    if not isinstance(widgets_raw, list) or len(widgets_raw) > MAX_EXTENSION_WIDGETS:
        raise AppDockError("widget configuration is invalid")
    widgets: list[WidgetSpec] = []
    widget_ids: set[str] = set()
    for item in widgets_raw:
        if not isinstance(item, dict) or not {"id", "type", "title", "provider_id"}.issubset(item) or not set(item).issubset(
            {"id", "type", "title", "provider_id", "drill_down_url"}
        ):
            raise AppDockError("widget configuration is invalid")
        widget_id = item.get("id")
        provider_id = item.get("provider_id")
        widget_type = item.get("type")
        if not isinstance(widget_id, str) or not APP_ID_RE.fullmatch(widget_id) or widget_id in widget_ids:
            raise AppDockError("widget ID is invalid")
        if provider_id not in provider_ids or widget_type not in {"metrics", "progress"}:
            raise AppDockError("widget provider or type is invalid")
        title = _bounded_text(item.get("title"), "widget title", maximum=80)
        drill_down_url = _validate_url(item.get("drill_down_url"), "drill_down_url")
        widgets.append(WidgetSpec(widget_id, widget_type, title, provider_id, drill_down_url))
        widget_ids.add(widget_id)
    return ExtensionConfig(frozenset(hidden_ids), tuple(providers), tuple(widgets))


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > 8:
        return depth
    if isinstance(value, dict):
        return max([depth, *(_json_depth(item, depth + 1) for item in value.values())])
    if isinstance(value, list):
        return max([depth, *(_json_depth(item, depth + 1) for item in value)])
    return depth


def _normalize_provider_widget(raw: Any, spec: WidgetSpec) -> dict[str, Any]:
    if not isinstance(raw, dict) or not set(raw).issubset({"status", "metrics", "progress", "timestamp"}):
        raise AppDockError("provider widget payload is invalid")
    status = raw.get("status", "unavailable")
    if status not in {"ok", "warning", "unavailable"}:
        raise AppDockError("provider widget status is invalid")
    timestamp = raw.get("timestamp", "")
    if timestamp:
        timestamp = _bounded_text(timestamp, "provider timestamp", maximum=64)
    result: dict[str, Any] = {
        "id": spec.widget_id,
        "type": spec.widget_type,
        "title": spec.title,
        "status": status,
        "timestamp": timestamp,
        "drill_down_url": spec.drill_down_url,
    }
    if spec.widget_type == "metrics":
        if "progress" in raw:
            raise AppDockError("metrics widget cannot contain progress data")
        metrics = raw.get("metrics", [])
        if not isinstance(metrics, list) or len(metrics) > MAX_WIDGET_METRICS:
            raise AppDockError("provider metrics are invalid")
        normalized_metrics: list[dict[str, str]] = []
        for item in metrics:
            if not isinstance(item, dict) or set(item) != {"label", "value"}:
                raise AppDockError("provider metric is invalid")
            normalized_metrics.append(
                {
                    "label": _bounded_text(item.get("label"), "metric label", maximum=40),
                    "value": _bounded_text(item.get("value"), "metric value", maximum=80),
                }
            )
        result["metrics"] = normalized_metrics
    else:
        if "metrics" in raw:
            raise AppDockError("progress widget cannot contain metric data")
        progress = raw.get("progress", [])
        if not isinstance(progress, list) or len(progress) > MAX_WIDGET_PROGRESS:
            raise AppDockError("provider progress data is invalid")
        normalized_progress: list[dict[str, Any]] = []
        for item in progress:
            if not isinstance(item, dict) or not set(item).issubset({"label", "value", "reset_at"}) or not {"label", "value"}.issubset(item):
                raise AppDockError("provider progress item is invalid")
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                raise AppDockError("provider progress value is invalid") from None
            if not 0 <= value <= 1:
                raise AppDockError("provider progress value is invalid")
            reset_at = item.get("reset_at", "")
            if reset_at:
                reset_at = _bounded_text(reset_at, "progress reset value", maximum=80)
            normalized_progress.append(
                {
                    "label": _bounded_text(item.get("label"), "progress label", maximum=40),
                    "value": value,
                    "reset_at": reset_at,
                }
            )
        result["progress"] = normalized_progress
    return result


class ExtensionManager:
    def __init__(self, config: AppDockConfig):
        self.config = config
        self._lock = threading.RLock()
        self._active = ExtensionConfig()
        self._last_config_signature: tuple[str, tuple[str, ...] | None] | None = None
        self._config_error = ""
        self._provider_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

    def _disable(self, error: str, signature: tuple[str, tuple[str, ...] | None] | None) -> ExtensionConfig:
        self._active = ExtensionConfig()
        self._last_config_signature = signature
        self._config_error = error
        self._provider_cache.clear()
        return self._active

    def _load_config(self, valid_app_ids: Iterable[str] | None = None) -> ExtensionConfig:
        path = self.config.extension_config_path
        known = tuple(sorted(set(valid_app_ids))) if valid_app_ids is not None else None
        signature: tuple[str, tuple[str, ...] | None] | None = None
        try:
            stat_result = path.stat()
            if not path.is_file() or path.is_symlink() or _is_link_or_reparse(path) or stat_result.st_size > MAX_EXTENSION_CONFIG_BYTES:
                raise AppDockError("private extension configuration is invalid")
            payload = path.read_bytes()
            if len(payload) > MAX_EXTENSION_CONFIG_BYTES:
                raise AppDockError("private extension configuration is invalid")
            signature = (hashlib.sha256(payload).hexdigest(), known)
            if signature == self._last_config_signature:
                return self._active
            raw = _loads_strict_json(payload.decode("utf-8"))
            parsed = parse_extension_config(raw)
            if known is not None and not parsed.hidden_app_ids.issubset(set(known)):
                raise AppDockError("visibility configuration references an unknown registration")
            self._active = parsed
            self._last_config_signature = signature
            self._config_error = ""
            self._provider_cache.clear()
            return parsed
        except FileNotFoundError:
            return self._disable("", None)
        except (OSError, UnicodeDecodeError, AppDockError):
            return self._disable(
                "Private extensions are unavailable because their configuration is invalid.",
                signature,
            )

    def hidden_app_ids(self, valid_app_ids: Iterable[str] | None = None) -> frozenset[str]:
        with self._lock:
            return self._load_config(valid_app_ids).hidden_app_ids

    def _fetch_provider(self, spec: ProviderSpec) -> dict[str, Any]:
        key = (spec.provider_id, spec.url, spec.connect_timeout, spec.read_timeout, spec.cache_seconds)
        now = time.monotonic()
        cached = self._provider_cache.get(key)
        if cached and cached[0] >= now:
            return cached[1]
        parsed = urllib.parse.urlsplit(spec.url)
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=spec.connect_timeout)
        try:
            connection.connect()
            if connection.sock is None:
                raise AppDockError("provider connection failed")
            connection.sock.settimeout(spec.read_timeout)
            connection.request("GET", target, headers={"Accept": "application/json", "User-Agent": f"AppDock/{CURRENT_VERSION}"})
            response = connection.getresponse()
            if 300 <= response.status < 400 or response.getheader("Location"):
                raise AppDockError("provider redirects are not allowed")
            if response.status != 200:
                raise AppDockError("provider returned an unavailable status")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise AppDockError("provider response must be JSON")
            content_length = response.getheader("Content-Length")
            if content_length is not None and int(content_length) > MAX_PROVIDER_RESPONSE_BYTES:
                raise AppDockError("provider response is too large")
            body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
                raise AppDockError("provider response is too large")
            raw = _loads_strict_json(body.decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"schema_version", "widgets"} or raw.get("schema_version") != 1:
                raise AppDockError("provider response schema is invalid")
            if not isinstance(raw.get("widgets"), dict) or _json_depth(raw) > 5:
                raise AppDockError("provider response is unbounded")
            payload = raw
        except (OSError, TimeoutError, ValueError, UnicodeDecodeError, http.client.HTTPException, AppDockError) as exc:
            payload = {"error": str(exc)}
        finally:
            connection.close()
        self._provider_cache[key] = (now + max(1.0, spec.cache_seconds), payload)
        return payload

    def snapshot(self, valid_app_ids: Iterable[str] | None = None) -> dict[str, Any]:
        with self._lock:
            config = self._load_config(valid_app_ids)
            providers = {item.provider_id: item for item in config.providers}
            provider_payloads = {provider_id: self._fetch_provider(spec) for provider_id, spec in providers.items()}
            widgets: list[dict[str, Any]] = []
            for spec in config.widgets:
                payload = provider_payloads.get(spec.provider_id, {})
                try:
                    if "error" in payload:
                        raise AppDockError("provider unavailable")
                    raw_widget = payload.get("widgets", {}).get(spec.widget_id)
                    widgets.append(_normalize_provider_widget(raw_widget, spec))
                except AppDockError:
                    widgets.append(
                        {
                            "id": spec.widget_id,
                            "type": spec.widget_type,
                            "title": spec.title,
                            "status": "unavailable",
                            "timestamp": "",
                            "drill_down_url": spec.drill_down_url,
                            "metrics": [] if spec.widget_type == "metrics" else None,
                            "progress": [] if spec.widget_type == "progress" else None,
                        }
                    )
            for widget in widgets:
                if widget.get("metrics") is None:
                    widget.pop("metrics", None)
                if widget.get("progress") is None:
                    widget.pop("progress", None)
            return {"enabled": bool(config.widgets), "widgets": widgets, "error": self._config_error}


def _safe_package_file(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or "\\" in relative or "\x00" in relative:
        raise AppDockError("private package path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise AppDockError("private package path is invalid")
    path = (root / Path(*pure.parts)).resolve()
    if not _inside(path, root) or not path.is_file() or path.is_symlink() or _is_link_or_reparse(path):
        raise AppDockError("private package file is missing or unsafe")
    return path


def _read_regular_single_link(path: Path, maximum: int) -> bytes:
    """Read an existing file while binding its bytes to one stable inode."""
    path = Path(path).expanduser().absolute()
    _assert_no_link_or_reparse_ancestor(path)
    try:
        observed = path.lstat()
    except OSError as exc:
        raise AppDockError("file is missing or unsafe") from exc
    if _is_link_or_reparse(path) or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise AppDockError("file is not a regular single-link file")
    if observed.st_size > maximum:
        raise AppDockError("file is too large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AppDockError("file could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (observed.st_dev, observed.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != identity
            or opened.st_size > maximum
        ):
            raise AppDockError("file changed while opening")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise AppDockError("file is too large")
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != identity
            or after.st_size != total
        ):
            raise AppDockError("file changed while reading")
        try:
            final = path.lstat()
        except OSError as exc:
            raise AppDockError("file changed after reading") from exc
        if (
            _is_link_or_reparse(path)
            or not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino) != identity
            or final.st_size != total
        ):
            raise AppDockError("file changed after reading")
        return b"".join(chunks)
    except OSError as exc:
        if isinstance(exc, AppDockError):
            raise
        raise AppDockError("file could not be read safely") from exc
    finally:
        os.close(descriptor)


def _read_bounded_json(path: Path, maximum: int = MAX_JSON_BYTES) -> Any:
    try:
        return _loads_strict_json(_read_regular_single_link(path, maximum).decode("utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise AppDockError("JSON file is invalid") from exc


def _update_startup_receipt_path(data: Path, token: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
        raise AppDockError("invalid update startup token")
    data = data.expanduser().absolute()
    runtime = data / "runtime"
    _assert_safe_directory_ancestors(data)
    _assert_safe_directory_ancestors(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    _assert_safe_directory_ancestors(runtime)
    return runtime / f"update-startup-{token}.json"


def _write_update_startup_handoff(data: str | Path, install: str | Path, token: str) -> Path:
    data_path = Path(data)
    receipt = _update_startup_receipt_path(data_path, token)
    _durable_write_json(receipt, {
        "schema_version": 1,
        "token": token,
        "owner_pid": os.getpid(),
        "install": str(Path(install).expanduser().absolute().resolve()),
        "data": str(data_path.expanduser().absolute().resolve()),
    })
    return receipt


def _process_exists(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _consume_update_startup_handoff(data: Path, install: Path, token: str) -> None:
    receipt_path = _update_startup_receipt_path(data, token)
    try:
        receipt = _read_bounded_json(receipt_path)
    except AppDockError as exc:
        raise AppDockError("update startup authorization is invalid") from exc
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema_version", "token", "owner_pid", "install", "data"}
        or receipt.get("schema_version") != 1
        or receipt.get("token") != token
        or not isinstance(receipt.get("owner_pid"), int)
        or not isinstance(receipt.get("install"), str)
        or not isinstance(receipt.get("data"), str)
        or not _process_exists(receipt["owner_pid"])
    ):
        raise AppDockError("update startup authorization is invalid")
    if Path(receipt["install"]).expanduser().absolute().resolve() != install.expanduser().absolute().resolve():
        raise AppDockError("update startup authorization is bound to another installation")
    if Path(receipt["data"]).expanduser().absolute().resolve() != data.expanduser().absolute().resolve():
        raise AppDockError("update startup authorization is bound to another data root")
    receipt_path.unlink()


def _private_package_files(root: Path) -> set[str]:
    files: set[str] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        if _is_link_or_reparse(current_path):
            raise AppDockError("private package contains a symlink or reparse point")
        for name in directories:
            if _is_link_or_reparse(current_path / name):
                raise AppDockError("private package contains a symlink or reparse point")
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or _is_link_or_reparse(path) or not path.is_file():
                raise AppDockError("private package contains a non-file or unsafe member")
            relative = path.relative_to(root).as_posix()
            _safe_package_file(root, relative)
            if relative in files:
                raise AppDockError("private package contains duplicate members")
            files.add(relative)
    return files


def _verify_private_package_manifest(root: Path) -> dict[str, Any]:
    manifest_path = _safe_package_file(root, PRIVATE_PACKAGE_HASH_MANIFEST)
    manifest = _read_bounded_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "package", "migration_digest", "files"}
        or manifest.get("schema_version") != 1
        or manifest.get("package") != "AppDock private integration package"
        or not isinstance(manifest.get("migration_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest["migration_digest"])
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise AppDockError("private package hash manifest is invalid")
    declared: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise AppDockError("private package hash manifest entry is invalid")
        relative, digest, size = item["path"], item["sha256"], item["size"]
        if relative in declared or relative == PRIVATE_PACKAGE_HASH_MANIFEST:
            raise AppDockError("private package hash manifest contains duplicate or self entries")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AppDockError("private package hash manifest checksum is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_UPDATE_UNCOMPRESSED_BYTES:
            raise AppDockError("private package hash manifest size is invalid")
        path = _safe_package_file(root, relative)
        payload = path.read_bytes()
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise AppDockError("private package file does not match its declared size and checksum")
        declared[relative] = item
    actual = _private_package_files(root)
    if actual != {*declared, PRIVATE_PACKAGE_HASH_MANIFEST}:
        raise AppDockError("private package member set does not exactly match its hash manifest")
    identity = {
        relative: hashlib.sha256(_safe_package_file(root, relative).read_bytes()).hexdigest()
        for relative in sorted(actual)
    }
    return {"manifest": manifest, "declared": declared, "identity": identity}


def preview_private_package(package_root: str | Path) -> dict[str, Any]:
    root = Path(package_root).expanduser().resolve()
    if not root.is_dir() or root.is_symlink() or _is_link_or_reparse(root):
        raise AppDockError("private package directory is invalid")
    package_integrity = _verify_private_package_manifest(root)
    package_path = _safe_package_file(root, PRIVATE_PACKAGE_MANIFEST)
    package = _read_bounded_json(package_path)
    if not isinstance(package, dict):
        raise AppDockError("private package manifest is invalid")
    schema_version = package.get("schema_version")
    if schema_version == 1:
        required = {"schema_version", "registrations", "order_path", "extension_config_path"}
        if set(package) != required:
            raise AppDockError("private package manifest is invalid")
        declared_path_flavor: str | None = None
    elif schema_version == 2:
        required = {"schema_version", "path_flavor", "registrations", "order_path", "extension_config_path"}
        if set(package) != required or package.get("path_flavor") != "windows":
            raise AppDockError("private package manifest is invalid")
        declared_path_flavor = "windows"
    else:
        raise AppDockError("private package manifest is invalid")

    registration_paths = package.get("registrations")
    if not isinstance(registration_paths, list) or not registration_paths or len(registration_paths) > 256:
        raise AppDockError("private package registrations are invalid")
    if any(not isinstance(item, str) for item in registration_paths) or len(set(registration_paths)) != len(registration_paths):
        raise AppDockError("private package contains duplicate registration paths")

    raw_registrations: list[tuple[Path, dict[str, Any]]] = []
    inferred_flavors: set[str] = set()
    for relative in registration_paths:
        path = _safe_package_file(root, relative)
        raw = _read_bounded_json(path)
        if not isinstance(raw, dict):
            raise AppDockError("private registration must preserve an absolute external directory")
        if declared_path_flavor is None:
            flavor = _private_path_flavor(raw.get("directory"))
            if flavor is None:
                raise AppDockError("private registration must preserve an absolute external directory")
            inferred_flavors.add(flavor)
        raw_registrations.append((path, raw))

    if declared_path_flavor is None:
        if len(inferred_flavors) != 1:
            raise AppDockError("private package contains mixed or ambiguous external path forms")
        path_flavor = next(iter(inferred_flavors))
    else:
        path_flavor = declared_path_flavor

    registrations: dict[str, dict[str, Any]] = {}
    for path, raw in raw_registrations:
        normalized = normalize_private_registration(raw, manifest_dir=path.parent, path_flavor=path_flavor)
        app_id = normalized["id"]
        if app_id in registrations:
            raise AppDockError("private package contains duplicate registrations")
        registrations[app_id] = normalized

    order_path = _safe_package_file(root, package.get("order_path"))
    order = _read_bounded_json(order_path)
    if (
        not isinstance(order, list)
        or len(order) != len(registrations)
        or any(not isinstance(item, str) or item not in registrations for item in order)
        or len(set(order)) != len(order)
    ):
        raise AppDockError("private package order must contain each registration exactly once")
    extension_path = _safe_package_file(root, package.get("extension_config_path"))
    extension_raw = _read_bounded_json(extension_path, MAX_EXTENSION_CONFIG_BYTES)
    extension = parse_extension_config(extension_raw)
    if not extension.hidden_app_ids.issubset(registrations):
        raise AppDockError("visibility configuration references an unknown registration")
    normalized_extensions = _normalized_extension_payload(extension)
    normalized: dict[str, Any] = {
        "schema_version": schema_version,
        "registrations": {app_id: registrations[app_id] for app_id in sorted(registrations)},
        "order": order,
        "extensions": normalized_extensions,
    }
    if schema_version == 2:
        normalized["path_flavor"] = path_flavor
        # Preserve a single documented field order in human-readable evidence while
        # canonical hashing remains key-sorted.
        normalized = {
            "schema_version": schema_version,
            "path_flavor": path_flavor,
            "registrations": normalized["registrations"],
            "order": order,
            "extensions": normalized_extensions,
        }
    digest = _digest(normalized)
    if digest != package_integrity["manifest"]["migration_digest"]:
        raise AppDockError("private package migration digest does not match its hash manifest")
    return {
        "schema_version": schema_version,
        "path_flavor": path_flavor,
        "registration_count": len(registrations),
        "visible_count": len(registrations) - len(extension.hidden_app_ids),
        "order_count": len(order),
        "digest": digest,
        "normalized": normalized,
        "package_identity": package_integrity["identity"],
    }


def _migration_targets(preview: dict[str, Any], config: AppDockConfig) -> dict[Path, bytes]:
    normalized = preview["normalized"]
    path_flavor = preview.get("path_flavor")
    if path_flavor not in {"windows", "posix"}:
        raise AppDockError("private package path flavor is unsupported")
    targets: dict[Path, bytes] = {}
    for app_id, manifest in normalized["registrations"].items():
        persisted = dict(manifest)
        persisted["_appdock_private_import"] = True
        persisted["path_flavor"] = path_flavor
        targets[config.registry_root / app_id / MANIFEST_NAME] = json.dumps(persisted, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    targets[config.order_path] = json.dumps(normalized["order"], indent=2).encode("utf-8") + b"\n"
    targets[config.extension_config_path] = json.dumps(normalized["extensions"], indent=2, sort_keys=True).encode("utf-8") + b"\n"
    return targets


def _migration_transaction_path(config: AppDockConfig, operation_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise AppDockError("migration transaction identity is invalid")
    path = (config.migration_root / "transactions" / operation_id).resolve()
    if not _inside(path, config.migration_root):
        raise AppDockError("migration transaction path is unsafe")
    return path


def _migration_journal(path: Path) -> dict[str, Any]:
    journal = _read_bounded_json(path / "transaction.json")
    if not isinstance(journal, dict) or journal.get("schema_version") != 2:
        raise AppDockError("migration transaction is invalid")
    required = {"schema_version", "operation_id", "digest", "phase", "recovery", "entries"}
    if set(journal) != required or journal["phase"] not in {"prepared", "applying", "committed", "complete", "rolled_back"}:
        raise AppDockError("migration transaction is invalid")
    if journal["recovery"] not in {"restore-old", "finish-new"} or not isinstance(journal["entries"], list):
        raise AppDockError("migration transaction is invalid")
    return journal


def _migration_entry_paths(entry: dict[str, Any], tx_root: Path, config: AppDockConfig) -> tuple[Path, Path, Path]:
    required = {"target", "existed", "old_sha256", "new_sha256", "backup", "staged"}
    if not isinstance(entry, dict) or set(entry) != required or not isinstance(entry["existed"], bool):
        raise AppDockError("migration transaction entry is invalid")
    target = (config.data_root / Path(*PurePosixPath(entry["target"]).parts)).resolve()
    backup = (tx_root / Path(*PurePosixPath(entry["backup"]).parts)).resolve()
    staged = (tx_root / Path(*PurePosixPath(entry["staged"]).parts)).resolve()
    if not _inside(target, config.data_root) or not _inside(backup, tx_root) or not _inside(staged, tx_root):
        raise AppDockError("migration transaction path escapes its root")
    return target, backup, staged


def _set_migration_phase(tx_root: Path, journal: dict[str, Any], phase: str, recovery: str) -> None:
    journal["phase"] = phase
    journal["recovery"] = recovery
    _durable_write_json(tx_root / "transaction.json", journal)


def _recover_one_migration(tx_root: Path, config: AppDockConfig, phase_hook: Callable[[str], None] | None = None) -> str:
    journal = _migration_journal(tx_root)
    if journal["phase"] in {"complete", "rolled_back"}:
        return journal["phase"]
    finish_new = journal["phase"] == "committed" or journal["recovery"] == "finish-new"
    direction = "finish-new" if finish_new else "restore-old"
    for entry in journal["entries"]:
        target, backup, staged = _migration_entry_paths(entry, tx_root, config)
        if phase_hook:
            phase_hook(f"recovery:{direction}:{entry['target']}")
        if finish_new:
            if not staged.is_file() or hashlib.sha256(staged.read_bytes()).hexdigest() != entry["new_sha256"]:
                raise AppDockError("migration staged recovery payload is missing or invalid")
            _durable_copy(staged, target)
        elif entry["existed"]:
            if not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != entry["old_sha256"]:
                raise AppDockError("migration backup is missing or invalid")
            _durable_copy(backup, target)
        else:
            target.unlink(missing_ok=True)
            _fsync_directory(target.parent)
    final_phase = "complete" if finish_new else "rolled_back"
    _set_migration_phase(tx_root, journal, final_phase, direction)
    return final_phase


def recover_private_migrations(config: AppDockConfig, *, phase_hook: Callable[[str], None] | None = None) -> list[str]:
    config.ensure()
    transactions_root = config.migration_root / "transactions"
    transactions_root.mkdir(parents=True, exist_ok=True)
    recovered: list[str] = []
    for tx_root in sorted(transactions_root.iterdir(), key=lambda item: item.name):
        if not tx_root.is_dir() or tx_root.is_symlink() or _is_link_or_reparse(tx_root):
            raise AppDockError("migration transaction root is unsafe")
        journal = _migration_journal(tx_root)
        if journal["phase"] not in {"complete", "rolled_back"}:
            recovered.append(_recover_one_migration(tx_root, config, phase_hook))
    return recovered


def _migration_receipt(tx_root: Path, journal: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "digest": journal["digest"],
        "transaction_root": str(tx_root),
        "entries": journal["entries"],
    }


def import_private_package(
    package_root: str | Path,
    config: AppDockConfig,
    *,
    expected_digest: str | None = None,
    failure_hook: Callable[[str], None] | None = None,
    phase_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    config.ensure()
    recover_private_migrations(config)
    preview = preview_private_package(package_root)
    if not isinstance(expected_digest, str) or not secrets.compare_digest(expected_digest, preview["digest"]):
        raise AppDockError("caller-confirmed migration digest is stale or invalid")
    targets = _migration_targets(preview, config)
    if all(path.is_file() and path.read_bytes() == payload for path, payload in targets.items()):
        return {"changed": False, "digest": preview["digest"], "receipt": "", "registration_count": preview["registration_count"]}
    operation_id = uuid.uuid4().hex
    tx_root = _migration_transaction_path(config, operation_id)
    staged_root = tx_root / "staged"
    backup_root = tx_root / "backup"
    staged_root.mkdir(parents=True, exist_ok=False)
    backup_root.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []
    ordered_targets = sorted(targets.items(), key=lambda item: str(item[0]))
    for index, (target, payload) in enumerate(ordered_targets):
        if not _inside(target.resolve(), config.data_root):
            raise AppDockError("migration target escapes the AppDock data root")
        relative = target.resolve().relative_to(config.data_root.resolve()).as_posix()
        staged_relative = f"staged/{index:04d}.json"
        backup_relative = f"backup/{index:04d}.backup"
        staged = tx_root / staged_relative
        _durable_write_bytes(staged, payload)
        existed = target.is_file()
        old_sha = ""
        if existed:
            old_payload = target.read_bytes()
            old_sha = hashlib.sha256(old_payload).hexdigest()
            _durable_write_bytes(tx_root / backup_relative, old_payload)
        entries.append({
            "target": relative,
            "existed": existed,
            "old_sha256": old_sha,
            "new_sha256": hashlib.sha256(payload).hexdigest(),
            "backup": backup_relative,
            "staged": staged_relative,
        })
    journal = {
        "schema_version": 2,
        "operation_id": operation_id,
        "digest": preview["digest"],
        "phase": "prepared",
        "recovery": "restore-old",
        "entries": entries,
    }
    _durable_write_json(tx_root / "transaction.json", journal)
    if phase_hook:
        phase_hook("after-journal")
        phase_hook("after-staging")
    try:
        fresh = preview_private_package(package_root)
        if fresh["digest"] != preview["digest"] or fresh["package_identity"] != preview["package_identity"]:
            raise AppDockError("private package changed after preview")
        for entry in entries:
            _target, _backup, staged = _migration_entry_paths(entry, tx_root, config)
            if hashlib.sha256(staged.read_bytes()).hexdigest() != entry["new_sha256"]:
                raise AppDockError("staged migration payload changed before write")
        _set_migration_phase(tx_root, journal, "applying", "restore-old")
        for entry in entries:
            target, _backup, staged = _migration_entry_paths(entry, tx_root, config)
            if failure_hook:
                failure_hook(entry["target"])
            if phase_hook:
                phase_hook(f"before-replace:{entry['target']}")
            _durable_copy(staged, target)
            if phase_hook:
                phase_hook(f"after-replace:{entry['target']}")
        for entry in entries:
            target, _backup, _staged = _migration_entry_paths(entry, tx_root, config)
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != entry["new_sha256"]:
                raise AppDockError("migration target verification failed")
        if phase_hook:
            phase_hook("before-commit")
        _set_migration_phase(tx_root, journal, "committed", "finish-new")
        if phase_hook:
            phase_hook("after-commit")
        receipt = _migration_receipt(tx_root, journal)
        receipt_path = tx_root / "receipt.json"
        _durable_write_json(receipt_path, receipt)
        _set_migration_phase(tx_root, journal, "complete", "finish-new")
        return {
            "changed": True,
            "digest": preview["digest"],
            "receipt": str(receipt_path),
            "registration_count": preview["registration_count"],
        }
    except BaseException:
        _recover_one_migration(tx_root, config)
        raise


def rollback_private_package(receipt_path: str | Path, config: AppDockConfig) -> dict[str, Any]:
    recover_private_migrations(config)
    path = Path(receipt_path).expanduser().resolve()
    if not path.is_file() or path.is_symlink() or not _inside(path, config.migration_root / "transactions"):
        raise AppDockError("migration receipt is invalid")
    receipt = _read_bounded_json(path)
    if not isinstance(receipt, dict) or set(receipt) != {"schema_version", "digest", "transaction_root", "entries"} or receipt.get("schema_version") != 2:
        raise AppDockError("migration receipt is invalid")
    tx_root = Path(receipt["transaction_root"]).resolve()
    if not _inside(tx_root, config.migration_root / "transactions") or path.parent != tx_root:
        raise AppDockError("migration receipt is invalid")
    journal = _migration_journal(tx_root)
    if journal["phase"] != "complete" or journal["entries"] != receipt["entries"]:
        raise AppDockError("migration receipt is not final")
    for entry in journal["entries"]:
        target, _backup, _staged = _migration_entry_paths(entry, tx_root, config)
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != entry["new_sha256"]:
            raise AppDockError("migration rollback refused because imported state changed")
    journal["phase"] = "applying"
    journal["recovery"] = "restore-old"
    _durable_write_json(tx_root / "transaction.json", journal)
    _recover_one_migration(tx_root, config)
    return {"rolled_back": True, "digest": receipt.get("digest", "")}


class AppManager:
    def __init__(self, apps_root: Path | None = None, config: AppDockConfig | None = None, extensions: ExtensionManager | None = None):
        self.config = config or AppDockConfig.from_environment()
        self.legacy_direct_root = apps_root is not None
        if not self.legacy_direct_root:
            recover_private_migrations(self.config)
        self.apps_root = Path(apps_root).expanduser().resolve() if apps_root is not None else self.config.registry_root
        self.order_path = (self.apps_root / ".appdock-order.json") if self.legacy_direct_root else self.config.order_path
        self.extensions = extensions or ExtensionManager(self.config)
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
                        cwd = directory if relative_cwd == "." else directory + "\\" + relative_cwd
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
        specs = self.discover()
        hidden = self.extensions.hidden_app_ids(specs) if not self.legacy_direct_root else frozenset()
        return [self.status(spec) for spec in self._ordered_specs(specs) if spec.app_id not in hidden]

    def move(self, app_id: str, direction: str) -> dict[str, Any]:
        specs = self.discover()
        if app_id not in specs:
            raise KeyError(app_id)
        ids = [spec.app_id for spec in self._ordered_specs(specs)]
        hidden = self.extensions.hidden_app_ids(specs) if not self.legacy_direct_root else frozenset()
        movable = [item for item in ids if item not in hidden]
        index = movable.index(app_id)
        target = index - 1 if direction == "up" else index + 1 if direction == "down" else index
        if 0 <= target < len(movable):
            left, right = ids.index(movable[index]), ids.index(movable[target])
            ids[left], ids[right] = ids[right], ids[left]
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
        runtime_cwd = _native_runtime_cwd(spec)
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
                        cwd=runtime_cwd,
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
        _native_runtime_cwd(spec)
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


def _update_channel_path(config: AppDockConfig) -> Path:
    return config.data_root / "update-settings.json"


def read_update_channel(config: AppDockConfig) -> str:
    path = _update_channel_path(config)
    if not path.exists():
        return DEFAULT_UPDATE_CHANNEL
    try:
        raw = _read_bounded_json(path)
        if (
            isinstance(raw, dict)
            and set(raw) == {"schema_version", "channel"}
            and raw.get("schema_version") == 1
            and raw.get("channel") in UPDATE_CHANNELS
        ):
            return str(raw["channel"])
    except (AppDockError, OSError, ValueError, TypeError):
        pass
    return DEFAULT_UPDATE_CHANNEL


def write_update_channel(config: AppDockConfig, channel: str) -> str:
    if channel not in UPDATE_CHANNELS:
        raise AppDockError("update channel must be stable or beta")
    config.ensure()
    _durable_write_json(_update_channel_path(config), {"schema_version": 1, "channel": channel})
    return channel


def parse_release(payload: dict[str, Any], *, channel: str = DEFAULT_UPDATE_CHANNEL) -> dict[str, Any]:
    if channel not in UPDATE_CHANNELS:
        raise AppDockError("update channel is invalid")
    if not isinstance(payload, dict):
        raise AppDockError("release response is invalid")
    if payload.get("draft"):
        raise AppDockError("draft releases are not eligible for updates")
    prerelease = payload.get("prerelease") is True
    tag_raw = str(payload.get("tag_name") or "")
    tag = tag_raw[1:] if tag_raw.startswith("v") else tag_raw
    match = SEMVER_RE.fullmatch(tag)
    if match is None:
        raise AppDockError("release tag is not a semantic version")
    if channel == "stable":
        if prerelease or match.group(4):
            raise AppDockError("release is not a stable public release")
    else:
        if not prerelease or BETA_TAG_RE.fullmatch(tag_raw) is None:
            raise AppDockError("release is not an AppDock Beta prerelease")
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
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _fetch_json(self, endpoint: str) -> Any:
        request = urllib.request.Request(endpoint, headers={"Accept": "application/vnd.github+json", "User-Agent": "AppDock"})
        try:
            response = self.opener(request, timeout=5)
        except TypeError:
            response = self.opener(request)
        if hasattr(response, "__enter__"):
            with response as stream:
                return json.loads(stream.read().decode("utf-8"))
        return json.loads(response.read().decode("utf-8"))

    def check(self, channel: str = DEFAULT_UPDATE_CHANNEL) -> dict[str, Any]:
        if channel not in UPDATE_CHANNELS:
            raise AppDockError("update channel is invalid")
        now = time.monotonic()
        cached = self._cache.get(channel)
        if cached and now - cached[0] < self.cache_ttl:
            return cached[1]
        try:
            if channel == "stable":
                endpoint = f"https://api.github.com/repos/{self.repository}/releases/latest"
                release = parse_release(self._fetch_json(endpoint), channel="stable")
            else:
                endpoint = f"https://api.github.com/repos/{self.repository}/releases?per_page=100"
                payload = self._fetch_json(endpoint)
                if not isinstance(payload, list):
                    raise AppDockError("release response is invalid")
                candidates: list[dict[str, Any]] = []
                for item in payload:
                    try:
                        candidates.append(parse_release(item, channel="beta"))
                    except (AppDockError, ValueError, TypeError):
                        continue
                if candidates:
                    release = candidates[0]
                    for candidate in candidates[1:]:
                        if compare_semver(candidate["version"], release["version"]) > 0:
                            release = candidate
                else:
                    release = {"version": "", "latest": "", "release_url": "", "notes": "", "assets": []}
            release["channel"] = channel
            release["current"] = self.current
            release["available"] = bool(release["version"])
            release["update_available"] = bool(release["version"]) and compare_semver(release["version"], self.current) > 0
            self._cache[channel] = (now, release)
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
    if info.is_dir() or normalized.endswith("/"):
        raise AppDockError("update ZIP contains an explicit directory entry")
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
    mode = stat.S_IFMT(info.external_attr >> 16)
    if not info.is_dir() and mode not in {0, stat.S_IFREG}:
        raise AppDockError("update ZIP contains a nonregular member")


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


def _release_path(root: Path, relative: str, *, require_file: bool = True) -> Path:
    root = root.expanduser().absolute()
    _assert_safe_directory_ancestors(root)
    target_lexical = root / Path(*PurePosixPath(relative).parts)
    _assert_no_link_or_reparse_ancestor(target_lexical)
    if _is_link_or_reparse(target_lexical):
        raise AppDockError("release inventory member is a symlink or reparse point")
    target = target_lexical.resolve()
    if not _inside(target, root):
        raise AppDockError("release inventory path escapes its root")
    if not target_lexical.exists():
        if require_file:
            raise AppDockError("release inventory member is missing")
        return target_lexical
    metadata = target_lexical.lstat()
    if _is_link_or_reparse(target_lexical) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise AppDockError("release inventory member is not a regular single-link file")
    return target_lexical


def _load_release_inventory(root: Path, *, complete: bool) -> dict[str, str]:
    root = root.expanduser().absolute()
    _assert_safe_directory_ancestors(root)
    if _is_link_or_reparse(root):
        raise AppDockError("release tree root is unsafe")
    manifest_path = root / RELEASE_MANIFEST_NAME
    if not manifest_path.exists():
        if complete:
            raise AppDockError("release inventory is missing")
        return {}
    inventory = _parse_release_manifest(_read_regular_single_link(manifest_path, MAX_JSON_BYTES))
    if complete:
        actual: set[str] = set()
        for path in root.rglob("*"):
            if path.is_symlink() or _is_link_or_reparse(path):
                raise AppDockError("release tree contains a symlink or reparse point")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        expected = {*inventory, RELEASE_MANIFEST_NAME}
        if actual != expected:
            raise AppDockError("release tree does not match its inventory")
        for relative, expected_digest in inventory.items():
            target = _release_path(root, relative)
            actual_digest = hashlib.sha256(_read_regular_single_link(target, MAX_UPDATE_UNCOMPRESSED_BYTES)).hexdigest()
            if actual_digest != expected_digest:
                raise AppDockError("release file checksum does not match its inventory")
    else:
        for relative, expected_digest in inventory.items():
            target = _release_path(root, relative)
            actual_digest = hashlib.sha256(_read_regular_single_link(target, MAX_UPDATE_UNCOMPRESSED_BYTES)).hexdigest()
            if actual_digest != expected_digest:
                raise AppDockError("release file checksum does not match its inventory")
    return inventory


def _staged_inventory_records(staged: Path, inventory: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "path": relative,
            "sha256": digest,
            "size": len(_read_regular_single_link(_release_path(staged, relative), MAX_UPDATE_UNCOMPRESSED_BYTES)),
        }
        for relative, digest in sorted(inventory.items())
    ]


def _staged_identity(staged: Path, *, zip_sha256: str | None = None, complete: bool = True) -> dict[str, Any]:
    inventory = _load_release_inventory(staged, complete=complete)
    records = _staged_inventory_records(staged, inventory)
    helper = _release_path(staged, "scripts/update_helper.py")
    helper_digest = hashlib.sha256(_read_regular_single_link(helper, MAX_UPDATE_UNCOMPRESSED_BYTES)).hexdigest()
    identity = {
        "zip_sha256": zip_sha256,
        "inventory_sha256": hashlib.sha256(_canonical_json(records)).hexdigest(),
        "helper_sha256": helper_digest,
        "inventory": records,
    }
    if zip_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", zip_sha256):
        raise AppDockError("staged update ZIP identity is invalid")
    return identity


def _verify_identity_tree(root: Path, identity: dict[str, Any], *, complete: bool) -> dict[str, Any]:
    verified = _validate_staged_identity(identity, require_zip=identity.get("zip_sha256") is not None)
    actual = _staged_identity(root, zip_sha256=verified["zip_sha256"], complete=complete)
    if actual != verified:
        raise AppDockError("staged update identity does not match staged bytes")
    return actual


def _validate_staged_identity(identity: Any, *, require_zip: bool = True) -> dict[str, Any]:
    if not isinstance(identity, dict) or set(identity) != {"zip_sha256", "inventory_sha256", "helper_sha256", "inventory"}:
        raise AppDockError("staged update identity is invalid")
    zip_sha256 = identity["zip_sha256"]
    if zip_sha256 is not None and (not isinstance(zip_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", zip_sha256)):
        raise AppDockError("staged update ZIP identity is invalid")
    if require_zip and zip_sha256 is None:
        raise AppDockError("staged update ZIP identity is missing")
    for field_name in ("inventory_sha256", "helper_sha256"):
        if not isinstance(identity[field_name], str) or not re.fullmatch(r"[0-9a-f]{64}", identity[field_name]):
            raise AppDockError("staged update identity checksum is invalid")
    records = identity["inventory"]
    if not isinstance(records, list) or not records:
        raise AppDockError("staged update inventory identity is invalid")
    previous = ""
    normalized: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise AppDockError("staged update inventory identity is invalid")
        relative, digest, size = record["path"], record["sha256"], record["size"]
        if not isinstance(relative, str) or relative <= previous or "\\" in relative or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AppDockError("staged update inventory identity is invalid")
        try:
            _assert_zip_member(relative, zipfile.ZipInfo(relative))
        except AppDockError as exc:
            raise AppDockError("staged update inventory identity is invalid") from exc
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise AppDockError("staged update inventory identity is invalid")
        previous = relative
        normalized.append({"path": relative, "sha256": digest, "size": size})
    if hashlib.sha256(_canonical_json(normalized)).hexdigest() != identity["inventory_sha256"]:
        raise AppDockError("staged update inventory identity digest is invalid")
    return {"zip_sha256": zip_sha256, "inventory_sha256": identity["inventory_sha256"], "helper_sha256": identity["helper_sha256"], "inventory": normalized}


def _staged_record_digest(version: str, identity: dict[str, Any]) -> str:
    return _digest({"version": version, "identity": identity})


def _verify_staged_record(config: AppDockConfig, record: dict[str, Any], *, require_zip: bool = True) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise AppDockError("staged update receipt is invalid")
    allowed = {"version", "path", "digest", "identity"}
    if set(record) == allowed:
        pass
    elif set(record) == {"staged", *allowed} and record.get("staged") is True:
        pass
    else:
        raise AppDockError("staged update receipt is invalid")
    version = record["version"]
    raw_path = record["path"]
    digest = record["digest"]
    identity = record["identity"]
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version) or not isinstance(raw_path, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AppDockError("staged update receipt is invalid")
    normalized_identity = _validate_staged_identity(identity, require_zip=require_zip)
    staged = _validate_staged_path(config, version, raw_path)
    actual = _staged_identity(staged, zip_sha256=normalized_identity["zip_sha256"])
    if actual != normalized_identity or _staged_record_digest(version, actual) != digest:
        raise AppDockError("staged update identity does not match staged bytes")
    return {"staged": True, "version": version, "path": str(staged), "digest": digest, "identity": actual}


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


def _staged_receipt_path(config: AppDockConfig) -> Path:
    _assert_safe_directory_ancestors(config.data_root)
    _assert_safe_directory_ancestors(config.runtime_root)
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    _assert_safe_directory_ancestors(config.runtime_root)
    return config.runtime_root / "staged-update.json"


def _validate_staged_path(config: AppDockConfig, version: str, raw_path: str) -> Path:
    expected = _safe_version_child(config.updates_root, version)
    staged_lexical = Path(raw_path).expanduser().absolute()
    _assert_safe_directory_ancestors(staged_lexical)
    if _is_link_or_reparse(staged_lexical):
        raise AppDockError("staged update root is a symlink or reparse point")
    staged = staged_lexical.resolve()
    if staged != expected.resolve() or not staged.is_dir() or not _inside(staged, config.updates_root):
        raise AppDockError("staged update receipt path is invalid")
    _assert_tree_safe(staged_lexical, config.updates_root)
    _load_release_inventory(staged, complete=True)
    return staged


def _read_staged_receipt(config: AppDockConfig) -> dict[str, Any] | None:
    receipt_path = _staged_receipt_path(config)
    if not receipt_path.exists():
        return None
    _assert_no_link_or_reparse_ancestor(receipt_path)
    receipt_stat = receipt_path.stat()
    if _is_link_or_reparse(receipt_path) or not stat.S_ISREG(receipt_stat.st_mode) or receipt_stat.st_nlink != 1:
        raise AppDockError("staged update receipt is unsafe")
    receipt = _read_bounded_json(receipt_path)
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema_version", "version", "path", "digest", "identity"}
        or receipt.get("schema_version") != 2
    ):
        raise AppDockError("staged update receipt is invalid")
    return _verify_staged_record(config, {
        "version": receipt["version"],
        "path": receipt["path"],
        "digest": receipt["digest"],
        "identity": receipt["identity"],
    })


def _write_staged_receipt(config: AppDockConfig, staged: dict[str, Any]) -> None:
    if not isinstance(staged, dict) or set(staged) != {"staged", "version", "path", "digest", "identity"} or staged.get("staged") is not True:
        raise AppDockError("staged update receipt data is invalid")
    verified = _verify_staged_record(config, {
        "version": staged["version"],
        "path": staged["path"],
        "digest": staged["digest"],
        "identity": staged["identity"],
    })
    _durable_write_json(_staged_receipt_path(config), {
        "schema_version": 2,
        "version": verified["version"],
        "path": verified["path"],
        "digest": verified["digest"],
        "identity": verified["identity"],
    })


def _clear_staged_receipt(config: AppDockConfig | str | Path, expected_path: str | Path | None = None) -> None:
    if not isinstance(config, AppDockConfig):
        config = AppDockConfig.from_environment(data_dir=config)
    receipt_path = _staged_receipt_path(config)
    if not receipt_path.exists():
        return
    if expected_path is not None:
        try:
            receipt = _read_bounded_json(receipt_path)
            actual = Path(str(receipt.get("path") or "")).expanduser().absolute().resolve()
            expected = Path(expected_path).expanduser().absolute().resolve()
        except (AppDockError, OSError):
            return
        if actual != expected:
            return
    if _is_link_or_reparse(receipt_path):
        raise AppDockError("staged update receipt is unsafe")
    receipt_path.unlink(missing_ok=True)


def stage_update(release: dict[str, Any], config: AppDockConfig, *, opener: Callable[..., Any] | None = None, repository: str = DEFAULT_UPDATE_REPOSITORY) -> dict[str, Any]:
    version = str(release.get("version") or release.get("latest") or "")
    version = version[1:] if version.startswith("v") else version
    if compare_semver(version, CURRENT_VERSION) <= 0:
        raise AppDockError("release is not newer than the current version")
    assets = select_trusted_assets(release, repository)
    opener = opener or urllib.request.urlopen
    zip_bytes = _read_asset(opener, assets["appdock-windows.zip"]["url"], MAX_UPDATE_ASSET_BYTES)
    sums = _read_asset(opener, assets["SHA256SUMS.txt"]["url"], 1024 * 1024).decode("utf-8", "replace")
    zip_sha256 = verify_sha256(zip_bytes, sums)
    validate_zip(zip_bytes)
    config.ensure()
    if _staged_receipt_path(config).exists():
        _read_staged_receipt(config)
        raise AppDockError("an update is already staged")
    destination = _safe_version_child(config.updates_root, version)
    if destination.exists():
        # A completed directory without its durable receipt can only remain after
        # the staging owner exited between the atomic rename and receipt write.
        # Validate the exact managed tree before deleting it, then make the same
        # version safely retryable while the caller still owns the update lock.
        _validate_staged_path(config, version, str(destination))
        _remove_tree(destination)
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
    result_identity = _staged_identity(destination, zip_sha256=zip_sha256)
    result = {
        "staged": True,
        "version": version,
        "path": str(destination),
        "identity": result_identity,
        "digest": _staged_record_digest(version, result_identity),
    }
    try:
        _write_staged_receipt(config, result)
    except Exception:
        _remove_tree(destination, ignore_errors=True)
        raise
    return result


def _update_transactions_root(data: Path) -> Path:
    data = Path(data).expanduser().absolute()
    _assert_safe_directory_ancestors(data)
    root = data / "updates" / "transactions"
    _assert_safe_directory_ancestors(root)
    if not _inside(root, data):
        raise AppDockError("update transaction root is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    _assert_safe_directory_ancestors(root)
    if _is_link_or_reparse(root) or not root.is_dir():
        raise AppDockError("update transaction root is unsafe")
    return root.resolve()


def _update_journal(tx_root: Path) -> dict[str, Any]:
    journal = _read_bounded_json(tx_root / "transaction.json")
    required = {"schema_version", "operation_id", "install", "candidate", "backup", "old_exists", "phase", "recovery", "files", "preexisting", "identity"}
    if (
        not isinstance(journal, dict)
        or set(journal) != required
        or journal.get("schema_version") != 2
        or journal.get("phase") not in {"prepared", "swapping", "committed", "complete", "rolled_back"}
        or journal.get("recovery") not in {"restore-old", "finish-new"}
        or not isinstance(journal.get("old_exists"), bool)
        or not isinstance(journal.get("files"), list)
        or not isinstance(journal.get("preexisting"), list)
        or not all(isinstance(journal.get(field), str) and journal[field] for field in ("operation_id", "install", "candidate", "backup"))
    ):
        raise AppDockError("update transaction is invalid")
    if not isinstance(journal["identity"], dict):
        raise AppDockError("update transaction identity is invalid")
    if journal["phase"] in {"prepared", "swapping", "rolled_back"} and journal["recovery"] != "restore-old":
        raise AppDockError("update transaction recovery phase is invalid")
    if journal["phase"] in {"committed", "complete"} and journal["recovery"] != "finish-new":
        raise AppDockError("update transaction recovery phase is invalid")
    for field in ("files", "preexisting"):
        values = journal[field]
        if not all(isinstance(item, str) and item and "\\" not in item and not Path(item).is_absolute() and all(part not in {"", ".", ".."} for part in PurePosixPath(item).parts) for item in values):
            raise AppDockError("update transaction file list is invalid")
        if len(values) != len(set(values)):
            raise AppDockError("update transaction file list contains duplicates")
    if not set(journal["preexisting"]).issubset(set(journal["files"])):
        raise AppDockError("update transaction preexisting list is invalid")
    _validate_staged_identity(journal["identity"], require_zip=journal["identity"].get("zip_sha256") is not None)
    return journal


def _lexical_path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(Path(path).expanduser()))))


def _transaction_paths(install: Path, data: Path, operation_id: str) -> dict[str, Path]:
    if not UPDATE_OPERATION_ID_RE.fullmatch(operation_id):
        raise AppDockError("update transaction operation id is invalid")
    install, data = _validated_update_roots(install, data)
    transactions = _update_transactions_root(data)
    tx_root = transactions / operation_id
    candidate = install.parent / f".{install.name}.appdock-{operation_id}.candidate"
    backup = install.parent / f".{install.name}.appdock-{operation_id}.backup"
    evidence_backup = data / "updates" / "backups" / operation_id
    for path in (tx_root, candidate, backup, evidence_backup):
        _assert_no_link_or_reparse_ancestor(path)
    return {
        "tx_root": tx_root,
        "journal": tx_root / "transaction.json",
        "install": install,
        "candidate": candidate,
        "backup": backup,
        "evidence_backup": evidence_backup,
    }


def _validated_transaction_context(tx_root: Path, install: Path, data: Path) -> dict[str, Any]:
    """Validate all transaction paths before exposing any destructive operation."""
    install, data = _validated_update_roots(install, data)
    tx_lexical = Path(tx_root).expanduser().absolute()
    _assert_no_link_or_reparse_ancestor(tx_lexical)
    if _is_link_or_reparse(tx_lexical) or not tx_lexical.is_dir() or not UPDATE_OPERATION_ID_RE.fullmatch(tx_lexical.name):
        raise AppDockError("update transaction root is unsafe")
    derived = _transaction_paths(install, data, tx_lexical.name)
    if _lexical_path_key(tx_lexical) != _lexical_path_key(derived["tx_root"]):
        raise AppDockError("update transaction root does not match its operation")
    journal = _update_journal(tx_lexical)
    operation_id = journal["operation_id"]
    if operation_id != tx_lexical.name:
        raise AppDockError("update transaction operation does not match its root")
    if _lexical_path_key(journal["install"]) != _lexical_path_key(derived["install"]):
        raise AppDockError("update transaction installation path is invalid")
    if _lexical_path_key(journal["candidate"]) != _lexical_path_key(derived["candidate"]):
        raise AppDockError("update transaction candidate path is invalid")
    if _lexical_path_key(journal["backup"]) != _lexical_path_key(derived["backup"]):
        raise AppDockError("update transaction backup path is invalid")
    for name in ("candidate", "backup"):
        path = derived[name]
        _assert_no_link_or_reparse_ancestor(path)
        if path.exists() and (not path.is_dir() or _is_link_or_reparse(path)):
            raise AppDockError("update transaction path is not a directory")
    return {**derived, "journal_data": journal}


def _set_update_phase(tx_root: Path, journal: dict[str, Any], phase: str, recovery: str) -> None:
    journal["phase"] = phase
    journal["recovery"] = recovery
    _durable_write_json(tx_root / "transaction.json", journal)


def _copy_release_tree(source: Path, destination: Path) -> None:
    _assert_safe_directory_ancestors(destination.parent)
    if _is_link_or_reparse(destination):
        raise AppDockError("release tree destination is unsafe")
    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink() or _is_link_or_reparse(path):
            raise AppDockError("release tree contains a symlink or reparse point")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            _durable_copy(path, target)
        else:
            raise AppDockError("release tree contains a non-file member")
    _fsync_directory(destination)


def _safe_backup_copy(source: Path, destination: Path) -> None:
    """Snapshot a managed source before copying it into the transaction backup."""
    payload = _read_regular_single_link(source, MAX_UPDATE_UNCOMPRESSED_BYTES)
    _assert_safe_directory_ancestors(destination.parent)
    probe = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.backup-tmp")
    try:
        # Keep the existing copy2 failure seam while publishing only the
        # opened-handle-verified snapshot through the durable writer.
        shutil.copy2(source, probe)
    finally:
        probe.unlink(missing_ok=True)
    _durable_write_bytes(destination, payload)


LEGACY_RELEASE_TOP_LEVEL = {
    "appdock.py",
    "appdock.example.json",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
}
LEGACY_RELEASE_TOP_LEVEL_DIRS = {"appdock", "appdock_core", "static", "docs", "scripts", "templates"}


def _legacy_release_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return relative in LEGACY_RELEASE_TOP_LEVEL or (len(path.parts) > 1 and path.parts[0] in LEGACY_RELEASE_TOP_LEVEL_DIRS)


def _validate_installed_tree(install: Path) -> tuple[dict[str, str], list[str]]:
    if not install.exists():
        return {}, []
    if not install.is_dir() or install.is_symlink() or _is_link_or_reparse(install):
        raise AppDockError("managed installation root is unsafe")
    inventory = _load_release_inventory(install, complete=False)
    actual: set[str] = set()
    for path in install.rglob("*"):
        if path.is_symlink() or _is_link_or_reparse(path):
            raise AppDockError("managed installation contains a symlink or reparse point")
        if path.is_file():
            relative = path.relative_to(install).as_posix()
            _release_path(install, relative)
            actual.add(path.relative_to(install).as_posix())
    allowed_generated = {"run-appdock.cmd"}
    if not inventory:
        if not actual:
            return {}, []
        managed = actual - allowed_generated
        if "appdock.py" not in managed or not all(_legacy_release_path(relative) for relative in managed):
            raise AppDockError("managed installation has no trusted release inventory")
        inventory = {
            relative: hashlib.sha256(_read_regular_single_link(_release_path(install, relative), MAX_UPDATE_UNCOMPRESSED_BYTES)).hexdigest()
            for relative in sorted(managed)
        }
        return inventory, sorted(actual - managed)
    expected = {*inventory, RELEASE_MANIFEST_NAME}
    extras = actual - expected
    if not extras.issubset(allowed_generated):
        raise AppDockError("managed installation contains unexpected unowned files")
    for relative, digest in inventory.items():
        target = _release_path(install, relative)
        if hashlib.sha256(_read_regular_single_link(target, MAX_UPDATE_UNCOMPRESSED_BYTES)).hexdigest() != digest:
            raise AppDockError("managed installation file does not match its release inventory")
    return inventory, sorted(extras)


def _recover_one_update(tx_root: Path, *, install: Path, data: Path, phase_hook: Callable[[str], None] | None = None) -> str:
    context = _validated_transaction_context(tx_root, install, data)
    journal = context["journal_data"]
    if journal["phase"] in {"complete", "rolled_back"}:
        return journal["phase"]
    install = context["install"]
    candidate = context["candidate"]
    backup = context["backup"]
    finish_new = journal["phase"] == "committed" or journal["recovery"] == "finish-new"
    if phase_hook:
        phase_hook("recovery:finish-new" if finish_new else "recovery:restore-old")
    if finish_new:
        if not install.exists() and candidate.is_dir():
            _verify_identity_tree(candidate, journal["identity"], complete=False)
            os.replace(candidate, install)
            _fsync_directory(install.parent)
        if install.exists():
            _verify_identity_tree(install, journal["identity"], complete=False)
        _validate_installed_tree(install)
        if backup.exists():
            _remove_tree(backup)
        if candidate.exists():
            _remove_tree(candidate)
        _remove_tree(context["evidence_backup"], ignore_errors=True)
        _set_update_phase(context["tx_root"], journal, "complete", "finish-new")
        return "complete"
    if journal["old_exists"]:
        if backup.is_dir():
            if install.exists():
                _remove_tree(install)
            os.replace(backup, install)
            _fsync_directory(install.parent)
        elif not install.is_dir():
            raise AppDockError("update backup is missing during recovery")
        _validate_installed_tree(install)
    elif install.exists():
        _remove_tree(install)
    if candidate.exists():
        _verify_identity_tree(candidate, journal["identity"], complete=False)
        _remove_tree(candidate)
    _remove_tree(context["evidence_backup"], ignore_errors=True)
    _set_update_phase(context["tx_root"], journal, "rolled_back", "restore-old")
    return "rolled_back"


@_locked_transaction(0)
def recover_update_transactions(data_dir: str | Path, *, expected_install: str | Path | None = None, phase_hook: Callable[[str], None] | None = None) -> list[str]:
    if expected_install is None:
        raise AppDockError("expected installation root is required for update recovery")
    expected, data = _validated_update_roots(expected_install, data_dir)
    root = _update_transactions_root(data)
    recovered: list[str] = []
    for tx_root in sorted(root.iterdir(), key=lambda item: item.name):
        if not tx_root.is_dir() or tx_root.is_symlink() or _is_link_or_reparse(tx_root):
            raise AppDockError("update transaction root is unsafe")
        context = _validated_transaction_context(tx_root, expected, data)
        journal = context["journal_data"]
        if journal["phase"] not in {"complete", "rolled_back"}:
            recovered.append(_recover_one_update(tx_root, install=expected, data=data, phase_hook=phase_hook))
    return recovered


def _validated_update_roots(install_dir: str | Path, data_dir: str | Path) -> tuple[Path, Path]:
    install_lexical = Path(install_dir).expanduser().absolute()
    data_lexical = Path(data_dir).expanduser().absolute()
    for root in (install_lexical, data_lexical):
        _assert_no_link_or_reparse_ancestor(root)
    if install_lexical.exists() and not install_lexical.is_dir():
        raise AppDockError("installation root is not a directory")
    if data_lexical.exists() and not data_lexical.is_dir():
        raise AppDockError("update data root is not a directory")
    install, data = install_lexical.resolve(), data_lexical.resolve()
    if _inside(install, data) or _inside(data, install):
        raise AppDockError("installation and data roots must not overlap")
    return install, data


@_locked_transaction(2)
def apply_update(
    staged_dir: str | Path,
    install_dir: str | Path,
    data_dir: str | Path,
    *,
    restart: Callable[[], Any] | None = None,
    phase_hook: Callable[[str], None] | None = None,
    expected_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    staged_lexical = Path(staged_dir).expanduser().absolute()
    install, data = _validated_update_roots(install_dir, data_dir)
    if _is_link_or_reparse(staged_lexical):
        raise AppDockError("staged update root is a symlink or reparse point")
    staged = staged_lexical.resolve()
    if not staged.is_dir() or not _inside(staged, data / "updates"):
        raise AppDockError("staged update path is invalid")
    _assert_tree_safe(staged_lexical, data / "updates")
    recover_update_transactions(data, expected_install=install)
    if expected_identity is None:
        target_identity = _staged_identity(staged, complete=True)
    elif isinstance(expected_identity, dict) and "identity" in expected_identity:
        config = AppDockConfig.from_environment(data_dir=data)
        verified_record = _verify_staged_record(config, expected_identity)
        if Path(verified_record["path"]).resolve() != staged:
            raise AppDockError("staged update identity path does not match apply path")
        target_identity = verified_record["identity"]
    else:
        target_identity = _validate_staged_identity(expected_identity)
        _verify_identity_tree(staged, target_identity, complete=True)
    target_inventory = {record["path"]: record["sha256"] for record in target_identity["inventory"]}
    current_inventory, generated = _validate_installed_tree(install)
    target_files = {*target_inventory, RELEASE_MANIFEST_NAME}
    current_files = set(current_inventory)
    if (install / RELEASE_MANIFEST_NAME).exists():
        _release_path(install, RELEASE_MANIFEST_NAME)
        current_files.add(RELEASE_MANIFEST_NAME)
    affected = sorted(target_files | current_files)
    operation_id = uuid.uuid4().hex
    paths = _transaction_paths(install, data, operation_id)
    tx_root = paths["tx_root"]
    tx_root.mkdir(parents=True, exist_ok=False)
    _assert_no_link_or_reparse_ancestor(tx_root)
    evidence_backup = paths["evidence_backup"]
    try:
        if install.exists():
            for path in sorted(install.rglob("*")):
                if path.is_file():
                    destination = evidence_backup / path.relative_to(install)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _safe_backup_copy(path, destination)
    except Exception as exc:
        _remove_tree(evidence_backup, ignore_errors=True)
        _remove_tree(tx_root, ignore_errors=True)
        raise AppDockError("update backup failed; installation was not changed") from exc
    candidate = paths["candidate"]
    backup = paths["backup"]
    if candidate.exists() or backup.exists():
        raise AppDockError("update transaction paths already exist")
    try:
        _copy_release_tree(staged, candidate)
        for relative in generated:
            _durable_copy(_release_path(install, relative), _release_path(candidate, relative, require_file=False))
        _load_release_inventory(candidate, complete=False)
        candidate_inventory, candidate_generated = _validate_installed_tree(candidate)
        if candidate_inventory != target_inventory or candidate_generated != generated:
            raise AppDockError("candidate program tree verification failed")
        _verify_identity_tree(candidate, target_identity, complete=False)
    except Exception:
        _remove_tree(candidate, ignore_errors=True)
        _remove_tree(tx_root, ignore_errors=True)
        raise
    journal = {
        "schema_version": 2,
        "operation_id": operation_id,
        "install": str(install),
        "candidate": str(candidate),
        "backup": str(backup),
        "old_exists": install.exists(),
        "phase": "prepared",
        "recovery": "restore-old",
        "files": affected,
        "preexisting": sorted(current_files),
        "identity": target_identity,
    }
    _durable_write_json(tx_root / "transaction.json", journal)
    if phase_hook:
        phase_hook("prepared")
    result = {
        "applied": True,
        "backup": str(backup),
        "files": affected,
        "preexisting": sorted(current_files),
        "identity": target_identity,
        "transaction": str(tx_root / "transaction.json"),
    }
    try:
        _set_update_phase(tx_root, journal, "swapping", "restore-old")
        if install.exists():
            os.replace(install, backup)
            _fsync_directory(install.parent)
        if phase_hook:
            phase_hook("after-backup")
        os.replace(candidate, install)
        _fsync_directory(install.parent)
        if phase_hook:
            phase_hook("after-activate")
        _validate_installed_tree(install)
        if phase_hook:
            phase_hook("before-commit")
        _set_update_phase(tx_root, journal, "committed", "finish-new")
        if phase_hook:
            phase_hook("after-commit")
        if restart:
            restart()
            finalize_update(result, install, data)
    except BaseException as exc:
        _recover_one_update(tx_root, install=install, data=data)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise AppDockError("update failed and was rolled back") from exc
    return result


@_locked_transaction(2)
def finalize_update(applied: dict[str, Any], install_dir: str | Path, data_dir: str | Path) -> None:
    install, data = _validated_update_roots(install_dir, data_dir)
    transaction = Path(str(applied.get("transaction") or "")).expanduser().absolute()
    _assert_no_link_or_reparse_ancestor(transaction)
    if transaction.name != "transaction.json":
        raise AppDockError("update transaction path is invalid")
    tx_root = transaction.parent
    context = _validated_transaction_context(tx_root, install, data)
    if _lexical_path_key(transaction) != _lexical_path_key(context["journal"]) or context["journal_data"]["phase"] != "committed":
        raise AppDockError("update transaction is not ready to finalize")
    _verify_identity_tree(install, context["journal_data"]["identity"], complete=False)
    _recover_one_update(tx_root, install=install, data=data)


@_locked_transaction(2)
def rollback_update(applied: dict[str, Any], install_dir: str | Path, data_dir: str | Path) -> None:
    install, data = _validated_update_roots(install_dir, data_dir)
    transaction_raw = applied.get("transaction")
    if isinstance(transaction_raw, str) and transaction_raw:
        transaction = Path(transaction_raw).expanduser().absolute()
        _assert_no_link_or_reparse_ancestor(transaction)
        if transaction.name != "transaction.json":
            raise AppDockError("update transaction path is invalid")
        tx_root = transaction.parent
        context = _validated_transaction_context(tx_root, install, data)
        if _lexical_path_key(transaction) != _lexical_path_key(context["journal"]):
            raise AppDockError("update transaction path is invalid")
        journal = context["journal_data"]
        if journal["phase"] == "complete":
            raise AppDockError("finalized update can no longer be rolled back automatically")
        journal["phase"] = "swapping"
        journal["recovery"] = "restore-old"
        _durable_write_json(context["journal"], journal)
        _recover_one_update(tx_root, install=install, data=data)
        return
    # Compatibility for pre-v0.1.1 in-memory results retained for focused rollback tests.
    backup = Path(str(applied.get("backup") or "")).expanduser().absolute()
    backup_root = data / "updates" / "backups"
    _assert_no_link_or_reparse_ancestor(backup_root)
    _assert_no_link_or_reparse_ancestor(backup)
    if not backup.is_dir() or _is_link_or_reparse(backup) or not _inside(backup, backup_root):
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
        target = _release_path(install, raw_relative, require_file=False)
        saved = _release_path(backup, raw_relative)
        if raw_relative in preexisting_set:
            _durable_copy(saved, target)
        else:
            if target.exists():
                _release_path(install, raw_relative)
                target.unlink()


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
    expected_identity: dict[str, Any] | None = None,
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
    if not _inside(staged, data / "updates"):
        raise AppDockError("staged update path is invalid")
    helper = _release_path(staged, "scripts/update_helper.py")
    staged_config = AppDockConfig.from_environment(data_dir=data)
    claimed_record = expected_identity if isinstance(expected_identity, dict) and "identity" in expected_identity else None
    if claimed_record is not None:
        verified_claim = _verify_staged_record(staged_config, claimed_record)
        identity = verified_claim["identity"]
    else:
        identity = _staged_identity(staged)
        if expected_identity is not None:
            identity_claim = _validate_staged_identity(expected_identity)
            if identity_claim["inventory_sha256"] != identity["inventory_sha256"] or identity_claim["helper_sha256"] != identity["helper_sha256"] or identity_claim["inventory"] != identity["inventory"]:
                raise AppDockError("staged update identity does not match staged bytes")
            identity = identity_claim
    if hashlib.sha256(_read_regular_single_link(helper, MAX_UPDATE_UNCOMPRESSED_BYTES)).hexdigest() != identity["helper_sha256"]:
        raise AppDockError("staged update helper checksum does not match its identity")
    if claimed_record is not None:
        # The stage is user-writable after staging. Revalidate the complete
        # receipt/tree a second time immediately before constructing Popen.
        verified_claim = _verify_staged_record(staged_config, claimed_record)
        identity = verified_claim["identity"]
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
    if claimed_record is not None:
        helper_command.extend([
            "--expected-version", verified_claim["version"],
            "--expected-digest", verified_claim["digest"],
            "--expected-zip-sha256", identity["zip_sha256"],
            "--expected-inventory-sha256", identity["inventory_sha256"],
            "--expected-helper-sha256", identity["helper_sha256"],
        ])
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
    <button id="menuButton" class="menu-button" type="button" aria-expanded="false" aria-controls="drawer" aria-label="Open navigation">☰</button>
    <div class="brand">App<span>Dock</span></div>
    <div class="actions" aria-label="AppDock actions">
      <button id="addButton" type="button" class="primary">Add App</button>
    </div>
  </header>

  <div id="drawerBackdrop" class="drawer-backdrop" hidden></div>
  <aside id="drawer" class="drawer" aria-label="AppDock navigation" aria-hidden="true" inert>
    <div class="drawer-heading"><strong>Navigate</strong><button id="closeDrawerButton" type="button" aria-label="Close navigation">×</button></div>
    <nav class="drawer-nav">
      <button id="dashboardLink" type="button" data-view="dashboard">Dashboard</button>
      <button id="lmStudioLink" type="button" data-view="lm-studio">LM Studio</button>
      <button id="updatesLink" type="button" data-view="updates">Updates <span id="updatesBadge" class="nav-badge" hidden aria-label="Updates available">!</span></button>
    </nav>
  </aside>

  <main class="shell">
    <div id="updateBanner" class="update-banner" role="status" hidden>
      <div><strong>AppDock update available.</strong><span id="updateBannerText"></span></div>
      <button id="bannerUpdatesButton" type="button">Review updates</button>
    </div>

    <section id="dashboardView" class="view" data-view="dashboard">
      <div class="toolbar">
        <div>
          <h1>Your apps</h1>
          <p class="muted">Register explicit manifests, then control each process locally.</p>
        </div>
        <button id="refreshButton" type="button">Refresh</button>
      </div>
      <section id="apps" class="apps" aria-live="polite"></section>

      <section id="extensionsPanel" class="extensions" aria-labelledby="extensionsTitle" hidden>
        <h2 id="extensionsTitle">Extensions</h2>
        <p id="extensionsError" class="status warning" role="status" hidden></p>
        <div id="widgets" class="widgets" aria-live="polite"></div>
      </section>
    </section>

    <section id="lmStudioView" class="view" data-view="lm-studio" hidden>
      <div class="toolbar">
        <div>
          <h1>LM Studio</h1>
          <p class="muted">Optional local model management through the LM Studio <code>lms</code> CLI.</p>
        </div>
        <button id="lmRefreshButton" type="button">Refresh</button>
      </div>
      <p id="lmStatus" class="status" role="status"></p>
      <p id="lmHelp" class="muted">LM Studio is optional. <a href="https://lmstudio.ai/docs/cli" target="_blank" rel="noopener noreferrer">Install or read the lms CLI docs</a>.</p>
      <section class="panel" aria-labelledby="lmLoadedTitle"><h2 id="lmLoadedTitle">Loaded instances</h2><div id="lmLoaded" class="lm-list" aria-live="polite"></div></section>
      <section class="panel" aria-labelledby="lmModelsTitle"><h2 id="lmModelsTitle">Installed models</h2><div id="lmModels" class="lm-list" aria-live="polite"></div></section>
    </section>

    <section id="updatesPanel" class="view panel" data-view="updates" hidden>
      <h1>AppDock updates</h1>
      <p class="muted">Checks contact GitHub Releases. Downloads happen only after you explicitly confirm Update now; release assets are checksum-verified, staged, backed up, and rollback-safe.</p>
      <label class="update-channel" for="updateChannel">
        <span>Update channel</span>
        <select id="updateChannel">
          <option value="stable">Stable</option>
          <option value="beta">Beta (pre-release)</option>
        </select>
      </label>
      <p id="betaChannelWarning" class="warning" hidden>Beta builds are optional prereleases and may contain unfinished features or regressions. They use the same verified update and rollback pipeline as Stable.</p>
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
    def __init__(self, config: AppDockConfig | None = None) -> None:
        self._lock = threading.Lock()
        self._staged: dict[str, Any] | None = None
        self._staging = False
        self._applying = False
        self._update_lock: UpdateLock | None = None
        self._config: AppDockConfig | None = None
        if config is not None:
            self.bind(config)

    def bind(self, config: AppDockConfig) -> None:
        with self._lock:
            self._config = config
            if self._staged is None:
                staged = _read_staged_receipt(config)
                if staged is not None:
                    self._staged = staged

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
            if self._config is not None and _staged_receipt_path(self._config).exists():
                self._staged = _read_staged_receipt(self._config)
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

    def retain_update_lock(self, update_lock: UpdateLock) -> None:
        with self._lock:
            if self._update_lock is not None:
                raise AppDockError("an updater lock is already retained")
            self._update_lock = update_lock

    def release_update_lock(self) -> None:
        with self._lock:
            update_lock = self._update_lock
            self._update_lock = None
        if update_lock is not None:
            update_lock.release()


def stage_coordinated_update(
    release: dict[str, Any],
    config: AppDockConfig,
    coordinator: UpdateCoordinator,
    *,
    repository: str,
    stager: Callable[..., dict[str, Any]] = stage_update,
) -> dict[str, Any]:
    coordinator.bind(config)
    coordinator.begin_stage()
    update_lock: UpdateLock | None = None
    staged: dict[str, Any] | None = None
    try:
        update_lock = acquire_update_lock(config.data_root)
        staged = stager(release, config, repository=repository)
        coordinator.finish_stage(staged)
        coordinator.retain_update_lock(update_lock)
        update_lock = None
    except Exception:
        if staged is not None:
            raw_path = staged.get("path")
            if isinstance(raw_path, str):
                staged_path = Path(raw_path).resolve()
                if staged_path != config.updates_root.resolve() and _inside(staged_path, config.updates_root):
                    _remove_tree(staged_path, ignore_errors=True)
        _clear_staged_receipt(config)
        coordinator.cancel_stage()
        raise
    finally:
        if update_lock is not None:
            update_lock.release()
    return staged


class Handler(BaseHTTPRequestHandler):
    manager: AppManager = AppManager()
    config: AppDockConfig = manager.config
    extensions: ExtensionManager = manager.extensions
    local: LocalFolderOnboarding = LocalFolderOnboarding(config)
    github: GitHubOnboarding = GitHubOnboarding(config)
    checker: ReleaseChecker = ReleaseChecker(config.update_repository)
    coordinator: UpdateCoordinator = UpdateCoordinator()
    lm_adapter: LMStudioAdapter = LMStudioAdapter()
    lm_mutation_lock = threading.Lock()
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
        value = _loads_strict_json(raw.decode("utf-8")) if raw else {}
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
            elif path == "/api/extensions": self._json(self.extensions.snapshot(self.manager.discover()))
            elif path == "/api/config": self._json({"version": CURRENT_VERSION, "update_repository": self.config.update_repository, "update_channel": read_update_channel(self.config)})
            elif path == "/api/updates/check":
                release = self.checker.check(read_update_channel(self.config))
                self._json({**release, "confirmation_digest": _digest(release)})
            elif path == "/api/lm-studio":
                self._json(self.lm_adapter.snapshot())
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
            if path == "/api/updates/channel":
                if set(body) != {"channel"}:
                    raise AppDockError("update channel request is invalid")
                channel = write_update_channel(self.config, body.get("channel"))
                self._json({"channel": channel}); return
            if path == "/api/updates/stage":
                release = self.checker.check(read_update_channel(self.config))
                if not release.get("update_available") or body.get("confirmation") != _digest(release):
                    raise AppDockError("update confirmation is stale or invalid")
                staged = stage_coordinated_update(release, self.config, Handler.coordinator, repository=self.config.update_repository)
                self._json({"staged": True, "version": staged["version"], "confirmation_digest": staged["digest"]}); return
            if path == "/api/updates/apply":
                staged = Handler.coordinator.claim(body.get("confirmation", ""))
                install_dir = Path(__file__).resolve().parent
                restart_args = ["--host", str(self.server.server_address[0]), "--port", str(self.server.server_address[1]), "--data-dir", str(self.config.data_root)]
                try:
                    launch_update_helper(
                        staged["path"], install_dir, self.config.data_root,
                        current_pid=os.getpid(), restart_args=restart_args,
                        expected_identity=staged,
                    )
                except Exception:
                    Handler.coordinator.restore(staged)
                    Handler.coordinator.release_update_lock()
                    raise
                self._json({"restart_pending": True, "version": staged.get("version")}, 202)
                threading.Thread(target=self.server.shutdown, daemon=True).start(); return
            if path in {"/api/lm-studio/load", "/api/lm-studio/unload"}:
                if not self.lm_mutation_lock.acquire(blocking=False):
                    self._json({"error": "another LM Studio operation is already in progress; retry shortly"}, 409)
                    return
                try:
                    snapshot = self.lm_adapter.snapshot()
                    if not snapshot.get("available"):
                        self._json({"error": snapshot.get("error") or "LM Studio is unavailable"}, 503)
                        return
                    if not snapshot.get("running"):
                        self._json({"error": "LM Studio is not running; start LM Studio, then retry."}, 503)
                        return
                    if path.endswith("/load"):
                        args = build_lm_load_args(body, snapshot.get("installed_models") or [])
                    else:
                        args = build_lm_unload_args(body, snapshot.get("loaded_instances") or [])
                    ok, timed_out, detail = self.lm_adapter.execute(args)
                    if not ok:
                        self._json({"error": detail}, 504 if timed_out else 500)
                    else:
                        self._json({"ok": True, "message": "LM Studio operation requested"})
                except ValueError as exc:
                    status = 409 if "currently loaded" in str(exc) else 400
                    self._json({"error": str(exc)}, status)
                finally:
                    self.lm_mutation_lock.release()
                return
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
    parser.add_argument("--update-helper-startup", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    validate_bind_host(args.host)
    if args.ready_token is not None and not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", args.ready_token):
        parser.error("invalid readiness token")
    if args.update_helper_startup is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", args.update_helper_startup) or args.ready_token is None or not secrets.compare_digest(args.update_helper_startup, args.ready_token):
            parser.error("invalid update helper startup authorization")
    config = AppDockConfig.from_environment(data_dir=args.data_dir)
    config.ensure()
    if args.update_helper_startup is not None:
        _consume_update_startup_handoff(config.data_root, Path(__file__).resolve().parent, args.update_helper_startup)
        startup_coordinator = UpdateCoordinator()
    else:
        with acquire_update_lock(config.data_root):
            recover_private_migrations(config)
            recover_update_transactions(config.data_root, expected_install=Path(__file__).resolve().parent)
            startup_coordinator = UpdateCoordinator(config)
    Handler.config = config; Handler.extensions = ExtensionManager(config); Handler.manager = AppManager(config=config, extensions=Handler.extensions); Handler.local = LocalFolderOnboarding(config); Handler.github = GitHubOnboarding(config); Handler.checker = ReleaseChecker(config.update_repository); Handler.coordinator = startup_coordinator; Handler.lm_adapter = LMStudioAdapter(); Handler.lm_mutation_lock = threading.Lock(); Handler.ready_token = args.ready_token
    cleanup_stop = threading.Event()
    cleanup_thread = threading.Thread(target=_staging_cleanup_loop, args=(Handler.github, cleanup_stop), name="appdock-staging-cleanup", daemon=True)
    cleanup_thread.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AppDock listening at http://{args.host}:{args.port}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        cleanup_stop.set()
        Handler.coordinator.release_update_lock()
        server.server_close()


if __name__ == "__main__": main()
