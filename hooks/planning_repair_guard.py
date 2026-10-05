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

import json
import os
from pathlib import Path
import stat
import sys


_LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from repair_envelope import (  # noqa: E402
    RepairEnvelopeError,
    direct_repair_path_reason,
    load_repair_envelope_file,
)


def _decision(value: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": value,
            "permissionDecisionReason": reason,
        }
    }, separators=(",", ":")))


def _lexical_target(root: Path, raw: str) -> tuple[Path, str] | None:
    expanded = os.path.expandvars(os.path.expanduser(str(raw)))
    candidate = Path(expanded)
    try:
        absolute = (
            candidate
            if candidate.is_absolute()
            else root / candidate
        )
        absolute = Path(os.path.abspath(str(absolute)))
        rel = absolute.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    if not rel or rel == ".":
        return None
    return absolute, rel


def _has_symlink_component(root: Path, rel: str) -> bool:
    current = root
    for part in Path(rel).parts:
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
    return False


def main() -> int:
    try:
        event = json.load(sys.stdin)
        tool = str(event.get("tool_name") or "")
        ti = event.get("tool_input") or {}
        root_raw = os.environ.get("CLAUDE_AUTO_PLAN_REPAIR_ROOT")
        if not root_raw:
            _decision("deny", "Planning repair guard has no trusted root binding.")
            return 0
        root = Path(root_raw).expanduser().resolve()

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
        mapped = _lexical_target(root, str(raw))
        if mapped is None:
            _decision("deny", "Planning repair target escapes the dedicated worktree.")
            return 0
        target, rel = mapped
        if _has_symlink_component(root, rel):
            _decision("deny", "Planning repair target may not traverse a symlink.")
            return 0
        try:
            physical_parent = target.parent.resolve(strict=True)
            physical_parent.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            _decision("deny", "Planning repair target parent escapes the dedicated worktree.")
            return 0

        envelope_raw = os.environ.get("CLAUDE_AUTO_PLAN_REPAIR_ENVELOPE")
        if envelope_raw:
            envelope = load_repair_envelope_file(Path(envelope_raw))
            reason = direct_repair_path_reason(root, envelope, rel)
            if reason:
                _decision("deny", f"RepairEnvelope denial for {rel}: {reason}")
                return 0
            _decision("allow", f"RepairEnvelope-admitted repairable planning mutation: {rel}")
            return 0

        # RC3/P4 one-file compatibility until new repairs are routed through
        # the P5 RepairEnvelope lifecycle.
        plan_raw = os.environ.get("CLAUDE_AUTO_PLAN_REPAIR_PATH")
        if not plan_raw:
            _decision("deny", "Planning repair guard has no RepairEnvelope or legacy plan binding.")
            return 0
        plan = Path(plan_raw).expanduser().resolve()
        if target.resolve(strict=False) != plan:
            _decision("deny", f"Planning repair architect may modify only the canonical plan: {plan}")
            return 0
        _decision("allow", "Canonical plan mutation allowed in the dedicated planning worktree.")
        return 0
    except RepairEnvelopeError as exc:
        _decision("deny", f"Planning RepairEnvelope failed closed: {exc}")
        return 0
    except Exception as exc:
        _decision("deny", f"Planning repair guard failed closed: {type(exc).__name__}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
