"""Read-only local code review swarm tool.

This is intentionally an Odysseus-native orchestration layer rather than a
provider-specific "agent swarm" API. It fans a confined code snapshot out to a
small set of specialist reviewers, then asks the configured utility model to
synthesize their findings. It never writes files and never bypasses the same
allowed-root policy used by grep/glob/ls.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_SWARM_AGENTS = 5
MAX_SWARM_AGENTS = 10
MAX_PARALLEL_REVIEWERS = 5
MAX_FILES_LISTED = 300
MAX_REVIEW_FILES = 36
MAX_FILE_BYTES = 160_000
MAX_SNAPSHOT_CHARS = 32_000
MAX_SNIPPET_CHARS_PER_FILE = 3_500
MAX_FINAL_REPORT_CHARS = 18_000

ProgressCallback = Optional[Callable[[Dict[str, Any]], Awaitable[None]]]
Candidate = Tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".cache", "site-packages", ".idea", ".tox", "target",
})

TEXT_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".css", ".scss",
    ".html", ".htm", ".json", ".toml", ".yaml", ".yml", ".md", ".txt",
    ".rs", ".go", ".java", ".cs", ".cpp", ".c", ".h", ".hpp", ".sh",
    ".ps1", ".bat", ".cmd", ".sql", ".ini", ".cfg", ".env.example",
})

HIGH_VALUE_FILENAMES = frozenset({
    "readme.md", "agents.md", "skill.md", "pyproject.toml", "package.json",
    "requirements.txt", "cargo.toml", "tauri.conf.json", "dockerfile",
    "docker-compose.yml", "vite.config.js", "vite.config.ts", "tsconfig.json",
})

SENSITIVE_NAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "secrets.json", "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys",
    "known_hosts",
})

SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".crt", ".cer")

ROLE_FOCUS = {
    "architecture": "module boundaries, coupling, cohesion, maintainability, and clear ownership",
    "correctness": "bugs, edge cases, error handling, data flow, and surprising behavior",
    "security": "unsafe filesystem/network use, secrets exposure, auth/permission mistakes, injection risks",
    "tests": "missing regression tests, brittle tests, fixture gaps, and manual verification needs",
    "performance": "expensive loops, I/O hotspots, memory pressure, startup cost, and concurrency risks",
    "portability": "Windows/Linux/macOS path assumptions, packaging, environment assumptions, and runtime drift",
    "api": "public interfaces, request/response contracts, backward compatibility, and integration behavior",
    "ux": "user-facing behavior, failure messages, confusing flows, and operational ergonomics",
    "dependencies": "dependency risk, version drift, optional packages, and build reproducibility",
    "docs": "missing operator docs, setup gaps, misleading comments, and release notes",
}

DEFAULT_ROLES = [
    "architecture",
    "correctness",
    "security",
    "tests",
    "performance",
    "portability",
    "api",
    "ux",
    "dependencies",
    "docs",
]


@dataclass
class SwarmArgs:
    path: str
    goal: str
    roles: List[str]
    model: str
    max_files: int


@dataclass
class FileSample:
    relpath: str
    size: int
    text: str


@dataclass
class Snapshot:
    root: str
    files_seen: int
    files_listed: List[str]
    samples: List[FileSample]
    skipped_large: int
    skipped_sensitive: int
    extension_counts: Dict[str, int]


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _normalize_role_name(role: Any) -> str:
    text = str(role or "").strip().lower()
    text = "".join(ch if ch.isalnum() or ch in ("-", "_", " ") else " " for ch in text)
    text = " ".join(text.replace("_", " ").replace("-", " ").split())
    return text[:48]


def _select_roles(args: Dict[str, Any]) -> List[str]:
    requested = args.get("agents") or args.get("roles")
    roles: List[str] = []
    if isinstance(requested, list):
        for item in requested:
            role = _normalize_role_name(item)
            if role and role not in roles:
                roles.append(role)

    default_count = len(roles) if roles else DEFAULT_SWARM_AGENTS
    count = _clamp_int(args.get("agent_count") or args.get("agents_count"), default_count, 1, MAX_SWARM_AGENTS)
    for role in DEFAULT_ROLES:
        if len(roles) >= count:
            break
        if role not in roles:
            roles.append(role)
    return roles[:count]


def _parse_args(content: str) -> SwarmArgs:
    raw = (content or "").strip()
    data: Dict[str, Any] = {}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("run_code_review_swarm expects a JSON object")
        data = parsed
    else:
        lines = raw.splitlines()
        if lines:
            data["path"] = lines[0].strip()
            if len(lines) > 1:
                data["goal"] = "\n".join(lines[1:]).strip()

    path = str(data.get("path") or "").strip()
    if not path:
        raise ValueError("path is required")
    goal = str(data.get("goal") or "Review code quality and identify the highest-risk issues.").strip()
    model = str(data.get("model") or "").strip()
    max_files = _clamp_int(data.get("max_files"), MAX_REVIEW_FILES, 1, 120)
    return SwarmArgs(path=path, goal=goal, roles=_select_roles(data), model=model, max_files=max_files)


def _is_sensitive_file(path: str) -> bool:
    from src.tool_execution import _is_sensitive_path

    name = os.path.basename(path).lower()
    if name in SENSITIVE_NAMES:
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return _is_sensitive_path(path)


def _should_sample_file(path: str) -> bool:
    name = os.path.basename(path).lower()
    ext = Path(path).suffix.lower()
    return name in HIGH_VALUE_FILENAMES or ext in TEXT_EXTENSIONS


def _file_priority(relpath: str) -> int:
    rel = relpath.replace("\\", "/").lower()
    name = os.path.basename(rel).lower()
    ext = Path(name).suffix.lower()
    score = 0
    if name in HIGH_VALUE_FILENAMES:
        score += 80
    if "/test" in rel or name.startswith("test_") or name.endswith("_test.py"):
        score += 25
    if ext in {".py", ".ts", ".tsx", ".js", ".rs", ".go", ".cs"}:
        score += 45
    if ext in {".md", ".toml", ".json", ".yml", ".yaml"}:
        score += 25
    depth = rel.count("/")
    score -= depth * 3
    return score


def _iter_files(root: str) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS
        ]
        for filename in filenames:
            yield os.path.join(dirpath, filename)


def _collect_snapshot(root: str, *, max_files: int = MAX_REVIEW_FILES) -> Snapshot:
    root_real = os.path.realpath(root)
    all_files: List[Tuple[str, str, int]] = []
    skipped_large = 0
    skipped_sensitive = 0
    ext_counts: Dict[str, int] = {}

    for path in _iter_files(root_real):
        try:
            if _is_sensitive_file(path):
                skipped_sensitive += 1
                continue
            stat = os.stat(path)
        except OSError:
            continue
        rel = os.path.relpath(path, root_real).replace("\\", "/")
        all_files.append((path, rel, stat.st_size))
        ext = Path(path).suffix.lower() or "<none>"
        ext_counts[ext] = ext_counts.get(ext, 0) + 1
        if stat.st_size > MAX_FILE_BYTES:
            skipped_large += 1

    all_files.sort(key=lambda item: (-_file_priority(item[1]), item[1].lower()))
    listed = [rel for _, rel, _ in sorted(all_files, key=lambda item: item[1].lower())[:MAX_FILES_LISTED]]

    samples: List[FileSample] = []
    budget = MAX_SNAPSHOT_CHARS
    for path, rel, size in all_files:
        if len(samples) >= max_files or budget <= 0:
            break
        if size > MAX_FILE_BYTES or not _should_sample_file(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(min(MAX_SNIPPET_CHARS_PER_FILE + 1, budget + 1))
        except (OSError, UnicodeError):
            continue
        if not text:
            continue
        if len(text) > MAX_SNIPPET_CHARS_PER_FILE:
            text = text[:MAX_SNIPPET_CHARS_PER_FILE] + "\n... [file truncated for review snapshot]"
        if len(text) > budget:
            text = text[:budget] + "\n... [snapshot budget exhausted]"
        budget -= len(text)
        samples.append(FileSample(relpath=rel, size=size, text=text))

    return Snapshot(
        root=root_real,
        files_seen=len(all_files),
        files_listed=listed,
        samples=samples,
        skipped_large=skipped_large,
        skipped_sensitive=skipped_sensitive,
        extension_counts=dict(sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:16]),
    )


def _render_snapshot(snapshot: Snapshot) -> str:
    parts = [
        f"Root: {snapshot.root}",
        f"Files seen: {snapshot.files_seen}",
        f"Sensitive files skipped: {snapshot.skipped_sensitive}",
        f"Large files skipped: {snapshot.skipped_large}",
        "Extensions: " + json.dumps(snapshot.extension_counts, sort_keys=True),
        "",
        "File tree sample:",
    ]
    for rel in snapshot.files_listed:
        parts.append(f"- {rel}")
    if snapshot.files_seen > len(snapshot.files_listed):
        parts.append(f"- ... {snapshot.files_seen - len(snapshot.files_listed)} more files")
    parts.append("")
    parts.append("Selected file excerpts:")
    for sample in snapshot.samples:
        parts.append(f"\n--- {sample.relpath} ({sample.size} bytes) ---")
        parts.append(sample.text)
    return "\n".join(parts)


def _role_focus(role: str) -> str:
    normalized = _normalize_role_name(role)
    if normalized in ROLE_FOCUS:
        return ROLE_FOCUS[normalized]
    for key, focus in ROLE_FOCUS.items():
        if key in normalized:
            return focus
    return f"{role} concerns, practical code quality, and defects relevant to this project"


def _review_messages(role: str, goal: str, snapshot_text: str) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are one specialist reviewer in a read-only local code review swarm. "
                "Use only the provided repository snapshot. Do not claim you ran commands. "
                "Prefer concrete, evidence-backed findings over generic advice. "
                "Mention relative file paths when possible. If evidence is insufficient, say so."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Review goal:\n{goal}\n\n"
                f"Your specialist role: {role}\nFocus on: {_role_focus(role)}.\n\n"
                "Return concise markdown with:\n"
                "1. Findings ordered by severity, each with evidence and suggested fix.\n"
                "2. Test or verification gaps.\n"
                "3. Limits of this snapshot.\n\n"
                f"Repository snapshot:\n{snapshot_text}"
            ),
        },
    ]


def _synthesis_messages(goal: str, snapshot: Snapshot, reviewer_outputs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    sections = []
    for item in reviewer_outputs:
        sections.append(f"## Reviewer: {item['role']}\n{item['output']}")
    return [
        {
            "role": "system",
            "content": (
                "You are the lead reviewer synthesizing a read-only code review swarm. "
                "Deduplicate findings, rank by practical risk, and do not invent line numbers. "
                "Mark speculative items as 'needs verification'. Keep the report actionable."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Review goal:\n{goal}\n\n"
                f"Root reviewed: {snapshot.root}\n"
                f"Files seen: {snapshot.files_seen}; files sampled: {len(snapshot.samples)}; "
                f"sensitive files skipped: {snapshot.skipped_sensitive}; large files skipped: {snapshot.skipped_large}.\n\n"
                "Reviewer outputs:\n\n"
                + "\n\n".join(sections)
                + "\n\nReturn final markdown with: Summary, High-priority findings, Medium/low findings, Tests to add, and Limits."
            ),
        },
    ]


async def _emit(progress_cb: ProgressCallback, start: float, text: str) -> None:
    if not progress_cb:
        return
    try:
        await progress_cb({"elapsed": int(time.time() - start), "tail": text})
    except Exception:
        pass


def _resolve_review_root(raw_path: str, workspace: Optional[str]) -> str:
    from src.tool_execution import _resolve_search_root

    root = _resolve_search_root(raw_path, workspace)
    if not os.path.isdir(root):
        raise ValueError(f"path '{raw_path}' is not an existing directory")
    return root


def _resolve_candidates(model_spec: str, owner: Optional[str]) -> Tuple[List[Candidate], str]:
    if model_spec:
        from src.ai_interaction import _resolve_model

        url, model, headers = _resolve_model(model_spec, owner=owner)
        return [(url, model, headers)], model

    from src.endpoint_resolver import resolve_endpoint, resolve_utility_fallback_candidates

    url, model, headers = resolve_endpoint("utility", owner=owner)
    candidates: List[Candidate] = []
    if url and model:
        candidates.append((url, model, headers))
    candidates.extend(resolve_utility_fallback_candidates(owner=owner))
    if not candidates:
        raise ValueError("No utility/default model endpoint is configured for the code review swarm")
    return candidates, str(candidates[0][1] or "")


async def _call_llm(candidates: List[Candidate], messages: List[Dict[str, str]], *, max_tokens: int) -> str:
    from src.llm_core import llm_call_async_with_fallback

    return await llm_call_async_with_fallback(
        candidates,
        messages,
        temperature=0.2,
        max_tokens=max_tokens,
        timeout=240,
        prompt_type="code_review_swarm",
    )


def _truncate_report(text: str) -> str:
    if len(text) <= MAX_FINAL_REPORT_CHARS:
        return text
    return text[:MAX_FINAL_REPORT_CHARS] + f"\n\n... [swarm report truncated at {MAX_FINAL_REPORT_CHARS} chars]"


async def run_code_review_swarm(
    content: str,
    *,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
    workspace: Optional[str] = None,
    progress_cb: ProgressCallback = None,
) -> Dict[str, Any]:
    del session_id  # Reserved for future per-chat swarm records.
    start = time.time()
    try:
        args = _parse_args(content)
        root = _resolve_review_root(args.path, workspace)
        await _emit(progress_cb, start, f"swarm: collecting code snapshot from {root}")
        snapshot = await asyncio.to_thread(_collect_snapshot, root, max_files=args.max_files)
        if not snapshot.samples:
            return {
                "error": "run_code_review_swarm: no reviewable text/code files found under the allowed folder",
                "exit_code": 1,
            }

        candidates, selected_model = _resolve_candidates(args.model, owner)
        snapshot_text = _render_snapshot(snapshot)
        await _emit(
            progress_cb,
            start,
            f"swarm: starting {len(args.roles)} reviewers with {len(snapshot.samples)} sampled files",
        )

        sem = asyncio.Semaphore(min(MAX_PARALLEL_REVIEWERS, len(args.roles)))
        completed = 0

        async def review_role(role: str) -> Dict[str, str]:
            nonlocal completed
            async with sem:
                try:
                    output = await _call_llm(
                        candidates,
                        _review_messages(role, args.goal, snapshot_text),
                        max_tokens=3000,
                    )
                    output = output.strip() or "(reviewer returned an empty response)"
                except Exception as exc:
                    output = f"Reviewer failed: {type(exc).__name__}: {exc}"
                completed += 1
                await _emit(progress_cb, start, f"swarm: {role} reviewer complete ({completed}/{len(args.roles)})")
                return {"role": role, "output": output}

        reviewer_outputs = await asyncio.gather(*(review_role(role) for role in args.roles))
        await _emit(progress_cb, start, "swarm: synthesizing reviewer findings")
        try:
            final = await _call_llm(
                candidates,
                _synthesis_messages(args.goal, snapshot, reviewer_outputs),
                max_tokens=5000,
            )
            final = final.strip()
        except Exception as exc:
            joined = "\n\n".join(f"## {item['role']}\n{item['output']}" for item in reviewer_outputs)
            final = (
                "# Code Review Swarm\n\n"
                f"Synthesis failed ({type(exc).__name__}: {exc}). Raw reviewer findings follow.\n\n"
                + joined
            )

        if not final:
            final = "\n\n".join(f"## {item['role']}\n{item['output']}" for item in reviewer_outputs)

        header = (
            "# Code Review Swarm\n\n"
            f"- Root: `{snapshot.root}`\n"
            f"- Goal: {args.goal}\n"
            f"- Reviewers: {', '.join(args.roles)}\n"
            f"- Model: {selected_model or 'configured utility/default'}\n"
            f"- Files seen: {snapshot.files_seen}; sampled: {len(snapshot.samples)}; "
            f"sensitive skipped: {snapshot.skipped_sensitive}; large skipped: {snapshot.skipped_large}\n\n"
        )
        report = _truncate_report(header + final)
        return {
            "output": report,
            "exit_code": 0,
            "swarm": {
                "root": snapshot.root,
                "goal": args.goal,
                "agents": args.roles,
                "agent_count": len(args.roles),
                "model": selected_model,
                "files_seen": snapshot.files_seen,
                "files_sampled": len(snapshot.samples),
                "sensitive_files_skipped": snapshot.skipped_sensitive,
                "duration_s": round(time.time() - start, 2),
                "read_only": True,
            },
        }
    except (json.JSONDecodeError, ValueError) as exc:
        return {"error": f"run_code_review_swarm: {exc}", "exit_code": 1}
    except Exception as exc:
        return {"error": f"run_code_review_swarm failed: {type(exc).__name__}: {exc}", "exit_code": 1}
