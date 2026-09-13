"""Paths, model id, and numeric limits. Import-safe; no HTTP."""

from __future__ import annotations

import tempfile
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
K_DAT_PATH = REPO_ROOT / "K.dat"
# Created lazily by persist.py (0700). Importing this package must not mkdir.
MEMORY_ROOT = Path(tempfile.gettempdir()) / "deepseek_harness_memory"

# Per-project harness dir: logs, usage, scratch, backups, prompts. Created lazily.
G023_DIR_NAME = ".g023"
# Pre-.g023 last-wins backup dir. File tools still jail it; new backups
# go to `.g023/backups/<stamp>/`.
LEGACY_BACKUP_DIR_NAME = ".g023v2_backups"
BACKUP_DIR_NAME = LEGACY_BACKUP_DIR_NAME

BASE_URL = "https://api.deepseek.com"
RESPONSES_URL = f"{BASE_URL}/responses"
MODEL = "deepseek-flash"

SOFT_CONTEXT_LIMIT = 400_000
COMPACT_AT_FRACTION = 0.80
RECENT_WINDOW_TURNS = 6
# REPL /status next-request token guess. Compaction still uses
# estimate_tokens (JSON bytes // 4 over history only).
CHARS_PER_TOKEN = 3.25

MAX_TEAM_MEMBERS = 8
MAX_CONCURRENT_CHILDREN = 12
# Parent → lead → workers. Depth 0 is parent-spawned; a lead at 0 may
# spawn workers at 1. Nobody at depth >= 1 may spawn, and a lead cannot
# spawn another lead. Extra thinking layers bill output.
MAX_CHILD_DEPTH = 1
MAX_PLAN_ITEMS = 32
MAX_WORK_CONTINUES = 8
CHILD_RESULT_MAX_CHARS = 8_000
JOIN_CHILDREN_TIMEOUT = 600.0
MAX_TEAM_TASKS = 256
MAX_PENDING_MESSAGES = 64
MAX_GOAL_TURNS = 12
# Unattended infinite-orchestration brake (leftover issues → compact
# handoff → next orchestration). REPL auto does not hard-stop here.
MAX_GOAL_CHAIN = 8
# Max N for "run N automatic orchestrations, then ask again".
MAX_INFINITE_BURST = 64
# Tool-using HTTP rounds inside one run_turn. After this, one wrap-up
# request (tools are not dispatched) and the turn returns. Hitting the
# cap is not a cancel: /goal evaluator and later turns still run, and
# those later turns get a fresh cap (they may dispatch tools again).
MAX_TOOL_ROUNDS = 64
GOAL_CONTINUE_AUTO = "auto"
GOAL_CONTINUE_PROMPT = "prompt"
GOAL_CONTINUE_MODES = (GOAL_CONTINUE_AUTO, GOAL_CONTINUE_PROMPT)
MESSAGE_QUEUE_PRUNE_THRESHOLD = 128

HEARTBEAT_STALE_SECONDS = 30.0
THREAD_JOIN_TIMEOUT = 10.0

MAX_IMAGE_DIM = 800
MAX_IMAGE_SOURCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000

SHELL_TIMEOUT_DEFAULT = 60
SHELL_TIMEOUT_MAX = 300
SHELL_OUTPUT_MAX_BYTES = 1 * 1024 * 1024
# run_cli: other host CLIs / harnesses. Longer than run_shell because a
# nested agent (grok -p, tests, image gen) can sit on its own API for
# minutes. Output is relayed to the user and logged; the model sees a
# receipt unless it asks for a tail.
CLI_TIMEOUT_DEFAULT = 600
CLI_TIMEOUT_MAX = 1800
CLI_LOG_MAX_BYTES = 8 * 1024 * 1024
CLI_WANT_OUTPUT_CHARS = 4_000
CLI_FAILURE_TAIL_CHARS = 1_200

GET_CHILD_RESULT_TIMEOUT = 120.0
# Default thinking budgets by child role. Lower is cheaper (output is
# the expensive token). Override per spawn when the slice is actually hard.
ROLE_DEFAULT_EFFORT = {
    "research": 25,
    "explore": 20,
    "implement": 40,
    "verify": 25,
    "lead": 40,
}

GREP_MAX_RESULTS_CAP = 10_000
READ_FILE_MAX_LINES = 10_000

HTTP_CONNECT_TIMEOUT = 30
HTTP_READ_TIMEOUT = 900
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2.0
RETRY_MAX_DELAY = 30.0

DEFAULT_EFFORT = 60
DEFAULT_CHILD_EFFORT = 40
MAX_OUTPUT_TOKENS = 384_000

GREP_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "target", ".next", ".nuxt", ".cache", "coverage",
    ".idea", ".vscode", "site-packages",
    BACKUP_DIR_NAME,
    G023_DIR_NAME,
}

READ_FILE_MAX_BYTES = 256 * 1024
BINARY_HEADER_BYTES = 8192
LIST_DIR_MAX_ENTRIES = 500
FIND_FILES_MAX_RESULTS = 500
FIND_FILES_MAX_RESULTS_CAP = 10_000
REPLACE_IN_FILES_MAX_FILES = 200
REPLACE_IN_FILES_MAX_FILES_CAP = 2_000
READ_FILES_MAX_PATHS = 8
READ_FILES_MAX_PATHS_CAP = 32
APPLY_EDITS_MAX_HUNKS = 20
FETCH_TIMEOUT_DEFAULT = 15
FETCH_TIMEOUT_MAX = 30
FETCH_MAX_BYTES = 256 * 1024
FETCH_MAX_CHARS = 20_000
FETCH_MAX_CHARS_CAP = FETCH_MAX_BYTES
FETCH_CACHE_MAX_AGE_DEFAULT = 3600
FETCH_CACHE_MODES = ("auto", "fresh", "cache")
FETCH_EXTRACT_MODES = ("text", "markdown", "links", "raw")
# Ingest-time history budget for bulky tool results (shell/grep/fetch/…).
# read_file / read_files / read_image are not cut: the model asked for those
# bytes, and paging them costs an extra round. Vault files stay under the
# read_file byte cap so the model can page them with the existing tool.
TOOL_RESULT_BUDGET_CHARS = 8_000
VAULT_STORE_MAX_CHARS = READ_FILE_MAX_BYTES
# Global URL cache, shared across projects. Created lazily (not at import).
URL_CACHE_DIR = MEMORY_ROOT / "url_cache"

# Global skills: bundled with the package, user-written under ~/.g023v2.
# Importing this module does not mkdir the user dir.
SKILLS_BUNDLED_DIR = PACKAGE_DIR / "bundled_skills"
SKILLS_USER_DIR = Path.home() / ".g023v2" / "skills"
SKILL_BODY_MAX_CHARS = 24_000
SKILL_SEARCH_DEFAULT_LIMIT = 8
SKILL_SEARCH_MAX_LIMIT = 24
SKILL_CATALOG_MAX = 80

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".pyc", ".pyo", ".class", ".o", ".obj", ".wasm",
}
