from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "launch-windows.ps1"
CHECKER = ROOT / "scripts" / "check-windows-desktop.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_launcher_supports_desktop_checkonly_and_port_options():
    script = _text(LAUNCHER)

    assert "[switch]$CheckOnly" in script
    assert "[switch]$Desktop" in script
    assert "[int]$Port = 7000" in script
    assert '[string]$BindHost = "127.0.0.1"' in script
    assert "if ($CheckOnly)" in script


def test_launcher_keeps_chromadb_client_cleanup():
    script = _text(LAUNCHER)

    assert "pip show chromadb-client" in script
    assert "pip uninstall -y chromadb-client" in script
    assert "pip install --force-reinstall chromadb" in script


def test_windows_desktop_checker_is_read_only():
    script = _text(CHECKER).lower()

    forbidden = [
        "pip install",
        "pip uninstall",
        "npm install",
        "cargo install",
        "new-item",
        "remove-item",
        "set-content",
        "add-content",
        "out-file",
    ]
    for token in forbidden:
        assert token not in script


def test_windows_scripts_do_not_use_hidden_or_encoded_powershell():
    combined = (_text(LAUNCHER) + "\n" + _text(CHECKER)).lower()

    assert "-encodedcommand" not in combined
    assert "-enc " not in combined
    assert "-windowstyle hidden" not in combined


def test_windows_scripts_do_not_call_docker():
    combined = (_text(LAUNCHER) + "\n" + _text(CHECKER)).lower()

    assert "docker compose" not in combined
    assert "docker.exe" not in combined
    assert re.search(r"(^|[\s&;|])docker(\s|$)", combined) is None


def test_checker_reports_expected_prerequisites():
    script = _text(CHECKER)

    for expected in [
        "Find-Python",
        "Get-Command npm",
        "Get-Command node",
        "Get-Command cargo",
        "Test-WebView2",
        "Find-GitBash",
        "Test-GitBashPath",
        "Get-PipPackageInfo",
        '"chromadb"',
        '"chromadb-client"',
    ]:
        assert expected in script
