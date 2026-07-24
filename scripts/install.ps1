param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AppDock'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'AppDock'),
    [string]$PythonExe = '',
    [switch]$StartAtLogin,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'path_safety.ps1')

$InstallDir = Assert-AppDockSafePath -Path $InstallDir -Role InstallDir -SourceRoot $SourceRoot
$DataDir = Assert-AppDockSafePath -Path $DataDir -Role DataDir -SourceRoot $SourceRoot
if (Test-AppDockPathOverlap $InstallDir $DataDir) {
    throw 'InstallDir and DataDir must not overlap so updates and uninstall cannot overwrite user data.'
}

$RequiredSourceFiles = @(
    'appdock.py',
    'static\app.js',
    'static\app.css',
    'scripts\install.ps1',
    'scripts\uninstall.ps1',
    'scripts\path_safety.ps1',
    'scripts\update_helper.py'
)
foreach ($Relative in $RequiredSourceFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot $Relative) -PathType Leaf)) {
        throw "The AppDock source is incomplete: $Relative is missing."
    }
}

$IncomingReleaseManifest = Join-Path $SourceRoot 'RELEASE-MANIFEST.json'
if (Test-Path -LiteralPath $IncomingReleaseManifest -PathType Leaf) {
    try {
        $Manifest = Get-Content -Raw -LiteralPath $IncomingReleaseManifest | ConvertFrom-Json
    } catch {
        throw 'The incoming RELEASE-MANIFEST.json is invalid.'
    }
    if ($Manifest.schema_version -notin @(1, 2) -or -not $Manifest.files) {
        throw 'The incoming RELEASE-MANIFEST.json is invalid.'
    }
    foreach ($Entry in $Manifest.files) {
        $Relative = $Entry.path
        if (-not ($Relative -is [string]) -or -not ($Entry.sha256 -is [string])) {
            throw 'The incoming release inventory contains an invalid entry.'
        }
        $Parts = $Relative -split '/'
        if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains('\') -or ($Parts | Where-Object { $_ -in @('', '.', '..') })) {
            throw 'The incoming release inventory contains an unsafe path.'
        }
        $Source = [System.IO.Path]::GetFullPath((Join-Path $SourceRoot $Relative))
        $SourcePrefix = $SourceRoot.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
        if (-not $Source.StartsWith($SourcePrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $Source -PathType Leaf)) {
            throw "The incoming release inventory is missing $Relative."
        }
        $Actual = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Actual -ne $Entry.sha256.ToLowerInvariant()) {
            throw "The incoming release inventory checksum failed for $Relative."
        }
    }
}

if ($PythonExe) {
    $ResolvedPython = (Resolve-Path $PythonExe).Path
    & $ResolvedPython --version | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'The selected Python interpreter did not start.' }
    $PythonLine = '"' + $ResolvedPython + '"'
} else {
    & py -3.11 --version | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw 'Python 3.11+ was not found. Install Python, or pass -PythonExe with a full path.'
    }
    $PythonLine = 'py -3.11'
}

$InstallParent = Split-Path -Parent $InstallDir
$InstallName = Split-Path -Leaf $InstallDir
New-Item -ItemType Directory -Force -Path $InstallParent, $DataDir | Out-Null
$StageDir = Join-Path $InstallParent ('.' + $InstallName + '.stage-' + [Guid]::NewGuid().ToString('N'))
$BackupDir = Join-Path $InstallParent ('.' + $InstallName + '.backup-' + [Guid]::NewGuid().ToString('N'))

$AllowedFiles = @(
    'appdock.py', 'appdock.example.json', 'pyproject.toml',
    'README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md', 'LICENSE',
    'RELEASE-MANIFEST.json'
)
$AllowedDirectories = @('appdock', 'appdock_core', 'static', 'docs', 'scripts', 'templates')

try {
    New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
    foreach ($Name in $AllowedFiles) {
        $Source = Join-Path $SourceRoot $Name
        if (Test-Path -LiteralPath $Source -PathType Leaf) {
            Copy-Item -LiteralPath $Source -Destination $StageDir -Force
        }
    }
    foreach ($Name in $AllowedDirectories) {
        $Source = Join-Path $SourceRoot $Name
        if (Test-Path -LiteralPath $Source -PathType Container) {
            Copy-Item -LiteralPath $Source -Destination (Join-Path $StageDir $Name) -Recurse -Force
        }
    }

    $Launcher = Join-Path $StageDir 'run-appdock.cmd'
    $LauncherContent = @"
@echo off
setlocal
set "APPDOCK_DATA_DIR=$DataDir"
cd /d "$InstallDir"
$PythonLine "$InstallDir\appdock.py" --host 127.0.0.1 --port 8765
"@
    [System.IO.File]::WriteAllText($Launcher, $LauncherContent, [System.Text.UTF8Encoding]::new($false))

    $StageManifest = Join-Path $StageDir 'RELEASE-MANIFEST.json'
    if (-not (Test-Path -LiteralPath $StageManifest -PathType Leaf)) {
        $InventoryFiles = @(Get-ChildItem -LiteralPath $StageDir -Recurse -File | Where-Object {
            $_.FullName -ne $Launcher -and $_.FullName -ne $StageManifest
        } | ForEach-Object {
            $Relative = $_.FullName.Substring($StageDir.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
            @{path=$Relative; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
        })
        $Inventory = @{schema_version=2; files=$InventoryFiles} | ConvertTo-Json -Depth 5
        [System.IO.File]::WriteAllText($StageManifest, $Inventory, [System.Text.UTF8Encoding]::new($false))
    }
    Assert-AppDockInstallMarker -Path $StageDir | Out-Null

    if (Test-Path -LiteralPath $InstallDir) {
        $ExistingItems = @(Get-ChildItem -LiteralPath $InstallDir -Force)
        if ($ExistingItems.Count -gt 0) {
            Assert-AppDockInstallMarker -Path $InstallDir | Out-Null
            Get-CimInstance Win32_Process | Where-Object {
                $_.Name -match '^python(w)?\.exe$' -and
                (Test-AppDockOwnedProcessCommandLine -CommandLine $_.CommandLine -InstallDir $InstallDir)
            } | ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
            Move-Item -LiteralPath $InstallDir -Destination $BackupDir
        } else {
            Remove-Item -LiteralPath $InstallDir -Force
        }
    }

    try {
        Move-Item -LiteralPath $StageDir -Destination $InstallDir
    } catch {
        if (Test-Path -LiteralPath $InstallDir) {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force
        }
        if ((Test-Path -LiteralPath $BackupDir -PathType Container) -and -not (Test-Path -LiteralPath $InstallDir)) {
            Move-Item -LiteralPath $BackupDir -Destination $InstallDir
        }
        throw
    }

    if (Test-Path -LiteralPath $BackupDir) {
        Remove-Item -LiteralPath $BackupDir -Recurse -Force
    }
} finally {
    if (Test-Path -LiteralPath $StageDir) {
        Remove-Item -LiteralPath $StageDir -Recurse -Force
    }
}

$Launcher = Join-Path $InstallDir 'run-appdock.cmd'
if ($StartAtLogin) {
    $Startup = [Environment]::GetFolderPath('Startup')
    $ShortcutPath = Join-Path $Startup 'AppDock.lnk'
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Launcher
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.Description = 'Start AppDock at Windows sign-in'
    $Shortcut.Save()
    Write-Host "Created startup shortcut: $ShortcutPath"
}

Write-Host "Installed AppDock to: $InstallDir"
Write-Host "User data directory: $DataDir"
Write-Host 'Dashboard: http://127.0.0.1:8765'

if (-not $NoStart) {
    Start-Process -FilePath $Launcher -WorkingDirectory $InstallDir
    Write-Host 'AppDock is starting.'
}
