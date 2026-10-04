#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from profile_switch import request_profile_switch, supervisor_is_live
from repo_identity import find_repo_root, repo_state_dir
from state_store import load_json


RAW_RE = re.compile(r"^/profile(?:\\s+([a-zA-Z0-9_-]+))?\\s*$", re.IGNORECASE)
SENTINEL_RE = re.compile(
    r"^CLAUDE_AUTO_HUMAN_PROFILE_SWITCH(?:\\s+([a-zA-Z0-9_-]+))?\\s*$",
    re.IGNORECASE,
)


def _reply(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}, separators=(",", ":")))
    return 0


def _target(prompt: str) -> str | None:
    text = prompt.strip()
    for rx in (RAW_RE, SENTINEL_RE):
        match = rx.fullmatch(text)
        if match:
            return (match.group(1) or "status").lower()
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return 0
    target = _target(str(payload.get("prompt") or ""))
    if target is None:
        return 0

    try:
        state_dir_env = os.environ.get("CLAUDE_AUTONOMY_STATE_DIR")
        if state_dir_env:
            state_dir = Path(state_dir_env).expanduser().resolve()
        else:
            root = find_repo_root(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR"))
            state_dir = repo_state_dir(root)
        state = load_json(state_dir / "state.json", {})
    except Exception as exc:
        return _reply(f"Claude Auto profile control is unavailable: {exc}")

    current = str(state.get("autonomy_profile") or "balanced")
    if target == "status":
        pending = load_json(state_dir / "profile-switch-request.json", {})
        suffix = ""
        if isinstance(pending, dict) and pending.get("target_profile"):
            suffix = f"; pending switch -> {pending.get('target_profile')}"
        return _reply(f"Claude Auto active profile: {current}{suffix}.")

    if target == current:
        return _reply(f"Claude Auto profile {current} is already active; no restart was needed.")

    if target == "isolated-full":
        return _reply(
            "isolated-full cannot be hot-switched on this host. It requires a separately attested disposable VM/container."
        )
    if target not in {"strict", "balanced", "unattended"}:
        return _reply("Usage: /profile status|strict|balanced|unattended")

    if not supervisor_is_live(state) or state.get("supervisor_mode") != "start":
        return _reply(
            "No live interactive claude-auto start supervisor can apply this slash command. "
            "For a headless claude-auto run, use 'claude-auto profile request <profile>' from a separate operator terminal."
        )

    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return _reply("Claude Code did not provide a session ID, so the profile switch was refused rather than risking context loss.")

    try:
        request = request_profile_switch(
            state_dir,
            target,
            origin="user-prompt",
            current_profile=current,
            session_id=session_id,
            supervisor_pid=int(state.get("supervisor_pid") or 0),
            supervisor_start_token=str(state.get("supervisor_start_token") or ""),
        )
        pid = int(state.get("supervisor_pid") or 0)
        # Flush the hook decision before signalling the parent supervisor. The
        # signal deliberately terminates the Claude child, so emitting first
        # avoids losing the operator-facing acknowledgement in that handoff.
        message = (
            f"Human profile switch accepted: {current} -> {target} "
            f"(request {request['request_id']}). The supervisor will resume this same Claude session under the new profile."
        )
        print(json.dumps({"decision": "block", "reason": message}, separators=(",", ":")), flush=True)
        os.kill(pid, signal.SIGUSR1)
        return 0
    except Exception as exc:
        return _reply(f"Profile switch failed safely; active profile remains {current}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
