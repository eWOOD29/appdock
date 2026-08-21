# Changelog

All notable changes to AppDock are documented here. The project follows Semantic Versioning.

## [Unreleased]

## [0.2.2-beta.3] - 2026-08-19

### Fixed

- Launch the external Windows update helper from an explicit trusted filesystem root outside every installation, staging, data, backup, candidate, and transaction tree, preventing an inherited installation-root working directory from blocking the atomic directory swap with `WinError 32`.
- Fail closed before mutation if the helper's effective working directory overlaps a mutable update tree, and preserve bounded transaction-local causal evidence for a failed swap while keeping the public rollback error path sanitized.
- Bind restore-old recovery and relaunch to the transaction's exact pre-swap byte identity, persist terminal rollback state before cleanup, and atomically archive compatible rollback evidence outside the active transaction set before restarting an older AppDock runtime.
- Flush rollback-retirement parent directories through writable Windows directory handles and refuse relaunch if the move or durability flush cannot be proven.
- Treat a completed restore promotion whose destination-parent flush fails as retryable only when the installed bytes exactly match the recorded old identity; safely retire quarantine or re-promote the validated restore temp instead of rejecting the recovery as ambiguous.
- Bind the parent restart script to the exact installed `install\\appdock.py` before creating the helper handshake or runtime state, and publish staged directories through the same handle-bound move contract used by updater tree swaps.

### Verified

- Added a real Windows parent → external helper → directory swap → candidate readiness → transaction finalization regression that begins with the parent working directory inside the disposable installation root, plus causal-evidence and unsafe-cwd rejection coverage.
- Added real candidate-failure and post-readiness finalize-failure process proofs for exact v0.2.1 restoration, lock-release ordering, backward-compatible startup, single-listener ownership, and fail-closed recovery/retirement crash windows.

## [0.2.2-beta.2] - 2026-08-19

### Fixed

- Harden updater restart/rollback handling from PR #8: reject unsupported or mixed current installations before helper handshake, preserve the running service when preflight fails, persist restart stdout/stderr diagnostics under the data runtime root, harden updater log appends against symlink, reparse-point, hardlink, and replacement-race redirection, and accept syntactically valid option-like handshake tokens from the published v0.2.1 parent without weakening helper identity or preflight validation.

### Verified

- Closed PR #8 restart/rollback evidence against the accepted merge snapshot and successful exact-head and merge-commit CI (10/10 jobs each).

## [0.2.2-beta.1] - 2026-08-14

### Fixed

- Reject unsupported or mixed current installations before publishing the updater helper handshake, so the running AppDock service is not shut down when preflight fails.
- Persist updater restart stdout/stderr diagnostics under the data runtime root and harden updater log appends against symlink, reparse-point, hardlink, and replacement-race redirection.
- Accept syntactically valid option-like updater handshake tokens from the already-published v0.2.1 parent without weakening helper identity or preflight validation.

## [0.2.1] - 2026-08-12

### Added

- Opt-in **Beta** update channel with Stable as the fail-safe default.
- Persistent update-channel preference stored in the separate AppDock data root.
- Numbered Beta prerelease discovery (`vX.Y.Z-beta.N`) with SemVer ordering and no automatic downgrade when returning to Stable.
- Long-lived `develop` integration branch, CI on `develop`, and a separate Beta prerelease workflow that reuses the verified portable-release pipeline.

### Changed

- Stable publication excludes prerelease tags so Beta tags cannot enter the Stable release workflow.
- Update confirmations are bound to the selected channel while staging/application continues to use the existing checksum, inventory, backup, restart, health, rollback, and cleanup path.

## [0.2.0] - 2026-08-01

### Added

- Quiet automatic GitHub Releases checks with a non-modal availability banner and Updates navigation badge.
- Explicit, confirmation-digest-protected in-UI update progress with expected-version health polling.
- An accessible mobile-friendly navigation drawer for Dashboard, LM Studio, and Updates.
- Optional LM Studio model inspection and safe exact-instance load/unload controls through the local `lms` CLI.
- Public documentation for connection metadata, mutable user-data separation, and development-clone update guidance.

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
