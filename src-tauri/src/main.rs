use std::{
    fs::{self, OpenOptions},
    io::{Error, ErrorKind, Read, Write},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant, SystemTime},
};

use tauri::{
    http, AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};

const APP_URL: &str = "http://127.0.0.1:7000";
const HEALTH_ADDR: &str = "127.0.0.1:7000";
const HEALTH_REQUEST: &[u8] =
    b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:7000\r\nConnection: close\r\n\r\n";
const DEV_STARTUP_TIMEOUT: Duration = Duration::from_secs(180);
const INSTALLED_STARTUP_TIMEOUT: Duration = Duration::from_secs(420);
const POLL_INTERVAL: Duration = Duration::from_millis(500);
const BACKEND_RESOURCE_DIR: &str = "backend";
const PYTHON_RESOURCE_DIR: &str = "python";
const WHEELHOUSE_RESOURCE_DIR: &str = "wheelhouse";
const INSTALLED_DATA_DIR: &str = "OdysseusData";
const LEGACY_INSTALLED_APP_DIR: &str = "Odysseus";
const PYTHON_RUNTIME_ID_FILE: &str = "ODYSSEUS_PYTHON_RUNTIME_ID.txt";
const PYTHON_RUNTIME_MARKER: &str = ".odysseus-desktop-python-runtime";
const PRESERVED_BACKEND_NAMES: &[&str] = &["data", "logs", "venv", ".env", PYTHON_RUNTIME_MARKER];

const MAIN_LABEL: &str = "main";
const STARTUP_LABEL: &str = "startup";
const STARTUP_SCHEME: &str = "odysseus-startup";
const STARTUP_URL: &str = "odysseus-startup://localhost/";
const EVENT_CHECKING_BACKEND: &str = "startup://checking-existing-backend";
const EVENT_PREPARING_BACKEND: &str = "startup://preparing-backend";
const EVENT_STARTING_BACKEND: &str = "startup://starting-backend";
const EVENT_WAITING_FOR_HEALTH: &str = "startup://waiting-for-health";
const EVENT_OPENING_APP: &str = "startup://opening-app";
const EVENT_FAILED: &str = "startup://failed";

const STARTUP_HTML: &str = r#"<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Starting Odysseus</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: #111827;
      color: #f8fafc;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background:
        linear-gradient(135deg, rgba(17, 24, 39, 0.98), rgba(25, 33, 46, 0.98)),
        #111827;
    }
    main {
      width: min(440px, calc(100vw - 48px));
      padding: 28px;
    }
    h1 {
      margin: 0 0 10px;
      font-size: 24px;
      font-weight: 650;
      letter-spacing: 0;
    }
    #message {
      min-height: 48px;
      margin: 0 0 22px;
      color: #cbd5e1;
      line-height: 1.45;
      font-size: 14px;
    }
    .bar {
      width: 100%;
      height: 6px;
      overflow: hidden;
      background: #334155;
      border-radius: 3px;
    }
    .bar span {
      display: block;
      width: 44%;
      height: 100%;
      background: #38bdf8;
      border-radius: 3px;
      animation: slide 1.25s ease-in-out infinite;
    }
    @keyframes slide {
      0% { transform: translateX(-110%); }
      55% { transform: translateX(95%); }
      100% { transform: translateX(240%); }
    }
    #failure {
      display: none;
      margin-top: 18px;
    }
    #failure-text {
      white-space: pre-wrap;
      padding: 12px 0;
      color: #fecaca;
      line-height: 1.45;
      font-size: 13px;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    button {
      border: 1px solid #475569;
      background: #1f2937;
      color: #f8fafc;
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
    }
    button:hover { border-color: #38bdf8; }
    button[hidden] { display: none; }
    .repair-note {
      display: none;
      margin: 8px 0 0;
      color: #cbd5e1;
      font-size: 12px;
      line-height: 1.35;
    }
    textarea {
      position: fixed;
      left: -9999px;
      top: -9999px;
    }
  </style>
</head>
<body>
  <main>
    <h1>Starting Odysseus</h1>
    <p id="message">Preparing the desktop app...</p>
    <div class="bar" aria-hidden="true"><span></span></div>
    <section id="failure">
      <div id="failure-text"></div>
      <div class="actions">
        <button id="retry">Try Again</button>
        <button id="repair" hidden>Rebuild Venv & Retry</button>
        <button id="copy">Copy Diagnostics</button>
        <button id="logs">Open Logs</button>
        <button id="quit">Quit</button>
      </div>
      <p id="repair-note" class="repair-note">Rebuild keeps your data, settings, auth, logs, and Chroma store.</p>
    </section>
  </main>
  <textarea id="clipboard-fallback"></textarea>
  <script>
    const message = document.getElementById("message");
    const failure = document.getElementById("failure");
    const failureText = document.getElementById("failure-text");
    const retryButton = document.getElementById("retry");
    const repairButton = document.getElementById("repair");
    const repairNote = document.getElementById("repair-note");
    const copyButton = document.getElementById("copy");
    const logsButton = document.getElementById("logs");
    const quitButton = document.getElementById("quit");
    const fallback = document.getElementById("clipboard-fallback");
    const tauriApi = window.__TAURI__ || {};
    const listen = tauriApi.event && tauriApi.event.listen;
    const invoke = tauriApi.core && tauriApi.core.invoke;
    let failed = false;

    const statusText = {
      "startup://checking-existing-backend": "Checking for an existing Odysseus backend...",
      "startup://preparing-backend": "Preparing local desktop runtime...",
      "startup://starting-backend": "Starting the Odysseus backend...",
      "startup://waiting-for-health": "Waiting for Odysseus to become ready...",
      "startup://opening-app": "Opening Odysseus..."
    };

    async function register(eventName) {
      if (!listen) return;
      await listen(eventName, (event) => {
        const payload = event.payload || statusText[eventName] || "";
        message.textContent = payload;
      });
    }

    Object.keys(statusText).forEach((eventName) => register(eventName));
    function showFailure(text) {
      failed = true;
      message.textContent = "Odysseus could not start.";
      failureText.textContent = text || "Startup failed. Diagnostics may help.";
      failure.style.display = "block";
    }

    function resetForRetry(text) {
      failed = false;
      failure.style.display = "none";
      failureText.textContent = "";
      message.textContent = text;
    }

    function setRepairVisibility(diagnostics) {
      const repairAvailable = /^Repair available: true$/m.test(diagnostics);
      repairButton.hidden = !repairAvailable;
      repairNote.style.display = repairAvailable ? "block" : "none";
    }

    if (listen) {
      listen("startup://failed", (event) => showFailure(event.payload));
    }

    async function refreshFromDiagnostics() {
      if (!invoke) return;
      try {
        const diagnostics = await invoke("desktop_diagnostics");
        const status = diagnostics.match(/^Last status: (.*)$/m);
        const error = diagnostics.match(/^Last error: (.*)$/m);
        setRepairVisibility(diagnostics);
        if (!failed && status && status[1]) {
          message.textContent = status[1];
        }
        if (error && error[1] && error[1] !== "none") {
          showFailure(error[1]);
        }
      } catch (_) {
      }
    }

    setTimeout(refreshFromDiagnostics, 250);
    setInterval(refreshFromDiagnostics, 1000);

    async function invokeRecovery(command, text) {
      if (!invoke) return;
      resetForRetry(text);
      retryButton.disabled = true;
      repairButton.disabled = true;
      try {
        await invoke(command);
      } catch (error) {
        showFailure(String(error));
      } finally {
        retryButton.disabled = false;
        repairButton.disabled = false;
      }
    }

    retryButton.addEventListener("click", async () => {
      await invokeRecovery("retry_desktop_startup", "Retrying Odysseus startup...");
    });

    repairButton.addEventListener("click", async () => {
      await invokeRecovery("rebuild_desktop_venv_and_retry", "Rebuilding the installed venv and retrying...");
    });

    copyButton.addEventListener("click", async () => {
      if (!invoke) return;
      const diagnostics = await invoke("desktop_diagnostics");
      try {
        await navigator.clipboard.writeText(diagnostics);
      } catch (_) {
        fallback.value = diagnostics;
        fallback.select();
        document.execCommand("copy");
      }
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy Diagnostics"; }, 1800);
    });

    logsButton.addEventListener("click", async () => {
      if (invoke) await invoke("open_desktop_log_folder");
    });

    quitButton.addEventListener("click", async () => {
      if (invoke) await invoke("quit_desktop");
    });
  </script>
</body>
</html>
"#;

struct BackendLaunch {
    root: PathBuf,
    python_exe: Option<PathBuf>,
    wheelhouse_dir: Option<PathBuf>,
}

#[derive(Clone)]
struct DesktopDiagnostics {
    mode: String,
    backend_root: Option<PathBuf>,
    log_path: Option<PathBuf>,
    python_exe: Option<PathBuf>,
    wheelhouse_dir: Option<PathBuf>,
    last_status: String,
    last_error: Option<String>,
    last_repair_action: Option<String>,
    startup_active: bool,
    owned_backend: bool,
    reused_backend: bool,
    port_conflict: bool,
}

impl Default for DesktopDiagnostics {
    fn default() -> Self {
        Self {
            mode: "unknown".to_string(),
            backend_root: None,
            log_path: None,
            python_exe: None,
            wheelhouse_dir: None,
            last_status: "Starting Odysseus desktop".to_string(),
            last_error: None,
            last_repair_action: None,
            startup_active: false,
            owned_backend: false,
            reused_backend: false,
            port_conflict: false,
        }
    }
}

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
    startup_active: Mutex<bool>,
    diagnostics: Mutex<DesktopDiagnostics>,
}

impl Drop for BackendState {
    fn drop(&mut self) {
        let Ok(mut child) = self.child.lock() else {
            return;
        };
        if let Some(child) = child.take() {
            kill_process_tree(child);
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .register_uri_scheme_protocol(STARTUP_SCHEME, |_ctx, _request| {
            http::Response::builder()
                .header(http::header::CONTENT_TYPE, "text/html; charset=utf-8")
                .body(STARTUP_HTML.as_bytes().to_vec())
                .expect("valid startup response")
        })
        .invoke_handler(tauri::generate_handler![
            desktop_diagnostics,
            open_desktop_log_folder,
            retry_desktop_startup,
            rebuild_desktop_venv_and_retry,
            quit_desktop
        ])
        .manage(BackendState::default())
        .setup(|app| {
            create_startup_window(app)?;
            let app_handle = app.handle().clone();
            start_desktop_startup_task(app_handle, "Preparing local desktop runtime...")
                .map_err(|err| Error::new(ErrorKind::Other, err))?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if !matches!(event, WindowEvent::CloseRequested { .. }) {
                return;
            }

            if window.label() == MAIN_LABEL {
                stop_owned_backend(window.app_handle());
            } else if window.label() == STARTUP_LABEL
                && window.app_handle().get_webview_window(MAIN_LABEL).is_none()
            {
                stop_owned_backend(window.app_handle());
                window.app_handle().exit(0);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Odysseus desktop");
}

#[tauri::command]
fn desktop_diagnostics(state: State<'_, BackendState>) -> String {
    build_diagnostics(&state)
}

#[tauri::command]
fn open_desktop_log_folder(state: State<'_, BackendState>) -> Result<(), String> {
    let snapshot = diagnostics_snapshot(&state);
    let folder = snapshot
        .log_path
        .as_ref()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .or_else(|| snapshot.backend_root.as_ref().map(|root| root.join("logs")))
        .ok_or_else(|| "Log folder is not known yet.".to_string())?;

    Command::new("explorer.exe")
        .arg(folder)
        .spawn()
        .map_err(|err| format!("Failed to open log folder: {err}"))?;
    Ok(())
}

#[tauri::command]
fn retry_desktop_startup(app: AppHandle) -> Result<(), String> {
    let snapshot = diagnostics_snapshot(&app.state::<BackendState>());
    if snapshot.startup_active {
        return Err("Odysseus startup is already running.".to_string());
    }
    stop_owned_backend(&app);
    if let Some(root) = snapshot.backend_root {
        append_log_line(&root, "Retrying desktop startup without repair");
    }
    start_desktop_startup_task(app, "Retrying Odysseus startup...")
}

#[tauri::command]
fn rebuild_desktop_venv_and_retry(app: AppHandle) -> Result<(), String> {
    let snapshot = diagnostics_snapshot(&app.state::<BackendState>());
    let venv = repair_venv_target(&snapshot)?;
    stop_owned_backend(&app);
    remove_repair_venv_only(&venv).map_err(|err| {
        format!(
            "Failed to remove installed venv at {}: {err}",
            venv.display()
        )
    })?;
    if let Some(root) = venv.parent() {
        append_log_line(
            root,
            "Repair requested: removed installed backend venv; preserving data, logs, .env, auth, settings, and Chroma",
        );
    }
    update_diagnostics(&app, |diagnostics| {
        diagnostics.last_repair_action = Some(format!(
            "Removed installed backend venv at {}",
            venv.display()
        ));
        diagnostics.last_error = None;
        diagnostics.port_conflict = false;
    });
    start_desktop_startup_task(app, "Rebuilding installed venv and retrying startup...")
}

#[tauri::command]
fn quit_desktop(app: AppHandle) -> Result<(), String> {
    stop_owned_backend(&app);
    app.exit(0);
    Ok(())
}

fn create_startup_window(app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    WebviewWindowBuilder::new(
        app,
        STARTUP_LABEL,
        WebviewUrl::CustomProtocol(STARTUP_URL.parse().expect("valid startup URL")),
    )
    .title("Starting Odysseus")
    .inner_size(520.0, 420.0)
    .min_inner_size(480.0, 360.0)
    .build()?;
    Ok(())
}

fn start_desktop_startup_task(app: AppHandle, message: &str) -> Result<(), String> {
    mark_startup_active(&app)?;
    emit_startup_status(&app, EVENT_PREPARING_BACKEND, message);
    thread::spawn(move || run_desktop_startup(app));
    Ok(())
}

fn run_desktop_startup(app: AppHandle) {
    let result = run_desktop_startup_inner(&app);
    mark_startup_inactive(&app);
    if let Err(err) = result {
        fail_startup(&app, &err);
    }
}

fn run_desktop_startup_inner(app: &AppHandle) -> Result<(), String> {
    emit_startup_status(
        app,
        EVENT_PREPARING_BACKEND,
        "Preparing local desktop runtime...",
    );
    let backend = resolve_backend_launch(app).map_err(|err| err.to_string())?;
    record_backend(app, &backend);

    emit_startup_status(
        app,
        EVENT_CHECKING_BACKEND,
        "Checking for an existing Odysseus backend...",
    );
    if health_ok() {
        record_reused_backend(app);
        append_log_line(&backend.root, "Reusing existing Odysseus backend");
        emit_startup_status(app, EVENT_OPENING_APP, "Opening Odysseus...");
        open_main_window(app).map_err(|err| err.to_string())?;
        close_startup_window(app);
        return Ok(());
    }

    if port_listening() {
        record_port_conflict(app);
        return Err(
            "Port 7000 is already in use, but it is not an Odysseus backend. Stop the other service and launch Odysseus again.".to_string(),
        );
    }

    emit_startup_status(
        app,
        EVENT_STARTING_BACKEND,
        "Starting the Odysseus backend...",
    );
    let child = start_backend(&backend).map_err(|err| err.to_string())?;
    {
        let state = app.state::<BackendState>();
        let mut backend_child = state
            .child
            .lock()
            .map_err(|_| "backend lock poisoned".to_string())?;
        *backend_child = Some(child);
    }
    record_owned_backend(app);

    emit_startup_status(
        app,
        EVENT_WAITING_FOR_HEALTH,
        "Waiting for Odysseus to become ready...",
    );
    let startup_timeout = startup_timeout_for(&backend);
    if !wait_for_health(startup_timeout) {
        stop_owned_backend(app);
        return Err(format!(
            "Odysseus backend did not become healthy within {} seconds.",
            startup_timeout.as_secs()
        ));
    }

    emit_startup_status(app, EVENT_OPENING_APP, "Opening Odysseus...");
    open_main_window(app).map_err(|err| err.to_string())?;
    close_startup_window(app);
    Ok(())
}

fn open_main_window(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    WebviewWindowBuilder::new(
        app,
        MAIN_LABEL,
        WebviewUrl::External(APP_URL.parse().expect("valid Odysseus URL")),
    )
    .title("Odysseus")
    .inner_size(1280.0, 860.0)
    .min_inner_size(960.0, 640.0)
    .build()?;
    Ok(())
}

fn close_startup_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window(STARTUP_LABEL) {
        let _ = window.close();
    }
}

fn emit_startup_status(app: &AppHandle, event: &str, message: &str) {
    update_diagnostics(app, |diagnostics| {
        diagnostics.last_status = message.to_string();
        diagnostics.last_error = None;
    });
    let _ = app.emit_to(STARTUP_LABEL, event, message.to_string());
}

fn fail_startup(app: &AppHandle, message: &str) {
    update_diagnostics(app, |diagnostics| {
        diagnostics.last_status = "Startup failed".to_string();
        diagnostics.last_error = Some(message.to_string());
    });
    let _ = app.emit_to(STARTUP_LABEL, EVENT_FAILED, message.to_string());
    if let Some(window) = app.get_webview_window(STARTUP_LABEL) {
        let _ = window.set_title("Odysseus Startup Error");
    }
}

fn record_backend(app: &AppHandle, backend: &BackendLaunch) {
    update_diagnostics(app, |diagnostics| {
        diagnostics.mode = if backend.python_exe.is_some() {
            "installed".to_string()
        } else {
            "developer checkout".to_string()
        };
        diagnostics.backend_root = Some(backend.root.clone());
        diagnostics.log_path = Some(backend.root.join("logs").join("odysseus-desktop.log"));
        diagnostics.python_exe = backend.python_exe.clone();
        diagnostics.wheelhouse_dir = backend.wheelhouse_dir.clone();
        diagnostics.port_conflict = false;
    });
}

fn record_owned_backend(app: &AppHandle) {
    update_diagnostics(app, |diagnostics| {
        diagnostics.owned_backend = true;
        diagnostics.reused_backend = false;
    });
}

fn record_reused_backend(app: &AppHandle) {
    update_diagnostics(app, |diagnostics| {
        diagnostics.owned_backend = false;
        diagnostics.reused_backend = true;
    });
}

fn record_port_conflict(app: &AppHandle) {
    update_diagnostics(app, |diagnostics| {
        diagnostics.port_conflict = true;
        diagnostics.owned_backend = false;
        diagnostics.reused_backend = false;
    });
}

fn mark_startup_active(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<BackendState>();
    {
        let mut active = state
            .startup_active
            .lock()
            .map_err(|_| "startup guard lock poisoned".to_string())?;
        if *active {
            return Err("Odysseus startup is already running.".to_string());
        }
        *active = true;
    }
    update_diagnostics(app, |diagnostics| {
        diagnostics.startup_active = true;
        diagnostics.last_error = None;
    });
    Ok(())
}

fn mark_startup_inactive(app: &AppHandle) {
    let state = app.state::<BackendState>();
    if let Ok(mut active) = state.startup_active.lock() {
        *active = false;
    }
    update_diagnostics(app, |diagnostics| {
        diagnostics.startup_active = false;
    });
}

fn update_diagnostics<F>(app: &AppHandle, update: F)
where
    F: FnOnce(&mut DesktopDiagnostics),
{
    let state = app.state::<BackendState>();
    if let Ok(mut diagnostics) = state.diagnostics.lock() {
        update(&mut diagnostics);
    };
}

fn diagnostics_snapshot(state: &BackendState) -> DesktopDiagnostics {
    state
        .diagnostics
        .lock()
        .map(|diagnostics| diagnostics.clone())
        .unwrap_or_default()
}

fn build_diagnostics(state: &BackendState) -> String {
    let snapshot = diagnostics_snapshot(state);
    let health = health_ok();
    let port = port_listening();
    let mut output = String::new();
    output.push_str("Odysseus Desktop Diagnostics\n");
    output.push_str("============================\n");
    output.push_str(&format!("App version: {}\n", env!("CARGO_PKG_VERSION")));
    output.push_str(&format!("Mode: {}\n", snapshot.mode));
    output.push_str(&format!("Last status: {}\n", snapshot.last_status));
    output.push_str(&format!(
        "Last error: {}\n",
        value_or_none(&snapshot.last_error)
    ));
    output.push_str(&format!(
        "Last repair action: {}\n",
        value_or_none(&snapshot.last_repair_action)
    ));
    output.push_str(&format!("Startup active: {}\n", snapshot.startup_active));
    output.push_str(&format!("Owned backend: {}\n", snapshot.owned_backend));
    output.push_str(&format!("Reused backend: {}\n", snapshot.reused_backend));
    output.push_str(&format!("Port conflict: {}\n", snapshot.port_conflict));
    output.push_str(&format!(
        "Repair available: {}\n",
        repair_venv_target(&snapshot).is_ok()
    ));
    output.push_str(&format!("Health probe healthy: {}\n", health));
    output.push_str(&format!("Port 7000 listening: {}\n", port));
    output.push_str(&format!(
        "Backend root: {}\n",
        path_value(&snapshot.backend_root)
    ));
    output.push_str(&format!("Log path: {}\n", path_value(&snapshot.log_path)));
    output.push_str(&format!(
        "Bundled Python: {}\n",
        path_value(&snapshot.python_exe)
    ));
    output.push_str(&format!(
        "Bundled wheelhouse: {}\n",
        path_value(&snapshot.wheelhouse_dir)
    ));
    output.push_str(&format!(
        "Installed venv: {}\n",
        path_value(&snapshot.backend_root.as_ref().map(|root| root.join("venv")))
    ));
    output.push_str("\nRedacted desktop log tail\n");
    output.push_str("-------------------------\n");
    output.push_str(&redacted_log_tail(snapshot.log_path.as_deref()));
    output
}

fn repair_venv_target(snapshot: &DesktopDiagnostics) -> Result<PathBuf, String> {
    if snapshot.startup_active {
        return Err("Startup is already running.".to_string());
    }
    if snapshot.port_conflict {
        return Err(
            "Port 7000 is already in use. Stop the other service before repairing the venv."
                .to_string(),
        );
    }
    if snapshot.mode != "installed" || snapshot.python_exe.is_none() {
        return Err(
            "Venv repair is only available for installed Odysseus desktop launches.".to_string(),
        );
    }

    let backend_root = snapshot
        .backend_root
        .as_ref()
        .ok_or_else(|| "Backend root is not known yet.".to_string())?;
    let expected_backend_root = local_data_root()
        .map_err(|err| err.to_string())?
        .join("backend");
    if !same_path_or_equal(backend_root, &expected_backend_root) {
        return Err(format!(
            "Refusing to repair unexpected backend root: {}",
            backend_root.display()
        ));
    }

    let venv = backend_root.join("venv");
    let expected_venv = expected_backend_root.join("venv");
    if !same_path_or_equal(&venv, &expected_venv) {
        return Err(format!(
            "Refusing to repair unexpected venv path: {}",
            venv.display()
        ));
    }
    Ok(venv)
}

fn value_or_none(value: &Option<String>) -> String {
    value.clone().unwrap_or_else(|| "none".to_string())
}

fn path_value(path: &Option<PathBuf>) -> String {
    match path {
        Some(path) => format!("{} (exists: {})", path.display(), path.exists()),
        None => "unknown".to_string(),
    }
}

fn redacted_log_tail(log_path: Option<&Path>) -> String {
    let Some(log_path) = log_path else {
        return "Log path is not known yet.\n".to_string();
    };
    let Ok(contents) = fs::read_to_string(log_path) else {
        return format!("Could not read log at {}\n", log_path.display());
    };
    let lines: Vec<&str> = contents.lines().collect();
    let start = lines.len().saturating_sub(140);
    let mut output = String::new();
    for line in &lines[start..] {
        output.push_str(&redact_diagnostic_line(line));
        output.push('\n');
    }
    output
}

fn redact_diagnostic_line(line: &str) -> String {
    const SENSITIVE_DIAGNOSTIC_PATTERNS: &[&str] = &[
        "password",
        "passwd",
        "api_key",
        "apikey",
        "secret",
        "token",
        "authorization",
        "cookie",
        "set-cookie",
        "bearer ",
        "private_key",
        "credential",
        ".env",
        "auth.json",
    ];
    let lower = line.to_lowercase();
    if SENSITIVE_DIAGNOSTIC_PATTERNS
        .iter()
        .any(|pattern| lower.contains(pattern))
    {
        "[redacted sensitive log line]".to_string()
    } else {
        line.to_string()
    }
}

fn resolve_backend_launch(app: &AppHandle) -> Result<BackendLaunch, Box<dyn std::error::Error>> {
    if let Some(repo_root) = find_dev_repo_root()? {
        return Ok(BackendLaunch {
            root: repo_root,
            python_exe: None,
            wheelhouse_dir: None,
        });
    }

    prepare_installed_backend(app)
}

fn find_dev_repo_root() -> Result<Option<PathBuf>, Box<dyn std::error::Error>> {
    let mut starts = Vec::new();
    starts.push(std::env::current_dir()?);
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            starts.push(parent.to_path_buf());
        }
    }

    for start in starts {
        for dir in start.ancestors() {
            if dir.join("launch-windows.ps1").is_file() {
                return Ok(Some(dir.to_path_buf()));
            }
        }
    }

    Ok(None)
}

fn prepare_installed_backend(app: &AppHandle) -> Result<BackendLaunch, Box<dyn std::error::Error>> {
    let resource_backend = app.path().resource_dir()?.join(BACKEND_RESOURCE_DIR);
    if !resource_backend.join("launch-windows.ps1").is_file() {
        return Err(Error::new(
            ErrorKind::NotFound,
            format!(
                "Could not find bundled Odysseus backend at {}",
                resource_backend.display()
            ),
        )
        .into());
    }

    let resource_python = app.path().resource_dir()?.join(PYTHON_RESOURCE_DIR);
    let python_exe = resource_python.join("python.exe");
    if !python_exe.is_file() {
        return Err(Error::new(
            ErrorKind::NotFound,
            format!(
                "Could not find bundled Odysseus Python runtime at {}",
                python_exe.display()
            ),
        )
        .into());
    }

    let resource_wheelhouse = app.path().resource_dir()?.join(WHEELHOUSE_RESOURCE_DIR);
    if !resource_wheelhouse.is_dir() {
        return Err(Error::new(
            ErrorKind::NotFound,
            format!(
                "Could not find bundled Odysseus wheelhouse at {}",
                resource_wheelhouse.display()
            ),
        )
        .into());
    }

    let backend_root = local_data_root()?.join("backend");
    fs::create_dir_all(&backend_root)?;
    migrate_legacy_installed_state(&backend_root)?;
    let runtime_id = read_python_runtime_id(&resource_python)?;
    let runtime_changed = python_runtime_changed(&backend_root, &runtime_id);

    if same_path(&resource_backend, &backend_root) {
        fs::create_dir_all(&backend_root)?;
    } else {
        copy_backend_resources(&resource_backend, &backend_root)?;
    }
    if runtime_changed {
        remove_backend_venv(&backend_root)?;
        fs::write(backend_root.join(PYTHON_RUNTIME_MARKER), runtime_id)?;
    }
    append_log_line(
        &backend_root,
        &format!(
            "Prepared installed backend from {}",
            resource_backend.display()
        ),
    );
    Ok(BackendLaunch {
        root: backend_root,
        python_exe: Some(python_exe),
        wheelhouse_dir: Some(resource_wheelhouse),
    })
}

fn local_app_data() -> Result<PathBuf, Box<dyn std::error::Error>> {
    let local_app_data = std::env::var_os("LOCALAPPDATA").ok_or_else(|| {
        Error::new(
            ErrorKind::NotFound,
            "LOCALAPPDATA is not set; cannot prepare installed Odysseus backend",
        )
    })?;
    Ok(PathBuf::from(local_app_data))
}

fn local_data_root() -> Result<PathBuf, Box<dyn std::error::Error>> {
    Ok(local_app_data()?.join(INSTALLED_DATA_DIR))
}

fn migrate_legacy_installed_state(target: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let legacy = local_app_data()?
        .join(LEGACY_INSTALLED_APP_DIR)
        .join("backend");
    if !legacy.exists() || same_path(&legacy, target) {
        return Ok(());
    }

    fs::create_dir_all(target)?;
    for name in ["data", "logs", ".env"] {
        let source = legacy.join(name);
        let destination = target.join(name);
        if source.exists() && !destination.exists() {
            copy_backend_entry(&source, &destination)?;
        }
    }
    Ok(())
}

fn read_python_runtime_id(resource_python: &Path) -> Result<String, Box<dyn std::error::Error>> {
    let runtime_id_path = resource_python.join(PYTHON_RUNTIME_ID_FILE);
    let runtime_id = fs::read_to_string(&runtime_id_path).map_err(|err| {
        Error::new(
            err.kind(),
            format!(
                "Could not read bundled Python runtime id at {}: {}",
                runtime_id_path.display(),
                err
            ),
        )
    })?;
    Ok(runtime_id.trim().to_string())
}

fn python_runtime_changed(backend_root: &Path, runtime_id: &str) -> bool {
    let marker_path = backend_root.join(PYTHON_RUNTIME_MARKER);
    match fs::read_to_string(marker_path) {
        Ok(existing) => existing.trim() != runtime_id,
        Err(_) => true,
    }
}

fn remove_backend_venv(backend_root: &Path) -> std::io::Result<()> {
    let venv = backend_root.join("venv");
    if !venv.exists() {
        return Ok(());
    }
    let metadata = fs::metadata(&venv)?;
    if metadata.is_dir() {
        fs::remove_dir_all(venv)
    } else {
        fs::remove_file(venv)
    }
}

fn remove_repair_venv_only(venv: &Path) -> std::io::Result<()> {
    let name = venv.file_name().and_then(|name| name.to_str());
    if !matches!(name, Some(name) if name.eq_ignore_ascii_case("venv")) {
        return Err(Error::new(
            ErrorKind::InvalidInput,
            format!("refusing to remove non-venv path: {}", venv.display()),
        ));
    }
    if !venv.exists() {
        return Ok(());
    }
    let metadata = fs::metadata(venv)?;
    if metadata.is_dir() {
        fs::remove_dir_all(venv)
    } else {
        fs::remove_file(venv)
    }
}

fn copy_backend_resources(source: &Path, target: &Path) -> Result<(), Box<dyn std::error::Error>> {
    fs::create_dir_all(target)?;
    remove_unpreserved_backend_entries(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let name = entry.file_name();
        if is_preserved_backend_name(&name) {
            continue;
        }
        copy_backend_entry(&entry.path(), &target.join(name))?;
    }
    Ok(())
}

fn remove_unpreserved_backend_entries(target: &Path) -> std::io::Result<()> {
    if !target.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(target)? {
        let entry = entry?;
        if is_preserved_backend_name(&entry.file_name()) {
            continue;
        }
        let path = entry.path();
        let metadata = entry.metadata()?;
        if metadata.is_dir() {
            fs::remove_dir_all(path)?;
        } else {
            fs::remove_file(path)?;
        }
    }
    Ok(())
}

fn copy_backend_entry(source: &Path, target: &Path) -> std::io::Result<()> {
    let metadata = fs::metadata(source)?;
    if metadata.is_dir() {
        fs::create_dir_all(target)?;
        for entry in fs::read_dir(source)? {
            let entry = entry?;
            copy_backend_entry(&entry.path(), &target.join(entry.file_name()))?;
        }
    } else if metadata.is_file() {
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(source, target)?;
    }
    Ok(())
}

fn is_preserved_backend_name(name: &std::ffi::OsStr) -> bool {
    let name = name.to_string_lossy();
    PRESERVED_BACKEND_NAMES
        .iter()
        .any(|preserved| name.eq_ignore_ascii_case(preserved))
}

fn same_path(left: &Path, right: &Path) -> bool {
    match (fs::canonicalize(left), fs::canonicalize(right)) {
        (Ok(left), Ok(right)) => left == right,
        _ => false,
    }
}

fn same_path_or_equal(left: &Path, right: &Path) -> bool {
    if same_path(left, right) {
        return true;
    }
    path_text(left).eq_ignore_ascii_case(&path_text(right))
}

fn path_text(path: &Path) -> String {
    path.to_string_lossy()
        .trim_end_matches(['\\', '/'])
        .to_string()
}

fn health_ok() -> bool {
    let addr: SocketAddr = match HEALTH_ADDR.parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(700)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(700)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(700)));

    if stream.write_all(HEALTH_REQUEST).is_err() {
        return false;
    }

    let mut response = Vec::new();
    let mut buf = [0u8; 512];
    loop {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                response.extend_from_slice(&buf[..n]);
                if response.len() >= 4096 {
                    break;
                }
            }
            Err(_) => break,
        }
    }

    if response.is_empty() {
        return false;
    }

    let response = String::from_utf8_lossy(&response);
    let status_ok = response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200");
    let body_ok = response.contains("\"status\"") && response.contains("\"healthy\"");
    status_ok && body_ok
}

fn port_listening() -> bool {
    let addr: SocketAddr = match HEALTH_ADDR.parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(700)).is_ok()
}

fn startup_timeout_for(backend: &BackendLaunch) -> Duration {
    if backend.python_exe.is_some() {
        INSTALLED_STARTUP_TIMEOUT
    } else {
        DEV_STARTUP_TIMEOUT
    }
}

fn wait_for_health(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if health_ok() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn start_backend(backend: &BackendLaunch) -> Result<Child, Box<dyn std::error::Error>> {
    let repo_root = &backend.root;
    fs::create_dir_all(repo_root.join("logs"))?;
    let log_path = repo_root.join("logs").join("odysseus-desktop.log");
    append_log_line(repo_root, "Starting Odysseus backend from desktop wrapper");
    if let Some(python_exe) = &backend.python_exe {
        append_log_line(
            repo_root,
            &format!("Using bundled Python runtime at {}", python_exe.display()),
        );
    }
    if let Some(wheelhouse_dir) = &backend.wheelhouse_dir {
        append_log_line(
            repo_root,
            &format!(
                "Using bundled Python wheelhouse at {}",
                wheelhouse_dir.display()
            ),
        );
    }

    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)?;
    let stderr = stdout.try_clone()?;

    let mut command = Command::new("powershell.exe");
    command
        .current_dir(repo_root)
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-File")
        .arg(repo_root.join("launch-windows.ps1"))
        .arg("-Desktop")
        .arg("-Port")
        .arg("7000")
        .arg("-BindHost")
        .arg("127.0.0.1")
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));

    if let Some(python_exe) = &backend.python_exe {
        command.env("ODYSSEUS_PYTHON_EXE", python_exe);
    }
    if let Some(wheelhouse_dir) = &backend.wheelhouse_dir {
        command.env("ODYSSEUS_WHEELHOUSE_DIR", wheelhouse_dir);
    }

    let child = command.spawn()?;

    append_log_line(
        repo_root,
        &format!("Spawned backend launcher pid={}", child.id()),
    );
    Ok(child)
}

fn append_log_line(repo_root: &Path, message: &str) {
    let log_dir = repo_root.join("logs");
    if fs::create_dir_all(&log_dir).is_err() {
        return;
    }
    let log_path = log_dir.join("odysseus-desktop.log");
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(file, "[{:?}] {}", SystemTime::now(), message);
    }
}

fn stop_owned_backend(app: &tauri::AppHandle) {
    let state = app.state::<BackendState>();
    let mut guard = match state.child.lock() {
        Ok(guard) => guard,
        Err(_) => return,
    };
    let Some(child) = guard.take() else {
        return;
    };
    update_diagnostics(app, |diagnostics| {
        diagnostics.owned_backend = false;
    });

    kill_process_tree(child);
}

fn kill_process_tree(mut child: Child) {
    let pid = child.id().to_string();
    let _ = Command::new("taskkill")
        .args(["/F", "/T", "/PID", &pid])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = child.wait();
}
