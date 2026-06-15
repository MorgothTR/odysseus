import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "..");
const PYTHON_DIR = path.join(REPO_ROOT, "src-tauri", "resources", "python");
const PYTHON_EXE = path.join(PYTHON_DIR, "python.exe");
const OUT_DIR = path.join(REPO_ROOT, "src-tauri", "resources", "wheelhouse");
const REQUIREMENTS_PATH = path.join(REPO_ROOT, "requirements.txt");
const MANIFEST_PATH = path.join(SCRIPT_DIR, "python-wheelhouse.manifest.json");
const LOCK_PATH = path.join(REPO_ROOT, "requirements.lock");
const UPDATE_MANIFEST =
  process.argv.includes("--update-manifest") || process.env.ODYSSEUS_UPDATE_WHEELHOUSE_MANIFEST === "1";

function normalizeRepoPath(file) {
  return path.relative(REPO_ROOT, file).replaceAll("\\", "/");
}

function sha256File(file) {
  return createHash("sha256").update(readFileSync(file)).digest("hex");
}

function stableJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function pythonVersion() {
  return execFileSync(PYTHON_EXE, ["-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
  }).trim();
}

function assertBundledPython() {
  if (!existsSync(PYTHON_EXE)) {
    throw new Error(
      `Bundled Python runtime is missing at ${PYTHON_EXE}. Run scripts/prepare-python-runtime.mjs first.`,
    );
  }
}

function downloadWheelhouse(useLock) {
  rmSync(OUT_DIR, { recursive: true, force: true });
  mkdirSync(OUT_DIR, { recursive: true });
  const args = [
    "-m",
    "pip",
    "download",
    "--disable-pip-version-check",
    "--only-binary=:all:",
    "--dest",
    OUT_DIR,
    "-r",
    REQUIREMENTS_PATH,
  ];
  // On normal builds, constrain every package (direct + transitive) to the
  // committed lock so the wheelhouse is reproducible and the manifest cannot
  // drift. When refreshing the manifest we deliberately resolve free to pick up
  // intended upgrades, then rewrite the lock from whatever resolved.
  if (useLock && existsSync(LOCK_PATH)) {
    args.push("-c", LOCK_PATH);
  }
  execFileSync(PYTHON_EXE, args, { cwd: REPO_ROOT, stdio: "inherit" });
}

function wheelEntries() {
  return readdirSync(OUT_DIR)
    .filter((name) => name.toLowerCase().endsWith(".whl"))
    .sort((left, right) => left.localeCompare(right))
    .map((filename) => {
      const fullPath = path.join(OUT_DIR, filename);
      return {
        filename,
        size: statSync(fullPath).size,
        sha256: sha256File(fullPath),
      };
    });
}

function buildManifest() {
  return {
    schemaVersion: 1,
    pythonVersion: pythonVersion(),
    requirements: {
      path: normalizeRepoPath(REQUIREMENTS_PATH),
      sha256: sha256File(REQUIREMENTS_PATH),
    },
    wheelhouse: {
      path: normalizeRepoPath(OUT_DIR),
    },
    wheels: wheelEntries(),
  };
}

function writeLockFile(manifest) {
  // Wheel filenames escape the distribution name's separators to underscores,
  // so the version is always the 2nd '-'-separated field; normalize the name to
  // PEP 503 form. Pinning the whole resolved set makes builds reproducible.
  const pins = manifest.wheels
    .map((wheel) => {
      const parts = wheel.filename.split("-");
      const name = parts[0].replaceAll("_", "-").toLowerCase();
      return `${name}==${parts[1]}`;
    })
    .sort((left, right) => left.localeCompare(right));
  const header = [
    "# Auto-generated lock - do NOT edit by hand.",
    "# Pins every wheel (direct + transitive) so release builds are reproducible",
    "# and scripts/python-wheelhouse.manifest.json stops drifting. Used as a pip",
    "# constraints file (-c) on normal builds; rewritten on --update-manifest.",
    "# Regenerate after changing requirements.txt:",
    "#   node scripts/prepare-python-wheelhouse.mjs --update-manifest",
    `# Python ${manifest.pythonVersion}, ${pins.length} packages.`,
    "",
  ];
  writeFileSync(LOCK_PATH, `${header.join("\n")}${pins.join("\n")}\n`, "utf8");
}

function verifyManifest(actual) {
  if (!existsSync(MANIFEST_PATH)) {
    throw new Error(
      `Wheelhouse manifest is missing at ${normalizeRepoPath(MANIFEST_PATH)}. Run: node scripts/prepare-python-wheelhouse.mjs --update-manifest`,
    );
  }

  const expected = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));
  if (stableJson(actual) !== stableJson(expected)) {
    throw new Error(
      `Wheelhouse differs from committed manifest. Run: node scripts/prepare-python-wheelhouse.mjs --update-manifest`,
    );
  }
}

assertBundledPython();
downloadWheelhouse(!UPDATE_MANIFEST);

const manifest = buildManifest();
if (manifest.wheels.length === 0) {
  throw new Error("Wheelhouse download produced no wheels.");
}

if (UPDATE_MANIFEST) {
  writeFileSync(MANIFEST_PATH, stableJson(manifest), "utf8");
  writeLockFile(manifest);
  console.log(`Updated Python wheelhouse manifest: ${normalizeRepoPath(MANIFEST_PATH)}`);
  console.log(`Updated requirements lock: ${normalizeRepoPath(LOCK_PATH)}`);
} else {
  verifyManifest(manifest);
}

const size = manifest.wheels.reduce((total, wheel) => total + wheel.size, 0);
console.log(
  `Prepared Python wheelhouse: ${manifest.wheels.length} wheels, ${(size / 1024 / 1024).toFixed(2)} MB -> ${normalizeRepoPath(OUT_DIR)}`,
);
