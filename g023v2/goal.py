"""Goal-mode compaction, leftover handoff, and completion evaluator.

Used only while a `/goal` run is active. Ordinary turns keep the automatic
oldest-span compaction in orchestration.compact_history. The outer leftover
chain (infinite orchestration: leftover issues → compact handoff → next
orchestration) lives here as parse/handoff/decide helpers; Session runs
the loop for /goal and for ordinary prompts. See g023v2.infinite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from g023v2.constants import SOFT_CONTEXT_LIMIT
from g023v2.messages import user_message
from g023v2.plan import CHILD_RESULTS_HEADER

GOAL_COMPACT_HEADER = "[GOAL COMPACT]"
GOAL_COMPACT_FOOTER = "[END GOAL COMPACT]"
CONTEXT_HINT_HEADER = "[CONTEXT LENGTH HINT]"
GOAL_MODE_HEADER = "[GOAL MODE]"
LEFTOVERS_HEADER = "[GOAL LEFTOVERS]"
LEFTOVERS_FOOTER = "[END GOAL LEFTOVERS]"
HANDOFF_HEADER = "[GOAL HANDOFF]"
HANDOFF_FOOTER = "[END GOAL HANDOFF]"
GOAL_WORKSPACE_HEADER = "[GOAL WORKSPACE]"
GOAL_CONTINUE_HEADER = "[GOAL CONTINUE]"

GOAL_MODE_INSTRUCTIONS = """[GOAL MODE]
You are executing a /goal run. compact_goal_conversation is listed in this request's schema; call it only during this run. Seeing a tool name is not permission to run a tool this mode must not execute.

When to compact:
- Call compact_goal_conversation only at a safe point: no in-flight file edit, no unanswered tool ERROR, and every fact you still need is recorded in the four compact fields.
- Call it only when the conversation is getting long.
- Do not compact a short conversation.

What compact does:
- Replaces the working history with one compact record of: goal, what we've done, what is to be done, and what is to be done next.
- The next request is the frozen system prompt plus that compact text. Pre-compact turns, tool calls, and reasoning items are not sent.

Workspace:
- A [GOAL WORKSPACE] item names this run's stamp and scratch folder (.g023/scratch/<stamp>/). Use it for drafts, notes, and intermediate files. Write final deliverables to the paths the goal requires. Do not write into .g023/logs, .g023/usage, .g023/backups, or .g023/prompts; the harness owns those.

Finish:
- Keep working until the stated goal and every objective are done. An evaluator checks them before this /goal run is reported complete. If the evaluator lists missing objectives, continue; do not claim success.
- A turn may wrap up after a finite number of tool rounds. That is not a cancellation and not the end of the /goal. The next inner turn (and the leftover chain) can call tools again. Continue remaining work; do not stop because a wrap-up happened.
- When the stated goal and objectives are done, surface leftover issues and recommendations (bugs, polish, robustness) even if the original bullets are met. Emit this block before HARNESS_META (empty lists if none remain):

[GOAL LEFTOVERS]
issues:
- ...
recommendations:
- ...
[END GOAL LEFTOVERS]
"""

_BULLET_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+(.*)$")
_GOAL_PREFIX_RE = re.compile(r"^/goal\b", re.IGNORECASE)


def parse_goal_spec(text: str) -> tuple[str, list[str]]:
    """Split a `/goal` body into a goal statement and objective list.

    Bullet / numbered lines are objectives. Remaining lines join into the goal.
    If every line is a bullet, the first bullet is also the goal statement.
    """
    raw = (text or "").strip()
    raw = _GOAL_PREFIX_RE.sub("", raw, count=1).strip()
    if not raw:
        return "", []
    objectives: list[str] = []
    goal_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _BULLET_RE.match(stripped)
        if match:
            obj = match.group(1).strip()
            if obj:
                objectives.append(obj)
        else:
            goal_lines.append(stripped)
    goal = " ".join(goal_lines).strip()
    if not goal and objectives:
        goal = objectives[0]
    return goal, objectives


def format_goal_opener(goal: str, objectives: Sequence[str]) -> str:
    lines = ["[GOAL]", f"goal: {goal}"]
    if objectives:
        lines.append("objectives:")
        lines.extend(f"- {o}" for o in objectives)
    else:
        lines.append("objectives: (the goal statement is the sole objective)")
    lines.append(
        "Work until every objective is done. Call compact_goal_conversation "
        "only at a safe point and only when the conversation is getting long."
    )
    return "\n".join(lines)


def format_goal_compact_text(
    *,
    goal: str,
    done: str,
    remaining: str,
    next_step: str,
) -> str:
    """Single compact blob with the four tracked fields."""
    return (
        f"{GOAL_COMPACT_HEADER}\n"
        f"goal: {goal}\n"
        f"what we've done: {done}\n"
        f"what is to be done: {remaining}\n"
        f"what is to be done next: {next_step}\n"
        f"{GOAL_COMPACT_FOOTER}"
    )


def apply_goal_compaction(
    history: list[dict[str, Any]],
    *,
    goal: str,
    done: str,
    remaining: str,
    next_step: str,
) -> list[dict[str, Any]]:
    """Transform (goal state + history) into one compact user item.

    Drops pre-compact turns, tool calls, and reasoning items. Does not mutate
    `history`; the caller replaces the working list with the return value.
    """
    del history  # explicit: prior items are not carried forward
    return [user_message(format_goal_compact_text(
        goal=goal, done=done, remaining=remaining, next_step=next_step
    ))]


def format_context_length_hint(
    estimated_tokens: int,
    soft_limit: int = SOFT_CONTEXT_LIMIT,
) -> str:
    """Per-request hint so the model can judge whether the conversation is long."""
    limit = soft_limit if soft_limit else 1
    fraction = estimated_tokens / limit
    return (
        f"{CONTEXT_HINT_HEADER}\n"
        f"Estimated conversation size: {estimated_tokens} tokens "
        f"({fraction:.1%} of the {limit}-token soft limit)."
    )


def goal_mode_input_items(
    estimated_tokens: int = 0,
    soft_limit: int = SOFT_CONTEXT_LIMIT,
) -> list[dict[str, Any]]:
    """Stable post-prefix input items for a /goal request. Not stored in history.

    The numeric [CONTEXT LENGTH HINT] is not prepended: it would change every
    tool round and bust the cacheable prefix of growing history. Keep only
    the frozen [GOAL MODE] copy. `estimated_tokens` / `soft_limit` are unused
    (kept so callers do not break).
    """
    del estimated_tokens, soft_limit
    return [user_message(GOAL_MODE_INSTRUCTIONS)]


def _item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (
                "input_text",
                "output_text",
                "text",
            ):
                parts.append(part.get("text") or "")
        return "\n".join(parts)
    output = item.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for part in output:
            if isinstance(part, dict) and part.get("type") in (
                "input_text",
                "output_text",
                "text",
            ):
                parts.append(part.get("text") or "")
        return "\n".join(parts)
    return ""


def collect_goal_evidence(
    history: list[dict[str, Any]],
    last_text: str = "",
    extra: str = "",
) -> str:
    """Text the evaluator may search for completed objectives.

    Includes tool results, compact records (minus done/remaining/next), and
    `[CHILD RESULTS]` blocks from joined delegates. Excludes assistant prose
    (recitation of the objective list), the `/goal` opener, and evaluator
    feedback. `last_text` is ignored (assistant recitation). `extra` is
    omitted-span vault text (not sent to the model).
    """
    chunks: list[str] = []
    _ = last_text
    for item in history:
        role = item.get("role")
        typ = item.get("type")
        text = _item_text(item)
        if not text:
            continue
        if typ == "function_call_output":
            chunks.append(text)
        elif role == "user" and GOAL_COMPACT_HEADER in text:
            chunks.append(text)
        elif role == "user" and CHILD_RESULTS_HEADER in text:
            chunks.append(text)
    if extra:
        chunks.append(extra)
    return "\n".join(chunks)


def _evidence_for_completion(evidence: str) -> str:
    """Drop compact remaining/next lines so leftover work is not counted done."""
    if GOAL_COMPACT_HEADER not in (evidence or ""):
        return evidence or ""
    kept: list[str] = []
    skip_prefixes = (
        "what we've done:",
        "what is to be done:",
        "what is to be done next:",
    )
    for line in evidence.splitlines():
        lower = line.strip().lower()
        if any(lower.startswith(p) for p in skip_prefixes):
            continue
        kept.append(line)
    return "\n".join(kept)


@dataclass
class GoalEvalResult:
    complete: bool
    missing: list[str] = field(default_factory=list)
    goal: str = ""

    def format_visible_missing(self) -> str:
        if self.complete:
            return "(none)"
        if not self.missing:
            return "(incomplete, no named objectives)"
        return "\n".join(f"- {m}" for m in self.missing)


@dataclass
class GoalLeftovers:
    """Issues and recommendations surfaced after a finished /goal orchestration."""

    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not self.issues and not self.recommendations

    def items(self) -> list[str]:
        out: list[str] = []
        out.extend(f"issue: {i}" for i in self.issues)
        out.extend(f"recommendation: {r}" for r in self.recommendations)
        return out


@dataclass
class GoalRunResult:
    complete: bool
    text: str
    missing: list[str] = field(default_factory=list)
    goal: str = ""
    objectives: list[str] = field(default_factory=list)
    leftovers: GoalLeftovers = field(default_factory=GoalLeftovers)
    chain_reason: str = ""
    orchestrations: int = 1
    stamp: str = ""
    log_rel: str = ""
    usage_rel: str = ""


def format_goal_workspace_text(stamp: str) -> str:
    """Stable-for-this-goal extra naming scratch and backup folders.

    Not stored in history. The live stamp must not go in SYSTEM_PROMPT.
    """
    scratch = f".g023/scratch/{stamp}/"
    backups = f".g023/backups/{stamp}/"
    return (
        f"{GOAL_WORKSPACE_HEADER}\n"
        f"stamp: {stamp}\n"
        f"scratch: {scratch}\n"
        f"Use {scratch} as a working folder for drafts, notes, and intermediate "
        f"files for this goal. Write final deliverables to the paths the goal "
        f"requires, not only to scratch.\n"
        f"File-tool changes of existing files are backed up under {backups}. "
        f"Do not write into .g023/logs, .g023/usage, .g023/backups, or "
        f".g023/prompts yourself.\n"
    )


def goal_workspace_input_item(stamp: str) -> dict[str, Any]:
    return user_message(format_goal_workspace_text(stamp))


def evaluate_goal_completion(
    goal: str,
    objectives: Sequence[str],
    evidence: str,
) -> GoalEvalResult:
    """Return complete only when the goal and every objective are evidenced.

    An objective is met when its text appears in `evidence` (case-insensitive).
    Unmet objectives are named in `missing`. An empty goal is never complete.
    With no explicit objectives, the goal statement itself must appear in evidence.
    """
    goal_s = (goal or "").strip()
    evidence_l = _evidence_for_completion(evidence or "").lower()
    missing: list[str] = []
    objs = [o.strip() for o in objectives if (o or "").strip()]

    if not goal_s:
        missing.append("goal is unstated")

    if objs:
        for obj in objs:
            if obj.lower() not in evidence_l:
                missing.append(obj)
    elif goal_s and goal_s.lower() not in evidence_l:
        missing.append(goal_s)

    return GoalEvalResult(
        complete=not missing,
        missing=missing,
        goal=goal_s,
    )


def format_evaluator_feedback(result: GoalEvalResult) -> str:
    lines = [
        "[GOAL EVALUATOR] incomplete. The /goal run is not finished.",
        "Missing objectives:",
        result.format_visible_missing(),
        "Continue until every objective is done. Do not claim the goal is complete.",
    ]
    return "\n".join(lines)


def format_cap_continue_feedback(result: GoalEvalResult) -> str:
    """Next inner /goal turn after a tool-round cap. Tools are allowed again."""
    lines = [
        GOAL_CONTINUE_HEADER,
        "The previous inner turn reached the tool-round cap. "
        "That is not a cancellation and the /goal is not finished.",
        "Tools are available again this turn. Call them. Continue remaining work.",
        "",
        format_evaluator_feedback(result),
    ]
    return "\n".join(lines)


_LEFTOVER_BLOCK_RE = re.compile(
    re.escape(LEFTOVERS_HEADER) + r"(.*?)" + re.escape(LEFTOVERS_FOOTER),
    re.DOTALL | re.IGNORECASE,
)


def parse_goal_leftovers(text: str) -> GoalLeftovers:
    """Parse a [GOAL LEFTOVERS] block from finished-turn text.

    Missing block → empty leftovers (chain stop). An explicit empty block is
    also empty. Bullet lines under `issues:` / `recommendations:` are kept.
    """
    issues: list[str] = []
    recs: list[str] = []
    match = _LEFTOVER_BLOCK_RE.search(text or "")
    if not match:
        return GoalLeftovers()
    section = "issues"
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower().rstrip(":")
        if lower in ("issues", "issue"):
            section = "issues"
            continue
        if lower in ("recommendations", "recommendation"):
            section = "recommendations"
            continue
        match_b = _BULLET_RE.match(stripped)
        if not match_b:
            continue
        item = match_b.group(1).strip()
        if not item:
            continue
        if section == "issues":
            issues.append(item)
        else:
            recs.append(item)
    return GoalLeftovers(issues=issues, recommendations=recs)


def leftovers_from_turn(
    history: list[dict[str, Any]],
    last_text: str = "",
) -> GoalLeftovers:
    """Leftovers from the finishing message, else the newest history block."""
    if LEFTOVERS_HEADER in (last_text or ""):
        return parse_goal_leftovers(last_text)
    for item in reversed(history):
        text = _item_text(item)
        if LEFTOVERS_HEADER in text:
            return parse_goal_leftovers(text)
    return GoalLeftovers()


def format_leftovers_visible(leftovers: GoalLeftovers) -> str:
    if leftovers.empty():
        return "[leftovers] (none)"
    lines = ["[leftovers]"]
    if leftovers.issues:
        lines.append("issues:")
        lines.extend(f"- {i}" for i in leftovers.issues)
    if leftovers.recommendations:
        lines.append("recommendations:")
        lines.extend(f"- {r}" for r in leftovers.recommendations)
    return "\n".join(lines)


def format_goal_handoff(
    *,
    goal: str,
    done: str,
    leftovers: GoalLeftovers,
    next_step: str,
) -> str:
    """Compact transcript passed to the next orchestration. Fresh conversation."""
    lines = [
        HANDOFF_HEADER,
        f"goal: {goal}",
        f"what we've done: {done}",
        "remaining issues:",
    ]
    if leftovers.issues:
        lines.extend(f"- {i}" for i in leftovers.issues)
    else:
        lines.append("(none)")
    lines.append("remaining recommendations:")
    if leftovers.recommendations:
        lines.extend(f"- {r}" for r in leftovers.recommendations)
    else:
        lines.append("(none)")
    lines.append(f"next step: {next_step}")
    lines.append(HANDOFF_FOOTER)
    return "\n".join(lines)


def apply_goal_handoff(
    history: list[dict[str, Any]],
    *,
    goal: str,
    done: str,
    leftovers: GoalLeftovers,
    next_step: str,
) -> list[dict[str, Any]]:
    """Replace working history with one compact handoff user item.

    Does not mutate `history`; the caller replaces the working list.
    """
    del history
    return [user_message(format_goal_handoff(
        goal=goal, done=done, leftovers=leftovers, next_step=next_step
    ))]


def format_chain_next_spec(goal: str, leftovers: GoalLeftovers) -> str:
    """`/goal` body for the next leftover orchestration."""
    lines = [
        "Finish leftover issues and recommendations from the previous /goal run. "
        f"Original goal: {goal}",
    ]
    for item in leftovers.items():
        lines.append(f"- {item}")
    if leftovers.empty():
        lines.append("- (none)")
    return "\n".join(lines)


def decide_chain_continue(
    *,
    leftovers: GoalLeftovers,
    chain_index: int,
    max_chain: int,
    inner_complete: bool,
    cancelled: bool = False,
) -> tuple[bool, str]:
    """Whether to start the next orchestration, and why we would stop.

    `chain_index` is 0-based (the orchestration that just finished).
    Cap-stop with leftovers still open is `"cap"`, not success.
    Unattended infinite orchestration uses this cap; a REPL auto chain
    passes a large max_chain so the user is the brake.
    """
    if cancelled:
        return False, "cancelled"
    if not inner_complete:
        return False, "incomplete"
    if leftovers.empty():
        return False, "satisfied"
    if chain_index + 1 >= max_chain:
        return False, "cap"
    return True, "continue"


def summarize_goal_done(goal: str, objectives: Sequence[str], last_text: str) -> str:
    """Short `what we've done` line for the outer handoff."""
    bits = [goal.strip()] if (goal or "").strip() else []
    bits.extend(o.strip() for o in objectives if (o or "").strip())
    summary = "; ".join(bits) if bits else "completed stated objectives"
    extra = " ".join((last_text or "").split())
    if extra:
        extra = extra[:240]
        return f"{summary}. last: {extra}"
    return summary
