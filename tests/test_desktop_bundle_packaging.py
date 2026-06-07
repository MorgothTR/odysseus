import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PREP_SCRIPT = ROOT / "scripts" / "prepare-desktop-bundle.mjs"
PYTHON_PREP_SCRIPT = ROOT / "scripts" / "prepare-python-runtime.mjs"
PYTHON_MANIFEST = ROOT / "scripts" / "python-runtime.manifest.json"
WHEELHOUSE_PREP_SCRIPT = ROOT / "scripts" / "prepare-python-wheelhouse.mjs"
WHEELHOUSE_MANIFEST = ROOT / "scripts" / "python-wheelhouse.manifest.json"
SIGN_SCRIPT = ROOT / "scripts" / "sign-windows.ps1"
TAURI_CONFIG = ROOT / "src-tauri" / "tauri.conf.json"
TAURI_MAIN = ROOT / "src-tauri" / "src" / "main.rs"
PACKAGE_JSON = ROOT / "package.json"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_desktop_build_prepares_backend_resource_bundle():
    package = json.loads(_text(PACKAGE_JSON))
    config = json.loads(_text(TAURI_CONFIG))

    assert (
        "node scripts/prepare-desktop-bundle.mjs && node scripts/prepare-python-runtime.mjs && node scripts/prepare-python-wheelhouse.mjs && tauri build"
        == package["scripts"]["desktop:build"]
    )
    assert config["bundle"]["resources"] == {
        "resources/backend": "backend",
        "resources/python": "python",
        "resources/wheelhouse": "wheelhouse",
    }


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
        "scripts/prepare-python-runtime.mjs",
        "scripts/prepare-python-wheelhouse.mjs",
        "scripts/python-runtime.manifest.json",
        "scripts/python-wheelhouse.manifest.json",
        "scripts/sign-windows.ps1",
    ]:
        assert excluded in script


def test_tauri_installed_mode_uses_local_appdata_and_preserves_runtime_state():
    main = _text(TAURI_MAIN)

    assert 'const INSTALLED_DATA_DIR: &str = "OdysseusData";' in main
    assert 'const LEGACY_INSTALLED_APP_DIR: &str = "Odysseus";' in main
    assert 'const PYTHON_RESOURCE_DIR: &str = "python";' in main
    assert 'const WHEELHOUSE_RESOURCE_DIR: &str = "wheelhouse";' in main
    assert 'const PYTHON_RUNTIME_MARKER: &str = ".odysseus-desktop-python-runtime";' in main
    assert 'std::env::var_os("LOCALAPPDATA")' in main
    assert 'app.path().resource_dir()?.join(BACKEND_RESOURCE_DIR)' in main
    assert 'app.path().resource_dir()?.join(PYTHON_RESOURCE_DIR)' in main
    assert 'app.path().resource_dir()?.join(WHEELHOUSE_RESOURCE_DIR)' in main
    assert 'command.env("ODYSSEUS_PYTHON_EXE", python_exe);' in main
    assert 'command.env("ODYSSEUS_WHEELHOUSE_DIR", wheelhouse_dir);' in main
    assert "migrate_legacy_installed_state" in main
    assert "remove_backend_venv" in main
    assert "find_dev_repo_root" in main
    assert "prepare_installed_backend" in main


def test_python_runtime_manifest_and_prepare_script_pin_managed_python():
    manifest = json.loads(_text(PYTHON_MANIFEST))
    script = _text(PYTHON_PREP_SCRIPT)

    assert manifest["name"] == "python"
    assert manifest["pythonVersion"] == "3.12.10"
    assert manifest["release"] == "nuget"
    assert manifest["target"] == "x64"
    assert manifest["flavor"] == "nuget"
    assert manifest["license"] == "Python-2.0"
    assert manifest["sha256"] == "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"
    assert manifest["extractRoot"] == "tools"

    for expected in [
        "src-tauri",
        "target",
        "python-runtime",
        "resources",
        "python.exe",
        "createHash",
        "tar",
        "extractRoot",
        "ensurepip",
        "venv",
        "ODYSSEUS_PYTHON_RUNTIME_ID.txt",
    ]:
        assert expected in script


def test_tauri_signing_hook_is_noop_by_default_and_env_driven():
    config = json.loads(_text(TAURI_CONFIG))
    signer = _text(SIGN_SCRIPT)

    sign_command = config["bundle"]["windows"]["signCommand"]
    assert sign_command["cmd"] == "powershell.exe"
    assert "../scripts/sign-windows.ps1" in sign_command["args"]
    assert "%1" in sign_command["args"]

    for expected in [
        "ODYSSEUS_SIGN_COMMAND",
        "ODYSSEUS_SIGNTOOL_PATH",
        "ODYSSEUS_CERT_THUMBPRINT",
        "ODYSSEUS_TIMESTAMP_URL",
        "Skipping Windows code signing",
    ]:
        assert expected in signer


def test_python_wheelhouse_manifest_and_prepare_script_are_reproducible():
    manifest = json.loads(_text(WHEELHOUSE_MANIFEST))
    script = _text(WHEELHOUSE_PREP_SCRIPT)

    assert manifest["schemaVersion"] == 1
    assert manifest["pythonVersion"] == "3.12.10"
    assert manifest["requirements"]["path"] == "requirements.txt"
    assert manifest["requirements"]["sha256"]
    assert manifest["wheelhouse"]["path"] == "src-tauri/resources/wheelhouse"
    assert len(manifest["wheels"]) >= 100

    first = manifest["wheels"][0]
    assert set(first) == {"filename", "size", "sha256"}
    assert first["filename"].endswith(".whl")
    assert first["size"] > 0
    assert len(first["sha256"]) == 64

    for expected in [
        "pip",
        "download",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--update-manifest",
        "ODYSSEUS_UPDATE_WHEELHOUSE_MANIFEST",
        "python-wheelhouse.manifest.json",
        "src-tauri",
        "resources",
        "wheelhouse",
    ]:
        assert expected in script
