"""
tool_parsing.py

Regex-based parsing of tool invocations from LLM response text.
Supports fenced code blocks, [TOOL_CALL] blocks, and XML-style <invoke> blocks.
"""

import ast
import json
import logging
import re
import shlex
from typing import List, Optional

from src.agent_tools import ToolBlock, TOOL_TAGS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Pattern 1: ```bash ... ``` fenced code blocks
_TOOL_BLOCK_RE = re.compile(
    r"```(" + "|".join(TOOL_TAGS) + r")\s*\n([\s\S]*?)```",
    re.IGNORECASE,
)

# Pattern 2: [TOOL_CALL] ... [/TOOL_CALL] blocks (some models use this format)
# Matches: {tool => "shell", args => {--command "ls -la"}} etc.
_TOOL_CALL_RE = re.compile(
    r"\[TOOL_CALL\]\s*\{([\s\S]*?)\}\s*\[/TOOL_CALL\]",
    re.IGNORECASE,
)

# Pattern 3: XML-style tool calls (minimax, some other models)
# <minimax:tool_call><invoke name="bash"><parameter name="command">...</parameter></invoke></minimax:tool_call>
# Also handles: <tool_call><invoke ...>, <function_call><invoke ...>, plain <invoke ...>
_XML_TOOL_CALL_RE = re.compile(
    r"<(?:[\w]+:)?(?:tool_call|function_call)>\s*([\s\S]*?)</(?:[\w]+:)?(?:tool_call|function_call)>",
    re.IGNORECASE,
)
_XML_INVOKE_RE = re.compile(
    r'<invoke\s+name=["\'](\w+)["\']>\s*([\s\S]*?)</invoke>',
    re.IGNORECASE,
)
_XML_PARAM_RE = re.compile(
    r'<parameter\s+name=["\'](\w+)["\']>([\s\S]*?)</parameter>',
    re.IGNORECASE,
)

# Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)
# {tool => 'tool_name', args => '<param>value</param>'}
_TOOL_CODE_RE = re.compile(
    r"<tool_code>\s*\{([\s\S]*?)\}\s*</tool_code>",
    re.IGNORECASE,
)

_BARE_FILE_TOOL_NAMES = frozenset({"ls", "read_file", "grep", "glob"})
_BARE_FILE_TOOL_SHELL_RE = re.compile(
    r"^(ls|read_file|grep|glob)\s+(.+?)\s*$",
    re.IGNORECASE,
)
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_HAS_GLOB_RE = re.compile(r"[*?\[]")
_SHELL_META_RE = re.compile(r"(?:[|;&]|\|\||&&|>>?|<|\b2>|`|\$\(|\${)")

# Pattern 5: DeepSeek DSML markup leaking into content. When deepseek
# models can't emit structured tool_calls (e.g. we sent no tool schemas
# that round, or the API didn't parse them), they fall back to raw
# markup using fullwidth-pipe delimiters:
#   <｜｜DSML｜｜tool_calls>
#     <｜｜DSML｜｜invoke name="web_search">
#       <｜｜DSML｜｜parameter name="query" string="true">QUERY</｜｜DSML｜｜parameter>
#     </｜｜DSML｜｜invoke>
#   </｜｜DSML｜｜tool_calls>
# We normalize it into the standard <invoke>/<parameter> form so the
# existing XML parser + stripper handle it (parse → execute; strip →
# never show the garbage to the user). The pipe run is tolerant of
# fullwidth (U+FF5C) and ascii '|' in any count.
_DSML_PIPES = r"[｜|]+"
def _normalize_dsml(text: str) -> str:
    if not isinstance(text, str):
        return ""
    if "DSML" not in text:
        return text
    t = text
    t = re.sub(rf"<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*tool_calls\s*>", "<tool_call>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*tool_calls\s*>", "</tool_call>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*invoke\s+name=", "<invoke name=", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*invoke\s*>", "</invoke>", t, flags=re.IGNORECASE)
    # parameter open tag — drop any extra attrs (e.g. string="true").
    t = re.sub(rf'<\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*parameter\s+name=(["\'][^"\']+["\'])[^>]*>',
               r"<parameter name=\1>", t, flags=re.IGNORECASE)
    t = re.sub(rf"<\s*/\s*{_DSML_PIPES}\s*DSML\s*{_DSML_PIPES}\s*parameter\s*>", "</parameter>", t, flags=re.IGNORECASE)
    return t

# Map model tool names to our tool types
_TOOL_NAME_MAP = {
    "shell": "bash",
    "bash": "bash",
    "terminal": "bash",
    "command": "bash",
    "execute": "bash",
    "run": "bash",
    "python": "python",
    "code": "python",
    "search": "web_search",
    "web_search": "web_search",
    "websearch": "web_search",
    "google_search": "web_search",
    "google_search_retrieval": "web_search",
    "google_search_grounding": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "fetch_url": "web_fetch",
    "fetch": "web_fetch",
    "read": "read_file",
    "read_file": "read_file",
    "cat": "read_file",
    "write": "write_file",
    "write_file": "write_file",
    "save": "write_file",
    "document": "update_document",
    "update_document": "update_document",
    "create_document": "create_document",
    "edit": "edit_document",
    "edit_document": "edit_document",
    "search_chats": "search_chats",
    "search_conversations": "search_chats",
    "find_chat": "search_chats",
    "chat_with_model": "chat_with_model",
    "ask_model": "chat_with_model",
    "chat_model": "chat_with_model",
    "create_session": "create_session",
    "new_session": "create_session",
    "list_sessions": "list_sessions",
    "send_to_session": "send_to_session",
    "message_session": "send_to_session",
    "pipeline": "pipeline",
    "chain": "pipeline",
    "manage_session": "manage_session",
    "session_control": "manage_session",
    "manage_memory": "manage_memory",
    "memory": "manage_memory",
    "manage_tasks": "manage_tasks",
    "tasks": "manage_tasks",
    "schedule": "manage_tasks",
    "list_models": "list_models",
    "models": "list_models",
    "available_models": "list_models",
    "ui_control": "ui_control",
    "ui": "ui_control",
    "control": "ui_control",
    "api_call": "api_call",
    "api": "api_call",
    "integration": "api_call",
    "ask_teacher": "ask_teacher",
    "teacher": "ask_teacher",
    "manage_skills": "manage_skills",
    "skills": "manage_skills",
    "skill": "manage_skills",
    "suggest_document": "suggest_document",
    "suggest": "suggest_document",
    "review_document": "suggest_document",
    "manage_endpoints": "manage_endpoints",
    "endpoints": "manage_endpoints",
    "manage_mcp": "manage_mcp",
    "mcp_servers": "manage_mcp",
    "manage_webhooks": "manage_webhooks",
    "webhooks": "manage_webhooks",
    "manage_tokens": "manage_tokens",
    "tokens": "manage_tokens",
    "manage_documents": "manage_documents",
    "documents": "manage_documents",
    "manage_research": "manage_research",
    "list_research": "manage_research",
    "read_research": "manage_research",
    "open_research": "manage_research",
    "delete_research": "manage_research",
    "manage_settings": "manage_settings",
    "settings": "manage_settings",
    "preferences": "manage_settings",
    "manage_notes": "manage_notes",
    "notes": "manage_notes",
    "todo": "manage_notes",
    "todos": "manage_notes",
    "swarm": "run_code_review_swarm",
    "code_review_swarm": "run_code_review_swarm",
    "review_swarm": "run_code_review_swarm",
    "run_code_review_swarm": "run_code_review_swarm",
    "manage_processes": "manage_processes",
    "manage_process": "manage_processes",
    "processes": "manage_processes",
    "process_manager": "manage_processes",
    "background_service": "manage_processes",
    "spawn_agent": "spawn_agent",
    "spawn_agents": "spawn_agent",
    "spawn_subagent": "spawn_agent",
    "delegate": "spawn_agent",
    "delegate_task": "spawn_agent",
    "subagent": "spawn_agent",
    "sub_agent": "spawn_agent",
}

_MISFENCED_WEB_TOOL_NAMES = {
    "web_search": "web_search",
    "websearch": "web_search",
    "google_search": "web_search",
    "google_search_retrieval": "web_search",
    "google_search_grounding": "web_search",
    "web_fetch": "web_fetch",
    "webfetch": "web_fetch",
    "fetch_url": "web_fetch",
}


# ---------------------------------------------------------------------------
# Parsing functions
# ---------------------------------------------------------------------------

def _literal_string(value) -> Optional[str]:
    """Return a string from a small literal AST node, or None."""
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        return None
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _parse_misfenced_web_lookup(content: str) -> Optional[ToolBlock]:
    """Recover simple web_search/web_fetch calls wrapped in python/bash fences.

    Some local fenced-tool models write:

        ```python
        web_search("latest python release")
        ```

    That is an intended tool call, not Python code. Keep this intentionally
    narrow: only a single bare function call to a known web tool alias converts.
    """
    try:
        module = ast.parse(content.strip(), mode="exec")
    except SyntaxError:
        return None
    if len(module.body) != 1 or not isinstance(module.body[0], ast.Expr):
        return None
    call = module.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None

    mapped = _MISFENCED_WEB_TOOL_NAMES.get(call.func.id.lower())
    if mapped not in ("web_search", "web_fetch"):
        return None
    if len(call.args) > 1:
        return None

    args = {}
    if call.args:
        key = "url" if mapped == "web_fetch" else "query"
        value = _literal_string(call.args[0])
        if not value:
            return None
        args[key] = value

    allowed = {"query", "queries", "url", "time_filter", "freshness", "max_pages"}
    for keyword in call.keywords:
        if keyword.arg not in allowed:
            return None
        key = "query" if keyword.arg == "queries" else keyword.arg
        value = _literal_string(keyword.value)
        if value is not None:
            args[key] = value
            continue
        try:
            parsed = ast.literal_eval(keyword.value)
        except (ValueError, SyntaxError, TypeError):
            return None
        if key == "max_pages" and isinstance(parsed, int):
            args[key] = parsed
            continue
        return None

    if mapped == "web_search":
        query = args.get("query")
        if not query:
            return None
        payload = {"query": query}
        for key in ("time_filter", "freshness", "max_pages"):
            if key in args:
                payload[key] = args[key]
        if len(payload) == 1:
            return ToolBlock("web_search", query)
        return ToolBlock("web_search", json.dumps(payload))

    url = args.get("url")
    if not url:
        return None
    return ToolBlock("web_fetch", url)


def _literal_jsonable(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _looks_like_abs_or_relative_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip().strip('"\'')
    return bool(
        _WINDOWS_ABS_PATH_RE.match(text)
        or text.startswith(("./", ".\\", "../", "..\\", "/", "\\"))
    )


def _split_shellish_file_tool_args(arg: str) -> Optional[List[str]]:
    """Tokenize simple read-only file-tool text without accepting shell syntax."""

    if not isinstance(arg, str) or not arg.strip():
        return None
    if "\n" in arg or _SHELL_META_RE.search(arg):
        return None
    try:
        return shlex.split(arg, posix=False)
    except ValueError:
        return None


def _strip_token_quotes(token: str) -> str:
    text = (token or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _split_glob_path(raw_path: str) -> tuple[str, str]:
    """Return (base path, glob pattern) for a wildcard path."""

    path = _strip_token_quotes(raw_path).replace("\\", "/")
    wildcard_positions = [pos for ch in ("*", "?", "[") if (pos := path.find(ch)) >= 0]
    if not wildcard_positions:
        return path, "*"
    first_wildcard = min(wildcard_positions)
    slash = path.rfind("/", 0, first_wildcard)
    if slash < 0:
        return ".", path
    base = path[:slash] or "/"
    pattern = path[slash + 1 :] or "*"
    return base, pattern


def _parse_shellish_ls_or_glob(tool: str, arg: str) -> Optional[ToolBlock]:
    tokens = _split_shellish_file_tool_args(arg)
    if not tokens or len(tokens) != 1:
        return None
    raw_path = _strip_token_quotes(tokens[0])
    if not raw_path:
        return None
    if _PATH_HAS_GLOB_RE.search(raw_path):
        base, pattern = _split_glob_path(raw_path)
        return ToolBlock("glob", json.dumps({"pattern": pattern, "path": base}))
    return ToolBlock(tool, raw_path)


def _parse_shellish_grep(arg: str) -> Optional[ToolBlock]:
    tokens = _split_shellish_file_tool_args(arg)
    if not tokens:
        return None

    pattern = None
    path = ""
    glob_pat = ""
    ignore_case = False
    i = 0
    while i < len(tokens):
        token = _strip_token_quotes(tokens[i])
        if not token:
            i += 1
            continue
        if token in ("-r", "-R", "--recursive", "-n", "--line-number", "--no-heading"):
            i += 1
            continue
        if token in ("-i", "--ignore-case"):
            ignore_case = True
            i += 1
            continue
        if token in ("--glob", "-g"):
            if i + 1 >= len(tokens):
                return None
            glob_pat = _strip_token_quotes(tokens[i + 1])
            i += 2
            continue
        if token.startswith("-"):
            return None
        if pattern is None:
            pattern = token
        elif not path and _looks_like_abs_or_relative_path(token):
            path = token
        else:
            return None
        i += 1

    if not pattern:
        return None
    payload = {"pattern": pattern}
    if path:
        if _PATH_HAS_GLOB_RE.search(path):
            base, split_glob = _split_glob_path(path)
            payload["path"] = base
            payload["glob"] = glob_pat or split_glob
        else:
            payload["path"] = path
    if glob_pat and "glob" not in payload:
        payload["glob"] = glob_pat
    if ignore_case:
        payload["ignore_case"] = True
    return ToolBlock("grep", json.dumps(payload))


def _parse_bare_file_tool_call_line(line: str) -> Optional[ToolBlock]:
    """Parse local-model bare file-tool calls such as ``ls("C:/x")``.

    Some fenced-tool local models emit Python-ish or shell-ish text for simple
    file tools even when instructed to use ```ls fences. Keep this parser
    intentionally narrow: only dedicated file tools, only one line, no bash or
    python aliases.
    """

    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text:
        return None
    if text.startswith("```") and text.endswith("```") and len(text) > 6:
        text = text[3:-3].strip()
    elif text.startswith("`") and text.endswith("`") and len(text) > 2:
        text = text[1:-1].strip()
    if not text or "\n" in text:
        return None

    try:
        expr = ast.parse(text, mode="eval").body
    except SyntaxError:
        expr = None
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        tool = expr.func.id.lower()
        if tool not in _BARE_FILE_TOOL_NAMES:
            return None
        if tool in ("ls", "read_file"):
            if len(expr.args) == 1 and not expr.keywords:
                value = _literal_string(expr.args[0])
                return ToolBlock(tool, value.strip()) if value else None
            payload = {}
            for keyword in expr.keywords:
                if not keyword.arg:
                    return None
                value = _literal_jsonable(keyword.value)
                if value is None:
                    return None
                payload[keyword.arg] = value
            if payload:
                return ToolBlock(tool, json.dumps(payload))
            return None
        payload = {}
        if len(expr.args) == 1:
            value = _literal_jsonable(expr.args[0])
            if isinstance(value, dict):
                payload.update(value)
            elif isinstance(value, str):
                payload["pattern"] = value
            else:
                return None
        elif expr.args:
            return None
        for keyword in expr.keywords:
            if not keyword.arg:
                return None
            value = _literal_jsonable(keyword.value)
            if value is None:
                return None
            payload[keyword.arg] = value
        return ToolBlock(tool, json.dumps(payload)) if payload else None

    match = _BARE_FILE_TOOL_SHELL_RE.match(text)
    if not match:
        return None
    tool = match.group(1).lower()
    arg = match.group(2).strip()
    if not arg:
        return None
    if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
        arg = arg[1:-1].strip()
    if not arg:
        return None
    if tool in ("ls", "glob"):
        shellish = _parse_shellish_ls_or_glob(tool, arg)
        if shellish:
            return shellish
        return None
    if tool == "grep":
        shellish = _parse_shellish_grep(arg)
        if shellish:
            return shellish
        if arg.startswith("{"):
            return ToolBlock(tool, arg)
        return None
    return ToolBlock(tool, arg)


def _parse_bare_file_tool_calls(text: str) -> List[ToolBlock]:
    blocks: List[ToolBlock] = []
    seen = set()
    for raw_line in text.splitlines():
        block = _parse_bare_file_tool_call_line(raw_line)
        if not block:
            continue
        key = (block.tool_type, block.content)
        if key in seen:
            continue
        seen.add(key)
        blocks.append(block)
    return blocks

def _parse_tool_call_block(raw: str) -> Optional[ToolBlock]:
    """Parse a [TOOL_CALL] block into a ToolBlock.

    Handles formats like:
      {tool => "shell", args => {--command "ls -la"}}
      {tool: "shell", command: "ls -la"}
    """
    # Try to extract tool name
    tool_match = re.search(r'tool\s*(?:=>|:|=)\s*["\']?(\w+)["\']?', raw, re.IGNORECASE)
    if not tool_match:
        return None

    tool_name = tool_match.group(1).lower()
    # Fall back to the raw name when it's a real tool but not in the alias
    # map, so known tools (e.g. manage_calendar) aren't silently dropped.
    mapped = _TOOL_NAME_MAP.get(tool_name) or (tool_name if tool_name in TOOL_TAGS else None)
    if not mapped:
        return None

    # Extract the command/content — try several patterns
    content = None

    # Pattern: --command "value" or --command 'value'
    cmd_match = re.search(r'--command\s+["\'](.+?)["\']', raw, re.DOTALL)
    if cmd_match:
        content = cmd_match.group(1)

    # Pattern: command => "value" or command: "value"
    if not content:
        cmd_match = re.search(r'command\s*(?:=>|:|=)\s*["\'](.+?)["\']', raw, re.DOTALL)
        if cmd_match:
            content = cmd_match.group(1)

    # Pattern: args => {content} — extract everything inside the nested braces
    if not content:
        args_match = re.search(r'args\s*(?:=>|:|=)\s*\{([\s\S]*)\}', raw, re.DOTALL)
        if args_match:
            inner = args_match.group(1).strip()
            # Strip quotes and key prefixes
            inner = re.sub(r'^--?\w+\s+', '', inner)
            inner = inner.strip('\'"')
            if inner:
                content = inner

    # Pattern: query/path/code => "value"
    if not content:
        for key in ("query", "path", "code", "content", "text", "file"):
            m = re.search(rf'{key}\s*(?:=>|:|=)\s*["\'](.+?)["\']', raw, re.DOTALL)
            if m:
                content = m.group(1)
                break

    # Last resort: take everything after the tool declaration
    if not content:
        rest = raw[tool_match.end():].strip()
        rest = re.sub(r'^[,;]\s*', '', rest)
        rest = rest.strip('{} \t\n\'"')
        if rest:
            content = rest

    if content:
        return ToolBlock(mapped, content.strip())
    return None


def _parse_xml_invoke(inv_match) -> Optional[ToolBlock]:
    """Parse an <invoke name="tool"><parameter ...>...</parameter></invoke> match.

    Delegates content-shaping to function_call_to_tool_block — the SAME
    converter used for native function calls — so the full tool set (every
    name in TOOL_TAGS, plus email + MCP tools) and the correct per-tool
    content format are handled in ONE place. The previous version duplicated
    a partial, hand-maintained tool-name map plus a `key: value` serializer:
    any tool missing from that map (e.g. `manage_calendar`) was silently
    dropped, and JSON-arg tools got an unparseable `k: v` blob. Both bugs
    made deepseek's DSML `create_event` calls vanish with no execution.
    """
    # Lowercase the tool name: models often emit capitalized invoke names
    # (e.g. <invoke name="Bash">) and function_call_to_tool_block matches
    # case-sensitively against the lowercase _TOOL_NAME_MAP / TOOL_TAGS, so a
    # raw capitalized name would be silently dropped.
    tool_name = inv_match.group(1).lower()
    body = inv_match.group(2)
    params = {}
    for pm in _XML_PARAM_RE.finditer(body):
        params[pm.group(1)] = pm.group(2).strip()
    # Local import to avoid a circular import at module load.
    from src.tool_schemas import function_call_to_tool_block
    return function_call_to_tool_block(tool_name, json.dumps(params))


def _parse_tool_code_block(raw: str) -> Optional[ToolBlock]:
    """Parse a <tool_code>{tool => 'name', args => '...'}</tool_code> block (MiniMax style)."""
    # Extract tool name
    tool_match = re.search(r"tool\s*=>\s*['\"](\S+?)['\"]", raw)
    if not tool_match:
        return None
    tool_name = tool_match.group(1).lower().replace('-', '_')
    # Strip MCP prefixes like "mcp__server__" or "cli-mcp-server-"
    for prefix in ("mcp__", "cli_mcp_server_", "desktop_commander_", "mcp_code_executor_"):
        if tool_name.startswith(prefix):
            tool_name = tool_name[len(prefix):]
            break

    mapped = _TOOL_NAME_MAP.get(tool_name)

    # Extract args content
    args_match = re.search(r"args\s*=>\s*['\"]?\s*([\s\S]*?)\s*['\"]?\s*$", raw, re.DOTALL)
    args_body = args_match.group(1).strip().strip("'\"") if args_match else ""

    # Parse XML params inside args (e.g. <command>ls</command>)
    xml_params = {}
    for pm in re.finditer(r"<(\w+)>([\s\S]*?)</\1>", args_body):
        xml_params[pm.group(1)] = pm.group(2).strip()

    # When the model gave structured params, hand them to the canonical
    # converter (same as native calls + <invoke>) so the full tool set and
    # correct per-tool content format apply — not a partial map + k:v blob.
    if xml_params:
        from src.tool_schemas import function_call_to_tool_block
        block = function_call_to_tool_block(mapped or tool_name, json.dumps(xml_params))
        if block:
            return block

    # No structured params: args_body is a raw single value (e.g. a bash
    # command). Keep the freeform special-casing for the simple tools.
    if mapped:
        if mapped == "bash":
            content = xml_params.get("command", args_body)
        elif mapped == "python":
            content = xml_params.get("code", args_body)
        elif mapped == "web_search":
            content = xml_params.get("query", args_body)
        elif mapped == "web_fetch":
            content = xml_params.get("url", args_body)
        elif mapped in ("read_file", "write_file"):
            content = xml_params.get("path", xml_params.get("file_path", args_body))
        else:
            content = "\n".join(f"{k}: {v}" for k, v in xml_params.items()) if xml_params else args_body
        if content:
            return ToolBlock(mapped, content.strip())
    elif tool_name and args_body:
        # Unknown tool — try as MCP tool call
        content = "\n".join(f"{k}: {v}" for k, v in xml_params.items()) if xml_params else args_body
        return ToolBlock(tool_name, content.strip())
    return None


def parse_tool_blocks(text: str) -> List[ToolBlock]:
    """Extract executable tool blocks from LLM response text.

    Supports multiple formats:
    1. ```bash ... ``` fenced code blocks (standard)
    2. [TOOL_CALL] ... [/TOOL_CALL] blocks (some models)
    3. XML-style <tool_call>/<invoke> blocks
    4. <tool_code> blocks (MiniMax-M2.5 style)
    5. DeepSeek DSML markup (normalized to <invoke> first)
    """
    blocks = []

    # Normalize DeepSeek DSML markup into standard <invoke> form so the
    # XML patterns below catch it.
    text = _normalize_dsml(text)

    # Pattern 1: fenced code blocks
    for m in _TOOL_BLOCK_RE.finditer(text):
        tag = m.group(1).lower()
        content = m.group(2).strip()
        if not content:
            continue
        # If a code block's content is an <invoke> XML call (some models wrap
        # tool calls in ```python or ```xml fences), parse the invoke instead.
        if '<invoke' in content:
            for inv in _XML_INVOKE_RE.finditer(content):
                block = _parse_xml_invoke(inv)
                if block:
                    blocks.append(block)
            # This fenced block is <invoke> markup, not literal code. Whether or
            # not any call converted, never fall through to append the raw XML as
            # a python/bash block — e.g. a hyphenated/namespaced tool name that
            # _XML_INVOKE_RE's \w+ can't match would otherwise be executed as code.
            continue
        if tag in ("python", "bash"):
            block = _parse_misfenced_web_lookup(content)
            if block:
                blocks.append(block)
                continue
        blocks.append(ToolBlock(tag, content))

    # Local-model compatibility: bare dedicated file calls such as
    # `ls("C:/Projects/x")` or `ls C:/Projects/x` (only if no fenced blocks
    # found). This catches models that learned function-ish syntax but not
    # Odysseus' fenced tool convention.
    if not blocks:
        blocks = _parse_bare_file_tool_calls(text)

    # Pattern 2: [TOOL_CALL] blocks (only if no fenced blocks found)
    if not blocks:
        for m in _TOOL_CALL_RE.finditer(text):
            block = _parse_tool_call_block(m.group(1))
            if block:
                blocks.append(block)

    # Pattern 3: XML-style <tool_call>/<invoke> blocks
    if not blocks:
        # Try wrapped: <tool_call><invoke ...>...</invoke></tool_call>
        for m in _XML_TOOL_CALL_RE.finditer(text):
            for inv in _XML_INVOKE_RE.finditer(m.group(1)):
                block = _parse_xml_invoke(inv)
                if block:
                    blocks.append(block)
        # Try bare <invoke> without wrapper
        if not blocks:
            for inv in _XML_INVOKE_RE.finditer(text):
                block = _parse_xml_invoke(inv)
                if block:
                    blocks.append(block)

    # Pattern 4: <tool_code> blocks (MiniMax-M2.5 style)
    if not blocks:
        for m in _TOOL_CODE_RE.finditer(text):
            block = _parse_tool_code_block(m.group(1))
            if block:
                blocks.append(block)

    return blocks


def strip_tool_blocks(text: str) -> str:
    """Remove executable tool blocks from text for clean display."""
    # Normalize DSML first so its markup gets stripped by the <invoke>
    # / <tool_call> removers below instead of leaking to the user.
    text = _normalize_dsml(text)
    cleaned = _TOOL_BLOCK_RE.sub('', text)
    cleaned = _TOOL_CALL_RE.sub('', cleaned)
    cleaned = _XML_TOOL_CALL_RE.sub('', cleaned)
    cleaned = _TOOL_CODE_RE.sub('', cleaned)
    # Strip bare <invoke> blocks not wrapped in <tool_call>
    cleaned = re.sub(r'<invoke\s+name=["\'].*?</invoke>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()
