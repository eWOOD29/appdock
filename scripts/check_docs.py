from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
EXCLUDED_PARTS = {".git", ".venv", "dist", "build", "runtime", "data"}


def broken_links(root: Path = ROOT) -> list[str]:
    broken: list[str] = []
    for document in sorted(root.rglob("*.md")):
        relative = document.relative_to(root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        text = document.read_text(encoding="utf-8")
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            path_text = unquote(target.split("#", 1)[0])
            resolved = (document.parent / path_text).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                broken.append(f"{relative}: link escapes repository: {target}")
                continue
            if not resolved.exists():
                broken.append(f"{relative}: missing target: {target}")
    return broken


def main() -> None:
    failures = broken_links()
    if failures:
        print("Documentation link check failed:")
        print("\n".join(f"- {failure}" for failure in failures))
        raise SystemExit(1)
    print("Documentation link check passed.")


if __name__ == "__main__":
    main()
