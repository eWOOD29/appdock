# Private-state migration

AppDock 0.1.1 includes a deterministic preview/import/rollback tool for a protected private package. It does not move application source directories and does not start applications.

## Package contract

A private package is rootless and contains exactly the regular files declared by root `PACKAGE-MANIFEST.json`, plus that manifest itself. The hash manifest declares every payload path, byte size, SHA-256 digest, and the expected normalized migration digest. Extra, missing, duplicate, symlinked, reparse-point, malformed, hash-mismatched, or size-mismatched members are rejected.

`appdock-private-package.json` then identifies the migration inputs:

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

Every registration must preserve an absolute external application directory and an argument-array command. The order must contain every package registration exactly once. Visibility may reference only registrations in the same package. Duplicate JSON keys and unsupported fields are rejected before any destination changes.

## Preview

```powershell
python scripts/migrate_private_state.py --data-dir C:\AppDock-Preview-Data --preview C:\Path\To\Private-Package
```

Preview verifies the complete package manifest, parses and normalizes all migration inputs, returns counts and a deterministic digest, and makes no destination changes. Record the returned digest for the immediately following import.

## Import

```powershell
python scripts/migrate_private_state.py `
  --data-dir C:\AppDock-Preview-Data `
  --import-package C:\Path\To\Private-Package `
  --expected-digest <digest-returned-by-preview>
```

Import requires the caller-confirmed preview digest. It re-verifies the complete package identity before writing and verifies durable staged bytes immediately before activation. A stale digest or any package mutation is rejected.

The importer creates a durable transaction journal, complete staged target set, and complete prior-state backup before changing active state. Each target replacement is durable. If the process is terminated at any point before the durable commit marker, the next importer invocation or AppDock startup restores the complete old state before it can be consumed. If termination occurs after the commit marker, recovery completes and verifies the complete new state. Recovery is idempotent if recovery itself is interrupted. Re-importing identical content reports `changed: false`. No process is started.

## Rollback

A completed import returns a receipt path under `migrations\transactions`. Rollback first verifies that every imported target still has the committed digest, then restores the complete prior state:

```powershell
python scripts/migrate_private_state.py --data-dir C:\AppDock-Preview-Data --rollback C:\AppDock-Preview-Data\migrations\transactions\...\receipt.json
```

Rollback refuses to overwrite state that changed after import.

## Deterministic protected archive

A package owner can build a rootless deterministic ZIP after the source directory passes the complete package verification:

```powershell
python scripts/build_private_package.py C:\Path\To\Private-Package C:\Path\To\AppDock-Private-Integration-Package.zip
```

The builder writes only manifest-authorized regular files, in sorted order, with fixed ZIP metadata and no wrapper directory or explicit directory entries.

## First 0.1.0 to 0.1.1 transition

AppDock 0.1.0 intentionally cannot apply the schema-2 0.1.1 archive with its one-click updater. The first transition requires a separately authorized, verified manual installation and private-state import. Later compatible versions may use in-app updates after independent review and local validation.
