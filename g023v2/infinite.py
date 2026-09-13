"""Infinite orchestration: leftover-driven chain of fresh orchestrations.

After a finished task (/goal or an ordinary prompt), leftover issues and
recommendations can start a new orchestration from a compact handoff.
That chain is infinite orchestration: it continues until leftovers are
empty or the user stops it.

Continue policy:
  auto    start each next orchestration without asking
  prompt  ask before every subsequent orchestration
  N       run N automatic subsequent orchestrations, then ask again

Unattended `--once auto` still brakes at MAX_GOAL_CHAIN. A REPL auto
chain does not hard-stop at that cap; the user is the brake (picker,
burst N, or Ctrl+C). Child/member turns do not surface leftovers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from g023v2.constants import MAX_GOAL_CHAIN, MAX_INFINITE_BURST, MAX_GOAL_TURNS
from g023v2.goal import GoalLeftovers
from g023v2.messages import user_message

INFINITE_ORCH_HEADER = "[INFINITE ORCHESTRATION]"

INFINITE_ORCH_INSTRUCTIONS = """[INFINITE ORCHESTRATION]
After this task is done, surface leftover issues and recommendations (bugs, polish, robustness) even if the original request is met. Do not silently expand this turn to fix them — name them here so a later orchestration can take them. If this was not an implementation or fix task, emit empty lists. Emit this block before HARNESS_META:

[GOAL LEFTOVERS]
issues:
- ...
recommendations:
- ...
[END GOAL LEFTOVERS]
"""

INFINITE_CONTINUE_AUTO = "auto"
INFINITE_CONTINUE_PROMPT = "prompt"


@dataclass
class InfinitePolicy:
    """How the leftover chain starts the next orchestration.

    `mode` is `auto`, `prompt`, or `burst`. `remaining` is auto steps
    left in a burst (ignored for auto/prompt).
    """

    mode: str
    remaining: int = 0

    def should_prompt(self) -> bool:
        if self.mode == INFINITE_CONTINUE_AUTO:
            return False
        if self.mode == INFINITE_CONTINUE_PROMPT:
            return True
        return self.remaining <= 0

    def consume_auto_step(self) -> None:
        if self.mode == "burst" and self.remaining > 0:
            self.remaining -= 1

    def wire_value(self) -> str:
        if self.mode == "burst":
            return str(self.remaining if self.remaining > 0 else 1)
        return self.mode

    def describe(self) -> str:
        if self.mode == "burst":
            return f"burst {self.remaining} then prompt"
        return self.mode


def clamp_infinite_burst(n: int) -> int:
    return max(1, min(int(n), MAX_INFINITE_BURST))


def parse_infinite_policy(value: str | None) -> InfinitePolicy | None:
    """Parse `auto`, `prompt`, or a positive integer N. None if unset/empty."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in (INFINITE_CONTINUE_AUTO, "a"):
        return InfinitePolicy(mode=INFINITE_CONTINUE_AUTO)
    if s in (INFINITE_CONTINUE_PROMPT, "p"):
        return InfinitePolicy(mode=INFINITE_CONTINUE_PROMPT)
    if s.isdigit():
        n = int(s)
        if n < 1:
            raise ValueError(
                "infinite orchestration burst N must be a positive integer "
                f"(1-{MAX_INFINITE_BURST})"
            )
        return InfinitePolicy(mode="burst", remaining=clamp_infinite_burst(n))
    raise ValueError(
        "infinite orchestration mode must be auto, prompt, or a "
        f"positive integer N (1-{MAX_INFINITE_BURST})"
    )


def parse_infinite_arg(value: str) -> str:
    """argparse type: canonical `auto` / `prompt` / digit string."""
    policy = parse_infinite_policy(value)
    if policy is None:
        raise ValueError("infinite orchestration mode is empty")
    if policy.mode == "burst":
        return str(policy.remaining)
    return policy.mode


def interpret_infinite_reply(text: str) -> InfinitePolicy | None:
    """REPL picker line. None means stop. Unknown input is stop, not error."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if s in (INFINITE_CONTINUE_AUTO, "a"):
        return InfinitePolicy(mode=INFINITE_CONTINUE_AUTO)
    if s in (INFINITE_CONTINUE_PROMPT, "p", "y", "yes"):
        return InfinitePolicy(mode=INFINITE_CONTINUE_PROMPT)
    if s.isdigit():
        n = int(s)
        if n < 1:
            return None
        return InfinitePolicy(mode="burst", remaining=clamp_infinite_burst(n))
    if s in ("n", "no", "stop", "q", "quit"):
        return None
    return None


def policy_from_session_fields(
    mode: str | None,
    burst_left: int,
) -> InfinitePolicy:
    """Live policy from session continue string plus remaining burst."""
    if mode is not None:
        try:
            parsed = parse_infinite_policy(mode)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.mode == "burst":
                left = burst_left if burst_left > 0 else 0
                return InfinitePolicy(mode="burst", remaining=left)
            return parsed
    return InfinitePolicy(mode=INFINITE_CONTINUE_PROMPT)


def apply_policy_to_fields(policy: InfinitePolicy) -> tuple[str, int]:
    """Return `(goal_continue_mode, infinite_burst_left)` for a Session."""
    if policy.mode == "burst":
        n = clamp_infinite_burst(policy.remaining)
        return str(n), n
    return policy.mode, 0


def should_prompt_infinite(
    mode: str | None,
    burst_left: int,
    *,
    fallback_prompt: bool,
) -> bool:
    """Whether to ask before starting the next orchestration.

    Unset mode uses `fallback_prompt` (REPL with a confirm callback → True
    so the first leftover can pick auto / prompt / N / stop).
    """
    if mode is None or str(mode).strip() == "":
        return fallback_prompt
    policy = policy_from_session_fields(mode, burst_left)
    return policy.should_prompt()


def format_infinite_next_spec(
    goal: str,
    leftovers: GoalLeftovers,
    *,
    via_goal: bool = False,
) -> str:
    """User prompt for the next leftover orchestration."""
    origin = "previous /goal run" if via_goal else "previous task"
    label = f"Original goal: {goal}" if via_goal else f"Original task: {goal}"
    lines = [
        "Finish leftover issues and recommendations from the "
        f"{origin}. {label}",
    ]
    for item in leftovers.items():
        lines.append(f"- {item}")
    if leftovers.empty():
        lines.append("- (none)")
    return "\n".join(lines)


def infinite_orch_input_items() -> list[dict[str, Any]]:
    """Stable post-prefix extra for ordinary leftover surfacing. Not in history."""
    return [user_message(INFINITE_ORCH_INSTRUCTIONS)]


def format_infinite_picker_prompt() -> str:
    return (
        "Infinite orchestration: [auto] until empty, [prompt] before each, "
        "[N] auto then ask, [n] stop: "
    )


def format_infinite_yn_prompt(next_index: int) -> str:
    return f"Start orchestration {next_index} to finish leftovers? [y/N] "


def format_infinite_help() -> str:
    """Informative help for infinite orchestration (`/help` and `/infinite`)."""
    return (
        "Infinite orchestration\n"
        "  After a prompted task finishes, leftover issues and\n"
        "  recommendations can start a fresh next orchestration (compact\n"
        "  handoff). That leftover-driven chain is infinite orchestration:\n"
        "  it continues until leftovers are empty or you stop it. It is\n"
        "  available on /goal and on ordinary prompts (not /goal).\n"
        "\n"
        "  What happens\n"
        "    1. The current orchestration finishes and surfaces leftovers.\n"
        "    2. You choose how the next orchestrations start (or a flag /\n"
        "       /infinite policy already chose).\n"
        "    3. History is replaced with a compact handoff. The next\n"
        "       orchestration is a fresh conversation whose only carried\n"
        "       transcript is that handoff plus the frozen system prompt.\n"
        "    4. Repeat until leftovers are empty, you stop, or an\n"
        "       unattended run hits the chain cap.\n"
        "\n"
        "  Continue policy\n"
        "    auto     start each next orchestration without asking, until\n"
        "             leftovers are empty. Unattended --once still brakes\n"
        f"             at the chain cap ({MAX_GOAL_CHAIN}). In the REPL,\n"
        "             auto does not hard-stop at that cap; Ctrl+C cancels.\n"
        "    prompt   ask before every subsequent orchestration.\n"
        "    N        run N automatic subsequent orchestrations, then ask\n"
        "             again (pick another N, auto, prompt, or stop).\n"
        f"             N is 1-{MAX_INFINITE_BURST}.\n"
        "    n/stop   do not start the next orchestration.\n"
        "\n"
        "  First leftover in the REPL (no flag): the picker above.\n"
        "  --infinite auto|prompt|N  (also --goal-continue auto|prompt|N)\n"
        "  /infinite                 this help\n"
        "  /infinite auto|prompt|N   set the session policy for later chains\n"
        "\n"
        "  /goal still has an inner evaluator (stated objectives) before\n"
        f"  leftovers are chained. Inner evaluator turns cap at {MAX_GOAL_TURNS}.\n"
        "  Ordinary prompts have no evaluator: leftovers alone drive the\n"
        "  chain. Child/member turns do not surface leftovers. Cap-stop or\n"
        "  burst-stop with leftovers still open is incomplete, not success."
    )
