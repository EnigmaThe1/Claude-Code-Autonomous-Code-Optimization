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

import os
import sys
from pathlib import Path

from profile_switch import process_descends_from, supervisor_is_live
from repo_identity import repo_state_dir
from state_store import load_json


def require_top_level_operator(root: Path, operation: str) -> None:
    """Require human operator context for external authority-changing CLI actions.

    Supervisor-internal automation calls the underlying functions directly and is
    therefore unaffected.  The CLI path requires a human TTY and, when a live
    supervisor exists, rejects any caller below that supervisor process tree.
    """
    if os.environ.get("CLAUDECODE") == "1" or os.environ.get("CLAUDE_AUTO_HEADLESS") == "1":
        raise ValueError(
            f"{operation} is human/operator authority and cannot run inside an active Claude worker"
        )
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(f"{operation} requires an interactive top-level operator TTY")

    state = load_json(repo_state_dir(root) / "state.json", {})
    if isinstance(state, dict) and supervisor_is_live(state):
        try:
            supervisor_pid = int(state.get("supervisor_pid") or 0)
        except (TypeError, ValueError):
            supervisor_pid = 0
        if supervisor_pid and process_descends_from(os.getpid(), supervisor_pid):
            raise ValueError(
                f"{operation} originated inside the active Claude Auto supervisor process tree; "
                "use a separate top-level operator terminal"
            )
