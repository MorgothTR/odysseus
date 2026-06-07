#Requires -Version 5.1
<#
  Odysseus - native Windows launcher (no Docker).

  One command to: create a virtualenv, install dependencies, run first-time
  setup (prints an admin password on first run), and start the server.
  Safe to re-run - it skips whatever already exists.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1
    powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -CheckOnly
    powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -Port 7000 -BindHost 127.0.0.1
    powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -Desktop -Port 7000 -BindHost 127.0.0.1

  Tip: bind 127.0.0.1 (default) for local-only use. Use 0.0.0.0 only when you
  intentionally want other devices on your LAN to reach it.
#>
param(
    [int]$Port = 7000,
    [string]$BindHost = "127.0.0.1",
    [switch]$Desktop,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host ""; Write-Host ("==> " + $msg) -ForegroundColor Cyan }
function Write-Check($status, $msg, $color) {
    Write-Host ("[{0}] {1}" -f $status, $msg) -ForegroundColor $color
}
function Fail($msg) {
    Write-Host ""
    Write-Host ("ERROR: " + $msg) -ForegroundColor Red
    Write-Host ""
    if (-not $Desktop -and -not $CheckOnly) {
        Read-Host "Press Enter to exit"
    }
    exit 1
}

function Test-Pip($pythonExe) {
    try {
        & $pythonExe -m pip --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
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

# 1. Locate a Python interpreter (3.11+ required)
Write-Step "Checking for Python"
function Get-PythonVersionText($launcher, $launcherArgs) {
    try {
        return (& $launcher @launcherArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null).Trim()
    } catch {
        return $null
    }
}

$pyExe = $null
$pyArgs = @()
$pyVersion = $null

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    foreach ($v in @("-3.13", "-3.12", "-3.11")) {
        $ver = Get-PythonVersionText $pyLauncher.Source @($v)
        if ($ver) {
            $pyExe = $pyLauncher.Source
            $pyArgs = @($v)
            $pyVersion = $ver
            break
        }
    }
}

if (-not $pyExe) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        $ver = Get-PythonVersionText $pythonCmd.Source @()
        if ($ver) {
            $versionParts = $ver.Split('.')
            $major = [int]$versionParts[0]
            $minor = [int]$versionParts[1]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                $pyExe = $pythonCmd.Source
                $pyVersion = $ver
            }
        }
    }
}

if (-not $pyExe) {
    Fail "Couldn't find Python 3.11+ for Windows setup. Install Python 3.11+ (or open the Python launcher with 'py -3.11') from https://www.python.org/downloads/, then re-run this script."
}
$pythonLabel = ("Using Python {0}: {1} {2}" -f $pyVersion, $pyExe, ($pyArgs -join ' ')).TrimEnd()
Write-Host $pythonLabel

if ($CheckOnly) {
    Write-Step "Windows native preflight"
    Write-Check "OK" $pythonLabel Green

    $venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        Write-Check "OK" ("venv found: {0}" -f $venvPy) Green
        if (Test-Pip $venvPy) {
            Write-Check "OK" "venv pip is available" Green
        } else {
            Write-Check "FAIL" "venv exists but pip is not available. Recreate venv or repair Python." Red
            exit 1
        }

        $fullChroma = Get-PipPackageInfo $venvPy "chromadb"
        $clientChroma = Get-PipPackageInfo $venvPy "chromadb-client"
        if ($clientChroma) {
            Write-Check "WARN" "chromadb-client is installed; the normal launcher will replace it with full chromadb." Yellow
        } elseif ($fullChroma) {
            Write-Check "OK" "full chromadb package is installed" Green
        } else {
            Write-Check "WARN" "chromadb is not installed yet; the normal launcher will install requirements.txt." Yellow
        }
    } else {
        Write-Check "WARN" "venv not found yet; the normal launcher will create it." Yellow
    }

    if (Find-GitBash) {
        Write-Check "OK" "Git Bash found for optional Cookbook/agent-shell parity" Green
    } else {
        Write-Check "WARN" "Git Bash not found. Core app works; install Git for Windows for full Cookbook/agent shell parity." Yellow
    }

    Write-Host ""
    Write-Host "Check complete. No files were changed."
    exit 0
}

# 2. Create the virtualenv if missing
$venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtual environment (venv)"
    & $pyExe @pyArgs -m venv venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Failed to create the virtual environment." }
} else {
    Write-Host "venv already exists - skipping creation."
}

# 3. Install / update dependencies
Write-Step "Installing dependencies (first run can take a few minutes)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed. Scroll up for the pip error." }

# chromadb-client is HTTP-only and conflicts with embedded ChromaDB. Remove it
# from existing venvs before forcing the full chromadb package into place.
$clientCheck = Get-PipPackageInfo $venvPy "chromadb-client"
if ($clientCheck) {
    Write-Step "Replacing HTTP-only chromadb-client with embedded ChromaDB"
    & $venvPy -m pip uninstall -y chromadb-client
    if ($LASTEXITCODE -ne 0) { Fail "Failed to remove chromadb-client." }
    & $venvPy -m pip install --force-reinstall chromadb
    if ($LASTEXITCODE -ne 0) { Fail "Failed to install full chromadb package." }
}

# 4. First-time setup (creates data dirs, DB, .env, admin user)
Write-Step "Running first-time setup"
$desktopEnvBackup = @{}
if ($Desktop) {
    foreach ($key in @("ODYSSEUS_SKIP_ADMIN_PROMPT", "ODYSSEUS_SKIP_RUN_HINT", "ODYSSEUS_SKIP_ADMIN_CREATE")) {
        $desktopEnvBackup[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, "1", "Process")
    }
}
& $venvPy setup.py
$setupExitCode = $LASTEXITCODE
if ($Desktop) {
    foreach ($key in $desktopEnvBackup.Keys) {
        [Environment]::SetEnvironmentVariable($key, $desktopEnvBackup[$key], "Process")
    }
}
if ($setupExitCode -ne 0) { Fail "setup.py failed." }

# 5. Friendly note about Git Bash (full Cookbook / agent-shell parity)
if (-not (Find-GitBash)) {
    Write-Host ""
    Write-Host "NOTE: Git Bash (bash.exe) was not found on PATH." -ForegroundColor Yellow
    Write-Host "      The core app works without it. For full Cookbook background" -ForegroundColor Yellow
    Write-Host "      downloads and the agent shell tool, install Git for Windows:" -ForegroundColor Yellow
    Write-Host "      https://git-scm.com/download/win" -ForegroundColor Yellow
}

# 6. Start the server (use `python -m uvicorn` - bare `uvicorn` may not be on PATH)
Write-Step ("Starting Odysseus at http://{0}:{1}" -f $BindHost, $Port)
Write-Host "Press Ctrl+C to stop."
Write-Host ""
& $venvPy -m uvicorn app:app --host $BindHost --port $Port
