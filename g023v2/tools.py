"""Project file tools with path sandbox, backup-before-mutate, and restore."""

from __future__ import annotations

import fnmatch
import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, TextIO
from urllib.parse import urlparse

from g023v2.constants import (
    APPLY_EDITS_MAX_HUNKS,
    BINARY_EXTENSIONS,
    BINARY_HEADER_BYTES,
    CLI_FAILURE_TAIL_CHARS,
    CLI_LOG_MAX_BYTES,
    CLI_TIMEOUT_DEFAULT,
    CLI_TIMEOUT_MAX,
    CLI_WANT_OUTPUT_CHARS,
    FETCH_CACHE_MAX_AGE_DEFAULT,
    FETCH_CACHE_MODES,
    FETCH_EXTRACT_MODES,
    FETCH_MAX_BYTES,
    FETCH_MAX_CHARS,
    FETCH_MAX_CHARS_CAP,
    FETCH_TIMEOUT_DEFAULT,
    FETCH_TIMEOUT_MAX,
    FIND_FILES_MAX_RESULTS,
    FIND_FILES_MAX_RESULTS_CAP,
    G023_DIR_NAME,
    GREP_MAX_RESULTS_CAP,
    GREP_SKIP_DIRS,
    LEGACY_BACKUP_DIR_NAME,
    LIST_DIR_MAX_ENTRIES,
    READ_FILE_MAX_BYTES,
    READ_FILE_MAX_LINES,
    READ_FILES_MAX_PATHS,
    READ_FILES_MAX_PATHS_CAP,
    REPLACE_IN_FILES_MAX_FILES,
    REPLACE_IN_FILES_MAX_FILES_CAP,
    SHELL_OUTPUT_MAX_BYTES,
    SHELL_TIMEOUT_DEFAULT,
    SHELL_TIMEOUT_MAX,
)
from g023v2.images import prepare_image_part, sniff_image_mime
from g023v2.persist import MemoryStore
from g023v2.url_cache import UrlCache, get_url_cache
from g023v2.url_fetch import FetchError, FetchResult, extract_page, http_get
from g023v2.util import COLOR_CLI, log, paint, write_stderr_raw
from g023v2.workdir import (
    BACKUPS_DIR,
    FileLedger,
    allocate_stamp,
    cli_log_rel,
    ensure_g023_tree,
    g023_root,
)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, path)
        # mkstemp creates 0600 files; source/HTML the model writes must stay
        # group/other-readable (public_html, git checkouts, shared projects).
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


def _atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class ProjectTools:
    def __init__(
        self,
        root: Path,
        memory: MemoryStore,
        url_cache: UrlCache | None = None,
    ):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory = memory
        self.stamp: str | None = None
        self.backup_root: Path | None = None
        self._stamp_lock = threading.Lock()
        self.ledger = FileLedger()
        self.url_cache = url_cache if url_cache is not None else get_url_cache()
        self._cli_lock = threading.Lock()
        self._cli_n = 0

    def _g023_root(self) -> Path:
        return g023_root(self.root).resolve()

    def _legacy_backup_root(self) -> Path:
        return (self.root / LEGACY_BACKUP_DIR_NAME).resolve()

    def _scratch_root(self) -> Path:
        return (self.root / G023_DIR_NAME / "scratch").resolve()

    def set_stamp(self, stamp: str | None, *, reset_ledger: bool = False) -> None:
        """Bind backups and scratch to `stamp`. None clears the current stamp."""
        with self._stamp_lock:
            self._apply_stamp(stamp, reset_ledger=reset_ledger)

    def _apply_stamp(self, stamp: str | None, *, reset_ledger: bool) -> None:
        if stamp:
            ensure_g023_tree(self.root)
            backup_root = (
                self.root / G023_DIR_NAME / BACKUPS_DIR / stamp
            ).resolve()
            scratch = self.root / G023_DIR_NAME / "scratch" / stamp
            scratch.mkdir(parents=True, exist_ok=True)
            self.backup_root = backup_root
            self.stamp = stamp
        else:
            self.backup_root = None
            self.stamp = None
        if reset_ledger:
            self.ledger = FileLedger()

    def _ensure_stamp(self) -> str:
        with self._stamp_lock:
            if not self.stamp:
                self._apply_stamp(allocate_stamp(self.root), reset_ledger=False)
            assert self.stamp is not None
            return self.stamp

    def _rel_posix(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not _is_under(p, self.root):
            raise ValueError(f"path escapes project root: {rel}")
        return p

    def _is_scratch(self, p: Path) -> bool:
        scratch = self._scratch_root()
        return p == scratch or _is_under(p, scratch)

    def _protected_reason(self, p: Path, *, write: bool) -> str | None:
        """Error fragment if `p` is off-limits to file tools, else None."""
        legacy = self._legacy_backup_root()
        if p == legacy or _is_under(p, legacy):
            return "path is inside the backup store"
        if self.backup_root is not None and (
            p == self.backup_root or _is_under(p, self.backup_root)
        ):
            return "path is inside the backup store"
        g023 = self._g023_root()
        if p == g023:
            if write:
                return "path is inside the harness store"
            return None
        if _is_under(p, g023):
            if self._is_scratch(p):
                return None
            rel = p.relative_to(g023)
            top = rel.parts[0] if rel.parts else ""
            if top == BACKUPS_DIR:
                return "path is inside the backup store"
            return "path is inside the harness store"
        return None

    def _in_backup_store(self, p: Path) -> bool:
        reason = self._protected_reason(p, write=True)
        return reason is not None and "backup store" in reason

    def _open_project_path(self, rel: str, *, write: bool = False) -> Path:
        p = self._resolve(rel)
        reason = self._protected_reason(p, write=write)
        if reason:
            raise ValueError(f"{reason}: {rel}")
        return p

    def backup_path_for(self, p: Path) -> Path:
        self._ensure_stamp()
        backup_root = self.backup_root
        if backup_root is None:
            with self._stamp_lock:
                backup_root = self.backup_root
        if backup_root is None:
            raise RuntimeError("backup stamp was not allocated")
        rel = p.resolve().relative_to(self.root)
        return backup_root / rel

    def _backup_existing(self, p: Path) -> Path | None:
        if not p.is_file():
            return None
        dest = self.backup_path_for(p)
        _atomic_write_bytes(dest, p.read_bytes())
        return dest

    def last_backup_path(self, rel: str) -> Path | None:
        try:
            p = self._open_project_path(rel)
        except ValueError:
            return None
        rel_path = p.resolve().relative_to(self.root)
        if self.backup_root is not None:
            dest = self.backup_root / rel_path
            if dest.is_file():
                return dest
        backups_parent = self.root / G023_DIR_NAME / BACKUPS_DIR
        if backups_parent.is_dir():
            try:
                stamps = sorted(
                    (d for d in backups_parent.iterdir() if d.is_dir()),
                    key=lambda d: d.name,
                    reverse=True,
                )
            except OSError:
                stamps = []
            for d in stamps:
                cand = d / rel_path
                if cand.is_file():
                    return cand
        legacy = self._legacy_backup_root() / rel_path
        if legacy.is_file():
            return legacy
        return None

    def _restore_from_backup(self, p: Path) -> None:
        rel = self._rel_posix(p)
        dest = self.last_backup_path(rel)
        if dest is None or not dest.is_file():
            raise FileNotFoundError(f"no backup for {p}")
        _atomic_write_bytes(p, dest.read_bytes())

    def _is_binary(self, p: Path) -> bool:
        if p.suffix.lower() in BINARY_EXTENSIONS:
            return True
        try:
            with p.open("rb") as f:
                chunk = f.read(BINARY_HEADER_BYTES)
        except OSError:
            return True
        return b"\x00" in chunk

    def read_file(self, path: str, offset: int = 1, limit: int = 400) -> str:
        try:
            p = self._open_project_path(path)
        except ValueError as e:
            return f"ERROR: {e}"
        if not p.exists():
            return f"ERROR: no such file: {path}"
        if p.is_dir():
            return f"ERROR: {path} is a directory, not a file"
        if self._is_binary(p):
            return f"ERROR: {path} appears to be binary"
        size = p.stat().st_size
        if size > READ_FILE_MAX_BYTES:
            return f"ERROR: {path} is {size} bytes, exceeding the {READ_FILE_MAX_BYTES}-byte limit"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        try:
            offset_i = int(offset)
        except (TypeError, ValueError):
            offset_i = 1
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = 400
        if limit_i < 1:
            limit_i = 1
        limit_i = min(limit_i, READ_FILE_MAX_LINES)
        start = max(0, offset_i - 1)
        chunk = lines[start : start + limit_i]
        numbered = [f"{start + i + 1:>5}\t{line}" for i, line in enumerate(chunk)]
        header = f"{path} ({len(lines)} lines, showing {start + 1}-{start + len(chunk)})"
        return header + "\n" + "\n".join(numbered)

    def read_files(
        self,
        paths: Any,
        offset: int = 1,
        limit: int = 400,
    ) -> str:
        """Read several text files. Same jail and caps as read_file."""
        if isinstance(paths, str):
            items = [paths]
        elif isinstance(paths, (list, tuple)):
            items = [str(p) for p in paths]
        else:
            return "ERROR: paths must be a list of project-relative files"
        items = [p.strip() for p in items if str(p).strip()]
        if not items:
            return "ERROR: paths is empty"
        if len(items) > READ_FILES_MAX_PATHS_CAP:
            return (
                f"ERROR: too many paths ({len(items)}); "
                f"max {READ_FILES_MAX_PATHS_CAP}"
            )
        if len(items) > READ_FILES_MAX_PATHS:
            return (
                f"ERROR: too many paths ({len(items)}); "
                f"max {READ_FILES_MAX_PATHS}"
            )
        chunks = [self.read_file(p, offset=offset, limit=limit) for p in items]
        return "\n\n".join(chunks)

    def write_file(self, path: str, content: str) -> str:
        try:
            p = self._open_project_path(path, write=True)
        except ValueError as e:
            return f"ERROR: {e}"
        existed = p.is_file()
        if existed:
            try:
                self._backup_existing(p)
            except OSError as e:
                return f"ERROR: backup failed for {path}: {e}"
        try:
            _atomic_write_text(p, content)
        except OSError as e:
            if existed:
                try:
                    self._restore_from_backup(p)
                    return (
                        f"ERROR: write failed ({e}); restored previous content of {path}"
                    )
                except OSError as e2:
                    return f"ERROR: write failed ({e}); restore also failed ({e2})"
            return f"ERROR: write failed: {e}"
        rel = self._rel_posix(p)
        if existed:
            self.ledger.note_modified(rel)
        else:
            self.ledger.note_created(rel)
        return f"wrote {len(content)} bytes to {path}"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        try:
            p = self._open_project_path(path, write=True)
        except ValueError as e:
            return f"ERROR: {e}"
        if not p.exists():
            return f"ERROR: no such file: {path}"
        if not p.is_file():
            return f"ERROR: {path} is not a file"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return f"ERROR: {e}"
        count = text.count(old_string)
        if count == 0:
            return f"ERROR: old_string not found in {path}"
        if count > 1:
            return f"ERROR: old_string appears {count} times in {path}; make it unique"
        new_text = text.replace(old_string, new_string, 1)
        try:
            self._backup_existing(p)
        except OSError as e:
            return f"ERROR: backup failed for {path}: {e}"
        try:
            _atomic_write_text(p, new_text)
        except OSError as e:
            try:
                self._restore_from_backup(p)
                return f"ERROR: edit failed ({e}); restored previous content of {path}"
            except OSError as e2:
                return f"ERROR: edit failed ({e}); restore also failed ({e2})"
        self.ledger.note_modified(self._rel_posix(p))
        return f"edited {path}"

    def apply_edits(self, path: str, edits: Any) -> str:
        """Apply ordered unique replacements in one file. One backup."""
        try:
            p = self._open_project_path(path, write=True)
        except ValueError as e:
            return f"ERROR: {e}"
        if not p.exists():
            return f"ERROR: no such file: {path}"
        if not p.is_file():
            return f"ERROR: {path} is not a file"
        hunks = edits
        if isinstance(hunks, str):
            try:
                hunks = json.loads(hunks)
            except json.JSONDecodeError as e:
                return f"ERROR: edits is not valid JSON: {e}"
        if not isinstance(hunks, list) or not hunks:
            return "ERROR: edits must be a non-empty list"
        if len(hunks) > APPLY_EDITS_MAX_HUNKS:
            return (
                f"ERROR: too many edits ({len(hunks)}); max {APPLY_EDITS_MAX_HUNKS}"
            )
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return f"ERROR: {e}"
        current = text
        for i, hunk in enumerate(hunks, 1):
            if not isinstance(hunk, dict):
                return f"ERROR: edit {i} is not an object with old_string/new_string"
            old = hunk.get("old_string")
            new = hunk.get("new_string")
            if not isinstance(old, str) or old == "":
                return f"ERROR: old_string is empty (edit {i})"
            if not isinstance(new, str):
                return f"ERROR: new_string is required (edit {i})"
            count = current.count(old)
            if count == 0:
                return f"ERROR: old_string not found in {path} (edit {i})"
            if count > 1:
                return (
                    f"ERROR: old_string appears {count} times in {path} "
                    f"(edit {i}); make it unique"
                )
            current = current.replace(old, new, 1)
        try:
            self._backup_existing(p)
        except OSError as e:
            return f"ERROR: backup failed for {path}: {e}"
        try:
            _atomic_write_text(p, current)
        except OSError as e:
            try:
                self._restore_from_backup(p)
                return f"ERROR: apply_edits failed ({e}); restored previous content of {path}"
            except OSError as e2:
                return f"ERROR: apply_edits failed ({e}); restore also failed ({e2})"
        self.ledger.note_modified(self._rel_posix(p))
        return f"applied {len(hunks)} edit(s) in {path}"

    def replace_all(self, path: str, old_string: str, new_string: str) -> str:
        """Replace every occurrence of old_string. Backs up first like edit_file."""
        try:
            p = self._open_project_path(path, write=True)
        except ValueError as e:
            return f"ERROR: {e}"
        if not p.exists():
            return f"ERROR: no such file: {path}"
        if not p.is_file():
            return f"ERROR: {path} is not a file"
        if old_string == "":
            return "ERROR: old_string is empty"
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return f"ERROR: {e}"
        count = text.count(old_string)
        if count == 0:
            return f"ERROR: old_string not found in {path}"
        new_text = text.replace(old_string, new_string)
        try:
            self._backup_existing(p)
        except OSError as e:
            return f"ERROR: backup failed for {path}: {e}"
        try:
            _atomic_write_text(p, new_text)
        except OSError as e:
            try:
                self._restore_from_backup(p)
                return f"ERROR: replace_all failed ({e}); restored previous content of {path}"
            except OSError as e2:
                return f"ERROR: replace_all failed ({e}); restore also failed ({e2})"
        self.ledger.note_modified(self._rel_posix(p))
        return f"replaced {count} occurrence(s) in {path}"

    def replace_in_files(
        self,
        glob: str,
        old_string: str,
        new_string: str,
        path: str = ".",
        max_files: int = REPLACE_IN_FILES_MAX_FILES,
    ) -> str:
        """Replace old_string in every matching text file. Per-file backup."""
        if not isinstance(glob, str) or not glob.strip():
            return "ERROR: glob is required"
        glob = glob.strip()
        if not isinstance(old_string, str) or old_string == "":
            return "ERROR: old_string is empty"
        if not isinstance(new_string, str):
            return "ERROR: new_string is required"
        try:
            base = self._open_project_path(path)
        except ValueError as e:
            return f"ERROR: {e}"
        try:
            cap = int(max_files)
        except (TypeError, ValueError):
            cap = REPLACE_IN_FILES_MAX_FILES
        if cap < 1:
            cap = REPLACE_IN_FILES_MAX_FILES
        cap = min(cap, REPLACE_IN_FILES_MAX_FILES_CAP)
        if base.is_file():
            files: Iterator[Path] = iter([base])
        elif base.is_dir():
            files = self._walk_files(base)
        else:
            return f"ERROR: no such path: {path}"
        candidates: list[tuple[Path, str, int]] = []
        truncated = False
        for f in files:
            if not f.is_file():
                continue
            if not self._glob_matches(f, glob):
                continue
            if self._is_binary(f):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            count = text.count(old_string)
            if count == 0:
                continue
            candidates.append((f, text, count))
            if len(candidates) >= cap:
                truncated = True
                break
        if not candidates:
            return (
                f"ERROR: old_string not found in files matching {glob} under {path}"
            )
        changed: list[str] = []
        total = 0
        for f, text, count in candidates:
            rel = self._rel_posix(f)
            try:
                p = self._open_project_path(rel, write=True)
            except ValueError as e:
                return f"ERROR: {e}"
            new_text = text.replace(old_string, new_string)
            try:
                self._backup_existing(p)
            except OSError as e:
                return f"ERROR: backup failed for {rel}: {e}"
            try:
                _atomic_write_text(p, new_text)
            except OSError as e:
                try:
                    self._restore_from_backup(p)
                    return (
                        f"ERROR: replace_in_files failed ({e}); "
                        f"restored previous content of {rel}"
                    )
                except OSError as e2:
                    return (
                        f"ERROR: replace_in_files failed ({e}); "
                        f"restore also failed ({e2})"
                    )
            self.ledger.note_modified(rel)
            changed.append(f"{rel} ({count})")
            total += count
        noun = "file" if len(changed) == 1 else "files"
        occ = "occurrence" if total == 1 else "occurrences"
        lines = [
            f"replaced {total} {occ} across {len(changed)} {noun}:"
        ]
        lines.extend(changed)
        if truncated:
            lines.append(f"(truncated at {cap} files)")
        return "\n".join(lines)

    def read_image(self, path: str) -> str | list[dict[str, Any]]:
        """Return vision input_image parts for a jailed project image."""
        if not isinstance(path, str) or not path.strip():
            return "ERROR: path is required"
        path = path.strip()
        if path.startswith(("http://", "https://", "data:")):
            return "ERROR: read_image only accepts a local project path"
        try:
            p = self._open_project_path(path)
        except ValueError as e:
            return f"ERROR: {e}"
        part = prepare_image_part(str(p), project_root=self.root)
        if not isinstance(part, dict) or part.get("type") != "input_image":
            text = ""
            if isinstance(part, dict):
                text = str(part.get("text") or "").strip()
            if text.startswith("["):
                text = text[1:-1] if text.endswith("]") else text
            if not text:
                text = f"could not ingest image: {path}"
            if not text.startswith("ERROR:"):
                text = f"ERROR: {text}"
            return text
        url = str(part.get("image_url") or "")
        mime = "image"
        if url.startswith("data:") and ";" in url:
            mime = url.split(";", 1)[0].split(":", 1)[-1]
        elif url.startswith(("http://", "https://")):
            mime = "remote"
        else:
            try:
                raw = p.read_bytes()
            except OSError:
                raw = b""
            mime = sniff_image_mime(raw) or mime
        return [
            {"type": "input_text", "text": f"image {path} ({mime})"},
            part,
        ]

    def restore_file(self, path: str) -> str:
        try:
            p = self._open_project_path(path, write=True)
        except ValueError as e:
            return f"ERROR: {e}"
        dest = self.last_backup_path(path)
        if dest is None or not dest.is_file():
            return f"ERROR: no backup for {path}"
        try:
            self._restore_from_backup(p)
        except OSError as e:
            return f"ERROR: restore failed for {path}: {e}"
        self.ledger.note_modified(self._rel_posix(p))
        return f"restored {path} from last backup"

    def list_dir(self, path: str = ".") -> str:
        try:
            p = self._open_project_path(path)
        except ValueError as e:
            return f"ERROR: {e}"
        if not p.exists():
            return f"ERROR: no such directory: {path}"
        if not p.is_dir():
            return f"ERROR: {path} is not a directory"
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        total = len(entries)
        out = []
        for e in entries[:LIST_DIR_MAX_ENTRIES]:
            suffix = "/" if e.is_dir() else ""
            out.append(f"{e.name}{suffix}")
        if not out:
            return "(empty)"
        if total > LIST_DIR_MAX_ENTRIES:
            out.append(f"(truncated at {LIST_DIR_MAX_ENTRIES} of {total} entries)")
        return "\n".join(out)

    def _walk_files(self, base: Path) -> Iterator[Path]:
        for root, dirs, files in os.walk(base):
            root_path = Path(root)
            keep: list[str] = []
            for d in dirs:
                if d in GREP_SKIP_DIRS or d.startswith("."):
                    continue
                child = (root_path / d).resolve()
                if self._protected_reason(child, write=False) is not None:
                    continue
                keep.append(d)
            dirs[:] = keep
            for name in files:
                yield root_path / name

    def _glob_matches(self, f: Path, glob: str) -> bool:
        if not glob:
            return True
        if fnmatch.fnmatch(f.name, glob):
            return True
        try:
            rel = f.relative_to(self.root).as_posix()
        except ValueError:
            rel = f.as_posix()
        return fnmatch.fnmatch(rel, glob)

    def grep(self, pattern: str, path: str = ".", glob: str = "", max_results: int = 100) -> str:
        try:
            base = self._open_project_path(path)
        except ValueError as e:
            return f"ERROR: {e}"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"ERROR: invalid regex: {e}"
        try:
            cap = int(max_results)
        except (TypeError, ValueError):
            cap = 100
        if cap < 1:
            cap = 100
        cap = min(cap, GREP_MAX_RESULTS_CAP)
        hits: list[str] = []
        if base.is_file():
            files: Iterator[Path] = iter([base])
        elif base.is_dir():
            files = self._walk_files(base)
        else:
            return f"ERROR: no such path: {path}"
        for f in files:
            if glob and not self._glob_matches(f, glob):
                continue
            if self._is_binary(f):
                continue
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if regex.search(line):
                            try:
                                rel = f.relative_to(self.root)
                            except ValueError:
                                rel = f
                            hits.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(hits) >= cap:
                                return "\n".join(hits) + f"\n(truncated at {cap})"
            except OSError:
                continue
        return "\n".join(hits) if hits else "(no matches)"

    def find_files(self, glob: str, path: str = ".", max_results: int = FIND_FILES_MAX_RESULTS) -> str:
        """Recursively list project files matching a name glob. Path-jailed like grep."""
        if not isinstance(glob, str) or not glob.strip():
            return "ERROR: glob is required"
        glob = glob.strip()
        try:
            base = self._open_project_path(path)
        except ValueError as e:
            return f"ERROR: {e}"
        try:
            cap = int(max_results)
        except (TypeError, ValueError):
            cap = FIND_FILES_MAX_RESULTS
        if cap < 1:
            cap = FIND_FILES_MAX_RESULTS
        cap = min(cap, FIND_FILES_MAX_RESULTS_CAP)
        if base.is_file():
            files: Iterator[Path] = iter([base])
        elif base.is_dir():
            files = self._walk_files(base)
        else:
            return f"ERROR: no such path: {path}"
        hits: list[str] = []
        truncated = False
        for f in files:
            if not f.is_file():
                continue
            if not self._glob_matches(f, glob):
                continue
            try:
                rel = f.relative_to(self.root).as_posix()
            except ValueError:
                continue
            hits.append(rel)
            if len(hits) >= cap:
                truncated = True
                break
        hits.sort()
        if not hits:
            return "(no matches)"
        if truncated:
            hits.append(f"(truncated at {cap})")
        return "\n".join(hits)

    def fetch_url(
        self,
        url: str,
        timeout: int = FETCH_TIMEOUT_DEFAULT,
        cache_mode: str = "auto",
        max_age: int = FETCH_CACHE_MAX_AGE_DEFAULT,
        extract: str = "text",
        max_chars: int = FETCH_MAX_CHARS,
    ) -> str:
        """GET one http(s) URL as readable text, using the global URL cache.

        cache_mode:
          auto  — serve a stored copy younger than max_age, else hit the network
          cache — only the store; miss tells the model to re-call with fresh
          fresh — always hit the network and replace the stored copy
        """
        if not isinstance(url, str) or not url.strip():
            return "ERROR: url is required"
        url = url.strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            scheme = parsed.scheme or "no scheme"
            return f"ERROR: only http and https URLs are allowed (got {scheme})"
        if not parsed.netloc:
            return "ERROR: url is missing a host"
        clamped = clamp_fetch_timeout(timeout)
        if isinstance(clamped, str):
            return clamped
        mode = clamp_fetch_cache_mode(cache_mode)
        if isinstance(mode, str) and mode.startswith("ERROR:"):
            return mode
        age_limit = clamp_fetch_max_age(max_age)
        if isinstance(age_limit, str):
            return age_limit
        extract_mode = clamp_fetch_extract(extract)
        if isinstance(extract_mode, str) and extract_mode.startswith("ERROR:"):
            return extract_mode
        char_cap = clamp_fetch_max_chars(max_chars)
        if isinstance(char_cap, str):
            return char_cap

        cached = self.url_cache.get(url, touch=False)
        if cached and (
            mode == "cache"
            or (mode == "auto" and cached["age_seconds"] <= age_limit)
        ):
            self.url_cache.touch(url)
            self.ledger.note_fetch_url()
            return _render_fetch(_result_from_cache(cached), extract_mode, char_cap)

        if mode == "cache":
            return (
                f"ERROR: cache miss for {url}. "
                "Re-call with cache_mode='fresh' to fetch it."
            )

        try:
            result = http_get(url, clamped)
        except FetchError as e:
            msg = str(e)
            if msg.startswith("fetch timed out"):
                out = f"ERROR: {msg}"
            elif msg.startswith("fetch failed:"):
                out = f"ERROR: {msg}"
            else:
                out = f"ERROR: fetch failed: {e}"
            if cached:
                age = int(cached["age_seconds"])
                out += (
                    f" A cached copy exists (age={age}s). "
                    "Re-call with cache_mode='cache' to use it."
                )
            return out

        if "\x00" in result.body:
            ctype = result.headers.get("content-type") or "unknown"
            return f"ERROR: response is binary ({ctype}; {len(result.body.encode('utf-8'))} bytes)"
        if result.status >= 400:
            reason = result.body.strip().splitlines()[0][:120] if result.body.strip() else "error"
            out = f"ERROR: HTTP {result.status} fetching {url}: {reason}"
            if cached:
                age = int(cached["age_seconds"])
                out += (
                    f" A cached copy exists (age={age}s). "
                    "Re-call with cache_mode='cache' to use it."
                )
            return out

        self.url_cache.put(
            url,
            body=result.body,
            status=result.status,
            headers=result.headers,
            final_url=result.final_url,
            engine=result.engine,
            truncated=result.truncated,
        )
        result.from_cache = False
        result.age_seconds = 0.0
        result.fetched_at = time.time()
        self.ledger.note_fetch_url()
        return _render_fetch(result, extract_mode, char_cap)

    def run_shell(self, command: str, timeout: int = SHELL_TIMEOUT_DEFAULT) -> str:
        """Run a command in the project cwd. Unsandboxed; path jail does not apply.

        Bounded: timeout is clamped, the process group is killed on timeout or
        output cap, and captured stdout/stderr are truncated.
        """
        clamped = clamp_shell_timeout(timeout)
        if isinstance(clamped, str):
            return clamped
        timeout_s = clamped
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            return f"ERROR: failed to start command: {e}"
        stdout, stderr, reason = _communicate_capped(
            proc, timeout_s, SHELL_OUTPUT_MAX_BYTES
        )
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        if reason == "timeout":
            return f"ERROR: command timed out after {timeout_s}s"
        parts = [f"exit={proc.returncode if proc.returncode is not None else 'killed'}"]
        if reason == "output-cap":
            parts.append(
                f"(output truncated at {SHELL_OUTPUT_MAX_BYTES} bytes; process group killed)"
            )
        if out_text:
            parts.append("STDOUT:\n" + out_text.rstrip())
        if err_text:
            parts.append("STDERR:\n" + err_text.rstrip())
        return "\n".join(parts)

    def run_cli(
        self,
        command: str,
        timeout: int = CLI_TIMEOUT_DEFAULT,
        want_output: Any = False,
        cancel_event: threading.Event | None = None,
        relay_stream: TextIO | None = None,
    ) -> str:
        """Run a host CLI in the project cwd. Unsandboxed; path jail does not apply.

        Live stdout/stderr is relayed to the user and written under
        `.g023/scratch/<stamp>/cli/N.log`. The model-facing result is a
        short receipt (exit, duration, log path). Full output is not
        returned unless `want_output` is true (a capped tail) or the
        process failed (a shorter recovery tail).
        """
        cmd = str(command or "").strip()
        if not cmd:
            return "ERROR: command must be a non-empty string"
        clamped = clamp_cli_timeout(timeout)
        if isinstance(clamped, str):
            return clamped
        timeout_s = clamped
        include_tail = _as_want_output(want_output)

        stamp, n, log_path = self._next_cli_slot()
        rel = cli_log_rel(stamp, n)
        started = time.time()
        preview = cmd if len(cmd) <= 200 else cmd[:197] + "..."
        banner = paint(f"[cli start] {preview}", COLOR_CLI)
        log(banner)
        if relay_stream is not None:
            try:
                relay_stream.write(banner + "\n")
                relay_stream.flush()
            except Exception:
                pass

        header = (
            f"# g023v2 run_cli\n"
            f"# command: {cmd[:2000]}\n"
            f"# cwd: {self.root}\n"
            f"# started: {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(started))}\n"
            f"# ---\n"
        )
        try:
            log_file = open(log_path, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            return f"ERROR: failed to open cli log {rel}: {e}"
        exit_label = "unknown"
        body_bytes = 0
        reason: str | None = None
        log_truncated = False
        tail = ""
        try:
            log_file.write(header)
            log_file.flush()
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=self.root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=_cli_env(),
                )
            except OSError as e:
                log_file.write(f"# failed to start: {e}\n")
                return f"ERROR: failed to start command: {e}"

            body_bytes, reason, log_truncated, tail = _stream_cli_output(
                proc,
                timeout_s,
                log_file,
                max_log_bytes=CLI_LOG_MAX_BYTES,
                cancel_event=cancel_event,
                relay_stream=relay_stream,
            )
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            duration = time.time() - started
            code = proc.returncode
            if reason == "timeout":
                exit_label = "killed"
            elif reason == "cancelled":
                exit_label = "killed"
            elif code is None:
                exit_label = "killed"
            else:
                exit_label = str(code)
            footer_bits = [
                f"exit={exit_label}",
                f"duration={duration:.2f}s",
                f"bytes={body_bytes}",
            ]
            if log_truncated:
                footer_bits.append(f"log_truncated_at={CLI_LOG_MAX_BYTES}")
            try:
                log_file.write("\n# ---\n# " + " ".join(footer_bits) + "\n")
            except OSError:
                pass
        finally:
            try:
                log_file.close()
            except OSError:
                pass
            try:
                os.chmod(log_path, 0o644)
            except OSError:
                pass

        duration = time.time() - started
        receipt_bits = [
            f"exit={exit_label}",
            f"duration={duration:.2f}s",
            f"bytes={body_bytes}",
            f"log={rel}",
        ]
        if log_truncated:
            receipt_bits.append(
                f"(log truncated at {CLI_LOG_MAX_BYTES} bytes; process kept running)"
            )
        lines: list[str] = []
        if reason == "timeout":
            lines.append(f"ERROR: command timed out after {timeout_s}s")
        elif reason == "cancelled":
            lines.append("ERROR: command cancelled")
        lines.append(" ".join(receipt_bits))

        failed = reason is not None or exit_label not in ("0",)
        tail_limit = CLI_WANT_OUTPUT_CHARS if include_tail else (
            CLI_FAILURE_TAIL_CHARS if failed else 0
        )
        if tail_limit > 0 and tail:
            snippet = tail[-tail_limit:]
            lines.append("output_tail:")
            lines.append(snippet.rstrip() or "(empty)")
        end = paint(
            f"[cli end] exit={exit_label} duration={duration:.2f}s log={rel}",
            COLOR_CLI,
        )
        log(end)
        if relay_stream is not None:
            try:
                relay_stream.write(end + "\n")
                relay_stream.flush()
            except Exception:
                pass
        return "\n".join(lines)

    def _next_cli_slot(self) -> tuple[str, int, Path]:
        stamp = self._ensure_stamp()
        with self._cli_lock:
            self._cli_n += 1
            n = self._cli_n
        rel = cli_log_rel(stamp, n)
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return stamp, n, path


def clamp_fetch_timeout(timeout: Any) -> int | str:
    """Return a positive timeout clamped to FETCH_TIMEOUT_MAX, or an ERROR string."""
    if timeout is None:
        timeout = FETCH_TIMEOUT_DEFAULT
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        return "ERROR: timeout must be a positive integer"
    if value <= 0:
        return "ERROR: timeout must be a positive integer"
    return min(value, FETCH_TIMEOUT_MAX)


def clamp_fetch_cache_mode(mode: Any) -> str:
    if mode is None or mode == "":
        return "auto"
    text = str(mode).strip().lower()
    if text not in FETCH_CACHE_MODES:
        allowed = ", ".join(FETCH_CACHE_MODES)
        return f"ERROR: cache_mode must be one of: {allowed}"
    return text


def clamp_fetch_extract(mode: Any) -> str:
    if mode is None or mode == "":
        return "text"
    text = str(mode).strip().lower()
    if text not in FETCH_EXTRACT_MODES:
        allowed = ", ".join(FETCH_EXTRACT_MODES)
        return f"ERROR: extract must be one of: {allowed}"
    return text


def clamp_fetch_max_age(value: Any) -> int | str:
    if value is None or value == "":
        return FETCH_CACHE_MAX_AGE_DEFAULT
    try:
        age = int(value)
    except (TypeError, ValueError):
        return "ERROR: max_age must be a non-negative integer"
    if age < 0:
        return "ERROR: max_age must be a non-negative integer"
    return age


def clamp_fetch_max_chars(value: Any) -> int | str:
    if value is None or value == "":
        return FETCH_MAX_CHARS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "ERROR: max_chars must be a positive integer"
    if n <= 0:
        return "ERROR: max_chars must be a positive integer"
    return min(n, FETCH_MAX_CHARS_CAP)


def _result_from_cache(cached: dict[str, Any]) -> FetchResult:
    headers = cached.get("headers") if isinstance(cached.get("headers"), dict) else {}
    return FetchResult(
        url=str(cached.get("url") or ""),
        final_url=str(cached.get("final_url") or cached.get("url") or ""),
        status=int(cached.get("status") or 0),
        headers={str(k).lower(): str(v) for k, v in headers.items()},
        body=str(cached.get("body") or ""),
        engine=str(cached.get("engine") or "cache"),
        truncated=bool(cached.get("truncated")),
        fetched_at=float(cached.get("fetched_at") or 0.0),
        from_cache=True,
        age_seconds=float(cached.get("age_seconds") or 0.0),
    )


def _render_fetch(result: FetchResult, extract_mode: str, max_chars: int) -> str:
    ctype = result.headers.get("content-type") or "unknown"
    extracted = extract_page(
        result.body,
        content_type=ctype,
        final_url=result.final_url or result.url,
        mode=extract_mode,
    )
    content = extracted.get("content")
    if not isinstance(content, str):
        content = str(content or "")
    truncated_chars = False
    if len(content) > max_chars:
        content = content[:max_chars]
        truncated_chars = True
    source = "cache" if result.from_cache else "network"
    bits = [
        f"status={result.status}",
        f"content-type={ctype}",
        f"source={source}",
        f"age={int(result.age_seconds)}s",
        f"engine={result.engine}",
        f"extract={extracted.get('kind') or extract_mode}",
        f"chars={len(content)}",
    ]
    title = extracted.get("title") or ""
    if isinstance(title, str) and title.strip():
        bits.append("title=" + " ".join(title.split())[:80])
    notes: list[str] = []
    if result.truncated:
        notes.append(f"truncated at {FETCH_MAX_BYTES} bytes")
    if truncated_chars:
        notes.append(
            f"truncated at {max_chars} chars; "
            "re-call with a larger max_chars and cache_mode='cache' to read more"
        )
    header = " ".join(bits)
    if notes:
        header += " (" + "; ".join(notes) + ")"
    return header + "\n" + content


def clamp_shell_timeout(timeout: Any) -> int | str:
    """Return a positive timeout clamped to SHELL_TIMEOUT_MAX, or an ERROR string."""
    if timeout is None:
        timeout = SHELL_TIMEOUT_DEFAULT
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        return "ERROR: timeout must be a positive integer"
    if value <= 0:
        return "ERROR: timeout must be a positive integer"
    return min(value, SHELL_TIMEOUT_MAX)


def clamp_cli_timeout(timeout: Any) -> int | str:
    """Return a positive timeout clamped to CLI_TIMEOUT_MAX, or an ERROR string."""
    if timeout is None or timeout == "":
        timeout = CLI_TIMEOUT_DEFAULT
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        return "ERROR: timeout must be a positive integer"
    if value <= 0:
        return "ERROR: timeout must be a positive integer"
    return min(value, CLI_TIMEOUT_MAX)


def _as_want_output(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    return s in ("true", "1", "yes", "on")


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _stream_cli_output(
    proc: subprocess.Popen,
    timeout: int,
    log_file: Any,
    *,
    max_log_bytes: int,
    cancel_event: threading.Event | None,
    relay_stream: TextIO | None,
) -> tuple[int, str | None, bool, str]:
    """Relay and log merged stdout until the process ends, times out, or is cancelled.

    Does not kill on a large log: the omitted tail is dropped from the file
    only. Returns (body_bytes, reason, log_truncated, tail_text).
    """
    q: queue.Queue[bytes | None] = queue.Queue()

    def reader() -> None:
        stream = proc.stdout
        try:
            if stream is None:
                return
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                q.put(chunk)
        except OSError:
            pass
        finally:
            q.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    body_bytes = 0
    log_truncated = False
    reason: str | None = None
    tail = ""
    deadline = time.monotonic() + timeout
    ended = False
    while not ended:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "timeout"
            _kill_process_group(proc)
            break
        if cancel_event is not None and cancel_event.is_set():
            reason = "cancelled"
            _kill_process_group(proc)
            break
        try:
            chunk = q.get(timeout=min(0.2, remaining))
        except queue.Empty:
            if proc.poll() is not None and not thread.is_alive():
                break
            continue
        if chunk is None:
            ended = True
            continue
        text = chunk.decode("utf-8", errors="replace").replace("\x00", "\ufffd")
        write_stderr_raw(text, stream=relay_stream)
        tail = (tail + text)[-CLI_WANT_OUTPUT_CHARS:]
        encoded = text.encode("utf-8")
        if log_truncated:
            continue
        room = max_log_bytes - body_bytes
        if room <= 0:
            log_truncated = True
            try:
                log_file.write("\n# --- log truncated ---\n")
                log_file.flush()
            except OSError:
                pass
            continue
        if len(encoded) <= room:
            piece = text
        else:
            piece = encoded[:room].decode("utf-8", errors="replace")
            log_truncated = True
        try:
            log_file.write(piece)
            log_file.flush()
        except OSError:
            pass
        body_bytes += len(piece.encode("utf-8"))
        if log_truncated:
            try:
                log_file.write("\n# --- log truncated ---\n")
                log_file.flush()
            except OSError:
                pass

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
    return body_bytes, reason, log_truncated, tail


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _communicate_capped(
    proc: subprocess.Popen,
    timeout: int,
    max_bytes: int,
) -> tuple[bytes, bytes, str | None]:
    """Read stdout/stderr up to max_bytes each; kill the group on timeout or cap."""
    q: queue.Queue[tuple[str, bytes | None]] = queue.Queue()

    def reader(stream, tag: str) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                q.put((tag, chunk))
        except OSError:
            pass
        finally:
            q.put((tag, None))

    threads = []
    if proc.stdout is not None:
        t = threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True)
        t.start()
        threads.append(t)
    if proc.stderr is not None:
        t = threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True)
        t.start()
        threads.append(t)

    out = bytearray()
    err = bytearray()
    ended = 0
    expected = len(threads)
    reason: str | None = None
    deadline = time.monotonic() + timeout
    while ended < expected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "timeout"
            _kill_process_group(proc)
            break
        try:
            tag, chunk = q.get(timeout=min(0.2, remaining))
        except queue.Empty:
            if proc.poll() is not None and all(not t.is_alive() for t in threads):
                break
            continue
        if chunk is None:
            ended += 1
            continue
        buf = out if tag == "out" else err
        room = max_bytes - len(buf)
        if room > 0:
            buf.extend(chunk[:room])
        if len(chunk) > max(0, room):
            reason = "output-cap"
            _kill_process_group(proc)
            break

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    return bytes(out), bytes(err), reason
