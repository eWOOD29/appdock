from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import appdock
from scripts.build_private_package import build_private_archive


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def build_fixture(output: Path) -> dict[str, object]:
    if output.exists():
        shutil.rmtree(output)
    source = output / "source"
    source.mkdir(parents=True)
    ids = [f"synthetic-{index}" for index in range(7)] + ["synthetic-backend"]
    registration_paths: list[str] = []
    normalized: dict[str, dict[str, object]] = {}
    for index, app_id in enumerate(ids):
        path = source / "migration" / "registry" / app_id / "appdock.json"
        raw = {
            "id": app_id,
            "name": app_id,
            "description": "synthetic cross-host fixture",
            "external": True,
            "directory": rf"C:\Synthetic\Application{index}",
            "command": ["python", "-c", "raise SystemExit('must never execute')"],
            "cwd": r"runtime\worker",
            "port": None,
            "health_url": "",
            "local_url": "http://127.0.0.1:19000",
            "private_url": "https://private.example.invalid/app",
            "env": {},
            "process_name": "",
            "stop_timeout": 3.0,
        }
        write_json(path, raw)
        registration_paths.append(path.relative_to(source).as_posix())
        normalized[app_id] = appdock.normalize_private_registration(raw, manifest_dir=path.parent, path_flavor="windows")
    extensions = {
        "schema_version": 1,
        "visibility": {"hidden_app_ids": ["synthetic-backend"]},
        "providers": [],
        "widgets": [],
    }
    write_json(source / "migration" / "app-order.json", ids)
    write_json(source / "migration" / "extensions.json", extensions)
    write_json(source / appdock.PRIVATE_PACKAGE_MANIFEST, {
        "schema_version": 2,
        "path_flavor": "windows",
        "registrations": registration_paths,
        "order_path": "migration/app-order.json",
        "extension_config_path": "migration/extensions.json",
    })
    normalized_payload = {
        "schema_version": 2,
        "path_flavor": "windows",
        "registrations": {app_id: normalized[app_id] for app_id in sorted(normalized)},
        "order": ids,
        "extensions": extensions,
    }
    migration_digest = appdock._digest(normalized_payload)
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            files.append({
                "path": path.relative_to(source).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
    write_json(source / appdock.PRIVATE_PACKAGE_HASH_MANIFEST, {
        "schema_version": 1,
        "package": "AppDock private integration package",
        "migration_digest": migration_digest,
        "files": files,
    })
    preview = appdock.preview_private_package(source)
    write_json(output / "preview.json", preview)
    archive = output / "AppDock-Private-Fixture.zip"
    archive_sha256 = build_private_archive(source, archive)
    runtime_config = appdock.AppDockConfig.from_environment(data_dir=output / "runtime-data")
    appdock.import_private_package(source, runtime_config, expected_digest=preview["digest"])
    manager = appdock.AppManager(config=runtime_config)
    specs = manager.discover()
    discovery = {
        "schema_version": 1,
        "path_flavor": "windows",
        "registrations": [
            {"id": app_id, "directory": str(specs[app_id].directory), "cwd": str(specs[app_id].cwd), "path_flavor": specs[app_id].path_flavor}
            for app_id in ids
        ],
        "visible_ids": [item["id"] for item in manager.all_status()],
    }
    write_json(output / "discovery.json", discovery)
    print(json.dumps({
        "digest": preview["digest"],
        "registration_count": preview["registration_count"],
        "visible_count": preview["visible_count"],
        "order_count": preview["order_count"],
        "archive_sha256": archive_sha256,
        "archive_size": archive.stat().st_size,
    }, sort_keys=True))
    return preview


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit a deterministic synthetic Windows-flavor private package fixture")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_fixture(args.output)


if __name__ == "__main__":
    main()
