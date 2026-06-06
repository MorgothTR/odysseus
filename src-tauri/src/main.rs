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

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendState::default())
        .setup(|app| {
            let repo_root = find_repo_root()?;
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

fn find_repo_root() -> Result<PathBuf, Box<dyn std::error::Error>> {
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
                return Ok(dir.to_path_buf());
            }
        }
    }

    Err(Error::new(
        ErrorKind::NotFound,
        "Could not find launch-windows.ps1 from current directory or executable path",
    )
    .into())
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
    let Some(mut child) = guard.take() else {
        return;
    };

    let pid = child.id().to_string();
    let _ = Command::new("taskkill")
        .args(["/F", "/T", "/PID", &pid])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = child.wait();
}
