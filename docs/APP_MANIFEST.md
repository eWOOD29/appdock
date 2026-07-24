# `appdock.json` manifest reference

An AppDock manifest describes one local application. It is data, not a shell script.

## Minimal manifest

```json
{
  "schema_version": 1,
  "id": "sample-app",
  "name": "Sample App",
  "command": ["python", "-m", "sample_app"]
}
```

## Full example

```json
{
  "schema_version": 1,
  "id": "sample-app",
  "name": "Sample App",
  "description": "A local sample service",
  "command": [".venv/Scripts/python.exe", "-m", "sample_app", "--port", "8787"],
  "cwd": ".",
  "port": 8787,
  "health_url": "http://127.0.0.1:8787/health",
  "local_url": "http://127.0.0.1:8787/",
  "private_url": "",
  "stop_timeout": 5
}
```

## Fields

| Field | Required | Type | Meaning |
|---|---:|---|---|
| `schema_version` | recommended | integer | Manifest schema. Use `1`. |
| `id` | yes | string | Stable lowercase ID. Use letters, digits, and hyphens; maximum length is enforced. |
| `name` | yes | string | Human-readable name. |
| `description` | no | string | Short dashboard description. |
| `command` | yes | array of strings | Executable followed by arguments. Shell strings are rejected. |
| `cwd` | no | string | Working directory relative to the app directory. Defaults to `.`. |
| `port` | no | integer | Local listening port, 1–65535. |
| `health_url` | no | URL | Cheap local HTTP endpoint used for health checks. |
| `local_url` | no | URL | Link opened from the dashboard. |
| `private_url` | no | URL | Optional authenticated private-network link. Do not publish personal hostnames in reusable manifests. |
| `stop_timeout` | no | number | Grace period before forced termination, within AppDock's allowed bounds. |
| `env` | no | object | Small environment overrides. Do not put secrets here. Prefer an app-owned ignored `.env` or OS credential store. |

AppDock may normalize legacy `tailscale_url` to the generic private URL field for compatibility.

## Path rules

For a repository manifest:

- `cwd` must resolve to the repository root or a descendant;
- relative command paths resolve in that working directory at process launch;
- `..` must not escape the app directory;
- symlink-based escapes are rejected during GitHub onboarding;
- a reusable manifest must not contain a user-specific absolute directory.

When you register an existing folder, AppDock stores the absolute path in a private proxy manifest under your data directory. That generated proxy is user state and should never be committed.

## Command rules

Correct:

```json
"command": [".venv/Scripts/python.exe", "-m", "my_app", "--port", "8787"]
```

Rejected:

```json
"command": "python -m my_app && start http://localhost:8787"
```

AppDock calls the operating system with an argument array and `shell=False`. Do not use `cmd /c`, PowerShell command strings, or shell operators to bypass this model.

## Python app example

```json
{
  "schema_version": 1,
  "id": "notes",
  "name": "Local Notes",
  "description": "Private notes app",
  "command": [".venv/Scripts/python.exe", "-m", "notes_app"],
  "cwd": ".",
  "port": 8810,
  "health_url": "http://127.0.0.1:8810/health",
  "local_url": "http://127.0.0.1:8810/"
}
```

## Node app example

```json
{
  "schema_version": 1,
  "id": "local-kanban",
  "name": "Local Kanban",
  "command": ["node", "server.js", "--port", "8820"],
  "cwd": ".",
  "port": 8820,
  "health_url": "http://127.0.0.1:8820/health",
  "local_url": "http://127.0.0.1:8820/"
}
```

The repository README should tell users to run `npm install` themselves before starting. AppDock intentionally does not run package-manager install hooks.

## External proxy manifests

AppDock generates external proxy manifests when a user registers an existing folder. A proxy may contain:

```json
{
  "schema_version": 1,
  "id": "existing-app",
  "name": "Existing App",
  "external": true,
  "directory": "C:\\path\\chosen-by-the-user\\existing-app",
  "command": [".venv/Scripts/python.exe", "app.py"],
  "cwd": "."
}
```

This file belongs only in the user's AppDock data directory. Never copy a generated proxy manifest into a public repository.

## Secrets

Do not store API keys, passwords, OAuth tokens, cookies, private keys, or personal records in a manifest. A manifest can be displayed in the browser, copied into a private registry, and inspected for troubleshooting.
