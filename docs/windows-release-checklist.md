# Windows Unsigned Release Checklist

Use this checklist after `npm run desktop:release` when preparing a private
unsigned Windows build.

## Package And Verify

```powershell
npm run desktop:release
npm run desktop:release:verify
```

The release folder should contain:

```text
dist/windows-unsigned/Odysseus-1.0.0/
  Odysseus_1.0.0_x64-setup.exe
  Odysseus_1.0.0_x64_en-US.msi
  SHA256SUMS.txt
  RELEASE-NOTES-unsigned-windows.md
```

To verify a copied release folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-windows-release.ps1 -ReleaseDir "C:\Path\To\Odysseus-1.0.0"
```

## Clean Install Smoke

1. Install `Odysseus_1.0.0_x64-setup.exe` from the release folder.
2. Launch Odysseus from the Start Menu or installed app shortcut.
3. Confirm the startup window appears, then the setup/login page opens.
4. Confirm the backend is healthy:

```powershell
Invoke-WebRequest http://127.0.0.1:7000/api/health |
  Select-Object -ExpandProperty Content
```

5. Confirm runtime state exists under:

```text
%LOCALAPPDATA%\OdysseusData\backend
```

## Upgrade Smoke

1. Install the new NSIS artifact over an existing installed Odysseus desktop app.
2. Launch Odysseus and confirm login still works.
3. Confirm existing state is preserved under `%LOCALAPPDATA%\OdysseusData`, including:
   `data`, `logs`, `.env`, auth, settings, Chroma storage, and user files.
4. Confirm `/api/health` is healthy after relaunch.

## Self-Repair Smoke

1. Simulate a broken installed venv only when you are comfortable testing repair.
2. Launch the installed app and wait for the startup recovery window.
3. Click **Rebuild Venv & Retry**.
4. Confirm only `%LOCALAPPDATA%\OdysseusData\backend\venv` is recreated.
5. Confirm user data and settings remain intact.

## Uninstall Expectation

Uninstall should remove installed app binaries and shortcuts. User runtime data
under `%LOCALAPPDATA%\OdysseusData` is intentionally preserved unless manually
deleted by the user.

## Unsigned Warning Expectation

These artifacts are not code-signed. Windows SmartScreen, Microsoft Defender,
Norton, or other antivirus tools may warn that the publisher is unknown. For
private use, prefer artifacts you built locally or downloaded from a trusted
GitHub release, then compare SHA-256 hashes before installing.
