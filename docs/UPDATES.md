# Updates, trust, and rollback

AppDock's updater is designed around versioned GitHub release assets and a user-data directory that is separate from program files.

## Update channels and notification behavior

AppDock has two release channels:

- **Stable** (default): checks GitHub's `releases/latest` endpoint and rejects drafts and prereleases.
- **Beta (pre-release)** (explicit opt-in): enumerates GitHub Releases and considers only non-draft numbered prereleases matching `v<major>.<minor>.<patch>-beta.<n>`. Raw `develop` branch snapshots are never update candidates.

The selected channel is stored as `update-settings.json` in the separate AppDock data root, so it survives program-file replacement. A missing, corrupt, or unsupported setting fails safe to Stable. Switching from a newer Beta build back to Stable does not downgrade AppDock; Stable becomes eligible only when a strictly newer stable version exists.

After the UI starts, AppDock performs a non-blocking check against the selected channel. It repeats no more than once every six hours, while the server-side `ReleaseChecker` cache remains authoritative per channel. A newer eligible release is shown in a non-modal dashboard banner and Updates navigation badge. Automatic failures are quiet during normal dashboard use; a manual check displays an actionable failure on the Updates page. Checks never download or apply an update.

GitHub may receive ordinary connection metadata for the Releases request, such as the client IP and user agent. AppDock itself has no update telemetry or analytics.

## Check flow

1. AppDock reads the persisted update channel; Stable is used if no valid preference exists.
2. Stable queries the configured repository's GitHub `releases/latest` API. Beta queries the Releases collection and filters to valid numbered AppDock Beta prereleases. Drafts are always rejected.
3. It compares the selected release tag with the running semantic version.
4. It displays the channel, version, public release URL, and release notes.
5. The confirmation digest includes the selected release metadata and channel, so changing channels invalidates a stale update confirmation.
6. It does not download or apply anything during a check.

## Updating from v0.1.0

The v0.1.0 one-click updater intentionally cannot apply v0.1.1's release-inventory schema. Install v0.1.1 manually once using the verified ZIP and Windows installer; see [Migrating an existing AppDock setup](MIGRATING.md#v010-v011-safety-migration). This fail-closed transition prevents the older helper from bypassing v0.1.1's instance-specific readiness and durable replacement behavior.

## Apply flow

After the user chooses **Update now** and confirms:

1. AppDock re-reads the persisted channel and reuses only release metadata that still matches the channel-bound confirmation digest.
2. It selects only `appdock-windows.zip` and `SHA256SUMS.txt` assets from that same release.
3. It applies bounded timeouts and download-size limits, and validates the final redirect against GitHub-owned release-asset hosts.
4. It parses the checksum file and verifies the ZIP with SHA-256.
5. It verifies `RELEASE-MANIFEST.json`: every packaged program file must be listed with its own SHA-256 digest, required AppDock files must be present, and unlisted or missing files are rejected.
6. It rejects absolute paths, `..` traversal, symlinks, device/reserved paths, and entries outside the update staging directory.
7. It extracts into the AppDock data directory's update staging area.
8. AppDock launches the checksum-verified incoming helper and waits for a fresh token-bound startup handshake. Only after the helper has imported and validated its fixed arguments does the current server shut down.
9. The helper validates the complete current managed installation, rejects unexpected unowned files, builds and verifies a complete candidate program tree beside the active installation, writes a durable transaction journal, and records a complete backup identity before activation.
10. Activation uses directory-level replacement rather than per-file mutation. Before the durable commit marker, recovery deterministically restores the complete old program tree. After the commit marker, recovery deterministically completes and verifies the complete new program tree. The helper and AppDock startup both execute mandatory recovery before update state can be consumed; recovery is idempotent if interrupted.
11. The helper gives the restarted process a fresh, instance-specific readiness token and accepts only an exact, non-redirected response from its local `/health` endpoint containing that token. Restart failure restores and verifies the previous complete tree. Successful readiness finalizes the transaction and removes the rollback tree.

After the apply endpoint returns HTTP 202, the browser shows progress and polls same-origin `/health` for a bounded period. It reloads only after the response is healthy and its version exactly matches the expected release. A timeout reports that success was not confirmed and points the user to the local update log/manual restart recovery; it does not claim the update succeeded.

The browser cannot supply an arbitrary download URL to the update endpoint. Update assets must come from the expected configured GitHub release selected by the persisted channel. Stable and Beta use the same checksum, inventory, staging, backup, restart, health, rollback, and cleanup implementation.

## Data preservation

The updater does not replace the user data directory. On Windows, the defaults are:

- program: `%LOCALAPPDATA%\Programs\AppDock`
- data: `%LOCALAPPDATA%\AppDock`

Registry manifests, downloaded apps, ordering, settings, and logs remain in the data directory.

## Manual recovery

Normal crash recovery is automatic at helper or AppDock startup. If neither can start, preserve the transaction journal and update log before manual action. Do not delete the data directory. A manual recovery should be performed only under an explicitly authorized procedure using the journal's exact active, candidate, and backup identities.

## Manual update

1. Download `appdock-windows.zip` and `SHA256SUMS.txt` from the release.
2. Verify the ZIP checksum.
3. Stop AppDock.
4. Extract the ZIP to a temporary folder and run `scripts\install.ps1` with the intended existing data directory.
5. Leave the data directory unchanged.
6. Start AppDock and check `/health` plus the displayed version.

A development clone should use Git (`git pull`) and the normal test workflow rather than the one-click updater. One-click update application is explicit and Windows-supported.

## Branch and publication model

- `main` is the Stable branch. Stable tags/releases are promoted only after final readiness.
- `develop` is the long-lived next-release integration branch. Normal feature iteration and Beta validation happen there.
- A Beta tag must match the source version, use `vX.Y.Z-beta.N`, and point to a commit contained in `develop`. The Beta workflow publishes it with GitHub's prerelease flag and explicitly does not mark it Latest.
- The Stable workflow excludes tags containing a prerelease suffix, so a Beta tag cannot accidentally publish through the Stable path.
- Beta and Stable publication both run tests, privacy/docs checks, exact portable build validation, release-inventory validation, and publish the tested artifact rather than rebuilding in the publish job.

## Release publisher checklist

Every release must:

- use a semantic version tag such as `v0.2.1` for Stable or `v0.2.1-beta.1` for Beta;
- run the test suite on Windows and Linux;
- build `appdock-windows.zip` from tracked release files on Windows and Ubuntu and prove the exact ZIP bytes are identical;
- publish `SHA256SUMS.txt` containing the archive digest;
- avoid user data, logs, local manifests, `.env` files, credentials, and personal paths;
- include a generated `RELEASE-MANIFEST.json` covering every program file;
- keep update-compatible top-level paths;
- include human-readable release notes and upgrade caveats.
