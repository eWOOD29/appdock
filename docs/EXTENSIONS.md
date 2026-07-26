# Private extensions

AppDock 0.1.1 supports a small, local-only extension boundary for visibility and read-only dashboard widgets. Concrete app IDs, provider ports, private links, hardware names, account data, and adapters belong in the persistent AppDock data root or another protected local directory, never in the program directory or release archive.

## File location and ownership

The default configuration file is `<APPDOCK_DATA_DIR>/private/extensions.json`. It is preserved across update, reinstall, rollback, and uninstall because the program directory and data directory must not overlap.

Use only schema version 1:

```json
{
  "schema_version": 1,
  "visibility": {
    "hidden_app_ids": ["synthetic-backend"]
  },
  "providers": [
    {
      "id": "synthetic-provider",
      "url": "http://127.0.0.1:19091/widgets",
      "connect_timeout_ms": 300,
      "read_timeout_ms": 700,
      "cache_seconds": 5
    }
  ],
  "widgets": [
    {
      "id": "synthetic-metrics",
      "type": "metrics",
      "title": "Example metrics",
      "provider_id": "synthetic-provider",
      "drill_down_url": "https://private.example.invalid/details"
    }
  ]
}
```

The example values are deliberately synthetic. Provider URLs must use `http` with an explicit port and the literal loopback host `127.0.0.1` or `::1`. DNS names, credentials, redirects, remote hosts, fragments, oversized responses, unsupported content types, and malformed data are rejected.

## Provider response

A provider is a separate read-only process controlled outside AppDock. It returns bounded JSON only:

```json
{
  "schema_version": 1,
  "widgets": {
    "synthetic-metrics": {
      "status": "ok",
      "metrics": [
        {"label": "Example", "value": "42%"}
      ],
      "timestamp": "recent"
    }
  }
}
```

Progress widgets use `progress` rows with a numeric `value` from `0` to `1` and an optional bounded `reset_at` string. Providers cannot supply titles, links, markup, scripts, styles, commands, paths, or executable behavior. Drill-down URLs are static values in the protected private configuration and are validated by the public core.

Provider failure is isolated. App discovery, ordering, lifecycle controls, logs, onboarding, and updates continue to work; the affected widget renders as unavailable.

## Visibility and ordering

Visibility is keyed by validated app ID and is independent from the saved order. A hidden registration remains discoverable to AppDock and remains in `app-order.json`; it is simply omitted from the normal card list. Moving visible cards swaps only visible positions and does not delete or expose hidden entries.

## Local and private app links

Manifests may contain validated `local_url` and `private_url` values. The browser presents the local link first when AppDock is opened over loopback and the private link first in other approved contexts. Actual URLs remain in private manifests or configuration.
