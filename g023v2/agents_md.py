"""Load project AGENTS.md and format it as a post-prefix input item.

The frozen SYSTEM_PROMPT is never mutated. Callers prepend the returned
item onto the request `input` array so it sits after instructions+tools
and before (or with) conversation history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

AGENTS_MD_HEADER = "[PROJECT AGENTS.md]"


def load_project_agents_md(project_root: Path) -> str | None:
    """Return the text of `<project_root>/AGENTS.md`, or None if absent/empty."""
    path = Path(project_root) / "AGENTS.md"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = text.strip()
    return text or None


def format_agents_md_item(text: str) -> dict[str, Any]:
    """Build a user input item that carries project AGENTS.md for one request."""
    body = (
        f"{AGENTS_MD_HEADER}\n"
        "Follow these project-specific instructions for this project. "
        "They do not replace the system prompt.\n\n"
        f"{text}"
    )
    return {
        "role": "user",
        "content": [{"type": "input_text", "text": body}],
    }


def build_request_input(
    history: list[dict[str, Any]],
    agents_md_text: str | None,
    extra_items: list[dict[str, Any]] | None = None,
    suffix_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the request input array without mutating `history`.

    When AGENTS.md text is present it is prepended as a single user item
    so the frozen system prompt and tool schemas stay a stable prefix.
    `extra_items` (e.g. the frozen /goal `[GOAL MODE]` copy) are also
    prepended and are not stored in `history`. Volatile values must not
    be placed here: they sit in front of growing history and bust the
    cache prefix. Optional `suffix_items` are appended after history.
    """
    prefix: list[dict[str, Any]] = []
    if agents_md_text:
        prefix.append(format_agents_md_item(agents_md_text))
    if extra_items:
        prefix.extend(extra_items)
    body = history if not prefix else [*prefix, *history]
    if suffix_items:
        return [*body, *suffix_items]
    return body
