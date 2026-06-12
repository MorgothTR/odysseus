import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { createWriteStream, cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { get } from "node:https";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
const MANIFEST_PATH = path.join(SCRIPT_DIR, "python-runtime.manifest.json");
const CACHE_DIR = path.join(REPO_ROOT, "src-tauri", "target", "python-runtime");
const OUT_DIR = path.join(REPO_ROOT, "src-tauri", "resources", "python");
const EXTRACT_DIR = path.join(CACHE_DIR, "extract");
const RUNTIME_ID_FILE = "ODYSSEUS_PYTHON_RUNTIME_ID.txt";
const RUNTIME_MANIFEST_FILE = "ODYSSEUS_PYTHON_RUNTIME.json";

const manifest = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));
const runtimeId = [
  `${manifest.name}-${manifest.pythonVersion}`,
  manifest.target,
  manifest.flavor,
].join("-");
const archivePath = path.join(CACHE_DIR, manifest.archiveName);

function sha256(file) {
  return createHash("sha256").update(readFileSync(file)).digest("hex");
}

function download(url, target, redirectCount = 0) {
  if (redirectCount > 5) {
    throw new Error(`Too many redirects while downloading ${url}`);
  }

  return new Promise((resolve, reject) => {
    get(url, (response) => {
      if ([301, 302, 303, 307, 308].includes(response.statusCode) && response.headers.location) {
        response.resume();
        const redirected = new URL(response.headers.location, url).toString();
        download(redirected, target, redirectCount + 1).then(resolve, reject);
        return;
      }

      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`Download failed with HTTP ${response.statusCode}: ${url}`));
        return;
      }

      const file = createWriteStream(target);
      response.pipe(file);
      file.on("finish", () => file.close(resolve));
      file.on("error", reject);
    }).on("error", reject);
  });
}

async function ensureArchive() {
  mkdirSync(CACHE_DIR, { recursive: true });
  if (existsSync(archivePath) && sha256(archivePath) === manifest.sha256) {
    return;
  }

  rmSync(archivePath, { force: true });
  console.log(`Downloading ${manifest.archiveName}`);
  try {
    await download(manifest.url, archivePath);
  } catch (error) {
    if (process.platform !== "win32") {
      throw error;
    }
    console.log(`Node download failed (${error.message}); retrying with Windows PowerShell.`);
    execFileSync(
      "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        "$ErrorActionPreference = 'Stop'; Invoke-WebRequest -Uri $env:ODYSSEUS_PYTHON_RUNTIME_URL -OutFile $env:ODYSSEUS_PYTHON_RUNTIME_OUT",
      ],
      {
        stdio: "inherit",
        env: {
          ...process.env,
          ODYSSEUS_PYTHON_RUNTIME_URL: manifest.url,
          ODYSSEUS_PYTHON_RUNTIME_OUT: archivePath,
        },
      },
    );
  }

  const actualSha = sha256(archivePath);
  if (actualSha !== manifest.sha256) {
    rmSync(archivePath, { force: true });
    throw new Error(`Python runtime SHA-256 mismatch. Expected ${manifest.sha256}, got ${actualSha}`);
  }
}

function prepareRuntime() {
  rmSync(EXTRACT_DIR, { recursive: true, force: true });
  mkdirSync(EXTRACT_DIR, { recursive: true });
  execFileSync("tar", ["-xf", archivePath, "-C", EXTRACT_DIR], { stdio: "inherit" });

  const extractedPython = path.join(EXTRACT_DIR, manifest.extractRoot || "python");
  const pythonExe = path.join(extractedPython, "python.exe");
  if (!existsSync(pythonExe)) {
    throw new Error(`Extracted runtime is missing python.exe at ${pythonExe}`);
  }

  rmSync(OUT_DIR, { recursive: true, force: true });
  mkdirSync(OUT_DIR, { recursive: true });
  cpSync(extractedPython, OUT_DIR, { recursive: true });

  const bundledPython = path.join(OUT_DIR, "python.exe");
  execFileSync(bundledPython, ["-c", "import ensurepip, venv; print('ok')"], { stdio: "ignore" });
  writeFileSync(path.join(OUT_DIR, RUNTIME_ID_FILE), `${runtimeId}\n`, "utf8");
  writeFileSync(
    path.join(OUT_DIR, RUNTIME_MANIFEST_FILE),
    `${JSON.stringify({ ...manifest, runtimeId }, null, 2)}\n`,
    "utf8",
  );
}

await ensureArchive();
prepareRuntime();
console.log(`Prepared Python runtime ${runtimeId} -> ${path.relative(REPO_ROOT, OUT_DIR)}`);
