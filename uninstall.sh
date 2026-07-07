#!/usr/bin/env bash
#
# Uninstall the "sign-with-trezor" Claude skill.
#
# Removes the symlink from the global Claude skills directory and, if it created
# one, the managed clone. It will NOT delete a checkout it didn't create (e.g. your
# own working copy) — it prints how to remove that yourself.
#
#   ./uninstall.sh               remove the symlink and the managed clone
#   ./uninstall.sh --keep-clone  remove only the symlink
#
# Environment overrides (match install.sh):
#   TREZOR_SKILL_SRC    managed clone location  (default: ~/.local/share/trezor-skill)
#   CLAUDE_SKILLS_DIR   global skills directory (default: ~/.claude/skills)
#
set -euo pipefail

SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
DEFAULT_SRC="$HOME/.local/share/trezor-skill"

keep_clone=0
for arg in "$@"; do
  case "$arg" in
    --keep-clone) keep_clone=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Resolve the skill name from the manifest if reachable; else the known default.
self_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
skill_name=""
[ -f "$self_dir/SKILL.md" ] \
  && skill_name="$(awk -F': *' '/^name:/{print $2; exit}' "$self_dir/SKILL.md" 2>/dev/null || true)"
skill_name="${skill_name:-sign-with-trezor}"
link="$SKILLS_DIR/$skill_name"

# 1. Remove the symlink (only if it is one).
target=""
if [ -L "$link" ]; then
  target="$(readlink "$link")"
  rm -f "$link"
  echo "==> Removed symlink: $link"
elif [ -e "$link" ]; then
  echo "WARNING: $link exists but is not a symlink; left untouched." >&2
else
  echo "==> No symlink at $link (nothing to remove there)."
fi

# 2. Remove the managed clone — but never an arbitrary checkout.
if [ "$keep_clone" -eq 0 ]; then
  src="${TREZOR_SKILL_SRC:-${target:-$DEFAULT_SRC}}"
  if [ -d "$src/.git" ]; then
    if [ "$src" = "$DEFAULT_SRC" ] || [ -n "${TREZOR_SKILL_SRC:-}" ] && [ "$src" = "${TREZOR_SKILL_SRC:-}" ]; then
      rm -rf "$src"
      echo "==> Removed clone: $src"
    else
      echo "==> Clone at $src looks like your own checkout — NOT deleting."
      echo "    Remove it yourself if you want:  rm -rf \"$src\""
    fi
  fi
fi

# 3. The pairing credential is user state and is left in place.
if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  cred_hint='(Windows) %APPDATA%\trezor-skill\trezor_credential.json'
elif [ "$(uname -s)" = "Darwin" ]; then
  cred_hint="$HOME/Library/Application Support/trezor-skill/trezor_credential.json"
else
  cred_hint="${XDG_CONFIG_HOME:-$HOME/.config}/trezor-skill/trezor_credential.json"
fi
echo
echo "Skill uninstalled. Your pairing credential was left in place; for a full wipe, delete:"
echo "    $cred_hint"
