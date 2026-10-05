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
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


_LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from execution_envelope import (  # noqa: E402
    ExecutionEnvelopeError,
    evaluate_active_workspace,
    load_active_execution_envelope,
    load_task_violation,
    record_task_authority_failure,
    record_task_violation,
    task_owned_mode,
)
from state_store import StateCorruptionError, load_json  # noqa: E402


_MUTATING_TOOL_NAMES = {"Write", "Edit", "NotebookEdit", "Bash"}


def _block(reason: str) -> None:
    text = str(reason)[:4000]
    print(json.dumps({
        "decision": "block",
        "reason": text,
        "hookSpecificOutput": {
            "hookEventName": "PostToolBatch",
            "additionalContext": (
                "Claude Auto P3 stopped the task round because repository state "
                "no longer satisfies the active ExecutionEnvelope. Do not try to "
                "bypass or self-expand task authority. Reconcile the reported "
                "repository state through the package/operator boundary."
            ),
        },
    }, separators=(",", ":")))


def _batch_evidence(event: dict[str, Any]) -> dict[str, Any]:
    calls = event.get("tool_calls")
    compact: list[dict[str, str]] = []
    if isinstance(calls, list):
        for row in calls[:200]:
            if not isinstance(row, dict):
                continue
            compact.append({
                "tool_name": str(row.get("tool_name") or "")[:200],
                "tool_use_id": str(row.get("tool_use_id") or "")[:300],
            })
    return {
        "session_id": str(event.get("session_id") or "")[:300],
        "hook_event_name": str(event.get("hook_event_name") or "")[:100],
        "cwd": str(event.get("cwd") or "")[:1000],
        "tool_calls": compact,
    }


_PROMOTION_REASON_RE = re.compile(
    r"^promote-ff advanced HEAD from ([0-9a-f]{40,64}) to ([0-9a-f]{40,64})$"
)


def _single_promote_ff_command(event: dict[str, Any]) -> bool:
    """Recognise one simple package promotion command and nothing else."""
    rows = event.get("tool_calls")
    if not isinstance(rows, list) or len(rows) != 1:
        return False
    row = rows[0]
    if not isinstance(row, dict) or str(row.get("tool_name") or "") != "Bash":
        return False
    tool_input = row.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return False

    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return False
    if not tokens or any(token in {";", "&&", "||", "|", "&"} for token in tokens):
        return False

    idx = 0
    while idx < len(tokens) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[idx]
    ):
        idx += 1
    if idx < len(tokens) and Path(tokens[idx]).name == "command":
        idx += 1
    if idx < len(tokens) and Path(tokens[idx]).name == "env":
        idx += 1
        while idx < len(tokens) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[idx]
        ):
            idx += 1
    if idx < len(tokens) and Path(tokens[idx]).name == "sudo":
        idx += 1
        while idx < len(tokens) and tokens[idx].startswith("-"):
            idx += 1

    return (
        idx + 1 < len(tokens)
        and Path(tokens[idx]).name == "claude-auto"
        and tokens[idx + 1] == "promote-ff"
    )


def _git_head(root: Path) -> str | None:
    cp = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
        text=True,
        capture_output=True,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    return cp.stdout.strip().lower()


def _expected_promotion_transition(
    root: Path,
    durable: dict[str, Any],
    event: dict[str, Any],
) -> bool:
    if not _single_promote_ff_command(event):
        return False
    if durable.get("task_source_sha256") is not None:
        return False
    if any(
        durable.get(key) is not None
        for key in (
            "active_task_id",
            "active_task_spec_sha256",
            "active_execution_envelope_sha256",
        )
    ):
        return False
    reason = durable.get("task_authority_invalidated_reason")
    if not isinstance(reason, str):
        return False
    match = _PROMOTION_REASON_RE.fullmatch(reason)
    if not match:
        return False
    before, target = (part.lower() for part in match.groups())
    if before == target:
        return False
    return _git_head(root) == target


def _promotion_handoff() -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolBatch",
            "additionalContext": (
                "Claude Auto verified that the package-owned promote-ff broker "
                "advanced HEAD and intentionally invalidated the previous "
                "ExecutionEnvelope. That task authority is now closed. Do not "
                "perform further repository mutation in this turn; finish the "
                "round with AUTONOMY_STATUS: CONTINUE so the outer supervisor "
                "can resolve fresh TaskSourceSet/ExecutionEnvelope authority."
            ),
        },
    }, separators=(",", ":")))


def _has_mutating_tool(event: dict[str, Any]) -> bool:
    rows = event.get("tool_calls")
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict)
        and str(row.get("tool_name") or "") in _MUTATING_TOOL_NAMES
        for row in rows
    )


def main() -> int:
    root_raw = os.environ.get("CLAUDE_AUTO_REPO_ROOT")
    if not root_raw:
        return 0
    root = Path(root_raw).expanduser().resolve()
    state_dir_raw = os.environ.get("CLAUDE_AUTONOMY_STATE_DIR")
    state_dir = (
        Path(state_dir_raw).expanduser().resolve()
        if state_dir_raw
        else None
    )
    authority_raw = os.environ.get("CLAUDE_AUTO_AUTHORITY_ROOT")
    authority_root = (
        Path(authority_raw).expanduser().resolve()
        if authority_raw
        else root
    )

    try:
        event = json.load(sys.stdin)
    except Exception:
        # Without valid hook input there is no trustworthy batch identity. If
        # task authority is active, fail closed through an ordinary hook error.
        try:
            if (Path(os.environ.get("CLAUDE_AUTONOMY_STATE_DIR", "")) / "tasks" / "execution-envelope.json").exists():
                _block("PostToolBatch input was malformed while task authority was active.")
        except Exception:
            pass
        return 0
    if not isinstance(event, dict):
        return 0

    batch = _batch_evidence(event)

    try:
        governed = task_owned_mode(root)
    except Exception as exc:
        record_task_authority_failure(
            root,
            reason=f"unable to resolve task-owned governance after tool batch: {exc}",
            tool_batch=batch,
            state_dir=state_dir,
        )
        _block(f"Task governance could not be verified after the tool batch: {exc}")
        return 0

    if not governed:
        return 0

    envelope_file = (
        Path(state_dir_raw).expanduser().resolve()
        / "tasks"
        / "execution-envelope.json"
        if state_dir_raw
        else None
    )
    envelope_exists = bool(envelope_file and envelope_file.exists())
    state_active = False
    if state_dir_raw:
        try:
            durable = load_json(
                Path(state_dir_raw).expanduser().resolve() / "state.json",
                {},
            )
            if not isinstance(durable, dict):
                raise StateCorruptionError("durable state is not an object")
            state_active = bool(durable.get("active_execution_envelope_sha256"))
        except (StateCorruptionError, OSError) as exc:
            record_task_authority_failure(
                root,
                reason=f"durable task state could not be verified after tool batch: {exc}",
                tool_batch=batch,
            )
            _block(f"Durable task authority state failed closed: {exc}")
            return 0

    if state_active != envelope_exists:
        record_task_authority_failure(
            root,
            reason=(
                "durable active-task binding disagrees with ExecutionEnvelope "
                f"file presence (state_active={state_active}, "
                f"envelope_exists={envelope_exists})"
            ),
            tool_batch=batch,
            state_dir=state_dir,
        )
        _block(
            "Durable active-task binding and ExecutionEnvelope storage disagree."
        )
        return 0

    active_hint = state_active and envelope_exists

    # In task-owned mode, mutating worker tools are not permitted to run a
    # product round before activation. PreToolUse blocks the statically visible
    # cases; PostToolBatch prevents the model from continuing after an opaque
    # mutating command/tool when no envelope exists.
    if not active_hint:
        if _expected_promotion_transition(root, durable, event):
            _promotion_handoff()
            return 0
        if _has_mutating_tool(event):
            record_task_authority_failure(
                root,
                reason="mutating tool batch completed in task-owned mode without an active ExecutionEnvelope",
                tool_batch=batch,
            )
            _block(
                "Task-owned repository mutation occurred without an active ExecutionEnvelope."
            )
        return 0

    try:
        existing = load_task_violation(root, state_dir=state_dir)
        if existing is not None:
            _block(
                "An unresolved task-envelope violation is already recorded; "
                "task execution remains blocked until package reconciliation succeeds."
            )
            return 0

        envelope = load_active_execution_envelope(
            root,
            state_dir=state_dir,
            authority_root=authority_root,
        )
        result = evaluate_active_workspace(root, envelope=envelope)
        if result["status"] == "VALID":
            return 0

        record_task_violation(
            root,
            envelope=envelope,
            violations=result["violations"],
            tool_batch=batch,
            state_dir=state_dir,
        )
        summary = "; ".join(
            f"{row['path']}: {row['reason']}"
            for row in result["violations"][:30]
        )
        _block("ExecutionEnvelope violation after tool batch: " + summary)
        return 0
    except ExecutionEnvelopeError as exc:
        record_task_authority_failure(
            root,
            reason=f"task authority integrity/current-state check failed after tool batch: {exc}",
            tool_batch=batch,
            state_dir=state_dir,
        )
        _block(f"Task authority failed closed after the tool batch: {exc}")
        return 0
    except Exception as exc:
        record_task_authority_failure(
            root,
            reason=f"unexpected PostToolBatch task guard failure: {type(exc).__name__}: {exc}",
            tool_batch=batch,
            state_dir=state_dir,
        )
        _block(
            f"Task authority guard failed closed after the tool batch: {type(exc).__name__}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
