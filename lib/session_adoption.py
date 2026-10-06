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
import re
from pathlib import Path
from typing import Any

from execution_envelope import git_head
from governance_contract import canonical_json_bytes
from planning_repair import load_active_repair
from repo_identity import repo_id, repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json
from task_workspace import load_active_task_workspace
from telemetry import read_runtime_events


class SessionAdoptionError(ValueError):
    pass


_SEMANTIC_KEYS = (
    "schema_version",
    "selector_type",
    "selector_sha256",
    "selector_display",
    "coordinator_repo_id",
    "working_directory",
    "product_sha",
    "governance_snapshot_sha256",
    "task_source_sha256",
    "active_task_id",
    "task_workspace_sha256",
    "execution_envelope_sha256",
    "repair_envelope_sha256",
    "autonomy_profile",
    "settings_sha256",
    "role",
    "lifecycle_state",
    "adopted_session_id",
)

_STATES = {"PREPARING", "LAUNCHED", "ADOPTED", "BLOCKED", "ENDED"}


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _state_root(root: Path, state_dir: Path | None = None) -> Path:
    return (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )


def _adoption_dir(state_root: Path) -> Path:
    return ensure_private_dir(state_root / "session-adoption")


def _active_path(state_root: Path) -> Path:
    return _adoption_dir(state_root) / "active.json"


def _history_dir(state_root: Path) -> Path:
    return ensure_private_dir(_adoption_dir(state_root) / "history")


def _safe_display(selector: str) -> str | None:
    value = selector.strip()
    if not value or len(value) > 180:
        return None
    if re.fullmatch(r"[A-Za-z0-9._:@/+ =-]+", value) is None:
        return None
    return value


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    except OSError as exc:
        raise SessionAdoptionError(
            f"unable to hash session settings file {path}: {exc}"
        ) from exc
    return h.hexdigest()


def _semantic(record: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in _SEMANTIC_KEYS if key not in record]
    if missing:
        raise SessionAdoptionError(
            "SessionAdoptionRecord is missing semantic field(s): "
            + ", ".join(missing)
        )
    state = record.get("lifecycle_state")
    if state not in _STATES:
        raise SessionAdoptionError(
            f"SessionAdoptionRecord has unknown lifecycle state: {state!r}"
        )
    adopted = record.get("adopted_session_id")
    if state in {"ADOPTED", "ENDED"} and (
        not isinstance(adopted, str) or not adopted.strip()
    ):
        raise SessionAdoptionError(
            f"{state} SessionAdoptionRecord requires an adopted session ID"
        )
    return {key: record[key] for key in _SEMANTIC_KEYS}


def _persist(state_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    semantic = _semantic(record)
    result = {
        **record,
        "session_adoption_sha256": _digest(semantic),
        "updated_at": utcnow(),
    }
    json_dump(_active_path(state_root), result)
    state = load_json(state_root / "state.json", {})
    if not isinstance(state, dict):
        raise SessionAdoptionError("durable repository state is malformed")
    state["session_adoption"] = {
        "session_adoption_sha256": result["session_adoption_sha256"],
        "lifecycle_state": result["lifecycle_state"],
        "adopted_session_id": result.get("adopted_session_id"),
        "role": result["role"],
        "working_directory": result["working_directory"],
    }
    json_dump(state_root / "state.json", state)
    return result


def load_session_adoption(
    root: Path,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    state_root = _state_root(root, state_dir)
    path = _active_path(state_root)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionAdoptionError(
            f"SessionAdoptionRecord is unreadable or malformed: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise SessionAdoptionError("SessionAdoptionRecord must be a JSON object")
    if record.get("session_adoption_sha256") != _digest(_semantic(record)):
        raise SessionAdoptionError(
            "SessionAdoptionRecord semantic integrity check failed"
        )
    if record.get("coordinator_repo_id") != repo_id(root):
        raise SessionAdoptionError(
            "SessionAdoptionRecord coordinator repository identity is stale"
        )
    return record


def _archive_current(state_root: Path, record: dict[str, Any]) -> None:
    digest = str(record.get("session_adoption_sha256") or "")
    if not digest:
        raise SessionAdoptionError(
            "cannot archive SessionAdoptionRecord without semantic digest"
        )
    json_dump(_history_dir(state_root) / f"{digest}.json", record)


def prepare_session_adoption(
    root: Path,
    selector: str,
    *,
    working_directory: Path,
    settings_path: Path,
    autonomy_profile: str,
    role: str = "product",
    state_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state_root = _state_root(root, state_dir)
    selector = str(selector).strip()
    if not selector:
        raise SessionAdoptionError("session adoption requires a non-empty selector")
    if len(selector.encode("utf-8")) > 4096:
        raise SessionAdoptionError("session adoption selector exceeds 4096 bytes")
    if role not in {"product", "planning-architect"}:
        raise SessionAdoptionError(f"unsupported session adoption role: {role!r}")

    previous = load_session_adoption(root, state_dir=state_root)
    if previous is not None:
        if previous["lifecycle_state"] in {"PREPARING", "LAUNCHED", "ADOPTED"}:
            raise SessionAdoptionError(
                "another session adoption transaction is still active"
            )
        _archive_current(state_root, previous)

    state = load_json(state_root / "state.json", {})
    if not isinstance(state, dict):
        raise SessionAdoptionError("durable repository state is malformed")
    working = working_directory.expanduser().resolve()
    settings = settings_path.expanduser().resolve()
    if not settings.is_file():
        raise SessionAdoptionError(
            f"session adoption settings file does not exist: {settings}"
        )

    workspace = load_active_task_workspace(root, state_dir=state_root)
    repair = load_active_repair(root)
    if workspace is not None and repair:
        raise SessionAdoptionError(
            "session adoption found simultaneous active task and planning-repair state"
        )

    task_workspace_sha256 = None
    execution_envelope_sha256 = None
    active_task_id = None
    repair_envelope_sha256 = None
    if role == "product":
        if repair:
            raise SessionAdoptionError(
                "product-session adoption is blocked while planning repair is active"
            )
        if workspace is not None:
            if workspace.get("lifecycle_state") != "ACTIVE":
                raise SessionAdoptionError(
                    "product-session adoption requires ACTIVE task workspace state"
                )
            expected = Path(str(workspace["task_worktree"])).expanduser().resolve()
            if working != expected:
                raise SessionAdoptionError(
                    "product-session adoption working directory is not the exact active task worktree"
                )
            task_workspace_sha256 = workspace["task_workspace_sha256"]
            execution_envelope_sha256 = workspace["execution_envelope_sha256"]
            active_task_id = workspace["task_id"]
        elif working != root:
            raise SessionAdoptionError(
                "product-session adoption without an active task must use the coordinator root"
            )
    else:
        if workspace is not None:
            raise SessionAdoptionError(
                "planning-architect adoption is blocked while a product task workspace is active"
            )
        if not repair or repair.get("schema_version") != 2:
            raise SessionAdoptionError(
                "planning-architect adoption requires active schema-2 planning repair"
            )
        if repair.get("status") not in {"ACTIVE", "RECONCILING"}:
            raise SessionAdoptionError(
                "planning-architect adoption requires ACTIVE/RECONCILING repair state"
            )
        expected = Path(str(repair.get("worktree") or "")).expanduser().resolve()
        if working != expected:
            raise SessionAdoptionError(
                "planning-architect adoption working directory is not the exact repair worktree"
            )
        repair_envelope_sha256 = repair.get("repair_envelope_sha256")

    record = {
        "schema_version": 1,
        "selector_type": "id-or-name",
        "selector_sha256": hashlib.sha256(selector.encode("utf-8")).hexdigest(),
        "selector_display": _safe_display(selector),
        "coordinator_repo_id": repo_id(root),
        "working_directory": str(working),
        "product_sha": git_head(root),
        "governance_snapshot_sha256": state.get("governance_snapshot_sha256"),
        "task_source_sha256": state.get("task_source_sha256"),
        "active_task_id": active_task_id,
        "task_workspace_sha256": task_workspace_sha256,
        "execution_envelope_sha256": execution_envelope_sha256,
        "repair_envelope_sha256": repair_envelope_sha256,
        "autonomy_profile": autonomy_profile,
        "settings_sha256": _file_sha256(settings),
        "settings_path": str(settings),
        "role": role,
        "lifecycle_state": "PREPARING",
        "adopted_session_id": None,
        "prepared_at": utcnow(),
    }
    return _persist(state_root, record)


def mark_session_adoption_launched(
    root: Path,
    *,
    event_offset: int,
    pid: int,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state_root = _state_root(root, state_dir)
    record = load_session_adoption(root, state_dir=state_root)
    if record is None or record["lifecycle_state"] != "PREPARING":
        raise SessionAdoptionError(
            "session adoption launch requires PREPARING record"
        )
    return _persist(state_root, {
        **record,
        "lifecycle_state": "LAUNCHED",
        "runtime_event_offset": int(event_offset),
        "interactive_child_pid": int(pid),
        "launched_at": utcnow(),
    })


def capture_adopted_session_id(
    root: Path,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state_root = _state_root(root, state_dir)
    record = load_session_adoption(root, state_dir=state_root)
    if record is None or record["lifecycle_state"] not in {"LAUNCHED", "ADOPTED"}:
        raise SessionAdoptionError(
            "session adoption capture requires LAUNCHED or ADOPTED record"
        )
    if record["lifecycle_state"] == "ADOPTED":
        return record
    offset = int(record.get("runtime_event_offset") or 0)
    events = read_runtime_events(state_root, offset)
    session_id = next(
        (
            str(item.get("session_id")).strip()
            for item in events
            if item.get("event") == "SessionStart"
            and str(item.get("session_id") or "").strip()
        ),
        None,
    )
    if not session_id:
        return _persist(state_root, {
            **record,
            "lifecycle_state": "BLOCKED",
            "blocker": "forked Claude launch produced no exact SessionStart session ID",
            "blocked_at": utcnow(),
        })
    display = record.get("selector_display")
    if (
        isinstance(display, str)
        and display == session_id
        and re.fullmatch(r"[0-9a-fA-F-]{20,}", display)
    ):
        return _persist(state_root, {
            **record,
            "lifecycle_state": "BLOCKED",
            "blocker": "forked Claude launch reused the source session ID",
            "blocked_at": utcnow(),
        })
    return _persist(state_root, {
        **record,
        "lifecycle_state": "ADOPTED",
        "adopted_session_id": session_id,
        "adopted_at": utcnow(),
    })


def finish_session_adoption(
    root: Path,
    *,
    return_code: int,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    state_root = _state_root(root, state_dir)
    record = load_session_adoption(root, state_dir=state_root)
    if record is None:
        return None
    if record["lifecycle_state"] == "LAUNCHED":
        record = capture_adopted_session_id(root, state_dir=state_root)
    if record["lifecycle_state"] == "BLOCKED":
        return record
    if record["lifecycle_state"] != "ADOPTED":
        raise SessionAdoptionError(
            "session adoption cannot finish from lifecycle "
            + str(record["lifecycle_state"])
        )
    return _persist(state_root, {
        **record,
        "lifecycle_state": "ENDED",
        "return_code": int(return_code),
        "ended_at": utcnow(),
    })
