from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZIP_STORED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "appdock-windows.zip"
RELEASE_MANIFEST_NAME = "RELEASE-MANIFEST.json"
TOP_LEVEL_FILES = {
    "appdock.py",
    "appdock.example.json",
    "pyproject.toml",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
}
TOP_LEVEL_DIRS = {"appdock", "appdock_core", "static", "docs", "scripts", "templates"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".git", ".venv", "dist", "build", "runtime", "data"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log"}
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".ps1", ".py", ".toml", ".txt", ".yaml", ".yml"}
TEXT_FILENAMES = {"LICENSE"}

# These limits apply to the immutable source snapshot, before manifest generation.
# They are intentionally above the current release size while bounding memory and
# disk work if a source tree contains an unexpectedly large member.
MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_SOURCE_TOTAL_BYTES + 8 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
REPARSE_POINT = 0x400


def _lexical(path: Path) -> Path:
    return Path(path).expanduser().absolute()


def _metadata(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"source path cannot be inspected: {path}") from exc


def _unsafe_metadata(metadata) -> bool:
    return bool(stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & REPARSE_POINT)


def _assert_lexical_ancestors(path: Path) -> None:
    current = _lexical(path)
    while True:
        observed = _metadata(current)
        if observed is not None and _unsafe_metadata(observed):
            raise ValueError(f"source path contains a link or reparse point: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _require_source_directory(path: Path) -> Path:
    path = _lexical(path)
    _assert_lexical_ancestors(path)
    observed = _metadata(path)
    if observed is None or _unsafe_metadata(observed) or not stat.S_ISDIR(observed.st_mode):
        raise ValueError(f"source directory is missing or unsafe: {path}")
    return path


def _validate_source_file(path: Path, *, required: bool = True):
    path = _lexical(path)
    _assert_lexical_ancestors(path)
    observed = _metadata(path)
    if observed is None:
        if required:
            raise ValueError(f"source file is missing: {path}")
        return None
    if _unsafe_metadata(observed) or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ValueError(f"source file is not a regular single-link file: {path}")
    return observed


def _walk_release_directory(directory: Path, root: Path) -> list[Path]:
    pending = [_require_source_directory(directory)]
    files: list[Path] = []
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
                for entry in entries:
                    path = current / entry.name
                    relative = path.relative_to(root)
                    if any(part in EXCLUDED_PARTS for part in relative.parts):
                        continue
                    observed = _metadata(path)
                    if observed is None or _unsafe_metadata(observed):
                        raise ValueError(f"source member is linked or missing: {path}")
                    if stat.S_ISDIR(observed.st_mode):
                        pending.append(_require_source_directory(path))
                    elif stat.S_ISREG(observed.st_mode):
                        if observed.st_nlink != 1:
                            raise ValueError(f"source file is hardlinked: {path}")
                        if path.suffix.lower() not in EXCLUDED_SUFFIXES:
                            files.append(path)
                    else:
                        raise ValueError(f"source member is not a regular file or directory: {path}")
        except OSError as exc:
            raise ValueError(f"source directory cannot be enumerated: {current}") from exc
    return files


def release_files(root: Path = ROOT) -> list[Path]:
    root = _require_source_directory(root)
    files: list[Path] = []
    for name in sorted(TOP_LEVEL_FILES):
        path = root / name
        observed = _validate_source_file(path, required=name == "appdock.py")
        if observed is not None:
            files.append(path)
    for name in sorted(TOP_LEVEL_DIRS):
        directory = root / name
        observed = _metadata(_lexical(directory))
        if observed is None:
            continue
        _require_source_directory(directory)
        files.extend(_walk_release_directory(directory, root))
    if root / "appdock.py" not in files:
        raise FileNotFoundError("appdock.py is required")
    names: set[str] = set()
    for path in files:
        name = safe_archive_name(path, root)
        if name in names:
            raise ValueError(f"duplicate archive path: {name}")
        names.add(name)
    return files


def safe_archive_name(path: Path, root: Path = ROOT) -> str:
    root = _lexical(root)
    path = _lexical(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source path escapes root: {path}") from exc
    name = PurePosixPath(*relative.parts).as_posix()
    if name.startswith("/") or not name or ".." in PurePosixPath(name).parts:
        raise ValueError(f"unsafe archive path: {name}")
    return name


def _read_bounded_source(path: Path, maximum: int) -> bytes:
    observed = _validate_source_file(path)
    if observed.st_size > maximum:
        raise ValueError(f"source file exceeds its limit: {path}")
    identity = (observed.st_dev, observed.st_ino)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"source file could not be opened safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            _unsafe_metadata(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != identity
            or opened.st_size > maximum
        ):
            raise ValueError(f"source file changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, maximum - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"source file exceeds its limit: {path}")
        after = os.fstat(descriptor)
        if (
            _unsafe_metadata(after)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != identity
            or after.st_size != total
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError(f"source file changed while reading: {path}")
        final = _metadata(_lexical(path))
        if (
            final is None
            or _unsafe_metadata(final)
            or not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino) != identity
            or final.st_size != total
            or final.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError(f"source file changed after reading: {path}")
        return b"".join(chunks)
    except OSError as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"source file could not be read safely: {path}") from exc
    finally:
        os.close(descriptor)


def _ensure_output_parent(path: Path) -> Path:
    parent = _lexical(path).parent
    _assert_lexical_ancestors(parent)
    parent.mkdir(parents=True, exist_ok=True)
    parent = _require_source_directory(parent)
    return parent


def _assert_output_disjoint_from_source(path: Path, root: Path, *, label: str) -> None:
    path = _lexical(path)
    root = _lexical(root)
    if path == root or root in path.parents:
        raise ValueError(f"{label} must be external to the source tree: {path}")
    try:
        resolved = path.resolve(strict=False)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} cannot be resolved safely: {path}") from exc
    if resolved == resolved_root or resolved_root in resolved.parents:
        raise ValueError(f"{label} must not resolve into the source tree: {path}")


def _assert_outputs_disjoint_from_source(output: Path, sidecar: Path, root: Path) -> None:
    _assert_output_disjoint_from_source(output, root, label="archive output")
    _assert_output_disjoint_from_source(sidecar, root, label="checksum sidecar")


def _assert_outputs_not_aliasing_source_members(output: Path, sidecar: Path, files: list[Path]) -> None:
    source_identities = {
        (observed.st_dev, observed.st_ino)
        for path in files
        if (observed := _metadata(path)) is not None
    }
    for candidate, label in ((output, "archive output"), (sidecar, "checksum sidecar")):
        observed = _metadata(candidate)
        if observed is not None and (observed.st_dev, observed.st_ino) in source_identities:
            raise ValueError(f"{label} aliases a source member: {candidate}")


def _validate_output_destination(path: Path) -> Path:
    path = _lexical(path)
    _ensure_output_parent(path)
    observed = _metadata(path)
    if observed is not None and (_unsafe_metadata(observed) or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1):
        raise ValueError(f"output destination is not a regular single-link file: {path}")
    return path


def _atomic_write(path: Path, payload: bytes, *, source_root: Path | None = None, source_files: list[Path] | None = None) -> None:
    path = _validate_output_destination(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_output_destination(path)
        if source_root is not None and source_files is not None:
            _assert_output_disjoint_from_source(path, source_root, label="checksum sidecar")
            _assert_outputs_not_aliasing_source_members(path, path, source_files)
        temporary_metadata = _metadata(temporary)
        if temporary_metadata is None or _unsafe_metadata(temporary_metadata) or not stat.S_ISREG(temporary_metadata.st_mode) or temporary_metadata.st_nlink != 1:
            raise ValueError("temporary output is unsafe")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_zip(path: Path, payloads: dict[str, bytes], *, source_root: Path | None = None, source_files: list[Path] | None = None) -> None:
    path = _validate_output_destination(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            with ZipFile(stream, "w", compression=ZIP_STORED, allowZip64=True) as archive:
                for name, content in [*sorted(payloads.items()), (RELEASE_MANIFEST_NAME, _manifest_bytes(payloads))]:
                    info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                    info.create_system = 0
                    info.create_version = 20
                    info.extract_version = 20
                    info.flag_bits = 0
                    info.compress_type = ZIP_STORED
                    info.internal_attr = 0
                    info.external_attr = 0o100644 << 16
                    info.extra = b""
                    info.comment = b""
                    archive.writestr(info, content, compress_type=ZIP_STORED)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_metadata = _metadata(temporary)
        if temporary_metadata is None or _unsafe_metadata(temporary_metadata) or not stat.S_ISREG(temporary_metadata.st_mode) or temporary_metadata.st_nlink != 1:
            raise ValueError("temporary archive is unsafe")
        if temporary_metadata.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("portable archive exceeds its limit")
        _validate_output_destination(path)
        if source_root is not None and source_files is not None:
            _assert_output_disjoint_from_source(path, source_root, label="archive output")
            _assert_outputs_not_aliasing_source_members(path, path, source_files)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _manifest_bytes(payloads: dict[str, bytes]) -> bytes:
    manifest = {
        "schema_version": 2,
        "files": [
            {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in sorted(payloads.items())
        ],
    }
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_archive(output: Path = DEFAULT_OUTPUT, root: Path = ROOT) -> str:
    output = _lexical(output)
    sidecar = output.parent / "SHA256SUMS.txt"
    root = _require_source_directory(root)
    _assert_outputs_disjoint_from_source(output, sidecar, root)
    _validate_output_destination(output)
    if output == _lexical(sidecar):
        raise ValueError("archive output cannot be its checksum sidecar")
    _validate_output_destination(sidecar)
    files = release_files(root)
    _assert_outputs_disjoint_from_source(output, sidecar, root)
    _assert_outputs_not_aliasing_source_members(output, sidecar, files)
    payloads: dict[str, bytes] = {}
    aggregate = 0
    for path in files:
        name = safe_archive_name(path, root)
        if name in payloads:
            raise ValueError(f"duplicate archive path: {name}")
        remaining = MAX_SOURCE_TOTAL_BYTES - aggregate
        if remaining < 0:
            raise ValueError("source tree exceeds its aggregate limit")
        content = _read_bounded_source(path, min(MAX_SOURCE_FILE_BYTES, remaining))
        aggregate += len(content)
        if aggregate > MAX_SOURCE_TOTAL_BYTES:
            raise ValueError("source tree exceeds its aggregate limit")
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
            content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        payloads[name] = content
    _assert_outputs_disjoint_from_source(output, sidecar, root)
    _assert_outputs_not_aliasing_source_members(output, sidecar, files)
    _atomic_zip(output, payloads, source_root=root, source_files=files)
    archive_bytes = _read_bounded_source(output, MAX_ARCHIVE_BYTES)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    _assert_outputs_disjoint_from_source(output, sidecar, root)
    _assert_outputs_not_aliasing_source_members(output, sidecar, files)
    _atomic_write(
        sidecar,
        f"{digest}  {output.name}\n".encode("utf-8"),
        source_root=root,
        source_files=files,
    )
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the AppDock portable Windows release")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    digest = build_archive(args.output)
    print(f"built {_lexical(args.output)}")
    print(f"sha256 {digest}")


if __name__ == "__main__":
    main()
