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

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const APP_URL: &str = "http://127.0.0.1:7000";
const HEALTH_ADDR: &str = "127.0.0.1:7000";
const HEALTH_REQUEST: &[u8] =
    b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:7000\r\nConnection: close\r\n\r\n";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(180);
const POLL_INTERVAL: Duration = Duration::from_millis(500);
const BACKEND_RESOURCE_DIR: &str = "backend";
const INSTALLED_APP_DIR: &str = "Odysseus";
const PRESERVED_BACKEND_NAMES: &[&str] = &["data", "logs", "venv", ".env"];

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
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
        .manage(BackendState::default())
        .setup(|app| {
            let repo_root = resolve_backend_root(app)?;
            let reused = health_ok();

            if !reused {
                let child = start_backend(&repo_root)?;
                let state = app.state::<BackendState>();
                let mut backend_child = state
                    .child
                    .lock()
                    .map_err(|_| Error::new(ErrorKind::Other, "backend lock poisoned"))?;
                *backend_child = Some(child);

                if !wait_for_health(STARTUP_TIMEOUT) {
                    stop_owned_backend(app.handle());
                    return Err(Error::new(
                        ErrorKind::TimedOut,
                        "Odysseus backend did not become healthy within 180 seconds",
                    )
                    .into());
                }
            } else {
                append_log_line(&repo_root, "Reusing existing Odysseus backend");
            }

            WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::External(APP_URL.parse().expect("valid Odysseus URL")),
            )
            .title("Odysseus")
            .inner_size(1280.0, 860.0)
            .min_inner_size(960.0, 640.0)
            .build()?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::CloseRequested { .. }) && window.label() == "main" {
                stop_owned_backend(window.app_handle());
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Odysseus desktop");
}

fn resolve_backend_root(app: &tauri::App) -> Result<PathBuf, Box<dyn std::error::Error>> {
    if let Some(repo_root) = find_dev_repo_root()? {
        return Ok(repo_root);
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

fn prepare_installed_backend(app: &tauri::App) -> Result<PathBuf, Box<dyn std::error::Error>> {
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

    let backend_root = local_app_root()?.join("backend");
    if same_path(&resource_backend, &backend_root) {
        fs::create_dir_all(&backend_root)?;
    } else {
        copy_backend_resources(&resource_backend, &backend_root)?;
    }
    append_log_line(
        &backend_root,
        &format!(
            "Prepared installed backend from {}",
            resource_backend.display()
        ),
    );
    Ok(backend_root)
}

fn local_app_root() -> Result<PathBuf, Box<dyn std::error::Error>> {
    let local_app_data = std::env::var_os("LOCALAPPDATA").ok_or_else(|| {
        Error::new(
            ErrorKind::NotFound,
            "LOCALAPPDATA is not set; cannot prepare installed Odysseus backend",
        )
    })?;
    Ok(PathBuf::from(local_app_data).join(INSTALLED_APP_DIR))
}

fn copy_backend_resources(source: &Path, target: &Path) -> Result<(), Box<dyn std::error::Error>> {
    fs::create_dir_all(target)?;
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

fn start_backend(repo_root: &Path) -> Result<Child, Box<dyn std::error::Error>> {
    fs::create_dir_all(repo_root.join("logs"))?;
    let log_path = repo_root.join("logs").join("odysseus-desktop.log");
    append_log_line(repo_root, "Starting Odysseus backend from desktop wrapper");

    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)?;
    let stderr = stdout.try_clone()?;

    let child = Command::new("powershell.exe")
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
        .stderr(Stdio::from(stderr))
        .spawn()?;

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
