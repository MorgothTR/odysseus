#Requires -Version 5.1
<#
  Optional Windows signing hook for Tauri builds.

  Unsigned prototype builds leave the ODYSSEUS_* signing environment variables
  unset, so this script exits successfully without modifying the target file.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path)) {
    throw "Signing target was not found: $Path"
}

if ($env:ODYSSEUS_SIGN_COMMAND) {
    $target = $Path.Replace('"', '\"')
    $command = $env:ODYSSEUS_SIGN_COMMAND
    if ($command.Contains("%1")) {
        $command = $command.Replace("%1", ('"{0}"' -f $target))
    } else {
        $command = ('{0} "{1}"' -f $command, $target)
    }
    Write-Host "Signing with ODYSSEUS_SIGN_COMMAND"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command $command
    exit $LASTEXITCODE
}

$signTool = $env:ODYSSEUS_SIGNTOOL_PATH
if (-not $signTool) {
    $signToolCommand = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($signToolCommand) {
        $signTool = $signToolCommand.Source
    }
}

if (-not $signTool -or -not $env:ODYSSEUS_CERT_THUMBPRINT) {
    Write-Host "Skipping Windows code signing; set ODYSSEUS_SIGNTOOL_PATH and ODYSSEUS_CERT_THUMBPRINT to enable it."
    exit 0
}

$signArgs = @(
    "sign",
    "/fd", "SHA256",
    "/sha1", $env:ODYSSEUS_CERT_THUMBPRINT
)

if ($env:ODYSSEUS_TIMESTAMP_URL) {
    $signArgs += @("/tr", $env:ODYSSEUS_TIMESTAMP_URL, "/td", "SHA256")
}

$signArgs += $Path

& $signTool @signArgs
exit $LASTEXITCODE
