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

import argparse
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Callable

from environment_policy import capture_resume_environment
from repo_identity import SupervisorLease, find_repo_root, repo_id, repo_state_dir
from repo_runtime import activate
from permission_escalation import reset_permission_epoch
from state_store import json_dump, load_json


def _systemd_safe_text(value: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("systemd unit values cannot contain control characters")
    # systemd expands % specifiers even inside quoted ExecStart arguments.
    return value.replace("%", "%%")


def _systemd_escape_arg(value: str) -> str:
    safe = _systemd_safe_text(value)
    return '"' + safe.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _service_unit_name(root: Path) -> str:
    return f"claude-auto-{repo_id(root)[:16]}.service"


def _service_unit_path(root: Path, *, home: Path | None = None) -> Path:
    home = home or Path.home()
    return home / ".config" / "systemd" / "user" / _service_unit_name(root)


def _service_unit_text(root: Path, *, home: Path | None = None) -> str:
    home = home or Path.home()
    launcher = home / ".local" / "bin" / "claude-auto"
    root_text = _systemd_safe_text(str(root))
    cmd = f"{_systemd_escape_arg(str(launcher))} run --repo {_systemd_escape_arg(str(root))} --resume-config"
    return textwrap.dedent(f"""
    [Unit]
    Description=Claude Auto autonomous resume service for {root_text}
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    ExecStart={cmd}
    Restart=on-failure
    RestartSec=20
    # 7 is WAITING_RETRYABLE_LIMIT: the opt-in service should try again
    # after RestartSec once the bounded in-process retry window is exhausted.
    RestartPreventExitStatus=3 4 5 6 8
    TimeoutStopSec=30
    KillMode=control-group

    [Install]
    WantedBy=default.target
    """).lstrip()


def service_action(
    args: argparse.Namespace,
    *,
    home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> int:
    home = home or Path.home()
    which = which or shutil.which
    if sys.platform != "linux":
        print("claude-auto service is currently supported only on Linux/systemd user sessions.", file=sys.stderr)
        return 2
    root = find_repo_root(args.repo)
    path = _service_unit_path(root, home=home)
    action = args.service_action
    if action == "status":
        print(json.dumps({"unit": _service_unit_name(root), "path": str(path), "installed": path.exists()}, indent=2))
        return 0
    if action == "remove":
        if which("systemctl"):
            subprocess.run(["systemctl", "--user", "disable", "--now", _service_unit_name(root)], text=True, capture_output=True)
        path.unlink(missing_ok=True)
        if which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], text=True, capture_output=True)
        print(f"Removed {_service_unit_name(root)}")
        return 0
    sd = repo_state_dir(root)
    with SupervisorLease(sd, root):
        activate(root)
        state = load_json(sd / "state.json", {})
        if getattr(args, "objective", None):
            new_objective = args.objective
            objective_changed = state.get("objective") != new_objective
            if objective_changed:
                cfg = state.get("resume_config")
                if isinstance(cfg, dict) and cfg.get("profile") == "unattended":
                    cfg = dict(cfg)
                    cfg["profile"] = "balanced"
                    cfg["session_settings"] = "hermetic"
                    state["resume_config"] = cfg
                if state.get("autonomy_profile") == "unattended":
                    state["autonomy_profile"] = "balanced"
                reset_permission_epoch(
                    state,
                    new_objective_hash=None,
                    reason="service objective changed",
                )
                state["status"] = "READY"
                state["last_result_status"] = None
                state["blocker"] = None
            state["objective"] = new_objective
            state["status"] = state.get("status") if state.get("status") not in {None, "COMPLETE"} else "READY"
            json_dump(sd / "state.json", state)
        if not state.get("objective"):
            raise SystemExit("Install the resume service only after an objective exists, or provide --objective.")
        state["resume_environment"] = capture_resume_environment(root)
        json_dump(sd / "state.json", state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_service_unit_text(root, home=home))
    if which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], text=True, capture_output=True)
        if getattr(args, "start", False):
            cp = subprocess.run(["systemctl", "--user", "enable", "--now", _service_unit_name(root)], text=True, capture_output=True)
            if cp.returncode != 0:
                print((cp.stderr or cp.stdout).strip(), file=sys.stderr)
                return cp.returncode
    print(f"Installed {_service_unit_name(root)} at {path}")
    return 0
