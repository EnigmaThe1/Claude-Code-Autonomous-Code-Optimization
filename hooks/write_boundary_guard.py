#!/usr/bin/env python3
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

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys


def decision(value: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": value,
            "permissionDecisionReason": reason,
        }
    }, separators=(",", ":")))


def _root_and_target(root_raw: str, raw: str) -> tuple[Path, Path]:
    root = Path(root_raw).expanduser().resolve()
    expanded = os.path.expandvars(os.path.expanduser(str(raw)))
    target = Path(expanded)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()
    return root, target


def _approved_paths() -> set[Path]:
    raw = os.environ.get("CLAUDE_AUTO_APPROVED_OUTSIDE_PATHS", "[]")
    try:
        values = json.loads(raw)
    except Exception:
        return set()
    out: set[Path] = set()
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                out.add(Path(value).expanduser().resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                continue
    return out


def _approved_command_hashes() -> set[str]:
    raw = os.environ.get("CLAUDE_AUTO_APPROVED_OUTSIDE_COMMAND_HASHES", "[]")
    try:
        values = json.loads(raw)
    except Exception:
        return set()
    return {
        str(value).lower()
        for value in values if isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdefABCDEF" for ch in value)
    }


def _approved_target(target: Path) -> bool:
    try:
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved in _approved_paths()


def _protected_paths() -> set[Path]:
    out: set[Path] = set()

    raw = os.environ.get("CLAUDE_AUTO_PROTECTED_REPO_PATHS", "[]")
    try:
        values = json.loads(raw)
    except Exception:
        values = []
    if isinstance(values, list):
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                out.add(Path(value).expanduser().resolve(strict=False))
            except (OSError, RuntimeError, ValueError):
                continue

    # Durable planning authority must take effect immediately after configuration,
    # even if the active Claude settings file was generated before that policy.
    # The state directory is package-owned external state, not repository text.
    state_raw = os.environ.get("CLAUDE_AUTONOMY_STATE_DIR")
    root_raw = os.environ.get("CLAUDE_AUTO_REPO_ROOT")
    if state_raw and root_raw:
        try:
            policy_path = (
                Path(state_raw).expanduser().resolve()
                / "planning-repair"
                / "policy.json"
            )
            policy = json.loads(policy_path.read_text())
            canonical = policy.get("canonical_plan") if isinstance(policy, dict) else None
            if isinstance(canonical, str) and canonical.strip():
                root = Path(root_raw).expanduser().resolve()
                target = (root / canonical).resolve(strict=False)
                target.relative_to(root)
                out.add(target)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            # An unreadable/malformed optional policy cannot broaden authority.
            pass
    return out


def _planning_policy_invalid() -> bool:
    state_raw = os.environ.get("CLAUDE_AUTONOMY_STATE_DIR")
    root_raw = os.environ.get("CLAUDE_AUTO_REPO_ROOT")
    if not state_raw or not root_raw:
        return False
    try:
        policy_path = (
            Path(state_raw).expanduser().resolve()
            / "planning-repair"
            / "policy.json"
        )
        if not policy_path.exists():
            return False
        policy = json.loads(policy_path.read_text())
        if not isinstance(policy, dict):
            return True
        canonical = policy.get("canonical_plan")
        if not isinstance(canonical, str) or not canonical.strip():
            return True
        rel = Path(canonical)
        if rel.is_absolute() or ".." in rel.parts or rel == Path("."):
            return True
        root = Path(root_raw).expanduser().resolve()
        target = (root / rel).resolve(strict=False)
        target.relative_to(root)
        return False
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return True


def _protected_target(target: Path) -> bool:
    try:
        return target.resolve(strict=False) in _protected_paths()
    except (OSError, RuntimeError, ValueError):
        return True


def _outside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return False
    except ValueError:
        return True


def _bash_tokens(command: str) -> list[str]:
    # punctuation_chars makes redirects visible as tokens while shlex still
    # respects shell quoting.  This is deliberately a narrow path guard, not a
    # shell interpreter.
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
SHELL_WRAPPERS = {"sudo", "command", "env"}
NESTED_SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}


def _split_segments(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if cur:
                out.append(cur)
                cur = []
        else:
            cur.append(token)
    if cur:
        out.append(cur)
    return out


def _expand_shell_value(raw: str, variables: dict[str, str | None]) -> str | None:
    if "$(" in raw or chr(96) in raw or "$((" in raw:
        return None

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        value = variables.get(name)
        if value is None:
            value = os.environ.get(name)
        if value is None:
            raise KeyError(name)
        return value

    try:
        expanded = _VAR_RE.sub(repl, raw)
    except KeyError:
        return None
    # Any remaining dollar syntax is dynamic/unknown ($$, $?, positional args,
    # parameter operators, etc.) and cannot be proven repository-local.
    if "$" in expanded:
        return None
    return os.path.expanduser(expanded)


def _resolve_target(
    root: Path,
    cwd: Path,
    raw: str,
    variables: dict[str, str | None],
) -> Path | None:
    expanded = _expand_shell_value(raw, variables)
    if expanded is None:
        return None
    target = Path(expanded)
    try:
        return (cwd / target).resolve() if not target.is_absolute() else target.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _unwrap_command(seg: list[str], idx: int) -> int:
    while idx < len(seg):
        base = Path(seg[idx]).name
        if base not in SHELL_WRAPPERS:
            break
        if base == "env":
            idx += 1
            while idx < len(seg) and seg[idx].startswith("-"):
                # Conservative: env options that take arguments are uncommon in
                # generated engineering commands; if present, the eventual
                # command still remains subject to the shell/sandbox layers.
                idx += 1
            while idx < len(seg) and _ASSIGN_RE.match(seg[idx]):
                idx += 1
            continue
        idx += 1
        while idx < len(seg) and seg[idx].startswith("-"):
            idx += 1
    return idx


def _segment_write_targets(seg: list[str], cmd_idx: int) -> list[str]:
    out: list[str] = []

    # Output/error redirections. shlex returns punctuation runs such as ">" or
    # ">>" (and may return "&>" as one token).
    for i, token in enumerate(seg[:-1]):
        if ">" in token and token not in {"&&", "||"}:
            out.append(seg[i + 1])

    if cmd_idx >= len(seg):
        return out
    cmd = Path(seg[cmd_idx]).name
    args = seg[cmd_idx + 1 :]
    operands = [
        x for x in args
        if x not in {">", ">>", "<", "<<", "<>"} and not x.startswith("-")
    ]

    if cmd in {"touch", "mkdir", "rmdir", "rm", "unlink", "truncate", "chmod", "chown"}:
        out.extend(operands)
    elif cmd in {"cp", "mv", "install", "ln", "rsync"} and operands:
        out.append(operands[-1])
        for i, token in enumerate(args[:-1]):
            if token in {"-t", "--target-directory"}:
                out.append(args[i + 1])
            elif token.startswith("--target-directory="):
                out.append(token.split("=", 1)[1])
    elif cmd == "tee":
        out.extend(operands)
    elif cmd in {"sed", "perl"} and any(x == "-i" or x.startswith("-i") for x in args):
        out.extend(operands)
    elif cmd == "dd":
        out.extend(x.split("=", 1)[1] for x in args if x.startswith("of="))
    elif cmd in {"curl", "wget"}:
        for i, token in enumerate(args[:-1]):
            if token in {"-o", "--output", "-O"}:
                out.append(args[i + 1])
            elif token.startswith("--output="):
                out.append(token.split("=", 1)[1])
    elif cmd == "git":
        # Fence alternate worktrees/git dirs and the obvious subcommands that
        # accept an explicit output/destination path.
        for i, token in enumerate(args[:-1]):
            if token in {"-C", "--git-dir", "--work-tree"}:
                out.append(args[i + 1])
        for token in args:
            if token.startswith(("--git-dir=", "--work-tree=")):
                out.append(token.split("=", 1)[1])
        if "clone" in args and operands:
            out.append(operands[-1])
        if "worktree" in args and "add" in args and operands:
            out.append(operands[-1])
        for i, token in enumerate(args[:-1]):
            if token in {"-o", "--output"}:
                out.append(args[i + 1])
            elif token.startswith("--output="):
                out.append(token.split("=", 1)[1])
    return out


def _guard_bash(
    command: str,
    root_raw: str,
    *,
    initial_cwd: Path | None = None,
    inherited_vars: dict[str, str | None] | None = None,
) -> tuple[str, str]:
    if not command:
        return "deny", "Claude Auto Bash write-boundary guard received an empty command."
    digest = hashlib.sha256(command.encode()).hexdigest()
    if digest in _approved_command_hashes():
        return "allow", "Exact user-approved outside-repository Bash command."
    try:
        tokens = _bash_tokens(command)
    except ValueError:
        return "deny", "Claude Auto Bash write-boundary guard could not parse the command safely."

    root = Path(root_raw).expanduser().resolve()
    if _planning_policy_invalid():
        return "deny", "Claude Auto durable planning policy is invalid; repository mutation is blocked fail-closed."
    cwd = (initial_cwd or root).resolve()
    variables: dict[str, str | None] = dict(inherited_vars or {})
    variables.setdefault("PWD", str(cwd))
    directory_stack: list[Path] = []

    for seg in _split_segments(tokens):
        local_vars = dict(variables)
        idx = 0
        assignments: dict[str, str | None] = {}
        while idx < len(seg):
            m = _ASSIGN_RE.match(seg[idx])
            if not m:
                break
            value = _expand_shell_value(m.group(2), {**local_vars, **assignments})
            assignments[m.group(1)] = value
            idx += 1

        # A standalone assignment persists in the shell for later segments.
        if idx >= len(seg):
            variables.update(assignments)
            continue

        # export/readonly/declare with NAME=value also persists.
        builtin = Path(seg[idx]).name
        if builtin in {"export", "readonly", "declare", "typeset"}:
            changed = False
            for token in seg[idx + 1 :]:
                m = _ASSIGN_RE.match(token)
                if not m:
                    continue
                variables[m.group(1)] = _expand_shell_value(
                    m.group(2), {**variables, **assignments}
                )
                changed = True
            if changed:
                continue

        effective_vars = {**variables, **assignments}
        cmd_idx = _unwrap_command(seg, idx)
        if cmd_idx >= len(seg):
            continue
        cmd = Path(seg[cmd_idx]).name
        args = seg[cmd_idx + 1 :]

        # Directory changes are part of the path boundary.  Never permit the
        # shell to establish an outside cwd and then use apparently-relative
        # write targets.
        if cmd in {"cd", "pushd"}:
            raw_target = next((x for x in args if not x.startswith("-")), None)
            if raw_target is None:
                return "deny", f"{cmd} without an explicit repository-local target is denied."
            target = _resolve_target(root, cwd, raw_target, effective_vars)
            if target is None:
                return "deny", f"Claude Auto could not safely resolve {cmd} target: {raw_target}"
            if _outside(root, target):
                return "deny", f"{cmd} outside the selected repository root is denied: {target}"
            if cmd == "pushd":
                directory_stack.append(cwd)
            cwd = target
            variables["PWD"] = str(cwd)
            continue
        if cmd == "popd":
            if directory_stack:
                cwd = directory_stack.pop()
                variables["PWD"] = str(cwd)
            continue

        # Recursively inspect quoted nested shell payloads.
        if cmd in NESTED_SHELLS:
            for i, token in enumerate(args[:-1]):
                if token in {"-c", "-lc", "-cl"}:
                    return _guard_bash(
                        args[i + 1],
                        root_raw,
                        initial_cwd=cwd,
                        inherited_vars=effective_vars,
                    )

        for raw in _segment_write_targets(seg, cmd_idx):
            if not raw or raw in {"/dev/null", "-"}:
                continue
            target = _resolve_target(root, cwd, raw, effective_vars)
            if target is None:
                return "deny", f"Claude Auto could not safely resolve Bash write target: {raw}"
            if _protected_target(target):
                return "deny", f"Bash mutation of a protected repository path is denied: {target}"
            if _outside(root, target) and not _approved_target(target):
                return "deny", f"Bash mutation outside the selected repository root is denied: {target}"

    return "allow", "No statically identifiable Bash write target escapes the selected repository root."


def main() -> int:
    try:
        event = json.load(sys.stdin)
        tool = str(event.get("tool_name") or "")
        ti = event.get("tool_input") or {}
        root_raw = os.environ.get("CLAUDE_AUTO_REPO_ROOT")

        if tool == "Bash":
            if not root_raw:
                decision("deny", "Claude Auto Bash write-boundary guard has no repository root; failing closed.")
                return 0
            value, reason = _guard_bash(str(ti.get("command") or ""), root_raw)
            decision(value, reason)
            return 0

        if tool not in {"Write", "Edit", "NotebookEdit"}:
            decision("allow", "Tool is not a direct file mutation tool.")
            return 0
        if _planning_policy_invalid():
            decision("deny", "Claude Auto durable planning policy is invalid; repository mutation is blocked fail-closed.")
            return 0
        raw = ti.get("file_path") or ti.get("notebook_path")
        if not root_raw:
            decision("deny", "Claude Auto write-boundary guard has no repository root; failing closed.")
            return 0
        if not raw:
            decision("deny", "Claude Auto write-boundary guard could not identify the mutation path.")
            return 0
        root, target = _root_and_target(root_raw, str(raw))
        if _protected_target(target):
            decision("deny", f"Direct mutation of a protected repository path is denied: {target}")
            return 0
        if _outside(root, target) and not _approved_target(target):
            decision("deny", f"Direct mutation outside the selected repository root is denied: {target}")
            return 0
        decision("allow", "Direct mutation is inside the selected repository root.")
        return 0
    except Exception as exc:
        decision("deny", f"Claude Auto write-boundary guard failed closed: {type(exc).__name__}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
