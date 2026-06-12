"""Validation helpers for user-configured local file tool roots."""

from __future__ import annotations

import os
import ntpath
import re
from pathlib import PureWindowsPath
from typing import Iterable


class ToolPathRootError(ValueError):
    """Raised when a configured tool path root is unsafe or invalid."""


_SENSITIVE_COMPONENTS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
}

_SENSITIVE_COMPONENT_SEQUENCES = (
    (".config", "gcloud"),
    (".config", "gh"),
)


def _stored_path(path: str) -> str:
    value = path.replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    if re.fullmatch(r"[A-Za-z]:", value):
        value += "/"
    if re.fullmatch(r"[A-Za-z]:/", value):
        return value
    return value.rstrip("/")


def _case_key(path: str) -> str:
    return path.lower() if os.name == "nt" else path


def _windows_parts(path: str) -> tuple[str, ...]:
    return tuple(str(part).lower() for part in PureWindowsPath(path).parts)


def _is_windows_drive_root(path: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]:/", path))


def _is_windows_user_profile_root(path: str) -> bool:
    parts = _windows_parts(path)
    return (
        len(parts) == 3
        and re.fullmatch(r"[a-z]:\\", parts[0]) is not None
        and parts[1] == "users"
        and bool(parts[2])
    )


def _is_at_or_under_windows_path(path: str, blocked: str) -> bool:
    path_norm = ntpath.normcase(ntpath.normpath(path))
    blocked_norm = ntpath.normcase(ntpath.normpath(blocked))
    try:
        return ntpath.commonpath([path_norm, blocked_norm]) == blocked_norm
    except ValueError:
        return False


def _contains_sensitive_component(path: str) -> bool:
    parts = [part.lower() for part in PureWindowsPath(path).parts]
    if any(part in _SENSITIVE_COMPONENTS for part in parts):
        return True
    for seq in _SENSITIVE_COMPONENT_SEQUENCES:
        for idx in range(0, len(parts) - len(seq) + 1):
            if tuple(parts[idx : idx + len(seq)]) == seq:
                return True
    return False


def normalize_tool_path_root(raw_path: object) -> str:
    """Validate and normalize one extra tool root for storage.

    The returned path uses forward slashes so Windows paths are stable in
    ``data/settings.json`` and easy to read.
    """

    value = str(raw_path or "").strip()
    if not value:
        raise ToolPathRootError("Folder path is required")

    expanded = os.path.expandvars(os.path.expanduser(value))
    if not os.path.isabs(expanded):
        raise ToolPathRootError("Folder path must be absolute")
    if not os.path.isdir(expanded):
        raise ToolPathRootError("Folder must exist and be a directory")

    real = os.path.realpath(expanded)
    if str(real).startswith("\\\\"):
        raise ToolPathRootError("Network share paths are not supported for allowed local folders")
    normalized = _stored_path(os.path.normpath(real))

    if _is_windows_drive_root(normalized):
        raise ToolPathRootError("Drive roots such as C:/ are too broad")
    if _is_windows_user_profile_root(normalized):
        raise ToolPathRootError("Choose a folder inside your user profile, not the profile root")

    blocked_windows_roots = [
        os.path.realpath(os.environ.get("SystemRoot") or r"C:\Windows"),
        os.path.realpath(os.environ.get("ProgramFiles") or r"C:\Program Files"),
        os.path.realpath(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"),
    ]
    for blocked in blocked_windows_roots:
        blocked_normalized = _stored_path(os.path.normpath(blocked))
        if blocked_normalized and _is_at_or_under_windows_path(normalized, blocked_normalized):
            raise ToolPathRootError(f"{blocked_normalized} is a protected system location")

    if _contains_sensitive_component(normalized):
        raise ToolPathRootError("Folder path contains a sensitive credential or key directory")

    return normalized


def normalize_tool_path_roots(raw_paths: Iterable[object]) -> list[str]:
    """Validate, normalize, and deduplicate a list of extra roots."""

    if not isinstance(raw_paths, list):
        raise ToolPathRootError("Allowed local folders must be a list")

    roots: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        normalized = normalize_tool_path_root(raw)
        key = _case_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        roots.append(normalized)
    return roots


def safe_tool_path_roots(raw_paths: object) -> list[str]:
    """Best-effort sanitizer for enforcement-time loading.

    Invalid hand-edited settings are ignored so they do not expand file-tool
    access beyond the validated policy.
    """

    if not isinstance(raw_paths, list):
        return []
    roots: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        try:
            normalized = normalize_tool_path_root(raw)
        except ToolPathRootError:
            continue
        key = _case_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        roots.append(normalized)
    return roots
