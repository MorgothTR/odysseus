import os
from unittest.mock import patch

import pytest

from src.tool_path_roots import (
    ToolPathRootError,
    normalize_tool_path_root,
    normalize_tool_path_roots,
    safe_tool_path_roots,
)


def test_normalizes_existing_absolute_directory(tmp_path):
    folder = tmp_path / "Projects" / "Example"
    folder.mkdir(parents=True)

    normalized = normalize_tool_path_root(str(folder))

    assert "\\" not in normalized
    assert normalized.endswith("/Projects/Example")


def test_rejects_relative_missing_and_file_paths(tmp_path):
    with pytest.raises(ToolPathRootError, match="absolute"):
        normalize_tool_path_root("relative/path")

    with pytest.raises(ToolPathRootError, match="exist"):
        normalize_tool_path_root(str(tmp_path / "missing"))

    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory")
    with pytest.raises(ToolPathRootError, match="directory"):
        normalize_tool_path_root(str(file_path))


def test_deduplicates_case_insensitively_on_windows(tmp_path):
    folder = tmp_path / "CaseTest"
    folder.mkdir()
    first = str(folder)
    second = first.upper() if os.name == "nt" else first

    roots = normalize_tool_path_roots([first, second])

    assert len(roots) == 1


@pytest.mark.parametrize(
    "parts",
    [
        (".ssh",),
        (".gnupg",),
        (".aws",),
        (".azure",),
        (".kube",),
        (".docker",),
        (".config", "gcloud"),
        (".config", "gh"),
    ],
)
def test_rejects_sensitive_credential_directories(tmp_path, parts):
    folder = tmp_path.joinpath(*parts)
    folder.mkdir(parents=True)

    with pytest.raises(ToolPathRootError, match="sensitive"):
        normalize_tool_path_root(str(folder))


def test_safe_tool_path_roots_ignores_invalid_entries(tmp_path):
    valid = tmp_path / "allowed"
    valid.mkdir()
    sensitive = tmp_path / ".ssh"
    sensitive.mkdir()

    roots = safe_tool_path_roots([str(valid), str(sensitive), "relative/path"])

    assert roots == [normalize_tool_path_root(str(valid))]


def test_settings_shape_must_be_list(tmp_path):
    with pytest.raises(ToolPathRootError, match="list"):
        normalize_tool_path_roots(str(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="Windows path policy")
def test_rejects_windows_broad_roots():
    with pytest.raises(ToolPathRootError, match="Drive roots"):
        normalize_tool_path_root("C:/")

    if os.path.isdir("C:/Windows"):
        with pytest.raises(ToolPathRootError, match="protected"):
            normalize_tool_path_root("C:/Windows")

    if os.path.isdir("C:/Program Files"):
        with pytest.raises(ToolPathRootError, match="protected"):
            normalize_tool_path_root("C:/Program Files")

    profile = os.path.expanduser("~")
    if os.path.isdir(profile):
        with pytest.raises(ToolPathRootError, match="profile root"):
            normalize_tool_path_root(profile)


def test_invalid_hand_edited_extra_roots_do_not_expand_enforcement(tmp_path):
    from src.tool_execution import _resolve_tool_path, _tool_path_roots

    valid = tmp_path / "valid"
    valid.mkdir()
    target = valid / "note.txt"
    target.write_text("ok")
    invalid = tmp_path / ".ssh"
    invalid.mkdir()

    with patch("src.settings.get_setting", return_value=[str(valid), str(invalid)]):
        roots = _tool_path_roots()
        assert os.path.realpath(str(valid)) in roots
        assert os.path.realpath(str(invalid)) not in roots
        assert _resolve_tool_path(str(target)) == os.path.realpath(str(target))
