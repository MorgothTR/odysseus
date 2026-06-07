# Windows Desktop Wrapper

This is the Tauri desktop shell for Odysseus on Windows. It does not change
Docker support or rebuild the frontend. It opens the existing Odysseus UI from
`http://127.0.0.1:7000`.

Phase 6 adds a managed Python runtime and bundled dependency wheelhouse to the
unsigned installer prototype. Phase 7 adds a small startup/recovery window so
desktop users can see progress and copy safe diagnostics when startup fails.
Installed users do not need Python, Node.js, Rust, Docker, or a Git checkout
for the core app.

## Prerequisites

- Windows 10/11 with WebView2 Runtime.
- Python 3.11+ from <https://www.python.org/downloads/windows/> for developer
  checkout and browser/native runs.
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

A small startup window appears first. It reports backend preparation, startup,
health checks, and app opening progress before the main Odysseus window opens.

## Installed Prototype

Build the unsigned prototype installer from a developer checkout:

```powershell
npm install
npm run desktop:build
```

The build downloads a pinned official Python NuGet CPython runtime into
`src-tauri/target/python-runtime`, verifies its SHA-256, stages it under
`src-tauri/resources/python`, downloads the wheels in
`scripts/python-wheelhouse.manifest.json` into `src-tauri/resources/wheelhouse`,
then bundles both resources into the installer. The generated runtime and
wheelhouse resources are ignored by Git.

The recommended manual-test artifact is:

```text
src-tauri/target/release/bundle/nsis/Odysseus_1.0.0_x64-setup.exe
```

When launched from the installed app, Odysseus copies its bundled backend files
to:

```text
%LOCALAPPDATA%\OdysseusData\backend
```

Runtime state is preserved there across app launches:

```text
%LOCALAPPDATA%\OdysseusData\backend\venv
%LOCALAPPDATA%\OdysseusData\backend\data
%LOCALAPPDATA%\OdysseusData\backend\logs
%LOCALAPPDATA%\OdysseusData\backend\.env
```

The installed app uses the bundled Python runtime to create its own venv and
installs core dependencies from the bundled wheelhouse with `--no-index`, so
normal first launch does not need PyPI. Optional Cookbook/model downloads and
other user-triggered integrations may still use the network. This phase is not
code-signed, not auto-updating, and not fully offline.

The first installed launch can take several minutes while the venv is created,
dependencies are installed from the bundled wheelhouse, and the backend imports
for the first time. The startup window remains visible during that wait.

If you tested the Phase 5A installer, the current installed app copies existing
`data`, `logs`, and `.env` from `%LOCALAPPDATA%\Odysseus\backend` to the new
`%LOCALAPPDATA%\OdysseusData\backend` runtime root when those files are not
already present. The venv is recreated with the bundled Python runtime.

## First Run

Desktop startup output is appended to:

```text
logs/odysseus-desktop.log
```

For an installed app, the log is under:

```text
%LOCALAPPDATA%\OdysseusData\backend\logs\odysseus-desktop.log
```

On first installed launch, the login page should switch to first-time setup so
you can create your admin account in the UI. Developer/browser native launches
still use the existing terminal setup flow.

If desktop startup fails, the startup window stays open with:

- Copy Diagnostics: copies a redacted startup report.
- Open Logs: opens the Odysseus desktop log folder in Explorer.
- Quit: exits the desktop shell and stops only a backend process started by
  this desktop launch.

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

Installed prototype: `%LOCALAPPDATA%\OdysseusData\backend\data\chroma`

Set `CHROMADB_HOST` / `CHROMADB_PORT` only when you intentionally want Odysseus
to use a standalone ChromaDB service.

## Common Fixes

**Python missing:** this only applies to developer checkout/browser-native runs.
The installed desktop prototype bundles its own Python runtime. For developer
runs, install Python 3.11+ and make sure the Python launcher is available.
Re-run:

```powershell
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1 -CheckOnly
```

**npm missing:** install Node.js LTS, then run `npm install` again.

**Rust/Cargo missing:** install Rust, open a new PowerShell window, and run
`cargo --version`.

**WebView2 missing:** install Microsoft Edge WebView2 Runtime from
<https://developer.microsoft.com/microsoft-edge/webview2/>.

**pip certificate errors:** installed desktop first launch uses bundled wheels,
so PyPI certificate errors should not block the core app. Developer checkout
runs still use pip online; update Windows root certificates, then retry. If npm
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

The desktop shell uses port `7000`. If another non-Odysseus service is already
listening there, the startup window reports the conflict instead of launching a
second backend.

**Norton, SmartScreen, or antivirus warnings:** Phase 6 is unsigned. Review the
path shown by the warning, prefer locally built binaries from this repo, and
avoid allowing unrelated PowerShell commands. Code signing is not part of this
phase, but the build is signing-ready via `scripts/sign-windows.ps1` when
signing environment variables are configured.

## Build Smoke Test

```powershell
npm run desktop:build
```

Phase 6 produces an unsigned prototype installer that bundles Python and the
core dependency wheelhouse. Docker flows remain unchanged for users who prefer
Compose.
