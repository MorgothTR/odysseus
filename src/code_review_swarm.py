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
# Default snapshot budget is sized for modest local-model contexts. Big-context
# models (kimi-k2.6 at 256K, etc.) can raise it per run via the snapshot_chars
# arg — every reviewer receives the full snapshot, so a large budget multiplies
# token spend by the reviewer count; that's why it's a dial, not the default.
MAX_SNAPSHOT_CHARS = 32_000
MAX_SNAPSHOT_CHARS_CEILING = 300_000
MAX_SNIPPET_CHARS_PER_FILE = 3_500
MAX_SNIPPET_CHARS_CEILING = 24_000
MAX_FINAL_REPORT_CHARS = 18_000

# Agentic mode (phase 20A): each reviewer is a sub-agent that explores the repo
# with read-only tools instead of reviewing a fixed snapshot. Round budget per
# reviewer — enough to list the tree, grep its patterns, read suspect files, and
# write findings, not enough to wander.
AGENTIC_REVIEWER_ROUNDS = 6

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
    snapshot_chars: int
    snippet_chars: int
    agentic: bool


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
    snapshot_chars = _clamp_int(
        data.get("snapshot_chars"), MAX_SNAPSHOT_CHARS, 8_000, MAX_SNAPSHOT_CHARS_CEILING
    )
    # A raised snapshot budget with the default 3.5K per-file cap just means
    # "more files, all shallow" — scale the per-file cap with the budget unless
    # the caller pinned it explicitly.
    default_snippet = _clamp_int(
        snapshot_chars // 10, MAX_SNIPPET_CHARS_PER_FILE,
        MAX_SNIPPET_CHARS_PER_FILE, MAX_SNIPPET_CHARS_CEILING,
    )
    snippet_chars = _clamp_int(
        data.get("snippet_chars"), default_snippet, 1_000, MAX_SNIPPET_CHARS_CEILING
    )
    agentic = bool(data.get("agentic", False))
    return SwarmArgs(
        path=path, goal=goal, roles=_select_roles(data), model=model,
        max_files=max_files, snapshot_chars=snapshot_chars, snippet_chars=snippet_chars,
        agentic=agentic,
    )


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


def _collect_snapshot(
    root: str,
    *,
    max_files: int = MAX_REVIEW_FILES,
    snapshot_chars: int = MAX_SNAPSHOT_CHARS,
    snippet_chars: int = MAX_SNIPPET_CHARS_PER_FILE,
) -> Snapshot:
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
    budget = snapshot_chars
    for path, rel, size in all_files:
        if len(samples) >= max_files or budget <= 0:
            break
        if size > MAX_FILE_BYTES or not _should_sample_file(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(min(snippet_chars + 1, budget + 1))
        except (OSError, UnicodeError):
            continue
        if not text:
            continue
        if len(text) > snippet_chars:
            text = text[:snippet_chars] + "\n... [file truncated for review snapshot]"
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


def _render_file_tree(snapshot: Snapshot) -> str:
    """Tree-only map (no file bodies) — the starter context an agentic reviewer
    gets before it reads files itself."""
    parts = [
        f"Files seen: {snapshot.files_seen}",
        "Extensions: " + json.dumps(snapshot.extension_counts, sort_keys=True),
        "",
        "File tree:",
    ]
    for rel in snapshot.files_listed:
        parts.append(f"- {rel}")
    if snapshot.files_seen > len(snapshot.files_listed):
        parts.append(f"- ... {snapshot.files_seen - len(snapshot.files_listed)} more files")
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
        # Reviewers are utility calls: on native Ollama, think=False stops a
        # thinking model (kimi-k2.6 etc.) from burning the token budget on
        # hidden reasoning before the visible answer. Other providers ignore it.
        think=False,
    )


def _truncate_report(text: str) -> str:
    if len(text) <= MAX_FINAL_REPORT_CHARS:
        return text
    return text[:MAX_FINAL_REPORT_CHARS] + f"\n\n... [swarm report truncated at {MAX_FINAL_REPORT_CHARS} chars]"


def _reviewer_system_prompt(role: str) -> str:
    """System framing for an agentic reviewer — a sub-agent that explores the
    repo with read-only tools rather than reviewing a fixed snapshot."""
    return (
        "You are one specialist reviewer in a read-only local code review swarm. "
        f"Your specialist role: {role}. Focus on: {_role_focus(role)}.\n"
        "You have READ-ONLY tools (read_file, grep, glob, ls) confined to the review root. "
        "Investigate the real code: glob/ls to map it, grep for the patterns your role cares "
        "about, and read the files you find suspicious. Ground every finding in code you "
        "actually opened — cite relative file paths. Do not claim you ran commands you did not. "
        "When done, return concise markdown: findings ordered by severity, each with the file "
        "path, the evidence, and a suggested fix; then note test/verification gaps and the "
        "limits of what you inspected."
    )


async def _run_reviewer_agent(
    role: str,
    goal: str,
    root: str,
    file_tree: str,
    candidates: List[Candidate],
    owner: Optional[str],
) -> Dict[str, Any]:
    """Run one reviewer as a confined read-only sub-agent. Returns the same
    {role, output, ok} shape as the snapshot reviewer so synthesis is unchanged."""
    from src.subagents import run_subagent, READONLY_TOOLSET

    user_message = (
        f"Review goal:\n{goal}\n\n"
        f"You are reviewing the code under your accessible root for the '{role}' perspective. "
        f"Here is the file tree to orient you — read the files you need:\n\n{file_tree}"
    )
    ok = False
    try:
        output = await run_subagent(
            goal=user_message,
            system_prompt=_reviewer_system_prompt(role),
            candidate=candidates[0],
            root=root,
            toolset=set(READONLY_TOOLSET),
            fallbacks=candidates[1:],
            max_rounds=AGENTIC_REVIEWER_ROUNDS,
            owner=owner,
        )
        stripped = (output or "").strip()
        if stripped:
            output, ok = stripped, True
        else:
            output = "(reviewer returned an empty response)"
    except Exception as exc:
        output = f"Reviewer failed: {type(exc).__name__}: {exc}"
    return {"role": role, "output": output, "ok": ok}


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
        snapshot = await asyncio.to_thread(
            _collect_snapshot, root,
            max_files=args.max_files,
            snapshot_chars=args.snapshot_chars,
            snippet_chars=args.snippet_chars,
        )
        if not snapshot.samples:
            return {
                "error": "run_code_review_swarm: no reviewable text/code files found under the allowed folder",
                "exit_code": 1,
            }

        candidates, selected_model = _resolve_candidates(args.model, owner)
        # Snapshot mode renders the full snapshot (tree + file excerpts) into the
        # prompt. Agentic mode renders only the tree as a starter map and lets
        # each reviewer read files itself with read-only tools.
        snapshot_text = "" if args.agentic else _render_snapshot(snapshot)
        file_tree = _render_file_tree(snapshot) if args.agentic else ""
        mode_label = "agentic reviewers (read-only tools)" if args.agentic else f"reviewers with {len(snapshot.samples)} sampled files"
        await _emit(
            progress_cb,
            start,
            f"swarm: starting {len(args.roles)} {mode_label}",
        )

        sem = asyncio.Semaphore(min(MAX_PARALLEL_REVIEWERS, len(args.roles)))
        completed = 0

        async def _snapshot_review(role: str) -> Dict[str, Any]:
            ok = False
            try:
                output = await _call_llm(
                    candidates,
                    _review_messages(role, args.goal, snapshot_text),
                    # Generous budget: thinking models (kimi-k2.6 etc.) spend
                    # tokens on hidden reasoning FIRST — at 3000 the reasoning
                    # consumed the whole budget over large snapshots and 4 of 5
                    # reviewers returned empty text.
                    max_tokens=9000,
                )
                stripped = output.strip()
                if stripped:
                    output, ok = stripped, True
                else:
                    output = "(reviewer returned an empty response)"
            except Exception as exc:
                output = f"Reviewer failed: {type(exc).__name__}: {exc}"
            return {"role": role, "output": output, "ok": ok}

        async def review_role(role: str) -> Dict[str, Any]:
            nonlocal completed
            async with sem:
                if args.agentic:
                    result = await _run_reviewer_agent(role, args.goal, root, file_tree, candidates, owner)
                else:
                    result = await _snapshot_review(role)
                completed += 1
                await _emit(progress_cb, start, f"swarm: {role} reviewer complete ({completed}/{len(args.roles)})")
                return result

        reviewer_outputs = await asyncio.gather(*(review_role(role) for role in args.roles))

        # Graceful degradation: synthesize from the reviewers that produced
        # substantive findings, not from empties/failures. A thinking model that
        # burns its budget on hidden reasoning (or a flaky endpoint) can leave
        # some reviewers blank; feeding those placeholders to synthesis dilutes
        # the report. If EVERY reviewer came back blank, fail loudly instead of
        # emitting an authoritative-looking but empty review.
        substantive = [item for item in reviewer_outputs if item["ok"]]
        failed_roles = [item["role"] for item in reviewer_outputs if not item["ok"]]
        if not substantive:
            detail = "; ".join(f"{item['role']}: {item['output'][:100]}" for item in reviewer_outputs)
            return {
                "error": (
                    "run_code_review_swarm: every reviewer returned empty or failed, so no "
                    "review was produced. With a thinking model this usually means the token "
                    "budget was consumed by hidden reasoning — try a smaller snapshot_chars or "
                    f"a non-thinking model. Reviewer details: {detail}"
                ),
                "exit_code": 1,
            }

        await _emit(progress_cb, start, "swarm: synthesizing reviewer findings")
        try:
            final = await _call_llm(
                candidates,
                _synthesis_messages(args.goal, snapshot, substantive),
                max_tokens=10000,  # same thinking-model headroom as reviewers
            )
            final = final.strip()
        except Exception as exc:
            joined = "\n\n".join(f"## {item['role']}\n{item['output']}" for item in substantive)
            final = (
                "# Code Review Swarm\n\n"
                f"Synthesis failed ({type(exc).__name__}: {exc}). Raw reviewer findings follow.\n\n"
                + joined
            )

        if not final:
            final = "\n\n".join(f"## {item['role']}\n{item['output']}" for item in substantive)

        reviewer_line = f"- Reviewers: {', '.join(args.roles)}"
        if failed_roles:
            reviewer_line += (
                f" ({len(substantive)}/{len(args.roles)} produced findings; "
                f"no usable output from: {', '.join(failed_roles)})"
            )
        mode = "agentic (read-only tools)" if args.agentic else "snapshot"
        files_line = (
            f"- Files seen: {snapshot.files_seen}; reviewers explored the tree with read-only tools\n\n"
            if args.agentic else
            f"- Files seen: {snapshot.files_seen}; sampled: {len(snapshot.samples)}; "
            f"sensitive skipped: {snapshot.skipped_sensitive}; large skipped: {snapshot.skipped_large}\n\n"
        )
        header = (
            "# Code Review Swarm\n\n"
            f"- Root: `{snapshot.root}`\n"
            f"- Goal: {args.goal}\n"
            f"- Mode: {mode}\n"
            f"{reviewer_line}\n"
            f"- Model: {selected_model or 'configured utility/default'}\n"
            f"{files_line}"
        )
        report = _truncate_report(header + final)
        return {
            "output": report,
            "exit_code": 0,
            "swarm": {
                "root": snapshot.root,
                "goal": args.goal,
                "mode": "agentic" if args.agentic else "snapshot",
                "agents": args.roles,
                "agent_count": len(args.roles),
                "reviewers_succeeded": len(substantive),
                "reviewers_failed": failed_roles,
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
