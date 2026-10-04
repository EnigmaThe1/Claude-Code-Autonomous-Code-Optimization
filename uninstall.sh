#!/usr/bin/env bash
set -euo pipefail
umask 077
DEST="${CLAUDE_AUTONOMY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/claude-autonomy}"
BIN_DIR="${CLAUDE_AUTONOMY_BIN:-$HOME/.local/bin}"
LINK="$BIN_DIR/claude-auto"
MARKER="$DEST/.claude-autonomy-install.json"
PURGE="${1:-}"

python3 - "$DEST" "$MARKER" "$HOME" "${XDG_DATA_HOME:-$HOME/.local/share}" "$PURGE" <<'PY'
import json, os, sys
dest, marker, home, xdg, purge = sys.argv[1:6]
dest = os.path.realpath(os.path.expanduser(dest))
home = os.path.realpath(os.path.expanduser(home))
xdg = os.path.realpath(os.path.expanduser(xdg))
if purge not in {"", "--keep-state", "--purge-state"}:
    raise SystemExit("Usage: uninstall.sh [--keep-state|--purge-state]")
if dest in {"/", home, xdg, os.path.dirname(home)} or len([p for p in dest.split(os.sep) if p]) < 3:
    raise SystemExit(f"Refusing unsafe uninstall destination: {dest}")
try:
    with open(marker) as f:
        obj = json.load(f)
except Exception as exc:
    raise SystemExit(f"Refusing uninstall: valid package marker missing at {marker}: {exc}")
if obj.get("package_id") != "claude-autonomous-optimisation-pack":
    raise SystemExit("Refusing uninstall: package marker identity mismatch")
if os.path.realpath(obj.get("canonical_install_path", "")) != dest:
    raise SystemExit("Refusing uninstall: package marker path mismatch")
PY

# Remove user-scope rules/agents/skill/hook first while the package script still exists.
python3 "$DEST/lib/user_layer.py" remove >/dev/null || {
  echo "ERROR: could not safely remove the Claude user-level integration; package files were left in place." >&2
  exit 5
}

EXPECTED_TARGET="$DEST/bin/claude-auto"
if [[ -L "$LINK" && "$(readlink "$LINK" 2>/dev/null || true)" == "$EXPECTED_TARGET" ]]; then
  rm -f "$LINK"
elif [[ -e "$LINK" || -L "$LINK" ]]; then
  echo "WARNING: $LINK is not owned by this installation; leaving it untouched." >&2
fi

python3 - "$DEST" "$PURGE" <<'PY'
import os, shutil, sys
dest, purge = sys.argv[1:3]
if purge == "--purge-state":
    shutil.rmtree(dest)
else:
    names = [
        "bin", "lib", "templates", "docs", "tests", "hooks",
        ".gitignore", "README.md", "QUICKSTART.md", "COMMANDS.md", "SOURCES.md", "CHANGELOG.md",
        "MANIFEST.sha256", "VERSION", "install.sh", "uninstall.sh",
    ]
    for name in names:
        path = os.path.join(dest, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=False)
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
PY

if [[ "$PURGE" == "--purge-state" ]]; then
  echo "Removed package, user-level Claude integration, and all claude-auto external state."
else
  echo "Removed executable package files and user-level Claude integration. Per-repository external state and model registry were preserved."
fi
