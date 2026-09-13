"""Ingest-time tool-result budgeting for DeepSeek V4 prefix-unit cache.

Wasted context is cheap once it is a cache hit (Example 1: append-only) and
expensive to rewrite (Example 2: a middle change misses, then the common
prefix is persisted for the *next* request). Measured in
`tests/test_context_budget_sim.py`, the move that cuts the cost of the
final result is: never put the dump in history. Shape it at ingest, park
the tail in a scratch vault the model can `read_file`, and leave history
append-only.

Do not receipt `read_file` / `read_files` / `read_image`. The model asked
for those bytes; paging them through a vault costs an extra round.
`run_cli` is already a receipt (dump is on disk); keep it full.

Per-round observation masking and early compaction lose on this API:
they are Example 2 every time they fire.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from g023v2.constants import (
    G023_DIR_NAME,
    TOOL_RESULT_BUDGET_CHARS,
    VAULT_STORE_MAX_CHARS,
)
from g023v2.workdir import _write_text

TOOL_RECEIPT_HEADER = "[TOOL RECEIPT]"

# Tools whose result is the thing the model just asked to see.
KEEP_FULL_TOOLS = frozenset({
    "read_file",
    "read_files",
    "read_image",
    "write_file",
    "edit_file",
    "apply_edits",
    "replace_all",
    "replace_in_files",
    "restore_file",
    "memory_read",
    "memory_write",
    "memory_list",
    "skill_search",
    "skill_read",
    "skill_write",
    # Receipt is already compact; the dump lives in the cli log.
    "run_cli",
})

# Prefer the test-aware shaper; other bulky tools get head+tail.
SHAPE_SHELL_TOOLS = frozenset({"run_shell"})
BUDGET_TOOLS = frozenset({
    "run_shell",
    "grep",
    "fetch_url",
    "find_files",
    "list_dir",
})

_FAIL_LINE = re.compile(
    r"^(FAIL|ERROR|E\s|F\s|FAILED|AssertionError|Traceback|"
    r"FAILED |ERROR: |ok$|FAILED \(|={3,}|-{3,}|"
    r"Ran \d+ test|=+ FAILURES =+|=+ ERRORS =+)",
    re.I,
)


def vault_rel(stamp: str, vid: str) -> str:
    return f"{G023_DIR_NAME}/scratch/{stamp}/vault/{vid}.txt"


class ResultVault:
    """Session-scoped omitted tails. In-memory for the evaluator; on disk
    under `.g023/scratch/<stamp>/vault/` so `read_file` can page them.

    Not persisted in `session.jsonl`. On `--resume` the receipts remain in
    history; the model re-reads project files or re-runs a command.
    """

    def __init__(self) -> None:
        self.blobs: dict[str, str] = {}
        self.paths: dict[str, str] = {}
        self._n = 0
        self._lock = threading.Lock()

    def put(self, text: str, tools: Any | None = None) -> tuple[str, str | None]:
        stored = text if text is not None else ""
        encoded = stored.encode("utf-8")
        if len(encoded) > VAULT_STORE_MAX_CHARS:
            stored = encoded[:VAULT_STORE_MAX_CHARS].decode("utf-8", errors="ignore")
        with self._lock:
            self._n += 1
            vid = f"v{self._n}"
            self.blobs[vid] = stored
            rel: str | None = None
            if tools is not None:
                rel = self._write_disk(vid, stored, tools)
                if rel:
                    self.paths[vid] = rel
            return vid, rel

    def _write_disk(self, vid: str, stored: str, tools: Any) -> str | None:
        try:
            stamp = tools._ensure_stamp()
        except Exception:
            return None
        rel = vault_rel(stamp, vid)
        try:
            dest = Path(tools.root) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            _write_text(dest, stored)
        except OSError:
            return None
        return rel

    def evidence(self) -> str:
        if not self.blobs:
            return ""
        return "\n".join(self.blobs.values())


def shape_shell_output(text: str, max_chars: int = TOOL_RESULT_BUDGET_CHARS) -> str:
    """Keep test failures and a head/tail. Deterministic; no model call."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    keep: list[str] = []
    if lines:
        keep.append(lines[0])
    for line in lines[1:]:
        if _FAIL_LINE.search(line) or "FAILED" in line or "ERROR" in line:
            keep.append(line)
    head_n, tail_n = 40, 30
    head = lines[:head_n]
    tail = lines[-tail_n:] if len(lines) > head_n else []
    seen: set[str] = set()
    ordered: list[str] = []
    for line in head + keep + ["…"] + tail:
        if line in seen and line != "…":
            continue
        seen.add(line)
        ordered.append(line)
    body = "\n".join(ordered)
    if len(body) > max_chars:
        body = body[: max_chars // 2] + "\n…\n" + body[-(max_chars // 2) :]
    omitted = len(text) - len(body)
    return body + f"\n… [shaped shell; omitted {omitted} chars]"


def _head_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n… [{omitted} chars omitted]\n"
        + text[-tail:]
    )


def _should_budget(name: str, text: str, max_chars: int) -> bool:
    if not text:
        return False
    if name in KEEP_FULL_TOOLS:
        return False
    if name in BUDGET_TOOLS:
        return len(text) > max_chars
    return len(text) > max_chars


def budget_tool_result(
    name: str,
    result: Any,
    *,
    vault: ResultVault | None = None,
    tools: Any | None = None,
    max_chars: int = TOOL_RESULT_BUDGET_CHARS,
) -> Any:
    """Return the history-facing tool result. Non-strings (vision parts) pass through."""
    if not isinstance(result, str):
        return result
    if not _should_budget(name, result, max_chars):
        return result
    full = result
    if name in SHAPE_SHELL_TOOLS:
        body = shape_shell_output(full, max_chars=max_chars)
    else:
        body = _head_tail(full, max_chars)
    if len(body) >= len(full):
        return full
    vid = ""
    rel = None
    if vault is not None:
        vid, rel = vault.put(full, tools)
    omitted = len(full) - len(body)
    loc = f"path={rel}" if rel else "path=(not stored)"
    header = (
        f"{TOOL_RECEIPT_HEADER} tool={name} vault={vid or '-'} {loc} "
        f"omitted={omitted}\n"
    )
    if rel:
        header += (
            f"Omitted span is under {rel}. read_file that path (offset/limit) "
            "only if you need the omitted bytes.\n"
        )
    else:
        header += (
            "Omitted span was not stored. Re-run a narrower command or "
            "re-read the file instead of asking for the full dump again.\n"
        )
    return header + body
