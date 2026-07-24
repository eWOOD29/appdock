Set-StrictMode -Version Latest

function Get-AppDockFullPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'AppDock paths must not be empty.'
    }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd([char[]]@('\', '/'))
}

function Test-AppDockPathOverlap([string]$Left, [string]$Right) {
    $LeftRoot = Get-AppDockFullPath $Left
    $RightRoot = Get-AppDockFullPath $Right
    $Comparison = [System.StringComparison]::OrdinalIgnoreCase
    return $LeftRoot.Equals($RightRoot, $Comparison) -or
        $LeftRoot.StartsWith($RightRoot + [System.IO.Path]::DirectorySeparatorChar, $Comparison) -or
        $RightRoot.StartsWith($LeftRoot + [System.IO.Path]::DirectorySeparatorChar, $Comparison)
}

function Get-AppDockProtectedRoots {
    $Candidates = @(
        [System.IO.Path]::GetPathRoot((Get-AppDockFullPath $env:SystemRoot)),
        $env:SystemRoot,
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        $env:ProgramData,
        $env:PUBLIC,
        [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::MyDocuments),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::MyMusic),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::MyPictures),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::MyVideos),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::ApplicationData),
        [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    )
    $UserProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if ($UserProfile) {
        $Candidates += @(
            (Split-Path -Parent $UserProfile),
            (Join-Path $UserProfile 'Desktop'),
            (Join-Path $UserProfile 'Documents'),
            (Join-Path $UserProfile 'Downloads'),
            (Join-Path $UserProfile 'Music'),
            (Join-Path $UserProfile 'Pictures'),
            (Join-Path $UserProfile 'Videos'),
            (Join-Path $UserProfile 'AppData\Local'),
            (Join-Path $UserProfile 'AppData\Roaming')
        )
    }
    return @($Candidates | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { Get-AppDockFullPath $_ } | Select-Object -Unique)
}

function Assert-AppDockNoReparseAncestor([string]$Path) {
    $Current = Get-AppDockFullPath $Path
    while ($Current) {
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "AppDock refuses a path beneath a filesystem reparse point: $Current"
            }
        }
        $Parent = Split-Path -Parent $Current
        if (-not $Parent -or $Parent -eq $Current) { break }
        $Current = $Parent
    }
}

function Assert-AppDockSafePath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet('InstallDir', 'DataDir')][string]$Role,
        [string]$SourceRoot = ''
    )

    $FullPath = Get-AppDockFullPath $Path
    $PathRoot = Get-AppDockFullPath ([System.IO.Path]::GetPathRoot($FullPath))
    if ($FullPath.Equals($PathRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Role must not be a filesystem or volume root."
    }
    foreach ($Protected in Get-AppDockProtectedRoots) {
        $Comparison = [System.StringComparison]::OrdinalIgnoreCase
        $Prefix = $FullPath.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
        if ($FullPath.Equals($Protected, $Comparison) -or $Protected.StartsWith($Prefix, $Comparison)) {
            throw "$Role must not be a filesystem, system, program, users, public, or profile root."
        }
    }
    if ($SourceRoot -and (Test-AppDockPathOverlap $FullPath $SourceRoot)) {
        throw "$Role must not overlap the extracted AppDock source directory."
    }
    Assert-AppDockNoReparseAncestor $FullPath
    return $FullPath
}

function Assert-AppDockInstallMarker([string]$Path) {
    $Root = Get-AppDockFullPath $Path
    $Launcher = Join-Path $Root 'run-appdock.cmd'
    $ManifestPath = Join-Path $Root 'RELEASE-MANIFEST.json'
    if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf) -or
        -not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw 'Refusing to remove a directory that is not a recognizable AppDock installation.'
    }
    try {
        $Manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
    } catch {
        throw 'Refusing to remove an AppDock installation with an invalid release inventory.'
    }
    if ($Manifest.schema_version -notin @(1, 2) -or -not $Manifest.files) {
        throw 'Refusing to remove an AppDock installation with an invalid release inventory.'
    }
    $RequiredMarkerFiles = @('appdock.py', 'scripts/uninstall.ps1', 'scripts/update_helper.py', 'static/app.js', 'static/app.css')
    if ($Manifest.schema_version -eq 2) { $RequiredMarkerFiles += 'scripts/path_safety.ps1' }
    foreach ($Relative in $RequiredMarkerFiles) {
        $Entries = @($Manifest.files | Where-Object { $_.path -eq $Relative })
        $File = Join-Path $Root ($Relative -replace '/', '\')
        if ($Entries.Count -ne 1 -or -not (Test-Path -LiteralPath $File -PathType Leaf) -or
            -not ($Entries[0].sha256 -is [string]) -or $Entries[0].sha256 -notmatch '^[0-9a-fA-F]{64}$' -or
            (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash -ne $Entries[0].sha256) {
            throw 'Refusing to remove an AppDock installation whose release inventory does not validate.'
        }
    }
    return $Root
}

function Test-AppDockOwnedProcessCommandLine {
    param(
        [AllowNull()][string]$CommandLine,
        [Parameter(Mandatory)][string]$InstallDir
    )
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    $EntryPoint = Join-Path (Get-AppDockFullPath $InstallDir) 'appdock.py'
    $EscapedEntryPoint = [Regex]::Escape($EntryPoint)
    $Python = '(?:"[^"\r\n]*\\pythonw?\.exe"|[^\s"\r\n]*pythonw?\.exe)'
    $Script = '(?:"' + $EscapedEntryPoint + '"|' + $EscapedEntryPoint + ')'
    $Pattern = '(?i)^\s*' + $Python + '\s+' + $Script + '(?=\s|$)'
    return [Regex]::IsMatch($CommandLine, $Pattern)
}
