from __future__ import annotations

import argparse
import json
from pathlib import Path

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
            result = import_private_package(args.import_package, config)
            output = result
        else:
            result = rollback_private_package(args.rollback, config)
            output = result
    except AppDockError as exc:
        parser.error(str(exc))
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
