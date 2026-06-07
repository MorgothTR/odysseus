import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREP_SCRIPT = ROOT / "scripts" / "prepare-desktop-bundle.mjs"
TAURI_CONFIG = ROOT / "src-tauri" / "tauri.conf.json"
TAURI_MAIN = ROOT / "src-tauri" / "src" / "main.rs"
PACKAGE_JSON = ROOT / "package.json"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_desktop_build_prepares_backend_resource_bundle():
    package = json.loads(_text(PACKAGE_JSON))
    config = json.loads(_text(TAURI_CONFIG))

    assert "node scripts/prepare-desktop-bundle.mjs && tauri build" == package["scripts"]["desktop:build"]
    assert config["bundle"]["resources"] == {"resources/backend": "backend"}


def test_backend_bundle_script_uses_tracked_runtime_files_and_excludes_private_state():
    script = _text(PREP_SCRIPT)

    assert 'execFileSync("git", ["ls-files", "-z"]' in script
    for included in ["app.py", "launch-windows.ps1", "requirements.txt", "static", "routes", "src"]:
        assert included in script
    for excluded in [
        "data/",
        "logs/",
        "venv/",
        "node_modules/",
        "src-tauri/",
        "tests/",
        "docker/",
        "Dockerfile",
        "docker-compose.yml",
        "scripts/check-windows-desktop.ps1",
    ]:
        assert excluded in script


def test_tauri_installed_mode_uses_local_appdata_and_preserves_runtime_state():
    main = _text(TAURI_MAIN)

    assert 'const INSTALLED_APP_DIR: &str = "Odysseus";' in main
    assert 'std::env::var_os("LOCALAPPDATA")' in main
    assert 'app.path().resource_dir()?.join(BACKEND_RESOURCE_DIR)' in main
    assert 'const PRESERVED_BACKEND_NAMES: &[&str] = &["data", "logs", "venv", ".env"];' in main
    assert "find_dev_repo_root" in main
    assert "prepare_installed_backend" in main
