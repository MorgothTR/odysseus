"""
checkpoints.py — automatic file snapshots + undo for agent edits.

Every successful edit_file/write_file records the file's pre-edit (and post-edit)
content here, so a bad edit can be rolled back with one tool call. This is the
safety net that lets you trust the agent to touch real code.

Storage (outside the user's repo): data/checkpoints/<workspace_hash>/
  log.jsonl            append-only: one JSON record per edit (metadata only)
  blobs/<sha256>       content-addressed blobs (pre/post content; deduped)
  meta.json            {workspace: <abs path>}

Restore writes a blob back to its file. The log is capped (newest kept); blobs
no longer referenced by the kept log are garbage-collected. Recording is always
best-effort — a checkpoint failure must NEVER break the edit that triggered it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_LOG_ENTRIES = 300          # per workspace
_MAX_BLOB_BYTES = 5_000_000     # don't checkpoint files larger than ~5MB


def _root() -> str:
    try:
        from src.constants import DATA_DIR
        base = DATA_DIR
    except Exception:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return os.path.join(base, "checkpoints")


def _ws_key(workspace: Optional[str]) -> str:
    if not workspace:
        return "global"
    norm = os.path.normcase(os.path.realpath(workspace))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _ws_dir(workspace: Optional[str]) -> str:
    d = os.path.join(_root(), _ws_key(workspace))
    os.makedirs(os.path.join(d, "blobs"), exist_ok=True)
    return d


def _log_path(workspace: Optional[str]) -> str:
    return os.path.join(_ws_dir(workspace), "log.jsonl")


def _write_blob(workspace: Optional[str], text: str) -> str:
    """Content-address `text`; write the blob if new. Returns its sha256."""
    sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    bp = os.path.join(_ws_dir(workspace), "blobs", sha)
    if not os.path.exists(bp):
        tmp = bp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, bp)
    return sha


def _read_blob(workspace: Optional[str], sha: str) -> Optional[str]:
    bp = os.path.join(_ws_dir(workspace), "blobs", sha)
    try:
        with open(bp, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _read_log(workspace: Optional[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(_log_path(workspace), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return out


def _write_log(workspace: Optional[str], entries: List[Dict[str, Any]]) -> None:
    tmp = _log_path(workspace) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, _log_path(workspace))


def _prune(workspace: Optional[str], entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cap the log to the newest _MAX_LOG_ENTRIES and GC unreferenced blobs."""
    if len(entries) <= _MAX_LOG_ENTRIES:
        kept = entries
    else:
        kept = entries[-_MAX_LOG_ENTRIES:]
        _write_log(workspace, kept)
    # GC blobs no longer referenced by any kept entry.
    referenced = set()
    for e in kept:
        if e.get("pre_sha"):
            referenced.add(e["pre_sha"])
        if e.get("post_sha"):
            referenced.add(e["post_sha"])
    blob_dir = os.path.join(_ws_dir(workspace), "blobs")
    try:
        for name in os.listdir(blob_dir):
            if name.endswith(".tmp"):
                continue
            if name not in referenced:
                try:
                    os.remove(os.path.join(blob_dir, name))
                except OSError:
                    pass
    except OSError:
        pass
    return kept


# ── public API ───────────────────────────────────────────────────────────────

def record_edit(
    abs_path: str,
    pre_content: str,
    post_content: str,
    *,
    tool: str,
    workspace: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Record one edit. Best-effort — returns the checkpoint id, or None on any
    failure (callers must ignore failures so a checkpoint problem never breaks
    the edit)."""
    try:
        if pre_content == post_content:
            return None  # no-op edit, nothing to checkpoint
        if len(pre_content) > _MAX_BLOB_BYTES or len(post_content) > _MAX_BLOB_BYTES:
            logger.info("checkpoints: skipping oversized file %s", abs_path)
            return None
        try:
            rel = os.path.relpath(abs_path, workspace) if workspace else abs_path
        except ValueError:
            rel = abs_path
        cid = hashlib.sha256(f"{abs_path}\x00{time.time()}\x00{tool}".encode()).hexdigest()[:12]
        entry = {
            "id": cid,
            "ts": time.time(),
            "abs_path": os.path.realpath(abs_path),
            "rel": rel.replace("\\", "/"),
            "tool": tool,
            "session": session_id or "",
            "pre_sha": _write_blob(workspace, pre_content),
            "post_sha": _write_blob(workspace, post_content),
            "pre_size": len(pre_content),
            "post_size": len(post_content),
        }
        entries = _read_log(workspace)
        entries.append(entry)
        with open(_log_path(workspace), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _prune(workspace, entries)
        return cid
    except Exception as e:
        logger.warning("checkpoints: record_edit failed for %s: %s", abs_path, e)
        return None


def list_edits(workspace: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Recent edits, newest first (metadata only)."""
    entries = _read_log(workspace)
    entries.reverse()
    return entries[: max(1, limit)]


def _entry_diff(workspace: Optional[str], entry: Dict[str, Any]) -> str:
    from src.tool_execution import _unified_diff
    pre = _read_blob(workspace, entry.get("pre_sha", "")) or ""
    post = _read_blob(workspace, entry.get("post_sha", "")) or ""
    return _unified_diff(pre, post, entry.get("rel", "")) or "(no textual diff)"


def restore(
    which: str,
    *,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore file(s) to a recorded pre-edit state.

    `which`:
      "last"            → undo the single most recent edit
      "last N"          → undo the N most recent edits (each file back to its
                          earliest pre-state within that window)
      "<checkpoint id>" → undo that specific edit
      "<path>"          → restore that file to its most recent pre-edit state
    """
    entries = _read_log(workspace)
    if not entries:
        return {"restored": [], "message": "No checkpoints recorded yet."}

    newest_first = list(reversed(entries))
    targets: List[Dict[str, Any]] = []
    w = (which or "last").strip()

    if w.lower() == "last":
        targets = newest_first[:1]
    elif w.lower().startswith("last "):
        try:
            n = int(w.split()[1])
        except (IndexError, ValueError):
            n = 1
        # For each file in the window, restore to its EARLIEST pre-state — that
        # cleanly unwinds repeated edits of the same file back to where it was
        # before this batch started.
        window = newest_first[: max(1, n)]
        seen = {}
        for e in window:               # newest→oldest; keep the oldest per file
            seen[e["abs_path"]] = e
        targets = list(seen.values())
    else:
        # checkpoint id?
        by_id = next((e for e in entries if e.get("id") == w), None)
        if by_id:
            targets = [by_id]
        else:
            # treat as a path — restore its most recent pre-state
            wnorm = os.path.normcase(os.path.realpath(
                os.path.join(workspace, w) if (workspace and not os.path.isabs(w)) else w
            ))
            match = next((e for e in newest_first
                          if os.path.normcase(e.get("abs_path", "")) == wnorm
                          or e.get("rel") == w.replace("\\", "/")), None)
            if not match:
                return {"restored": [], "message": f"No checkpoint found for '{which}'."}
            targets = [match]

    restored, skipped = [], []
    for e in targets:
        abs_path = e.get("abs_path", "")
        # Confine: if a workspace is set, never write outside it.
        if workspace:
            base = os.path.normcase(os.path.realpath(workspace))
            if os.path.commonpath([os.path.normcase(os.path.realpath(abs_path)), base]) != base:
                skipped.append({"file": e.get("rel"), "reason": "outside workspace"})
                continue
        pre = _read_blob(workspace, e.get("pre_sha", ""))
        if pre is None:
            skipped.append({"file": e.get("rel"), "reason": "snapshot missing"})
            continue
        try:
            d = os.path.dirname(abs_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = abs_path + ".ckpt-tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(pre)
            os.replace(tmp, abs_path)
            restored.append({"file": e.get("rel"), "id": e.get("id"), "bytes": len(pre)})
        except OSError as ex:
            skipped.append({"file": e.get("rel"), "reason": str(ex)})

    return {"restored": restored, "skipped": skipped}


# ── tool entry point ─────────────────────────────────────────────────────────

def _ago(ts: float) -> str:
    try:
        d = max(0, int(time.time() - ts))
    except Exception:
        return "?"
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def _parse(content: str) -> Tuple[str, str, int]:
    raw = (content or "").strip()
    action, target, limit = "list", "", 20
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            action = str(d.get("action") or "list").strip().lower()
            target = str(d.get("target") or d.get("id") or d.get("which")
                         or d.get("file") or d.get("path") or "").strip()
            limit = int(d.get("limit") or 20)
        except (ValueError, TypeError):
            action = raw.lower()
    else:
        parts = raw.split(None, 1)
        action = (parts[0].lower() if parts else "list")
        target = parts[1].strip() if len(parts) > 1 else ""
    if action in ("undo", "revert", "rollback"):
        action, target = "restore", (target or "last")
    if action == "restore" and not target:
        target = "last"
    return action, target, max(1, min(limit, 100))


async def manage_checkpoints(content: str, *, workspace: Optional[str] = None,
                             owner: Optional[str] = None) -> Dict[str, Any]:
    """Tool: inspect and roll back automatic edit checkpoints.

    action=list    → recent edits (newest first)
    action=restore → undo edits (target: last | last N | <id> | <file>)
    action=diff    → show what an edit changed (target: <id>)
    """
    action, target, limit = _parse(content)

    if action == "list":
        edits = list_edits(workspace, limit)
        if not edits:
            return {"exit_code": 0, "edits": [],
                    "output": "No checkpoints yet — they're recorded automatically when the agent edits files."}
        lines = [f"# Edit checkpoints ({len(edits)} most recent)"]
        for i, e in enumerate(edits, 1):
            lines.append(f"{i}. `{e.get('rel')}` — {e.get('tool')}, {_ago(e.get('ts', 0))}  (id: {e.get('id')})")
        lines.append("\nRestore with: `restore last`, `restore last 3`, `restore <id>`, or `restore <file>`.")
        return {"exit_code": 0, "edits": edits, "output": "\n".join(lines)}

    if action == "diff":
        entries = _read_log(workspace)
        e = next((x for x in entries if x.get("id") == target), None)
        if not e:
            return {"exit_code": 1, "error": "not found",
                    "output": f"No checkpoint with id '{target}'. Use action=list to see ids."}
        return {"exit_code": 0, "output": f"# Diff for `{e.get('rel')}` ({e.get('id')})\n```diff\n{_entry_diff(workspace, e)}\n```"}

    if action == "restore":
        res = restore(target, workspace=workspace)
        restored = res.get("restored", [])
        skipped = res.get("skipped", [])
        if not restored and not skipped:
            return {"exit_code": 0, "output": res.get("message", "Nothing to restore.")}
        lines = []
        if restored:
            lines.append(f"Restored {len(restored)} file(s) to their pre-edit state:")
            lines += [f"  - `{r['file']}` ({r['bytes']} bytes)" for r in restored]
        if skipped:
            lines.append("Skipped:")
            lines += [f"  - `{s['file']}` ({s['reason']})" for s in skipped]
        return {"exit_code": 0, "restored": restored, "skipped": skipped, "output": "\n".join(lines)}

    return {"exit_code": 1, "error": "unknown action",
            "output": f"checkpoint action '{action}' not recognized. Use list, restore, or diff."}
