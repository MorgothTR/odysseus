"""Per-project context file loading (phase 19).

When a chat has a workspace folder attached, surface that project's own guidance
file to the agent so it knows the project's conventions, build/test/run commands,
and architecture without the user re-explaining every turn — the Odysseus
equivalent of a CLAUDE.md / AGENTS.md.

The content is USER-EDITABLE project data, so the caller injects it as UNTRUSTED
context (a user-role message, metadata.trusted=False), never the trusted system
role — the same treatment the skills index gets. This module only finds, reads,
and size-caps the file; it does not assemble prompt messages.
"""

from __future__ import annotations

import os
from typing import Optional

# Filenames to look for at the workspace root, in priority order. A dedicated
# .odysseus.md wins (it's written FOR the agent and can be kept short); then the
# common cross-tool conventions files.
CONTEXT_FILENAMES = (".odysseus.md", "AGENTS.md", "CLAUDE.md", ".cursorrules")

# Cap injected size. A 27 KB AGENTS.md on every turn is too expensive and crowds
# the real request, so truncate with a pointer — the agent can read the rest
# with read_file when it actually needs the detail.
MAX_CONTEXT_CHARS = 6000


def find_context_file(workspace: Optional[str]) -> Optional[str]:
    """Absolute path of the first matching context file at the workspace root,
    or None. Only the root is checked — this is project-level guidance, not a
    per-directory walk."""
    if not workspace:
        return None
    try:
        root = os.path.realpath(workspace)
        if not os.path.isdir(root):
            return None
    except OSError:
        return None
    for name in CONTEXT_FILENAMES:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    return None


def load_project_context(workspace: Optional[str]) -> Optional[str]:
    """Return ready-to-inject project guidance for a workspace, or None.

    Reads the first matching context file (capped to ``MAX_CONTEXT_CHARS``) and
    prefixes a short header that tells the agent to follow the conventions and
    run the project's tests after editing — the lightweight verify-after-edit
    loop. Returns None when there is no workspace, no file, or it is empty/
    unreadable, which callers treat as "nothing to inject"."""
    path = find_context_file(workspace)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_CONTEXT_CHARS + 1).strip()
    except OSError:
        return None
    if not text:
        return None

    name = os.path.basename(path)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS].rstrip() + (
            f"\n\n... [{name} truncated — read the full file with read_file if you need more]"
        )
    header = (
        f"Project guidance from `{name}` in the attached folder. Follow its "
        f"conventions and use the build/test/run commands it names. After you "
        f"edit code in this project, run its tests to verify your change before "
        f"reporting done.\n\n"
    )
    return header + text
