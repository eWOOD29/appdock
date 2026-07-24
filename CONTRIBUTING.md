# Contributing to AppDock

Thanks for helping improve AppDock.

## Before opening an issue

- Search existing issues and pull requests.
- Use a concise title and a minimal reproduction.
- Remove credentials, personal paths, hostnames, app names, screenshots with private data, and logs that contain secrets.
- For vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of filing a public issue.

## Development setup

Requirements: Python 3.11+; Git on Windows for GitHub-import integration tests.

```powershell
git clone https://github.com/eWOOD29/appdock.git
cd appdock
py -3.11 -m unittest discover -s tests -v
py -3.11 appdock.py --data-dir "$env:TEMP\appdock-dev"
```

AppDock intentionally uses only the Python standard library at runtime. Discuss new runtime dependencies before adding them.

## Pull requests

1. Fork and create a focused branch.
2. Write a failing behavior test before production code.
3. Keep generated user data outside the repository.
4. Run all tests and compile checks.
5. Exercise the live server with a disposable data directory.
6. Explain security implications for manifest, process, network, import, or update changes.
7. Keep public examples generic.

## Privacy checklist

Before committing, search for:

- home-directory paths and user names;
- private IPs, tailnet/MagicDNS names, and local hostnames;
- email addresses and account IDs;
- OAuth/API tokens, cookies, passwords, and `.env` content;
- private app/repository names;
- runtime logs, order files, downloaded apps, and generated registry manifests.

## Code style

- Python 3.11+ with type hints for public interfaces.
- Prefer small standard-library components and explicit data structures.
- Launch processes with argument arrays and `shell=False`.
- Resolve and validate paths before file operations.
- Bound network reads, request bodies, log tails, and archive extraction.
- Return errors that help the user without exposing environment contents or secrets.

## Tests

```powershell
py -3.11 -m unittest discover -s tests -v
py -3.11 -m compileall -q appdock.py tests scripts
```

Security-sensitive additions should include rejection tests, not only happy paths.
