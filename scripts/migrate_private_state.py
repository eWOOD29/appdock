from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from appdock import (
    AppDockConfig,
    AppDockError,
    import_private_package,
    preview_private_package,
    rollback_private_package,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview, import, or roll back a validated AppDock private-state package without starting apps."
    )
    parser.add_argument("--data-dir", type=Path, required=True, help="AppDock persistent data root")
    parser.add_argument("--expected-digest", help="Caller-confirmed digest returned by a fresh preview; required for import")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preview", type=Path, metavar="PACKAGE_DIR")
    action.add_argument("--import-package", type=Path, metavar="PACKAGE_DIR")
    action.add_argument("--rollback", type=Path, metavar="RECEIPT_JSON")
    args = parser.parse_args()

    config = AppDockConfig.from_environment(data_dir=args.data_dir)
    try:
        if args.preview:
            result = preview_private_package(args.preview)
            output = {key: value for key, value in result.items() if key != "normalized"}
        elif args.import_package:
            if not args.expected_digest:
                parser.error("--expected-digest is required with --import-package")
            result = import_private_package(args.import_package, config, expected_digest=args.expected_digest)
            output = result
        else:
            result = rollback_private_package(args.rollback, config)
            output = result
    except AppDockError as exc:
        parser.error(str(exc))
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
