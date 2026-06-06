# Windows Desktop Wrapper

This is the Tauri desktop shell for Odysseus on Windows. It does not bundle
Python, change Docker support, or rebuild the frontend. It opens the existing
Odysseus UI from `http://127.0.0.1:7000`.

## Prerequisites

- Windows 10/11 with WebView2 Runtime.
- Python 3.11+ from <https://www.python.org/downloads/windows/>.
- Node.js LTS/npm from <https://nodejs.org/>.
- Rust/Cargo from <https://www.rust-lang.org/tools/install>.
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

## First Run

Desktop startup output is appended to:

```text
logs/odysseus-desktop.log
```

On first run, setup may create the initial admin account with a temporary
password. If the desktop shell started the backend, check this log for the
password and change it after login.

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

```text
data/chroma
```

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

**Norton, SmartScreen, or antivirus warnings:** this developer-checkout build is
unsigned. Review the path shown by the warning, prefer locally built binaries
from this repo, and avoid allowing unrelated PowerShell commands. Code signing
is not part of this phase.

## Build Smoke Test

```powershell
npm run desktop:build
```

This is still a developer-checkout wrapper, not a standalone installer. Docker
flows remain unchanged for users who prefer Compose.
