"""Global URL-response cache. Shared across projects; freshness is the caller's choice."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from g023v2.constants import URL_CACHE_DIR
from g023v2.persist import ensure_memory_root

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]


def _url_key(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


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


class UrlCache:
    """JSON files under a cache root, keyed by sha256(url). Never expires on its own."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else URL_CACHE_DIR
        self.lock = threading.Lock()

    def _ensure_root(self) -> None:
        if self.root == URL_CACHE_DIR:
            ensure_memory_root()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def _path_for(self, url: str) -> Path:
        digest = _url_key(url)
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, url: str, *, touch: bool = True) -> dict[str, Any] | None:
        """Return the stored response for url, or None. Age is not filtered here."""
        if not isinstance(url, str) or not url.strip():
            return None
        path = self._path_for(url)
        with self.lock:
            if not path.is_file():
                return None
            try:
                with path.open("r", encoding="utf-8") as f:
                    _flock(f, exclusive=False)
                    try:
                        raw = json.loads(f.read())
                    finally:
                        _funlock(f)
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(raw, dict) or "body" not in raw:
                return None
            fetched_at = float(raw.get("fetched_at") or 0.0)
            record = {
                "url": str(raw.get("url") or url.strip()),
                "final_url": str(raw.get("final_url") or raw.get("url") or url.strip()),
                "status": int(raw.get("status") or 0),
                "headers": raw.get("headers") if isinstance(raw.get("headers"), dict) else {},
                "body": str(raw.get("body") or ""),
                "engine": str(raw.get("engine") or ""),
                "fetched_at": fetched_at,
                "accessed_at": float(raw.get("accessed_at") or fetched_at),
                "hit_count": int(raw.get("hit_count") or 0),
                "truncated": bool(raw.get("truncated")),
                "age_seconds": max(0.0, time.time() - fetched_at),
            }
            if touch:
                record["hit_count"] = record["hit_count"] + 1
                record["accessed_at"] = time.time()
                stored = {k: v for k, v in record.items() if k != "age_seconds"}
                self._write_unlocked(path, stored)
            return record

    def put(
        self,
        url: str,
        *,
        body: str,
        status: int,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
        engine: str = "",
        truncated: bool = False,
        fetched_at: float | None = None,
    ) -> None:
        url = url.strip()
        now = time.time() if fetched_at is None else float(fetched_at)
        record = {
            "url": url,
            "final_url": (final_url or url),
            "status": int(status),
            "headers": {str(k).lower(): str(v) for k, v in (headers or {}).items()},
            "body": body,
            "engine": engine,
            "fetched_at": now,
            "accessed_at": now,
            "hit_count": 0,
            "truncated": bool(truncated),
        }
        path = self._path_for(url)
        with self.lock:
            self._write_unlocked(path, record)

    def touch(self, url: str) -> None:
        self.get(url, touch=True)

    def clear(self, url: str | None = None) -> int:
        """Delete one URL, or every file under the cache root. Returns files removed."""
        with self.lock:
            if url:
                path = self._path_for(url)
                if path.is_file():
                    try:
                        path.unlink()
                        return 1
                    except OSError:
                        return 0
                return 0
            if not self.root.is_dir():
                return 0
            removed = 0
            for path in self.root.rglob("*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
            return removed

    def _write_unlocked(self, path: Path, record: dict[str, Any]) -> None:
        self._ensure_root()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(record, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


_global: UrlCache | None = None
_global_lock = threading.Lock()


def get_url_cache() -> UrlCache:
    """Process-wide cache used by every project. Created on first use."""
    global _global
    with _global_lock:
        if _global is None:
            _global = UrlCache()
        return _global


def reset_url_cache_singleton() -> None:
    """Drop the process singleton. Tests use this after pointing URL_CACHE_DIR elsewhere."""
    global _global
    with _global_lock:
        _global = None
