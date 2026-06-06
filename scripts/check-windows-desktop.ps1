#Requires -Version 5.1
<#
  Read-only Windows desktop prerequisite checker for Odysseus.

  This script does not install packages, create a venv, write files, or call
  Docker. It reports blockers for the native backend and Tauri desktop flow.
#>

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -Path $RepoRoot

$script:Failures = 0

function Write-Result($status, $message, $color) {
    Write-Host ("[{0}] {1}" -f $status, $message) -ForegroundColor $color
}

function Add-Fail($message) {
    $script:Failures += 1
    Write-Result "FAIL" $message Red
}

function Add-Warn($message) {
    Write-Result "WARN" $message Yellow
}

function Add-Ok($message) {
    Write-Result "OK" $message Green
}

function Get-PythonVersionText($launcher, $launcherArgs) {
    try {
        return (& $launcher @launcherArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null).Trim()
    } catch {
        return $null
    }
}

function Find-Python {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($v in @("-3.13", "-3.12", "-3.11")) {
            $ver = Get-PythonVersionText $pyLauncher.Source @($v)
            if ($ver) {
                return @{ Exe = $pyLauncher.Source; Args = @($v); Version = $ver }
            }
        }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $ver = Get-PythonVersionText $pythonCmd.Source @()
        if ($ver) {
            $parts = $ver.Split(".")
            if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11)) {
                return @{ Exe = $pythonCmd.Source; Args = @(); Version = $ver }
            }
        }
    }

    return $null
}

function Test-GitBashPath($path) {
    if (-not $path) { return $false }
    $normalized = $path.Replace("/", "\").ToLowerInvariant()
    return $normalized -match "\\git\\(bin|usr\\bin)\\bash\.exe$"
}

function Find-GitBash {
    $cmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($cmd -and (Test-GitBashPath $cmd.Source)) { return $cmd.Source }

    $roots = @()
    foreach ($name in @("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LocalAppData")) {
        $base = [Environment]::GetEnvironmentVariable($name)
        if ($base) { $roots += (Join-Path $base "Git") }
    }
    $roots += @("C:\Program Files\Git", "C:\Program Files (x86)\Git")

    foreach ($root in ($roots | Select-Object -Unique)) {
        foreach ($relative in @("bin\bash.exe", "usr\bin\bash.exe")) {
            $candidate = Join-Path $root $relative
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Test-WebView2 {
    $roots = @(
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\*",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\*"
    )
    foreach ($root in $roots) {
        foreach ($item in (Get-ItemProperty $root -ErrorAction SilentlyContinue)) {
            $name = [string]$item.name
            if ($name -match "WebView2") { return $true }
        }
    }

    $pathRoots = @()
    foreach ($base in @(
        [Environment]::GetEnvironmentVariable("ProgramFiles(x86)"),
        [Environment]::GetEnvironmentVariable("ProgramFiles")
    )) {
        if ($base) {
            $pathRoots += (Join-Path $base "Microsoft\EdgeWebView\Application")
        }
    }
    foreach ($root in $pathRoots) {
        if ($root -and (Test-Path $root)) {
            $exe = Get-ChildItem -Path $root -Recurse -Filter msedgewebview2.exe -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($exe) { return $true }
        }
    }
    return $false
}

function Get-CommandVersion($command, $commandArgs) {
    try {
        return (& $command @commandArgs 2>$null | Select-Object -First 1).Trim()
    } catch {
        return $null
    }
}

function Get-PipPackageInfo($pythonExe, $packageName) {
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $pythonExe -m pip show $packageName 2>$null
        if ($LASTEXITCODE -eq 0 -and $output) { return $output }
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $oldPreference
    }
    return $null
}

Write-Host ""
Write-Host "Odysseus Windows desktop prerequisite check"
Write-Host "Repo: $RepoRoot"
Write-Host ""

$python = Find-Python
if ($python) {
    Add-Ok ("Python {0}: {1} {2}" -f $python.Version, $python.Exe, ($python.Args -join " ")).TrimEnd()
} else {
    Add-Fail "Python 3.11+ was not found. Install it from https://www.python.org/downloads/windows/ and rerun this check."
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if ($npm) {
    $npmVersion = Get-CommandVersion $npm.Source @("--version")
    Add-Ok ("npm found: {0} {1}" -f $npm.Source, $npmVersion).TrimEnd()
} else {
    Add-Fail "npm was not found. Install Node.js LTS from https://nodejs.org/."
}

$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    $nodeVersion = Get-CommandVersion $node.Source @("--version")
    Add-Ok ("Node.js found: {0} {1}" -f $node.Source, $nodeVersion).TrimEnd()
} else {
    Add-Warn "node was not found separately. Installing Node.js LTS should provide both node and npm."
}

$cargo = Get-Command cargo -ErrorAction SilentlyContinue
if ($cargo) {
    $cargoVersion = Get-CommandVersion $cargo.Source @("--version")
    Add-Ok ("Cargo found: {0} {1}" -f $cargo.Source, $cargoVersion).TrimEnd()
} else {
    Add-Fail "Cargo/Rust was not found. Install Rust from https://www.rust-lang.org/tools/install."
}

if (Test-WebView2) {
    Add-Ok "WebView2 Runtime appears to be installed"
} else {
    Add-Warn "WebView2 Runtime was not detected. Install it from https://developer.microsoft.com/microsoft-edge/webview2/ if the desktop window will not open."
}

$bash = Find-GitBash
if ($bash) {
    Add-Ok "Git Bash found for optional Cookbook/agent-shell parity: $bash"
} else {
    Add-Warn "Git Bash not found. Core desktop works; install Git for Windows from https://git-scm.com/download/win for full Cookbook/agent shell parity."
}

$venvPy = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Add-Ok "venv found"
    $fullChroma = Get-PipPackageInfo $venvPy "chromadb"
    $clientChroma = Get-PipPackageInfo $venvPy "chromadb-client"
    if ($clientChroma) {
        Add-Warn "chromadb-client is installed in venv. Run .\launch-windows.ps1 to replace it with full chromadb."
    } elseif ($fullChroma) {
        Add-Ok "full chromadb package is installed in venv"
    } else {
        Add-Warn "chromadb is not installed in venv yet. Run .\launch-windows.ps1 to install requirements."
    }
} else {
    Add-Warn "venv not found yet. Run .\launch-windows.ps1 or npm run desktop:dev to create it."
}

Write-Host ""
if ($script:Failures -gt 0) {
    Write-Host ("Check completed with {0} blocker(s)." -f $script:Failures) -ForegroundColor Red
    exit 1
}

Write-Host "Check completed with no hard blockers." -ForegroundColor Green
exit 0
