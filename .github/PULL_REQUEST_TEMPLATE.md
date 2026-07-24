## Summary

<!-- What changes, and why? -->

## Test plan

- [ ] `python -m unittest discover -s tests -v`
- [ ] `python -m compileall -q appdock.py tests scripts`
- [ ] `python scripts/privacy_scan.py`
- [ ] Live server tested with a disposable data directory
- [ ] Mobile layout checked if UI changed

## Security and privacy

- [ ] No credentials, personal paths, hostnames, private app names, logs, or generated registry/data files are included.
- [ ] New behavior does not run an app during preview or registration.
- [ ] Process, path, Git, archive, network, and update boundaries have rejection tests where relevant.

## Documentation

- [ ] User-facing behavior and compatibility changes are documented.
