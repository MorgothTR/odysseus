#Requires -Version 5.1
<#
  Verifies an existing unsigned Windows desktop release folder.

  This script is intentionally read-only. It does not build, install, uninstall,
  sign, repair, or mutate backend/frontend files.
#>
param(
    [string]$ReleaseDir
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$tauriConfigPath = Join-Path $repoRoot "src-tauri\tauri.conf.json"
$script:FailureCount = 0
$script:WarningCount = 0

function Write-Pass {
    param([string]$Message)
    Write-Host "PASS $Message"
}

function Write-WarnLine {
    param([string]$Message)
    $script:WarningCount += 1
    Write-Host "WARN $Message"
}

function Write-FailLine {
    param([string]$Message)
    $script:FailureCount += 1
    Write-Host "FAIL $Message"
}

function Get-TauriVersion {
    if (-not (Test-Path -LiteralPath $tauriConfigPath -PathType Leaf)) {
        throw "Tauri config not found: $tauriConfigPath"
    }
    $config = Get-Content -LiteralPath $tauriConfigPath -Raw | ConvertFrom-Json
    if (-not $config.version) {
        throw "Tauri version was not found in $tauriConfigPath"
    }
    return [string]$config.version
}

function Resolve-ReleaseDirectory {
    param([string]$Version)
    if ($ReleaseDir) {
        return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ReleaseDir)
    }
    return Join-Path $repoRoot "dist\windows-unsigned\Odysseus-$Version"
}

function Test-ReleaseFile {
    param(
        [string]$Path,
        [string]$Label,
        [int64]$MinimumBytes = 0
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-FailLine "$Label missing: $Path"
        return $null
    }

    $item = Get-Item -LiteralPath $Path
    if ($MinimumBytes -gt 0 -and $item.Length -lt $MinimumBytes) {
        Write-FailLine "$Label looks too small: $Path ($($item.Length) bytes)"
        return $item
    }

    Write-Pass "$Label present: $($item.Name) ($($item.Length) bytes)"
    return $item
}

function Read-Sha256Sums {
    param([string]$Path)
    $hashes = @{}
    $lineNumber = 0
    foreach ($line in Get-Content -LiteralPath $Path) {
        $lineNumber += 1
        if (-not $line.Trim()) {
            continue
        }
        if ($line -notmatch '^\s*([a-fA-F0-9]{64})\s+(.+?)\s*$') {
            Write-FailLine "SHA256SUMS.txt line $lineNumber is not in '<sha256>  <filename>' format."
            continue
        }
        $hashes[$Matches[2]] = $Matches[1].ToLowerInvariant()
    }
    return $hashes
}

function Test-HashEntry {
    param(
        [hashtable]$ExpectedHashes,
        [string]$ArtifactPath
    )
    $name = Split-Path -Leaf $ArtifactPath
    if (-not $ExpectedHashes.ContainsKey($name)) {
        Write-FailLine "SHA256SUMS.txt missing hash entry for $name"
        return
    }

    $actual = (Get-FileHash -LiteralPath $ArtifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $expected = $ExpectedHashes[$name]
    if ($actual -ne $expected) {
        Write-FailLine "SHA-256 mismatch for $name"
        return
    }

    Write-Pass "SHA-256 verified for $name"
}

$version = Get-TauriVersion
$resolvedReleaseDir = Resolve-ReleaseDirectory -Version $version
Write-Host "Verifying unsigned Windows release:"
Write-Host "  $resolvedReleaseDir"
Write-Host ""

if (-not (Test-Path -LiteralPath $resolvedReleaseDir -PathType Container)) {
    Write-FailLine "Release directory missing: $resolvedReleaseDir"
} else {
    Write-Pass "Release directory exists."
}

$nsisPath = Join-Path $resolvedReleaseDir "Odysseus_${version}_x64-setup.exe"
$msiPath = Join-Path $resolvedReleaseDir "Odysseus_${version}_x64_en-US.msi"
$hashPath = Join-Path $resolvedReleaseDir "SHA256SUMS.txt"
$notesPath = Join-Path $resolvedReleaseDir "RELEASE-NOTES-unsigned-windows.md"

$nsis = Test-ReleaseFile -Path $nsisPath -Label "NSIS installer" -MinimumBytes 10MB
$msi = Test-ReleaseFile -Path $msiPath -Label "MSI installer" -MinimumBytes 10MB
$hashFile = Test-ReleaseFile -Path $hashPath -Label "SHA256SUMS.txt"
$notesFile = Test-ReleaseFile -Path $notesPath -Label "Unsigned release notes"

if ($hashFile) {
    $hashes = Read-Sha256Sums -Path $hashPath
    if ($nsis) {
        Test-HashEntry -ExpectedHashes $hashes -ArtifactPath $nsisPath
    }
    if ($msi) {
        Test-HashEntry -ExpectedHashes $hashes -ArtifactPath $msiPath
    }
}

if ($notesFile) {
    $notes = Get-Content -LiteralPath $notesPath -Raw
    if ($notes -match '(?i)unsigned' -and $notes -match '(?i)(SmartScreen|Norton|Defender)') {
        Write-Pass "Release notes mention unsigned-app warnings."
    } else {
        Write-WarnLine "Release notes do not clearly mention unsigned-app warnings."
    }
}

Write-Host ""
if ($script:FailureCount -gt 0) {
    Write-Host "FAIL Release verification found $script:FailureCount failure(s) and $script:WarningCount warning(s)."
    exit 1
}

if ($script:WarningCount -gt 0) {
    Write-Host "WARN Release verification passed with $script:WarningCount warning(s)."
} else {
    Write-Host "PASS Release verification passed."
}
