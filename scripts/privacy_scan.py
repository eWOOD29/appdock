from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".ps1", ".cmd", ".html", ".css", ".js", ""}
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", "dist", "build", "runtime", "data"}

PATTERNS = {
    "absolute Windows user path": re.compile(r"(?i)\b[A-Z]:\\Users\\(?!<|%)[^\\\s`\"']+"),
    "absolute POSIX home path": re.compile(r"(?m)(?<![\w.-])/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "credential-bearing GitHub URL": re.compile(r"https://[^/@\s]+:[^/@\s]+@github\.com", re.I),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "likely assigned secret": re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*[\"'][^\"']{8,}[\"']"),
}


def scan(root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                # This exact placeholder belongs in a rejection test; any other
                # credential-bearing URL still fails the release gate.
                if (
                    label == "credential-bearing GitHub URL"
                    and relative.parts
                    and relative.parts[0] == "tests"
                    and match.group(0).lower() == "https://" + "user:pass@" + "github.com"
                ):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: {label}")
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan release text for likely private data or secrets")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    findings = scan(args.root.resolve())
    if findings:
        print("Privacy scan failed:")
        print("\n".join(f"- {finding}" for finding in findings))
        raise SystemExit(1)
    print("Privacy scan passed: no likely personal paths or embedded secrets found.")


if __name__ == "__main__":
    main()
