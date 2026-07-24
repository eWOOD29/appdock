# Architecture

AppDock is a single-user, local-first control plane for explicitly registered applications. The first public release uses Python's standard library to keep installation, startup, and update behavior inspectable.

## Process boundaries

```text
Browser on localhost/private authenticated proxy
        |
        | HTTP JSON + same-origin POSTs
        v
AppDock server (loopback by default)
        |
        +-- private registry manifests
        +-- child processes started with argv + shell=False
        +-- loopback health probes
        +-- bounded app log tails
        +-- GitHub release checks / explicit imports
        +-- external update helper for stop-replace-restart
```

AppDock is not a sandbox. A child application runs as the current Windows user. The safety model prevents accidental shell/path confusion and requires explicit user approval; it does not make untrusted code safe.

## Installation and data separation

Program files and mutable user state have different roots.

- **Program root:** versioned AppDock source, static assets, scripts, and documentation.
- **Data root:** registry, downloaded apps, staging, ordering, logs, settings, update archives, and backups.

An update may replace program files but must not write through, delete, or treat the data root as installation content. The installer and updater reject overlapping roots.

## Registry model

Only explicit manifests become apps. A private registry entry contains a normalized manifest and points at either:

- an existing folder selected by the user; or
- a repository moved from AppDock's GitHub staging area into its managed app-install root.

The registry owns ordering and process metadata; an app repository owns its source, dependencies, and runtime data.

App IDs are strict and stable because they become registry directory names, log names, route identifiers, and ordering keys.

## Onboarding state machine

```text
input path or GitHub URL
        |
        v
validate source + normalize manifest
        |
        v
preview (safe fields + digest; no execution)
        |
        +-- cancel / cleanup
        |
        v
explicit confirmation with matching digest
        |
        v
atomic private registry write
        |
        v
registered but stopped
```

Registration recomputes the source manifest digest. A changed manifest, staging path, repository URL, or confirmation token invalidates the preview.

### Local folders

The source folder remains in place. AppDock writes a private proxy manifest containing the user-selected absolute directory. That generated proxy is never part of the public AppDock repository.

### GitHub repositories

The Advanced flow accepts canonical public GitHub HTTPS repository URLs only. Clone uses an argument list and `shell=False`, writes into a unique private staging directory, and does not run setup hooks, package managers, or application code. The root manifest and checkout tree are validated before the user can register it.

## Process lifecycle

- **Start:** resolve a registered spec, detect an already-listening app when possible, then launch argv with `shell=False` if needed.
- **Status:** separate process existence from HTTP health.
- **Stop:** terminate only a safely identified managed process tree; never target protected system PIDs.
- **Restart:** stop, then start.
- **Logs:** capture stdout/stderr only for AppDock-managed launches and expose a bounded tail.

Status values are `stopped`, `running`, `healthy`, `unhealthy`, and `crashed`.

## HTTP boundary

The dashboard defaults to `127.0.0.1`. State-changing routes require:

- same-origin requests when an `Origin` header is present;
- `Content-Type: application/json`;
- a bounded object-shaped request body;
- strict app IDs and route actions;
- explicit preview/update confirmation digests.

Responses use no-store and browser hardening headers. User-controlled labels, descriptions, URLs, paths, logs, and release notes are rendered as text or escaped attributes, not executable markup.

## Update trust chain

```text
configured GitHub repository
        |
        v
latest release metadata + SemVer comparison
        |
        v
exact expected archive/checksum assets
        |
        v
bounded download + SHA-256 verification
        |
        v
ZIP path/type/size validation
        |
        v
private staging + explicit confirmation
        |
        v
external helper waits for AppDock exit
        |
        v
backup -> replace program files -> restart
        |
        +-- failure -> rollback backup
```

The browser cannot provide an arbitrary update asset URL. Both assets must come from the configured repository's release. The checksum proves integrity relative to that GitHub release; it is not an independent signature.

## Extension principles

Future integrations should remain optional and disabled by default. They must:

- store machine-specific configuration only in the private data directory;
- avoid adding public paths, hostnames, account IDs, or secrets to the repository;
- expose a bounded, testable interface;
- preserve loopback binding and explicit user actions;
- fail closed without breaking the core registry.
