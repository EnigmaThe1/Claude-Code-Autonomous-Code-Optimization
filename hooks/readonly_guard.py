#!/usr/bin/env python3
from __future__ import annotations
import json
import shlex
import sys

SHELL_META = (";", "&&", "||", "|", ">", "<", "`", "$(", "${")
SIMPLE = {"pwd", "ls", "cat", "head", "tail", "wc", "stat", "file", "readlink", "realpath", "rg", "grep"}
GIT_READ = {
    "status", "diff", "show", "log", "rev-parse", "describe", "ls-files",
    "ls-tree", "cat-file", "grep", "name-rev", "merge-base", "remote",
}

def decision(value: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": value,
            "permissionDecisionReason": reason,
        }
    }, separators=(",", ":")))

def git_safe(argv: list[str]) -> bool:
    if len(argv) < 2 or argv[0] != "git":
        return False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in {"-C", "--git-dir", "--work-tree", "--namespace"}:
            i += 2
            continue
        if a.startswith(("--git-dir=", "--work-tree=", "--namespace=")):
            i += 1
            continue
        if a in {"--no-pager", "--literal-pathspecs", "--no-literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs"}:
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] not in GIT_READ:
        return False
    sub = argv[i]
    rest = argv[i + 1:]
    if sub == "remote" and any(x in {"add", "remove", "rm", "rename", "set-url", "set-head", "prune", "update"} for x in rest):
        return False
    if any(x in {"--ext-diff", "--textconv"} for x in rest):
        return False
    # Several otherwise read-only git subcommands can write via output options.
    if any(x in {"--output", "-o", "-O"} or x.startswith("--output=") for x in rest):
        return False
    return True

def simple_safe(argv: list[str]) -> bool:
    if not argv or argv[0] not in SIMPLE:
        return False
    # Commands with recursive/external helper semantics are intentionally excluded.
    if argv[0] == "grep" and any(x in {"--exclude-from", "-f", "--file"} for x in argv[1:]):
        return False
    if argv[0] == "rg" and any(
        x in {"--pre", "--pre-glob"} or x.startswith(("--pre=", "--pre-glob="))
        for x in argv[1:]
    ):
        return False
    return True

def main() -> int:
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            decision("allow", "Non-Bash tool handled by static read-only settings.")
            return 0
        command = str((event.get("tool_input") or {}).get("command") or "").strip()
        if not command or any(m in command for m in SHELL_META) or "\n" in command or "\r" in command:
            decision("deny", "Read-only session: compound, redirected, substituted, or empty shell commands are denied.")
            return 0
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            decision("deny", "Read-only session: command could not be parsed safely.")
            return 0
        if git_safe(argv) or simple_safe(argv):
            decision("allow", "Read-only command allowlisted by claude-auto.")
        else:
            decision("deny", "Read-only session: Bash command is not on the deterministic non-mutating allowlist.")
        return 0
    except Exception as exc:
        decision("deny", f"Read-only guard failed closed: {type(exc).__name__}")
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
