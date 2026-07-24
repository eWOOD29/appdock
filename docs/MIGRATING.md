# Migrating an existing AppDock setup

Use this process when moving from an earlier folder-based AppDock installation to the public release. It keeps the old dashboard available until the new one is proven.

## Principles

- Do not move application source folders just to migrate AppDock.
- Do not publish existing generated proxy manifests; they can contain private absolute paths and network URLs.
- Keep the old AppDock process and startup launcher until the new installation passes a live verification.
- Run the new build on a temporary port and data directory first.
- Registration must not start applications or change their existing startup behavior.

## 1. Back up old state

Back up:

- existing `appdock.json` manifests;
- the AppDock order file;
- startup launchers;
- any AppDock-specific configuration;
- the list of current local/private URLs and health endpoints.

Do not copy logs or secrets into a public repository.

## 2. Start the public build in parallel

Choose a disposable data directory and unused port:

```powershell
py -3.11 appdock.py --data-dir "$env:TEMP\appdock-migration" --port 8876
```

Open <http://127.0.0.1:8876>. The existing AppDock can continue using port 8765.

## 3. Register existing folders

For each valid application or legacy proxy folder:

1. Open **Add app → Local folder**.
2. Select the folder containing its `appdock.json`.
3. Review the resolved source directory, working directory, command, port, health URL, and links.
4. Register it.
5. Verify it remains stopped in the new AppDock.

Legacy `tailscale_url` values are accepted as private-link aliases and normalized into private AppDock user state. Do not copy those values into reusable public manifests.

Invalid/incomplete manifests are ignored. Fix them at their source or leave them unregistered; do not loosen AppDock's validator to preserve broken entries.

## 4. Verify without duplicating apps

For applications already running outside the new AppDock:

- confirm their existing port is detected;
- confirm the new dashboard reports them as externally running/healthy when possible;
- press **Start** only on a disposable test app and verify no duplicate process is created;
- verify Stop is offered only for a safely identified target;
- exercise ordering, links, logs, health state, and mobile layout.

## 5. Install the release

Install into the normal per-user program directory with a permanent data root. Re-register or copy only the normalized private data created during the test after verifying its paths.

Do not overlap the program and data directories.

## 6. Switch startup atomically

1. Record the old AppDock startup launcher's exact contents.
2. Stop only the old AppDock process.
3. Replace or disable only the old **AppDock** launcher.
4. Leave unrelated application/Hermes startup launchers untouched.
5. Start the public AppDock on the original port.
6. Verify local health and any existing authenticated private proxy route.
7. Restore the old launcher immediately if verification fails.

## 7. Keep a rollback window

Keep the old program folder and launcher backup until the public build has survived normal use. User data and app source folders should remain untouched by rollback.

## Compatibility notes

- Public AppDock stores mutable state outside the repository and installation directory.
- Machine-specific telemetry/widgets from a private build are not enabled in the public core by default. Treat them as optional local integrations and verify that loss before replacing a live dashboard.
- A source/development checkout should be updated with Git, not the one-click release updater.
