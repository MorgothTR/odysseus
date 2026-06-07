"""Per-turn tool policy composition for agent execution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Set, Tuple


GUIDE_ONLY_DIRECTIVE = (
    "## GUIDE-ONLY MODE - TOOL POLICY\n"
    "The latest user turn explicitly forbids tool use. Do not call tools, do not "
    "run shell commands, and do not inspect local files or the environment. "
    "Respond in normal text by guiding the user or asking them to paste the "
    "output they will produce locally."
)


_COMMON_TOOL_NAMES = {
    "api_call",
    "app_api",
    "archive_email",
    "ask_teacher",
    "ask_user",
    "bash",
    "bulk_email",
    "builtin_browser",
    "cancel_download",
    "chat_with_model",
    "create_document",
    "create_session",
    "delete_email",
    "download_model",
    "edit_document",
    "edit_file",
    "edit_image",
    "generate_image",
    "glob",
    "grep",
    "list_cached_models",
    "list_cookbook_servers",
    "list_downloads",
    "list_emails",
    "list_models",
    "list_serve_presets",
    "list_served_models",
    "list_sessions",
    "ls",
    "manage_calendar",
    "manage_contact",
    "manage_documents",
    "manage_endpoints",
    "manage_mcp",
    "manage_memory",
    "manage_notes",
    "manage_research",
    "manage_session",
    "manage_settings",
    "manage_skills",
    "manage_tasks",
    "manage_tokens",
    "manage_webhooks",
    "mark_email_read",
    "pipeline",
    "python",
    "read_email",
    "read_file",
    "reply_to_email",
    "resolve_contact",
    "search_chats",
    "search_hf_models",
    "send_email",
    "send_to_session",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "suggest_document",
    "trigger_research",
    "ui_control",
    "update_document",
    "update_plan",
    "vault_get",
    "vault_search",
    "vault_unlock",
    "web_fetch",
    "web_search",
    "write_file",
}


_GUIDE_ONLY_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\bguide[-\s]?only mode\b", "guide-only mode requested"),
        (r"\bno[-\s]?tools? mode\b", "no-tools mode requested"),
        (r"\bdo not use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bdon'?t use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to:?.{0,120}\buse (?:any )?tools?\b", "user forbade tool use"),
        (r"\bask (?:me )?(?:for confirmation )?before using tools?\b", "user requested confirmation before tools"),
    )
)


_SMART_FILE_ROUTING_TOOLS = frozenset({
    # Power tools that local models tend to overuse for simple file browsing.
    "bash",
    "python",
    # Prevent unsolicited "I saved the listing to a file/document" behavior on
    # read-only browse turns. Explicit write/edit/save requests bypass this
    # router entirely.
    "write_file",
    "edit_file",
    "create_document",
    "edit_document",
    "update_document",
    "suggest_document",
})

_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s`\"']*")
_RELATIVE_PATH_RE = re.compile(r"(?<!\w)(?:\.{1,2}[\\/])[^\s`\"']+")
_DEDICATED_FILE_TOOL_RE = re.compile(
    r"\b(?:dedicated\s+)?(?:odysseus\s+)?(?:ls|read_file|grep|glob)\s+tool\b",
    re.IGNORECASE,
)
_FILE_ACCESS_INTENT_RE = re.compile(
    r"\b(?:list|show|display|read|open|view|cat|search|find|grep|glob|"
    r"contents?|director(?:y|ies)|folders?|files?)\b",
    re.IGNORECASE,
)
_FILE_MUTATION_INTENT_RE = re.compile(
    r"\b(?:write|save|create|edit|modify|change|delete|remove|rename|"
    r"move|copy|append|export)\b|"
    r"\bmake\b.{0,40}\b(?:file|document|folder|directory)\b",
    re.IGNORECASE,
)
_NEGATED_POWER_TOOL_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|without|no)\s+"
    r"(?:use\s+)?(?:bash|shell|powershell|pwsh|cmd|command\s+prompt|terminal|python)\b",
    re.IGNORECASE,
)
_EXPLICIT_POWER_TOOL_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:use|run|execute|call|invoke|via|with|through|as)\s+"
        r"(?:the\s+)?(?:bash|shell|powershell|pwsh|cmd|command\s+prompt|terminal|python)\b",
        r"\b(?:bash|shell|powershell|pwsh|cmd|command\s+prompt|terminal|python)\s+"
        r"(?:command|script|tool)\b",
        r"\b(?:run|execute)\s+(?:this|the\s+)?(?:command|script)\b",
        r"^\s*(?:dir|ls|cat|type|gci|get-childitem|python|py|powershell|pwsh|"
        r"cmd|git|npm|pip|cargo|pytest)\b",
    )
)


@dataclass(frozen=True)
class ToolPolicy:
    """Effective tool behavior for one agent turn."""

    disabled_tools: frozenset[str] = frozenset()
    hidden_tools: frozenset[str] = frozenset()
    reasons: Mapping[str, str] = field(default_factory=dict)
    mode: str = "normal"
    block_all_tool_calls: bool = False
    disable_mcp: bool = False

    def all_disabled_names(self) -> Set[str]:
        return set(self.disabled_tools) | set(self.hidden_tools)

    def blocks(self, tool_name: Optional[str]) -> bool:
        if not tool_name:
            return False
        return self.block_all_tool_calls or tool_name in self.disabled_tools or tool_name in self.hidden_tools

    def reason_for(self, tool_name: Optional[str]) -> str:
        if tool_name and tool_name in self.reasons:
            return self.reasons[tool_name]
        if self.block_all_tool_calls and self.mode == "guide_only":
            return "Tool use is disabled for this guide-only turn."
        return "Tool use is disabled for this turn."


def detect_guide_only_turn(message: object) -> Optional[str]:
    """Return a reason when the latest user turn strongly requests no tools."""

    if not isinstance(message, str) or not message.strip():
        return None
    text = re.sub(r"\s+", " ", message.strip())
    for pattern, reason in _GUIDE_ONLY_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def detect_smart_file_routing_turn(message: object) -> Optional[str]:
    """Return a reason when a turn should prefer read-only file tools.

    This is intentionally narrow. It catches plain "list/read/search this
    local path" requests and lets explicit command/script/Python or write/edit
    requests pass through unchanged so power-user tools remain available.
    """

    if not isinstance(message, str) or not message.strip():
        return None
    text = re.sub(r"\s+", " ", message.strip())
    if not text:
        return None

    # Explicit write/edit/save requests are not read-only browsing.
    if _FILE_MUTATION_INTENT_RE.search(text):
        return None

    # "Do not use bash" should not count as an explicit request for bash.
    power_check = _NEGATED_POWER_TOOL_RE.sub(" ", text)
    if any(pattern.search(power_check) for pattern in _EXPLICIT_POWER_TOOL_PATTERNS):
        return None

    has_path = bool(_WINDOWS_PATH_RE.search(text) or _RELATIVE_PATH_RE.search(text))
    asks_for_file_tool = bool(_DEDICATED_FILE_TOOL_RE.search(text))
    has_file_intent = bool(_FILE_ACCESS_INTENT_RE.search(text))
    if has_file_intent and (has_path or asks_for_file_tool):
        return "Plain local file access should use dedicated file tools for this turn."
    return None


def known_tool_names() -> Set[str]:
    """Best-effort set of native tool names for prompt hiding and denylisting."""

    names = set(_COMMON_TOOL_NAMES)
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

        for schema in FUNCTION_TOOL_SCHEMAS:
            name = (schema.get("function") or {}).get("name") or schema.get("name")
            if name:
                names.add(name)
    except Exception:
        pass
    try:
        from src.agent_loop import TOOL_SECTIONS

        names.update(TOOL_SECTIONS.keys())
    except Exception:
        pass
    try:
        from src.tool_security import PLAN_MODE_READONLY_TOOLS, _PLAN_MODE_KNOWN_MUTATORS

        names.update(PLAN_MODE_READONLY_TOOLS)
        names.update(_PLAN_MODE_KNOWN_MUTATORS)
    except Exception:
        pass
    return names


def build_effective_tool_policy(
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    last_user_message: object = "",
) -> ToolPolicy:
    """Compose the effective policy for one agent turn.

    Existing callers still provide the already-composed disabled-tool denylist.
    This function adds higher-level turn policy on top so enforcement is not
    delegated to prompt compliance.
    """

    disabled = {str(t) for t in (disabled_tools or []) if t}
    hidden: Set[str] = set()
    reasons = {tool: "Tool is disabled for this request." for tool in disabled}

    guide_reason = detect_guide_only_turn(last_user_message)
    if guide_reason:
        all_tools = known_tool_names()
        disabled.update(all_tools)
        hidden.update(all_tools)
        reasons.update({tool: f"{guide_reason}." for tool in all_tools})
        return ToolPolicy(
            disabled_tools=frozenset(disabled),
            hidden_tools=frozenset(hidden),
            reasons=MappingProxyType(dict(reasons)),
            mode="guide_only",
            block_all_tool_calls=True,
            disable_mcp=True,
        )

    file_route_reason = detect_smart_file_routing_turn(last_user_message)
    if file_route_reason:
        hidden.update(_SMART_FILE_ROUTING_TOOLS)
        reasons.update({tool: file_route_reason for tool in _SMART_FILE_ROUTING_TOOLS})
        return ToolPolicy(
            disabled_tools=frozenset(disabled),
            hidden_tools=frozenset(hidden),
            reasons=MappingProxyType(dict(reasons)),
            mode="file_routing",
        )

    return ToolPolicy(
        disabled_tools=frozenset(disabled),
        hidden_tools=frozenset(hidden),
        reasons=MappingProxyType(dict(reasons)),
    )
