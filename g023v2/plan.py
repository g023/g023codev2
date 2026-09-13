"""Work plan, delegate extras, and the cheap-vs-bulky cost estimate.

Parent-child sessions keep a small todo board so the orchestrator can
fan work out to short-history children. Cached parent input is cheap
(Example 1); children still win when dumps would inflate parent thinking
(output is the expensive token). Numeric limits live in constants.py.
"""

from __future__ import annotations

import threading
from typing import Any

from g023v2.constants import (
    CHARS_PER_TOKEN,
    CHILD_RESULT_MAX_CHARS,
    DEFAULT_CHILD_EFFORT,
    MAX_PLAN_ITEMS,
    ROLE_DEFAULT_EFFORT,
)
from g023v2.messages import user_message

WORKER_ROLES = ("research", "explore", "implement", "verify")
LEAD_ROLE = "lead"
ALL_ROLES = WORKER_ROLES + (LEAD_ROLE,)

DELEGATE_MODE_HEADER = "[DELEGATE MODE]"
CHILD_RESULTS_HEADER = "[CHILD RESULTS]"
WORK_CONTINUE_HEADER = "[WORK CONTINUE]"
PLAN_PROGRESS_HEADER = "[PLAN]"

# Stable extra for every child/member. Identical bytes so sibling children
# share a cache unit after instructions+tools. No names, stamps, or tasks.
DELEGATE_MODE_INSTRUCTIONS = """[DELEGATE MODE]
You are a delegate completing one TASK. Do that TASK. Do not take over the user's whole request.

Return a compact report: what you found or changed (paths), the outcome, and leftover risks. Do not paste raw file bodies, full grep dumps, or fetched HTML back to the parent. Write bulky notes with memory_write or under .g023/scratch/ when a [GOAL WORKSPACE] item is present.

If ROLE is lead, you may plan_work and spawn workers (not leads), then join_children before you finish. If ROLE is not lead, do the work yourself; spawn_child will ERROR.

fresh=false is forbidden. Do not claim the parent goal is done; report only this TASK.
"""

LEAD_TOOL_NAMES = frozenset({
    "plan_work",
    "spawn_child",
    "join_children",
    "get_child_result",
    "list_children",
    "send_message",
    "stop_child",
})

# V4.1 Flash off-peak USD / million tokens (public rates, Sept 2026).
# Local estimate only; never sent to the API and not a bill.
FLASH_OFFPEAK_HIT_PER_M = 0.003
FLASH_OFFPEAK_MISS_PER_M = 0.15
FLASH_OFFPEAK_OUTPUT_PER_M = 0.60


def normalize_role(role: str | None) -> str:
    text = (role or "").strip().lower()
    if text in ALL_ROLES:
        return text
    return ""


def effort_for_role(role: str | None, effort: int | None = None) -> int:
    if effort is not None:
        return int(effort)
    key = normalize_role(role)
    if key and key in ROLE_DEFAULT_EFFORT:
        return int(ROLE_DEFAULT_EFFORT[key])
    return int(DEFAULT_CHILD_EFFORT)


def compact_delegate_result(
    text: str,
    max_chars: int = CHILD_RESULT_MAX_CHARS,
) -> str:
    body = text if text is not None else ""
    if len(body) <= max_chars:
        return body
    omitted = len(body) - max_chars
    return (
        body[:max_chars]
        + f"\n… [truncated {omitted} chars; full text is not in the parent. "
        "Re-read files or memory_read instead of asking the child to dump again.]"
    )


def delegate_mode_input_items() -> list[dict[str, Any]]:
    """Stable post-prefix extra for child/member turns. Not stored in history."""
    return [user_message(DELEGATE_MODE_INSTRUCTIONS)]


def estimate_bloat_cost_usd(
    *,
    bulky_chars: int,
    parent_followup_rounds: int,
    child_result_chars: int = CHILD_RESULT_MAX_CHARS,
    child_output_tokens: int = 800,
    chars_per_token: float = CHARS_PER_TOKEN,
    attention_tax: float = 0.01,
) -> tuple[float, float]:
    """(keep_in_parent_usd, delegate_usd) at Flash off-peak.

    Shared cached prefix is ignored (same both paths). Parent path is
    Example 1: the dump is a cache miss once, then hits. `attention_tax`
    (default 0.01) adds parent thinking tokens proportional to dump size
    on later rounds (context rot). That is why bulky dumps still belong
    in a child even though cached input is cheap. Delegate path pays the
    dump miss once inside the child, then the compact result on the
    parent (miss once, then hits), plus child output.
    """
    cpt = chars_per_token if chars_per_token else 3.25
    bulky_tok = max(0.0, bulky_chars) / cpt
    result_tok = max(0.0, child_result_chars) / cpt
    rounds = max(0, int(parent_followup_rounds))
    miss = FLASH_OFFPEAK_MISS_PER_M / 1_000_000.0
    hit = FLASH_OFFPEAK_HIT_PER_M / 1_000_000.0
    out = FLASH_OFFPEAK_OUTPUT_PER_M / 1_000_000.0
    tax = max(0.0, float(attention_tax))

    def _carry(tokens: float) -> float:
        if rounds <= 0:
            return 0.0
        return tokens * miss + tokens * hit * max(0, rounds - 1)

    parent = _carry(bulky_tok) + bulky_tok * tax * rounds * out
    delegate = (
        bulky_tok * miss
        + _carry(result_tok)
        + max(0, int(child_output_tokens)) * out
    )
    return parent, delegate


def should_delegate_for_bloat(
    bulky_chars: int,
    parent_followup_rounds: int,
    **kwargs: Any,
) -> bool:
    """True when a short-history child is cheaper than parking the dump here."""
    parent, delegate = estimate_bloat_cost_usd(
        bulky_chars=bulky_chars,
        parent_followup_rounds=parent_followup_rounds,
        **kwargs,
    )
    return delegate < parent


def format_child_results_block(rows: list[str]) -> str:
    if not rows:
        return ""
    return CHILD_RESULTS_HEADER + "\n" + "\n".join(rows)


def normalize_plan_files(files: list[str] | None) -> frozenset[str]:
    """Project-relative paths used for spawn occupancy. Empty means unspecified."""
    out: set[str] = set()
    for raw in files or []:
        text = str(raw).strip().replace("\\", "/")
        if not text:
            continue
        while text.startswith("./"):
            text = text[2:]
        text = text.lstrip("/")
        if text:
            out.add(text)
    return frozenset(out)


def plan_files_overlap(a: list[str] | None, b: list[str] | None) -> bool:
    """True only when both sides declared files and they share a path."""
    left = normalize_plan_files(a)
    right = normalize_plan_files(b)
    if not left or not right:
        return False
    return not left.isdisjoint(right)


def format_work_continue(
    *,
    child_block: str,
    plan_lines: list[str],
    still_running: list[str],
) -> str:
    lines = [
        WORK_CONTINUE_HEADER,
        "Open work remains. The user task is not finished.",
    ]
    if child_block:
        lines.append("")
        lines.append(child_block)
    if plan_lines:
        lines.append("")
        lines.append("plan:")
        lines.extend(plan_lines)
    if still_running:
        lines.append("")
        lines.append("still running: " + ", ".join(still_running))
    lines.append("")
    lines.append(
        "Join remaining children, integrate their compact results, and "
        "finish every plan item. Do not stop at a plan or a partial edit."
    )
    return "\n".join(lines)


class WorkPlan:
    """Small in-process todo board for parent-child (not teams) sessions."""

    def __init__(self, max_items: int = MAX_PLAN_ITEMS):
        self.max_items = max_items
        self.items: dict[str, dict[str, Any]] = {}
        self._n = 0
        self.lock = threading.Lock()

    def add(
        self,
        title: str,
        details: str = "",
        role: str = "",
        effort: int | None = None,
        depends_on: list[str] | None = None,
        files: list[str] | None = None,
    ) -> str:
        title = (title or "").strip()
        if not title:
            raise RuntimeError("plan item title is empty")
        with self.lock:
            active = sum(
                1
                for it in self.items.values()
                if it["status"] != "completed"
            )
            if active >= self.max_items:
                raise RuntimeError(f"work plan is full ({self.max_items} active items)")
            self._n += 1
            item_id = f"p{self._n}"
            self.items[item_id] = {
                "id": item_id,
                "title": title,
                "details": details or "",
                "role": normalize_role(role),
                "effort": effort_for_role(role, effort),
                "depends_on": list(depends_on or []),
                "files": list(files or []),
                "status": "pending",
                "owner": None,
                "result": None,
            }
            return item_id

    def update(
        self,
        item_id: str,
        *,
        title: str | None = None,
        details: str | None = None,
        role: str | None = None,
        effort: int | None = None,
        depends_on: list[str] | None = None,
        files: list[str] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            item = self._require(item_id)
            if title is not None:
                title = title.strip()
                if not title:
                    raise RuntimeError("plan item title is empty")
                item["title"] = title
            if details is not None:
                item["details"] = details
            if role is not None:
                item["role"] = normalize_role(role)
            if effort is not None:
                item["effort"] = int(effort)
            if depends_on is not None:
                item["depends_on"] = list(depends_on)
            if files is not None:
                item["files"] = list(files)
            return dict(item)

    def complete(self, item_id: str, result: str = "") -> dict[str, Any]:
        with self.lock:
            item = self._require(item_id)
            item["status"] = "completed"
            item["result"] = result or item.get("result") or ""
            return dict(item)

    def claim(self, item_id: str, owner: str) -> dict[str, Any]:
        with self.lock:
            item = self._require(item_id)
            if item["status"] != "pending":
                raise RuntimeError(f"{item_id} is {item['status']}, not pending")
            for dep in item["depends_on"]:
                dep_item = self.items.get(dep)
                if not dep_item or dep_item["status"] != "completed":
                    raise RuntimeError(f"{item_id} depends on {dep} which is not complete")
            item["status"] = "running"
            item["owner"] = owner
            return dict(item)

    def release(self, item_id: str) -> dict[str, Any]:
        with self.lock:
            item = self._require(item_id)
            if item["status"] == "completed":
                return dict(item)
            item["status"] = "pending"
            item["owner"] = None
            return dict(item)

    def record_owner_result(self, owner: str, result: str) -> list[str]:
        """Mark running items owned by `owner` complete. Returns their ids."""
        done: list[str] = []
        with self.lock:
            for item in self.items.values():
                if item["owner"] == owner and item["status"] == "running":
                    item["status"] = "completed"
                    item["result"] = result
                    done.append(item["id"])
        return done

    def ready_pending(self) -> list[dict[str, Any]]:
        with self.lock:
            out: list[dict[str, Any]] = []
            for item in self.items.values():
                if item["status"] != "pending":
                    continue
                ok = True
                for dep in item["depends_on"]:
                    dep_item = self.items.get(dep)
                    if not dep_item or dep_item["status"] != "completed":
                        ok = False
                        break
                if ok:
                    out.append(dict(item))
            return out

    def incomplete(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                dict(it)
                for it in self.items.values()
                if it["status"] != "completed"
            ]

    def get(self, item_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self.items.get(item_id)
            return dict(item) if item else None

    def format_lines(self, items: list[dict[str, Any]] | None = None) -> list[str]:
        rows = items if items is not None else self.list_items()
        if not rows:
            return []
        lines = []
        for it in rows:
            owner = it.get("owner") or "-"
            role = it.get("role") or "worker"
            lines.append(
                f"- {it['id']} [{it['status']}] role={role} owner={owner}  {it['title']}"
            )
        return lines

    def list_items(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(it) for it in self.items.values()]

    def format_list(self) -> str:
        rows = self.list_items()
        if not rows:
            return "(work plan empty)"
        return "\n".join(self.format_lines(rows))

    def format_progress(self) -> str:
        """Short snapshot after join/spawn so the parent does not re-complete."""
        lines = [PLAN_PROGRESS_HEADER]
        incomplete = self.incomplete()
        if not incomplete:
            lines.append("plan empty")
            return "\n".join(lines)
        ready = self.ready_pending()
        if ready:
            lines.append(
                "ready to spawn: " + ", ".join(it["id"] for it in ready)
            )
        open_bits = [f"{it['id']}={it['status']}" for it in incomplete]
        lines.append("open: " + ", ".join(open_bits))
        return "\n".join(lines)

    def occupied_files(self) -> frozenset[str]:
        """Files claimed by currently running plan items."""
        occupied: set[str] = set()
        with self.lock:
            for item in self.items.values():
                if item["status"] != "running":
                    continue
                occupied.update(normalize_plan_files(item.get("files")))
        return frozenset(occupied)

    def _require(self, item_id: str) -> dict[str, Any]:
        item = self.items.get(item_id)
        if not item:
            raise RuntimeError(f"unknown plan item: {item_id}")
        return item

    def task_text(self, item: dict[str, Any]) -> str:
        parts = [item["title"]]
        details = (item.get("details") or "").strip()
        if details:
            parts.append(details)
        files = item.get("files") or []
        if files:
            parts.append("Files in scope:\n" + "\n".join(f"- {f}" for f in files))
        return "\n\n".join(parts)
