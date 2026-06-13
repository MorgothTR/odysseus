"""
code_search.py — the ``semantic_code_search`` tool.

Indexes a workspace's source files into a vector store and searches them by
MEANING (not just literal text). Reuses the existing embedding-lane + ChromaDB
infrastructure (src/embedding_lanes.py, src/chroma_client.py) so it adds no new
embedding backend.

Design notes:
  * Chunks are LINE WINDOWS (not prose sentences) so every result carries a real
    ``file:line`` range — the thing that makes code search useful.
  * One shared collection (``odysseus_code``) scoped per workspace via a
    ``workspace`` metadata key + ``where`` filter, mirroring the owner-scoping
    the RAG store already uses.
  * Freshness: per-file mtime tracking. Each search re-embeds only the files that
    changed (or are new) since last time and drops chunks for files that were
    deleted — so the index stays correct as the agent edits the code.
  * Read-only and confined to the granted workspace (via _resolve_search_root).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_COLLECTION_BASE = "odysseus_code"

# Source extensions worth indexing.
_CODE_EXTS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".scala", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".swift", ".m",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".css", ".scss", ".less",
    ".html", ".vue", ".svelte", ".lua", ".r", ".jl", ".dart", ".ex", ".exs",
    ".json", ".yaml", ".yml", ".toml", ".md",
}

# Directory names never worth indexing (build output, deps, caches, VCS).
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", ".next", ".nuxt", "target", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "coverage", ".idea", ".vscode",
    "vendor", "bower_components", ".cache", "site-packages", ".tox",
    "fastembed_cache", "chroma", ".gradle", "Pods", ".terraform",
}

_LINES_PER_CHUNK = 60
_CHUNK_OVERLAP = 12
_MAX_FILE_BYTES = 1_000_000      # skip files > 1MB (generated/minified/data)
_MAX_CHUNKS_PER_INDEX = 4000     # bound the first-index embedding time
_DEFAULT_K = 8
_MAX_RESULTS = 15
_SNIPPET_CHARS = 700


# ── helpers ──────────────────────────────────────────────────────────────────

def _ws_id(root: str) -> str:
    """Stable short id for a workspace root — the per-workspace metadata filter."""
    norm = os.path.normcase(os.path.realpath(root))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _chunk_lines(text: str):
    """Yield (start_line, end_line, chunk_text) windows over text, 1-based lines."""
    lines = text.split("\n")
    n = len(lines)
    step = max(1, _LINES_PER_CHUNK - _CHUNK_OVERLAP)
    i = 0
    while i < n:
        end = min(i + _LINES_PER_CHUNK, n)
        chunk = "\n".join(lines[i:end])
        if chunk.strip():
            yield (i + 1, end, chunk)
        if end >= n:
            break
        i += step


def _iter_code_files(root: str):
    """Walk root yielding indexable source files, skipping junk dirs/big files."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored + hidden dirs in place so os.walk doesn't descend them.
        dirnames[:] = [
            d for d in dirnames
            if d not in _IGNORE_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in _CODE_EXTS:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield fp


def _parse_args(content: str) -> Tuple[str, int, str]:
    """Parse the tool input into (query, k, sub_path). Accepts a JSON object
    {"query","k","path"} or a bare query string."""
    raw = (content or "").strip()
    query, k, sub_path = "", _DEFAULT_K, ""
    if raw.startswith("{"):
        try:
            d = json.loads(raw)
            query = str(d.get("query") or d.get("q") or d.get("text") or "").strip()
            k = int(d.get("k") or d.get("limit") or d.get("top_k") or _DEFAULT_K)
            sub_path = str(d.get("path") or d.get("dir") or d.get("directory") or "").strip()
        except (ValueError, TypeError):
            query = raw
    else:
        query = raw
    k = max(1, min(k, _MAX_RESULTS))
    return query, k, sub_path


# ── index ────────────────────────────────────────────────────────────────────

class _CodeIndex:
    """Process-local semantic code index over granted workspaces."""

    def __init__(self):
        from src.embedding_lanes import build_embedding_lanes
        try:
            self._lanes = build_embedding_lanes(_COLLECTION_BASE)
        except Exception as e:
            logger.warning("code index: could not build embedding lanes: %s", e)
            self._lanes = []
        # {ws_id: {file_path: mtime}} — what we've already embedded this process.
        self._seen: Dict[str, Dict[str, float]] = {}

    @property
    def healthy(self) -> bool:
        return bool(self._lanes)

    def _delete_source(self, ws_id: str, source: str) -> None:
        for lane in self._lanes:
            try:
                lane.collection.delete(
                    where={"$and": [{"workspace": ws_id}, {"source": source}]}
                )
            except Exception as e:
                logger.warning("code index: delete %s failed: %s", source, e)

    def _add_chunks(self, ws_id: str, source: str, chunks: List[Tuple[int, int, str]]) -> None:
        if not chunks:
            return
        fname = os.path.basename(source)
        lang = os.path.splitext(source)[1].lower().lstrip(".")
        ids, docs, metas = [], [], []
        for (start, end, text) in chunks:
            cid = "code_" + hashlib.sha256(
                f"{ws_id}\x00{source}\x00{start}\x00{text}".encode("utf-8")
            ).hexdigest()[:20]
            ids.append(cid)
            docs.append(text)
            metas.append({
                "workspace": ws_id, "source": source, "filename": fname,
                "lang": lang, "start_line": start, "end_line": end,
            })
        for lane in self._lanes:
            for b in range(0, len(ids), 100):
                bi, bd, bm = ids[b:b + 100], docs[b:b + 100], metas[b:b + 100]
                try:
                    lane.collection.add(
                        ids=bi, documents=bd, metadatas=bm, embeddings=lane.encode(bd),
                    )
                except Exception as e:
                    logger.warning("code index: add to %s lane failed: %s", lane.name, e)
                    break

    def ensure_fresh(self, root: str, ws_id: str) -> Tuple[int, int, bool]:
        """Re-index changed/new files, drop deleted ones.
        Returns (files_reindexed, chunks_reindexed, truncated)."""
        seen = self._seen.setdefault(ws_id, {})
        current: Dict[str, float] = {}
        budget = _MAX_CHUNKS_PER_INDEX
        files_idx = chunks_idx = 0
        truncated = False
        for fp in _iter_code_files(root):
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            current[fp] = mt
            if seen.get(fp) == mt:
                continue  # unchanged since last index
            if budget <= 0:
                truncated = True
                break
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            chunks = list(_chunk_lines(text))[:budget]
            budget -= len(chunks)
            self._delete_source(ws_id, fp)   # clear stale chunks for this file first
            self._add_chunks(ws_id, fp, chunks)
            seen[fp] = mt
            files_idx += 1
            chunks_idx += len(chunks)
        # Files that vanished from disk → remove their chunks from the index.
        for gone in [p for p in seen if p not in current]:
            self._delete_source(ws_id, gone)
            seen.pop(gone, None)
        return files_idx, chunks_idx, truncated

    def search(self, query: str, root: str, k: int) -> Tuple[List[Dict[str, Any]], int, int, bool]:
        from src.embedding_lanes import query_lanes, dedupe_results, lane_count
        ws_id = _ws_id(root)
        files_idx, chunks_idx, truncated = self.ensure_fresh(root, ws_id)
        if lane_count(self._lanes) == 0:
            return [], files_idx, chunks_idx, truncated

        query_words = set(query.lower().split())
        candidates: List[Dict[str, Any]] = []
        for lane, results in query_lanes(
            self._lanes,
            query,
            n_results=lambda lane: min(max(k * 3, 20), lane.count()),
            include=["documents", "metadatas", "distances"],
            where={"workspace": ws_id},
        ):
            ids0 = (results.get("ids") or [[]])[0]
            for idx in range(len(ids0)):
                distance = results["distances"][0][idx]
                doc_text = results["documents"][0][idx]
                meta = results["metadatas"][0][idx]
                vsim = 1.0 - distance
                dwords = set(doc_text.lower().split())
                kscore = (len(query_words & dwords) / len(query_words)) if query_words else 0.0
                score = 0.75 * vsim + 0.25 * kscore
                candidates.append({
                    "id": ids0[idx],
                    "document": doc_text,
                    "metadata": meta,
                    "similarity": round(score, 4),
                    "vector_similarity": round(vsim, 4),
                })
        candidates.sort(key=lambda c: c["similarity"], reverse=True)
        top = dedupe_results(candidates, limit=k)
        return top, files_idx, chunks_idx, truncated


_index: Optional[_CodeIndex] = None


def _get_index() -> _CodeIndex:
    global _index
    if _index is None:
        _index = _CodeIndex()
    return _index


# ── output ───────────────────────────────────────────────────────────────────

def _format(query: str, root: str, top: List[Dict[str, Any]],
            files_idx: int, chunks_idx: int, truncated: bool) -> Dict[str, Any]:
    results = []
    lines = [f"# Semantic code search — {query!r}"]
    if files_idx:
        note = f"(indexed {files_idx} changed/new file(s), {chunks_idx} chunk(s)"
        note += " — workspace large, partial index)" if truncated else ")"
        lines.append(note)
    if not top:
        body = lines[:1] + ["", f"No semantically-relevant code found for: {query!r}."]
        return {"exit_code": 0, "results": [], "output": "\n".join(body),
                "indexed": {"files": files_idx, "chunks": chunks_idx}}
    lines.append("")
    for r in top:
        m = r.get("metadata") or {}
        src = m.get("source", "?")
        try:
            rel = os.path.relpath(src, root) if src != "?" else "?"
        except ValueError:
            rel = src
        s, e = m.get("start_line"), m.get("end_line")
        loc = f"{rel}:{s}-{e}" if s and e else rel
        snippet = r.get("document", "")
        if len(snippet) > _SNIPPET_CHARS:
            snippet = snippet[:_SNIPPET_CHARS] + "\n…"
        lines.append(f"## {loc}  (score {r.get('similarity')})")
        lines.append("```" + (m.get("lang") or ""))
        lines.append(snippet)
        lines.append("```")
        lines.append("")
        results.append({
            "file": rel, "start_line": s, "end_line": e,
            "similarity": r.get("similarity"), "snippet": snippet,
        })
    return {"exit_code": 0, "results": results, "output": "\n".join(lines).strip(),
            "indexed": {"files": files_idx, "chunks": chunks_idx}}


# ── tool entry point ─────────────────────────────────────────────────────────

async def semantic_code_search(content: str, *, workspace: Optional[str] = None,
                               owner: Optional[str] = None) -> Dict[str, Any]:
    """Search the workspace's code by meaning. Returns ranked file:line snippets."""
    query, k, sub_path = _parse_args(content)
    if not query:
        return {"exit_code": 1, "error": "query is required",
                "output": "semantic_code_search: provide a natural-language 'query'."}

    try:
        from src.tool_execution import _resolve_search_root
        root = _resolve_search_root(sub_path or ".", workspace)
    except Exception as e:
        return {"exit_code": 1, "error": str(e), "output": f"semantic_code_search: {e}"}

    if os.path.isfile(root):
        root = os.path.dirname(root)
    if not os.path.isdir(root):
        return {"exit_code": 1, "error": "not a directory",
                "output": f"semantic_code_search: '{sub_path or root}' is not a directory."}

    idx = _get_index()
    if not idx.healthy:
        return {"exit_code": 1, "error": "embeddings unavailable",
                "output": ("semantic_code_search: no embedding backend is available "
                           "(ChromaDB/fastembed not ready). Falling back to grep is fine.")}

    try:
        top, files_idx, chunks_idx, truncated = await asyncio.to_thread(
            idx.search, query, root, k
        )
    except Exception as e:
        logger.exception("semantic_code_search failed")
        return {"exit_code": 1, "error": str(e),
                "output": f"semantic_code_search failed: {e}"}

    return _format(query, root, top, files_idx, chunks_idx, truncated)
