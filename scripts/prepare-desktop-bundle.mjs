import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
const OUT_DIR = path.join(REPO_ROOT, "src-tauri", "resources", "backend");

const ROOT_FILES = new Set([
  ".env.example",
  "app.py",
  "launch-windows.ps1",
  "requirements.txt",
  "setup.py",
  "LICENSE",
  "ACKNOWLEDGMENTS.md",
]);

const RUNTIME_DIRS = [
  "companion",
  "config",
  "core",
  "integrations",
  "licenses",
  "mcp_servers",
  "routes",
  "scripts",
  "services",
  "src",
  "static",
];

const EXCLUDED_PREFIXES = [
  ".git/",
  ".github/",
  "data/",
  "dev-docs/",
  "docker/",
  "docs/",
  "logs/",
  "node_modules/",
  "reports/",
  "src-tauri/",
  "tasks/",
  "tests/",
  "venv/",
];

const EXCLUDED_FILES = new Set([
  ".dockerignore",
  ".gitignore",
  "Dockerfile",
  "docker-compose.gpu-amd.yml",
  "docker-compose.gpu-nvidia.yml",
  "docker-compose.yml",
  "scripts/check-windows-desktop.ps1",
  "scripts/prepare-desktop-bundle.mjs",
  "scripts/prepare-python-runtime.mjs",
  "scripts/prepare-python-wheelhouse.mjs",
  "scripts/python-runtime.manifest.json",
  "scripts/python-wheelhouse.manifest.json",
  "scripts/sign-windows.ps1",
]);

function normalizeGitPath(file) {
  return file.replaceAll("\\", "/");
}

function isRuntimeFile(file) {
  const normalized = normalizeGitPath(file);
  if (!normalized || EXCLUDED_FILES.has(normalized)) return false;
  if (EXCLUDED_PREFIXES.some((prefix) => normalized.startsWith(prefix))) return false;
  if (normalized.includes("/node_modules/")) return false;

  if (ROOT_FILES.has(normalized)) return true;
  return RUNTIME_DIRS.some((dir) => normalized === dir || normalized.startsWith(`${dir}/`));
}

function trackedFiles() {
  const output = execFileSync("git", ["ls-files", "-z"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  return output.split("\0").filter(Boolean).map(normalizeGitPath);
}

function untrackedRuntimeFiles() {
  const output = execFileSync("git", ["ls-files", "-z", "--others", "--exclude-standard"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  });
  return output.split("\0").filter(Boolean).map(normalizeGitPath).filter(isRuntimeFile);
}

function copyFileIntoBundle(file) {
  const source = path.join(REPO_ROOT, file);
  const target = path.join(OUT_DIR, file);
  if (!statSync(source).isFile()) return;
  mkdirSync(path.dirname(target), { recursive: true });
  cpSync(source, target, { force: true });
}

// The bundle is built from `git ls-files`, so an untracked runtime file would
// be silently absent from the installer while edits to tracked files around it
// are picked up — the installed app then half-runs the new feature.
const untracked = untrackedRuntimeFiles();
if (untracked.length) {
  console.error("Refusing to bundle: untracked runtime files would be skipped by the installer:");
  for (const file of untracked) console.error(`  ${file}`);
  console.error("git add (or delete) these files, then rebuild.");
  process.exit(1);
}

rmSync(OUT_DIR, { recursive: true, force: true });
mkdirSync(OUT_DIR, { recursive: true });

const files = trackedFiles().filter(isRuntimeFile).sort();
for (const file of files) {
  copyFileIntoBundle(file);
}

console.log(`Prepared desktop backend bundle: ${files.length} tracked files -> ${path.relative(REPO_ROOT, OUT_DIR)}`);
