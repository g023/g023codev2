"""Per-project `.g023` workspace: stamp, scratch, backups, logs, usage, prompts.

Created lazily when a `/goal` starts, on the first file-tool backup, on
the first saved user prompt, on the first tool-result vault write, or on
the first `run_cli` log. Importing this module does not mkdir.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from g023v2.constants import G023_DIR_NAME

LOGS_DIR = "logs"
USAGE_DIR = "usage"
SCRATCH_DIR = "scratch"
BACKUPS_DIR = "backups"
PROMPTS_DIR = "prompts"
CLI_DIR = "cli"

_INTERNAL_GITIGNORE = "*\n!.gitignore\n"


def make_stamp(when: datetime | None = None) -> str:
    """`YYYYMMDD_HHMMSS` local stamp. Taken at the start of a goal."""
    dt = when or datetime.now()
    return dt.strftime("%Y%m%d_%H%M%S")


def g023_root(project_root: Path) -> Path:
    return Path(project_root) / G023_DIR_NAME


def scratch_rel(stamp: str) -> str:
    return f"{G023_DIR_NAME}/{SCRATCH_DIR}/{stamp}/"


def cli_log_rel(stamp: str, n: int) -> str:
    """Project-relative path for one `run_cli` log (readable via `read_file`)."""
    return f"{G023_DIR_NAME}/{SCRATCH_DIR}/{stamp}/{CLI_DIR}/{int(n)}.log"


def backups_rel(stamp: str) -> str:
    return f"{G023_DIR_NAME}/{BACKUPS_DIR}/{stamp}/"


def log_rel(stamp: str) -> str:
    return f"{G023_DIR_NAME}/{LOGS_DIR}/{stamp}.log"


def usage_rel(stamp: str) -> str:
    return f"{G023_DIR_NAME}/{USAGE_DIR}/{stamp}.md"


def prompts_dir(project_root: Path) -> Path:
    return g023_root(project_root) / PROMPTS_DIR


def prompt_file_rel(n: int) -> str:
    return f"{G023_DIR_NAME}/{PROMPTS_DIR}/{int(n)}.txt"


def list_prompt_numbers(project_root: Path) -> list[int]:
    """Canonical `N.txt` ids under `.g023/prompts/`, sorted ascending.

    `01.txt` is ignored so numbering stays `1.txt`, `2.txt`, … (the stem
    must equal `str(int(stem))`). Gaps are kept; the next id is max+1.
    """
    folder = prompts_dir(project_root)
    if not folder.is_dir():
        return []
    nums: list[int] = []
    try:
        names = list(folder.iterdir())
    except OSError:
        return []
    for p in names:
        if not p.is_file() or p.suffix != ".txt":
            continue
        stem = p.stem
        if not stem.isdigit():
            continue
        n = int(stem)
        if n < 1 or stem != str(n):
            continue
        nums.append(n)
    nums.sort()
    return nums


def next_prompt_number(project_root: Path) -> int:
    nums = list_prompt_numbers(project_root)
    return (max(nums) + 1) if nums else 1


def load_saved_prompts(project_root: Path) -> list[str]:
    """Prompt texts oldest-first. Missing files are skipped."""
    texts: list[str] = []
    for n in list_prompt_numbers(project_root):
        path = prompts_dir(project_root) / f"{n}.txt"
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return texts


def save_user_prompt(project_root: Path, text: str) -> Path | None:
    """Write `<project>/.g023/prompts/{n}.txt` and return the path.

    Creates `.g023` lazily (same as /goal and backups). Empty text is
    not stored. `n` is one higher than the current max canonical id.
    """
    if text == "":
        return None
    ensure_g023_tree(project_root)
    folder = prompts_dir(project_root)
    folder.mkdir(parents=True, exist_ok=True)
    n = next_prompt_number(project_root)
    path = folder / f"{n}.txt"
    while path.exists():
        n += 1
        path = folder / f"{n}.txt"
        if n > 1_000_000:
            raise OSError("prompt history id overflow")
    _write_text(path, text)
    return path


class PromptHistory:
    """In-memory list plus `.g023/prompts/{n}.txt` on disk. Oldest first."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.entries: list[str] = load_saved_prompts(self.project_root)

    def record(self, text: str) -> Path | None:
        path = save_user_prompt(self.project_root, text)
        if path is not None:
            self.entries.append(text)
        return path


def ensure_g023_tree(project_root: Path) -> Path:
    """Create `<project>/.g023` and an internal gitignore. No-op if present."""
    root = g023_root(project_root)
    root.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    if not gi.is_file():
        _write_text(gi, _INTERNAL_GITIGNORE)
    return root


def stamp_taken(project_root: Path, stamp: str) -> bool:
    root = g023_root(project_root)
    return any(
        (
            (root / LOGS_DIR / f"{stamp}.log").exists(),
            (root / USAGE_DIR / f"{stamp}.md").exists(),
            (root / SCRATCH_DIR / stamp).exists(),
            (root / BACKUPS_DIR / stamp).exists(),
        )
    )


def allocate_stamp(project_root: Path, when: datetime | None = None) -> str:
    """Unique stamp under this project. Suffix `_2`, `_3`, … on collision."""
    base = make_stamp(when)
    stamp = base
    n = 2
    while stamp_taken(project_root, stamp):
        stamp = f"{base}_{n}"
        n += 1
        if n > 10_000:
            stamp = f"{base}_{time.time_ns()}"
            break
    return stamp


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _rel_list(paths: Iterable[str]) -> str:
    items = [p for p in paths if p]
    if not items:
        return "(none)"
    return ", ".join(f"`{p}`" for p in items)


def _truncate(text: str, limit: int) -> str:
    one = " ".join((text or "").split())
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


class FileLedger:
    """Created / modified / deleted paths plus fetch_url count for one stamp."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.created: list[str] = []
        self.modified: list[str] = []
        self.deleted: list[str] = []
        self.fetch_url = 0
        self._created = set()
        self._modified = set()
        self._deleted = set()

    def note_created(self, rel: str) -> None:
        rel = (rel or "").replace("\\", "/").strip()
        if not rel:
            return
        with self._lock:
            if rel not in self._created:
                self._created.add(rel)
                self.created.append(rel)

    def note_modified(self, rel: str) -> None:
        rel = (rel or "").replace("\\", "/").strip()
        if not rel:
            return
        with self._lock:
            if rel not in self._modified:
                self._modified.add(rel)
                self.modified.append(rel)

    def note_deleted(self, rel: str) -> None:
        rel = (rel or "").replace("\\", "/").strip()
        if not rel:
            return
        with self._lock:
            if rel not in self._deleted:
                self._deleted.add(rel)
                self.deleted.append(rel)

    def note_fetch_url(self) -> None:
        with self._lock:
            self.fetch_url += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "created": list(self.created),
                "modified": list(self.modified),
                "deleted": list(self.deleted),
                "fetch_url": self.fetch_url,
            }


@dataclass
class GoalRecord:
    """Usage and mission log for one `/goal` (including the outer chain)."""

    stamp: str
    project_root: Path
    goal: str
    objectives: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    started_mono: float = field(default_factory=time.monotonic)
    cached: int = 0
    uncached: int = 0
    output: int = 0
    web_searches: int = 0
    tool_calls: int = 0
    summaries: list[str] = field(default_factory=list)
    turns: int = 0

    def note_turn(self, result: Any, summary: str = "") -> None:
        self.turns += 1
        round_tokens = getattr(result, "round_tokens", None)
        if round_tokens:
            self.cached += sum(t.cached for t in round_tokens)
            self.uncached += sum(t.uncached for t in round_tokens)
            self.output += sum(t.output for t in round_tokens)
        usage = getattr(result, "usage", None) or {}
        if not round_tokens and usage:
            self.cached += int(usage.get("prompt_cache_hit_tokens") or 0)
            miss = usage.get("prompt_cache_miss_tokens")
            if miss is None:
                inp = int(usage.get("input_tokens") or 0)
                hit = int(usage.get("prompt_cache_hit_tokens") or 0)
                miss = max(0, inp - hit)
            self.uncached += int(miss or 0)
            self.output += int(
                usage.get("output_tokens") or usage.get("completion_tokens") or 0
            )
        searches = getattr(result, "web_search_items", None) or []
        self.web_searches += len(searches)
        calls = getattr(result, "tool_calls", None) or []
        self.tool_calls += len(calls)
        text = (summary or "").strip()
        if text and text != "Turn completed without explicit metadata.":
            if text not in self.summaries:
                self.summaries.append(text)

    def write_outputs(
        self,
        *,
        complete: bool,
        conclusion: str,
        last_text: str = "",
        missing: Sequence[str] | None = None,
        leftovers_issues: Sequence[str] | None = None,
        leftovers_recs: Sequence[str] | None = None,
        chain_reason: str = "",
        orchestrations: int = 1,
        ledger: dict[str, Any] | None = None,
    ) -> tuple[Path, Path]:
        ended = datetime.now()
        duration = max(0.0, time.monotonic() - self.started_mono)
        snap = ledger or {
            "created": [],
            "modified": [],
            "deleted": [],
            "fetch_url": 0,
        }
        log_path = self.project_root / log_rel(self.stamp)
        usage_path = self.project_root / usage_rel(self.stamp)
        _write_text(
            log_path,
            format_mission_log(
                stamp=self.stamp,
                project_root=self.project_root,
                started_at=self.started_at,
                ended_at=ended,
                goal=self.goal,
                objectives=self.objectives,
                summaries=self.summaries,
                last_text=last_text,
                complete=complete,
                conclusion=conclusion,
                missing=missing or [],
                leftovers_issues=leftovers_issues or [],
                leftovers_recs=leftovers_recs or [],
                chain_reason=chain_reason,
                ledger=snap,
            ),
        )
        _write_text(
            usage_path,
            format_usage_md(
                stamp=self.stamp,
                project_root=self.project_root,
                started_at=self.started_at,
                ended_at=ended,
                duration=duration,
                complete=complete,
                chain_reason=chain_reason,
                orchestrations=orchestrations,
                turns=self.turns,
                cached=self.cached,
                uncached=self.uncached,
                output=self.output,
                web_searches=self.web_searches,
                fetch_url=int(snap.get("fetch_url") or 0),
                tool_calls=self.tool_calls,
                created=list(snap.get("created") or []),
                modified=list(snap.get("modified") or []),
                deleted=list(snap.get("deleted") or []),
            ),
        )
        return log_path, usage_path


def format_mission_log(
    *,
    stamp: str,
    project_root: Path,
    started_at: datetime,
    ended_at: datetime,
    goal: str,
    objectives: Sequence[str],
    summaries: Sequence[str],
    last_text: str,
    complete: bool,
    conclusion: str,
    missing: Sequence[str],
    leftovers_issues: Sequence[str],
    leftovers_recs: Sequence[str],
    chain_reason: str,
    ledger: dict[str, Any],
) -> str:
    lines = [
        f"stamp: {stamp}",
        f"project: {project_root}",
        f"started: {started_at.isoformat(timespec='seconds')}",
        f"ended: {ended_at.isoformat(timespec='seconds')}",
        "",
        f"goal: {goal or '(unstated)'}",
        "objectives:",
    ]
    if objectives:
        lines.extend(f"- {o}" for o in objectives)
    else:
        lines.append("- (the goal statement is the sole objective)")
    lines.append("")
    lines.append("what happened:")
    if summaries:
        lines.extend(f"- {s}" for s in summaries[:24])
        if len(summaries) > 24:
            lines.append(f"- ({len(summaries) - 24} more summaries omitted)")
    else:
        lines.append("- (no turn summaries)")
    created = list(ledger.get("created") or [])
    modified = list(ledger.get("modified") or [])
    deleted = list(ledger.get("deleted") or [])
    if created:
        lines.append("- created: " + ", ".join(created[:20]))
    if modified:
        lines.append("- modified: " + ", ".join(modified[:20]))
    if deleted:
        lines.append("- deleted: " + ", ".join(deleted[:20]))
    extra = _truncate(last_text, 400)
    if extra:
        lines.append(f"- last: {extra}")
    lines.append("")
    lines.append("conclusion:")
    lines.append(f"complete: {'yes' if complete else 'no'}")
    if chain_reason:
        lines.append(f"chain: {chain_reason}")
    if conclusion:
        lines.append(conclusion)
    if missing:
        lines.append("missing:")
        lines.extend(f"- {m}" for m in missing)
    else:
        lines.append("missing: (none)")
    if leftovers_issues or leftovers_recs:
        if leftovers_issues:
            lines.append("leftover issues:")
            lines.extend(f"- {i}" for i in leftovers_issues)
        if leftovers_recs:
            lines.append("leftover recommendations:")
            lines.extend(f"- {r}" for r in leftovers_recs)
    else:
        lines.append("leftovers: (none)")
    lines.append("")
    return "\n".join(lines)


def format_usage_md(
    *,
    stamp: str,
    project_root: Path,
    started_at: datetime,
    ended_at: datetime,
    duration: float,
    complete: bool,
    chain_reason: str,
    orchestrations: int,
    turns: int,
    cached: int,
    uncached: int,
    output: int,
    web_searches: int,
    fetch_url: int,
    tool_calls: int,
    created: Sequence[str],
    modified: Sequence[str],
    deleted: Sequence[str],
) -> str:
    rows = [
        ("stamp", stamp),
        ("project", str(project_root)),
        ("started", started_at.isoformat(timespec="seconds")),
        ("ended", ended_at.isoformat(timespec="seconds")),
        ("duration_seconds", f"{duration:.3f}"),
        ("complete", "yes" if complete else "no"),
        ("chain_reason", chain_reason or "(n/a)"),
        ("orchestrations", str(orchestrations)),
        ("goal_turns", str(turns)),
        ("cached_tokens", f"{cached}"),
        ("uncached_tokens", f"{uncached}"),
        ("output_tokens", f"{output}"),
        ("web_searches", str(web_searches)),
        ("fetch_url", str(fetch_url)),
        ("tool_calls", str(tool_calls)),
        ("files_created", _rel_list(created)),
        ("files_modified", _rel_list(modified)),
        ("files_deleted", _rel_list(deleted)),
        ("scratch", f"`{scratch_rel(stamp)}`"),
        ("backups", f"`{backups_rel(stamp)}`"),
        ("log", f"`{log_rel(stamp)}`"),
    ]
    lines = [
        f"# g023 usage {stamp}",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key, value in rows:
        cell = str(value).replace("|", "\\|")
        lines.append(f"| {key} | {cell} |")
    lines.append("")
    return "\n".join(lines)


def conclusion_for_result(result: Any) -> str:
    """One-line conclusion from a GoalRunResult."""
    complete = bool(getattr(result, "complete", False))
    reason = (getattr(result, "chain_reason", "") or "").strip()
    if complete and reason in ("", "satisfied"):
        return "goal complete; leftover issues empty" if reason == "satisfied" else "goal complete"
    if reason == "cap":
        return "stopped at chain cap with leftover work still open"
    if reason == "burst":
        return "stopped after auto burst with leftover work still open"
    if reason == "declined":
        return "next orchestration was not affirmed"
    if reason == "cancelled":
        return "cancelled before completion"
    if reason == "incomplete":
        return "inner evaluator did not accept every objective"
    missing = list(getattr(result, "missing", None) or [])
    if missing:
        return "incomplete; missing objectives remain"
    return "incomplete"
