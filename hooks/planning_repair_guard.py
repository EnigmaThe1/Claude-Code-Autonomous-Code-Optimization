#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def _decision(value: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": value,
            "permissionDecisionReason": reason,
        }
    }, separators=(",", ":")))


def main() -> int:
    try:
        event = json.load(sys.stdin)
        tool = str(event.get("tool_name") or "")
        ti = event.get("tool_input") or {}
        root_raw = os.environ.get("CLAUDE_AUTO_PLAN_REPAIR_ROOT")
        plan_raw = os.environ.get("CLAUDE_AUTO_PLAN_REPAIR_PATH")
        if not root_raw or not plan_raw:
            _decision("deny", "Planning repair guard has no trusted root/plan binding.")
            return 0
        root = Path(root_raw).expanduser().resolve()
        plan = Path(plan_raw).expanduser().resolve()

        if tool == "Bash" or tool == "NotebookEdit":
            _decision("deny", f"{tool} is not available to the planning repair architect.")
            return 0
        if tool not in {"Edit", "Write"}:
            _decision("allow", "Read-only planning tool.")
            return 0

        raw = ti.get("file_path")
        if not raw:
            _decision("deny", "Planning repair mutation has no file path.")
            return 0
        target = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        target = (root / target).resolve() if not target.is_absolute() else target.resolve()
        if target != plan:
            _decision("deny", f"Planning repair architect may modify only the canonical plan: {plan}")
            return 0
        _decision("allow", "Canonical plan mutation allowed in the dedicated planning worktree.")
        return 0
    except Exception as exc:
        _decision("deny", f"Planning repair guard failed closed: {type(exc).__name__}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
