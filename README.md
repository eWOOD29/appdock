# AppDock

AppDock is a lightweight, local-first dashboard for organizing and running web apps, developer tools, and repositories on a Windows PC.

It discovers explicit `appdock.json` manifests, shows process and health state, preserves app order, captures bounded log tails, and provides safe start/stop/restart controls. Apps can be registered from an existing local folder or—through an Advanced flow—cloned from a public GitHub repository and reviewed before registration.

> AppDock never runs a newly added app automatically. Review the displayed command and trust the app's source before pressing **Start**.

## Highlights

- **Local-first:** AppDock binds to `127.0.0.1` by default and stores state on your computer.
- **Portable:** Python 3.11+ standard library only; no runtime packages to install.
- **Explicit registry:** only folders with valid manifests become apps.
- **Two onboarding paths:** register a downloaded folder or import a public GitHub repository from the Advanced view.
- **Preview before trust:** inspect the resolved directory, command, URLs, and manifest before registration.
- **Safe process launch:** argument arrays with `shell=False`; no arbitrary shell-command field.
- **Health-aware controls:** distinguish stopped, running, healthy, unhealthy, and crashed apps.
- **One-click updates:** check GitHub-hosted releases, verify the release's published SHA-256 checksum, stage a backup, and preserve user data.
- **Windows-friendly:** guided installer, optional startup shortcut, portable ZIP releases, CI, and rollback-oriented updates.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer (`py -3.11 --version`)
- Git only if you want AppDock to clone GitHub-hosted apps

The core server also runs on macOS and Linux, but the first supported installer and updater target is Windows.

## Quick start

### Recommended: Windows release

1. Download `appdock-windows.zip` and `SHA256SUMS.txt` from the [latest release](https://github.com/eWOOD29/appdock/releases/latest).
2. Verify the checksum as described in [Windows installation](docs/INSTALL_WINDOWS.md).
3. Extract the ZIP.
4. Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

5. Open <http://127.0.0.1:8765>.

### Run directly from a clone

```powershell
git clone https://github.com/eWOOD29/appdock.git
cd appdock
py -3.11 appdock.py --host 127.0.0.1 --port 8765
```

For an isolated test profile:

```powershell
py -3.11 appdock.py --data-dir "$env:TEMP\appdock-demo"
```

## Where AppDock stores data

Installation files and user data are separate so updates do not overwrite your apps or configuration.

| Purpose | Windows default |
|---|---|
| Installed program | `%LOCALAPPDATA%\Programs\AppDock` |
| User data | `%LOCALAPPDATA%\AppDock` |
| Registry manifests | `%LOCALAPPDATA%\AppDock\registry` |
| GitHub-installed apps | `%LOCALAPPDATA%\AppDock\apps` |
| Logs/order/update staging | `%LOCALAPPDATA%\AppDock\runtime` and related data folders |

Override the data directory with `--data-dir PATH` or `APPDOCK_DATA_DIR`. Do not commit the data directory to source control.

## Add an app

### Existing local folder

1. Add an `appdock.json` file to the app's root.
2. In AppDock, open **Add app** → **Local folder**.
3. Enter the folder path and choose **Preview**.
4. Review the resolved command and paths.
5. Choose **Register**. The app remains stopped until you explicitly start it.

### Public GitHub repository

1. Open **Add app** → **Advanced: GitHub**.
2. Paste a canonical public URL such as `https://github.com/owner/project`.
3. AppDock clones into a private staging directory and validates the root manifest.
4. Review the preview, then register it.
5. Press **Start** only after you trust the repository and command.

GitHub import accepts only public `https://github.com/<owner>/<repo>` URLs. It rejects credentials, query strings, fragments, SSH URLs, alternate hosts, and nested paths.

## Manifest example

```json
{
  "schema_version": 1,
  "id": "sample-app",
  "name": "Sample App",
  "description": "A small local web app",
  "command": [".venv/Scripts/python.exe", "-m", "sample_app"],
  "cwd": ".",
  "port": 8787,
  "health_url": "http://127.0.0.1:8787/health",
  "local_url": "http://127.0.0.1:8787"
}
```

Commands are JSON arrays, not shell strings. See the complete [manifest reference](docs/APP_MANIFEST.md).

## Make your own repository AppDock-ready

Add `appdock.json` at the repository root and include an **Add to AppDock** section in your README:

```markdown
## Add to AppDock

In AppDock, open **Add app → Advanced: GitHub**, paste this repository URL,
review the manifest and command, then register it.
```

A repository should also document its own prerequisites and setup. AppDock does not silently install dependencies or run setup scripts.

## Updates

Open **Settings → Updates → Check for updates**. If a newer GitHub release is available, AppDock shows the version and release notes. **Update now** downloads only the expected release asset, verifies `SHA256SUMS.txt`, checks the ZIP for unsafe paths, stages a backup, preserves your data directory, and restarts AppDock. See [Update design and recovery](docs/UPDATES.md).

## Documentation

- [Windows installation](docs/INSTALL_WINDOWS.md)
- [Migrating an existing setup](docs/MIGRATING.md)
- [Usage guide](docs/USAGE.md)
- [Manifest reference](docs/APP_MANIFEST.md)
- [Architecture and trust boundaries](docs/ARCHITECTURE.md)
- [Publishing AppDock-ready apps](docs/PUBLISHING_APPS.md)
- [Privacy](docs/PRIVACY.md)
- [FAQ](docs/FAQ.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Updates and rollback](docs/UPDATES.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## Development

```powershell
py -3.11 -m unittest discover -s tests -v
py -3.11 -m compileall -q appdock.py tests scripts
py -3.11 scripts/privacy_scan.py
py -3.11 scripts/check_docs.py
node --check static/app.js
```

Run the development server with disposable data:

```powershell
py -3.11 appdock.py --data-dir "$env:TEMP\appdock-dev" --port 8765
```

## Security model

AppDock is a local control plane, not a sandbox. Starting an app runs its declared executable with your Windows account's permissions. AppDock reduces accidental command injection and path confusion, but it cannot make untrusted code safe. Read [SECURITY.md](SECURITY.md) before importing repositories you do not control.

## License

[MIT](LICENSE)
