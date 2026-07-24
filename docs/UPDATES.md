# Updates, trust, and rollback

AppDock's updater is designed around versioned GitHub release assets and a user-data directory that is separate from program files.

## Check flow

1. AppDock queries the configured repository's GitHub `releases/latest` API.
2. It compares the release tag with the running semantic version.
3. It displays the version, public release URL, and release notes.
4. It does not download or apply anything during a check.

## Updating from v0.1.0

The v0.1.0 one-click updater intentionally cannot apply v0.1.1's release-inventory schema. Install v0.1.1 manually once using the verified ZIP and Windows installer; see [Migrating an existing AppDock setup](MIGRATING.md#v010--v011-safety-migration). This fail-closed transition prevents the older helper from bypassing v0.1.1's instance-specific readiness and retry-safe rollback behavior.

## Apply flow

After the user chooses **Update now** and confirms:

1. AppDock reuses the trusted release metadata it fetched from the configured repository.
2. It selects only `appdock-windows.zip` and `SHA256SUMS.txt` assets from that same release.
3. It applies bounded timeouts and download-size limits, and validates the final redirect against GitHub-owned release-asset hosts.
4. It parses the checksum file and verifies the ZIP with SHA-256.
5. It verifies `RELEASE-MANIFEST.json`: every packaged program file must be listed with its own SHA-256 digest, required AppDock files must be present, and unlisted or missing files are rejected.
6. It rejects absolute paths, `..` traversal, symlinks, device/reserved paths, and entries outside the update staging directory.
7. It extracts into the AppDock data directory's update staging area.
8. AppDock launches the checksum-verified incoming helper and waits for a fresh token-bound startup handshake. Only after the helper has imported, parsed its trusted arguments, opened its update log, and entered the wait/recovery path does the current server shut down. The helper then waits for the server to exit, backs up current managed files, replaces the installation, removes obsolete inventory-owned files, and restarts AppDock.
9. The helper gives the restarted process a fresh, instance-specific readiness token and accepts only an exact, non-redirected response from its local `/health` endpoint containing that token. Backup creation must finish before any installed file changes. If replacement, launch, early startup, redirect, token validation, or readiness fails, it stops the failed process, restores only files whose prior state was recorded, and verifies a restart of the restored AppDock version. Completed and failed version stages are removed so a failed update can be retried.

The browser cannot supply an arbitrary download URL to the update endpoint. Update assets must come from the expected configured GitHub release.

## Data preservation

The updater does not replace the user data directory. On Windows, the defaults are:

- program: `%LOCALAPPDATA%\Programs\AppDock`
- data: `%LOCALAPPDATA%\AppDock`

Registry manifests, downloaded apps, ordering, settings, and logs remain in the data directory.

## Manual rollback

If AppDock does not restart:

1. Open `%LOCALAPPDATA%\AppDock\updates` and locate the most recent backup recorded in the update log.
2. Stop any remaining AppDock process.
3. Rename the failed program directory out of the way.
4. Restore the backup to the previous program path.
5. Run `scripts\start-appdock.cmd` from the restored installation.
6. Review `%LOCALAPPDATA%\AppDock\runtime\update.log` before retrying.

Do not delete the data directory during rollback.

## Manual update

1. Download `appdock-windows.zip` and `SHA256SUMS.txt` from the release.
2. Verify the ZIP checksum.
3. Stop AppDock.
4. Extract the ZIP to a temporary folder and run `scripts\install.ps1` with the intended existing data directory.
5. Leave the data directory unchanged.
6. Start AppDock and check `/health` plus the displayed version.

## Release publisher checklist

Every release must:

- use a semantic version tag such as `v0.2.0`;
- run the test suite on Windows and Linux;
- build `appdock-windows.zip` from tracked release files;
- publish `SHA256SUMS.txt` containing the archive digest;
- avoid user data, logs, local manifests, `.env` files, credentials, and personal paths;
- include a generated `RELEASE-MANIFEST.json` covering every program file;
- keep update-compatible top-level paths;
- include human-readable release notes and upgrade caveats.
