# Install AppDock on Windows

## Prerequisites

- Windows 10 or 11
- Python 3.11 or newer
- Git for the optional GitHub import workflow

Check them in PowerShell:

```powershell
py -3.11 --version
git --version
```

Python is available from [python.org](https://www.python.org/downloads/windows/) or the Microsoft Store. During a python.org install, enable the Python launcher.

## Install from a release

1. Open the [latest AppDock release](https://github.com/eWOOD29/appdock/releases/latest).
2. Download both `appdock-windows.zip` and `SHA256SUMS.txt` into the same folder.
3. Verify the archive:

```powershell
$expected = (Get-Content .\SHA256SUMS.txt | Where-Object { $_ -match 'appdock-windows\.zip$' }).Split()[0].ToLower()
$actual = (Get-FileHash .\appdock-windows.zip -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { throw "Checksum mismatch" }
"Checksum verified: $actual"
```

4. Extract the ZIP and enter the extracted folder.
5. Install for the current Windows user:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The default destination is `%LOCALAPPDATA%\Programs\AppDock`. No administrator access is required.

6. Open <http://127.0.0.1:8765>.

## Installer options

```powershell
# Install to a custom directory
.\scripts\install.ps1 -InstallDir 'D:\Tools\AppDock'

# Use a custom data directory
.\scripts\install.ps1 -DataDir 'D:\AppDockData'

# Start AppDock at Windows sign-in
.\scripts\install.ps1 -StartAtLogin

# Install but do not start now
.\scripts\install.ps1 -NoStart
```

The installer copies only program files. User data is created separately and is never copied into the installation directory.

## Portable mode

AppDock can run directly from the extracted release:

```powershell
py -3.11 .\appdock.py --data-dir "$env:LOCALAPPDATA\AppDock" --host 127.0.0.1 --port 8765
```

Portable mode still keeps user state outside the extracted folder unless you explicitly select another data directory.

## Source installation

```powershell
git clone https://github.com/eWOOD29/appdock.git
cd appdock
py -3.11 -m unittest discover -s tests -v
py -3.11 appdock.py
```

## Network access

AppDock defaults to loopback (`127.0.0.1`). That is the safest normal configuration. Do not bind AppDock to a public interface or expose it directly to the internet: it controls local processes.

If you need private access from another device, use an authenticated private-network proxy such as Tailscale and keep AppDock itself loopback-bound. AppDock validates the incoming `Host` header; add the proxy hostname explicitly before starting it:

```powershell
$env:APPDOCK_ALLOWED_HOSTS = 'appdock.your-private-domain.example'
py -3.11 appdock.py
```

Use a comma-separated list for multiple proxy hostnames. Do not add public or wildcard hosts. AppDock does not configure remote access for you.

## Upgrade

Use **Settings → Updates** in AppDock. Manual replacement is also supported: stop AppDock, install the new release over the program directory, and keep `%LOCALAPPDATA%\AppDock` unchanged.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\AppDock\scripts\uninstall.ps1"
```

By default, the uninstaller preserves user data. Pass `-RemoveUserData` only when you intentionally want to remove manifests, downloaded apps, logs, and settings.

## Troubleshooting

### `py -3.11` is not recognized

Install Python 3.11+ and reopen PowerShell. You can pass a full interpreter path to the installer with `-PythonExe`.

### Port 8765 is already in use

Run AppDock on another port:

```powershell
py -3.11 appdock.py --port 8876
```

### Windows Defender prompts

AppDock launches only commands from manifests that you register. Verify the repository and command before allowing a newly downloaded app. AppDock itself does not require an inbound firewall rule when bound to `127.0.0.1`.

### Update failed

Read `%LOCALAPPDATA%\AppDock\runtime\update.log` and see [Updates and rollback](UPDATES.md). AppDock stages a backup before replacing program files.
