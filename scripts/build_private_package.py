from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from appdock import PRIVATE_PACKAGE_HASH_MANIFEST, preview_private_package


def build_private_archive(source: Path, output: Path) -> str:
    source = source.expanduser().resolve()
    preview_private_package(source)
    members = sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
    if PRIVATE_PACKAGE_HASH_MANIFEST not in members:
        raise ValueError("PACKAGE-MANIFEST.json is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_STORED, allowZip64=True) as archive:
        for name in members:
            payload = (source / Path(*name.split("/"))).read_bytes()
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
            archive.writestr(info, payload, compress_type=ZIP_STORED)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a rootless deterministic AppDock private package")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    digest = build_private_archive(args.source, args.output)
    print(f"built {args.output.resolve()}")
    print(f"sha256 {digest}")


if __name__ == "__main__":
    main()
