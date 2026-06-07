import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TAURI_MAIN = ROOT / "src-tauri" / "src" / "main.rs"
TAURI_CONFIG = ROOT / "src-tauri" / "tauri.conf.json"
TAURI_CAPABILITY = ROOT / "src-tauri" / "capabilities" / "default.json"
TAURI_CARGO = ROOT / "src-tauri" / "Cargo.toml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_body(text: str, name: str) -> str:
    start = text.index(f"fn {name}")
    candidates = [
        text.find("\nfn ", start + 1),
        text.find("\n#[tauri::command]", start + 1),
    ]
    end_candidates = [candidate for candidate in candidates if candidate != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end]


def test_startup_recovery_uses_local_custom_protocol_and_keeps_external_app_url():
    main = _text(TAURI_MAIN)
    config = json.loads(_text(TAURI_CONFIG))

    assert config["build"]["frontendDist"] == "http://127.0.0.1:7000"
    assert 'const STARTUP_SCHEME: &str = "odysseus-startup";' in main
    assert 'const STARTUP_URL: &str = "odysseus-startup://localhost/";' in main
    assert ".register_uri_scheme_protocol(STARTUP_SCHEME" in main
    assert "WebviewUrl::CustomProtocol(STARTUP_URL.parse()" in main
    assert "WebviewUrl::External(APP_URL.parse()" in main
    assert 'const MAIN_LABEL: &str = "main";' in main
    assert 'const STARTUP_LABEL: &str = "startup";' in main


def test_startup_progress_events_and_commands_are_registered():
    main = _text(TAURI_MAIN)

    for event in [
        "startup://checking-existing-backend",
        "startup://preparing-backend",
        "startup://starting-backend",
        "startup://waiting-for-health",
        "startup://opening-app",
        "startup://failed",
    ]:
        assert event in main

    for command in [
        "desktop_diagnostics",
        "open_desktop_log_folder",
        "retry_desktop_startup",
        "rebuild_desktop_venv_and_retry",
        "quit_desktop",
    ]:
        assert command in main

    assert "tauri::generate_handler![" in main
    assert "emit_to(STARTUP_LABEL" in main


def test_startup_window_capability_does_not_expand_plugin_permissions():
    capability = json.loads(_text(TAURI_CAPABILITY))
    cargo = _text(TAURI_CARGO).lower()

    assert capability["windows"] == ["main", "startup"]
    assert capability["permissions"] == ["core:default", "dialog:allow-open"]
    assert "tauri-plugin-shell" not in cargo
    assert "tauri-plugin-fs" not in cargo
    assert "tauri-plugin-opener" not in cargo


def test_port_conflict_path_does_not_spawn_backend():
    main = _text(TAURI_MAIN)

    assert "fn port_listening()" in main
    assert "record_port_conflict(app);" in main
    assert "Port 7000 is already in use" in main
    assert main.index("if port_listening()") < main.index("start_backend(&backend)")


def test_retry_and_repair_use_single_startup_task_guard():
    main = _text(TAURI_MAIN)
    retry = _function_body(main, "retry_desktop_startup")
    repair = _function_body(main, "rebuild_desktop_venv_and_retry")
    task = _function_body(main, "start_desktop_startup_task")

    assert "startup_active: Mutex<bool>" in main
    assert "fn mark_startup_active" in main
    assert "Odysseus startup is already running." in main
    assert "start_desktop_startup_task(app" in retry
    assert "start_desktop_startup_task(app" in repair
    assert "thread::spawn(move || run_desktop_startup(app))" in task
    assert "start_backend(&backend)" not in retry
    assert "start_backend(&backend)" not in repair


def test_installed_repair_is_gated_and_hidden_for_port_conflicts():
    main = _text(TAURI_MAIN)
    target = _function_body(main, "repair_venv_target")

    assert 'button id="repair" hidden' in main
    assert "Repair available:" in main
    assert "setRepairVisibility(diagnostics)" in main
    assert 'snapshot.mode != "installed"' in target
    assert "snapshot.python_exe.is_none()" in target
    assert "snapshot.port_conflict" in target
    assert "Port 7000 is already in use. Stop the other service before repairing the venv." in target


def test_repair_deletes_only_verified_installed_venv():
    main = _text(TAURI_MAIN)
    repair = _function_body(main, "rebuild_desktop_venv_and_retry")
    remove = _function_body(main, "remove_repair_venv_only")
    target = _function_body(main, "repair_venv_target")

    assert "local_data_root()" in target
    assert '.join("backend")' in target
    assert '.join("venv")' in target
    assert "same_path_or_equal" in target
    assert "remove_repair_venv_only(&venv)" in repair
    assert 'name.eq_ignore_ascii_case("venv")' in remove
    assert "fs::remove_dir_all(venv)" in remove
    assert "fs::remove_file(venv)" in remove
    for protected in [
        'join("data")',
        'join("logs")',
        'join(".env")',
        'join("auth.json")',
        'join("settings.json")',
        'join("chroma")',
    ]:
        assert protected not in repair


def test_installed_first_launch_gets_longer_health_timeout():
    main = _text(TAURI_MAIN)

    assert "const DEV_STARTUP_TIMEOUT: Duration = Duration::from_secs(180);" in main
    assert "const INSTALLED_STARTUP_TIMEOUT: Duration = Duration::from_secs(420);" in main
    assert "fn startup_timeout_for(backend: &BackendLaunch) -> Duration" in main
    assert "backend.python_exe.is_some()" in main
    assert "wait_for_health(startup_timeout)" in main


def test_desktop_diagnostics_redacts_sensitive_log_lines():
    main = _text(TAURI_MAIN)

    assert "fn redact_diagnostic_line" in main
    assert "[redacted sensitive log line]" in main
    for sensitive in [
        "password",
        "api_key",
        "secret",
        "token",
        "authorization",
        "cookie",
        "bearer ",
        "private_key",
        "credential",
        ".env",
        "auth.json",
    ]:
        assert sensitive in main

    for expected in [
        "App version:",
        "Backend root:",
        "Log path:",
        "Last repair action:",
        "Startup active:",
        "Repair available:",
        "Installed venv:",
        "Health probe healthy:",
        "Port 7000 listening:",
        "Redacted desktop log tail",
    ]:
        assert expected in main
