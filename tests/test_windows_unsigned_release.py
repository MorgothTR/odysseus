import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "package.json"
RELEASE_SCRIPT = ROOT / "scripts" / "package-windows-unsigned-release.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_desktop_release_script_is_wired_to_package_json():
    package = json.loads(_text(PACKAGE_JSON))

    assert (
        package["scripts"]["desktop:release"]
        == "powershell -ExecutionPolicy Bypass -File scripts/package-windows-unsigned-release.ps1"
    )


def test_unsigned_release_script_builds_unless_skip_build_is_set():
    script = _text(RELEASE_SCRIPT)

    assert "[switch]$SkipBuild" in script
    assert "if (-not $SkipBuild)" in script
    assert "npm run desktop:build" in script
    assert "Skipping build; packaging existing desktop artifacts." in script


def test_unsigned_release_script_packages_nsis_and_msi_with_hashes():
    script = _text(RELEASE_SCRIPT)

    assert "src-tauri\\target\\release\\bundle\\nsis\\Odysseus_${version}_x64-setup.exe" in script
    assert "src-tauri\\target\\release\\bundle\\msi\\Odysseus_${version}_x64_en-US.msi" in script
    assert "Copy-Item -LiteralPath $nsisArtifact" in script
    assert "Copy-Item -LiteralPath $msiArtifact" in script
    assert "SHA256SUMS.txt" in script
    assert "Get-FileHash -LiteralPath $artifact -Algorithm SHA256" in script
    assert "RELEASE-NOTES-unsigned-windows.md" in script


def test_unsigned_release_script_reads_tauri_version_and_uses_dist_output():
    script = _text(RELEASE_SCRIPT)

    assert "src-tauri\\tauri.conf.json" in script
    assert "ConvertFrom-Json" in script
    assert "dist\\windows-unsigned" in script
    assert 'Join-Path $distRoot "Odysseus-$version"' in script
    assert "[string]$OutputDir" in script


def test_unsigned_release_script_does_not_require_signing_or_call_docker():
    script = _text(RELEASE_SCRIPT).lower()

    assert "odysseus_cert_thumbprint" not in script
    assert "odysseus_signtool_path" not in script
    assert "odysseus_sign_command" not in script
    assert "docker-compose" not in script
    assert "docker compose" not in script
    assert "& docker" not in script
    assert "start-process docker" not in script


def test_unsigned_release_notes_warn_about_private_unsigned_use():
    script = _text(RELEASE_SCRIPT)

    assert "not code-signed" in script
    assert "private/local testing" in script
    assert "SmartScreen" in script
    assert "Norton" in script
    assert "SHA256SUMS.txt" in script
