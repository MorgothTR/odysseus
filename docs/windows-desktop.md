# Windows Desktop Wrapper

This is the Phase 1 Tauri desktop shell for Odysseus on Windows. It does not
bundle Python, change Docker support, or rebuild the frontend. It opens the
existing Odysseus UI from `http://127.0.0.1:7000`.

## Prerequisites

- Windows with WebView2 Runtime installed.
- Python 3.11+ available as `py -3.11`, `py -3.12`, `py -3.13`, or `python`.
- Node.js/npm.
- Rust/Cargo with the Windows build tools required by Tauri.

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

## Logs

Desktop startup output is appended to:

```text
logs/odysseus-desktop.log
```

On first run, setup may create the initial admin account with a temporary
password. If the desktop shell started the backend, check this log for the
password.

## Build Smoke Test

```powershell
npm run desktop:build
```

Phase 1 is a developer-checkout wrapper, not a standalone installer. ChromaDB,
allowed folders, LM Studio presets, and Docker flows are intentionally unchanged.
