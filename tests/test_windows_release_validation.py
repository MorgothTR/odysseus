import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "package.json"
VERIFY_SCRIPT = ROOT / "scripts" / "verify-windows-release.ps1"
CHECKLIST = ROOT / "docs" / "windows-release-checklist.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_desktop_release_verify_script_is_wired_to_package_json():
    package = json.loads(_text(PACKAGE_JSON))

    assert (
        package["scripts"]["desktop:release:verify"]
        == "powershell -ExecutionPolicy Bypass -File scripts/verify-windows-release.ps1"
    )


def test_release_verifier_reads_tauri_version_and_supports_release_dir():
    script = _text(VERIFY_SCRIPT)

    assert "[string]$ReleaseDir" in script
    assert "src-tauri\\tauri.conf.json" in script
    assert "ConvertFrom-Json" in script
    assert "dist\\windows-unsigned\\Odysseus-$Version" in script
    assert "GetUnresolvedProviderPathFromPSPath($ReleaseDir)" in script


def test_release_verifier_checks_expected_artifacts_and_sizes():
    script = _text(VERIFY_SCRIPT)

    assert "Odysseus_${version}_x64-setup.exe" in script
    assert "Odysseus_${version}_x64_en-US.msi" in script
    assert "SHA256SUMS.txt" in script
    assert "RELEASE-NOTES-unsigned-windows.md" in script
    assert "MinimumBytes 10MB" in script
    assert "looks too small" in script
    assert "Release directory missing" in script


def test_release_verifier_uses_sha256_and_fails_on_hash_problems():
    script = _text(VERIFY_SCRIPT)

    assert "Get-FileHash -LiteralPath $ArtifactPath -Algorithm SHA256" in script
    assert "SHA256SUMS.txt missing hash entry" in script
    assert "SHA-256 mismatch" in script
    assert "line $lineNumber is not in" in script
    assert "exit 1" in script


def test_release_verifier_is_read_only_and_does_not_sign_or_call_docker():
    script = _text(VERIFY_SCRIPT).lower()

    for forbidden in [
        "npm run desktop:build",
        "package-windows-unsigned-release",
        "start-process",
        "msiexec",
        "remove-item",
        "signtool",
        "odysseus_cert_thumbprint",
        "docker-compose",
        "docker compose",
        "& docker",
    ]:
        assert forbidden not in script


def test_release_checklist_documents_private_release_smokes_and_warnings():
    checklist = _text(CHECKLIST)

    for expected in [
        "npm run desktop:release",
        "npm run desktop:release:verify",
        "Clean Install Smoke",
        "Upgrade Smoke",
        "Self-Repair Smoke",
        "Uninstall Expectation",
        "%LOCALAPPDATA%\\OdysseusData",
        "SHA256SUMS.txt",
        "SmartScreen",
        "Norton",
    ]:
        assert expected in checklist
