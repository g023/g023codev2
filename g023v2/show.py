"""Local `/show` inspect: what the next request would send.

Does not call the API. Does not mutate `SYSTEM_PROMPT`, history, or tools.
The cacheable prefix is still instructions + tool schemas; AGENTS.md and
mode extras sit after that prefix.
"""

from __future__ import annotations

import json
from typing import Any

from g023v2.agents_md import (
    AGENTS_MD_HEADER,
    build_request_input,
    format_agents_md_item,
    load_project_agents_md,
)
from g023v2.constants import CHARS_PER_TOKEN, MODEL, SOFT_CONTEXT_LIMIT
from g023v2.goal import GOAL_MODE_HEADER, goal_mode_input_items, goal_workspace_input_item
from g023v2.infinite import INFINITE_ORCH_HEADER, infinite_orch_input_items
from g023v2.orchestration import reasoning_request_fields, tool_names_from_schemas
from g023v2.prompt import SYSTEM_PROMPT
from g023v2.schemas import GOAL_COMPACT_TOOL_NAME, dispatch_allowlist

SHOW_HISTORY_LIMIT = 20
_PREVIEW_CHARS = 240

_TEXT_PART_TYPES = ("input_text", "output_text", "reasoning_text", "text")

# Canonical topic <- aliases. Empty canonical is the catalog.
_TOPIC_ALIASES = {
    "prompt": "prompt",
    "system": "prompt",
    "system-prompt": "prompt",
    "system_prompt": "prompt",
    "instructions": "prompt",
    "agents": "agents",
    "agents.md": "agents",
    "agents_md": "agents",
    "tools": "tools",
    "tool": "tools",
    "tool-names": "tools",
    "schemas": "schemas",
    "schema": "schemas",
    "tool-schemas": "schemas",
    "extras": "extras",
    "extra": "extras",
    "prefix": "prefix",
    "cache": "prefix",
    "request": "request",
    "input": "request",
    "next": "request",
    "effort": "effort",
    "reasoning": "effort",
    "history": "history",
    "hist": "history",
    "help": "",
}

def is_show_command(line: str) -> bool:
    """True for `/show` and `/show <topic>` (first token exact)."""
    s = (line or "").strip()
    if not s:
        return False
    return s == "/show" or s.startswith("/show ")


def format_show_help() -> str:
    """Catalog for bare `/show` and unknown topics."""
    return (
        "/show [topic]   inspect the next request (local; no API call)\n"
        "\n"
        "  prompt     frozen SYSTEM_PROMPT (instructions; cacheable prefix)\n"
        "  agents     [PROJECT AGENTS.md] post-prefix item, or absent\n"
        "  tools      tool names on the wire vs dispatch allowlist\n"
        "  schemas    full tool schema JSON sent on every request\n"
        "  extras     post-prefix extras (AGENTS.md, leftover surface, /goal)\n"
        "  prefix     cacheable prefix only (instructions + tools)\n"
        "  request    next ordinary parent request shape\n"
        "  effort     next-request reasoning fields (not in the prefix)\n"
        "  history    recent stored history items (truncated)\n"
        "\n"
        "Cacheable prefix: instructions + tools. AGENTS.md, [GOAL MODE],\n"
        "[INFINITE ORCHESTRATION], and history sit after it. Changing the\n"
        "prefix invalidates the cache from that point. /show never sends a\n"
        "request and never writes SYSTEM_PROMPT."
    )


def format_show(session: Any, topic: str = "") -> str:
    """Render one inspect topic. Unknown topics return help plus an error line."""
    raw = (topic or "").strip()
    if not raw:
        return _format_catalog(session)
    key = _TOPIC_ALIASES.get(raw.lower())
    if key is None:
        return f"unknown /show topic: {raw}\nusage: /show [topic]\n\n" + format_show_help()
    if key == "":
        return _format_catalog(session)
    if key == "prompt":
        return _format_prompt(session)
    if key == "agents":
        return _format_agents(session)
    if key == "tools":
        return _format_tools(session)
    if key == "schemas":
        return _format_schemas(session)
    if key == "extras":
        return _format_extras(session)
    if key == "prefix":
        return _format_prefix(session)
    if key == "request":
        return _format_request(session)
    if key == "effort":
        return _format_effort(session)
    if key == "history":
        return _format_history(session)
    return f"unknown /show topic: {raw}\nusage: /show [topic]\n\n" + format_show_help()


def next_request_char_count(session: Any) -> int:
    """Character count of instructions + tools JSON + next `input` array.

    Matches the context the next parent request would send (AGENTS.md and
    mode extras prepended; not stored in history). Does not include
    `model` / `stream` / effort request fields.
    """
    prompt = _instructions(session)
    schemas = getattr(session, "tool_schemas", None) or []
    history = list(getattr(session, "history", None) or [])
    agents = _agents_text(session)
    extras = _next_extra_items(session)
    request_input = build_request_input(history, agents, extras)
    return (
        len(prompt)
        + len(json.dumps(schemas, ensure_ascii=False))
        + len(json.dumps(request_input, ensure_ascii=False))
    )


def estimate_next_request_tokens(session: Any) -> int:
    """Next-request tokens as chars / CHARS_PER_TOKEN (truncated)."""
    return int(next_request_char_count(session) / CHARS_PER_TOKEN)


def format_token_amount(n: int) -> str:
    """Compact token count for the REPL prompt (`12.4k`, `400k`)."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    k = n / 1000.0
    if k < 100:
        text = f"{k:.1f}"
        if text.endswith(".0"):
            text = text[:-2]
        return text + "k"
    if n < 1_000_000:
        return f"{int(k + 0.5)}k"
    m = n / 1_000_000.0
    text = f"{m:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text + "m"


def format_context_usage(
    tokens: int,
    limit: int = SOFT_CONTEXT_LIMIT,
) -> str:
    """`12.4k/400k` next-request estimate versus the soft context limit."""
    return f"{format_token_amount(tokens)}/{format_token_amount(limit)}"


def format_show_from_line(session: Any, line: str) -> str:
    """Parse a `/show` REPL/`--once` line into `format_show` output."""
    parts = (line or "").strip().split()
    if not parts or parts[0] != "/show":
        return format_show_help()
    if len(parts) > 2:
        return "usage: /show [topic]\n\n" + format_show_help()
    topic = parts[1] if len(parts) == 2 else ""
    return format_show(session, topic)


def _instructions(session: Any) -> str:
    text = getattr(session, "system_prompt", None)
    if not isinstance(text, str) or not text:
        return SYSTEM_PROMPT
    return text


def _agents_text(session: Any) -> str | None:
    root = getattr(session, "root", None)
    if root is None:
        return None
    return load_project_agents_md(root)


def _next_extra_items(session: Any) -> list[dict[str, Any]]:
    """Same extras `run_turn` would prepend on the next parent request."""
    if getattr(session, "goal_active", False):
        extras = list(goal_mode_input_items())
        stamp = getattr(session, "goal_stamp", None)
        if stamp:
            extras.append(goal_workspace_input_item(stamp))
        return extras
    return list(infinite_orch_input_items())


def _clip(text: str, limit: int = _PREVIEW_CHARS) -> str:
    one = text.replace("\r\n", "\n")
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


def _content_text(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in _TEXT_PART_TYPES:
                parts.append(str(part.get("text") or ""))
    if parts:
        return "\n".join(parts)
    typ = item.get("type")
    if typ == "function_call":
        name = str(item.get("name") or "")
        args = item.get("arguments")
        if isinstance(args, str) and args:
            return f"{name} {args}".strip()
        return name
    if typ == "function_call_output":
        output = item.get("output")
        if isinstance(output, str):
            return output
        if isinstance(output, list):
            bits: list[str] = []
            for part in output:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("input_text", "output_text", "text"):
                    bits.append(str(part.get("text") or ""))
                elif part.get("type") == "input_image":
                    bits.append("[input_image]")
            return "\n".join(bits)
        return str(output or "")
    return ""


def _item_kind(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return "item"
    typ = item.get("type")
    role = item.get("role")
    if typ == "reasoning":
        return "reasoning"
    if typ == "function_call":
        name = item.get("name") or ""
        return f"function_call {name}".strip()
    if typ == "function_call_output":
        return "function_call_output"
    if typ == "web_search_call":
        return "web_search_call"
    if role:
        return str(role)
    if typ:
        return str(typ)
    return "item"


def _extra_label(text: str) -> str:
    stripped = (text or "").lstrip()
    if stripped.startswith(AGENTS_MD_HEADER):
        return AGENTS_MD_HEADER
    if stripped.startswith(GOAL_MODE_HEADER):
        return GOAL_MODE_HEADER
    if stripped.startswith("[GOAL WORKSPACE]"):
        return "[GOAL WORKSPACE]"
    if stripped.startswith(INFINITE_ORCH_HEADER):
        return INFINITE_ORCH_HEADER
    first = stripped.split("\n", 1)[0].strip()
    return first[:40] if first else "extra"


def _format_catalog(session: Any) -> str:
    prompt = _instructions(session)
    names = sorted(tool_names_from_schemas(getattr(session, "tool_schemas", None)))
    agents = _agents_text(session)
    extras = _next_extra_items(session)
    extra_labels = [_extra_label(_content_text(item)) for item in extras]
    if agents:
        extra_labels.insert(0, AGENTS_MD_HEADER)
    effort = getattr(session, "effort", None)
    history = getattr(session, "history", None) or []
    mode = "goal" if getattr(session, "goal_active", False) else "ordinary parent"
    lines = [
        format_show_help(),
        "",
        "This session",
        f"  model: {MODEL}",
        f"  next turn: {mode}",
        f"  instructions: {len(prompt)} chars (frozen SYSTEM_PROMPT)",
        f"  tools: {len(names)} names",
        "  AGENTS.md: " + ("loaded" if agents else "absent"),
        "  extras: " + (", ".join(extra_labels) if extra_labels else "(none)"),
        f"  history: {len(history)} stored items",
        f"  context: {format_context_usage(estimate_next_request_tokens(session))} "
        f"(est. chars/{CHARS_PER_TOKEN:g})",
        f"  effort: {effort}  wire={json.dumps(reasoning_request_fields(effort or 0), sort_keys=True)}",
        "  Type /show prompt to dump instructions.",
    ]
    return "\n".join(lines)


def _format_prompt(session: Any) -> str:
    prompt = _instructions(session)
    header = (
        f"instructions  (frozen SYSTEM_PROMPT; cacheable prefix; "
        f"{len(prompt)} chars)"
    )
    return header + "\n\n" + prompt


def _format_agents(session: Any) -> str:
    text = _agents_text(session)
    if not text:
        return (
            "agents  (post-prefix; not in SYSTEM_PROMPT)\n\n"
            "(no AGENTS.md in this project)"
        )
    item = format_agents_md_item(text)
    body = _content_text(item)
    return (
        f"agents  (post-prefix {AGENTS_MD_HEADER}; {len(body)} chars; "
        "not stored in history)\n\n"
        + body
    )


def _format_tools(session: Any) -> str:
    schemas = getattr(session, "tool_schemas", None) or []
    wire = sorted(tool_names_from_schemas(schemas))
    teams = bool(getattr(session, "teams_enabled", False))
    parent = sorted(dispatch_allowlist(teams, goal_mode=False))
    goal = sorted(dispatch_allowlist(teams, goal_mode=True))
    child = sorted(dispatch_allowlist(teams, delegate=True))
    lead = sorted(dispatch_allowlist(teams, delegate=True, lead=True))
    listed_not_parent = [n for n in wire if n not in set(parent)]
    lines = [
        "tools  (session-lifetime superset sent on every request)",
        f"wire ({len(wire)}):",
    ]
    lines.extend(f"  {name}" for name in wire)
    lines.append("")
    lines.append(f"parent ordinary dispatch ({len(parent)}):")
    lines.extend(f"  {name}" for name in parent)
    if listed_not_parent:
        lines.append("")
        lines.append("listed on the wire, not executable on an ordinary parent turn:")
        for name in listed_not_parent:
            note = ""
            if name == GOAL_COMPACT_TOOL_NAME:
                note = "  (executable only during /goal)"
            lines.append(f"  {name}{note}")
    lines.append("")
    lines.append(
        f"/goal dispatch adds: {GOAL_COMPACT_TOOL_NAME} "
        f"({GOAL_COMPACT_TOOL_NAME in set(goal)})"
    )
    lines.append(f"worker child/member dispatch is BASE only ({len(child)} names).")
    lines.append(
        f"lead child dispatch adds spawn/join/plan ({len(lead)} names; depth cap 1)."
    )
    lines.append("Seeing a name in tools is not permission to run it.")
    return "\n".join(lines)


def _format_schemas(session: Any) -> str:
    schemas = getattr(session, "tool_schemas", None) or []
    blob = json.dumps(schemas, indent=2, sort_keys=False)
    return (
        f"schemas  (session-lifetime tools JSON; {len(schemas)} entries; "
        f"{len(blob)} chars)\n\n"
        + blob
    )


def _format_extras(session: Any) -> str:
    agents = _agents_text(session)
    extras = _next_extra_items(session)
    blocks: list[str] = [
        "extras  (post-prefix input items; not stored in history; "
        "not written into SYSTEM_PROMPT)",
        "",
    ]
    if agents:
        body = _content_text(format_agents_md_item(agents))
        blocks.append(f"--- {AGENTS_MD_HEADER} ({len(body)} chars) ---")
        blocks.append(body)
        blocks.append("")
    else:
        blocks.append("--- [PROJECT AGENTS.md] ---")
        blocks.append("(absent)")
        blocks.append("")
    if not extras:
        blocks.append("(no mode extras)")
        return "\n".join(blocks).rstrip()
    for item in extras:
        body = _content_text(item)
        label = _extra_label(body)
        blocks.append(f"--- {label} ({len(body)} chars) ---")
        blocks.append(body)
        blocks.append("")
    return "\n".join(blocks).rstrip()


def _format_prefix(session: Any) -> str:
    prompt = _instructions(session)
    names = sorted(tool_names_from_schemas(getattr(session, "tool_schemas", None)))
    lines = [
        "cacheable prefix  (instructions + tools; unchanged for the session)",
        f"instructions: {len(prompt)} chars (frozen SYSTEM_PROMPT)",
        f"tools: {len(names)} names",
    ]
    lines.extend(f"  {name}" for name in names)
    lines.append("")
    lines.append(
        "Not in this prefix: AGENTS.md, [GOAL MODE], [GOAL WORKSPACE], "
        "[INFINITE ORCHESTRATION], [DELEGATE MODE], history, effort."
    )
    lines.append("Any change here invalidates the cache from this point forward.")
    return "\n".join(lines)


def _format_request(session: Any) -> str:
    prompt = _instructions(session)
    schemas = getattr(session, "tool_schemas", None) or []
    history = list(getattr(session, "history", None) or [])
    agents = _agents_text(session)
    extras = _next_extra_items(session)
    request_input = build_request_input(history, agents, extras)
    n_prefix = (1 if agents else 0) + len(extras)
    effort = getattr(session, "effort", 0)
    wire = reasoning_request_fields(effort)
    mode = "goal" if getattr(session, "goal_active", False) else "ordinary parent"
    est = estimate_next_request_tokens(session)
    lines = [
        f"next request  ({mode}; not sent; local inspect)",
        f"model: {MODEL}",
        f"context: {format_context_usage(est)} "
        f"(est. chars/{CHARS_PER_TOKEN:g}; not sent to the model)",
        f"instructions: {len(prompt)} chars (frozen SYSTEM_PROMPT; cacheable prefix)",
        f"tools: {len(schemas)} schemas (byte-stable session superset; cacheable prefix)",
        f"reasoning: {json.dumps(wire, sort_keys=True)}  (request parameter; not a message)",
        f"input: {len(request_input)} items "
        f"({n_prefix} post-prefix extras + {len(history)} history)",
    ]
    for i, item in enumerate(request_input):
        kind = _item_kind(item)
        text = _content_text(item)
        extra_note = ""
        if i < n_prefix:
            extra_note = f"  extra={_extra_label(text)}"
        lines.append(
            f"  [{i}] {kind}  {len(text)} chars{extra_note}"
        )
        preview = _clip(text)
        if preview:
            for row in preview.splitlines()[:4]:
                lines.append(f"      {row}")
    lines.append("")
    lines.append(
        "AGENTS.md and extras are prepended each request and are not in "
        "Session.history. /show prompt dumps instructions in full."
    )
    return "\n".join(lines)


def _format_effort(session: Any) -> str:
    effort = getattr(session, "effort", 0)
    wire = reasoning_request_fields(effort)
    if effort == 0:
        meaning = "thinking off (reasoning.effort none; integer 0 still thinks)"
    else:
        meaning = f"thinking on (top-level reasoning_effort={effort})"
    return "\n".join(
        [
            "effort  (next-request parameter; not in instructions or history)",
            f"internal: {effort}",
            f"meaning: {meaning}",
            f"wire: {json.dumps(wire, sort_keys=True)}",
            "HARNESS_META on the last round owns later rounds; /effort N is one-shot.",
        ]
    )


def _format_history(session: Any) -> str:
    history = list(getattr(session, "history", None) or [])
    if not history:
        return "history  (stored Session.history; extras are not stored)\n\n(empty)"
    start = max(0, len(history) - SHOW_HISTORY_LIMIT)
    lines = [
        f"history  ({len(history)} stored items; showing {start}..{len(history) - 1}; "
        "AGENTS.md/extras are not stored)",
        "",
    ]
    for i in range(start, len(history)):
        item = history[i]
        kind = _item_kind(item) if isinstance(item, dict) else type(item).__name__
        text = _content_text(item) if isinstance(item, dict) else str(item)
        lines.append(f"[{i}] {kind}  {len(text)} chars")
        preview = _clip(text)
        if preview:
            for row in preview.splitlines()[:6]:
                lines.append(f"    {row}")
        lines.append("")
    return "\n".join(lines).rstrip()
