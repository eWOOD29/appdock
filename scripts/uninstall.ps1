param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AppDock'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'AppDock'),
    [switch]$RemoveUserData
)

$ErrorActionPreference = 'Stop'
$BootstrapRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd([char[]]@('\', '/'))
$RequestedInstall = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd([char[]]@('\', '/'))
if (-not $BootstrapRoot.Equals($RequestedInstall, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to uninstall from a script outside the requested AppDock installation.'
}
$BootstrapManifestPath = Join-Path $BootstrapRoot 'RELEASE-MANIFEST.json'
$BootstrapSafetyPath = Join-Path $PSScriptRoot 'path_safety.ps1'
$BootstrapUninstallerPath = Join-Path $PSScriptRoot 'uninstall.ps1'
if (-not (Test-Path -LiteralPath $BootstrapManifestPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $BootstrapSafetyPath -PathType Leaf)) {
    throw 'Refusing to uninstall without a release inventory and path-safety module.'
}
try {
    $BootstrapManifest = Get-Content -Raw -LiteralPath $BootstrapManifestPath | ConvertFrom-Json
} catch {
    throw 'Refusing to uninstall with an invalid release inventory.'
}
foreach ($BootstrapFile in @(
    @{path='scripts/path_safety.ps1'; file=$BootstrapSafetyPath},
    @{path='scripts/uninstall.ps1'; file=$BootstrapUninstallerPath}
)) {
    $Entries = @($BootstrapManifest.files | Where-Object { $_.path -eq $BootstrapFile.path })
    if ($BootstrapManifest.schema_version -ne 2 -or $Entries.Count -ne 1 -or
        -not ($Entries[0].sha256 -is [string]) -or $Entries[0].sha256 -notmatch '^[0-9a-fA-F]{64}$' -or
        (Get-FileHash -LiteralPath $BootstrapFile.file -Algorithm SHA256).Hash -ne $Entries[0].sha256) {
        throw 'Refusing to uninstall because bootstrap files are not trusted by the release inventory.'
    }
}
. (Join-Path $PSScriptRoot 'path_safety.ps1')
$InstallDir = Assert-AppDockSafePath -Path $InstallDir -Role InstallDir
$DataDir = Assert-AppDockSafePath -Path $DataDir -Role DataDir
if (Test-AppDockPathOverlap $InstallDir $DataDir) {
    throw 'Refusing to uninstall because InstallDir and DataDir overlap.'
}
if (Test-Path -LiteralPath $DataDir -PathType Leaf) {
    throw 'Refusing to use a file as DataDir.'
}

if (Test-Path -LiteralPath $InstallDir) {
    Assert-AppDockInstallMarker -Path $InstallDir | Out-Null
}

$StartupShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'AppDock.lnk'
if (Test-Path -LiteralPath $StartupShortcut) {
    Remove-Item -LiteralPath $StartupShortcut -Force
    Write-Host "Removed startup shortcut: $StartupShortcut"
}

# Stop only Python processes whose command line has this exact installation's appdock.py token.
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and
    (Test-AppDockOwnedProcessCommandLine -CommandLine $_.CommandLine -InstallDir $InstallDir)
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
    Write-Host "Removed program files: $InstallDir"
}

if ($RemoveUserData) {
    if (Test-Path -LiteralPath $DataDir) {
        Remove-Item -LiteralPath $DataDir -Recurse -Force
        Write-Host "Removed user data: $DataDir"
    }
} else {
    Write-Host "Preserved user data: $DataDir"
    Write-Host 'Run again with -RemoveUserData only if you want to delete manifests, downloaded apps, settings, and logs.'
}
