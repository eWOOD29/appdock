# Private-state migration

AppDock 0.1.1 includes a deterministic preview/import/rollback tool for a protected private package. It does not move application source directories and does not start applications.

## Package contract

A private package contains `appdock-private-package.json`, referenced normalized registration manifests, one saved-order file, and one extension configuration file. Additional private adapters may be stored beside these inputs, but the migration tool reads only explicitly referenced files.

```json
{
  "schema_version": 1,
  "registrations": [
    "migration/registry/synthetic-app/appdock.json"
  ],
  "order_path": "migration/app-order.json",
  "extension_config_path": "migration/extensions.json"
}
```

Every registration must preserve an absolute external application directory and an argument-array command. The order must contain every package registration exactly once. Visibility is validated separately. Duplicate, conflicting, malformed, unsupported, missing, symlinked, or unsafe inputs are rejected before any destination changes.

## Preview

```powershell
python scripts/migrate_private_state.py --data-dir C:\AppDock-Preview-Data --preview C:\Path\To\Private-Package
```

Preview parses and normalizes all inputs, returns counts and a deterministic digest, and makes no changes.

## Import

```powershell
python scripts/migrate_private_state.py --data-dir C:\AppDock-Preview-Data --import-package C:\Path\To\Private-Package
```

Import stages all target files under the data root, snapshots any existing registry manifests, order, and extension configuration, then replaces only those files. A failure restores the prior files. Re-importing identical content is idempotent and reports `changed: false`. No process is started.

## Rollback

The import result includes a receipt path. Rollback first verifies that imported files still match the receipt, then restores the snapshot:

```powershell
python scripts/migrate_private_state.py --data-dir C:\AppDock-Preview-Data --rollback C:\AppDock-Preview-Data\migrations\backups\...\receipt.json
```

Rollback refuses to overwrite state that changed after import.

## First 0.1.0 to 0.1.1 transition

AppDock 0.1.0 intentionally cannot apply the schema-2 0.1.1 archive with its one-click updater. The first transition requires a separately authorized, verified manual installation and private-state import. Later compatible versions may use in-app updates after independent review and local validation.
