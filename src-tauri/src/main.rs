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
const PYTHON_RESOURCE_DIR: &str = "python";
const WHEELHOUSE_RESOURCE_DIR: &str = "wheelhouse";
const INSTALLED_DATA_DIR: &str = "OdysseusData";
const LEGACY_INSTALLED_APP_DIR: &str = "Odysseus";
const PYTHON_RUNTIME_ID_FILE: &str = "ODYSSEUS_PYTHON_RUNTIME_ID.txt";
const PYTHON_RUNTIME_MARKER: &str = ".odysseus-desktop-python-runtime";
const PRESERVED_BACKEND_NAMES: &[&str] = &["data", "logs", "venv", ".env", PYTHON_RUNTIME_MARKER];

struct BackendLaunch {
    root: PathBuf,
    python_exe: Option<PathBuf>,
    wheelhouse_dir: Option<PathBuf>,
}

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
            let backend = resolve_backend_launch(app)?;
            let reused = health_ok();

            if !reused {
                let child = start_backend(&backend)?;
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
                append_log_line(&backend.root, "Reusing existing Odysseus backend");
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

fn resolve_backend_launch(app: &tauri::App) -> Result<BackendLaunch, Box<dyn std::error::Error>> {
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

fn prepare_installed_backend(
    app: &tauri::App,
) -> Result<BackendLaunch, Box<dyn std::error::Error>> {
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
