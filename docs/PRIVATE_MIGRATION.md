# Private-state migration

AppDock 0.1.2 includes a deterministic preview/import/rollback tool for a protected private package. It does not move application source directories and does not start applications. Published v0.1.1 bytes remain immutable, but v0.1.1 private packages must not be reused for the corrected Windows migration.

## Package contract

A private package is rootless and contains exactly the regular files declared by root `PACKAGE-MANIFEST.json`, plus that manifest itself. The hash manifest declares every payload path, byte size, SHA-256 digest, and the expected normalized migration digest. Extra, missing, duplicate, symlinked, reparse-point, malformed, hash-mismatched, or size-mismatched members are rejected.

New packages use descriptor schema 2 and declare the intended external-path grammar explicitly:

```json
{
  "schema_version": 2,
  "path_flavor": "windows",
  "registrations": [
    "migration/registry/synthetic-app/appdock.json"
  ],
  "order_path": "migration/app-order.json",
  "extension_config_path": "migration/extensions.json"
}
```

Windows-flavor registrations accept drive-qualified absolute paths such as `C:\Folder\Application`. Drive letters are normalized to uppercase and trailing separators are removed. AppDock rejects `/C:\Folder`, `C:Folder`, `\Folder`, relative paths, mixed slash forms, repeated or dot path components, UNC paths, device namespaces, NULs, reserved device components, and other ambiguous forms. The external source directory does not need to exist during preview or package building.

Schema-1 compatibility is bounded to packages whose registrations are all unambiguously Windows drive-qualified paths or all unambiguously POSIX absolute paths. Mixed or ambiguous schema-1 forms fail closed. New Windows packages must use schema 2.

Every registration must preserve an absolute external application directory and an argument-array command. The order must contain every package registration exactly once. Visibility may reference only registrations in the same package. Duplicate JSON keys and unsupported fields are rejected before any destination changes.

## Preview

```powershell
python scripts/migrate_private_state.py --data-dir C:\AppDock-Preview-Data --preview C:\Path\To\Private-Package
```

Preview verifies the complete package manifest, applies the descriptor's explicit path flavor, parses and normalizes all migration inputs, returns counts and a deterministic digest, and makes no destination changes. The same valid Windows-flavor package produces identical normalized JSON and digest on Windows and Ubuntu. Record the returned digest for the immediately following import.

Correcting an external directory changes normalized state and therefore changes the migration digest. Never reuse or hard-code a digest from an older package.

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

The builder writes only manifest-authorized regular files, in sorted order, with fixed ZIP metadata and no wrapper directory or explicit directory entries. It rejects the malformed `/C:\...` package shape on every host. The same valid protected source tree produces byte-identical ZIP output on Windows and Ubuntu.

## Version transition

AppDock 0.1.0 intentionally cannot apply the schema-2 release inventory used by v0.1.1 and later through its one-click updater. The first transition from v0.1.0 still requires a separately authorized, verified manual installation. For the Windows private migration described here, use AppDock v0.1.2 only with a newly reviewed schema-2 Windows-flavor private package.
