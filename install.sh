#!/usr/bin/env bash
# install.sh — idempotent setup for the g023v2 harness.
#
# Detects what is not connected yet and only does those pieces.
# Re-running is safe: already-correct steps print [ok] and are skipped.
#
# What this script is responsible for (keep in sync with AGENTS.md):
#   - Python >= 3.10 (the interpreter `bin/g023v2` shebang will find)
#   - hard dep: requests          (g023v2.http imports it at module load)
#   - fetch_url extras: curl_cffi (preferred Chrome-TLS engine), then httpx
#     urllib is the stdlib fallback and is never "installed"
#   - image extra: Pillow / PIL   (optional resize; magic-byte MIME without it)
#   - bin/g023v2 executable bit + ~/.local/bin/g023v2 symlink
#   - ~/.local/bin on PATH, persisted in bashrc/profile/zshrc
#   - K.dat at repo root, non-empty, mode 0600 (never printed, never overwritten)
#
# Usage:
#   ./install.sh          detect + install missing pieces
#   ./install.sh --check  report only; exit 1 if required pieces are missing
#   ./install.sh --help

set -euo pipefail

CHECK=0
for arg in "$@"; do
  case "$arg" in
    --check|-c) CHECK=1 ;;
    --help|-h)
      sed -n '2,20p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$arg" >&2
      printf 'usage: %s [--check] [--help]\n' "$0" >&2
      exit 2
      ;;
  esac
done

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BIN_SRC="$REPO_ROOT/bin/g023v2"
K_DAT="$REPO_ROOT/K.dat"
LINK_DIR="${HOME}/.local/bin"
LINK_PATH="$LINK_DIR/g023v2"
MIN_PY_MAJOR=3
MIN_PY_MINOR=10
PATH_MARKER_BEGIN="# >>> g023v2 PATH >>>"
PATH_MARKER_END="# <<< g023v2 PATH >>>"

OK=0
FIXED=0
MISSING=0
WARN=0
PYTHON=""

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
  C_OK=$'\033[32m'
  C_FIX=$'\033[33m'
  C_NEED=$'\033[31m'
  C_WARN=$'\033[33m'
  C_DIM=$'\033[90m'
  C_RST=$'\033[0m'
else
  C_OK="" C_FIX="" C_NEED="" C_WARN="" C_DIM="" C_RST=""
fi

ok()   { printf '  %s[ok]%s   %s\n' "$C_OK" "$C_RST" "$*"; OK=$((OK + 1)); }
fix()  { printf '  %s[fix]%s  %s\n' "$C_FIX" "$C_RST" "$*"; FIXED=$((FIXED + 1)); }
need() { printf '  %s[need]%s %s\n' "$C_NEED" "$C_RST" "$*"; MISSING=$((MISSING + 1)); }
warn() { printf '  %s[warn]%s %s\n' "$C_WARN" "$C_RST" "$*"; WARN=$((WARN + 1)); }
note() { printf '  %s[..]%s   %s\n' "$C_DIM" "$C_RST" "$*"; }

py_import() {
  local mod="$1"
  "$PYTHON" -c "import ${mod}" >/dev/null 2>&1
}

py_version() {
  "$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'
}

path_contains_dir() {
  # String match on PATH entries. LINK_DIR is already expanded ($HOME/...).
  local dir="${1%/}"
  case ":${PATH:-}:" in
    *":${dir}:"*|*:${dir}/:*) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_python() {
  local cand
  for cand in python3 python3.12 python3.11 python3.10; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)" 2>/dev/null; then
        PYTHON=$(command -v "$cand")
        ok "python ${cand} $(py_version) (>= ${MIN_PY_MAJOR}.${MIN_PY_MINOR})"
        return 0
      fi
    fi
  done
  local have
  have=$(command -v python3 2>/dev/null || true)
  if [[ -n "$have" ]]; then
    need "python3 is $($have -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo unknown); need >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
  else
    need "python3 not found; install Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
  fi
  return 1
}

ensure_pip() {
  if "$PYTHON" -m pip --version >/dev/null 2>&1; then
    ok "pip ($("$PYTHON" -m pip --version | awk '{print $1" "$2}'))"
    return 0
  fi
  if [[ "$CHECK" -eq 1 ]]; then
    need "python3 -m pip is not available (needed to install missing packages)"
    return 1
  fi
  note "pip missing; trying ensurepip --user"
  if "$PYTHON" -m ensurepip --user >/dev/null 2>&1 && "$PYTHON" -m pip --version >/dev/null 2>&1; then
    fix "installed pip via ensurepip --user"
    return 0
  fi
  if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    note "installing python3-pip via apt"
    if sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip >/dev/null 2>&1 \
       && "$PYTHON" -m pip --version >/dev/null 2>&1; then
      fix "installed python3-pip"
      return 0
    fi
  fi
  need "could not install pip. Install python3-pip (or enable ensurepip) and re-run."
  return 1
}

pip_install() {
  local pip_name="$1"
  local logf
  logf=$(mktemp)
  if "$PYTHON" -m pip install --user "$pip_name" >"$logf" 2>&1; then
    rm -f "$logf"
    return 0
  fi
  if grep -qiE 'externally-managed-environment|externally managed' "$logf"; then
    # --user writes to ~/.local; --break-system-packages is the PEP 668
    # override Debian/Ubuntu require. Same path curl_cffi/httpx already use.
    if "$PYTHON" -m pip install --user --break-system-packages "$pip_name" >"$logf" 2>&1; then
      rm -f "$logf"
      return 0
    fi
  fi
  printf '%s\n' "${C_DIM}$(tail -n 8 "$logf")${C_RST}" >&2
  rm -f "$logf"
  return 1
}

ensure_pkg() {
  local pip_name="$1"
  local import_name="$2"
  local kind="$3"   # required | extra
  local why="$4"

  if py_import "$import_name"; then
    ok "${pip_name} (${why})"
    return 0
  fi
  if [[ "$CHECK" -eq 1 ]]; then
    if [[ "$kind" == "required" ]]; then
      need "${pip_name} not importable (${why})"
    else
      warn "${pip_name} not importable (${why})"
    fi
    return 1
  fi
  note "installing ${pip_name} (${why})"
  if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    if [[ "$kind" == "required" ]]; then
      need "cannot install ${pip_name}: pip is missing"
    else
      warn "cannot install ${pip_name}: pip is missing"
    fi
    return 1
  fi
  if pip_install "$pip_name" && py_import "$import_name"; then
    fix "installed ${pip_name}"
    return 0
  fi
  if [[ "$kind" == "required" ]]; then
    need "failed to install ${pip_name} (${why})"
  else
    warn "failed to install ${pip_name} (${why}); continuing"
  fi
  return 1
}

ensure_launcher() {
  if [[ ! -f "$BIN_SRC" ]]; then
    need "launcher missing: $BIN_SRC"
    return 1
  fi
  if [[ -x "$BIN_SRC" ]]; then
    ok "launcher executable ($BIN_SRC)"
  else
    if [[ "$CHECK" -eq 1 ]]; then
      need "launcher is not executable: $BIN_SRC"
      return 1
    fi
    chmod +x "$BIN_SRC"
    fix "chmod +x $BIN_SRC"
  fi
  return 0
}

ensure_symlink() {
  if [[ "$CHECK" -eq 1 ]]; then
    if [[ -L "$LINK_PATH" ]]; then
      local dest
      dest=$(readlink -f "$LINK_PATH" 2>/dev/null || true)
      local src
      src=$(readlink -f "$BIN_SRC" 2>/dev/null || echo "$BIN_SRC")
      if [[ "$dest" == "$src" ]]; then
        ok "symlink $LINK_PATH -> $BIN_SRC"
        return 0
      fi
      need "symlink $LINK_PATH points at ${dest:-broken}, expected $BIN_SRC"
      return 1
    fi
    if [[ -e "$LINK_PATH" ]]; then
      need "$LINK_PATH exists and is not a symlink; not replacing"
      return 1
    fi
    need "no symlink at $LINK_PATH"
    return 1
  fi

  mkdir -p "$LINK_DIR"
  if [[ -L "$LINK_PATH" ]]; then
    local dest src
    dest=$(readlink -f "$LINK_PATH" 2>/dev/null || true)
    src=$(readlink -f "$BIN_SRC" 2>/dev/null || echo "$BIN_SRC")
    if [[ "$dest" == "$src" ]]; then
      ok "symlink $LINK_PATH -> $BIN_SRC"
      return 0
    fi
    ln -sfn "$BIN_SRC" "$LINK_PATH"
    fix "repointed symlink $LINK_PATH -> $BIN_SRC (was ${dest:-broken})"
    return 0
  fi
  if [[ -e "$LINK_PATH" ]]; then
    need "$LINK_PATH exists and is not a symlink; move it aside and re-run"
    return 1
  fi
  ln -s "$BIN_SRC" "$LINK_PATH"
  fix "linked $LINK_PATH -> $BIN_SRC"
  return 0
}

rc_has_marker() {
  local file="$1"
  [[ -f "$file" ]] && grep -qF "$PATH_MARKER_BEGIN" "$file"
}

profile_has_local_bin() {
  local file="$1"
  [[ -f "$file" ]] && grep -q 'HOME/.local/bin' "$file"
}

append_path_block() {
  local file="$1"
  if [[ ! -e "$file" ]]; then
    mkdir -p "$(dirname "$file")"
    touch "$file"
  fi
  cat >>"$file" <<EOF

${PATH_MARKER_BEGIN}
# Added by g023v2 install.sh. No-op if ~/.local/bin is already on PATH.
if [ -d "\$HOME/.local/bin" ]; then
  case ":\${PATH:-}:" in
    *":\$HOME/.local/bin:"*) ;;
    *) export PATH="\$HOME/.local/bin:\$PATH" ;;
  esac
fi
${PATH_MARKER_END}
EOF
}

ensure_path() {
  local path_now=0
  if path_contains_dir "$LINK_DIR"; then
    path_now=1
  fi

  local persist_ok=0
  local persist_needed=()
  # Login shells: Ubuntu .profile already prepends ~/.local/bin when the
  # directory exists. Interactive non-login bash does not source .profile,
  # so .bashrc needs its own guarded block.
  if [[ -f "$HOME/.profile" ]] && profile_has_local_bin "$HOME/.profile"; then
    persist_ok=1
  elif rc_has_marker "$HOME/.profile"; then
    persist_ok=1
  else
    persist_needed+=("$HOME/.profile")
  fi
  if [[ -f "$HOME/.bashrc" || "${SHELL:-}" == *bash* ]]; then
    if rc_has_marker "$HOME/.bashrc"; then
      persist_ok=1
    else
      persist_needed+=("$HOME/.bashrc")
    fi
  fi
  if [[ -f "$HOME/.zshrc" || "${SHELL:-}" == *zsh* ]]; then
    if rc_has_marker "$HOME/.zshrc"; then
      persist_ok=1
    else
      persist_needed+=("$HOME/.zshrc")
    fi
  fi

  if [[ "$path_now" -eq 1 && ${#persist_needed[@]} -eq 0 ]]; then
    ok "PATH includes $LINK_DIR (persisted in shell rc)"
    return 0
  fi

  if [[ "$CHECK" -eq 1 ]]; then
    if [[ ${#persist_needed[@]} -gt 0 ]]; then
      need "PATH persistence missing in: ${persist_needed[*]}"
    fi
    if [[ "$path_now" -eq 1 ]]; then
      ok "PATH includes $LINK_DIR"
    elif [[ ${#persist_needed[@]} -eq 0 ]]; then
      warn "$LINK_DIR not on PATH in this process; rc files already persist it (source ~/.bashrc)"
    fi
    return 0
  fi

  # Make this process able to find g023v2 for the verify step.
  if [[ "$path_now" -eq 0 ]]; then
    export PATH="$LINK_DIR:$PATH"
    fix "prepended $LINK_DIR to PATH for this process"
  fi

  if [[ ${#persist_needed[@]} -gt 0 ]]; then
    local f
    for f in "${persist_needed[@]}"; do
      if rc_has_marker "$f"; then
        continue
      fi
      append_path_block "$f"
      fix "added PATH block to $f (open a new terminal, or: source $f)"
    done
  fi

  if path_contains_dir "$LINK_DIR"; then
    return 0
  fi
  need "PATH still missing $LINK_DIR after update"
  return 1
}

ensure_kdat() {
  if [[ -f "$K_DAT" ]]; then
    local size mode
    size=$(wc -c <"$K_DAT" | tr -d ' ')
    if [[ "$size" -eq 0 ]]; then
      need "K.dat exists but is empty ($K_DAT). Put the DeepSeek API key on one line."
      return 1
    fi
    # strip-empty check matching get_api_key()
    if ! grep -q '[^[:space:]]' "$K_DAT"; then
      need "K.dat is whitespace-only ($K_DAT)"
      return 1
    fi
    mode=$(stat -c '%a' "$K_DAT" 2>/dev/null || stat -f '%OLp' "$K_DAT")
    if [[ "$mode" != "600" && "$mode" != "0600" ]]; then
      if [[ "$CHECK" -eq 1 ]]; then
        warn "K.dat mode is ${mode}; expected 600 (key is present)"
      else
        chmod 600 "$K_DAT"
        fix "chmod 600 K.dat (was ${mode})"
      fi
    else
      ok "K.dat present (mode 600, not printed)"
    fi
    return 0
  fi

  local key="${DEEPSEEK_API_KEY:-${G023V2_API_KEY:-}}"
  if [[ -n "$key" ]]; then
    if [[ "$CHECK" -eq 1 ]]; then
      need "K.dat missing; env key is set and would be written on a real install"
      return 1
    fi
    umask 077
    printf '%s\n' "$key" >"$K_DAT"
    chmod 600 "$K_DAT"
    fix "wrote K.dat from DEEPSEEK_API_KEY/G023V2_API_KEY (mode 600)"
    return 0
  fi
  need "K.dat missing ($K_DAT). Create it with the DeepSeek API key on one line, or export DEEPSEEK_API_KEY and re-run."
  return 1
}

report_fetch_engine() {
  if [[ -z "$PYTHON" ]]; then
    return 0
  fi
  local engine
  engine=$(
    "$PYTHON" - "$REPO_ROOT" <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, sys.argv[1])
try:
    from g023v2.url_fetch import _engine_funcs
    print(_engine_funcs()[0][0])
except Exception as e:
    print("unavailable:" + type(e).__name__, file=sys.stderr)
    raise SystemExit(1)
PY
  ) || engine=""
  if [[ -z "$engine" ]]; then
    warn "could not resolve fetch_url engine (package import failed)"
    return 1
  fi
  if [[ "$engine" == "urllib" ]]; then
    warn "fetch_url engine is urllib (stdlib). curl_cffi or httpx is preferred."
  else
    ok "fetch_url engine: $engine"
  fi
}

verify_launch() {
  if [[ ! -x "$LINK_PATH" && ! -x "$BIN_SRC" ]]; then
    return 1
  fi
  local target="$BIN_SRC"
  [[ -x "$LINK_PATH" ]] && target="$LINK_PATH"
  if "$target" --help >/dev/null 2>&1; then
    ok "g023v2 --help runs ($target)"
    return 0
  fi
  need "g023v2 --help failed via $target"
  return 1
}

printf '%sg023v2 install%s  %s\n' "$C_DIM" "$C_RST" "$([[ "$CHECK" -eq 1 ]] && echo '(check only)' || echo '(idempotent)')"
printf '%srepo%s %s\n' "$C_DIM" "$C_RST" "$REPO_ROOT"
echo

ensure_python || true
if [[ -n "$PYTHON" ]]; then
  ensure_pip || true
  ensure_pkg requests requests required "Responses API HTTP (g023v2.http)" || true
  ensure_pkg curl_cffi curl_cffi extra "fetch_url preferred engine (Chrome TLS)" || true
  ensure_pkg httpx httpx extra "fetch_url fallback engine" || true
  ensure_pkg Pillow PIL extra "image resize for --image" || true
fi
ensure_launcher || true
ensure_symlink || true
ensure_path || true
ensure_kdat || true
if [[ -n "$PYTHON" ]]; then
  report_fetch_engine || true
fi
if [[ "$MISSING" -eq 0 && -n "$PYTHON" ]]; then
  verify_launch || true
fi

echo
printf '%s%d ok, %d fixed, %d need, %d warn%s\n' "$C_DIM" "$OK" "$FIXED" "$MISSING" "$WARN" "$C_RST"

if [[ "$MISSING" -gt 0 ]]; then
  if [[ "$CHECK" -eq 1 ]]; then
    printf 'check failed: %d required piece(s) not connected.\n' "$MISSING" >&2
  else
    printf 'install incomplete: %d required piece(s) still missing.\n' "$MISSING" >&2
  fi
  exit 1
fi

if [[ "$CHECK" -eq 1 ]]; then
  printf 'check passed.\n'
else
  printf 'install complete. From any project folder: g023v2\n'
  if [[ "$FIXED" -gt 0 ]]; then
    printf 'If a new terminal does not find g023v2, run: source ~/.bashrc\n'
  fi
fi
exit 0
