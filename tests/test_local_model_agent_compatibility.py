from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_LOOP = ROOT / "src" / "agent_loop.py"
ADMIN_JS = ROOT / "static" / "js" / "admin.js"
WINDOWS_DOCS = ROOT / "docs" / "windows-desktop.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_endpoint_tool_mode_toggle_is_visible_and_patches_existing_setting():
    admin = _text(ADMIN_JS)

    assert "function _endpointToolModeSelect" in admin
    assert 'data-adm-ep-tools="' in admin
    assert "Native tool calling mode" in admin
    assert ">Auto<" in admin
    assert ">Native<" in admin
    assert ">Fenced<" in admin
    assert "supports_tools" in admin
    assert "value === '' ? null : value === 'true'" in admin
    assert "method: 'PATCH'" in admin
    assert "Content-Type': 'application/json'" in admin
    assert "model_type === 'image'" in admin


def test_endpoint_tool_mode_select_does_not_expand_endpoint_rows():
    admin = _text(ADMIN_JS)

    assert "input, select, label" in admin


def test_fenced_prompt_documents_dedicated_file_tools_for_local_models():
    agent = _text(AGENT_LOOP)

    assert '"ls": """' in agent
    assert "```ls" in agent
    assert "List a directory using Odysseus file access rules" in agent
    assert "Prefer this over `bash ls`, `dir`, or PowerShell" in agent
    assert '"grep": """' in agent
    assert "```grep" in agent
    assert '"glob": """' in agent
    assert "```glob" in agent


def test_agent_rules_prefer_dedicated_file_tools_over_shell_refusals():
    agent = _text(AGENT_LOOP)

    assert "For local file/folder access, prefer dedicated tools" in agent
    assert "do not claim you lack filesystem access" in agent
    assert "`powershell` is NOT an executable tool tag" in agent
    assert "For local file/folder access, call `ls`, `read_file`, `grep`, or `glob` before considering `bash`" in agent


def test_active_workspace_note_no_longer_only_points_at_bash():
    agent = _text(AGENT_LOOP)

    assert "For directory listing/searching, prefer the dedicated `ls` and `glob`" in agent
    assert "never use `powershell` because it is not an" in agent
    assert "executable tool tag" in agent


def test_windows_docs_explain_lm_studio_fenced_tool_mode():
    docs = _text(WINDOWS_DOCS)
    squashed = _squash(docs)

    assert "LM Studio/local Agent mode will not use tools" in docs
    assert "set **Tools** to" in docs
    assert "**Fenced**" in docs
    assert "`ls`, `read_file`, `grep`, or `glob`" in squashed
