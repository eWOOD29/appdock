# Troubleshooting

## AppDock does not start

Run it in a visible PowerShell window:

```powershell
cd "$env:LOCALAPPDATA\Programs\AppDock"
py -3.11 appdock.py --data-dir "$env:LOCALAPPDATA\AppDock"
```

Check:

- Python 3.11+ is installed;
- port 8765 is free;
- the installation and data directories are different;
- Windows security software did not quarantine a file;
- the current user can write the data directory.

## Port 8765 is already in use

Use another port temporarily:

```powershell
py -3.11 appdock.py --port 8876
```

Do not start two AppDock instances against the same data directory for normal use.

## A manifest is ignored

Use **Add app → Local folder → Preview** for a specific validation error. Common causes:

- missing/invalid `id`;
- command is a string rather than an array;
- `cwd` escapes the app directory;
- invalid port;
- health URL is not loopback HTTP(S);
- malformed JSON;
- duplicate ID;
- symlink or path escape.

See [the manifest reference](APP_MANIFEST.md).

## An app will not start

Open its recent log tail and verify:

- the executable in `command[0]` exists or is on `PATH`;
- dependencies were installed according to the app README;
- `cwd` is correct;
- the configured port is not owned by an unrelated process;
- required app-owned environment/secrets exist outside the manifest;
- Windows execution policy or security software did not block the executable.

AppDock does not install dependencies automatically.

## App shows `running` but not `healthy`

`running` means a process exists. `healthy` requires a successful configured health URL.

- Open the health URL locally.
- Confirm the URL uses `127.0.0.1` or `::1` and the correct port/path.
- Make the endpoint cheap and return HTTP 2xx/3xx without authentication.
- Avoid redirecting health checks.

An app with no health URL can remain `running` rather than `healthy`.

## AppDock detects an app that it did not start

AppDock can observe an already-listening local app to avoid launching a duplicate. Such a row is marked externally running (`managed: false`). Stop behavior remains conservative because killing a process AppDock did not create can be unsafe.

## GitHub import fails

Check:

- Git is installed and available in PowerShell;
- the URL is exactly `https://github.com/owner/repository`;
- the repository is public;
- the root contains `appdock.json`;
- the checkout stays within AppDock's staging limits;
- the manifest contains no symlink/path escape;
- antivirus did not interrupt Git.

Clone manually and use Local Folder registration when you need credentials, a non-GitHub host, a branch/ref, or custom Git configuration.

## Preview works but registration fails

The source manifest or staging checkout may have changed after preview. Preview again and confirm the new digest. AppDock intentionally rejects stale previews.

A duplicate app ID or existing destination directory also blocks registration.

## Update check fails

Possible causes:

- no network access to GitHub;
- GitHub rate limiting or service interruption;
- no stable release exists yet;
- the configured update repository is invalid;
- a proxy/security product blocked the GitHub API.

A source/development checkout should use `git pull` rather than one-click update.

## Update staging fails

AppDock requires both `appdock-windows.zip` and `SHA256SUMS.txt` from the same release. It rejects:

- checksum mismatch;
- wrong repository paths;
- oversized downloads or expanded archives;
- too many archive entries;
- absolute, traversal, reserved-data, Windows device, ADS, or symlink paths;
- an archive without root `appdock.py`.

Do not bypass these checks; download and verify the release manually if diagnosis is needed.

## AppDock did not restart after an update

1. Read `%LOCALAPPDATA%\AppDock\runtime\update.log`.
2. Confirm the old process exited.
3. Follow [manual rollback](UPDATES.md#manual-rollback).
4. Preserve the data directory.
5. Report a sanitized bug if the helper failed after checksum/archive validation.

## Browser shows stale behavior

Hard-refresh the page after a restart. AppDock sends `Cache-Control: no-store`, so a persistent stale page can indicate that a different process is still listening on the port.

## Remote/private access does not work

First verify <http://127.0.0.1:8765/health> on the AppDock PC. Then inspect your authenticated private proxy independently. AppDock does not configure remote access and should not be exposed directly to the public internet.

## Preparing logs for a public issue

Remove:

- Windows user names and absolute paths;
- private IP addresses/hostnames;
- tokens, cookies, passwords, and environment values;
- private repository/app names;
- personal or client data printed by managed apps.

Use private vulnerability reporting for security-sensitive material.
