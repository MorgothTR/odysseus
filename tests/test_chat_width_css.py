import re
from pathlib import Path


STYLE_CSS = Path(__file__).resolve().parents[1] / "static" / "style.css"


def test_desktop_chat_uses_wider_shared_readable_width():
    css = STYLE_CSS.read_text(encoding="utf-8")

    assert "--chat-readable-width: clamp(800px, 62vw, 1180px);" in css
    assert "--chat-max: var(--chat-readable-width);" in css

    for selector in (
        ".chat-container.welcome-active .chat-input-bar",
        ".chat-input-bar",
        ".attach-strip",
        ".import-prompt-banner",
    ):
        bodies = re.findall(rf"(?m)^\s*{re.escape(selector)}\s*\{{(.*?)\n\s*\}}", css, re.S)
        assert bodies, f"missing CSS rule for {selector}"
        assert any("max-width: var(--chat-readable-width);" in body for body in bodies)
