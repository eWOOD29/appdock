from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist" / "appdock-windows.zip"
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


def release_files(root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for name in sorted(TOP_LEVEL_FILES):
        path = root / name
        if path.is_file():
            files.append(path)
    for name in sorted(TOP_LEVEL_DIRS):
        directory = root / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root)
            if not path.is_file() or any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            files.append(path)
    if root / "appdock.py" not in files:
        raise FileNotFoundError("appdock.py is required")
    return files


def safe_archive_name(path: Path, root: Path = ROOT) -> str:
    relative = path.resolve().relative_to(root.resolve())
    name = PurePosixPath(*relative.parts).as_posix()
    if name.startswith("/") or ".." in PurePosixPath(name).parts:
        raise ValueError(f"unsafe archive path: {name}")
    return name


def build_archive(output: Path = DEFAULT_OUTPUT, root: Path = ROOT) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in release_files(root):
            info = ZipInfo(safe_archive_name(path, root), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum_path = output.parent / "SHA256SUMS.txt"
    checksum_path.write_text(f"{digest}  {output.name}\n", encoding="utf-8", newline="\n")
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the AppDock portable Windows release")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    digest = build_archive(args.output.resolve())
    print(f"built {args.output.resolve()}")
    print(f"sha256 {digest}")


if __name__ == "__main__":
    main()
