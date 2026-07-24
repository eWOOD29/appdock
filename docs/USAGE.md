# AppDock usage guide

## Dashboard

The dashboard lists registered apps in your saved order. Each row shows:

- lifecycle state: `stopped`, `running`, `healthy`, `unhealthy`, or `crashed`;
- process ID when AppDock can safely identify it;
- configured port and health result;
- local and optional private-network links;
- Start, Stop, Restart, and ordering controls;
- a bounded recent log tail for AppDock-managed launches.

AppDock polls conservatively. A running process is not considered healthy unless its configured health URL responds successfully.

## Add a local app

1. Put an `appdock.json` manifest in the app's root directory.
2. Open **Add app**.
3. Choose **Local folder**.
4. Paste or type the absolute folder path.
5. Select **Preview**.
6. Verify the app ID, source directory, working directory, exact command argument list, URLs, and port.
7. Select **Register**.

Registration creates a normalized proxy manifest in AppDock's private registry. It does not modify the original app and does not launch it.

If the app moves later, remove the registration and add it again from its new location.

## Import from GitHub (Advanced)

GitHub import is intentionally separated from routine local registration because cloning code adds a trust boundary.

1. Open **Add app** → **Advanced: GitHub**.
2. Paste `https://github.com/owner/repository`.
3. Select **Clone and preview**.
4. AppDock clones into its staging directory without running repository code.
5. AppDock requires `appdock.json` at the repository root and validates it.
6. Review the exact command and resolved paths.
7. Select **Register** to move the staged checkout into the configured app-install directory.
8. Start the app only after completing any documented prerequisites and deciding you trust it.

AppDock does not run `pip install`, `npm install`, build scripts, hooks, or setup commands automatically. A manifest should point to a command that becomes valid after the repository's documented setup.

## Remove an app

The first public release intentionally has no destructive **Delete source** button.

1. Stop the app from AppDock if AppDock launched it.
2. Close AppDock.
3. Delete `%LOCALAPPDATA%\AppDock\registry\<app-id>` to unregister it.
4. If AppDock cloned the repository and you also want to delete that checkout, separately review and delete `%LOCALAPPDATA%\AppDock\apps\<app-id>`.
5. Start AppDock again.

Never delete the original source folder for a locally registered app unless you independently intend to remove that project. Removing a registry entry does not require deleting local source code.

## Start and stop behavior

- **Start** uses the manifest's argument array with `shell=False`.
- If the configured port is already occupied, AppDock reports the listener and refuses to launch a duplicate process.
- **Stop** terminates only a process tree that AppDock launched and still tracks. It never kills an external process merely because that process owns the declared port.
- **Restart** stops an AppDock-managed process and starts it again. If an external listener owns the port, no external process is terminated.
- A nonzero exit caused by an intentional stop is shown as stopped, not crashed.

AppDock is not a permissions sandbox. The child process has your Windows account's normal access.

## Logs

AppDock redirects stdout and stderr from processes it starts into bounded app-local/runtime logs. It does not automatically capture output from apps launched outside AppDock.

Do not print passwords, API keys, access tokens, personal records, or other secrets to application logs.

## Reorder apps

Use the adjacent up/down controls. AppDock stores only app IDs in its order file, writes it atomically, ignores removed IDs, and appends new IDs without disturbing the existing order.

## Data-directory override

Command line:

```powershell
py -3.11 appdock.py --data-dir 'D:\AppDockData'
```

Environment variable:

```powershell
$env:APPDOCK_DATA_DIR = 'D:\AppDockData'
py -3.11 appdock.py
```

The command-line value takes precedence.

## Updates

1. Open **Settings** → **Updates**.
2. Select **Check for updates**.
3. Read the target version and release notes.
4. Select **Update now** and confirm.

AppDock accepts update files only from the configured official GitHub repository release, validates the expected asset names and checksum, scans ZIP paths, backs up the current program files, applies the release, and restarts. User data stays in the separate data directory.

## Backups

Back up your AppDock data directory to preserve:

- registry manifests;
- AppDock-downloaded repositories;
- ordering and settings;
- runtime logs when useful.

The default Windows data directory is `%LOCALAPPDATA%\AppDock`.

## App authors

To make a repository easy to add:

1. commit a valid root `appdock.json`;
2. use relative command and working-directory paths;
3. include a cheap health endpoint for HTTP apps;
4. document prerequisite installation separately;
5. never put secrets or user-specific absolute paths in the manifest;
6. test registration from a clean checkout;
7. state clearly that AppDock does not auto-run or auto-install dependencies.
