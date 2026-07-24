# Publishing an AppDock-ready app

A repository is AppDock-ready when a fresh checkout contains a valid `appdock.json` at its root and its README explains prerequisites without relying on one developer's computer.

## 1. Make the app portable

Before writing the manifest:

- remove absolute home-directory paths;
- keep runtime data in an ignored app-owned data directory;
- read ports and storage locations from arguments or environment variables when practical;
- keep dependencies inside the project (`.venv`, `node_modules`, containers, or an app-specific installer);
- provide a cheap health endpoint for HTTP services;
- do not require administrator rights unless the app genuinely needs them;
- never commit secrets or `.env` files.

## 2. Add `appdock.json`

Start from [`appdock.example.json`](../appdock.example.json) or a template in [`templates`](../templates).

```json
{
  "schema_version": 1,
  "id": "your-app",
  "name": "Your App",
  "description": "One clear sentence",
  "command": [".venv/Scripts/python.exe", "-m", "your_app"],
  "cwd": ".",
  "port": 8787,
  "health_url": "http://127.0.0.1:8787/health",
  "local_url": "http://127.0.0.1:8787/"
}
```

Use a stable lowercase ID. The command must be an argument array. Every path in a reusable repository manifest should be relative to the repository.

## 3. Document setup separately

AppDock intentionally does not run dependency installers or repository setup hooks. Your README should include exact clean-checkout setup commands.

Python example:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Node example:

```powershell
npm ci
```

If setup is optional, say why. If an app needs a model, database, credential, or external service, disclose it before the Add to AppDock section.

## 4. Add an AppDock section to the app README

```markdown
## Add to AppDock

1. Complete the setup above.
2. In AppDock, open **Add app → Advanced: GitHub**.
3. Paste `https://github.com/OWNER/REPOSITORY`.
4. Review the resolved command and paths, then register the app.
5. AppDock will not start it until you press **Start**.
```

For users who already cloned the repository, also mention **Add app → Local folder**.

## 5. Test from a clean checkout

Do not test only from your working folder.

1. Clone into a temporary directory.
2. Follow the README setup exactly.
3. Register the local folder in AppDock.
4. Confirm the preview shows only paths inside the temporary checkout.
5. Register but verify the app remains stopped.
6. Start it and wait for the configured health endpoint.
7. Stop it and verify the process exits.
8. Remove the temporary registration and checkout.

## 6. Keep personal configuration private

Never publish:

- generated AppDock proxy manifests from a user's registry directory;
- private-network hostnames or IP addresses;
- a developer's absolute file paths;
- tokens, cookies, passwords, client data, or `.env` files;
- logs or database files;
- machine-specific app ordering or update state.

Use placeholders in documentation and local ignored configuration for real values.

## 7. Version your app

AppDock does not currently update managed applications automatically. Use your repository's normal releases and document the supported upgrade command. A user can update a checkout, then restart the app from AppDock.

## Compatibility checklist

- [ ] Root `appdock.json` uses schema version 1.
- [ ] ID is stable, lowercase, and contains no spaces.
- [ ] Command is an array, not a shell string.
- [ ] Working directory cannot escape the checkout.
- [ ] No personal absolute paths or private URLs are committed.
- [ ] README documents prerequisites and setup.
- [ ] Registration does not need to run code.
- [ ] App remains stopped after registration.
- [ ] Start, health, stop, and restart work from a clean checkout.
