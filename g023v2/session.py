"""Top-level session orchestrator."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from g023v2.agents_md import load_project_agents_md
from g023v2.context_budget import ResultVault
from g023v2.constants import (
    CHARS_PER_TOKEN,
    DEFAULT_CHILD_EFFORT,
    DEFAULT_EFFORT,
    GET_CHILD_RESULT_TIMEOUT,
    JOIN_CHILDREN_TIMEOUT,
    MAX_CHILD_DEPTH,
    MAX_CONCURRENT_CHILDREN,
    MAX_GOAL_CHAIN,
    MAX_GOAL_TURNS,
    MAX_TOOL_ROUNDS,
    MAX_WORK_CONTINUES,
    MEMORY_ROOT,
    THREAD_JOIN_TIMEOUT,
)
from g023v2.goal import (
    GoalLeftovers,
    GoalRunResult,
    apply_goal_handoff,
    collect_goal_evidence,
    decide_chain_continue,
    evaluate_goal_completion,
    format_cap_continue_feedback,
    format_chain_next_spec,
    format_evaluator_feedback,
    format_goal_opener,
    leftovers_from_turn,
    parse_goal_spec,
    summarize_goal_done,
)
from g023v2.infinite import (
    apply_policy_to_fields,
    format_infinite_next_spec,
    parse_infinite_policy,
    policy_from_session_fields,
    should_prompt_infinite,
)
from g023v2.images import prepare_image_part
from g023v2.messages import user_message
from g023v2.orchestration import (
    META_RE,
    _is_clean_start,
    compact_history,
    extract_metadata,
    parse_round_tokens,
    preview_text,
    run_turn,
    should_compact,
    strip_harness_meta_in_span,
)
from g023v2.persist import MemoryStore, SessionLog, project_store_id
from g023v2.plan import (
    WorkPlan,
    compact_delegate_result,
    effort_for_role,
    format_child_results_block,
    format_work_continue,
    normalize_plan_files,
    normalize_role,
)
from g023v2.prompt import SYSTEM_PROMPT
from g023v2.schemas import build_tool_schemas, dispatch_allowlist
from g023v2.show import estimate_next_request_tokens, format_context_usage
from g023v2.skills import SkillStore
from g023v2.team import Agent, AgentTeam
from g023v2.tools import ProjectTools
from g023v2.util import clamp_effort, log
from g023v2.workdir import (
    GoalRecord,
    allocate_stamp,
    backups_rel,
    conclusion_for_result,
    ensure_g023_tree,
    log_rel,
    scratch_rel,
    usage_rel,
)


class Session:
    def __init__(
        self,
        project_root: Path | str,
        *,
        teams_enabled: bool = False,
        fresh: bool = False,
        effort: int | None = None,
        project: str | None = None,
        skills: SkillStore | None = None,
    ):
        self.root = Path(project_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.project = project if project is not None else project_store_id(self.root)
        self.teams_enabled = teams_enabled
        self.memory = MemoryStore(self.project)
        self.tools = ProjectTools(self.root, self.memory)
        self.skills = skills if skills is not None else SkillStore()
        self.system_prompt = SYSTEM_PROMPT
        self.tool_schemas = build_tool_schemas(teams_enabled)

        self.log = SessionLog(MEMORY_ROOT / self.project / "session.jsonl", fresh=fresh)
        self.history: list[dict[str, Any]] = []
        self.turn_metadata: list[dict[str, Any]] = []
        # Single next-request effort. Constructor/`--effort` and `set_effort`
        # write it once; each round's HARNESS_META overwrites it before the
        # next HTTP call; TurnResult replaces it so a later user turn does
        # not see the original user-set value.
        self.effort = clamp_effort(effort) if effort is not None else DEFAULT_EFFORT
        self.cumulative_hit = 0
        self.cumulative_miss = 0
        self.cumulative_output = 0
        self.memory_revision_seen = self.memory.revision
        self.children: dict[str, Agent] = {}
        self.children_lock = threading.Lock()
        self.work_plan = WorkPlan()
        self.team: AgentTeam | None = AgentTeam(self) if teams_enabled else None
        self.shutdown_event = threading.Event()
        self.turn_cancel_event = threading.Event()
        self.in_turn = False
        self.goal_active = False
        self.goal_continue_mode: str | None = None
        self.infinite_burst_left: int = 0
        self.goal_record: GoalRecord | None = None
        # Last inner /goal turn hit MAX_TOOL_ROUNDS. Not a cancel; /status shows it.
        self.last_hit_round_cap = False
        self.last_chain_reason = ""
        self.last_orchestrations = 0
        self.result_vault = ResultVault()

        if not fresh:
            self._replay_log()
        # Process-start `--effort` applies to this process's first request,
        # including after log replay. It is not sticky across later turns.
        if effort is not None:
            self.effort = clamp_effort(effort)

    def project_agents_md(self) -> str | None:
        return load_project_agents_md(self.root)

    @property
    def goal_stamp(self) -> str | None:
        rec = self.goal_record
        return rec.stamp if rec is not None else None

    def _begin_goal_workspace(self, goal: str, objectives: list[str]) -> bool:
        """Start a stamp/scratch/backup folder for this `/goal`. False if already open."""
        if self.goal_record is not None:
            return False
        stamp = allocate_stamp(self.root)
        ensure_g023_tree(self.root)
        self.tools.set_stamp(stamp, reset_ledger=True)
        self.goal_record = GoalRecord(
            stamp=stamp,
            project_root=self.root,
            goal=goal,
            objectives=list(objectives),
        )
        self.last_hit_round_cap = False
        self.last_chain_reason = ""
        self.last_orchestrations = 0
        log(
            f"[goal] workspace stamp={stamp}  "
            f"scratch={scratch_rel(stamp)}  backups={backups_rel(stamp)}"
        )
        return True

    def _finish_goal_workspace(self, result: GoalRunResult) -> None:
        rec = self.goal_record
        if rec is None:
            return
        leftovers = getattr(result, "leftovers", None)
        issues = list(leftovers.issues) if leftovers is not None else []
        recs = list(leftovers.recommendations) if leftovers is not None else []
        try:
            log_path, usage_path = rec.write_outputs(
                complete=bool(result.complete),
                conclusion=conclusion_for_result(result),
                last_text=result.text or "",
                missing=list(result.missing or []),
                leftovers_issues=issues,
                leftovers_recs=recs,
                chain_reason=result.chain_reason or "",
                orchestrations=int(result.orchestrations or 1),
                ledger=self.tools.ledger.snapshot(),
            )
            result.stamp = rec.stamp
            result.log_rel = log_rel(rec.stamp)
            result.usage_rel = usage_rel(rec.stamp)
            log(f"[goal] wrote {result.log_rel} and {result.usage_rel}")
            del log_path, usage_path
        except OSError as e:
            log(f"[goal] failed to write .g023 log/usage: {e}")
            result.stamp = rec.stamp
            result.log_rel = log_rel(rec.stamp)
            result.usage_rel = usage_rel(rec.stamp)
        self.goal_record = None
        self.tools.set_stamp(None)

    def _note_goal_turn(self, result: Any, summary: str = "") -> None:
        rec = self.goal_record
        if rec is None:
            return
        rec.note_turn(result, summary)

    def _replay_log(self) -> None:
        for e in self.log.load():
            t = e.get("type")
            if t == "history_item":
                self.history.append(e["item"])
            elif t == "metadata":
                self.turn_metadata.append(e["meta"])
            elif t == "effort":
                self.effort = clamp_effort(e["value"])
            elif t == "compact_marker":
                self._apply_compact_event(e)

    def _apply_compact_event(self, event: dict[str, Any]) -> None:
        """Apply a logged compact as a history transform (ordinary or /goal)."""
        item = event.get("item")
        try:
            history_len = int(event.get("history_len") or 1)
        except (TypeError, ValueError):
            history_len = 1
        keep_tail = max(0, history_len - 1)
        tail = self.history[-keep_tail:] if keep_tail else []
        if item:
            self.history = [item] + tail
        elif keep_tail:
            self.history = tail
        meta_len = event.get("metadata_len")
        if meta_len is not None:
            try:
                keep_meta = max(0, int(meta_len))
            except (TypeError, ValueError):
                keep_meta = 0
            self.turn_metadata = self.turn_metadata[-keep_meta:] if keep_meta else []

    def _log_history_item(self, item: dict[str, Any]) -> None:
        self.history.append(item)
        self.log.append({"type": "history_item", "item": item})

    def _log_metadata(self, meta: dict[str, Any]) -> None:
        self.turn_metadata.append(meta)
        self.log.append({"type": "metadata", "meta": meta})

    def _persist_history_span(self, start: int) -> None:
        """Write history[start:] to the session log without appending again."""
        for item in self.history[start:]:
            self.log.append({"type": "history_item", "item": item})

    def finalize_and_persist_turn(
        self,
        mark: int,
        result_text: str,
        *,
        effort: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Strip HARNESS_META from committed assistant items and log the span.

        `run_turn` already appended reasoning, tool, and assistant items.
        Do not append a second assistant-only copy of the turn text — that
        unpaired message is what triggers DeepSeek's reasoning_text 400.

        When `effort` is given it is the actual next-request reasoning_effort
        after per-round HARNESS_META (including a missing block, which keeps
        the current value). Overlay it onto stored metadata so a missing
        fence does not record DEFAULT_META's next_reasoning_effort.
        """
        meta, cleaned = extract_metadata(result_text)
        if not META_RE.search(result_text or ""):
            log("[turn] no HARNESS_META; using defaults")
        if effort is not None:
            applied = clamp_effort(effort)
            meta["next_reasoning_effort"] = applied
            meta["reason_next"] = applied > 0
        strip_harness_meta_in_span(self.history, mark)
        self._log_metadata(meta)
        self._persist_history_span(mark)
        return meta, cleaned

    def set_effort(self, value: int | str) -> int:
        """One-shot next-request override. Later rounds follow HARNESS_META."""
        self.effort = clamp_effort(value)
        self.log.append({"type": "effort", "value": self.effort})
        return self.effort

    def set_infinite_policy(self, value: str) -> str:
        """Set infinite-orchestration continue policy (`auto`/`prompt`/`N`)."""
        policy = parse_infinite_policy(value)
        if policy is None:
            raise ValueError("infinite orchestration mode is empty")
        mode, burst = apply_policy_to_fields(policy)
        self.goal_continue_mode = mode
        self.infinite_burst_left = burst
        return policy.describe()

    def format_status(self) -> str:
        """REPL-visible cap, chain, and session state. Not a cancel signal."""
        mode = "Agent Teams" if self.teams_enabled else "parent-child"
        continue_mode = self.goal_continue_mode or "unset (REPL picks on first leftover)"
        if self.goal_continue_mode and str(self.goal_continue_mode).isdigit():
            continue_mode = (
                f"burst {self.infinite_burst_left} then prompt"
            )
        cap_note = (
            "wrap-up is not a cancel; later /goal turns and the leftover "
            "chain may still dispatch tools"
        )
        lines = [
            f"project: {self.root}",
            f"mode: {mode}",
            f"effort: {self.effort}",
            f"tool-round cap: {MAX_TOOL_ROUNDS} per turn ({cap_note})",
            f"goal inner turns: {MAX_GOAL_TURNS}",
            f"infinite orchestration: unattended cap {MAX_GOAL_CHAIN} "
            f"(continue={continue_mode})",
            f"history: {len(self.history)} items, {len(self.turn_metadata)} metadata",
            f"context: {self._format_context_usage()} "
            f"(est. next-request tokens, chars/{CHARS_PER_TOKEN:g})",
            f"goal active: {self.goal_active}",
            f"last inner tool-round cap: {self.last_hit_round_cap}",
            f"last chain: reason={self.last_chain_reason or '-'} "
            f"orchestrations={self.last_orchestrations}",
            f"children live: {self._live_child_count()}",
            f"work plan open: {len(self.work_plan.incomplete())}",
        ]
        return "\n".join(lines)

    def _format_context_usage(self) -> str:
        return format_context_usage(estimate_next_request_tokens(self))

    def inject_memory(self) -> None:
        snap = self.memory.snapshot()
        if snap:
            self._log_history_item(user_message(snap))

    def maybe_compact(self) -> None:
        if not should_compact(self.history):
            return
        new_history, new_meta, fired = compact_history(self.history, self.turn_metadata)
        if fired:
            self.history = new_history
            self.turn_metadata = new_meta
            replacement = new_history[0] if new_history else None
            self.log.append({
                "type": "compact_marker",
                "kind": "ordinary",
                "item": replacement,
                "history_len": len(new_history),
                "metadata_len": len(new_meta),
            })
            log(f"[compact] folded older turns; history now {len(new_history)} items")

    def _memory_delta(self) -> str:
        if self.memory.revision == self.memory_revision_seen:
            return ""
        self.memory_revision_seen = self.memory.revision
        snap = self.memory.snapshot()
        return "[memory update]\n" + snap if snap else ""

    def request_interrupt(self) -> str:
        """SIGINT: cancel an in-flight turn; if idle, request REPL shutdown."""
        if self.in_turn:
            self.turn_cancel_event.set()
            return "cancel_turn"
        self.shutdown_event.set()
        return "quit"

    def _deliver_captain_inbox(self) -> None:
        if not self.team:
            return
        inbox = self.team.drain_captain_inbox()
        if inbox:
            self._log_history_item(user_message(f"[captain inbox]\n{inbox}"))
            log("[turn] captain inbox delivered")

    def run_user_turn(self, user_text: str, images: list[str] | None = None) -> str:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user_text}]
        for img in images or []:
            content.append(prepare_image_part(img, project_root=self.root))
        self._log_history_item({"role": "user", "content": content})
        extra = f"  images={len(images)}" if images else ""
        log(f"[turn] user: {preview_text(user_text, 160)}{extra}")

        self._deliver_captain_inbox()

        self.in_turn = True
        self.turn_cancel_event.clear()
        mark = len(self.history)
        try:
            result = run_turn(
                self.history,
                self.effort,
                self.tools,
                self.memory,
                tool_schemas=self.tool_schemas,
                system_prompt=self.system_prompt,
                session=self,
                cancel_event=self.turn_cancel_event,
                allowed_names=dispatch_allowlist(
                    self.teams_enabled, goal_mode=False
                ),
                surface_leftovers=True,
            )
            _meta, cleaned = self.finalize_and_persist_turn(
                mark, result.text, effort=result.effort
            )

            delta = self._memory_delta()
            if delta:
                self._log_history_item(user_message(delta))

            self.effort = clamp_effort(result.effort)
            self.log.append({"type": "effort", "value": self.effort})
            self._account_turn_tokens(result)
            cleaned = self._drain_open_work(cleaned)
        finally:
            self.in_turn = False

        self.maybe_compact()
        return cleaned

    def _account_turn_tokens(self, result: Any) -> None:
        if result.round_tokens:
            hit = sum(t.cached for t in result.round_tokens)
            miss = sum(t.uncached for t in result.round_tokens)
            out = sum(t.output for t in result.round_tokens)
        else:
            tokens = parse_round_tokens(result.usage)
            hit, miss, out = tokens.cached, tokens.uncached, tokens.output
        self.cumulative_hit += hit
        self.cumulative_miss += miss
        self.cumulative_output += out
        log(
            f"[turn] effort={self.effort}  "
            f"cached={hit:,}  uncached={miss:,}  output={out:,}  "
            f"cumulative cached={self.cumulative_hit:,}  "
            f"uncached={self.cumulative_miss:,}  output={self.cumulative_output:,}"
        )

    def run_goal(
        self,
        spec: str,
        images: list[str] | None = None,
        *,
        max_turns: int | None = None,
    ) -> GoalRunResult:
        """Run a `/goal` loop with the compaction tool and a completion evaluator.

        Success is withheld until `evaluate_goal_completion` reports complete.
        Missing objectives stay visible on the returned result.
        """
        goal, objectives = parse_goal_spec(spec)
        cap = MAX_GOAL_TURNS if max_turns is None else max(1, int(max_turns))
        if not goal:
            return GoalRunResult(
                complete=False,
                text="usage: /goal <goal text and optional objective list>",
                missing=["goal is unstated"],
                goal="",
                objectives=[],
            )

        owns_workspace = self._begin_goal_workspace(goal, list(objectives))
        opener = format_goal_opener(goal, objectives)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": opener}]
        for img in images or []:
            content.append(prepare_image_part(img, project_root=self.root))
        self._log_history_item({"role": "user", "content": content})
        extra = f"  images={len(images)}" if images else ""
        log(f"[goal] start: {preview_text(goal, 160)}{extra}")

        last_cleaned = ""
        evaluation = evaluate_goal_completion(goal, objectives, "")
        run_result = GoalRunResult(
            complete=False,
            text="",
            missing=list(evaluation.missing),
            goal=goal,
            objectives=list(objectives),
        )
        self.goal_active = True
        self.in_turn = True
        self.turn_cancel_event.clear()
        try:
            for turn_i in range(cap):
                if self.shutdown_event.is_set() or self.turn_cancel_event.is_set():
                    break
                self._deliver_captain_inbox()
                mark = len(self.history)
                result = run_turn(
                    self.history,
                    self.effort,
                    self.tools,
                    self.memory,
                    tool_schemas=self.tool_schemas,
                    system_prompt=self.system_prompt,
                    session=self,
                    cancel_event=self.turn_cancel_event,
                    goal_mode=True,
                    allowed_names=dispatch_allowlist(
                        self.teams_enabled, goal_mode=True
                    ),
                )
                if self.turn_cancel_event.is_set():
                    last_cleaned = result.text
                    break
                if result.hit_round_cap:
                    self.last_hit_round_cap = True
                    log(
                        "[goal] inner turn hit tool-round cap; "
                        "not cancelling — evaluator will run; "
                        "later inner turns can still dispatch tools"
                    )
                if result.goal_compacted:
                    strip_harness_meta_in_span(self.history, 0)
                    meta, cleaned = extract_metadata(result.text)
                    if not META_RE.search(result.text or ""):
                        log("[turn] no HARNESS_META; using defaults")
                    applied = clamp_effort(result.effort)
                    meta["next_reasoning_effort"] = applied
                    meta["reason_next"] = applied > 0
                    self.turn_metadata = []
                    self._log_metadata(meta)
                    replacement = self.history[0] if self.history else None
                    self.log.append({
                        "type": "compact_marker",
                        "kind": "goal",
                        "item": replacement,
                        "history_len": 1,
                        "metadata_len": len(self.turn_metadata),
                    })
                    if len(self.history) > 1:
                        self._persist_history_span(1)
                else:
                    _meta, cleaned = self.finalize_and_persist_turn(
                        mark, result.text, effort=result.effort
                    )

                last_cleaned = cleaned
                self.effort = clamp_effort(result.effort)
                self.log.append({"type": "effort", "value": self.effort})
                self._account_turn_tokens(result)
                summary = ""
                if self.turn_metadata:
                    summary = str(self.turn_metadata[-1].get("summary") or "")
                self._note_goal_turn(result, summary)

                delta = self._memory_delta()
                if delta:
                    self._log_history_item(user_message(delta))

                rows, still = self._wait_owned_children()
                if rows:
                    self._log_history_item(
                        user_message(format_child_results_block(rows))
                    )
                evidence = collect_goal_evidence(
                    self.history,
                    cleaned,
                    extra=self.result_vault.evidence(),
                )
                evaluation = evaluate_goal_completion(goal, objectives, evidence)
                for item in self.work_plan.incomplete():
                    evaluation.complete = False
                    evaluation.missing.append(
                        f"plan {item['id']}: {item['title']}"
                    )
                if still or self._live_child_count():
                    evaluation.complete = False
                    evaluation.missing.append("running child agents")
                if evaluation.complete:
                    log("[goal] evaluator complete")
                    run_result = GoalRunResult(
                        complete=True,
                        text=last_cleaned,
                        missing=[],
                        goal=goal,
                        objectives=list(objectives),
                    )
                    return run_result
                log(
                    "[goal] evaluator incomplete; missing: "
                    + ", ".join(evaluation.missing)
                )
                if turn_i + 1 < cap and not self.shutdown_event.is_set():
                    if result.hit_round_cap:
                        feedback = format_cap_continue_feedback(evaluation)
                    else:
                        feedback = format_evaluator_feedback(evaluation)
                    self._log_history_item(user_message(feedback))
            run_result = GoalRunResult(
                complete=False,
                text=last_cleaned,
                missing=list(evaluation.missing),
                goal=goal,
                objectives=list(objectives),
            )
            return run_result
        finally:
            self.goal_active = False
            self.in_turn = False
            run_result.text = last_cleaned or run_result.text
            run_result.missing = list(evaluation.missing)
            if owns_workspace:
                self._finish_goal_workspace(run_result)

    def _apply_chain_handoff(
        self,
        *,
        goal: str,
        done: str,
        leftovers: GoalLeftovers,
        next_step: str,
        kind: str = "goal_handoff",
    ) -> None:
        """Reset history to the compact handoff; log as a compact_marker."""
        new_history = apply_goal_handoff(
            self.history,
            goal=goal,
            done=done,
            leftovers=leftovers,
            next_step=next_step,
        )
        self.history = new_history
        self.turn_metadata = []
        replacement = self.history[0] if self.history else None
        self.log.append({
            "type": "compact_marker",
            "kind": kind,
            "item": replacement,
            "history_len": 1,
            "metadata_len": 0,
        })
        log("[infinite] compact handoff; history reset for next orchestration")

    def _prime_infinite_burst(self, mode: str) -> None:
        policy = parse_infinite_policy(mode)
        if policy is not None and policy.mode == "burst":
            self.infinite_burst_left = policy.remaining
        else:
            self.infinite_burst_left = 0

    def _infinite_gate(
        self,
        leftovers: GoalLeftovers,
        last: GoalRunResult,
        *,
        chain_index: int,
        cap: int,
        unattended: bool,
        fallback_mode: str,
        confirm_continue: Any | None,
    ) -> str | None:
        """Return a chain_reason to stop, or None to start the next orchestration."""
        cancelled = self.shutdown_event.is_set() or self.turn_cancel_event.is_set()
        max_chain = cap if unattended else max(cap, 10**9)
        should, reason = decide_chain_continue(
            leftovers=leftovers,
            chain_index=chain_index,
            max_chain=max_chain,
            inner_complete=last.complete,
            cancelled=cancelled,
        )
        if not should:
            if reason == "cap":
                last.missing = list(leftovers.items()) or list(last.missing)
                log("[infinite] unattended cap reached with leftovers still open")
            return reason

        mode = (
            self.goal_continue_mode
            if self.goal_continue_mode is not None
            else fallback_mode
        )
        ask = should_prompt_infinite(
            mode,
            self.infinite_burst_left,
            fallback_prompt=not unattended,
        )
        if ask:
            if confirm_continue is None:
                policy = policy_from_session_fields(mode, self.infinite_burst_left)
                if policy.mode == "burst":
                    last.missing = list(leftovers.items())
                    log("[infinite] burst exhausted with leftovers still open")
                    return "burst"
                last.missing = list(leftovers.items())
                return "declined"
            ok = bool(confirm_continue(leftovers, last))
            if not ok:
                last.missing = list(leftovers.items())
                return "declined"
        policy = policy_from_session_fields(
            self.goal_continue_mode if self.goal_continue_mode is not None else mode,
            self.infinite_burst_left,
        )
        if policy.mode == "burst" and self.infinite_burst_left > 0:
            self.infinite_burst_left -= 1
        return None

    def run_goal_chain(
        self,
        spec: str,
        images: list[str] | None = None,
        *,
        max_turns: int | None = None,
        max_chain: int | None = None,
        continue_mode: str | None = None,
        confirm_continue: Any | None = None,
    ) -> GoalRunResult:
        """Outer loop: run /goal, surface leftovers, compact-punt, repeat.

        Infinite orchestration: each leftover chain step is a fresh
        conversation whose only carried transcript is the compact handoff
        plus the frozen system prompt. Stops when leftovers are empty
        (`satisfied`), an unattended run hits the chain cap (`cap`), a
        burst N is exhausted unattended (`burst`), the inner evaluator
        fails, the user declines, or the run is cancelled.

        `continue_mode`: `auto` starts the next orchestration without a
        human reply; `prompt` calls `confirm_continue(leftovers, result)`
        and does not continue unless that returns true; a digit string N
        runs N automatic subsequent orchestrations then asks (or stops
        unattended). Session `goal_continue_mode` is the fallback.
        """
        cap = MAX_GOAL_CHAIN if max_chain is None else max(1, int(max_chain))
        mode = continue_mode if continue_mode is not None else self.goal_continue_mode
        if mode is None:
            # --once / no callback → auto. REPL with a confirm callback → prompt
            # so the user can pick auto vs prompt-each / N after the first leftover.
            mode = "prompt" if confirm_continue is not None else "auto"
        self._prime_infinite_burst(mode)
        current_spec = spec
        parsed_goal, parsed_objectives = parse_goal_spec(spec)
        last = GoalRunResult(complete=False, text="", missing=["goal is unstated"])
        owns_workspace = False
        if parsed_goal:
            owns_workspace = self._begin_goal_workspace(
                parsed_goal, list(parsed_objectives)
            )
        try:
            last = self._run_goal_chain_loop(
                current_spec=current_spec,
                images=images,
                max_turns=max_turns,
                cap=cap,
                mode=mode,
                confirm_continue=confirm_continue,
            )
            self.last_chain_reason = last.chain_reason or ""
            self.last_orchestrations = last.orchestrations
            return last
        finally:
            if owns_workspace:
                self._finish_goal_workspace(last)

    def _run_goal_chain_loop(
        self,
        *,
        current_spec: str,
        images: list[str] | None,
        max_turns: int | None,
        cap: int,
        mode: str,
        confirm_continue: Any | None,
    ) -> GoalRunResult:
        last = GoalRunResult(complete=False, text="", missing=["goal is unstated"])
        unattended = confirm_continue is None
        i = 0
        while True:
            if self.shutdown_event.is_set() or self.turn_cancel_event.is_set():
                last.chain_reason = "cancelled"
                last.orchestrations = i
                last.complete = False
                return last
            last = self.run_goal(
                current_spec,
                images if i == 0 else None,
                max_turns=max_turns,
            )
            leftovers = leftovers_from_turn(self.history, last.text)
            last.leftovers = leftovers
            last.orchestrations = i + 1
            stop = self._infinite_gate(
                leftovers,
                last,
                chain_index=i,
                cap=cap,
                unattended=unattended,
                fallback_mode=mode,
                confirm_continue=confirm_continue,
            )
            if stop is not None:
                if stop == "satisfied":
                    last.chain_reason = "satisfied"
                    return last
                last.complete = False
                last.chain_reason = stop
                return last
            done = summarize_goal_done(last.goal, last.objectives, last.text)
            self._apply_chain_handoff(
                goal=last.goal or current_spec,
                done=done,
                leftovers=leftovers,
                next_step="finish remaining issues and recommendations",
            )
            current_spec = format_chain_next_spec(last.goal, leftovers)
            log(
                f"[infinite] orchestration {i + 1} → {i + 2}; "
                f"leftovers={len(leftovers.items())}"
            )
            i += 1

    def continue_ordinary_infinite(
        self,
        last_text: str,
        leftovers: GoalLeftovers,
        *,
        original_prompt: str,
        continue_mode: str | None = None,
        confirm_continue: Any | None = None,
        max_chain: int | None = None,
    ) -> GoalRunResult:
        """Infinite orchestration after an ordinary (non-/goal) turn.

        The first orchestration already ran. This loop gates leftovers,
        compact-handoffs, and runs further ordinary turns until leftovers
        are empty or the continue policy stops.
        """
        cap = MAX_GOAL_CHAIN if max_chain is None else max(1, int(max_chain))
        mode = continue_mode if continue_mode is not None else self.goal_continue_mode
        if mode is None:
            mode = "prompt" if confirm_continue is not None else "auto"
        self._prime_infinite_burst(mode)
        goal = (original_prompt or "").strip() or "ordinary task"
        last = GoalRunResult(
            complete=True,
            text=last_text or "",
            goal=goal,
            leftovers=leftovers,
            orchestrations=1,
        )
        unattended = confirm_continue is None
        i = 0
        while True:
            stop = self._infinite_gate(
                last.leftovers,
                last,
                chain_index=i,
                cap=cap,
                unattended=unattended,
                fallback_mode=mode,
                confirm_continue=confirm_continue,
            )
            if stop is not None:
                last.chain_reason = stop
                if stop != "satisfied":
                    last.complete = False
                    last.missing = list(last.leftovers.items()) or list(last.missing)
                self.last_chain_reason = last.chain_reason or ""
                self.last_orchestrations = last.orchestrations
                return last
            if self.shutdown_event.is_set() or self.turn_cancel_event.is_set():
                last.complete = False
                last.chain_reason = "cancelled"
                self.last_chain_reason = last.chain_reason
                self.last_orchestrations = last.orchestrations
                return last
            done = summarize_goal_done(last.goal, last.objectives, last.text)
            self._apply_chain_handoff(
                goal=last.goal or goal,
                done=done,
                leftovers=last.leftovers,
                next_step="finish remaining issues and recommendations",
                kind="infinite_handoff",
            )
            next_spec = format_infinite_next_spec(
                last.goal or goal, last.leftovers, via_goal=False
            )
            log(
                f"[infinite] orchestration {i + 1} → {i + 2}; "
                f"leftovers={len(last.leftovers.items())}"
            )
            text = self.run_user_turn(next_spec)
            cancelled = (
                self.shutdown_event.is_set() or self.turn_cancel_event.is_set()
            )
            nxt = leftovers_from_turn(self.history, text)
            last = GoalRunResult(
                complete=not cancelled,
                text=text,
                goal=last.goal or goal,
                leftovers=nxt,
                orchestrations=i + 2,
                chain_reason="cancelled" if cancelled else "",
            )
            i += 1

    def _live_child_count(self) -> int:
        with self.children_lock:
            return self._live_child_count_unlocked()

    def spawn_child(
        self,
        name: str,
        task: str,
        effort: int | None = None,
        context: str = "",
        fresh: bool = True,
        role: str = "",
    ) -> str:
        caller = self._caller_agent()
        role_n = normalize_role(role)
        if caller is not None:
            if caller.role != "lead" or caller.depth != 0:
                return "ERROR: only a depth-0 lead may spawn children"
            if role_n == "lead":
                return "ERROR: a lead cannot spawn another lead"
            fresh = True
            depth = caller.depth + 1
            if depth > MAX_CHILD_DEPTH:
                return f"ERROR: child depth cap reached ({MAX_CHILD_DEPTH})"
            spawned_by = caller.name
        else:
            depth = 0
            spawned_by = None
        resolved_effort = effort_for_role(role_n, effort)
        with self.children_lock:
            existing = self.children.get(name)
            if existing is not None:
                if existing.persistent or existing.thread_live:
                    return f"ERROR: child '{name}' already exists"
                self.children.pop(name, None)
            if self._live_child_count_unlocked() >= MAX_CONCURRENT_CHILDREN:
                return f"ERROR: concurrent child limit reached ({MAX_CONCURRENT_CHILDREN})"
            child = Agent(
                name=name,
                session=self,
                effort=resolved_effort,
                workspace=self.root,
                persistent=False,
                role=role_n,
            )
            child.depth = depth
            child.spawned_by = spawned_by
            self.children[name] = child

        parent_history = None
        if not fresh:
            parent_history = self._clean_parent_history()

        child.start_one_shot(task, context, fresh=fresh, parent_history=parent_history)
        role_bit = f" role={role_n}" if role_n else ""
        return (
            f"spawned child '{name}' effort={clamp_effort(resolved_effort)} "
            f"fresh={fresh}{role_bit}"
        )

    def _live_child_count_unlocked(self) -> int:
        # Include children whose thread has not started yet so two concurrent
        # spawn_child calls cannot both slip under the cap.
        return sum(
            1
            for c in self.children.values()
            if c.thread_live or c.thread is None
        )

    def _clean_parent_history(self) -> list[dict[str, Any]]:
        idx = len(self.history)
        while idx > 0 and not _is_clean_start(self.history[idx - 1]):
            idx -= 1
        return list(self.history[:idx])

    def _resolve_child(self, child: str) -> Agent | None:
        return self.children.get(child)

    def send_to_child(self, child: str, message: str, kind: str = "supplement") -> str:
        c = self._resolve_child(child)
        if not c:
            return f"ERROR: unknown child '{child}'"
        try:
            msg_id = c.send(message, kind=kind)
            return f"queued message {msg_id} for '{child}'"
        except RuntimeError as e:
            return f"ERROR: {e}"

    def edit_child_message(self, message_id: str, new_content: str) -> str:
        for c in self.children.values():
            if c.messages.edit(message_id, new_content):
                return f"edited message {message_id}"
        return f"ERROR: message {message_id} not found or not editable"

    def delete_child_message(self, message_id: str) -> str:
        for c in self.children.values():
            if c.messages.delete(message_id):
                return f"deleted message {message_id}"
        return f"ERROR: message {message_id} not found or not deletable"

    def interject_child(self, child: str, message: str) -> str:
        c = self._resolve_child(child)
        if not c:
            return f"ERROR: unknown child '{child}'"
        msg_id = c.interject(message)
        return f"interjected message {msg_id} into '{child}'"

    def stop_child(self, child: str) -> str:
        c = self._resolve_child(child)
        if not c:
            return f"ERROR: unknown child '{child}'"
        c.stop()
        if c.thread is not None:
            c.thread.join(timeout=THREAD_JOIN_TIMEOUT)
        if not c.persistent:
            with self.children_lock:
                self.children.pop(child, None)
            return f"stopped one-shot child '{child}'; name is free"
        return f"cancelled current turn for '{child}'"

    def list_children(self) -> str:
        owned = self._owned_children()
        if not owned:
            return "(no child agents)"
        lines = []
        for c in owned:
            pending = len(c.messages.pending())
            alive = "alive" if c.thread_live else "dead"
            role = c.role or "-"
            lines.append(
                f"{c.name}: status={c.status()} [{alive}] role={role} "
                f"depth={c.depth} effort={c.effort} "
                f"pending_msgs={pending} summary={c.summary or '-'}"
            )
        return "\n".join(lines)

    def get_child_result(self, child: str, timeout: float | None = None) -> str:
        c = self._resolve_owned_child(child)
        if c is None:
            return f"ERROR: unknown child '{child}'"
        wait_s = GET_CHILD_RESULT_TIMEOUT
        if timeout is not None:
            try:
                wait_s = float(timeout)
            except (TypeError, ValueError):
                wait_s = GET_CHILD_RESULT_TIMEOUT
        wait_s = max(0.0, min(wait_s, GET_CHILD_RESULT_TIMEOUT))
        if not c.completion_event.wait(timeout=wait_s):
            return (
                f"child '{child}' is still running\n"
                + self.work_plan.format_progress()
            )
        body = self._consume_child_result(c)
        return body + "\n" + self.work_plan.format_progress()

    def join_children(
        self,
        children: list[str] | None = None,
        timeout: float | None = None,
    ) -> str:
        names = None
        if children:
            names = [str(n) for n in children if str(n).strip()]
        wait_s = JOIN_CHILDREN_TIMEOUT
        if timeout is not None:
            try:
                wait_s = float(timeout)
            except (TypeError, ValueError):
                wait_s = JOIN_CHILDREN_TIMEOUT
        wait_s = max(0.0, min(wait_s, JOIN_CHILDREN_TIMEOUT))
        rows, still = self._wait_owned_children(timeout=wait_s, names=names)
        parts = []
        if rows:
            parts.append(format_child_results_block(rows))
        if still:
            parts.append("still running: " + ", ".join(still))
        if not parts:
            parts.append("(no children to join)")
        parts.append(self.work_plan.format_progress())
        return "\n".join(parts)

    def plan_work(
        self,
        action: str,
        item_id: str = "",
        title: str = "",
        details: str = "",
        role: str = "",
        effort: Any = None,
        depends_on: list[str] | None = None,
        files: list[str] | None = None,
        result: str = "",
    ) -> str:
        if self.teams_enabled:
            return (
                "ERROR: plan_work is parent-child mode; "
                "use team_create_task with --teams"
            )
        act = (action or "").strip().lower()
        effort_n: int | None
        if effort is None or effort == "":
            effort_n = None
        else:
            try:
                effort_n = int(effort)
            except (TypeError, ValueError):
                return "ERROR: effort must be an integer"
        try:
            if act == "list":
                return self.work_plan.format_list()
            if act == "add":
                pid = self.work_plan.add(
                    title=title,
                    details=details,
                    role=role,
                    effort=effort_n,
                    depends_on=list(depends_on or []),
                    files=list(files or []),
                )
                item = self.work_plan.get(pid)
                role_bit = f" role={item['role']}" if item and item["role"] else ""
                return f"added {pid}{role_bit} effort={item['effort'] if item else '-'}"
            if act == "update":
                item = self.work_plan.update(
                    item_id,
                    title=title or None,
                    details=details if details else None,
                    role=role or None,
                    effort=effort_n,
                    depends_on=list(depends_on) if depends_on is not None else None,
                    files=list(files) if files is not None else None,
                )
                return f"updated {item['id']}"
            if act == "complete":
                item = self.work_plan.complete(item_id, result=result)
                return f"completed {item['id']}"
            if act == "spawn":
                return self._spawn_ready_plan_items()
        except RuntimeError as e:
            return f"ERROR: {e}"
        return (
            "ERROR: plan_work action must be add, update, list, complete, or spawn"
        )

    def _spawn_ready_plan_items(self) -> str:
        caller = self._caller_agent()
        ready = self.work_plan.ready_pending()
        if not ready:
            return "(no ready plan items)\n" + self.work_plan.format_progress()
        occupied = set(self.work_plan.occupied_files())
        lines: list[str] = []
        for item in ready:
            if caller is not None and item.get("role") == "lead":
                lines.append(f"ERROR: {item['id']} role=lead cannot be spawned by a lead")
                continue
            item_files = normalize_plan_files(item.get("files"))
            if item_files and item_files & occupied:
                overlap = ", ".join(sorted(item_files & occupied))
                lines.append(
                    f"held {item['id']}: files overlap running work ({overlap})"
                )
                continue
            try:
                self.work_plan.claim(item["id"], item["id"])
            except RuntimeError as e:
                lines.append(f"ERROR: {e}")
                continue
            out = self.spawn_child(
                name=item["id"],
                task=self.work_plan.task_text(item),
                effort=int(item["effort"]),
                role=str(item.get("role") or ""),
                fresh=True,
            )
            if out.startswith("ERROR:"):
                self.work_plan.release(item["id"])
                lines.append(f"{item['id']}: {out}")
            else:
                lines.append(f"{item['id']}: {out}")
                occupied.update(item_files)
        lines.append(self.work_plan.format_progress())
        return "\n".join(lines) if lines else "(no ready plan items)"

    def _caller_agent(self) -> Agent | None:
        ident = threading.current_thread()
        with self.children_lock:
            for c in self.children.values():
                if c.thread is ident:
                    return c
        if self.team:
            for m in self.team.members.values():
                if m.thread is ident:
                    return m
        return None

    def _owned_children(self, names: list[str] | None = None) -> list[Agent]:
        caller = self._caller_agent()
        with self.children_lock:
            pool = list(self.children.values())
        if caller is not None:
            pool = [c for c in pool if c.spawned_by == caller.name]
        if names:
            want = {n for n in names}
            pool = [c for c in pool if c.name in want]
        return pool

    def _resolve_owned_child(self, name: str) -> Agent | None:
        owned = self._owned_children([name])
        return owned[0] if owned else None

    def _consume_child_result(self, child: Agent) -> str:
        child.result_consumed = True
        if child.error:
            text = f"ERROR: child '{child.name}' failed: {child.error}"
        else:
            text = child.result or "(empty result)"
        text = compact_delegate_result(text)
        self.work_plan.record_owner_result(child.name, text)
        return text

    def _wait_owned_children(
        self,
        timeout: float | None = None,
        names: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        wait_s = JOIN_CHILDREN_TIMEOUT if timeout is None else max(0.0, float(timeout))
        agents = self._owned_children(names)
        deadline = time.monotonic() + wait_s
        for c in agents:
            if c.completion_event.is_set():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            c.completion_event.wait(timeout=remaining)
        rows: list[str] = []
        still: list[str] = []
        for c in agents:
            if not c.completion_event.is_set():
                still.append(c.name)
                continue
            if c.result_consumed:
                continue
            rows.append(f"{c.name}: {self._consume_child_result(c)}")
        return rows, still

    def _drain_open_work(self, cleaned: str) -> str:
        """Join children and continue until the work plan is empty.

        Ordinary turns take extra inner run_turn loops (capped). /goal
        injects child results and lets the evaluator loop continue.
        Cancelled turns do not drain. Teams mode has its own board.
        """
        if self.teams_enabled:
            return cleaned
        continues = 0
        while continues < MAX_WORK_CONTINUES:
            if self.shutdown_event.is_set() or self.turn_cancel_event.is_set():
                break
            rows, still = self._wait_owned_children()
            incomplete = self.work_plan.incomplete()
            if not rows and not incomplete and not still:
                break
            if rows:
                block = format_child_results_block(rows)
            else:
                block = ""
            if self.goal_active:
                if block:
                    self._log_history_item(user_message(block))
                break
            feedback = format_work_continue(
                child_block=block,
                plan_lines=self.work_plan.format_lines(incomplete),
                still_running=still,
            )
            self._log_history_item(user_message(feedback))
            continues += 1
            mark = len(self.history)
            result = run_turn(
                self.history,
                self.effort,
                self.tools,
                self.memory,
                tool_schemas=self.tool_schemas,
                system_prompt=self.system_prompt,
                session=self,
                cancel_event=self.turn_cancel_event,
                allowed_names=dispatch_allowlist(
                    self.teams_enabled, goal_mode=False
                ),
                surface_leftovers=True,
            )
            _meta, cleaned = self.finalize_and_persist_turn(
                mark, result.text, effort=result.effort
            )
            delta = self._memory_delta()
            if delta:
                self._log_history_item(user_message(delta))
            self.effort = clamp_effort(result.effort)
            self.log.append({"type": "effort", "value": self.effort})
            self._account_turn_tokens(result)
        return cleaned

    def _require_team(self) -> AgentTeam:
        if not self.team:
            raise RuntimeError("Agent Teams mode is not enabled; run with --teams")
        return self.team

    def team_create_member(self, name: str, role: str, effort: int = DEFAULT_CHILD_EFFORT) -> str:
        return self._require_team().create_member(name, role, effort)

    def team_create_task(self, title: str, details: str, depends_on: list[str] | None = None, files: list[str] | None = None) -> str:
        tid = self._require_team().task_board.create(title, details, depends_on, files)
        return f"created {tid}"

    def team_claim_task(self, task_id: str, member: str) -> str:
        team = self._require_team()
        claimed = False
        try:
            team.task_board.claim(task_id, member)
            claimed = True
            team.dispatch_task(task_id, member)
            return f"{member} claimed {task_id} and the task was dispatched"
        except RuntimeError as e:
            if claimed:
                try:
                    team.task_board.release(task_id, member)
                except RuntimeError:
                    pass
            return f"ERROR: {e}"

    def team_complete_task(self, task_id: str, member: str, result: str) -> str:
        try:
            self._require_team().task_board.complete(task_id, member, result)
            return f"{member} completed {task_id}"
        except RuntimeError as e:
            return f"ERROR: {e}"

    def team_send_message(self, sender: str, recipient: str, message: str) -> str:
        try:
            mid = self._require_team().send_message(sender, recipient, message)
            return f"message {mid} sent from {sender} to {recipient}"
        except RuntimeError as e:
            return f"ERROR: {e}"

    def team_status(self) -> str:
        return self._require_team().status()

    def team_interrupt(self, member: str) -> str:
        team = self._require_team()
        c = team.members.get(member)
        if not c:
            return f"ERROR: unknown member '{member}'"
        c.cancel()
        return f"interrupted '{member}'"

    def team_list_tasks(self) -> str:
        tasks = self._require_team().task_board.list_tasks()
        if not tasks:
            return "(no tasks)"
        lines = []
        for t in tasks:
            deps = ",".join(t["depends_on"]) or "-"
            lines.append(
                f"{t['id']}: [{t['status']}] owner={t['owner'] or '-'} "
                f"deps={deps}  {t['title']}"
            )
        return "\n".join(lines)

    def shutdown(self) -> None:
        self.shutdown_event.set()
        threads: list[tuple[str, threading.Thread]] = []
        for c in self.children.values():
            c.stop()
            if c.thread:
                threads.append((f"child:{c.name}", c.thread))
        if self.team:
            for m in self.team.members.values():
                m.stop()
                if m.thread:
                    threads.append((f"member:{m.name}", m.thread))
        deadline = time.monotonic() + THREAD_JOIN_TIMEOUT
        for label, t in threads:
            remaining = max(0.1, deadline - time.monotonic())
            t.join(timeout=remaining)
            if t.is_alive():
                log(f"[shutdown] {label} did not stop within {THREAD_JOIN_TIMEOUT}s; leaving as daemon")
