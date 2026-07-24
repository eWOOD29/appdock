param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AppDock'),
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'AppDock'),
    [switch]$RemoveUserData
)

$ErrorActionPreference = 'Stop'
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
    throw 'Refusing to uninstall because InstallDir and DataDir overlap.'
}

$StartupShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'AppDock.lnk'
if (Test-Path $StartupShortcut) {
    Remove-Item -Force $StartupShortcut
    Write-Host "Removed startup shortcut: $StartupShortcut"
}

# Stop only Python processes whose command line names this installation's appdock.py.
$EscapedInstall = [Regex]::Escape($InstallDir)
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match $EscapedInstall -and $_.CommandLine -match 'appdock\.py'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir
    Write-Host "Removed program files: $InstallDir"
}

if ($RemoveUserData) {
    if (Test-Path $DataDir) {
        Remove-Item -Recurse -Force $DataDir
        Write-Host "Removed user data: $DataDir"
    }
} else {
    Write-Host "Preserved user data: $DataDir"
    Write-Host 'Run again with -RemoveUserData only if you want to delete manifests, downloaded apps, settings, and logs.'
}
