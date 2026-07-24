param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AppDock'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'AppDock'),
    [string]$PythonExe = '',
    [switch]$StartAtLogin,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$DataDir = [System.IO.Path]::GetFullPath($DataDir)

function Test-PathOverlap([string]$Left, [string]$Right) {
    $LeftRoot = $Left.TrimEnd([char[]]@('\', '/'))
    $RightRoot = $Right.TrimEnd([char[]]@('\', '/'))
    $Comparison = [System.StringComparison]::OrdinalIgnoreCase
    return $LeftRoot.Equals($RightRoot, $Comparison) -or
        $LeftRoot.StartsWith($RightRoot + [System.IO.Path]::DirectorySeparatorChar, $Comparison) -or
        $RightRoot.StartsWith($LeftRoot + [System.IO.Path]::DirectorySeparatorChar, $Comparison)
}

if (Test-PathOverlap $InstallDir $DataDir) {
    throw 'InstallDir and DataDir must not overlap so updates and uninstall cannot overwrite user data.'
}

$PreviousManagedFiles = @()
$ExistingReleaseManifest = Join-Path $InstallDir 'RELEASE-MANIFEST.json'
if (Test-Path $ExistingReleaseManifest -PathType Leaf) {
    try {
        $PreviousManagedFiles = @((Get-Content -Raw $ExistingReleaseManifest | ConvertFrom-Json).files | ForEach-Object { $_.path })
    } catch {
        throw 'The existing RELEASE-MANIFEST.json is invalid; refusing to remove managed files.'
    }
}
$IncomingReleaseManifest = Join-Path $SourceRoot 'RELEASE-MANIFEST.json'
$IncomingManagedFiles = @()
if (Test-Path $IncomingReleaseManifest -PathType Leaf) {
    try {
        $IncomingManagedFiles = @((Get-Content -Raw $IncomingReleaseManifest | ConvertFrom-Json).files | ForEach-Object { $_.path })
    } catch {
        throw 'The incoming RELEASE-MANIFEST.json is invalid.'
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

New-Item -ItemType Directory -Force -Path $InstallDir, $DataDir | Out-Null

$AllowedFiles = @(
    'appdock.py', 'appdock.example.json', 'pyproject.toml',
    'README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md', 'LICENSE',
    'RELEASE-MANIFEST.json'
)
foreach ($Name in $AllowedFiles) {
    $Source = Join-Path $SourceRoot $Name
    if (Test-Path $Source) { Copy-Item -Force $Source $InstallDir }
}

$AllowedDirectories = @('appdock', 'appdock_core', 'static', 'docs', 'scripts', 'templates')
foreach ($Name in $AllowedDirectories) {
    $Source = Join-Path $SourceRoot $Name
    if (Test-Path $Source) {
        $Destination = Join-Path $InstallDir $Name
        if (Test-Path $Destination) { Remove-Item -Recurse -Force $Destination }
        Copy-Item -Recurse -Force $Source $Destination
    }
}

if (-not (Test-Path (Join-Path $InstallDir 'appdock.py'))) {
    throw 'The release does not contain appdock.py.'
}

if ($IncomingManagedFiles.Count -gt 0) {
    $InstallPrefix = $InstallDir.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
    foreach ($Relative in $PreviousManagedFiles) {
        if (-not ($Relative -is [string]) -or $IncomingManagedFiles -contains $Relative) { continue }
        $Parts = $Relative -split '/'
        if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative.Contains('\') -or ($Parts | Where-Object { $_ -in @('', '.', '..') })) {
            throw 'The existing release inventory contains an unsafe path.'
        }
        $Obsolete = [System.IO.Path]::GetFullPath((Join-Path $InstallDir $Relative))
        if (-not $Obsolete.StartsWith($InstallPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'The existing release inventory escapes InstallDir.'
        }
        if (Test-Path $Obsolete -PathType Leaf) {
            Remove-Item -Force $Obsolete
        } elseif (Test-Path $Obsolete) {
            throw 'An obsolete managed path is not a regular file.'
        }
    }
}

$Launcher = Join-Path $InstallDir 'run-appdock.cmd'
$LauncherContent = @"
@echo off
setlocal
set "APPDOCK_DATA_DIR=$DataDir"
cd /d "$InstallDir"
$PythonLine appdock.py --host 127.0.0.1 --port 8765
"@
[System.IO.File]::WriteAllText($Launcher, $LauncherContent, [System.Text.UTF8Encoding]::new($false))

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
