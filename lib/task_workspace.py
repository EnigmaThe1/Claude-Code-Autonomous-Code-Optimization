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
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from execution_envelope import (
    ExecutionEnvelopeError,
    capture_workspace_baseline,
    git_branch,
    git_head,
    load_active_execution_envelope,
)
from git_trust import trusted_git_env
from governance_contract import canonical_json_bytes
from repo_identity import SupervisorLease, repo_id, repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json
from task_authority import (
    TaskAuthorityError,
    activate_task,
    ready_frontier,
    task_readiness,
)
from task_sources import TaskSourceError, resolve_task_sources


class TaskWorkspaceError(ValueError):
    pass


_SEMANTIC_KEYS = (
    "schema_version",
    "task_id",
    "task_source_set_sha256",
    "task_spec_sha256",
    "authority_snapshot_sha256",
    "execution_envelope_sha256",
    "coordinator_repo_id",
    "product_base_sha",
    "product_branch",
    "task_branch",
    "task_worktree",
    "primary_baseline_sha256",
    "git_ref_binding_sha256",
    "candidate_sha",
    "verified_candidate_sha",
    "acceptance_attestation_sha256",
    "lifecycle_state",
)

_ALLOWED_STATES = {
    "PREPARING",
    "ACTIVE",
    "CANDIDATE",
    "VERIFYING",
    "VERIFIED_PENDING_PROMOTION",
    "PROMOTING",
    "ACCEPTED_PENDING_CLEANUP",
    "PRIMARY_DRIFT",
    "STALE_BASE",
    "BLOCKED",
    "ABANDONED_PRESERVED",
}


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git(
    root: Path,
    *args: str,
    state_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=trusted_git_env(root, state_dir=state_dir),
    )


def _state_root(root: Path, state_dir: Path | None = None) -> Path:
    return (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )


def _tasks_dir(state_root: Path) -> Path:
    return ensure_private_dir(state_root / "tasks")


def _active_path(state_root: Path) -> Path:
    return state_root / "tasks" / "workspace-active.json"


def _workspace_parent(state_root: Path) -> Path:
    return ensure_private_dir(_tasks_dir(state_root) / "workspaces")


def _semantic(record: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in _SEMANTIC_KEYS if key not in record]
    if missing:
        raise TaskWorkspaceError(
            "TaskWorkspaceRecord is missing semantic field(s): "
            + ", ".join(missing)
        )
    out = {key: record[key] for key in _SEMANTIC_KEYS}
    state = out["lifecycle_state"]
    if state not in _ALLOWED_STATES:
        raise TaskWorkspaceError(
            f"TaskWorkspaceRecord has unknown lifecycle state: {state!r}"
        )
    envelope = out["execution_envelope_sha256"]
    refs_digest = out["git_ref_binding_sha256"]
    if state == "PREPARING":
        if envelope is not None and (
            not isinstance(envelope, str) or len(envelope) != 64
        ):
            raise TaskWorkspaceError(
                "PREPARING TaskWorkspaceRecord envelope digest is invalid"
            )
        if refs_digest is not None and (
            not isinstance(refs_digest, str) or len(refs_digest) != 64
        ):
            raise TaskWorkspaceError(
                "PREPARING TaskWorkspaceRecord ref binding digest is invalid"
            )
    else:
        if not isinstance(envelope, str) or len(envelope) != 64:
            raise TaskWorkspaceError(
                "TaskWorkspaceRecord requires an ExecutionEnvelope digest"
            )
        if not isinstance(refs_digest, str) or len(refs_digest) != 64:
            raise TaskWorkspaceError(
                "TaskWorkspaceRecord requires a Git ref binding digest"
            )
    return out


def _persist(state_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    semantic = _semantic(record)
    result = {
        **record,
        "task_workspace_sha256": _digest(semantic),
        "updated_at": utcnow(),
    }
    json_dump(_active_path(state_root), result)
    return result


def capture_git_ref_binding(
    root: Path,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    cp = _git(
        root,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
        state_dir=state_dir,
    )
    if cp.returncode != 0:
        detail = cp.stderr or cp.stdout or "git for-each-ref failed"
        raise TaskWorkspaceError(detail.strip()[:1600])

    rows: list[dict[str, str]] = []
    for raw in cp.stdout.splitlines():
        if not raw:
            continue
        try:
            name, object_id = raw.split("\t", 1)
        except ValueError as exc:
            raise TaskWorkspaceError(
                "unexpected Git ref record while binding task workspace"
            ) from exc
        digest = object_id.strip().lower()
        if not name.startswith("refs/") or not (
            40 <= len(digest) <= 64
            and all(ch in "0123456789abcdef" for ch in digest)
        ):
            raise TaskWorkspaceError(
                "unexpected Git ref identity while binding task workspace"
            )
        rows.append({"name": name, "object": digest})

    rows.sort(key=lambda row: row["name"].encode("utf-8"))
    return {"schema_version": 1, "refs": rows}


def _ref_binding_digest(binding: dict[str, Any]) -> str:
    refs = binding.get("refs")
    if (
        binding.get("schema_version") != 1
        or not isinstance(refs, list)
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("object"), str)
            for row in refs
        )
    ):
        raise TaskWorkspaceError(
            "TaskWorkspaceRecord Git ref binding is malformed"
        )
    return _digest(binding)


def _workspace_path_is_owned(state_root: Path, worktree: Path) -> bool:
    parent = _workspace_parent(state_root).resolve()
    target = worktree.expanduser().resolve()
    try:
        target.relative_to(parent)
    except ValueError:
        return False
    return target != parent


def load_active_task_workspace(
    coordinator_root: Path,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = _state_root(coordinator_root, state_dir)
    path = _active_path(state_root)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskWorkspaceError(
            f"TaskWorkspaceRecord is unreadable or malformed: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise TaskWorkspaceError("TaskWorkspaceRecord must be a JSON object")
    semantic = _semantic(record)
    if record.get("task_workspace_sha256") != _digest(semantic):
        raise TaskWorkspaceError(
            "TaskWorkspaceRecord semantic integrity check failed"
        )
    baseline = record.get("primary_baseline")
    if not isinstance(baseline, dict):
        raise TaskWorkspaceError(
            "TaskWorkspaceRecord primary baseline evidence is missing"
        )
    if _digest(baseline) != record.get("primary_baseline_sha256"):
        raise TaskWorkspaceError(
            "TaskWorkspaceRecord primary baseline binding is invalid"
        )

    refs_digest = record.get("git_ref_binding_sha256")
    if refs_digest is not None:
        binding = record.get("git_ref_binding")
        if not isinstance(binding, dict):
            raise TaskWorkspaceError(
                "TaskWorkspaceRecord Git ref binding evidence is missing"
            )
        if _ref_binding_digest(binding) != refs_digest:
            raise TaskWorkspaceError(
                "TaskWorkspaceRecord Git ref binding integrity check failed"
            )
    worktree = Path(str(record.get("task_worktree") or ""))
    if not _workspace_path_is_owned(state_root, worktree):
        raise TaskWorkspaceError(
            "TaskWorkspaceRecord worktree is outside package-owned task state"
        )
    if record.get("coordinator_repo_id") != repo_id(coordinator_root):
        raise TaskWorkspaceError(
            "TaskWorkspaceRecord coordinator repository identity is stale"
        )
    return record


def _branch_exists(
    root: Path,
    branch: str,
    *,
    state_dir: Path | None = None,
) -> bool:
    return (
        _git(
            root,
            "show-ref",
            "--verify",
            f"refs/heads/{branch}",
            state_dir=state_dir,
        ).returncode
        == 0
    )


def _ensure_exact_worktree(
    coordinator_root: Path,
    record: dict[str, Any],
    *,
    state_root: Path,
) -> Path:
    branch = str(record["task_branch"])
    base = str(record["product_base_sha"]).lower()
    worktree = Path(str(record["task_worktree"])).expanduser().resolve()
    branch_exists = _branch_exists(
        coordinator_root,
        branch,
        state_dir=state_root,
    )

    if worktree.exists():
        if not branch_exists:
            raise TaskWorkspaceError(
                "task worktree exists but its recorded task branch is missing"
            )
    elif branch_exists:
        ensure_private_dir(worktree.parent)
        cp = _git(
            coordinator_root,
            "worktree",
            "add",
            str(worktree),
            branch,
            state_dir=state_root,
        )
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout or "git worktree recovery failed")
            raise TaskWorkspaceError(detail.strip()[:1600])
    else:
        ensure_private_dir(worktree.parent)
        cp = _git(
            coordinator_root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            base,
            state_dir=state_root,
        )
        if cp.returncode != 0:
            detail = (cp.stderr or cp.stdout or "git worktree add failed")
            raise TaskWorkspaceError(detail.strip()[:1600])

    head = git_head(worktree, state_dir=state_root)
    current_branch = git_branch(worktree, state_dir=state_root)
    if head.lower() != base:
        raise TaskWorkspaceError(
            "task worktree HEAD does not match its recorded product base"
        )
    if current_branch != branch:
        raise TaskWorkspaceError(
            "task worktree is not on its exact recorded package branch"
        )
    status = _git(
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        state_dir=state_root,
    )
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "git status failed")
        raise TaskWorkspaceError(detail.strip()[:1600])
    if status.stdout:
        raise TaskWorkspaceError(
            "task worktree must be clean before ExecutionEnvelope activation"
        )
    return worktree


def _primary_matches_record(
    coordinator_root: Path,
    record: dict[str, Any],
    *,
    state_root: Path,
) -> bool:
    if git_head(
        coordinator_root,
        state_dir=state_root,
    ).lower() != str(record["product_base_sha"]).lower():
        return False
    if git_branch(
        coordinator_root,
        state_dir=state_root,
    ) != record["product_branch"]:
        return False
    return (
        _digest(
            capture_workspace_baseline(
                coordinator_root,
                state_dir=state_root,
            )
        )
        == record["primary_baseline_sha256"]
    )


def _state_has_active_task(state: dict[str, Any]) -> bool:
    return any(
        state.get(key) is not None
        for key in (
            "active_task_id",
            "active_task_spec_sha256",
            "active_execution_envelope_sha256",
        )
    )


def _bind_workspace_state(
    state_root: Path,
    record: dict[str, Any],
) -> None:
    state = load_json(state_root / "state.json", {})
    if not isinstance(state, dict):
        raise TaskWorkspaceError("durable coordinator state is malformed")
    state["active_task_workspace_sha256"] = record[
        "task_workspace_sha256"
    ]
    state["active_task_worktree"] = record["task_worktree"]
    state["active_task_branch"] = record["task_branch"]
    state["task_workspace_lifecycle_state"] = record["lifecycle_state"]
    json_dump(state_root / "state.json", state)


def evaluate_task_workspace_boundary(
    coordinator_root: Path,
    *,
    task_root: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = _state_root(coordinator_root, state_dir)
    record = load_active_task_workspace(
        coordinator_root,
        state_dir=state_root,
    )
    if record is None:
        return {"status": "UNCONFIGURED", "violations": []}

    violations: list[dict[str, str]] = []
    if not _primary_matches_record(
        coordinator_root,
        record,
        state_root=state_root,
    ):
        violations.append({
            "path": "PRIMARY",
            "reason": (
                "coordinator primary checkout changed since task workspace "
                "creation"
            ),
        })

    expected_refs = record.get("git_ref_binding_sha256")
    if isinstance(expected_refs, str):
        try:
            current_binding = capture_git_ref_binding(
                coordinator_root,
                state_dir=state_root,
            )
            current_digest = _ref_binding_digest(current_binding)
        except TaskWorkspaceError as exc:
            violations.append({
                "path": "GIT_REFS",
                "reason": f"unable to verify coordinator Git refs: {exc}",
            })
        else:
            if current_digest != expected_refs:
                violations.append({
                    "path": "GIT_REFS",
                    "reason": (
                        "coordinator Git refs changed outside package-owned "
                        "task operations"
                    ),
                })

    recorded_task = Path(str(record["task_worktree"])).expanduser().resolve()
    effective_task = (
        task_root.expanduser().resolve()
        if task_root is not None
        else recorded_task
    )
    if effective_task != recorded_task:
        violations.append({
            "path": "TASK_ROOT",
            "reason": (
                "worker task root does not match the durable "
                "TaskWorkspaceRecord"
            ),
        })
    elif not effective_task.exists():
        violations.append({
            "path": "TASK_ROOT",
            "reason": "recorded task worktree is missing",
        })
    else:
        try:
            if git_head(
                effective_task,
                state_dir=state_root,
            ).lower() != str(record["product_base_sha"]).lower():
                violations.append({
                    "path": "TASK_HEAD",
                    "reason": (
                        "task worktree HEAD moved away from the recorded "
                        "product base"
                    ),
                })
            if git_branch(
                effective_task,
                state_dir=state_root,
            ) != record["task_branch"]:
                violations.append({
                    "path": "TASK_BRANCH",
                    "reason": (
                        "task worktree branch differs from the recorded "
                        "package task branch"
                    ),
                })
        except ExecutionEnvelopeError as exc:
            violations.append({
                "path": "TASK_HEAD",
                "reason": (
                    "unable to verify task worktree Git identity: "
                    + str(exc)
                ),
            })

    return {
        "status": "VALID" if not violations else "VIOLATION",
        "task_id": record["task_id"],
        "task_workspace_sha256": record["task_workspace_sha256"],
        "violations": violations,
    }


def mark_task_workspace_guard_failure(
    coordinator_root: Path,
    *,
    violations: list[dict[str, str]],
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = _state_root(coordinator_root, state_dir)
    record = load_active_task_workspace(
        coordinator_root,
        state_dir=state_root,
    )
    if record is None:
        raise TaskWorkspaceError("no active TaskWorkspaceRecord exists")

    next_state = (
        "PRIMARY_DRIFT"
        if any(row.get("path") == "PRIMARY" for row in violations)
        else "BLOCKED"
    )
    updated = _persist(
        state_root,
        {
            **record,
            "lifecycle_state": next_state,
            "guard_violations": violations[:100],
            "guard_failed_at": utcnow(),
        },
    )
    _bind_workspace_state(state_root, updated)
    return updated


def _resume_workspace(
    coordinator_root: Path,
    record: dict[str, Any],
    *,
    state_root: Path,
) -> dict[str, Any]:
    if not _primary_matches_record(
        coordinator_root,
        record,
        state_root=state_root,
    ):
        raise TaskWorkspaceError(
            "coordinator primary checkout changed since task workspace creation"
        )

    worktree = _ensure_exact_worktree(
        coordinator_root,
        record,
        state_root=state_root,
    )
    state = load_json(state_root / "state.json", {})
    if not isinstance(state, dict):
        raise TaskWorkspaceError("durable coordinator state is malformed")

    if record["lifecycle_state"] == "PREPARING":
        envelope: dict[str, Any]
        if _state_has_active_task(state):
            try:
                envelope = load_active_execution_envelope(
                    worktree,
                    state_dir=state_root,
                    authority_root=coordinator_root,
                    git_state_dir=state_root,
                )
            except ExecutionEnvelopeError as exc:
                raise TaskWorkspaceError(
                    "PREPARING task workspace has inconsistent active "
                    f"ExecutionEnvelope state: {exc}"
                ) from exc
            if envelope["task_id"] != record["task_id"]:
                raise TaskWorkspaceError(
                    "PREPARING task workspace conflicts with another active task"
                )
        else:
            try:
                activated = activate_task(
                    worktree,
                    str(record["task_id"]),
                    acquire_lease=False,
                    state_dir=state_root,
                    authority_root=coordinator_root,
                    git_state_dir=state_root,
                )
            except TaskAuthorityError as exc:
                raise TaskWorkspaceError(str(exc)) from exc
            envelope = load_active_execution_envelope(
                worktree,
                state_dir=state_root,
                authority_root=coordinator_root,
                git_state_dir=state_root,
            )
            if (
                activated["execution_envelope_sha256"]
                != envelope["execution_envelope_sha256"]
            ):
                raise TaskWorkspaceError(
                    "activated task envelope changed during workspace activation"
                )

        ref_binding = capture_git_ref_binding(
            coordinator_root,
            state_dir=state_root,
        )
        record = _persist(
            state_root,
            {
                **record,
                "execution_envelope_sha256": envelope[
                    "execution_envelope_sha256"
                ],
                "git_ref_binding": ref_binding,
                "git_ref_binding_sha256": _ref_binding_digest(ref_binding),
                "lifecycle_state": "ACTIVE",
                "activated_at": utcnow(),
            },
        )
        _bind_workspace_state(state_root, record)
        return record

    if record["lifecycle_state"] != "ACTIVE":
        raise TaskWorkspaceError(
            "task workspace begin/reuse currently requires PREPARING or ACTIVE "
            f"state, found {record['lifecycle_state']!r}"
        )

    try:
        envelope = load_active_execution_envelope(
            worktree,
            state_dir=state_root,
            authority_root=coordinator_root,
            git_state_dir=state_root,
        )
    except ExecutionEnvelopeError as exc:
        raise TaskWorkspaceError(str(exc)) from exc
    if (
        envelope["task_id"] != record["task_id"]
        or envelope["execution_envelope_sha256"]
        != record["execution_envelope_sha256"]
    ):
        raise TaskWorkspaceError(
            "active task workspace and ExecutionEnvelope identities disagree"
        )
    _bind_workspace_state(state_root, record)
    return record


def _task_record_index(task_set: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = task_set.get("tasks")
    if not isinstance(rows, list):
        raise TaskWorkspaceError("TaskSourceSet task records are malformed")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("task"), dict)
            or not isinstance(row["task"].get("id"), str)
        ):
            raise TaskWorkspaceError(
                "TaskSourceSet contains a malformed task record"
            )
        out[row["task"]["id"]] = row
    return out


def _begin_locked(
    coordinator_root: Path,
    *,
    task_id: str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = _state_root(coordinator_root, state_dir)

    existing = load_active_task_workspace(
        coordinator_root,
        state_dir=state_root,
    )
    if existing is not None:
        if task_id is not None and task_id != existing["task_id"]:
            raise TaskWorkspaceError(
                "another task workspace is already active: "
                f"{existing['task_id']!r}"
            )
        return _resume_workspace(
            coordinator_root,
            existing,
            state_root=state_root,
        )

    state = load_json(state_root / "state.json", {})
    if not isinstance(state, dict):
        raise TaskWorkspaceError("durable coordinator state is malformed")
    if _state_has_active_task(state):
        raise TaskWorkspaceError(
            "legacy/P3 active task authority exists without a P4 workspace; "
            "reconcile or deactivate it before beginning a task workspace"
        )

    try:
        task_set = resolve_task_sources(coordinator_root, persist=True)
    except TaskSourceError as exc:
        raise TaskWorkspaceError(str(exc)) from exc
    if task_set.get("status") != "READY":
        raise TaskWorkspaceError(
            "repository TaskSources did not resolve to a READY TaskSourceSet"
        )

    state = load_json(state_root / "state.json", {})
    if not isinstance(state, dict):
        raise TaskWorkspaceError("durable coordinator state is malformed")
    frontier = ready_frontier(
        coordinator_root,
        task_set=task_set,
        state=state,
        state_dir=state_root,
        authority_root=coordinator_root,
    )
    if task_id is None:
        if not frontier:
            readiness = task_readiness(
                coordinator_root,
                task_set=task_set,
                state=state,
                state_dir=state_root,
                authority_root=coordinator_root,
            )
            unresolved = [
                key
                for key, value in sorted(readiness.items())
                if value.get("status") != "ACCEPTED"
            ]
            if not unresolved:
                raise TaskWorkspaceError(
                    "all current TaskSpecs are already durably accepted"
                )
            raise TaskWorkspaceError(
                "no dependency-safe READY TaskSpec is available"
            )
        selected = frontier[0]
    else:
        if task_id not in frontier:
            raise TaskWorkspaceError(
                f"TaskSpec {task_id!r} is not in the dependency-safe READY frontier"
            )
        selected = task_id

    index = _task_record_index(task_set)
    task_record = index[selected]
    product_base = str(task_set.get("product_head") or "").lower()
    if (
        not product_base
        or git_head(
            coordinator_root,
            state_dir=state_root,
        ).lower() != product_base
    ):
        raise TaskWorkspaceError(
            "coordinator HEAD changed since TaskSourceSet resolution"
        )
    product_branch = git_branch(
        coordinator_root,
        state_dir=state_root,
    )
    if not product_branch:
        raise TaskWorkspaceError(
            "P4 task workspace requires a named coordinator product branch"
        )

    primary_baseline = capture_workspace_baseline(
        coordinator_root,
        state_dir=state_root,
    )
    token = hashlib.sha256(
        (
            f"{repo_id(coordinator_root)}:{selected}:"
            f"{task_record['task_spec_sha256']}:{product_base}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    task_branch = f"claude-auto/task/{token}"
    worktree = (
        _workspace_parent(state_root)
        / token
        / "worktree"
    ).resolve()

    if _branch_exists(
        coordinator_root,
        task_branch,
        state_dir=state_root,
    ):
        raise TaskWorkspaceError(
            "stale package task branch exists without a durable workspace record: "
            + task_branch
        )
    if worktree.exists():
        raise TaskWorkspaceError(
            "stale package task worktree exists without a durable workspace record: "
            + str(worktree)
        )

    preparing = _persist(
        state_root,
        {
            "schema_version": 1,
            "task_id": selected,
            "task_source_set_sha256": task_set["task_source_set_sha256"],
            "task_spec_sha256": task_record["task_spec_sha256"],
            "authority_snapshot_sha256": task_set[
                "authority_snapshot_sha256"
            ],
            "execution_envelope_sha256": None,
            "coordinator_repo_id": repo_id(coordinator_root),
            "product_base_sha": product_base,
            "product_branch": product_branch,
            "task_branch": task_branch,
            "task_worktree": str(worktree),
            "primary_baseline_sha256": _digest(primary_baseline),
            "git_ref_binding_sha256": None,
            "candidate_sha": None,
            "verified_candidate_sha": None,
            "acceptance_attestation_sha256": None,
            "lifecycle_state": "PREPARING",
            "primary_baseline": primary_baseline,
            "created_at": utcnow(),
        },
    )
    _bind_workspace_state(state_root, preparing)
    return _resume_workspace(
        coordinator_root,
        preparing,
        state_root=state_root,
    )


def begin_task_workspace(
    coordinator_root: Path,
    *,
    task_id: str | None = None,
    acquire_lease: bool = True,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = _state_root(coordinator_root, state_dir)
    context = (
        SupervisorLease(state_root, coordinator_root)
        if acquire_lease
        else nullcontext()
    )
    with context:
        return _begin_locked(
            coordinator_root,
            task_id=task_id,
            state_dir=state_root,
        )


def task_workspace_status(
    coordinator_root: Path,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = _state_root(coordinator_root, state_dir)
    record = load_active_task_workspace(
        coordinator_root,
        state_dir=state_root,
    )
    return {
        "repository": str(coordinator_root),
        "state_dir": str(state_root),
        "active": record,
    }
