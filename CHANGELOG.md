# Changelog

All notable changes to AppDock are documented here. The project follows Semantic Versioning.

## [Unreleased]

## [0.1.0] - 2026-07-24

### Added

- Public, local-first AppDock dashboard for explicit app manifests.
- Safe start, stop, restart, health checks, app ordering, and bounded log tails.
- Local-folder registration with preview and explicit confirmation.
- Advanced public-GitHub clone/import workflow with preview before registration.
- Configurable data, registry, install, staging, update, and runtime directories.
- GitHub release version checking and checksum-verified staged updates.
- Per-file release inventories with obsolete managed-file cleanup and rollback restoration.
- Windows installer, optional startup shortcut, portable release archive, and uninstaller.
- CI, automated release packaging, security policy, manifest reference, and usage documentation.

### Security

- User state is separated from installation files and excluded from source control.
- Shell command strings, unsafe app IDs, path escapes, unsafe GitHub URLs, arbitrary update URLs, ZIP traversal, symlink archive members, and protected system PIDs are rejected.
- Newly registered apps never start automatically.

[Unreleased]: https://github.com/eWOOD29/appdock/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/eWOOD29/appdock/releases/tag/v0.1.0
