#!/usr/bin/env bash
# AegisX Agent installer — installs the agent from GitHub and puts the
# `aegisx` command on your PATH.
#
#   curl -fsSL https://raw.githubusercontent.com/aegisxresearch/AegisX-Agent/main/installer.sh | bash
#
# or, from an existing checkout:
#
#   ./installer.sh            # install this checkout in place
#   ./installer.sh --help     # all options
#
# What it does:
#   1. clones the repo (or updates an existing install; or uses this checkout)
#   2. creates a virtualenv (uv if available, otherwise python3 -m venv)
#   3. pip-installs the package in editable mode
#   4. writes an `aegisx` wrapper into ~/.local/bin
#   5. cleans stale `aegisx` aliases and PATH lines from shell rc files
#      (leftovers from earlier manual installs shadow the fresh wrapper)
#
# The wrapper delegates to the repo's own `aegisx` launcher instead of
# symlinking it, because the launcher resolves its repo root from the path
# it was invoked with — through a symlink that resolution breaks.
#
# Uninstall: installer.sh --uninstall   (add --purge to also wipe ~/.aegisx)

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

REPO="aegisxresearch/AegisX-Agent"
REPO_URL="https://github.com/${REPO}"
DEFAULT_REF="main"
DEFAULT_INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/aegisx-agent"
DEFAULT_BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${AEGISX_DATA_DIR:-$HOME/.aegisx}"
PYTHON_VERSION="3.11"   # version uv provisions when no suitable python exists

REF="$DEFAULT_REF"
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
BIN_DIR="$DEFAULT_BIN_DIR"
WITH_MCP=0
WITH_DEV=0
UNINSTALL=0
PURGE=0
ASSUME_YES=0

# ─────────────────────────────────────────────────────────────────────────────
# Output helpers — info on stdout, errors on stderr, non-zero exit on failure
# ─────────────────────────────────────────────────────────────────────────────

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_OK=""; C_ERR=""; C_OFF=""
fi

info()  { printf '%s\n' "${C_DIM}==>${C_OFF} $*"; }
ok()    { printf '%s\n' "${C_OK}✓${C_OFF} $*"; }
err()   { printf '%s\n' "${C_ERR}error:${C_OFF} $*" >&2; }
die()   { err "$*"; exit 1; }
ask()   { [ "$ASSUME_YES" = 1 ] && return 0; read -r -p "$1 [y/N] " reply; [ "${reply:-}" = y ] || [ "${reply:-}" = Y ]; }

# ─────────────────────────────────────────────────────────────────────────────
# Stale rc cleanup: an earlier manual install often left shell rc lines —
# `alias aegisx=...` or `export PATH=".../aegisx.../bin:$PATH"` — pointing at
# directories that no longer exist. An alias shadows the wrapper on PATH, so
# a stale one makes every `aegisx` invocation fail with "No such file or
# directory" even though the fresh install works. Remove only provably dead
# lines (their target is missing), never comments or lines that point at this
# install, and keep a timestamped backup of every touched rc file.
# ─────────────────────────────────────────────────────────────────────────────

RC_FILES=("$HOME/.bashrc" "$HOME/.bash_aliases" "$HOME/.profile" "$HOME/.zshrc")

clean_stale_rc_entries() {
  local rc numbers n line trimmed trimmed_lc target dir removed backup
  for rc in "${RC_FILES[@]}"; do
    [ -f "$rc" ] || continue
    grep -iq "aegisx" "$rc" || continue
    numbers=""
    removed=0
    while IFS=: read -r n line; do
      trimmed="${line#\"${line%%[![:space:]]*}\"}"
      trimmed_lc="${trimmed,,}"
      case "$trimmed_lc" in
        \#*) continue ;;                       # comments are left alone
        *alias\ aegisx=*)
          target="${trimmed#*alias aegisx=}"
          # Quoted value: cut at the first matching close quote, so a trailing
          # inline comment is ignored; unquoted: cut at the first space.
          case "$target" in
            \"*) target="${target#\"}"; target="${target%%\"*}" ;;
            \'*) target="${target#\'}"; target="${target%%\'*}" ;;
            *)  target="${target%%[[:space:]]*}" ;;
          esac
          target="${target/#\~/$HOME}"
          case "${target,,}" in *aegisx*) ;; *) continue ;; esac
          [ -n "$target" ] && [ -e "$target" ] && continue
          ;;
        *export\ PATH=*|*path=*)
          dir="${trimmed#*PATH=}"
          [ "$dir" = "$trimmed" ] && dir="${trimmed#*path=}"
          dir="${dir#\"}"; dir="${dir#\'}"      # opening quote
          dir="${dir%%:*}"                       # first PATH segment
          dir="${dir%\"}"; dir="${dir%\'}"      # closing quote / stray
          dir="${dir//\$HOME/$HOME}"
          dir="${dir/#\~/$HOME}"
          case "${dir,,}" in *aegisx*) ;; *) continue ;; esac
          [ "$dir" = "$BIN_DIR" ] && continue   # this install's own entry stays
          [ -e "$dir" ] && continue
          ;;
        *) continue ;;
      esac
      numbers="${numbers:+$numbers }$n"
      printf '  %s:%s: %s\n' "$rc" "$n" "$line"
      removed=$((removed + 1))
    done < <(grep -in "aegisx" "$rc")
    if [ "$removed" -gt 0 ]; then
      backup="$rc.bak-aegisx-$(date +%Y%m%d-%H%M%S)"
      cp "$rc" "$backup"
      for n in $(printf '%s\n' "$numbers" | tr ' ' '\n' | sort -rn); do
        sed -i "${n}d" "$rc"
      done
      ok "Removed $removed stale aegisx line(s) from $rc (backup: $backup)"
    fi
  done
}

usage() {
  cat <<EOF
AegisX Agent installer

Usage:
  installer.sh [options]

Options:
  --dir <path>       Install location (default: $DEFAULT_INSTALL_DIR)
  --bin <path>       Directory for the aegisx wrapper (default: $DEFAULT_BIN_DIR)
  --ref <git-ref>    Branch or tag to install (default: $DEFAULT_REF)
  --with-mcp         Also install the optional MCP extra (mcp 2.x)
  --dev              Also install dev tools (pytest, ruff, mypy)
  --yes              Non-interactive: assume "yes" for prompts
  --uninstall        Remove the installed code and the aegisx wrapper
  --purge            With --uninstall: also delete config/data in $DATA_DIR
  -h, --help         Show this help

Environment overrides: AEGISX_DATA_DIR, XDG_DATA_HOME, XDG_BIN_HOME, NO_COLOR
EOF
}

# ─────────────────────────────────────────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────────────────────────────────────────

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)       [ $# -ge 2 ] || die "--dir needs a path";   INSTALL_DIR="$2"; shift 2 ;;
    --bin)       [ $# -ge 2 ] || die "--bin needs a path";   BIN_DIR="$2"; shift 2 ;;
    --ref|--branch) [ $# -ge 2 ] || die "$1 needs a value";  REF="$2"; shift 2 ;;
    --with-mcp)  WITH_MCP=1; shift ;;
    --dev)       WITH_DEV=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge)     PURGE=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    --version)   printf 'installer for %s (ref %s)\n' "$REPO" "$REF"; exit 0 ;;
    *)           usage >&2; die "unknown option: $1" ;;
  esac
done

# ─────────────────────────────────────────────────────────────────────────────
# Uninstall path
# ─────────────────────────────────────────────────────────────────────────────

if [ "$UNINSTALL" = 1 ]; then
  info "Removing $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  if [ -e "$BIN_DIR/aegisx" ]; then
    info "Removing $BIN_DIR/aegisx"
    rm -f "$BIN_DIR/aegisx"
  fi
  if [ "$PURGE" = 1 ] && [ -e "$DATA_DIR" ]; then
    if ask "Also delete all AegisX data in $DATA_DIR (config, skills, scheduler DB)?"; then
      rm -rf "$DATA_DIR"
      ok "Purged $DATA_DIR"
    else
      info "Kept $DATA_DIR"
    fi
  fi
  # Removing the install can strand rc lines that pointed at it; sweep those.
  clean_stale_rc_entries
  ok "AegisX Agent uninstalled"
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# Source tree: this checkout, an existing install, or a fresh clone
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_PATH="${BASH_SOURCE[0]:-}"
IN_REPO=0
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  [ -f "$SCRIPT_DIR/pyproject.toml" ] && IN_REPO=1
fi

VENV="$INSTALL_DIR/.venv"

have() { command -v "$1" >/dev/null 2>&1; }

install_deps_into_venv() {
  local extras=""
  [ "$WITH_MCP" = 1 ] && extras="mcp"
  [ "$WITH_DEV" = 1 ] && extras="${extras:+${extras},}dev"

  local spec="$INSTALL_DIR"
  [ -n "$extras" ] && spec="$INSTALL_DIR[$extras]"

  info "Installing the package${extras:+ (extras: $extras)} into the venv"
  if have uv; then
    uv pip install --python "$VENV/bin/python" -e "$spec"
  else
    "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
    "$VENV/bin/python" -m pip install -e "$spec"
  fi
}

if [ "$IN_REPO" = 1 ]; then
  info "Running from a checkout: installing $SCRIPT_DIR in place"
  INSTALL_DIR="$SCRIPT_DIR"
  VENV="$INSTALL_DIR/.venv"
else
  if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing install at $INSTALL_DIR (ref: $REF)"
    if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ] && ! ask "$INSTALL_DIR has local changes; discard them?"; then
      die "local changes present; resolve them or pass --yes / choose another --dir"
    fi
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REF"
    git -C "$INSTALL_DIR" reset --hard FETCH_HEAD
  else
    info "Cloning $REPO_URL (ref: $REF) into $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 -b "$REF" "$REPO_URL" "$INSTALL_DIR"
  fi
fi

cd "$INSTALL_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# Python + virtualenv
# ─────────────────────────────────────────────────────────────────────────────

if have uv; then
  info "Creating venv with uv (python $PYTHON_VERSION; downloaded automatically if missing)"
  # --clear: a re-run replaces the existing venv instead of failing/prompting —
  # this is what makes "re-run the installer to update" work unattended.
  uv venv --clear --python "$PYTHON_VERSION" "$VENV"
else
  have python3 || die "python3 not found. Install Python >= 3.10 or uv (https://docs.astral.sh/uv/)"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "python3 is $(python3 -V 2>&1 | cut -d' ' -f2); AegisX needs >= 3.10. Or install uv and rerun."
  info "Creating venv with python3 -m venv"
  python3 -m venv --clear "$VENV"
fi

install_deps_into_venv

# ─────────────────────────────────────────────────────────────────────────────
# Verify the install actually runs before touching PATH
# ─────────────────────────────────────────────────────────────────────────────

VERSION="$("$VENV/bin/python" -c 'import aegisx_agent; print(aegisx_agent.__version__)')"
"$VENV/bin/aegisx" --help >/dev/null
ok "AegisX Agent $VERSION installed in $INSTALL_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# Wrapper on PATH (exec, not symlink: the launcher resolves its repo root
# from the invocation path, which a symlink would break)
# ─────────────────────────────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/aegisx" <<WRAPPER
#!/usr/bin/env bash
exec "$INSTALL_DIR/aegisx" "\$@"
WRAPPER
chmod +x "$BIN_DIR/aegisx"
ok "Wrote $BIN_DIR/aegisx"

# A stale `alias aegisx=...` in an rc file shadows this wrapper, so sweep rc
# files for provably dead entries before telling the user everything is ready.
clean_stale_rc_entries

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf '%s\n' "${C_BOLD}note:${C_OFF} $BIN_DIR is not on your PATH." >&2
    printf '%s\n' "Add it, e.g.:" >&2
    printf '  echo '\''export PATH="%s:$PATH"'\'' >> ~/.bashrc && source ~/.bashrc\n' "$BIN_DIR" >&2
    ;;
esac

cat <<EOF

${C_BOLD}AegisX Agent $VERSION is ready.${C_OFF}

  aegisx                    # interactive chat (auto-detects a local Ollama)
  aegisx run "task"         # one-shot, scriptable
  aegisx config-info        # show current configuration

No API key is needed if an Ollama server is already running. Otherwise:

  export AEGISX_OPENAI_API_KEY="sk-..."        # or another provider:
  export AEGISX_LLM_PROVIDER=anthropic         # anthropic | groq | custom
  export AEGISX_ANTHROPIC_API_KEY="sk-ant-..."

Later updates:    re-run this installer (it updates an existing install)
Uninstall:        installer.sh --uninstall${PURGE:+ (add --purge for data)}
EOF
