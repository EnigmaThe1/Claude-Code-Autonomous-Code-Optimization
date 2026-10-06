#!/usr/bin/env bash
# Copyright 2026 Bogdan Carp (@EnigmaThe1)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${CLAUDE_AUTONOMY_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/claude-autonomy}"
BIN_DIR="${CLAUDE_AUTONOMY_BIN:-$HOME/.local/bin}"
LINK="$BIN_DIR/claude-auto"
MARKER="$DEST/.claude-autonomy-install.json"
PACKAGE_ONLY=0
WITH_PLUGINS=0
INCLUDE_LSP=0
INCLUDE_DEFERRED=0

for arg in "$@"; do
  case "$arg" in
    --package-only) PACKAGE_ONLY=1 ;;
    --with-plugins) WITH_PLUGINS=1 ;;
    --no-plugins) WITH_PLUGINS=0 ;;
    --include-lsp) INCLUDE_LSP=1 ;;
    --include-deferred) INCLUDE_DEFERRED=1 ;;
    *) echo "Usage: ./install.sh [--package-only] [--with-plugins|--no-plugins] [--include-lsp] [--include-deferred]" >&2; exit 64 ;;
  esac
done
[[ "$PACKAGE_ONLY" -eq 1 ]] && WITH_PLUGINS=0

python3 - "$DEST" "$HOME" "${XDG_DATA_HOME:-$HOME/.local/share}" <<'PY'
import os, sys
raw_dest = os.path.abspath(os.path.expanduser(sys.argv[1]))
if os.path.islink(raw_dest):
    raise SystemExit(f"Refusing symlink install destination: {raw_dest}")
dest = os.path.realpath(raw_dest)
home = os.path.realpath(os.path.expanduser(sys.argv[2]))
xdg = os.path.realpath(os.path.expanduser(sys.argv[3]))
bad = {"/", home, xdg, os.path.dirname(home)}
if dest in bad or len([p for p in dest.split(os.sep) if p]) < 3:
    raise SystemExit(f"Refusing unsafe install destination: {dest}")
PY

# Existing installations must prove package ownership before any staged
# upgrade copies or rewrites their contents.  A corrupt/foreign marker is not
# evidence that Claude Auto may adopt the destination.
python3 - "$DEST" "$MARKER" <<'PY'
import json, os, stat, sys
dest, marker = sys.argv[1:3]
dest = os.path.realpath(os.path.expanduser(dest))
marker = os.path.abspath(os.path.expanduser(marker))
if os.path.realpath(os.path.dirname(marker)) != dest or os.path.basename(marker) != ".claude-autonomy-install.json":
    raise SystemExit("Refusing upgrade: package marker path is outside the canonical install directory")
if os.path.lexists(marker):
    try:
        st = os.lstat(marker)
    except OSError as exc:
        raise SystemExit(f"Refusing upgrade: cannot inspect existing package marker: {exc}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SystemExit("Refusing upgrade: existing package marker must be a regular non-symlink file")
    try:
        with open(marker, encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception as exc:
        raise SystemExit(
            f"Refusing upgrade: existing package marker is invalid at {marker}: {exc}"
        )
    if not isinstance(obj, dict):
        raise SystemExit("Refusing upgrade: existing package marker is not a JSON object")
    if obj.get("package_id") != "claude-autonomous-optimisation-pack":
        raise SystemExit("Refusing upgrade: existing package marker identity mismatch")
    recorded = os.path.realpath(
        os.path.expanduser(str(obj.get("canonical_install_path") or ""))
    )
    if recorded != dest:
        raise SystemExit("Refusing upgrade: existing package marker path mismatch")
PY

mkdir -p "$BIN_DIR" "$(dirname "$DEST")"
EXPECTED_TARGET="$DEST/bin/claude-auto"
if [[ -e "$LINK" || -L "$LINK" ]]; then
  if [[ -L "$LINK" && "$(readlink "$LINK" 2>/dev/null || true)" == "$EXPECTED_TARGET" ]]; then
    :
  else
    echo "ERROR: $LINK already exists and is not this package's launcher. Refusing to overwrite it." >&2
    exit 2
  fi
fi

if [[ -d "$DEST" && ! -f "$MARKER" ]]; then
  if [[ -n "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] \
     && [[ ! -f "$DEST/VERSION" || ! -f "$DEST/bin/claude-auto" ]]; then
    echo "ERROR: $DEST is non-empty but is not identifiable as a claude-auto installation." >&2
    exit 3
  fi
fi

PARENT="$(dirname "$DEST")"
BASE="$(basename "$DEST")"
STAGE="$PARENT/.${BASE}.stage.$"
BACKUP="$PARENT/.${BASE}.rollback.$"
SMOKE_STATE="$PARENT/.${BASE}.smoke-state.$"
HAD_DEST=0
SWAPPED=0
LINK_CREATED=0
rm -rf "$STAGE" "$BACKUP" "$SMOKE_STATE"
mkdir -p "$STAGE"
chmod 700 "$STAGE"

rollback() {
  rc=$?
  if [[ "$rc" -eq 0 ]]; then return; fi
  echo "ERROR: Claude Auto installation failed; restoring the previous known-good installation." >&2
  if [[ "$SWAPPED" -eq 1 ]]; then
    rm -rf "$DEST"
    if [[ "$HAD_DEST" -eq 1 && -d "$BACKUP" ]]; then
      mv "$BACKUP" "$DEST"
      if [[ "$PACKAGE_ONLY" -eq 0 && -f "$DEST/lib/user_layer.py" ]]; then
        python3 "$DEST/lib/user_layer.py" install --package-root "$DEST" >/dev/null 2>&1 || true
      fi
    fi
  fi
  [[ "$LINK_CREATED" -eq 1 ]] && rm -f "$LINK"
  rm -rf "$STAGE" "$BACKUP" "$SMOKE_STATE"
  exit "$rc"
}
trap rollback EXIT

# Build a complete candidate beside the live tree. Copy runtime state first, then
# replace only package-owned files/directories. This preserves repos/, provider
# profiles, model registry and other external state across upgrades.
if [[ -d "$DEST" ]]; then
  HAD_DEST=1
  cp -a "$DEST/." "$STAGE/"
fi

python3 - "$SELF_DIR" "$STAGE" <<'PY'
import os, shutil, sys
src, stage = map(os.path.realpath, sys.argv[1:3])
package_dirs = {"bin", "docs", "hooks", "lib", "scripts", "templates", "tests"}
package_files = {".gitignore", "CHANGELOG.md", "COMMANDS.md", "LICENSE", "MANIFEST.sha256", "NOTICE", "QUICKSTART.md", "README.md", "SOURCES.md", "VERSION", "install.sh", "uninstall.sh"}
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache")
for name in package_dirs:
    d = os.path.join(stage, name)
    if os.path.lexists(d):
        shutil.rmtree(d) if os.path.isdir(d) and not os.path.islink(d) else os.unlink(d)
    shutil.copytree(os.path.join(src, name), d, ignore=ignore)
for name in package_files:
    shutil.copy2(os.path.join(src, name), os.path.join(stage, name))
PY

chmod +x "$STAGE/bin/claude-auto" "$STAGE/install.sh" "$STAGE/uninstall.sh" "$STAGE/hooks/"*.py "$STAGE/lib/user_layer.py" 2>/dev/null || true

# Preserve the install identity but write the canonical final path into the staged marker.
python3 - "$MARKER" "$STAGE/.claude-autonomy-install.json" "$DEST" <<'PY'
import json, os, secrets, stat, sys
old_marker, stage_marker, dest = sys.argv[1:4]
old = {}
if os.path.lexists(old_marker):
    st = os.lstat(old_marker)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SystemExit("Refusing upgrade: existing package marker changed to a non-regular file")
    with open(old_marker, encoding="utf-8") as f:
        old = json.load(f)
obj = {
    "package_id": "claude-autonomous-optimisation-pack",
    "install_uuid": old.get("install_uuid") or secrets.token_hex(16),
    "canonical_install_path": os.path.realpath(dest),
    "version": open(os.path.join(os.path.dirname(stage_marker), "VERSION"), encoding="utf-8").read().strip(),
}
flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
fd = os.open(stage_marker, flags, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2)
    f.write("\n")
os.chmod(stage_marker, 0o600, follow_symlinks=False)
PY

# Candidate must be self-consistent before the live install is moved at all.
# Smoke tests receive isolated external state so candidate validation can never
# create/mutate the future live DEST before the transactional swap.
mkdir -p "$SMOKE_STATE"
chmod 700 "$SMOKE_STATE"
(
  export CLAUDE_AUTONOMY_HOME="$SMOKE_STATE"
  cd "$STAGE"
  python3 - <<'PY'
from pathlib import Path
import sys

package_dirs = ("bin", "docs", "hooks", "lib", "scripts", "templates", "tests")
package_files = {
    ".gitignore", "CHANGELOG.md", "COMMANDS.md", "LICENSE", "NOTICE", "QUICKSTART.md", "README.md",
    "SOURCES.md", "VERSION", "install.sh", "uninstall.sh",
}
expected = set(package_files)
for dirname in package_dirs:
    base = Path(dirname)
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"} or ".pytest_cache" in p.parts:
            continue
        expected.add(p.as_posix())

listed = set()
for raw in Path("MANIFEST.sha256").read_text().splitlines():
    if not raw.strip():
        continue
    try:
        _digest, rel = raw.split(None, 1)
    except ValueError:
        print(f"Malformed MANIFEST.sha256 entry: {raw!r}", file=sys.stderr)
        raise SystemExit(1)
    listed.add(rel.strip())

missing = sorted(expected - listed)
unexpected = sorted(listed - expected)
if missing or unexpected:
    if missing:
        print("MANIFEST.sha256 missing package files: " + ", ".join(missing), file=sys.stderr)
    if unexpected:
        print("MANIFEST.sha256 contains non-package/stale files: " + ", ".join(unexpected), file=sys.stderr)
    raise SystemExit(1)
PY
  sha256sum -c MANIFEST.sha256 >/dev/null
  python3 -m py_compile lib/*.py hooks/*.py tests/*.py
  python3 - <<'PY'
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str((Path.cwd() / "lib").resolve()))

from control_plane import _completion_source_unchanged
from environment_policy import apply_resume_environment, capture_resume_environment, sanitised_subprocess_env
from planning_support import validate_plan_graph, validate_progress_checkpoint
from repo_profile import detect_commands, load_declared_verification_commands
from repo_runtime import git_snapshot
from service_manager import _systemd_escape_arg, _systemd_safe_text
from supervisor_support import resolve_session_settings
from telemetry import circuit_breaker_reason, update_global_usage, usage_from_result
from toolchain_preflight import normalise_local_shell_entrypoint
from verification import _verification_commands
from workspace_recovery import cleanup_untracked_file, promote_fast_forward

valid = {
    "acceptance_criteria": ["behaviour verified"],
    "tasks": [
        {"id": "T001", "title": "first", "depends_on": [], "verification": ["check"], "risk": "low"},
        {"id": "T002", "title": "second", "depends_on": ["T001"], "verification": ["check"], "risk": "medium"},
    ],
}
assert validate_plan_graph(valid) == []
assert validate_progress_checkpoint(
    valid,
    {
        "completed_task_ids": ["T001", "T002"],
        "remaining_task_ids": [],
        "verification": {"smoke": "PASS"},
        "external_checkpoint": None,
    },
    require_partition=True,
) == []
assert validate_progress_checkpoint(
    valid,
    {
        "completed_task_ids": ["T001", "T002"],
        "remaining_task_ids": [],
        "verification": {"smoke": "FAIL"},
        "external_checkpoint": None,
    },
    require_partition=True,
), "COMPLETE + FAIL verification must be rejected"

bad = {
    "acceptance_criteria": ["x"],
    "tasks": [
        {"id": "T001", "title": "a", "depends_on": ["T001"], "verification": ["x"], "risk": "low"},
        {"id": "T001", "title": "b", "depends_on": [], "verification": ["x"], "risk": "low"},
    ],
}
errors = validate_plan_graph(bad)
assert any("duplicate task id" in x for x in errors)
assert any("depend on itself" in x for x in errors)

deep_tasks = []
for i in range(1500):
    task_id = f"D{i:04d}"
    dep = [] if i == 0 else [f"D{i-1:04d}"]
    deep_tasks.append({
        "id": task_id, "title": task_id, "depends_on": dep,
        "verification": ["check"], "risk": "low",
    })
deep_tasks[0]["depends_on"] = ["D1499"]
deep_errors = validate_plan_graph({
    "acceptance_criteria": ["cycle rejected"],
    "tasks": deep_tasks,
})
assert any("dependency cycle:" in x for x in deep_errors)

base = {
    "head": "a",
    "worktree_diff_sha256": "b",
    "index_diff_sha256": "c",
    "untracked_sha256": "d",
}
assert _completion_source_unchanged(base, dict(base))
moved = dict(base); moved["head"] = "changed"
assert not _completion_source_unchanged(base, moved)

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    contract = root / ".claude-auto" / "verification.json"
    contract.parent.mkdir(parents=True)
    commands = [f"python3 check_{i:02d}.py" for i in range(60)]
    contract.write_text(json.dumps({
        "schema_version": 1,
        "commands": {"test": commands},
    }))
    hints = detect_commands(root, set())
    assert all(command in hints["test"] for command in commands)
    final = [command for category, command in _verification_commands({
        "repo_root": str(root),
        "build_test_hints": hints,
    }) if category == "test"]
    assert all(command in final for command in commands)

with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ext:
    root = Path(td)
    target = Path(ext) / "verification.json"
    target.write_text(json.dumps({
        "schema_version": 1,
        "commands": {"test": ["echo outside"]},
    }))
    contract = root / ".claude-auto" / "verification.json"
    contract.parent.mkdir(parents=True)
    contract.symlink_to(target)
    try:
        load_declared_verification_commands(root)
    except ValueError:
        pass
    else:
        raise AssertionError("symlinked verification contract must fail closed")

assert "%%n" in _systemd_escape_arg("repo-%n")
try:
    _systemd_safe_text("repo\nInjected=1")
except ValueError:
    pass
else:
    raise AssertionError("systemd control characters must be rejected")

invalid_usage = usage_from_result({
    "usage": {"input_tokens": -1},
    "total_cost_usd": float("nan"),
})
assert invalid_usage == {"_invalid_accounting": 1.0}
usage_state = {
    "total_reported_turns": 0,
    "total_cost_usd": 0.5,
    "total_wall_seconds": 0,
}
update_global_usage(usage_state, {
    "usage": {"total_cost_usd": float("nan")},
    "wall_seconds": 0,
})
breaker_args = type("Args", (), {
    "max_total_turns": 0,
    "max_wall_seconds": 0,
    "max_total_budget_usd": 1.0,
})()
assert circuit_breaker_reason(breaker_args, usage_state)

assert resolve_session_settings(None, "balanced", headless=True) == "hermetic"
assert resolve_session_settings(None, "balanced", headless=False) == "compatibility"
assert resolve_session_settings("compatibility", "balanced", headless=True) == "compatibility"

clean = sanitised_subprocess_env({
    "PATH": "/usr/bin",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.bad",
    "GIT_CONFIG_VALUE_0": "value",
})
assert "GIT_CONFIG_COUNT" not in clean
assert "GIT_CONFIG_KEY_0" not in clean
assert "GIT_CONFIG_VALUE_0" not in clean
assert clean["PYTHONDONTWRITEBYTECODE"] == "1"

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    saved = capture_resume_environment(root, {
        "PATH": "/custom/toolchain/bin:/usr/bin",
        "JAVA_HOME": "/custom/jdk",
        "GIT_CONFIG_COUNT": "1",
    })
    restored = apply_resume_environment(saved, {"PATH": "/usr/bin", "GIT_CONFIG_COUNT": "9"})
    assert restored["PATH"] == "/custom/toolchain/bin:/usr/bin"
    assert restored["JAVA_HOME"] == "/custom/jdk"
    assert "GIT_CONFIG_COUNT" not in restored

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    script = root / "verify.sh"
    script.write_text("#!/usr/bin/env bash\necho ok\n")
    script.chmod(0o600)
    effective, note = normalise_local_shell_entrypoint(root, "./verify.sh")
    assert effective == "bash ./verify.sh" and note

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "cleanup@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Cleanup Smoke"], check=True)
    (root / "tracked.txt").write_text("baseline\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
    base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    target = root / "recovery.txt"
    target.write_text("recoverable\n")
    subprocess.run(["git", "-C", str(root), "add", "recovery.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "repair"], check=True)
    repair = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(root), "reset", "--hard", "-q", base], check=True)
    target.write_text("recoverable\n")
    result = cleanup_untracked_file(root, "recovery.txt", repair)
    assert result["status"] == "deleted" and not target.exists()

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "promote@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Promote Smoke"], check=True)
    (root / "base.txt").write_text("base\n")
    (root / "wip.txt").write_text("clean\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    (root / "forward.txt").write_text("forward\n")
    subprocess.run(["git", "-C", str(root), "add", "forward.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "forward"], check=True)
    target = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(root), "reset", "--hard", "-q", base], check=True)
    (root / "wip.txt").write_text("dirty but non-overlapping\n")
    promoted = promote_fast_forward(root, target)
    assert promoted["status"] == "promoted"
    assert (root / "wip.txt").read_text() == "dirty but non-overlapping\n"

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "smoke@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Smoke"], check=True)
    (root / "tracked.txt").write_text("baseline\n")
    (root / ".gitignore").write_text(".claude-auto/\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
    contract = root / ".claude-auto" / "verification.json"
    contract.parent.mkdir(parents=True)
    contract.write_text(json.dumps({"schema_version": 1, "commands": {"test": ["echo one"]}}))
    before = git_snapshot(root)
    contract.write_text(json.dumps({"schema_version": 1, "commands": {"test": ["echo two"]}}))
    after = git_snapshot(root)
    assert not _completion_source_unchanged(before, after)
PY
  test "$(./bin/claude-auto --version)" = "claude-auto $(cat VERSION)"
)
rm -rf "$SMOKE_STATE"

if [[ "$HAD_DEST" -eq 1 ]]; then
  mv "$DEST" "$BACKUP"
fi
mv "$STAGE" "$DEST"
SWAPPED=1
chmod 700 "$DEST"

if [[ ! -e "$LINK" && ! -L "$LINK" ]]; then
  ln -s "$EXPECTED_TARGET" "$LINK"
  LINK_CREATED=1
fi
"$LINK" --version >/dev/null

if [[ "$PACKAGE_ONLY" -eq 0 ]]; then
  python3 "$DEST/lib/user_layer.py" install --package-root "$DEST" >/dev/null
fi

# Commit only after package + user layer passed. Optional plugins are intentionally
# post-commit because they are external optimisations, not release integrity.
rm -rf "$BACKUP"
SWAPPED=0
trap - EXIT

PLUGIN_NOTE="SKIPPED"
if [[ "$WITH_PLUGINS" -eq 1 ]]; then
  ARGS=(plugins --install)
  [[ "$INCLUDE_LSP" -eq 1 ]] && ARGS+=(--include-lsp)
  [[ "$INCLUDE_DEFERRED" -eq 1 ]] && ARGS+=(--include-deferred)
  if "$LINK" "${ARGS[@]}"; then
    PLUGIN_NOTE="CORE USER-SCOPE PLUGINS INSTALLED"
  else
    PLUGIN_NOTE="PARTIAL/FAILED — base Claude Auto package remains installed; rerun: claude-auto plugins --install"
    echo "WARNING: one or more optional optimisation plugins could not be installed. The transactional core installation remains valid." >&2
  fi
fi

cat <<MSG
Installed Claude Autonomous Optimisation Pack to:
  $DEST
Command:
  $LINK

Transactional upgrade verification:
  PASS (staged manifest + compile + behavioural control-plane smoke + launcher smoke before atomic swap)

User-level Claude integration:
  $([[ "$PACKAGE_ONLY" -eq 0 ]] && echo ENABLED || echo "SKIPPED (--package-only)")

User-scope optimisation plugins:
  $PLUGIN_NOTE

No repository was modified. Strong autonomous execution remains opt-in through claude-auto run/start from a top-level shell.
MSG
