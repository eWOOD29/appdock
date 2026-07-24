# Updates, trust, and rollback

AppDock's updater is designed around versioned GitHub release assets and a user-data directory that is separate from program files.

## Check flow

1. AppDock queries the configured repository's GitHub `releases/latest` API.
2. It compares the release tag with the running semantic version.
3. It displays the version, public release URL, and release notes.
4. It does not download or apply anything during a check.

## Apply flow

After the user chooses **Update now** and confirms:

1. AppDock reuses the trusted release metadata it fetched from the configured repository.
2. It selects only `appdock-windows.zip` and `SHA256SUMS.txt` assets from that same release.
3. It applies bounded timeouts and download-size limits.
4. It parses the checksum file and verifies the ZIP with SHA-256.
5. It rejects absolute paths, `..` traversal, symlinks, device/reserved paths, and entries outside the update staging directory.
6. It extracts into the AppDock data directory's update staging area.
7. An external helper waits for the server to exit, backs up current program files, replaces the installation, and restarts AppDock.
8. If replacement or launching the restarted process fails, the helper restores the previous program files.

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

1. Download and checksum-verify the desired release.
2. Stop AppDock.
3. Copy the new program files over the installation directory.
4. Leave the data directory unchanged.
5. Start AppDock and check `/health` plus the displayed version.

## Release publisher checklist

Every release must:

- use a semantic version tag such as `v0.2.0`;
- run the test suite on Windows and Linux;
- build `appdock-windows.zip` from tracked release files;
- publish `SHA256SUMS.txt` containing the archive digest;
- avoid user data, logs, local manifests, `.env` files, credentials, and personal paths;
- keep update-compatible top-level paths;
- include human-readable release notes and upgrade caveats.
