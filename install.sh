#!/usr/bin/env bash
#
# Install the "sign-with-trezor" Claude skill.
#
# Clones (or updates) this repo and symlinks it into the global Claude skills
# directory so Claude discovers the skill by its manifest name. Idempotent —
# safe to re-run; re-running just updates the clone and refreshes the link.
#
# Works two ways:
#   * from an existing checkout:   ./install.sh
#   * bootstrap on a fresh machine (nothing cloned yet):
#       curl -fsSL https://raw.githubusercontent.com/pbuda/trezor-skill/main/install.sh | bash
#
# Environment overrides:
#   TREZOR_SKILL_SRC    where to keep the clone   (default: ~/.local/share/trezor-skill)
#   CLAUDE_SKILLS_DIR   global skills directory   (default: ~/.claude/skills)
#
set -euo pipefail

REPO_URL="git@github.com:pbuda/trezor-skill.git"
DEFAULT_SRC="$HOME/.local/share/trezor-skill"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

# --- Resolve the source clone -------------------------------------------------
# Prefer an existing checkout this script lives in; otherwise clone fresh.
self_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "${TREZOR_SKILL_SRC:-}" ] \
   && [ -n "$self_dir" ] \
   && [ -f "$self_dir/SKILL.md" ] \
   && [ -d "$self_dir/scripts" ]; then
  SRC_DIR="$self_dir"
else
  SRC_DIR="${TREZOR_SKILL_SRC:-$DEFAULT_SRC}"
fi

if [ -d "$SRC_DIR/.git" ]; then
  echo "==> Updating clone at $SRC_DIR"
  # Force merge-semantics fast-forward-only, ignoring any pull.rebase config.
  git -C "$SRC_DIR" -c pull.rebase=false pull --ff-only \
    || echo "    (skipped fast-forward — update manually if needed)"
else
  echo "==> Cloning $REPO_URL -> $SRC_DIR"
  mkdir -p "$(dirname "$SRC_DIR")"
  git clone "$REPO_URL" "$SRC_DIR"
fi

# --- Read the skill name from the manifest ------------------------------------
skill_name="$(awk -F': *' '/^name:/{print $2; exit}' "$SRC_DIR/SKILL.md" 2>/dev/null || true)"
skill_name="${skill_name:-sign-with-trezor}"
link="$SKILLS_DIR/$skill_name"

# --- Link into the global skills directory ------------------------------------
mkdir -p "$SKILLS_DIR"
if [ -L "$link" ]; then
  rm -f "$link"
elif [ -e "$link" ]; then
  echo "ERROR: $link already exists and is not a symlink; refusing to overwrite." >&2
  exit 1
fi
ln -s "$SRC_DIR" "$link"
echo "==> Linked skill '$skill_name': $link -> $SRC_DIR"

# --- Point the user at the one-time setup -------------------------------------
echo
echo "Skill installed. One-time device setup, from the skill directory:"
echo "    cd \"$link\""
if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  echo "    # WSL detected — device I/O runs on Windows Python:"
  echo "    python.exe -m pip install --user -r scripts/requirements-windows.txt"
  echo "    python -m venv .venv && .venv/bin/pip install -r scripts/requirements-wsl.txt"
  echo "    python.exe scripts/trezor_signer.py pair"
else
  echo "    python -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt"
  echo "    .venv/bin/python scripts/trezor_signer.py pair"
fi
