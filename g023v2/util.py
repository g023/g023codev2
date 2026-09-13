"""Shared logging, API key load, and effort clamping. No HTTP."""

from __future__ import annotations

import os
import sys
import threading
from typing import TextIO

from g023v2.constants import K_DAT_PATH

_STDERR_LOCK = threading.Lock()
_ACTIVE_LIVE: LivePrinter | None = None

_API_KEY: str | None = None

RESET = "\033[0m"
COLOR_THINK = "\033[90m"  # grey
COLOR_ANSWER = "\033[32m"  # green
COLOR_TOOL = "\033[33m"  # yellow
COLOR_ERROR = "\033[31m"  # red — errors only
COLOR_TOOL_ERROR = COLOR_ERROR
COLOR_CLI = "\033[36m"  # cyan — run_cli start/end banners

CHANNEL_COLORS = {
    "think": COLOR_THINK,
    "model": COLOR_ANSWER,
}


def color_enabled(stream: TextIO | None = None) -> bool:
    """Whether ANSI color should be written to `stream` (default stderr).

    Off when NO_COLOR is set or TERM=dumb. FORCE_COLOR (non-zero) forces on.
    Otherwise requires a TTY. io.StringIO has no isatty; that is treated as off.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    force = os.environ.get("FORCE_COLOR")
    if force and force != "0":
        return True
    target = stream if stream is not None else sys.stderr
    try:
        return bool(target.isatty())
    except Exception:
        return False


def paint(
    text: str,
    color: str,
    *,
    enabled: bool | None = None,
    stream: TextIO | None = None,
) -> str:
    """Wrap `text` in ANSI color, or return it unchanged when color is off."""
    if not text:
        return text
    if enabled is None:
        enabled = color_enabled(stream)
    if not enabled:
        return text
    return f"{color}{text}{RESET}"


def tool_result_color(result: object) -> str:
    """Yellow for a normal tool result; red when dispatch returned `ERROR:`."""
    if isinstance(result, str) and result.startswith("ERROR:"):
        return COLOR_TOOL_ERROR
    return COLOR_TOOL


def _close_open_live_locked() -> None:
    """End an in-flight think/model line so a discrete log starts on its own line."""
    if _ACTIVE_LIVE is not None:
        _ACTIVE_LIVE._close_line_locked()


def log(msg: str) -> None:
    with _STDERR_LOCK:
        _close_open_live_locked()
        print(msg, file=sys.stderr, flush=True)


def write_stderr_raw(data: str, stream: TextIO | None = None) -> None:
    """Write already-decoded CLI output to stderr (or `stream`) without a newline.

    Takes the same lock as `log` / `LivePrinter` so a `[round]` banner cannot
    land in the middle of a child program's line. Does not color or prefix;
    the caller owns framing.
    """
    if not data:
        return
    with _STDERR_LOCK:
        _close_open_live_locked()
        out = stream if stream is not None else sys.stderr
        out.write(data)
        try:
            out.flush()
        except Exception:
            pass


class LivePrinter:
    """Echo streaming think/model text to stderr as it arrives.

    Discrete `log()` lines take the same lock and close an open live line
    first, so a `[round]` banner cannot land in the middle of a token.
    `prefix` (child/member name) is prepended to the channel tag:
    `[think] ` vs `[writer think] `.

    On a TTY, `think` is grey and `model` is green. `color=True/False`
    overrides auto-detect (tests pass a buffer and set this explicitly).
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        prefix: str = "",
        color: bool | None = None,
    ) -> None:
        self._stream = stream
        self.prefix = prefix
        self._color = color
        self._channel: str | None = None
        self._coloring = False
        self.channels_seen: set[str] = set()

    def _out(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stderr

    def _use_color(self) -> bool:
        if self._color is not None:
            return self._color
        return color_enabled(self._out())

    def _tag(self, channel: str) -> str:
        if self.prefix:
            return f"{self.prefix} {channel}"
        return channel

    def _close_line_locked(self) -> None:
        """Caller holds `_STDERR_LOCK`. End the current live line and reset color."""
        if self._channel is None:
            return
        out = self._out()
        if self._coloring:
            out.write(RESET)
            self._coloring = False
        out.write("\n")
        out.flush()
        self._channel = None

    def feed(self, channel: str, text: str) -> None:
        global _ACTIVE_LIVE
        if not text:
            return
        self.channels_seen.add(channel)
        with _STDERR_LOCK:
            if _ACTIVE_LIVE is not None and _ACTIVE_LIVE is not self:
                _close_open_live_locked()
            _ACTIVE_LIVE = self
            out = self._out()
            if self._channel != channel:
                if self._channel is not None:
                    self._close_line_locked()
                color = CHANNEL_COLORS.get(channel, "")
                if self._use_color() and color:
                    out.write(color)
                    self._coloring = True
                out.write(f"[{self._tag(channel)}] ")
                self._channel = channel
            out.write(text)
            out.flush()

    def end(self) -> None:
        global _ACTIVE_LIVE
        with _STDERR_LOCK:
            self._close_line_locked()
            if _ACTIVE_LIVE is self:
                _ACTIVE_LIVE = None


def get_api_key() -> str:
    global _API_KEY
    if _API_KEY is None:
        if not K_DAT_PATH.exists():
            raise SystemExit(f"missing API key file: {K_DAT_PATH}")
        key = K_DAT_PATH.read_text(encoding="utf-8").strip()
        if not key:
            raise SystemExit(f"empty API key file: {K_DAT_PATH}")
        _API_KEY = key
    return _API_KEY


# Internal 0 = thinking off (wire: reasoning.effort "none"). 1-100 = thinking-on
# budget sent as top-level integer reasoning_effort. Nested integer
# reasoning.effort is HTTP 400 and is never used.
MIN_THINKING_EFFORT = 1
MAX_THINKING_EFFORT = 100
REASONING_OFF = 0


def clamp_effort(value: int | float | str) -> int:
    """Clamp to 0-100. 0 is thinking-off; 1-100 is the thinking budget."""
    return max(REASONING_OFF, min(MAX_THINKING_EFFORT, int(value)))


def clamp_thinking_effort(value: int | float | str) -> int:
    """Clamp to the live thinking-on budget 1-100."""
    return max(MIN_THINKING_EFFORT, min(MAX_THINKING_EFFORT, int(value)))
