"""Session event log and memory store. No HTTP."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from g023v2.constants import MEMORY_ROOT
from g023v2.messages import reasoning_item
from g023v2.util import log

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]


def ensure_memory_root() -> Path:
    """Create MEMORY_ROOT with mode 0700. Not called at import time."""
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(MEMORY_ROOT, 0o700)
    except OSError:
        pass
    return MEMORY_ROOT


def project_store_id(root: Path) -> str:
    """Stable per-directory key so two folders with the same basename do not share logs."""
    resolved = Path(root).expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    name = resolved.name or "root"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-") or "root"
    return f"{safe}-{digest}"


def _flock(fh, exclusive: bool) -> None:
    if fcntl is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError:
        pass


def _funlock(fh) -> None:
    if fcntl is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class SessionLog:
    def __init__(self, path: Path, fresh: bool = False):
        self.path = path
        ensure_memory_root()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.lock = threading.Lock()
        if fresh and self.path.exists():
            self.path.unlink()

    def append(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                _flock(f, exclusive=True)
                try:
                    f.write(line)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                finally:
                    _funlock(f)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            _flock(f, exclusive=False)
            try:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        events.append(json.loads(raw))
                    except json.JSONDecodeError:
                        log("[session-log] corrupt JSON line skipped")
                        if "reasoning" in raw:
                            events.append({
                                "type": "history_item",
                                "item": reasoning_item(
                                    "[unreadable reasoning item from corrupt log line]"
                                ),
                            })
            finally:
                _funlock(f)
        return events


class MemoryStore:
    def __init__(self, project: str):
        ensure_memory_root()
        self.dir = MEMORY_ROOT / project
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.path = self.dir / "memory.json"
        self.lock = threading.Lock()
        self.revision = 0
        self.data: dict[str, str] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = {str(k): str(v) for k, v in loaded.items()}
            except json.JSONDecodeError:
                self.data = {}
        if self.data:
            self.revision = 1

    def write(self, key: str, value: str) -> str:
        with self.lock:
            self.data[key] = value
            self.revision += 1
            payload = json.dumps(self.data, indent=2, sort_keys=True)
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self.path)
        return f"stored {key}"

    def read(self, key: str) -> str:
        with self.lock:
            return self.data.get(key, "")

    def list_keys(self) -> str:
        """Stored keys only. Empty store is a distinct result; values are not listed."""
        with self.lock:
            if not self.data:
                return "(no keys stored)"
            return "\n".join(sorted(self.data.keys()))

    def snapshot(self) -> str:
        with self.lock:
            if not self.data:
                return ""
            lines = [f"- {k}: {v}" for k, v in sorted(self.data.items())]
        return "SESSION MEMORY\n" + "\n".join(lines)
