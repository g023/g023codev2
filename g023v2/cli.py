"""CLI entry for g023v2."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from g023v2.constants import (
    CHARS_PER_TOKEN,
    K_DAT_PATH,
    MAX_GOAL_CHAIN,
    MAX_GOAL_TURNS,
    MAX_INFINITE_BURST,
    MAX_TOOL_ROUNDS,
    MODEL,
    SOFT_CONTEXT_LIMIT,
)
from g023v2.goal import format_leftovers_visible, leftovers_from_turn
from g023v2.infinite import (
    apply_policy_to_fields,
    format_infinite_help,
    format_infinite_picker_prompt,
    format_infinite_yn_prompt,
    interpret_infinite_reply,
    parse_infinite_arg as _parse_infinite_arg,
)
from g023v2.line_edit import EOF, QUIT, SUBMIT, read_repl_line
from g023v2.session import Session
from g023v2.show import (
    estimate_next_request_tokens,
    format_context_usage,
    format_show_from_line,
    is_show_command,
)
from g023v2.util import (
    COLOR_ANSWER,
    COLOR_ERROR,
    COLOR_THINK,
    COLOR_TOOL,
    clamp_effort,
    log,
    paint,
)
from g023v2.workdir import PromptHistory

_BANNER_BOX_INNER = 58

_UNRECORDED_COMMANDS = {
    "/quit",
    "/exit",
    "/help",
    "/?",
    "/status",
    "/memory",
    "/history",
    "/children",
    "/team",
    "/infinite",
    "/show",
}


def should_record_prompt(line: str) -> bool:
    """True for user/goal prompts; false for empty lines and slash status cmds."""
    s = (line or "").strip()
    if not s:
        return False
    if s in _UNRECORDED_COMMANDS:
        return False
    if s.startswith("/effort"):
        return False
    if s.startswith("/infinite"):
        return False
    if is_show_command(s):
        return False
    return True


def format_repl_help() -> str:
    """REPL `/help` text: /goal, infinite orchestration, tool-round cap."""
    return (
        "g023v2 commands\n"
        "  /help              this text\n"
        "  /status            project, effort, tool-round cap, infinite orchestration\n"
        "  /show [topic]      inspect the next request (local; no API call).\n"
        "                     topics: prompt, agents, tools, schemas, extras,\n"
        "                     prefix, request, effort, history\n"
        "  /goal <text>       run a goal with a completion evaluator, leftover\n"
        "                     surfacing, and infinite orchestration. Optional\n"
        "                     bullet/numbered lines are objectives.\n"
        "  /infinite          help for infinite orchestration\n"
        "  /infinite auto|prompt|N  set leftover-chain continue policy\n"
        "  /effort N          next request only (0=off, 1-100=budget)\n"
        "  /children          child roster (plan_work / join_children in parent-child)\n"
        "  /team              team roster and task board\n"
        "  /memory            memory snapshot\n"
        "  /history           history and metadata counts\n"
        "  /quit, /exit       shut down\n"
        "\n"
        "The REPL prompt prefix is estimated next-request tokens "
        f"(chars/{CHARS_PER_TOKEN:g}) versus the "
        f"{SOFT_CONTEXT_LIMIT // 1000}k soft context limit, e.g. "
        "12.4k/400k >. That number is local only; it is not sent to "
        "the model and is not prepended to history.\n"
        "\n"
        "Prompt keys (TTY)\n"
        "  arrows             move the cursor (up/down: previous/next line,\n"
        "                     or start/end of a single-line buffer)\n"
        "  Ctrl+Up / Ctrl+Down  cycle older / newer saved prompts. The text\n"
        "                     you were already typing is kept as a draft and\n"
        "                     restored when you Ctrl+Down past the newest.\n"
        "  Ctrl+C             copy selection (or the whole line) to clipboard\n"
        "  Ctrl+V             paste at the cursor\n"
        "  Ctrl+Q             quit after confirmation\n"
        "  Saved prompts live in .g023/prompts/{n}.txt (1.txt, 2.txt, …).\n"
        "\n"
        + format_infinite_help()
        + "\n\n"
        "Tool-round cap\n"
        f"  Each run_turn dispatches at most {MAX_TOOL_ROUNDS} tool-using rounds,\n"
        "  then one wrap-up request that does not dispatch tools. That wrap-up\n"
        "  is not a cancel and is not the end of the /goal: the evaluator still\n"
        "  runs, later inner /goal turns get a fresh cap and may dispatch tools,\n"
        "  and infinite orchestration can still dispatch tools. History keeps the\n"
        "  tool results already produced. /status shows whether the last inner\n"
        "  turn hit the cap."
    )


def _banner_box(lines: list[str], inner: int = _BANNER_BOX_INNER) -> str:
    """UTF-8 box with a left-padded row per line. `inner` is chars between bars."""
    top = "╭" + "─" * inner + "╮"
    bot = "╰" + "─" * inner + "╯"
    rows = []
    for line in lines:
        content = ("  " + line)[:inner].ljust(inner)
        rows.append("│" + content + "│")
    return "\n".join([top, *rows, bot])


def _key_file_ready() -> bool:
    """True when K.dat exists and has a non-empty line. Never returns the secret."""
    try:
        return K_DAT_PATH.is_file() and bool(
            K_DAT_PATH.read_text(encoding="utf-8").strip()
        )
    except OSError:
        return False


def _memory_key_count(session: Session) -> int:
    data = getattr(session.memory, "data", None)
    return len(data) if isinstance(data, dict) else 0


def format_repl_banner(session: Session) -> str:
    """Interactive-REPL welcome: identity, this project, how to start.

    Caps and command names stay in the text so `/help` is discoverable and
    existing tests can still grep `tool-rounds/turn=`. `--once` does not print
    this. Color uses the existing palette (green title, yellow invite, grey
    keys, red only when K.dat is missing). The API key itself is never shown.
    """
    mode = "Agent Teams" if session.teams_enabled else "parent-child"
    continue_mode = session.goal_continue_mode or "ask"
    agents_note = (
        "AGENTS.md loaded" if session.project_agents_md() else "no AGENTS.md"
    )
    n_hist = len(session.history)
    n_meta = len(session.turn_metadata)
    if n_hist:
        hist_word = "item" if n_hist == 1 else "items"
        session_note = f"resumed · {n_hist} history {hist_word}"
        if n_meta:
            meta_word = "entry" if n_meta == 1 else "entries"
            session_note += f" · {n_meta} metadata {meta_word}"
    else:
        session_note = "fresh"
    n_mem = _memory_key_count(session)
    mem_note = f"{n_mem} key" + ("" if n_mem == 1 else "s")
    key_ok = _key_file_ready()
    key_note = "ready" if key_ok else f"missing or empty — {K_DAT_PATH}"

    header = paint(
        _banner_box(
            [
                "g023v2",
                "DeepSeek V4.1 Flash coding agent",
            ]
        ),
        COLOR_ANSWER,
        stream=sys.stdout,
    )
    key_line = f"  key       {key_note}"
    if not key_ok:
        key_line = paint(key_line, COLOR_ERROR, stream=sys.stdout)
    facts = "\n".join(
        [
            f"  project   {session.root}",
            f"  runtime   {MODEL}  mode={mode}  effort={session.effort}",
            f"  agents    {agents_note}",
            f"  session   {session_note}",
            f"  memory    {mem_note}",
            key_line,
            "",
            f"  tool-rounds/turn={MAX_TOOL_ROUNDS}  goal-turns={MAX_GOAL_TURNS}  "
            f"infinite-cap={MAX_GOAL_CHAIN}  continue={continue_mode}",
            "  wrap-up is not a cancel; later /goal turns and leftover chain "
            "can still dispatch tools",
        ]
    )
    invite = paint(
        "  Type a task, or /goal <text> for a tracked objective.",
        COLOR_TOOL,
        stream=sys.stdout,
    )
    commands = (
        "  Commands: /help, /status, /show, /goal, /infinite, /effort N, /children, "
        "/team, /memory, /history, /quit"
    )
    keys = paint(
        "  Keys: arrows move cursor, Ctrl+Up/Down prompt history, "
        "Ctrl+C copy, Ctrl+V paste, Ctrl+Q quit (confirm)",
        COLOR_THINK,
        stream=sys.stdout,
    )
    return "\n".join([header, "", facts, "", invite, commands, keys])


def format_repl_prompt(session: Session) -> str:
    """Idle REPL prompt: next-request token estimate, then `> `."""
    used = estimate_next_request_tokens(session)
    return f"{format_context_usage(used)} > "


def resolve_project_root(
    *,
    project: str | None,
    workspace: str | None,
    cwd: Path | None = None,
) -> Path:
    """Pick the sandboxed project directory.

    Default is the process cwd so `g023v2` launched from any folder operates
    on that folder. `--project` is a path. `--workspace` joins with `--project`
    when both are set; either flag alone is used as the root.
    """
    here = (cwd or Path.cwd()).expanduser().resolve()
    if workspace and project:
        proj_path = Path(project).expanduser()
        if proj_path.is_absolute():
            raise ValueError(
                "absolute --project cannot be combined with --workspace "
                "(joining would silently discard the workspace and sandbox "
                "the absolute path)"
            )
        return (Path(workspace).expanduser() / project).resolve()
    if project:
        return Path(project).expanduser().resolve()
    if workspace:
        return Path(workspace).expanduser().resolve()
    return here


def parse_infinite_cli_arg(value: str) -> str:
    """argparse wrapper so a bad MODE becomes ArgumentTypeError."""
    try:
        return _parse_infinite_arg(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g023v2",
        description=(
            "DeepSeek V4.1 Flash coding agent. Launch with no arguments from any "
            "project folder; the current working directory is the project. "
            f"Uses the Responses API with model {MODEL} only."
        ),
        epilog=(
            "Examples:\n"
            "  g023v2\n"
            "  g023v2 --resume\n"
            "  g023v2 \"what does this repo do?\"\n"
            "  g023v2 --once \"fix the tests\" --effort 80\n"
            "\n"
            "Interactive commands: /help, /status, /show [topic], /goal, /infinite, "
            "/effort N, /children, /team, /memory, /history, /quit.\n"
            "/show dumps the next request locally (system prompt, AGENTS.md, "
            "tools, extras, effort) without an API call.\n"
            "TTY keys: arrows move the cursor; Ctrl+Up/Down cycle "
            ".g023/prompts history (in-progress draft is kept); "
            "Ctrl+C copy, Ctrl+V paste, Ctrl+Q quit after confirm.\n"
            "/help documents /goal, infinite orchestration, and the "
            f"tool-round cap ({MAX_TOOL_ROUNDS} tool-using rounds, then wrap-up).\n"
            "/goal runs a completion evaluator plus leftover surfacing.\n"
            "Infinite orchestration: after a /goal or ordinary prompt "
            "finishes, leftover issues/recommendations start a fresh next "
            "orchestration (compact handoff) until leftovers are empty. "
            "--infinite auto|prompt|N (also --goal-continue): auto continues "
            "without a prompt; prompt waits for affirmation before each "
            "subsequent orchestration; N runs N automatic subsequent "
            f"orchestrations then asks (1-{MAX_INFINITE_BURST}). Non-TTY "
            "--once /goal auto-continues unless prompt/N is set. Ordinary "
            "--once chains only when --infinite/--goal-continue is set. "
            "REPL with no flag asks auto / prompt / N / stop on first leftover. "
            "Unattended auto brakes at the chain cap; REPL auto does not. "
            "Hitting the tool-round cap is not a cancel: later inner /goal "
            "turns and infinite orchestration can still dispatch tools. "
            "compact_goal_conversation is not executable outside /goal "
            "(seeing it in tools is not permission).\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="run this prompt and exit (same as --once); omit for the interactive REPL",
    )
    parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="project directory (default: current working directory)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        metavar="PATH",
        help=(
            "optional workspace root. With --project NAME, the root is "
            "workspace/NAME. Alone, this path is the project root."
        ),
    )
    parser.add_argument(
        "--teams",
        action="store_true",
        help="enable Agent Teams mode (captain-led team with shared task board)",
    )
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--fresh",
        dest="fresh",
        action="store_true",
        help=(
            "start with an empty conversation (default). Deletes the session "
            "log for this project. Does not clear memory.json or the URL cache."
        ),
    )
    session_group.add_argument(
        "--resume",
        dest="fresh",
        action="store_false",
        help=(
            "continue the previous conversation for this project: replay "
            "session.jsonl (history, compact markers, last effort). "
            "Default is a fresh start."
        ),
    )
    parser.set_defaults(fresh=True)
    parser.add_argument("--once", default=None, metavar="TEXT", help="run a single prompt and exit")
    parser.add_argument("--image", action="append", default=[], help="image path or URL to attach")
    parser.add_argument(
        "--effort",
        type=int,
        default=None,
        metavar="N",
        help=(
            "reasoning effort 0-100 (clamped) for the first round only "
            "(initial request of this process). 0 = thinking off; "
            "1-100 = thinking budget. Default 60. "
            "Also set interactively with /effort N (one-shot next request). "
            "Later rounds follow the model's next-round control: each round's "
            "HARNESS_META (reason_next, next_reasoning_effort) sets the next "
            "request. The original value is not reapplied on later turns. "
            "Thinking-on is sent as the request parameter reasoning_effort "
            "(integer 1-100); thinking-off uses reasoning.effort none. "
            "Not placed in the system prompt or other cacheable prefix."
        ),
    )
    parser.add_argument(
        "--infinite",
        "--goal-continue",
        dest="infinite",
        type=parse_infinite_cli_arg,
        default=None,
        metavar="MODE",
        help=(
            "infinite orchestration after leftover issues/recommendations: "
            "'auto' starts the next orchestration without a human reply; "
            "'prompt' waits for affirmation before each subsequent run; "
            f"a positive integer N (1-{MAX_INFINITE_BURST}) runs N automatic "
            "subsequent orchestrations then asks (or stops unattended). "
            "Applies to /goal and to ordinary prompts. Non-TTY --once /goal "
            "defaults to auto unless this flag is set. Ordinary --once "
            "chains only when this flag is set. Unattended auto brakes at "
            "the chain cap; cap-stop or burst-stop with leftovers open is "
            "incomplete, not success. --goal-continue is the same flag."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.prompt is not None and args.once is not None:
        parser.error("pass a prompt as a positional argument or --once, not both")
    once = args.once if args.once is not None else args.prompt

    try:
        root = resolve_project_root(project=args.project, workspace=args.workspace)
    except ValueError as e:
        parser.error(str(e))
    session = Session(
        root,
        teams_enabled=args.teams,
        fresh=args.fresh,
        effort=clamp_effort(args.effort) if args.effort is not None else None,
    )
    if args.infinite is not None:
        session.set_infinite_policy(args.infinite)
    if not session.history:
        session.inject_memory()

    def _handle_sigint(signum, frame):
        action = session.request_interrupt()
        if action == "cancel_turn":
            log("\n[signal] interrupt received; cancelling current turn")
        else:
            log("\n[signal] interrupt received; stopping")

    signal.signal(signal.SIGINT, _handle_sigint)

    pending_images = list(args.image or [])

    def _take_images() -> list[str]:
        images = pending_images[:]
        pending_images.clear()
        return images

    def _print_goal_workspace(result) -> None:
        log_rel = getattr(result, "log_rel", "") or ""
        usage_rel = getattr(result, "usage_rel", "") or ""
        if log_rel:
            print(f"[g023] log {log_rel}")
        if usage_rel:
            print(f"[g023] usage {usage_rel}")

    def _print_chain_footer(result, *, via_goal: bool) -> None:
        leftovers = getattr(result, "leftovers", None)
        if leftovers is not None:
            print(format_leftovers_visible(leftovers))
        reason = getattr(result, "chain_reason", "") or ""
        n = getattr(result, "orchestrations", 1)
        if result.complete and reason in ("", "satisfied"):
            if via_goal:
                print("[goal] complete")
            if n > 1:
                print(f"[infinite] {n} orchestration(s); satisfied")
            if via_goal:
                _print_goal_workspace(result)
            return
        if via_goal:
            print("[goal] incomplete")
        else:
            print("[infinite] incomplete")
        if reason == "cap":
            print("  unattended chain cap reached with leftovers still open")
        elif reason == "burst":
            print("  auto burst exhausted with leftovers still open")
        elif reason == "declined":
            print("  next orchestration was not affirmed")
        elif reason == "cancelled":
            print("  cancelled")
        for item in result.missing:
            print(f"  missing: {item}")
        if via_goal:
            _print_goal_workspace(result)

    def _print_goal_result(result) -> None:
        print(paint(result.text, COLOR_ANSWER, stream=sys.stdout))
        _print_chain_footer(result, via_goal=True)

    prompt_history = PromptHistory(session.root)

    def _read_line(
        prompt: str,
        *,
        initial: str = "",
        use_history: bool = False,
    ) -> LineResult:
        return read_repl_line(
            prompt,
            history=prompt_history.entries if use_history else None,
            initial=initial,
            stop_event=session.shutdown_event,
        )

    def _confirm_quit() -> bool:
        result = _read_line("Quit g023v2? [y/N] ")
        if result.action == QUIT:
            return True
        if result.action == EOF:
            return True
        return result.text.strip().lower() in ("y", "yes")

    def _confirm_chain_continue(leftovers, result) -> bool:
        print(format_leftovers_visible(leftovers))
        n = getattr(result, "orchestrations", 1)
        got = _read_line(format_infinite_yn_prompt(n + 1))
        if got.action != SUBMIT:
            return False
        return got.text.strip().lower() in ("y", "yes")

    def _repl_confirm_continue(leftovers, result) -> bool:
        print(format_leftovers_visible(leftovers))
        n = getattr(result, "orchestrations", 1)
        mode_now = session.goal_continue_mode
        if mode_now == "auto":
            return True
        if mode_now == "prompt":
            got = _read_line(format_infinite_yn_prompt(n + 1))
            if got.action != SUBMIT:
                return False
            return got.text.strip().lower() in ("y", "yes")
        got = _read_line(format_infinite_picker_prompt())
        if got.action != SUBMIT:
            return False
        policy = interpret_infinite_reply(got.text)
        if policy is None:
            return False
        mode, burst = apply_policy_to_fields(policy)
        session.goal_continue_mode = mode
        session.infinite_burst_left = burst
        return True

    if once:
        try:
            if should_record_prompt(once):
                prompt_history.record(once)
            images = _take_images()
            if is_show_command(once):
                print(format_show_from_line(session, once.strip()))
                return
            if once.strip().startswith("/goal"):
                mode = args.infinite if args.infinite is not None else "auto"
                result = session.run_goal_chain(
                    once,
                    images,
                    continue_mode=mode,
                    confirm_continue=(
                        _confirm_chain_continue if mode == "prompt" else None
                    ),
                )
                _print_goal_result(result)
            else:
                out = session.run_user_turn(once, images)
                print(paint(out, COLOR_ANSWER, stream=sys.stdout))
                leftovers = leftovers_from_turn(session.history, out)
                if args.infinite is not None and not leftovers.empty():
                    mode = args.infinite
                    result = session.continue_ordinary_infinite(
                        out,
                        leftovers,
                        original_prompt=once,
                        continue_mode=mode,
                        confirm_continue=(
                            _confirm_chain_continue if mode == "prompt" else None
                        ),
                    )
                    if getattr(result, "orchestrations", 1) > 1:
                        print(paint(result.text, COLOR_ANSWER, stream=sys.stdout))
                    _print_chain_footer(result, via_goal=False)
        finally:
            session.shutdown()
        return

    print(format_repl_banner(session))
    pending_initial = ""
    try:
        while not session.shutdown_event.is_set():
            print()
            result = _read_line(
                format_repl_prompt(session),
                initial=pending_initial,
                use_history=True,
            )
            pending_initial = ""
            if result.action == EOF:
                break
            if result.action == QUIT:
                pending_initial = result.text
                if _confirm_quit():
                    break
                continue
            line = result.text.strip()
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if should_record_prompt(line):
                prompt_history.record(line)
            if line in ("/help", "/?"):
                print(format_repl_help())
                continue
            if is_show_command(line):
                print(format_show_from_line(session, line))
                continue
            if line == "/status":
                print(session.format_status())
                continue
            if line == "/memory":
                print(session.memory.snapshot() or "(empty)")
                continue
            if line == "/history":
                ctx = format_context_usage(estimate_next_request_tokens(session))
                print(
                    f"{len(session.history)} items, "
                    f"{len(session.turn_metadata)} metadata entries, "
                    f"context {ctx}"
                )
                continue
            if line == "/children":
                print(session.list_children())
                continue
            if line == "/team":
                if session.team:
                    print(session.team_status())
                else:
                    print("Agent Teams mode is not enabled")
                continue
            if line.startswith("/effort"):
                parts = line.split()
                if len(parts) != 2:
                    print("usage: /effort N   (next request only, 0=off, 1-100)")
                    continue
                try:
                    session.set_effort(parts[1])
                    print(f"effort set to {session.effort} (next request only)")
                except (TypeError, ValueError):
                    print("usage: /effort N   (next request only, 0=off, 1-100)")
                continue
            if line.startswith("/infinite"):
                parts = line.split()
                if len(parts) == 1:
                    print(format_infinite_help())
                    continue
                if len(parts) != 2:
                    print(
                        "usage: /infinite            help\n"
                        "       /infinite auto|prompt|N   set continue policy"
                    )
                    continue
                try:
                    described = session.set_infinite_policy(parts[1])
                    print(f"infinite orchestration continue={described}")
                except ValueError as e:
                    print(str(e))
                continue
            if line.startswith("/goal"):
                spec = line[5:].strip()
                if not spec:
                    print("usage: /goal <goal text>  (optional bullet objectives)")
                    continue
                try:
                    result = session.run_goal_chain(
                        line,
                        _take_images(),
                        continue_mode=session.goal_continue_mode,
                        confirm_continue=_repl_confirm_continue,
                    )
                    _print_goal_result(result)
                except KeyboardInterrupt:
                    log("[goal] interrupted by user")
                    session.goal_active = False
                    session.turn_cancel_event.clear()
                continue
            try:
                out = session.run_user_turn(line, _take_images())
                print(paint(out, COLOR_ANSWER, stream=sys.stdout))
                leftovers = leftovers_from_turn(session.history, out)
                if not leftovers.empty():
                    result = session.continue_ordinary_infinite(
                        out,
                        leftovers,
                        original_prompt=line,
                        continue_mode=session.goal_continue_mode,
                        confirm_continue=_repl_confirm_continue,
                    )
                    if getattr(result, "orchestrations", 1) > 1:
                        print(paint(result.text, COLOR_ANSWER, stream=sys.stdout))
                    _print_chain_footer(result, via_goal=False)
            except KeyboardInterrupt:
                log("[turn] interrupted by user")
                session.turn_cancel_event.clear()
    finally:
        session.shutdown()
