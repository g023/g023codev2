"""Turn loop, tool dispatch, metadata parse, and compaction. HTTP only via run_turn."""

from __future__ import annotations

import copy
import json
import re
import threading
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from g023v2.agents_md import build_request_input, load_project_agents_md
from g023v2.constants import (
    COMPACT_AT_FRACTION,
    DEFAULT_CHILD_EFFORT,
    MAX_OUTPUT_TOKENS,
    MAX_TOOL_ROUNDS,
    MODEL,
    RECENT_WINDOW_TURNS,
    SOFT_CONTEXT_LIMIT,
)
from g023v2.context_budget import budget_tool_result
from g023v2.goal import (
    apply_goal_compaction,
    goal_mode_input_items,
    goal_workspace_input_item,
)
from g023v2.infinite import infinite_orch_input_items
from g023v2.plan import delegate_mode_input_items, effort_for_role
from g023v2.http import stream_response
from g023v2.messages import MessageQueue, assistant_message, reasoning_item, user_message
from g023v2.persist import MemoryStore
from g023v2.prompt import SYSTEM_PROMPT
from g023v2.schemas import GOAL_COMPACT_TOOL_NAME
from g023v2.skills import SkillStore
from g023v2.tools import ProjectTools
from g023v2.util import (
    COLOR_ANSWER,
    COLOR_ERROR,
    COLOR_THINK,
    COLOR_TOOL,
    LivePrinter,
    MIN_THINKING_EFFORT,
    clamp_effort,
    clamp_thinking_effort,
    log,
    paint,
    tool_result_color,
)

if TYPE_CHECKING:
    from g023v2.session import Session


# ---------------------------------------------------------------------------
# Response metadata
# ---------------------------------------------------------------------------

META_RE = re.compile(r"```HARNESS_META\s*\n(.*?)\n```", re.DOTALL)

DEFAULT_META = {
    "summary": "Turn completed without explicit metadata.",
    "compactable": True,
    "compact_after_turns": 2,
    "reason_next": True,
    "next_reasoning_effort": 40,
}


def _as_bool(value: Any) -> bool:
    """Liberal bool for model-emitted JSON (true/false, 0/1, yes/no strings)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("false", "0", "no", "off"):
            return False
        if s in ("true", "1", "yes", "on"):
            return True
    return bool(value)


def parse_harness_meta_block(text: str) -> tuple[dict[str, Any] | None, str]:
    """Raw HARNESS_META JSON, or None if missing/invalid. Second value is text with the fence removed."""
    if not text:
        return None, text or ""
    match = META_RE.search(text)
    if not match:
        return None, text
    cleaned = (text[: match.start()] + text[match.end() :]).rstrip()
    try:
        meta = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None, cleaned
    if not isinstance(meta, dict):
        return None, cleaned
    return meta, cleaned


def reasoning_request_fields(effort: int) -> dict[str, Any]:
    """Wire fields for the next Responses request from internal effort.

    Internal 0 → nested enum `reasoning.effort: "none"` (the only live off
    switch; top-level `reasoning_effort: 0` still thinks). 1-100 → top-level
    integer `reasoning_effort`. Nested integer `reasoning.effort` is HTTP 400
    and is never used. Callers must not also set the other field.
    """
    effort = clamp_effort(effort)
    if effort < MIN_THINKING_EFFORT:
        return {"reasoning": {"effort": "none"}}
    return {"reasoning_effort": int(effort)}


def effort_from_round_meta(raw_meta: dict[str, Any] | None, current: int) -> int:
    """Next request's internal effort from this round's raw HARNESS_META.

    Missing/invalid block keeps `current`. reason_next false (or 0/"false")
    is thinking off (internal 0; the request layer sends reasoning.effort
    "none", never integer 0). Otherwise next_reasoning_effort is clamped
    1-100 when present. reason_next true with no number keeps current,
    except current 0 becomes 1 (lowest thinking-on budget).
    """
    current = clamp_effort(current)
    if not raw_meta:
        return current
    if "reason_next" in raw_meta and not _as_bool(raw_meta["reason_next"]):
        return 0
    if "next_reasoning_effort" in raw_meta:
        try:
            return clamp_thinking_effort(raw_meta["next_reasoning_effort"])
        except (TypeError, ValueError):
            return current if current >= MIN_THINKING_EFFORT else MIN_THINKING_EFFORT
    if current < MIN_THINKING_EFFORT:
        return MIN_THINKING_EFFORT
    return current


def extract_metadata(text: str) -> tuple[dict[str, Any], str]:
    raw, cleaned = parse_harness_meta_block(text or "")
    merged = dict(DEFAULT_META)
    if not raw:
        return merged, text or ""
    for k in merged:
        if k in raw:
            merged[k] = raw[k]
    try:
        merged["next_reasoning_effort"] = clamp_effort(merged["next_reasoning_effort"])
    except (TypeError, ValueError):
        merged["next_reasoning_effort"] = DEFAULT_META["next_reasoning_effort"]
    merged["reason_next"] = _as_bool(merged["reason_next"])
    return merged, cleaned


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def tool_names_from_schemas(tool_schemas: list[dict[str, Any]] | None) -> frozenset[str]:
    """Names this turn actually offered. Isolation is this set, not a second table."""
    names: set[str] = set()
    for schema in tool_schemas or []:
        if not isinstance(schema, dict):
            continue
        name = schema.get("name")
        if name:
            names.add(str(name))
        elif schema.get("type") == "web_search":
            names.add("web_search")
    return frozenset(names)


def dispatch_tool(
    name: str,
    args: dict[str, Any],
    tools: ProjectTools,
    memory: MemoryStore,
    session: Session | None = None,
    allowed_names: set[str] | frozenset[str] | None = None,
    skills: SkillStore | None = None,
) -> Any:
    if allowed_names is not None and name not in allowed_names:
        return f"ERROR: tool '{name}' is not available in this turn"
    try:
        if name == "read_file":
            return tools.read_file(**args)
        if name == "read_files":
            return tools.read_files(**args)
        if name == "write_file":
            return tools.write_file(**args)
        if name == "edit_file":
            return tools.edit_file(**args)
        if name == "apply_edits":
            return tools.apply_edits(**args)
        if name == "replace_all":
            return tools.replace_all(**args)
        if name == "replace_in_files":
            return tools.replace_in_files(**args)
        if name == "restore_file":
            return tools.restore_file(**args)
        if name == "list_dir":
            return tools.list_dir(**args)
        if name == "find_files":
            return tools.find_files(**args)
        if name == "grep":
            return tools.grep(**args)
        if name == "read_image":
            return tools.read_image(**args)
        if name == "run_shell":
            return tools.run_shell(**args)
        if name == "run_cli":
            cancel = (
                getattr(session, "turn_cancel_event", None) if session is not None else None
            )
            kwargs = {k: v for k, v in args.items() if k != "cancel_event"}
            return tools.run_cli(cancel_event=cancel, **kwargs)
        if name == "memory_write":
            return memory.write(**args)
        if name == "memory_read":
            val = memory.read(**args)
            return val if val else "(no value stored)"
        if name == "memory_list":
            return memory.list_keys()
        if name in ("skill_search", "skill_read", "skill_write"):
            store = skills
            if store is None and session is not None:
                store = getattr(session, "skills", None)
            if store is None:
                return "ERROR: skill store is not available"
            if name == "skill_search":
                return store.search(
                    query=str(args.get("query") or ""),
                    limit=args.get("limit"),
                )
            if name == "skill_read":
                return store.read(slug=str(args.get("slug") or ""))
            return store.write(
                slug=str(args.get("slug") or ""),
                name=str(args.get("name") or ""),
                description=str(args.get("description") or ""),
                body=str(args.get("body") or ""),
                tags=args.get("tags"),
            )
        if name == "fetch_url":
            return tools.fetch_url(**args)

        if session is None:
            return f"ERROR: {name} requires an active session"

        if name == "spawn_child":
            effort_arg = args.get("effort")
            effort_val = None if effort_arg is None else int(effort_arg)
            return session.spawn_child(
                name=args["name"],
                task=args["task"],
                effort=effort_for_role(args.get("role"), effort_val),
                context=args.get("context", ""),
                fresh=bool(args.get("fresh", True)),
                role=str(args.get("role") or ""),
            )
        if name == "plan_work":
            return session.plan_work(
                action=str(args.get("action") or ""),
                item_id=str(args.get("id") or ""),
                title=str(args.get("title") or ""),
                details=str(args.get("details") or ""),
                role=str(args.get("role") or ""),
                effort=args.get("effort"),
                depends_on=args.get("depends_on"),
                files=args.get("files"),
                result=str(args.get("result") or ""),
            )
        if name == "join_children":
            return session.join_children(
                children=args.get("children"),
                timeout=args.get("timeout"),
            )
        if name == "send_message":
            return session.send_to_child(
                child=args["child"],
                message=args["message"],
                kind=args.get("kind", "supplement"),
            )
        if name == "edit_message":
            return session.edit_child_message(
                message_id=args["message_id"], new_content=args["new_content"]
            )
        if name == "delete_message":
            return session.delete_child_message(message_id=args["message_id"])
        if name == "interject":
            return session.interject_child(child=args["child"], message=args["message"])
        if name == "stop_child":
            return session.stop_child(child=args["child"])
        if name == "list_children":
            return session.list_children()
        if name == "get_child_result":
            timeout = args.get("timeout")
            return session.get_child_result(child=args["child"], timeout=timeout)

        if name == "team_create_member":
            return session.team_create_member(
                name=args["name"],
                role=args["role"],
                effort=int(args.get("effort", DEFAULT_CHILD_EFFORT)),
            )
        if name == "team_create_task":
            return session.team_create_task(
                title=args["title"],
                details=args["details"],
                depends_on=args.get("depends_on"),
                files=args.get("files"),
            )
        if name == "team_claim_task":
            return session.team_claim_task(task_id=args["task_id"], member=args["member"])
        if name == "team_complete_task":
            return session.team_complete_task(
                task_id=args["task_id"], member=args["member"], result=args["result"]
            )
        if name == "team_send_message":
            return session.team_send_message(
                sender=args["sender"], recipient=args["recipient"], message=args["message"]
            )
        if name == "team_status":
            return session.team_status()
        if name == "team_interrupt":
            return session.team_interrupt(member=args["member"])
        if name == "team_list_tasks":
            return session.team_list_tasks()

        if name == GOAL_COMPACT_TOOL_NAME:
            if session is None or not getattr(session, "goal_active", False):
                return "ERROR: compact_goal_conversation is only available during /goal"
            fields = ("goal", "done", "remaining", "next")
            missing = [f for f in fields if not str(args.get(f, "")).strip()]
            if missing:
                return (
                    "ERROR: compact_goal_conversation missing field(s): "
                    + ", ".join(missing)
                )
            return (
                "compacted goal conversation into one record "
                "(goal / done / remaining / next)"
            )

        return f"ERROR: unknown tool {name}"
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:
        log(paint(
            f"[dispatch] tool {name} raised:\n{traceback.format_exc()}",
            COLOR_ERROR,
        ))
        return f"ERROR: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

def estimate_tokens(history: list[dict[str, Any]]) -> int:
    total = 0
    for item in history:
        total += len(json.dumps(item, ensure_ascii=False)) // 4
    return total


def _is_clean_start(item: dict[str, Any]) -> bool:
    if item.get("role") == "user":
        return True
    if item.get("role") == "assistant":
        return True
    if item.get("type") == "message":
        return True
    # Remaining-turn reasoning must not be dropped when the split lands
    # mid-round (function_call / output are not clean starts).
    if item.get("type") == "reasoning":
        return True
    return False


def _find_clean_boundary(history: list[dict[str, Any]], start: int) -> int | None:
    i = start
    while i < len(history):
        if _is_clean_start(history[i]):
            return i
        i += 1
    return None


def _compact_delay(meta: dict[str, Any]) -> int:
    raw = meta.get("compact_after_turns", 0)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(10, n))


def _meta_ready_to_compact(meta: dict[str, Any], index: int, n_turns: int) -> bool:
    if not meta.get("compactable", True):
        return False
    return (n_turns - 1 - index) >= _compact_delay(meta)


def compact_history(
    history: list[dict[str, Any]],
    turn_metadata: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Fold the oldest eligible span into one summary message.

    Returns (new_history, new_metadata, fired). fired is False when compaction
    was skipped, in which case the caller should keep the original values.

    The recent window is counted in turn_metadata rows (user turns), not
    assistant scratchpad items. compact_after_turns delays a turn until that
    many later turns have been recorded.
    """
    n_turns = len(turn_metadata)
    if n_turns <= RECENT_WINDOW_TURNS:
        return history, turn_metadata, False
    cutoff = n_turns - RECENT_WINDOW_TURNS
    eligible = turn_metadata[:cutoff]
    if not eligible:
        return history, turn_metadata, False
    if not all(_meta_ready_to_compact(m, i, n_turns) for i, m in enumerate(eligible)):
        return history, turn_metadata, False
    summaries = [m.get("summary", "") for m in eligible if m.get("summary")]
    if not summaries:
        return history, turn_metadata, False

    summary_text = (
        "[COMPACTED HISTORY]\n"
        + "\n".join(f"- {s}" for s in summaries)
        + "\n[END COMPACTED HISTORY]"
    )

    user_indices = [
        i for i, item in enumerate(history)
        if item.get("role") == "user"
    ]
    if len(user_indices) <= RECENT_WINDOW_TURNS:
        return history, turn_metadata, False
    start = user_indices[len(user_indices) - RECENT_WINDOW_TURNS]
    boundary = _find_clean_boundary(history, start)
    if boundary is None:
        log("[compact] no clean boundary found; skipping compaction this turn")
        return history, turn_metadata, False

    new_history = [user_message(summary_text)] + history[boundary:]
    new_metadata = turn_metadata[cutoff:]
    return new_history, new_metadata, True


def should_compact(history: list[dict[str, Any]]) -> bool:
    return estimate_tokens(history) >= int(SOFT_CONTEXT_LIMIT * COMPACT_AT_FRACTION)


def parse_cache_usage(usage: dict[str, Any] | None) -> tuple[int, int]:
    """Return (hit, miss) token counts from a Responses API usage object.

    OpenAI-style fields are preferred when present. DeepSeek reports cache
    hits as input_tokens_details.cached_tokens; miss is the rest of input_tokens.
    """
    usage = usage or {}
    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens") or 0
    if hit or miss:
        return int(hit), int(miss)
    details = usage.get("input_tokens_details") or {}
    hit = int(details.get("cached_tokens") or 0)
    total_in = int(usage.get("input_tokens") or 0)
    miss = max(0, total_in - hit)
    return hit, miss


@dataclass(frozen=True)
class RoundTokens:
    """Per-round usage: cached input, uncached input, and output tokens."""

    cached: int = 0
    uncached: int = 0
    output: int = 0


def parse_round_tokens(usage: dict[str, Any] | None) -> RoundTokens:
    """Map a Responses API usage object to cached / uncached / output counts."""
    cached, uncached = parse_cache_usage(usage)
    usage = usage or {}
    output = usage.get("output_tokens")
    if output is None:
        output = usage.get("completion_tokens")
    return RoundTokens(
        cached=cached,
        uncached=uncached,
        output=int(output or 0),
    )


def format_round_tokens(round_no: int, tokens: RoundTokens) -> str:
    return (
        f"[round {round_no}] tokens  "
        f"cached={tokens.cached:,}  uncached={tokens.uncached:,}  "
        f"output={tokens.output:,}"
    )


# ---------------------------------------------------------------------------
# Stream-round commit (reasoning must be passed back when tools are sent)
# ---------------------------------------------------------------------------

def _reasoning_text_of(item: dict[str, Any]) -> str:
    content = item.get("content") or []
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "reasoning_text"
    )


def _assistant_text_of(item: dict[str, Any]) -> str:
    content = item.get("content") or []
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") in ("output_text", "text")
    )


def ensure_reasoning_content(
    reasoning_items: list[dict[str, Any]],
    fallback_by_index: dict[Any, str],
    fallback_concat: str,
) -> list[dict[str, Any]]:
    """Guarantee stored reasoning items carry reasoning_text parts.

    DeepSeek returns HTTP 400 if a later request includes `tools` but omits
    prior-turn `reasoning_text`. `response.output_item.done` sometimes has
    empty content; `response.reasoning_text.delta` / `.done` is the fallback.
    Mutates the given dicts in place so an arrival-order list that holds the
    same objects stays filled.
    """
    filled: list[dict[str, Any]] = []
    for i, item in enumerate(reasoning_items):
        item.setdefault("type", "reasoning")
        if not _reasoning_text_of(item):
            idx = item.get("output_index", i)
            text = fallback_by_index.get(idx) or fallback_concat
            if text:
                item["content"] = [{"type": "reasoning_text", "text": text}]
        if _reasoning_text_of(item):
            filled.append(item)
    if not filled and fallback_concat:
        filled.append(reasoning_item(fallback_concat))
    return filled


def _is_assistant_message_item(item: dict[str, Any]) -> bool:
    if item.get("role") == "assistant":
        return True
    if item.get("type") == "message" and item.get("role") in (None, "assistant"):
        return True
    return False


def strip_harness_meta_from_item(item: dict[str, Any]) -> bool:
    """Strip a HARNESS_META fence in-place. Keeps id/status/original parts.

    Returns True if any content text changed. Invalid JSON fences are
    removed via parse_harness_meta_block (same as commit-time strip).
    """
    content = item.get("content")
    changed = False
    if isinstance(content, str):
        _raw, cleaned = parse_harness_meta_block(content)
        if cleaned != content:
            item["content"] = cleaned
            changed = True
        return changed
    if not isinstance(content, list):
        return False
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") not in ("output_text", "text"):
            continue
        text = part.get("text") or ""
        _raw, cleaned = parse_harness_meta_block(text)
        if cleaned != text:
            part["text"] = cleaned
            changed = True
    return changed


def commit_stream_round(
    history: list[dict[str, Any]],
    *,
    output_items: list[dict[str, Any]] | None = None,
    reasoning_items: list[dict[str, Any]] | None = None,
    web_search_items: list[dict[str, Any]] | None = None,
    assistant_text: str = "",
    function_calls: list[dict[str, Any]] | None = None,
    function_call_outputs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Append one parsed Responses API round onto `history`.

    When `output_items` is set (API arrival order from parse_stream_events),
    those items are appended in that order. Message items keep id/status/parts
    with only the HARNESS_META fence stripped in content. Function-call
    outputs (local) still follow the API items.

    Without `output_items`, the legacy typed-list order is used: reasoning,
    web_search_call, assistant text, function_call, function_call_output.
    """
    appended: list[dict[str, Any]] = []

    def _add(item: dict[str, Any]) -> None:
        history.append(item)
        appended.append(item)

    if output_items is not None:
        for raw in output_items:
            item = copy.deepcopy(raw)
            typ = item.get("type")
            if typ == "reasoning" and not _reasoning_text_of(item):
                continue
            if _is_assistant_message_item(item):
                strip_harness_meta_from_item(item)
                if not (_assistant_text_of(item) or "").strip() and not item.get("id"):
                    continue
            _add(item)
        for item in function_call_outputs or []:
            _add(item)
        return appended

    for item in reasoning_items or []:
        if _reasoning_text_of(item):
            _add(item)
    for item in web_search_items or []:
        _add(item)
    if (assistant_text or "").strip():
        _add(assistant_message(assistant_text))
    for item in function_calls or []:
        _add(item)
    for item in function_call_outputs or []:
        _add(item)
    return appended


def strip_harness_meta_in_span(history: list[dict[str, Any]], start: int) -> None:
    """Remove a HARNESS_META fence from assistant items in history[start:].

    Edits the already-committed item in place (id/status/parts kept). Does
    not append a second assistant-only copy of the turn text.
    """
    for i in range(start, len(history)):
        item = history[i]
        if not _is_assistant_message_item(item):
            continue
        strip_harness_meta_from_item(item)


@dataclass
class ParsedStream:
    """Folded SSE round. Iterates as the legacy 6-tuple for unpack compatibility."""

    text: str
    function_calls: list[dict[str, Any]]
    reasoning_items: list[dict[str, Any]]
    web_search_items: list[dict[str, Any]]
    usage: dict[str, Any]
    cancelled: bool
    status: str = "completed"
    output_items: list[dict[str, Any]] = field(default_factory=list)

    def __iter__(self) -> Iterator[Any]:
        yield self.text
        yield self.function_calls
        yield self.reasoning_items
        yield self.web_search_items
        yield self.usage
        yield self.cancelled


def parse_stream_events(
    events: Iterator[tuple[str, dict[str, Any]]],
    cancel_event: threading.Event | None = None,
    live: LivePrinter | None = None,
) -> ParsedStream:
    """Fold Responses API SSE events into text, tool calls, and reasoning items.

    When `live` is set, reasoning and output deltas are echoed as they arrive
    so a long think is not a silent wait. `status` is `completed`, `incomplete`,
    or `failed` from the terminal SSE event (default completed if none arrives).
    """
    text_deltas: list[str] = []
    done_text_by_index: dict[int, str] = {}
    function_calls: list[dict[str, Any]] = []
    reasoning_items: list[dict[str, Any]] = []
    web_search_items: list[dict[str, Any]] = []
    output_items: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    cancelled = False
    stream_status = "completed"
    reasoning_deltas_by_index: dict[int, list[str]] = {}
    reasoning_done_by_index: dict[int, str] = {}

    try:
        for event_type, data in events:
            if event_type == "response.output_text.delta":
                delta = data.get("delta", "") or ""
                text_deltas.append(delta)
                if live and delta:
                    live.feed("model", delta)
            elif event_type == "response.reasoning_text.delta":
                idx = data.get("output_index", 0)
                if idx is None:
                    idx = 0
                delta = data.get("delta", "") or ""
                reasoning_deltas_by_index.setdefault(idx, []).append(delta)
                if live and delta:
                    live.feed("think", delta)
            elif event_type == "response.reasoning_text.done":
                idx = data.get("output_index", 0)
                if idx is None:
                    idx = 0
                done_reasoning = data.get("text") or ""
                if done_reasoning:
                    reasoning_done_by_index[idx] = done_reasoning
                    if live and idx not in reasoning_deltas_by_index:
                        live.feed("think", done_reasoning)
            elif event_type == "response.output_item.done":
                item = data.get("item", {})
                t = item.get("type")
                if t in ("function_call", "reasoning", "web_search_call", "message"):
                    output_items.append(item)
                if t == "function_call":
                    function_calls.append(item)
                elif t == "reasoning":
                    reasoning_items.append(item)
                elif t == "web_search_call":
                    web_search_items.append(item)
                elif t == "message":
                    idx = item.get("output_index", len(done_text_by_index))
                    content = item.get("content", [])
                    joined = "".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "output_text"
                    )
                    if joined:
                        done_text_by_index[idx] = joined
                        if live and not "".join(text_deltas):
                            live.feed("model", joined)
            elif event_type == "response.completed":
                resp = data.get("response", data)
                usage = resp.get("usage", {}) or usage
                stream_status = "completed"
            elif event_type == "response.incomplete":
                resp = data.get("response", data)
                usage = resp.get("usage", {}) or usage
                stream_status = "incomplete"
            elif event_type == "response.failed":
                resp = data.get("response", data)
                usage = resp.get("usage", {}) or usage
                stream_status = "failed"

            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
    finally:
        if live is not None:
            live.end()

    if done_text_by_index:
        text = "\n".join(done_text_by_index[i] for i in sorted(done_text_by_index))
    else:
        text = "".join(text_deltas)

    fallback_by_index: dict[Any, str] = {}
    for idx in set(reasoning_done_by_index) | set(reasoning_deltas_by_index):
        fallback_by_index[idx] = reasoning_done_by_index.get(idx) or "".join(
            reasoning_deltas_by_index.get(idx, [])
        )
    if reasoning_done_by_index:
        fallback_concat = "".join(
            reasoning_done_by_index[i] for i in sorted(reasoning_done_by_index)
        )
    else:
        fallback_concat = "".join(
            "".join(reasoning_deltas_by_index[i]) for i in sorted(reasoning_deltas_by_index)
        )
    reasoning_items = ensure_reasoning_content(
        reasoning_items, fallback_by_index, fallback_concat
    )
    present_ids = {id(x) for x in output_items}
    for r in reasoning_items:
        if id(r) not in present_ids:
            output_items.insert(0, r)
            present_ids.add(id(r))

    return ParsedStream(
        text=text,
        function_calls=function_calls,
        reasoning_items=reasoning_items,
        web_search_items=web_search_items,
        usage=usage,
        cancelled=cancelled,
        status=stream_status,
        output_items=output_items,
    )


# ---------------------------------------------------------------------------
# Turn runner
# ---------------------------------------------------------------------------

@dataclass
class TurnResult:
    text: str
    usage: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning_items: list[dict[str, Any]] = field(default_factory=list)
    web_search_items: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    round_tokens: list[RoundTokens] = field(default_factory=list)
    effort: int = 60
    goal_compacted: bool = False
    hit_round_cap: bool = False


def _drain_messages_into_history(
    message_source: MessageQueue | None,
    history: list[dict[str, Any]],
) -> int:
    if message_source is None:
        return 0
    msgs = message_source.drain()
    for m in msgs:
        history.append(user_message(f"[{m.kind} from {m.sender}] {m.content}"))
    return len(msgs)


def _process_stream(
    payload: dict[str, Any],
    cancel_event: threading.Event | None,
    live: LivePrinter | None = None,
) -> ParsedStream:
    return parse_stream_events(
        stream_response(payload, cancel_event=cancel_event),
        cancel_event,
        live=live,
    )


def preview_text(s: str, limit: int = 100) -> str:
    """One-line preview for stderr logs. Collapses whitespace."""
    one = " ".join((s or "").split())
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


def preview_tool_result(result: Any, limit: int = 120) -> str:
    """Preview a tool result that may be a string or vision content parts."""
    if isinstance(result, str):
        return preview_text(result, limit)
    if isinstance(result, list):
        kinds: list[str] = []
        texts: list[str] = []
        for part in result:
            if not isinstance(part, dict):
                continue
            typ = str(part.get("type") or "?")
            kinds.append(typ)
            if typ in ("input_text", "output_text", "text"):
                texts.append(str(part.get("text") or ""))
        label = ",".join(kinds) if kinds else "parts"
        extra = " ".join(texts).strip()
        if extra:
            return preview_text(f"{label} {extra}", limit)
        return preview_text(label, limit)
    return preview_text(str(result), limit)


def preview_tool_args(name: str, args: dict[str, Any], limit: int = 120) -> str:
    """One-line tool-argument preview. Does not dump write_file bodies."""
    if not isinstance(args, dict):
        return preview_text(str(args), limit)
    if name == "write_file":
        path = args.get("path", "?")
        n = len(args.get("content") or "")
        return f"path={path} content={n} chars"
    if name == "skill_write":
        slug = args.get("slug", "?")
        n = len(args.get("body") or "")
        return f"slug={slug} body={n} chars"
    if name == "skill_search":
        bits = [f"query={preview_text(str(args.get('query') or ''), 60)}"]
        if args.get("limit") is not None:
            bits.append(f"limit={args['limit']}")
        return " ".join(bits)
    if name == "skill_read":
        return f"slug={args.get('slug', '?')}"
    if name in ("edit_file", "replace_all"):
        path = args.get("path", "?")
        return (
            f"path={path} "
            f"old={preview_text(str(args.get('old_string') or ''), 40)} "
            f"new={preview_text(str(args.get('new_string') or ''), 40)}"
        )
    if name == "replace_in_files":
        bits = [
            f"glob={args.get('glob', '?')}",
            f"old={preview_text(str(args.get('old_string') or ''), 40)}",
            f"new={preview_text(str(args.get('new_string') or ''), 40)}",
        ]
        if args.get("path"):
            bits.append(f"path={args['path']}")
        if args.get("max_files") is not None:
            bits.append(f"max_files={args['max_files']}")
        return " ".join(bits)
    if name == "apply_edits":
        edits = args.get("edits")
        n = len(edits) if isinstance(edits, list) else "?"
        return f"path={args.get('path', '?')} edits={n}"
    if name == "read_files":
        paths = args.get("paths")
        n = len(paths) if isinstance(paths, list) else "?"
        return f"paths={n}"
    if name == "read_image":
        return f"path={args.get('path', '?')}"
    if name == "run_shell":
        cmd = preview_text(str(args.get("command") or ""), limit)
        timeout = args.get("timeout")
        if timeout is not None:
            return f"{cmd} timeout={timeout}"
        return cmd
    if name == "run_cli":
        cmd = preview_text(str(args.get("command") or ""), limit)
        bits = [cmd]
        if args.get("timeout") is not None:
            bits.append(f"timeout={args['timeout']}")
        if args.get("want_output"):
            bits.append("want_output")
        return " ".join(bits)
    if name == "find_files":
        bits = [f"glob={args.get('glob', '?')}"]
        if args.get("path"):
            bits.append(f"path={args['path']}")
        if args.get("max_results") is not None:
            bits.append(f"max_results={args['max_results']}")
        return " ".join(bits)
    if name == "fetch_url":
        bits = [f"url={preview_text(str(args.get('url') or ''), 80)}"]
        if args.get("timeout") is not None:
            bits.append(f"timeout={args['timeout']}")
        if args.get("cache_mode"):
            bits.append(f"cache_mode={args['cache_mode']}")
        if args.get("extract"):
            bits.append(f"extract={args['extract']}")
        if args.get("max_age") is not None:
            bits.append(f"max_age={args['max_age']}")
        if args.get("max_chars") is not None:
            bits.append(f"max_chars={args['max_chars']}")
        return " ".join(bits)
    if name in ("read_file", "restore_file", "list_dir"):
        bits = [f"path={args.get('path', '?')}"]
        if args.get("offset") is not None:
            bits.append(f"offset={args['offset']}")
        if args.get("limit") is not None:
            bits.append(f"limit={args['limit']}")
        return " ".join(bits)
    try:
        dumped = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        dumped = str(args)
    return preview_text(dumped, limit)


def select_turn_text(
    *,
    no_tool_texts: list[str],
    abort_notes: list[str],
    n_rounds: int,
    n_calls: int,
    hit_round_cap: bool,
    cancelled: bool,
) -> str:
    """User-visible turn text.

    Intermediate assistant text on tool-call rounds is scratchpad; it is
    logged to stderr, not printed as the answer. The CLI shows the last
    no-tool round. If that round is empty (the model stopped after tools
    without a closing message), return a short completion note rather
    than concatenating the scratchpad.
    """
    finals = [t.strip() for t in no_tool_texts if (t or "").strip()]
    notes = [t.strip() for t in abort_notes if (t or "").strip()]
    if finals:
        body = "\n".join(finals)
    elif notes:
        body = "\n".join(notes)
    elif cancelled:
        body = "Turn cancelled."
    elif hit_round_cap:
        body = (
            f"Turn stopped after {n_rounds} tool round(s) "
            f"({n_calls} tool call(s)); round cap reached. "
            "The model did not write a final message."
        )
    else:
        body = (
            f"Turn completed after {n_rounds} tool round(s) "
            f"({n_calls} tool call(s)). "
            "The model did not write a final message."
        )
    if cancelled and "cancelled" not in body.lower():
        body = (body + "\n[cancelled]").strip()
    return body


TOOL_ROUND_CAP_HEADER = "[TOOL ROUND CAP]"


def format_tool_round_cap_suffix(n_rounds: int, cap: int) -> str:
    """Suffix-only wrap-up instruction. Not stored in history (cache-safe)."""
    return (
        f"{TOOL_ROUND_CAP_HEADER}\n"
        f"This turn used {n_rounds} tool round(s) (limit {cap}). "
        "Do not call tools on THIS request. Write the current result, "
        "what is done, and what remains. The session continues after "
        "this turn; this is not a cancellation. The next turn (and a "
        "later /goal inner turn or leftover-chain orchestration) may "
        "call tools again."
    )


def run_turn(
    history: list[dict[str, Any]],
    effort: int,
    tools: ProjectTools,
    memory: MemoryStore,
    tool_schemas: list[dict[str, Any]],
    system_prompt: str = SYSTEM_PROMPT,
    session: Session | None = None,
    cancel_event: threading.Event | None = None,
    message_source: MessageQueue | None = None,
    log_prefix: str = "",
    goal_mode: bool = False,
    on_pulse: Callable[[], None] | None = None,
    allowed_names: set[str] | frozenset[str] | None = None,
    max_tool_rounds: int | None = None,
    surface_leftovers: bool = False,
    delegate_mode: bool = False,
) -> TurnResult:
    """Loop tool rounds until a no-tool completion, wrap-up cap, cancel, or abort.

    `tool_schemas` is what is sent on the wire (session-lifetime superset).
    `allowed_names` is the execution jail; it may be narrower. Seeing a name
    in `tools` is not permission to run it. When omitted, schema names are
    used and `compact_goal_conversation` is dropped unless `goal_mode`.

    After `max_tool_rounds` (default `MAX_TOOL_ROUNDS`) tool-using rounds,
    one wrap-up request is sent (suffix instruction, tools not dispatched)
    and the turn returns. That is not a cancel: callers such as `/goal`
    keep running.

    `surface_leftovers` injects the ordinary `[INFINITE ORCHESTRATION]`
    leftover-surfacing extra. Parent ordinary turns set it. Children,
    members, and `/goal` (which already has leftover text in `[GOAL MODE]`)
    leave it false.

    `delegate_mode` injects the frozen `[DELEGATE MODE]` extra (and the
    live `[GOAL WORKSPACE]` stamp when a /goal is open). Child/member
    turns set it. It is stable across children; do not put names or tasks
    in that extra.
    """
    no_tool_texts: list[str] = []
    abort_notes: list[str] = []
    all_tool_calls: list[dict[str, Any]] = []
    reasoning_items: list[dict[str, Any]] = []
    web_search_items: list[dict[str, Any]] = []
    last_usage: dict[str, Any] = {}
    collected_tokens: list[RoundTokens] = []
    was_cancelled = False
    effort = clamp_effort(effort)
    rounds_used = 0
    goal_compacted = False
    hit_round_cap = False
    wrapping_up = False
    round_cap = (
        MAX_TOOL_ROUNDS if max_tool_rounds is None else max(1, int(max_tool_rounds))
    )
    schema_names = tool_names_from_schemas(tool_schemas)
    if allowed_names is None:
        allowed = set(schema_names)
        if not goal_mode:
            allowed.discard(GOAL_COMPACT_TOOL_NAME)
        allowed_names = frozenset(allowed)
    else:
        allowed_names = frozenset(allowed_names)

    def emit(msg: str) -> None:
        if log_prefix:
            log(f"[{log_prefix}] {msg}")
        else:
            log(msg)

    def pulse() -> None:
        if on_pulse is not None:
            on_pulse()

    def _finish(*, cancelled: bool) -> TurnResult:
        return TurnResult(
            text=select_turn_text(
                no_tool_texts=no_tool_texts,
                abort_notes=abort_notes,
                n_rounds=rounds_used,
                n_calls=len(all_tool_calls),
                hit_round_cap=hit_round_cap,
                cancelled=cancelled,
            ),
            usage=last_usage,
            tool_calls=all_tool_calls,
            reasoning_items=reasoning_items,
            web_search_items=web_search_items,
            cancelled=cancelled,
            round_tokens=collected_tokens,
            effort=effort,
            goal_compacted=goal_compacted,
            hit_round_cap=hit_round_cap,
        )

    while True:
        if cancel_event is not None and cancel_event.is_set():
            was_cancelled = True
            break

        pulse()
        rounds_used += 1
        if wrapping_up:
            emit(
                f"[round {rounds_used}] wrap-up "
                f"(tool-round cap {round_cap}, session continues)"
            )
        else:
            emit(f"[round {rounds_used}] requesting (effort={effort})")

        n_msgs = _drain_messages_into_history(message_source, history)
        if n_msgs:
            emit(f"[round {rounds_used}] delivered {n_msgs} queued message(s)")

        agents_md_text = load_project_agents_md(tools.root)
        extra_items = None
        stamp = getattr(session, "goal_stamp", None) if session else None
        if delegate_mode:
            extra_items = delegate_mode_input_items()
            if stamp:
                extra_items = extra_items + [goal_workspace_input_item(stamp)]
        elif goal_mode:
            extra_items = goal_mode_input_items(estimate_tokens(history))
            if stamp:
                extra_items = extra_items + [goal_workspace_input_item(stamp)]
        elif surface_leftovers:
            extra_items = infinite_orch_input_items()
        suffix_items = None
        if wrapping_up:
            suffix_items = [
                user_message(format_tool_round_cap_suffix(round_cap, round_cap))
            ]
        request_input = build_request_input(
            history, agents_md_text, extra_items, suffix_items
        )

        payload: dict[str, Any] = {
            "model": MODEL,
            "instructions": system_prompt,
            "input": request_input,
            "tools": tool_schemas,
            "tool_choice": "auto",
            "stream": True,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 1.0,
        }
        payload.update(reasoning_request_fields(effort))

        live = LivePrinter(prefix=log_prefix)
        try:
            parsed = _process_stream(payload, cancel_event, live=live)
        except RuntimeError as e:
            live.end()
            was_cancelled = cancel_event is not None and cancel_event.is_set()
            note = f"[stream aborted: {e}]"
            abort_notes.append(note)
            emit(paint(note, COLOR_ERROR))
            break

        text, round_calls, round_reasoning, round_search, usage, cancelled = parsed
        status = getattr(parsed, "status", "completed") or "completed"
        round_output_items = getattr(parsed, "output_items", None)
        if not round_output_items:
            round_output_items = None
        pulse()

        tokens = parse_round_tokens(usage)
        collected_tokens.append(tokens)
        if usage:
            last_usage = usage
        emit(format_round_tokens(rounds_used, tokens))

        if (text or "").strip() and "model" not in live.channels_seen:
            emit(paint(f"[model] {preview_text(text)}", COLOR_ANSWER))
        if round_reasoning and "think" not in live.channels_seen:
            think_preview = _reasoning_text_of(round_reasoning[0])
            if think_preview:
                emit(paint(f"[think] {preview_text(think_preview)}", COLOR_THINK))

        if cancelled:
            was_cancelled = True
            break

        raw_meta, cleaned_text = parse_harness_meta_block(text or "")
        next_effort = effort_from_round_meta(raw_meta, effort)
        if raw_meta is not None:
            if "reason_next" in raw_meta:
                emit(
                    f"[round {rounds_used}] next effort={next_effort} "
                    f"(reason_next={str(_as_bool(raw_meta['reason_next'])).lower()})"
                )
            else:
                emit(f"[round {rounds_used}] next effort={next_effort}")

        # Commit every round, including the last no-tool round, so thinking
        # is in history for the next request (DeepSeek requires reasoning_text
        # whenever the request carries tools). HARNESS_META is harness
        # control, not conversation: strip it on the message item in place.
        # Arrival order is used when parse_stream_events supplied output_items.
        if status in ("failed", "incomplete"):
            if round_output_items is not None:
                safe_items = [
                    it for it in round_output_items
                    if it.get("type") != "function_call"
                ]
                commit_stream_round(history, output_items=safe_items)
            else:
                commit_stream_round(
                    history,
                    reasoning_items=round_reasoning,
                    web_search_items=round_search,
                    assistant_text=cleaned_text,
                )
            effort = next_effort
            reasoning_items.extend(round_reasoning)
            web_search_items.extend(round_search)
            note = (
                f"[response {status}; truncated tool calls were not dispatched]"
            )
            abort_notes.append(note)
            emit(paint(note, COLOR_ERROR))
            break

        if wrapping_up:
            hit_round_cap = True
            if round_output_items is not None:
                items = [
                    it for it in round_output_items
                    if it.get("type") != "function_call"
                ]
                has_msg = any(_is_assistant_message_item(it) for it in items)
                if (cleaned_text or "").strip() and not has_msg:
                    items.append(assistant_message(cleaned_text))
                commit_stream_round(history, output_items=items)
            else:
                commit_stream_round(
                    history,
                    reasoning_items=round_reasoning,
                    web_search_items=round_search,
                    assistant_text=cleaned_text,
                )
            effort = next_effort
            reasoning_items.extend(round_reasoning)
            web_search_items.extend(round_search)
            if round_calls:
                emit(
                    f"[round {rounds_used}] wrap-up dropped "
                    f"{len(round_calls)} tool call(s)"
                )
            if (text or "").strip():
                no_tool_texts.append(text)
                emit(f"[round {rounds_used}] wrap-up complete")
            else:
                emit(f"[round {rounds_used}] wrap-up empty completion")
            return _finish(cancelled=False)

        if round_output_items is not None:
            items = list(round_output_items)
            has_msg = any(_is_assistant_message_item(it) for it in items)
            if (cleaned_text or "").strip() and not has_msg:
                items.append(assistant_message(cleaned_text))
            commit_stream_round(history, output_items=items)
        else:
            commit_stream_round(
                history,
                reasoning_items=round_reasoning,
                web_search_items=round_search,
                assistant_text=cleaned_text,
                function_calls=round_calls,
            )
        effort = next_effort
        reasoning_items.extend(round_reasoning)
        web_search_items.extend(round_search)
        all_tool_calls.extend(round_calls)

        if round_search:
            emit(f"[round {rounds_used}] web_search x{len(round_search)}")

        if not round_calls:
            emit(f"[round {rounds_used}] complete (no tools)")
            if (text or "").strip():
                no_tool_texts.append(text)
            else:
                emit(f"[round {rounds_used}] empty completion")
            return _finish(cancelled=False)

        names = ", ".join(c.get("name") or "?" for c in round_calls)
        emit(f"[round {rounds_used}] {len(round_calls)} tool call(s): {names}")

        pending_compact: dict[str, str] | None = None
        for call in round_calls:
            raw_args = call.get("arguments", "{}")
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
            if not isinstance(args, dict):
                args = {}
            name = call.get("name", "")
            emit(paint(f"[call {name}] {preview_tool_args(name, args)}", COLOR_TOOL))
            pulse()
            result = dispatch_tool(
                name,
                args,
                tools,
                memory,
                session=session,
                allowed_names=allowed_names,
                skills=getattr(session, "skills", None) if session else None,
            )
            if isinstance(result, str):
                vault = getattr(session, "result_vault", None) if session else None
                result = budget_tool_result(
                    name, result, vault=vault, tools=tools
                )
            emit(paint(
                f"[tool {name}] {preview_tool_result(result, 120)}",
                tool_result_color(result),
            ))
            history.append({
                "type": "function_call_output",
                "call_id": call.get("call_id", ""),
                "output": result,
            })
            if name == GOAL_COMPACT_TOOL_NAME and not str(result).startswith("ERROR:"):
                pending_compact = {
                    "goal": str(args.get("goal", "")),
                    "done": str(args.get("done", "")),
                    "remaining": str(args.get("remaining", "")),
                    "next_step": str(args.get("next", "")),
                }

        if pending_compact is not None:
            new_history = apply_goal_compaction(history, **pending_compact)
            history.clear()
            history.extend(new_history)
            goal_compacted = True
            emit("[goal compact] replaced history with compact record")

        if rounds_used >= round_cap:
            hit_round_cap = True
            wrapping_up = True
            emit(
                f"[round {rounds_used}] tool-round cap {round_cap} reached; "
                "wrapping up (session continues)"
            )
            continue

    return _finish(cancelled=was_cancelled)
