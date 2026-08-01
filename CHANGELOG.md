# Changelog

All notable changes to AppDock are documented here. The project follows Semantic Versioning.

## [Unreleased]

## [0.1.2] - 2026-07-26

### Fixed

- Declare Windows external-path semantics explicitly in private-package descriptor schema 2.
- Validate and normalize drive-qualified Windows external directories identically on Windows and Ubuntu, rejecting malformed, ambiguous, mixed, device-namespace, UNC, drive-relative, root-relative, and unsafe forms.
- Make normalized private-package previews, migration digests, and deterministic protected ZIP bytes host-independent while retaining bounded compatibility for unambiguous schema-1 packages.
- Require a newly reviewed private package and newly confirmed migration digest for the corrected Windows migration while preserving published v0.1.1 bytes.

## [0.1.1] - 2026-07-24

### Fixed

- Enforce the protected private package's exact root manifest, per-file size and SHA-256 values, exact member set, migration digest, duplicate-key rejection, caller-confirmed preview digest, and immediate pre-write re-verification.
- Make private-state migration and updater replacement durable and crash-recoverable with explicit transaction phases, mandatory startup recovery, complete old/new state convergence, and abrupt-process-death tests.
- Disable stale extension visibility, widgets, providers, and caches when replacement configuration is invalid; reject duplicate JSON keys and hidden IDs that do not resolve to current registrations.
- Produce host-independent public ZIP bytes with explicit fixed ZIP metadata, build deterministic rootless private ZIPs, and remove the bootstrap uninstaller's `Get-FileHash` dependency.
- Reject installer and uninstaller paths that are filesystem/volume roots or broad ancestors of system, program, users, public, profile, AppData, or standard personal-data roots; reject source/install and install/data overlap, lexical staging/update-root aliases, and symlink/reparse ancestors.
- Build installs in a sibling staging directory and swap only after the complete program tree is ready; validate a release-inventory-backed install identity before replacement or recursive deletion; bootstrap-verify the uninstaller and path-safety module before loading safety code; and terminate only a Python process whose first script argument is the exact installed entry point.
- Bound GitHub preview staging during clone by time, per-checkout and aggregate file/byte quotas; disable Git LFS smudging; run an independent stale-stage janitor; and clean abandoned, late, or superseded browser previews.
- Serialize staged-update application before downloading so only one request can own a version stage; require the external helper to complete a token-bound startup handshake before the server shuts down; remove completed or failed stages; and require instance-specific restart readiness. Backup failure leaves installation files untouched; failed replacement rolls back and verifies a restart of the restored version.
- Remove absolute local paths from routine status/config API responses.
- Reject Unicode Windows device aliases and update downloads redirected outside GitHub-owned asset hosts.
- Use release-inventory schema `2` as a fail-closed migration gate: v0.1.0 rejects the v0.1.1 archive and must be upgraded manually once; v0.1.1 and later launch the checksum-verified helper from the incoming stage.

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

[Unreleased]: https://github.com/eWOOD29/appdock/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/eWOOD29/appdock/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/eWOOD29/appdock/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/eWOOD29/appdock/releases/tag/v0.1.0
