from __future__ import annotations

import argparse
import json
import re
import tomllib
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".ps1", ".cmd", ".html", ".css", ".js", ".txt", ""}
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build", "runtime", "data"}
MAX_SCAN_FILE_BYTES = 2 * 1024 * 1024

PATTERNS = {
    "absolute Windows user path": re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!<|%)[^\\/\s`\"']+"),
    "absolute POSIX home path": re.compile(r"(?m)(?<![\w.-])/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "credential-bearing URL": re.compile(r"https?://[^/@\s]+:[^/@\s]+@", re.I),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "likely assigned secret": re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret|recovery[_-]?code)\s*[:=]\s*[\"'][^\"']{8,}[\"']"),
    "Google Drive URL": re.compile(r"https?://(?:drive|docs)\.google\.com/", re.I),
    "Tailnet hostname": re.compile(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net\b"),
    "private IPv4 address": re.compile(r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})(?!\d)"),
}


def _variants(text: str) -> list[str]:
    variants = [text]
    current = text
    for _ in range(2):
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            break
        variants.append(decoded)
        current = decoded
    normalized = current.replace("\\\\", "\\").replace("\\/", "/")
    if normalized not in variants:
        variants.append(normalized)
    return variants


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _structured_strings(name: str, text: str) -> Iterable[str]:
    suffix = Path(name).suffix.lower()
    try:
        if suffix == ".json":
            yield from _walk_strings(json.loads(text))
        elif suffix == ".toml":
            yield from _walk_strings(tomllib.loads(text))
    except (ValueError, TypeError, tomllib.TOMLDecodeError):
        return


def _allowed_fixture(label: str, source: str, match: str) -> bool:
    source_path = source.replace("\\", "/")
    if source_path.endswith("scripts/privacy_scan.py"):
        return True
    return (
        label == "credential-bearing URL"
        and source_path.startswith("tests/")
        and match.lower() in {"http://user:pass@", "https://user:pass@"}
    )


def scan_text(source: str, text: str) -> list[str]:
    findings: set[str] = set()
    inputs = [text, *_structured_strings(source, text)]
    for candidate in inputs:
        for variant in _variants(candidate):
            for label, pattern in PATTERNS.items():
                for match in pattern.finditer(variant):
                    value = match.group(0)
                    if _allowed_fixture(label, source, value):
                        continue
                    line = text.count("\n", 0, min(match.start(), len(text))) + 1 if variant is text else 1
                    findings.add(f"{source}:{line}: {label}")
    return sorted(findings)


def scan(root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or path.is_symlink() or any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > MAX_SCAN_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(relative.as_posix(), text))
    return sorted(set(findings))


def scan_archive(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                findings.extend(scan_text(f"archive-name:{path.name}", info.filename))
                if info.is_dir() or Path(info.filename).suffix.lower() not in TEXT_SUFFIXES or info.file_size > MAX_SCAN_FILE_BYTES:
                    continue
                try:
                    text = archive.read(info).decode("utf-8")
                except (UnicodeDecodeError, OSError, RuntimeError):
                    continue
                findings.extend(scan_text(f"archive:{path.name}:{info.filename}", text))
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"{path}: archive could not be inspected: {exc}"]
    return sorted(set(findings))


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan source and exact release bytes for likely private data or secrets")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    findings = scan(args.root.resolve())
    if args.archive:
        findings.extend(scan_archive(args.archive.resolve()))
    findings = sorted(set(findings))
    if findings:
        print("Privacy scan failed:")
        print("\n".join(f"- {finding}" for finding in findings))
        raise SystemExit(1)
    print("Privacy scan passed: no likely personal paths, private network values, Drive links, or embedded secrets found.")


if __name__ == "__main__":
    main()
