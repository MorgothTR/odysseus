# Windows Desktop Wrapper

This is the Tauri desktop shell for Odysseus on Windows. It does not bundle
Python, change Docker support, or rebuild the frontend. It opens the existing
Odysseus UI from `http://127.0.0.1:7000`.

Phase 5A adds an unsigned installer prototype. Installed users still need
Python 3.11+, but they do not need Node.js, Rust, Docker, or a Git checkout.

## Prerequisites

- Windows 10/11 with WebView2 Runtime.
- Python 3.11+ from <https://www.python.org/downloads/windows/>.
- Node.js LTS/npm from <https://nodejs.org/> for development/builds only.
- Rust/Cargo from <https://www.rust-lang.org/tools/install> for
  development/builds only.
- Optional: Git for Windows from <https://git-scm.com/download/win> for full
  Cookbook and agent shell parity.

Run the read-only checker from the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check-windows-desktop.ps1
```

For the native backend only:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -CheckOnly
```

## Run In Development

From the repo root:

```powershell
npm install
npm run desktop:dev
```

The desktop shell first checks `http://127.0.0.1:7000/api/health`. If Odysseus
is already running, it reuses that backend and leaves it running when the window
closes. If nothing healthy is running, it starts:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launch-windows.ps1 -Desktop -Port 7000 -BindHost 127.0.0.1
```

When the desktop shell starts the backend, closing the desktop window stops only
that backend process tree.

## Installed Prototype

Build the unsigned prototype installer from a developer checkout:

```powershell
npm install
npm run desktop:build
```

The recommended manual-test artifact is:

```text
src-tauri/target/release/bundle/nsis/Odysseus_1.0.0_x64-setup.exe
```

When launched from the installed app, Odysseus copies its bundled backend files
to:

```text
%LOCALAPPDATA%\Odysseus\backend
```

Runtime state is preserved there across app launches:

```text
%LOCALAPPDATA%\Odysseus\backend\venv
%LOCALAPPDATA%\Odysseus\backend\data
%LOCALAPPDATA%\Odysseus\backend\logs
%LOCALAPPDATA%\Odysseus\backend\.env
```

The first installed launch creates the venv and installs Python dependencies,
so it can take several minutes and needs internet access for pip. This phase is
not code-signed, not auto-updating, and not fully offline.

## First Run

Desktop startup output is appended to:

```text
logs/odysseus-desktop.log
```

For an installed Phase 5A app, the log is under:

```text
%LOCALAPPDATA%\Odysseus\backend\logs\odysseus-desktop.log
```

On first installed launch, the login page should switch to first-time setup so
you can create your admin account in the UI. Developer/browser native launches
still use the existing terminal setup flow.

Verify the backend:

```powershell
Invoke-WebRequest http://127.0.0.1:7000/api/health |
  Select-Object -ExpandProperty Content
```

Verify native ChromaDB package state:

```powershell
.\venv\Scripts\python.exe -m pip show chromadb chromadb-client
```

Expected: `chromadb` is installed and `chromadb-client` is not found.

## Native Vector Storage

The desktop/native flow does not require Docker for ChromaDB. Unless
`CHROMADB_HOST` or `CHROMADB_PORT` is set, vector memory and RAG use embedded
ChromaDB storage in:

Developer checkout: `data/chroma`

Installed prototype: `%LOCALAPPDATA%\Odysseus\backend\data\chroma`

Set `CHROMADB_HOST` / `CHROMADB_PORT` only when you intentionally want Odysseus
to use a standalone ChromaDB service.

## Common Fixes

**Python missing:** install Python 3.11+ and make sure the Python launcher is
available. Re-run:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -CheckOnly
```

**npm missing:** install Node.js LTS, then run `npm install` again.

**Rust/Cargo missing:** install Rust, open a new PowerShell window, and run
`cargo --version`.

**WebView2 missing:** install Microsoft Edge WebView2 Runtime from
<https://developer.microsoft.com/microsoft-edge/webview2/>.

**pip certificate errors:** update Windows root certificates, then retry. If npm
has a local certificate issue, this environment has worked with:

```powershell
$env:NODE_OPTIONS='--use-system-ca'
npm install
```

**Port 7000 in use:** stop the existing app or run the native backend on another
port:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -Port 7010 -BindHost 127.0.0.1
```

**Norton, SmartScreen, or antivirus warnings:** Phase 5A is unsigned. Review the
path shown by the warning, prefer locally built binaries from this repo, and
avoid allowing unrelated PowerShell commands. Code signing is not part of this
phase.

## Build Smoke Test

```powershell
npm run desktop:build
```

Phase 5A produces an unsigned prototype installer that still requires Python
3.11+ and internet for first-run dependency installation. Docker flows remain
unchanged for users who prefer Compose.
