# Security policy

## Supported versions

Security fixes are applied to the latest released AppDock version. Until AppDock reaches 1.0, upgrades may also include documented compatibility changes.

## Report a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository when available. Do not open a public issue containing an exploit, credential, private path, or other sensitive data.

Include:

- affected AppDock version;
- Windows/Python version;
- minimal reproduction steps;
- security impact;
- whether untrusted repository content is required;
- suggested mitigation if known.

## Threat model

AppDock controls local processes. It is intended for one trusted Windows user on a private machine.

AppDock protects against several accidental or web-triggered hazards:

- manifest path traversal;
- shell-string command injection;
- unreviewed auto-start after registration;
- alternate-host or credential-bearing Git URLs;
- arbitrary updater URLs;
- ZIP traversal and symlink extraction;
- stopping protected Windows system PIDs;
- cross-origin JSON state-changing requests;
- user-data replacement during updates.

AppDock is **not**:

- a sandbox for untrusted applications;
- a multi-user authorization system;
- an internet-facing process manager;
- an antivirus scanner;
- a dependency or supply-chain verifier for imported repositories.

When you press **Start**, the selected app runs with your Windows account's permissions. Review the source, manifest, command, and dependencies first.

## Safe deployment

- Keep AppDock bound to `127.0.0.1`; non-loopback binds are rejected.
- For an authenticated private proxy, explicitly allow only its hostname with `APPDOCK_ALLOWED_HOSTS`. Never use wildcards or public hostnames.
- Never expose it directly to the public internet.
- Use authenticated private-network access if remote use is needed.
- Keep app secrets outside manifests and logs.
- Import only repositories you trust.
- Verify release checksums before manual installation.
- Back up the AppDock data directory.

## GitHub imports

The Advanced GitHub flow accepts only canonical public GitHub HTTPS repository URLs. Cloning does not run the application, package managers, hooks configured by AppDock, or setup scripts. A user must review and register the manifest, then explicitly start the app.

Git itself may honor machine-level configuration. Security-sensitive environments should audit their Git configuration or clone repositories manually before using local-folder registration.

## Update trust

AppDock update downloads are selected from the configured official GitHub repository release metadata. The archive must match the SHA-256 digest in the release's `SHA256SUMS.txt`, and extraction rejects unsafe entries. This verifies download integrity relative to the GitHub release; it does not replace the security of the repository owner's GitHub account.
