#Requires -Version 5.1
<#
  Packages the unsigned Windows desktop artifacts for private/local release.

  By default this script runs npm run desktop:build, then copies the Tauri NSIS
  and MSI outputs into dist/windows-unsigned/Odysseus-<version>/ with SHA-256
  hashes and a short unsigned-release note.
#>
param(
    [switch]$SkipBuild,
    [string]$OutputDir
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$tauriConfigPath = Join-Path $repoRoot "src-tauri\tauri.conf.json"
$distRoot = if ($OutputDir) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
} else {
    Join-Path $repoRoot "dist\windows-unsigned"
}

function Fail {
    param([string]$Message)
    throw $Message
}

function Get-TauriVersion {
    if (-not (Test-Path -LiteralPath $tauriConfigPath)) {
        Fail "Tauri config not found: $tauriConfigPath"
    }
    $config = Get-Content -LiteralPath $tauriConfigPath -Raw | ConvertFrom-Json
    if (-not $config.version) {
        Fail "Tauri version was not found in $tauriConfigPath"
    }
    return [string]$config.version
}

function Assert-ReleaseArtifact {
    param(
        [string]$Path,
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Fail "$Label artifact was not found: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -lt 10MB) {
        Fail "$Label artifact looks too small to be a complete desktop release: $Path ($($item.Length) bytes)"
    }
}

function Write-UnsignedReleaseNotes {
    param(
        [string]$Path,
        [string]$Version
    )
    $notes = @"
# Odysseus $Version unsigned Windows desktop release

This package is intended for private/local testing. The installer and MSI are
not code-signed, so Windows SmartScreen, Microsoft Defender, Norton, or other
security software may warn that the publisher is unknown.

Use these artifacts only if you built them yourself or downloaded them from a
release location you trust. To verify an artifact, compare its SHA-256 value
with `SHA256SUMS.txt` in this folder.

Included artifacts:

- `Odysseus_${Version}_x64-setup.exe` - recommended NSIS installer
- `Odysseus_${Version}_x64_en-US.msi` - MSI installer

Installed runtime state is stored under `%LOCALAPPDATA%\OdysseusData`.
The app bundles Python and the dependency wheelhouse, so normal first launch
does not require Docker, Node.js, Rust, or a Git checkout.
"@
    Set-Content -LiteralPath $Path -Value $notes -Encoding UTF8
}

$version = Get-TauriVersion

if (-not $SkipBuild) {
    Write-Host "Running desktop build before packaging unsigned release..."
    Push-Location $repoRoot
    try {
        & npm run desktop:build
        if ($LASTEXITCODE -ne 0) {
            Fail "npm run desktop:build failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "Skipping build; packaging existing desktop artifacts."
}

$nsisArtifact = Join-Path $repoRoot "src-tauri\target\release\bundle\nsis\Odysseus_${version}_x64-setup.exe"
$msiArtifact = Join-Path $repoRoot "src-tauri\target\release\bundle\msi\Odysseus_${version}_x64_en-US.msi"
Assert-ReleaseArtifact -Path $nsisArtifact -Label "NSIS"
Assert-ReleaseArtifact -Path $msiArtifact -Label "MSI"

$releaseDir = Join-Path $distRoot "Odysseus-$version"
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

$releaseNsis = Join-Path $releaseDir (Split-Path -Leaf $nsisArtifact)
$releaseMsi = Join-Path $releaseDir (Split-Path -Leaf $msiArtifact)
Copy-Item -LiteralPath $nsisArtifact -Destination $releaseNsis -Force
Copy-Item -LiteralPath $msiArtifact -Destination $releaseMsi -Force

$hashPath = Join-Path $releaseDir "SHA256SUMS.txt"
$hashLines = foreach ($artifact in @($releaseNsis, $releaseMsi)) {
    $hash = Get-FileHash -LiteralPath $artifact -Algorithm SHA256
    "{0}  {1}" -f $hash.Hash.ToLowerInvariant(), (Split-Path -Leaf $artifact)
}
Set-Content -LiteralPath $hashPath -Value $hashLines -Encoding ASCII

$notesPath = Join-Path $releaseDir "RELEASE-NOTES-unsigned-windows.md"
Write-UnsignedReleaseNotes -Path $notesPath -Version $version

Write-Host ""
Write-Host "Unsigned Windows release package ready:"
Write-Host "  $releaseDir"
Write-Host ""
Write-Host "Artifacts:"
Get-ChildItem -LiteralPath $releaseDir -File | Select-Object Name, Length | Format-Table -AutoSize
