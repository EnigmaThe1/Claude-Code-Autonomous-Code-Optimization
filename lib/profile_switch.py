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
import secrets
import sys
from pathlib import Path
from typing import Any

from repo_identity import find_repo_root, repo_state_dir
from runtime_paths import utcnow
from state_store import json_dump, load_json

HOT_SWITCH_PROFILES = ("strict", "balanced", "unattended")
REQUEST_NAME = "profile-switch-request.json"
HISTORY_NAME = "profile-switch-history.jsonl"


class ProfileSwitchInterrupt(BaseException):
    """Internal signal used only to restart an interactive Claude session safely."""

    def __init__(self, signum: int):
        super().__init__(f"profile switch requested by signal {signum}")
        self.signum = signum


def request_path(state_dir: Path) -> Path:
    return state_dir / REQUEST_NAME


def history_path(state_dir: Path) -> Path:
    return state_dir / HISTORY_NAME


def _append_history(state_dir: Path, payload: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str) + "\n"
    path = history_path(state_dir)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _normalise_target(target: str) -> str:
    value = str(target or "").strip().lower()
    if value == "isolated-full":
        raise ValueError(
            "isolated-full cannot be hot-switched on the current host; it requires a separately attested disposable VM/container boundary."
        )
    if value not in HOT_SWITCH_PROFILES:
        raise ValueError(
            "Hot switching supports only strict, balanced, and unattended."
        )
    return value


def process_start_token(pid: int) -> str | None:
    """Linux process birth token used to reject stale/reused supervisor PIDs."""
    if pid <= 0:
        return None
    stat = Path(f"/proc/{pid}/stat")
    try:
        raw = stat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    # proc(5): field 22 is starttime. fields[0] here is field 3.
    if len(fields) <= 19:
        return None
    return fields[19]


def _parent_pid(pid: int) -> int | None:
    if pid <= 1:
        return None
    status = Path(f"/proc/{pid}/status")
    try:
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("PPid:"):
                value = int(line.split(":", 1)[1].strip())
                return value if value > 0 else None
    except (OSError, ValueError):
        return None
    return None


def process_descends_from(pid: int, ancestor_pid: int, *, max_depth: int = 128) -> bool:
    """Return True only when /proc proves pid is below the active supervisor."""
    if pid <= 0 or ancestor_pid <= 0 or pid == ancestor_pid:
        return pid == ancestor_pid and pid > 0
    seen: set[int] = set()
    current = pid
    for _ in range(max_depth):
        if current in seen:
            return False
        seen.add(current)
        parent = _parent_pid(current)
        if parent is None:
            return False
        if parent == ancestor_pid:
            return True
        current = parent
    return False


def supervisor_is_live(state: dict[str, Any]) -> bool:
    try:
        pid = int(state.get("supervisor_pid") or 0)
    except (TypeError, ValueError):
        return False
    expected = str(state.get("supervisor_start_token") or "")
    if not pid or not expected:
        return False
    actual = process_start_token(pid)
    if actual != expected:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pending_profile_switch(state_dir: Path) -> dict[str, Any] | None:
    obj = load_json(request_path(state_dir), {})
    if not isinstance(obj, dict) or not obj.get("request_id"):
        return None
    return obj


def request_profile_switch(
    state_dir: Path,
    target_profile: str,
    *,
    origin: str,
    current_profile: str | None = None,
    session_id: str | None = None,
    supervisor_pid: int | None = None,
    supervisor_start_token: str | None = None,
) -> dict[str, Any]:
    target = _normalise_target(target_profile)
    existing = pending_profile_switch(state_dir)
    now = utcnow()
    material = (
        f"{now}|{target}|{origin}|{session_id or ''}|{secrets.token_hex(16)}"
    )
    request_id = hashlib.sha256(material.encode()).hexdigest()[:20]
    request = {
        "request_id": request_id,
        "requested_at": now,
        "origin": origin,
        "target_profile": target,
        "current_profile_at_request": current_profile,
        "session_id": session_id,
        "supervisor_pid": supervisor_pid,
        "supervisor_start_token": supervisor_start_token,
    }
    if existing:
        _append_history(state_dir, {
            "event": "superseded",
            "at": now,
            "request_id": existing.get("request_id"),
            "superseded_by": request_id,
            "target_profile": existing.get("target_profile"),
        })
    json_dump(request_path(state_dir), request)
    try:
        request_path(state_dir).chmod(0o600)
    except OSError:
        pass
    _append_history(state_dir, {"event": "requested", **request})
    return request


def finish_profile_switch(
    state_dir: Path,
    request: dict[str, Any],
    *,
    outcome: str,
    previous_profile: str | None,
    active_profile: str | None,
    reason: str | None = None,
) -> None:
    now = utcnow()
    current = pending_profile_switch(state_dir)
    if current and current.get("request_id") == request.get("request_id"):
        request_path(state_dir).unlink(missing_ok=True)
    record = {
        "event": outcome,
        "at": now,
        "request_id": request.get("request_id"),
        "origin": request.get("origin"),
        "requested_profile": request.get("target_profile"),
        "previous_profile": previous_profile,
        "active_profile": active_profile,
    }
    if reason:
        record["reason"] = str(reason)[:1600]
    _append_history(state_dir, record)


def profile_status(state_dir: Path) -> dict[str, Any]:
    state = load_json(state_dir / "state.json", {})
    return {
        "active_profile": state.get("autonomy_profile") or "balanced",
        "supervisor_mode": state.get("supervisor_mode"),
        "supervisor_live": supervisor_is_live(state),
        "pending_switch": pending_profile_switch(state_dir),
        "last_switch": state.get("last_profile_switch"),
    }


def profile_action(args) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    state_dir = repo_state_dir(root)
    if getattr(args, "profile_command", None) == "status":
        print(json.dumps(profile_status(state_dir), indent=2, sort_keys=True))
        return 0

    if getattr(args, "profile_command", None) != "request":
        return 2

    if os.environ.get("CLAUDECODE") == "1" or os.environ.get("CLAUDE_AUTO_HEADLESS") == "1":
        raise SystemExit(
            "Profile switching is human-only. Run this command from a separate top-level operator terminal, "
            "or use /profile inside an interactive claude-auto start session."
        )
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            "Profile switching requires an interactive human operator TTY."
        )

    state = load_json(state_dir / "state.json", {})
    if not supervisor_is_live(state):
        raise SystemExit("No live Claude Auto supervisor is available for hot switching.")
    supervisor_pid = int(state.get("supervisor_pid") or 0)
    if process_descends_from(os.getpid(), supervisor_pid):
        raise SystemExit(
            "Profile switching is human-only. The request originated inside the active Claude Auto process tree; "
            "use a separate top-level operator terminal instead."
        )
    mode = str(state.get("supervisor_mode") or "")
    if mode == "start":
        raise SystemExit(
            "Interactive claude-auto start sessions must switch with /profile <strict|balanced|unattended> "
            "inside that Claude session so its exact session ID can be resumed."
        )
    if mode != "run":
        raise SystemExit(f"Unsupported live supervisor mode for hot switching: {mode or 'unknown'}")

    target = _normalise_target(getattr(args, "target_profile", ""))
    current = str(state.get("autonomy_profile") or "balanced")
    if target == "unattended" and current != "unattended":
        print(
            "Unattended removes Claude Auto's normal operational restrictions for this active run.\n"
            "Type 'unattended' to confirm: ",
            end="",
            flush=True,
        )
        if input().strip().lower() != "unattended":
            print("Profile switch cancelled.")
            return 2

    request = request_profile_switch(
        state_dir,
        target,
        origin="operator-shell",
        current_profile=current,
        supervisor_pid=int(state.get("supervisor_pid") or 0),
        supervisor_start_token=str(state.get("supervisor_start_token") or ""),
    )
    print(
        f"Queued profile switch {current} -> {target} "
        f"(request {request['request_id']}). It will apply at the next safe supervisor boundary."
    )
    return 0
