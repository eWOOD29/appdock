# Updates, trust, and rollback

AppDock's updater is designed around versioned GitHub release assets and a user-data directory that is separate from program files.

## Check flow

1. AppDock queries the configured repository's GitHub `releases/latest` API.
2. It compares the release tag with the running semantic version.
3. It displays the version, public release URL, and release notes.
4. It does not download or apply anything during a check.

## Updating from v0.1.0

The v0.1.0 one-click updater intentionally cannot apply v0.1.1's release-inventory schema. Install v0.1.1 manually once using the verified ZIP and Windows installer; see [Migrating an existing AppDock setup](MIGRATING.md#v010--v011-safety-migration). This fail-closed transition prevents the older helper from bypassing v0.1.1's instance-specific readiness and durable replacement behavior.

## Apply flow

After the user chooses **Update now** and confirms:

1. AppDock reuses the trusted release metadata it fetched from the configured repository.
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

The browser cannot supply an arbitrary download URL to the update endpoint. Update assets must come from the expected configured GitHub release.

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

## Release publisher checklist

Every release must:

- use a semantic version tag such as `v0.2.0`;
- run the test suite on Windows and Linux;
- build `appdock-windows.zip` from tracked release files on Windows and Ubuntu and prove the exact ZIP bytes are identical;
- publish `SHA256SUMS.txt` containing the archive digest;
- avoid user data, logs, local manifests, `.env` files, credentials, and personal paths;
- include a generated `RELEASE-MANIFEST.json` covering every program file;
- keep update-compatible top-level paths;
- include human-readable release notes and upgrade caveats.
