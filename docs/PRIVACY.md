# Privacy

AppDock is designed to keep an app catalog and process state on the user's own computer.

## Data stored locally

Depending on enabled features, the AppDock data directory can contain:

- normalized registry manifests;
- absolute paths chosen by the user;
- repositories cloned through the Advanced GitHub flow;
- app ordering and settings;
- process IDs, health results, and bounded logs;
- update metadata, archives, backups, and update logs.
- optional LM Studio status snapshots and local operation results. Model and executable filesystem paths are not sent to the browser.

This data is not intended for source control. Default paths are excluded by the repository `.gitignore`.

## Network requests

AppDock makes network requests only for features the user invokes or configures:

- **Check for updates:** GitHub's releases API for the configured AppDock repository. Stable uses the latest-stable endpoint; an explicitly enabled Beta channel reads the Releases collection to find numbered prereleases.
- **Update now:** the expected release archive and checksum assets from GitHub.
- **Advanced GitHub import:** `git clone` of the public repository URL entered by the user.
- **Health checks:** URLs explicitly declared in registered manifests.
- **Optional LM Studio:** local `lms` CLI calls only; no network request is required for the integration.

AppDock does not include analytics, advertising, or usage telemetry.

GitHub may process ordinary connection metadata, including request IP and user agent, when AppDock checks GitHub Releases or downloads a confirmed release asset. AppDock does not add a separate analytics integration.

## Browser data

The dashboard is served by the local AppDock process. AppDock does not require a cloud account. Routine status/config responses omit absolute source, registry, installation, data, and log paths; an explicit registration preview still shows the selected app path so the user can review it. Avoid exposing the dashboard to the public internet because it controls local processes.

## Logs

AppDock records output from apps it starts. The content is controlled by those apps and may contain sensitive material if an app prints it. App authors and users should avoid logging secrets or personal records.

## Public bug reports

Before sharing a screenshot, manifest, log, or diagnostic output, remove:

- Windows user names and home paths;
- private IP addresses and network hostnames;
- repository names that are not public;
- email addresses, account identifiers, tokens, and cookies;
- app output containing personal or client data.

Use GitHub private vulnerability reporting for sensitive security findings.
