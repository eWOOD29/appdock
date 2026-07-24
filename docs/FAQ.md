# Frequently asked questions

## Does AppDock install an app's dependencies?

No. AppDock clones or registers source, validates its manifest, and manages the declared process. It does not automatically run `pip install`, `npm install`, build scripts, migrations, or repository hooks. Follow the app repository's setup instructions first.

## Does registering an app run it?

No. Preview and registration never start application code. A registered app remains stopped until you explicitly press **Start**.

## Is a GitHub repository safe because AppDock accepts it?

No. The Advanced flow validates the URL, checkout paths, and manifest, but AppDock is not an antivirus scanner or code sandbox. Review the repository, command, and dependencies. Starting it grants the app your Windows account's normal permissions.

## Why does AppDock require `appdock.json`?

The manifest makes process behavior explicit and reviewable. AppDock does not guess how to run every repository and does not expose a general shell command box.

## Can I add an app that is already downloaded?

Yes. Add a root manifest, then use **Add app → Local folder**. AppDock stores a private proxy registration without moving the source folder.

## Can I add an app directly from GitHub?

Yes, through **Add app → Advanced: GitHub**. AppDock accepts canonical public GitHub HTTPS repository URLs, clones into a private staging directory, validates the root manifest, and asks you to review and confirm registration.

## Can AppDock manage an app that is already running?

When AppDock can safely identify the configured local listening port/process, it reports the external process and avoids launching a duplicate. Process ownership and Stop availability remain conservative.

## Does AppDock expose my app paths publicly?

Not by itself. Paths are stored under the private local data directory and shown in the local dashboard/API. Do not expose AppDock directly to the internet, publish generated proxy manifests, or paste unsanitized screenshots/logs into public issues.

## Does AppDock send telemetry?

No. The public core has no analytics or usage telemetry. It contacts GitHub only for explicit import/update features and probes health URLs declared by registered apps.

## Can I open AppDock from another device?

Keep AppDock bound to `127.0.0.1` and place an authenticated private-network proxy in front of it. Do not bind it to a public interface or expose it directly to the internet; it controls local processes.

## Does AppDock update the apps it manages?

No. The one-click updater updates AppDock itself. Update managed applications through their normal Git/package/release workflow, then restart them from AppDock.

## How does AppDock update itself?

It checks the configured official GitHub release, selects exact expected assets, verifies the release checksum, scans the archive, stages a backup, exits, replaces program files through an external helper, restarts, and rolls back on failure. Development checkouts should use `git pull` instead.

## Where is my data?

Windows defaults:

- program: `%LOCALAPPDATA%\Programs\AppDock`
- data: `%LOCALAPPDATA%\AppDock`

Use `--data-dir` or `APPDOCK_DATA_DIR` to override the data root.

## Does uninstalling delete my apps?

Not by default. The uninstaller removes program files and preserves the AppDock data directory. `-RemoveUserData` is an explicit destructive option; review it before use.

## Why is my manifest ignored?

Common causes:

- missing or unsafe app ID;
- command is not a non-empty JSON string array;
- `cwd` escapes the app folder;
- port is outside 1–65535;
- health URL is not a loopback HTTP(S) URL;
- invalid JSON or a symlink/path escape;
- duplicate app ID.

Use the Add App preview for a user-facing validation error.
