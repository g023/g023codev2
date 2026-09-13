"""Raw-mode REPL line editor: cursor, clipboard, Ctrl+Q, prompt history.

Stdlib only. When stdin/stdout are not a TTY (or TERM=dumb, or
G023V2_SIMPLE_INPUT is set), `read_repl_line` falls back to `input()`.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Sequence, TextIO

# In-process clipboard so Ctrl+C / Ctrl+V work with no system tool.
_internal_clipboard = ""

SUBMIT = "submit"
QUIT = "quit"
EOF = "eof"

_PASTE_MAX_CHARS = 1_000_000
_ESC_TIMEOUT = 0.05


@dataclass(frozen=True)
class Key:
    """One decoded keypress. `name` is `insert` when `char` should be typed."""

    name: str
    char: str = ""


@dataclass
class LineResult:
    text: str
    action: str  # submit | quit | eof


class PromptBuffer:
    """Cursor, selection, and Ctrl+Up/Down history. No terminal I/O."""

    def __init__(
        self,
        history: Sequence[str] | None = None,
        initial: str = "",
    ) -> None:
        self.history = list(history or [])
        self.buffer = initial
        self.cursor = len(initial)
        self.mark: int | None = None
        self._draft: str | None = None
        self._hist_index: int | None = None

    def selected_range(self) -> tuple[int, int] | None:
        if self.mark is None or self.mark == self.cursor:
            return None
        a, b = self.mark, self.cursor
        return (a, b) if a < b else (b, a)

    def selected_text(self) -> str:
        rng = self.selected_range()
        if rng is None:
            return self.buffer
        return self.buffer[rng[0] : rng[1]]

    def _clear_mark(self) -> None:
        self.mark = None

    def _clamp(self) -> None:
        if self.cursor < 0:
            self.cursor = 0
        elif self.cursor > len(self.buffer):
            self.cursor = len(self.buffer)

    def _replace_selection_or_insert(self, text: str) -> None:
        rng = self.selected_range()
        if rng is not None:
            a, b = rng
            self.buffer = self.buffer[:a] + text + self.buffer[b:]
            self.cursor = a + len(text)
            self.mark = None
            return
        i = self.cursor
        self.buffer = self.buffer[:i] + text + self.buffer[i:]
        self.cursor = i + len(text)

    def insert(self, text: str) -> None:
        if not text:
            return
        if len(text) > _PASTE_MAX_CHARS:
            text = text[:_PASTE_MAX_CHARS]
        text = (
            text.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", " ")
            .replace("\t", " ")
        )
        self._replace_selection_or_insert(text)

    def _line_starts(self) -> list[int]:
        starts = [0]
        for i, ch in enumerate(self.buffer):
            if ch == "\n":
                starts.append(i + 1)
        return starts

    def _row_col(self) -> tuple[int, int]:
        starts = self._line_starts()
        row = 0
        for i, s in enumerate(starts):
            if s <= self.cursor:
                row = i
        return row, self.cursor - starts[row]

    def _move_vertical(self, delta: int) -> None:
        starts = self._line_starts()
        row, col = self._row_col()
        dest = row + delta
        if dest < 0:
            self.cursor = 0
            return
        if dest >= len(starts):
            self.cursor = len(self.buffer)
            return
        start = starts[dest]
        if dest + 1 < len(starts):
            end = starts[dest + 1] - 1
        else:
            end = len(self.buffer)
        self.cursor = min(start + col, end)

    def _move_word(self, direction: int) -> None:
        i = self.cursor
        buf = self.buffer
        n = len(buf)
        if direction < 0:
            while i > 0 and buf[i - 1].isspace():
                i -= 1
            while i > 0 and not buf[i - 1].isspace():
                i -= 1
        else:
            while i < n and not buf[i].isspace():
                i += 1
            while i < n and buf[i].isspace():
                i += 1
        self.cursor = i

    def history_prev(self) -> None:
        if not self.history:
            return
        if self._hist_index is None:
            self._draft = self.buffer
            self._hist_index = len(self.history) - 1
        elif self._hist_index > 0:
            self._hist_index -= 1
        else:
            return
        self.buffer = self.history[self._hist_index]
        self.cursor = len(self.buffer)
        self.mark = None

    def history_next(self) -> None:
        if self._hist_index is None:
            return
        if self._hist_index < len(self.history) - 1:
            self._hist_index += 1
            self.buffer = self.history[self._hist_index]
        else:
            self._hist_index = None
            self.buffer = self._draft if self._draft is not None else ""
        self.cursor = len(self.buffer)
        self.mark = None

    def apply_key(
        self,
        key: Key,
        *,
        copy: Callable[[str], None] | None = None,
        paste: Callable[[], str] | None = None,
    ) -> str | None:
        """Apply one key. Returns submit/quit/eof, or None to keep editing."""
        name = key.name
        selecting = name in ("shift_left", "shift_right", "shift_home", "shift_end")

        def ensure_mark() -> None:
            if self.mark is None:
                self.mark = self.cursor

        if name == "insert":
            self.insert(key.char)
            return None
        if name == "enter":
            return SUBMIT
        if name == "ctrl_q":
            return QUIT
        if name == "ctrl_d":
            if self.buffer == "":
                return EOF
            name = "delete"
        if name == "ctrl_c":
            text = self.selected_text()
            if copy is not None:
                copy(text)
            else:
                copy_text(text)
            return None
        if name == "ctrl_v":
            chunk = paste() if paste is not None else paste_text()
            self.insert(chunk or "")
            return None
        if name == "ctrl_up":
            self.history_prev()
            return None
        if name == "ctrl_down":
            self.history_next()
            return None
        if name == "backspace":
            rng = self.selected_range()
            if rng is not None:
                a, b = rng
                self.buffer = self.buffer[:a] + self.buffer[b:]
                self.cursor = a
                self.mark = None
            elif self.cursor > 0:
                i = self.cursor
                self.buffer = self.buffer[: i - 1] + self.buffer[i:]
                self.cursor = i - 1
            return None
        if name == "delete":
            rng = self.selected_range()
            if rng is not None:
                a, b = rng
                self.buffer = self.buffer[:a] + self.buffer[b:]
                self.cursor = a
                self.mark = None
            elif self.cursor < len(self.buffer):
                i = self.cursor
                self.buffer = self.buffer[:i] + self.buffer[i + 1 :]
            return None

        if not selecting:
            self._clear_mark()
        else:
            ensure_mark()

        if name in ("left", "shift_left"):
            if self.cursor > 0:
                self.cursor -= 1
        elif name in ("right", "shift_right"):
            if self.cursor < len(self.buffer):
                self.cursor += 1
        elif name in ("up",):
            self._move_vertical(-1)
        elif name in ("down",):
            self._move_vertical(1)
        elif name in ("home", "ctrl_a", "shift_home"):
            starts = self._line_starts()
            row, _col = self._row_col()
            self.cursor = starts[row]
        elif name in ("end", "ctrl_e", "shift_end"):
            starts = self._line_starts()
            row, _col = self._row_col()
            if row + 1 < len(starts):
                self.cursor = starts[row + 1] - 1
            else:
                self.cursor = len(self.buffer)
        elif name == "ctrl_left":
            self._move_word(-1)
        elif name == "ctrl_right":
            self._move_word(1)
        self._clamp()
        return None


def copy_text(text: str, stdout: TextIO | None = None) -> None:
    """Copy to the in-process clipboard and, when possible, the system one."""
    global _internal_clipboard
    _internal_clipboard = text
    _system_copy(text)
    out = stdout if stdout is not None else sys.stdout
    _osc52_copy(text, out)


def paste_text() -> str:
    """System clipboard if a tool is present, else the in-process buffer."""
    grabbed = _system_paste()
    if grabbed is not None:
        return grabbed
    return _internal_clipboard


def _system_copy(text: str) -> bool:
    payload = text.encode("utf-8")
    candidates = (
        (["wl-copy"], payload),
        (["xclip", "-selection", "clipboard"], payload),
        (["xsel", "--clipboard", "--input"], payload),
        (["pbcopy"], payload),
    )
    for argv, data in candidates:
        if _clip_run(argv, data) is not None:
            return True
    return False


def _system_paste() -> str | None:
    candidates = (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["pbpaste"],
    )
    for argv in candidates:
        out = _clip_run(argv, None)
        if out is not None:
            return out.decode("utf-8", errors="replace")
    return None


def _clip_run(argv: list[str], data: bytes | None) -> bytes | None:
    try:
        r = subprocess.run(
            argv,
            input=data,
            capture_output=True,
            timeout=0.4,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    if data is not None:
        return b""
    return r.stdout


def _osc52_copy(text: str, stream: TextIO) -> None:
    try:
        if not stream.isatty():
            return
    except Exception:
        return
    import base64

    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    try:
        stream.write(f"\033]52;c;{b64}\007")
        stream.flush()
    except OSError:
        return


def decode_csi(params: str, final: str) -> Key | None:
    """Map a CSI sequence (`ESC [ params final`) onto a Key."""
    parts = [p for p in params.split(";") if p != ""] if params else []
    nums: list[int] = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    mod = 0
    if len(nums) >= 2:
        mod = nums[1]
    elif len(nums) == 1 and final in "ABCDHF":
        # `ESC [ 5 A` is not used; modifiers always have a semicolon
        # (`ESC [ 1 ; 5 A`). A lone number with ~ is a key id.
        pass

    def with_mod(base: str) -> str:
        # xterm: 1 none, 2 shift, 3 alt, 4 shift+alt, 5 ctrl, 6 shift+ctrl
        if mod in (5, 6):
            if base in ("up", "down", "left", "right"):
                return f"ctrl_{base}"
        if mod in (2, 4, 6):
            if base in ("left", "right"):
                return f"shift_{base}"
            if base in ("home", "end"):
                return f"shift_{base}"
        return base

    if final == "A":
        return Key(with_mod("up"))
    if final == "B":
        return Key(with_mod("down"))
    if final == "C":
        return Key(with_mod("right"))
    if final == "D":
        return Key(with_mod("left"))
    if final == "H":
        return Key(with_mod("home"))
    if final == "F":
        return Key(with_mod("end"))
    if final == "~":
        code = nums[0] if nums else 0
        if len(nums) >= 2:
            mod = nums[1]
        if code == 1 or code == 7:
            return Key(with_mod("home"))
        if code == 4 or code == 8:
            return Key(with_mod("end"))
        if code == 3:
            return Key("delete")
        if code == 200:
            return Key("paste_start")
        if code == 201:
            return Key("paste_end")
    return None


def decode_ss3(final: str) -> Key | None:
    return {
        "A": Key("up"),
        "B": Key("down"),
        "C": Key("right"),
        "D": Key("left"),
        "H": Key("home"),
        "F": Key("end"),
    }.get(final)


def keys_from_bytes(data: bytes) -> list[Key]:
    """Decode a complete byte string into keys. Incomplete ESC is dropped."""
    keys: list[Key] = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        i += 1
        if b == 0x1B:
            if i >= n:
                break
            nxt = data[i]
            i += 1
            if nxt == 0x5B:  # CSI
                params = []
                while i < n:
                    ch = data[i]
                    i += 1
                    if 0x40 <= ch <= 0x7E:
                        key = decode_csi(bytes(params).decode("ascii", "ignore"), chr(ch))
                        if key is not None:
                            keys.append(key)
                        break
                    params.append(ch)
                continue
            if nxt == 0x4F:  # SS3
                if i < n:
                    key = decode_ss3(chr(data[i]))
                    i += 1
                    if key is not None:
                        keys.append(key)
                continue
            continue
        key = _byte_to_key(b)
        if key is not None:
            if key.name == "insert" and (b & 0x80):
                # UTF-8: put the lead back and decode one character.
                i -= 1
                ch, i = _decode_utf8_at(data, i)
                if ch:
                    keys.append(Key("insert", ch))
            else:
                keys.append(key)
    return keys


def _byte_to_key(b: int) -> Key | None:
    if b in (0x0D, 0x0A):
        return Key("enter")
    if b in (0x7F, 0x08):
        return Key("backspace")
    if b == 0x03:
        return Key("ctrl_c")
    if b == 0x11:
        return Key("ctrl_q")
    if b == 0x16:
        return Key("ctrl_v")
    if b == 0x04:
        return Key("ctrl_d")
    if b == 0x01:
        return Key("ctrl_a")
    if b == 0x05:
        return Key("ctrl_e")
    if b == 0x09:
        return Key("insert", "\t")
    if 0x20 <= b <= 0x7E:
        return Key("insert", chr(b))
    if b >= 0x80:
        return Key("insert", "?")
    return None


def _decode_utf8_at(data: bytes, i: int) -> tuple[str, int]:
    if i >= len(data):
        return "", i
    lead = data[i]
    if lead < 0x80:
        return chr(lead), i + 1
    if 0xC2 <= lead <= 0xDF:
        need = 2
    elif 0xE0 <= lead <= 0xEF:
        need = 3
    elif 0xF0 <= lead <= 0xF4:
        need = 4
    else:
        return "\ufffd", i + 1
    chunk = data[i : i + need]
    if len(chunk) < need:
        return "\ufffd", len(data)
    try:
        return chunk.decode("utf-8"), i + need
    except UnicodeDecodeError:
        return "\ufffd", i + 1


def use_raw_editor(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    if os.environ.get("G023V2_SIMPLE_INPUT"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        return False
    inn = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    try:
        return bool(inn.isatty() and out.isatty())
    except Exception:
        return False


def read_repl_line(
    prompt: str = "> ",
    *,
    history: Sequence[str] | None = None,
    initial: str = "",
    stop_event: threading.Event | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> LineResult:
    """Read one submitted line. TTY: raw editor. Non-TTY: `input()`."""
    inn = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    if not use_raw_editor(inn, out):
        return _read_via_input(prompt, inn)
    return _read_raw(
        prompt,
        history=history,
        initial=initial,
        stop_event=stop_event,
        stdin=inn,
        stdout=out,
    )


def _read_via_input(prompt: str, stdin: TextIO) -> LineResult:
    try:
        # input() uses sys.stdin; honour a replaced stdin when possible.
        if stdin is sys.stdin:
            text = input(prompt)
        else:
            out = sys.stdout
            out.write(prompt)
            out.flush()
            text = stdin.readline()
            if text == "":
                return LineResult("", EOF)
            if text.endswith("\n"):
                text = text[:-1]
        return LineResult(text, SUBMIT)
    except EOFError:
        return LineResult("", EOF)
    except KeyboardInterrupt:
        return LineResult("", EOF)


def _read_raw(
    prompt: str,
    *,
    history: Sequence[str] | None,
    initial: str,
    stop_event: threading.Event | None,
    stdin: TextIO,
    stdout: TextIO,
) -> LineResult:
    import termios
    import tty

    fd = stdin.fileno()
    buf = PromptBuffer(history=history, initial=initial)
    old = termios.tcgetattr(fd)
    prev_rows = 1
    try:
        tty.setraw(fd, when=termios.TCSANOW)
        try:
            stdout.write("\033[?2004h")
            stdout.flush()
        except OSError:
            pass
        _render(stdout, prompt, buf, prev_rows)
        prev_rows = _occupied_rows(prompt, buf.buffer)
        paste_acc: list[str] | None = None
        while True:
            if stop_event is not None and stop_event.is_set():
                _finish_line(stdout)
                return LineResult(buf.buffer, EOF)
            key = _read_one_key(fd)
            if key is None:
                continue
            if paste_acc is not None:
                if key.name == "paste_end":
                    buf.insert("".join(paste_acc))
                    paste_acc = None
                elif key.name == "insert":
                    paste_acc.append(key.char)
                elif key.name == "enter":
                    paste_acc.append("\n")
                elif key.name == "paste_start":
                    pass
                else:
                    buf.insert("".join(paste_acc))
                    paste_acc = None
                    action = buf.apply_key(key)
                    if action:
                        _finish_line(stdout)
                        return LineResult(buf.buffer, action)
                prev_rows = _redraw(stdout, prompt, buf, prev_rows)
                continue
            if key.name == "paste_start":
                paste_acc = []
                continue
            action = buf.apply_key(key)
            if action:
                _finish_line(stdout)
                return LineResult(buf.buffer, action)
            prev_rows = _redraw(stdout, prompt, buf, prev_rows)
    except OSError:
        _finish_line(stdout)
        return LineResult(buf.buffer, EOF)
    finally:
        try:
            stdout.write("\033[?2004l")
            stdout.flush()
        except OSError:
            pass
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except termios.error:
            pass


def _occupied_rows(prompt: str, text: str) -> int:
    cols = max(8, shutil.get_terminal_size(fallback=(80, 24)).columns)
    total = _display_len(prompt) + _display_len(text)
    if total == 0:
        return 1
    return max(1, (total // cols) + (1 if total % cols else 0))


def _display_len(text: str) -> int:
    return sum(1 if ch != "\n" else 1 for ch in text)


def _redraw(stdout: TextIO, prompt: str, buf: PromptBuffer, prev_rows: int) -> int:
    try:
        cols = max(8, shutil.get_terminal_size(fallback=(80, 24)).columns)
        if prev_rows > 1:
            stdout.write(f"\033[{prev_rows - 1}A")
        stdout.write("\r\033[J")
        body = prompt + buf.buffer
        stdout.write(body)
        total = _display_len(prompt) + _display_len(buf.buffer)
        cur = _display_len(prompt) + _display_len(buf.buffer[: buf.cursor])
        end_row, _end_col = divmod(total, cols)
        cur_row, cur_col = divmod(cur, cols)
        if total > 0 and total % cols == 0:
            # Cursor wrapped to a new blank line after the last column.
            end_row = total // cols
        up = end_row - cur_row
        if up > 0:
            stdout.write(f"\033[{up}A")
        stdout.write("\r")
        if cur_col > 0:
            stdout.write(f"\033[{cur_col}C")
        stdout.flush()
        return _occupied_rows(prompt, buf.buffer)
    except OSError:
        return prev_rows


def _render(stdout: TextIO, prompt: str, buf: PromptBuffer, prev_rows: int) -> None:
    _redraw(stdout, prompt, buf, prev_rows)


def _finish_line(stdout: TextIO) -> None:
    try:
        stdout.write("\r\n")
        stdout.flush()
    except OSError:
        pass


def _read_one_key(fd: int) -> Key | None:
    b = _read_byte(fd)
    if b is None:
        return None
    if b == b"":
        return Key("ctrl_d")
    byte = b[0]
    if byte != 0x1B:
        if byte < 0x80:
            return _byte_to_key(byte)
        extra = _utf8_need(byte) - 1
        chunk = bytearray(b)
        for _ in range(max(0, extra)):
            nxt = _read_byte(fd, timeout=_ESC_TIMEOUT)
            if not nxt:
                break
            chunk.extend(nxt)
        try:
            return Key("insert", bytes(chunk).decode("utf-8"))
        except UnicodeDecodeError:
            return Key("insert", "\ufffd")
    nxt = _read_byte(fd, timeout=_ESC_TIMEOUT)
    if not nxt:
        return None
    lead = nxt[0]
    if lead == 0x5B:
        params = bytearray()
        while True:
            chb = _read_byte(fd, timeout=_ESC_TIMEOUT)
            if not chb:
                return None
            ch = chb[0]
            if 0x40 <= ch <= 0x7E:
                return decode_csi(params.decode("ascii", "ignore"), chr(ch))
            params.append(ch)
            if len(params) > 32:
                return None
        return None
    if lead == 0x4F:
        fin = _read_byte(fd, timeout=_ESC_TIMEOUT)
        if not fin:
            return None
        return decode_ss3(chr(fin[0]))
    return None


def _utf8_need(lead: int) -> int:
    if lead < 0x80:
        return 1
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 1


def _read_byte(fd: int, timeout: float | None = None) -> bytes | None:
    try:
        if timeout is not None:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return None
        data = os.read(fd, 1)
        return data
    except InterruptedError:
        return None
    except OSError:
        return b""
