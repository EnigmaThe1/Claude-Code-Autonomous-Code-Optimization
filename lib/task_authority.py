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
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from execution_envelope import (
    ExecutionEnvelopeError,
    _envelope_path,
    _violation_path,
    clear_task_violation,
    evaluate_active_workspace,
    load_active_execution_envelope,
    load_task_violation,
    persist_execution_envelope,
    prepare_execution_envelope,
    validate_staged_diff,
    workspace_matches_activation_baseline,
    task_owned_mode,
)
from git_trust import trusted_git_env
from operator_authority import require_top_level_operator
from repo_identity import SupervisorLease, repo_state_dir
from runtime_paths import utcnow
from state_store import json_dump, load_json
from task_sources import (
    TaskSourceError,
    load_resolved_task_source_set,
    resolve_task_sources,
    task_source_status,
)


class TaskAuthorityError(ValueError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=trusted_git_env(root),
    )


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    cp = _git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    if cp.returncode == 0:
        return True
    if cp.returncode == 1:
        return False
    raise TaskAuthorityError(
        f"unable to prove accepted-task ancestry: {(cp.stderr or cp.stdout or '').strip()[:800]}"
    )


def _task_index(task_set: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = task_set.get("tasks")
    if not isinstance(rows, list):
        raise TaskAuthorityError("TaskSourceSet task records are malformed")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("task"), dict):
            raise TaskAuthorityError("TaskSourceSet contains a malformed task record")
        task_id = row["task"].get("id")
        if not isinstance(task_id, str) or task_id in out:
            raise TaskAuthorityError("TaskSourceSet task identity is malformed or duplicated")
        out[task_id] = row
    return out


def _accepted_record_valid(
    root: Path,
    *,
    accepted: Any,
    task_record: dict[str, Any],
    current_base: str,
) -> tuple[bool, str]:
    if not isinstance(accepted, dict):
        return False, "no durable accepted-task record"
    expected_sha = task_record.get("task_spec_sha256")
    if accepted.get("task_spec_sha256") != expected_sha:
        return False, "accepted-task record is for a different TaskSpec digest"
    product_sha = accepted.get("accepted_product_sha")
    if not isinstance(product_sha, str) or not product_sha:
        return False, "accepted-task record has no accepted product SHA"
    probe = _git(root, "rev-parse", "--verify", f"{product_sha}^{{commit}}")
    if probe.returncode != 0:
        return False, "accepted-task product SHA is unavailable in local Git history"
    product_sha = probe.stdout.strip().lower()
    if not _is_ancestor(root, product_sha, current_base):
        return False, "accepted-task product SHA is not an ancestor of the current task base"
    return True, ""


def task_readiness(
    root: Path,
    *,
    task_set: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    state_dir: Path | None = None,
    authority_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    root = root.expanduser().resolve()
    try:
        current_set = task_set or load_resolved_task_source_set(
            root,
            state_dir=state_dir,
            authority_root=authority_root,
        )
    except TaskSourceError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    if state is None:
        state = _state(root, state_dir=state_dir)
    if not isinstance(state, dict):
        raise TaskAuthorityError("durable repository state is malformed")

    index = _task_index(current_set)
    accepted_tasks = state.get("accepted_tasks")
    if not isinstance(accepted_tasks, dict):
        accepted_tasks = {}
    base = current_set.get("product_head")
    if not isinstance(base, str):
        raise TaskAuthorityError("TaskSourceSet product base is malformed")

    result: dict[str, dict[str, Any]] = {}
    for task_id in sorted(index):
        row = index[task_id]
        task = row["task"]
        own_ok, own_reason = _accepted_record_valid(
            root,
            accepted=accepted_tasks.get(task_id),
            task_record=row,
            current_base=base,
        )
        if own_ok:
            result[task_id] = {
                "status": "ACCEPTED",
                "ready": False,
                "blockers": [],
            }
            continue

        blockers: list[dict[str, str]] = []
        for dependency in task.get("depends_on") or []:
            dep_row = index.get(dependency)
            if dep_row is None:
                blockers.append({
                    "dependency": dependency,
                    "reason": "external dependency has no local acceptance protocol in P3",
                })
                continue
            valid, reason = _accepted_record_valid(
                root,
                accepted=accepted_tasks.get(dependency),
                task_record=dep_row,
                current_base=base,
            )
            if not valid:
                blockers.append({
                    "dependency": dependency,
                    "reason": reason,
                })
        result[task_id] = {
            "status": "READY" if not blockers else "BLOCKED",
            "ready": not blockers,
            "blockers": blockers,
            **({"accepted_record_note": own_reason} if own_reason and task_id in accepted_tasks else {}),
        }
    return result


def ready_frontier(
    root: Path,
    *,
    task_set: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    state_dir: Path | None = None,
    authority_root: Path | None = None,
) -> list[str]:
    readiness = task_readiness(
        root,
        task_set=task_set,
        state=state,
        state_dir=state_dir,
        authority_root=authority_root,
    )
    return [
        task_id
        for task_id in sorted(readiness)
        if readiness[task_id]["status"] == "READY"
    ]


def _state(
    root: Path,
    *,
    state_dir: Path | None = None,
    authority_root: Path | None = None,
) -> dict[str, Any]:
    authority_check_root = (
        authority_root.expanduser().resolve()
        if authority_root is not None
        else root.expanduser().resolve()
    )
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )
    value = load_json(state_root / "state.json", {})
    if not isinstance(value, dict):
        raise TaskAuthorityError("durable repository state is malformed")
    return value


def _active_fields_present(state: dict[str, Any]) -> bool:
    return any(
        state.get(key) is not None
        for key in (
            "active_task_id",
            "active_task_spec_sha256",
            "active_execution_envelope_sha256",
        )
    )


def _ensure_no_orphan_authority(
    root: Path,
    state: dict[str, Any],
    *,
    state_dir: Path | None = None,
) -> None:
    envelope_exists = _envelope_path(root, state_dir=state_dir).exists()
    if envelope_exists and not _active_fields_present(state):
        raise TaskAuthorityError(
            "an orphaned ExecutionEnvelope exists without durable active-task binding; run tasks reconcile"
        )
    if _violation_path(root, state_dir=state_dir).exists() and not _active_fields_present(state):
        raise TaskAuthorityError(
            "an orphaned task violation record exists; run tasks reconcile"
        )


def _activate_locked(
    root: Path,
    task_id: str,
    *,
    state_dir: Path | None = None,
    authority_root: Path | None = None,
) -> dict[str, Any]:
    authority_check_root = (
        authority_root.expanduser().resolve()
        if authority_root is not None
        else root.expanduser().resolve()
    )
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )
    try:
        task_set = load_resolved_task_source_set(
            root,
            state_dir=state_root,
            authority_root=authority_check_root,
            git_state_dir=state_root,
        )
    except TaskSourceError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    state = _state(root, state_dir=state_root)
    _ensure_no_orphan_authority(root, state, state_dir=state_root)
    if _active_fields_present(state):
        raise TaskAuthorityError(
            f"another task is already active: {state.get('active_task_id')!r}"
        )

    index = _task_index(task_set)
    row = index.get(task_id)
    if row is None:
        raise TaskAuthorityError(f"TaskSpec {task_id!r} is not present in the current TaskSourceSet")

    readiness = task_readiness(
        root,
        task_set=task_set,
        state=state,
        state_dir=state_root,
        authority_root=authority_check_root,
    )
    status = readiness[task_id]
    if status["status"] == "ACCEPTED":
        raise TaskAuthorityError(f"TaskSpec {task_id!r} is already durably accepted")
    if not status["ready"]:
        detail = "; ".join(
            f"{item['dependency']}: {item['reason']}"
            for item in status["blockers"][:40]
        )
        raise TaskAuthorityError(
            f"TaskSpec {task_id!r} is not dependency-ready: {detail}"
        )

    try:
        envelope = prepare_execution_envelope(
            root,
            task_record=row,
            task_source_set=task_set,
            state=state,
            authority_root=authority_check_root,
            git_state_dir=state_root,
        )
    except ExecutionEnvelopeError as exc:
        raise TaskAuthorityError(str(exc)) from exc

    persist_execution_envelope(root, envelope, state_dir=state_root)
    state["active_task_id"] = task_id
    state["active_task_spec_sha256"] = row["task_spec_sha256"]
    state["active_execution_envelope_sha256"] = envelope["execution_envelope_sha256"]
    state["task_authority_activated_at"] = utcnow()
    state.pop("task_authority_invalidated_reason", None)
    state.pop("task_authority_invalidated_at", None)
    json_dump(state_root / "state.json", state)

    # Re-load through the full integrity/current-authority boundary before
    # reporting success. A crash between envelope and state persistence leaves a
    # fail-closed orphan rather than silently granting authority.
    try:
        verified = load_active_execution_envelope(
            root,
            state_dir=state_root,
            authority_root=authority_check_root,
        )
    except ExecutionEnvelopeError as exc:
        raise TaskAuthorityError(f"persisted task activation did not verify: {exc}") from exc

    return {
        "status": "ACTIVE",
        "task_id": task_id,
        "task_spec_sha256": verified["task_spec_sha256"],
        "execution_envelope_sha256": verified["execution_envelope_sha256"],
        "product_base_sha": verified["product_base_sha"],
        "direct_edit_paths": verified["direct_edit_paths"],
        "promotion_paths": verified["promotion_paths"],
        "runtime_scratch_paths": verified["runtime_scratch_paths"],
    }


def activate_task(
    root: Path,
    task_id: str,
    *,
    acquire_lease: bool = True,
    state_dir: Path | None = None,
    authority_root: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )
    context = (
        SupervisorLease(state_root, root)
        if acquire_lease
        else nullcontext()
    )
    with context:
        return _activate_locked(
            root,
            task_id,
            state_dir=state_root,
            authority_root=authority_root,
        )


def _deactivate_locked(root: Path) -> dict[str, Any]:
    try:
        envelope = load_active_execution_envelope(root)
    except ExecutionEnvelopeError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    try:
        violation = load_task_violation(root)
    except ExecutionEnvelopeError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    if violation is not None:
        raise TaskAuthorityError(
            "task deactivation is blocked by an unresolved envelope violation; reconcile repository state first"
        )
    if not workspace_matches_activation_baseline(root, envelope=envelope):
        raise TaskAuthorityError(
            "task deactivation requires exact activation-baseline repository state; task mutations are still present"
        )

    try:
        _envelope_path(root).unlink()
    except FileNotFoundError:
        pass
    state = _state(root)
    state["active_task_id"] = None
    state["active_task_spec_sha256"] = None
    state["active_execution_envelope_sha256"] = None
    state["task_authority_deactivated_at"] = utcnow()
    json_dump(repo_state_dir(root) / "state.json", state)
    return {
        "status": "INACTIVE",
        "task_id": envelope["task_id"],
    }


def deactivate_task(root: Path, *, acquire_lease: bool = True) -> dict[str, Any]:
    root = root.expanduser().resolve()
    context = (
        SupervisorLease(repo_state_dir(root), root)
        if acquire_lease
        else nullcontext()
    )
    with context:
        return _deactivate_locked(root)


def _reconcile_locked(root: Path) -> dict[str, Any]:
    state = _state(root)
    active = _active_fields_present(state)
    if not active:
        envelope_path = _envelope_path(root)
        violation_path = _violation_path(root)
        if not envelope_path.exists() and not violation_path.exists():
            return {"status": "CLEAN", "active_task_id": None}
        if envelope_path.exists():
            try:
                raw = json.loads(envelope_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise TaskAuthorityError(
                    f"orphaned ExecutionEnvelope is malformed and cannot be reconciled automatically: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise TaskAuthorityError("orphaned ExecutionEnvelope is malformed")
            baseline = raw.get("baseline_wip")
            if not isinstance(baseline, dict):
                raise TaskAuthorityError("orphaned ExecutionEnvelope has no valid workspace baseline")
            # Reuse the exact persisted envelope as an audit boundary only after
            # proving the checkout is back at its recorded baseline.
            try:
                product_base = raw["product_base_sha"]
            except KeyError as exc:
                raise TaskAuthorityError("orphaned ExecutionEnvelope is incomplete") from exc
            if _git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip().lower() != product_base:
                raise TaskAuthorityError("orphaned ExecutionEnvelope base no longer matches HEAD")
            from execution_envelope import capture_workspace_baseline
            current = capture_workspace_baseline(root)
            if (
                current["staged_paths"] != baseline.get("staged_paths")
                or current["unstaged"] != baseline.get("unstaged")
                or current["untracked"] != baseline.get("untracked")
            ):
                raise TaskAuthorityError(
                    "orphaned task authority cannot be cleared while repository state differs from its activation baseline"
                )
        try:
            envelope_path.unlink()
        except FileNotFoundError:
            pass
        try:
            violation_path.unlink()
        except FileNotFoundError:
            pass
        return {"status": "RECONCILED", "active_task_id": None}

    try:
        envelope = load_active_execution_envelope(root)
        result = evaluate_active_workspace(root, envelope=envelope)
    except ExecutionEnvelopeError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    if result["status"] != "VALID":
        raise TaskAuthorityError(
            "active task still violates its ExecutionEnvelope: "
            + "; ".join(
                f"{row['path']}: {row['reason']}"
                for row in result["violations"][:40]
            )
        )
    clear_task_violation(root)
    return {
        "status": "RECONCILED",
        "active_task_id": envelope["task_id"],
        "execution_envelope_sha256": envelope["execution_envelope_sha256"],
    }


def reconcile_task_authority(
    root: Path,
    *,
    acquire_lease: bool = True,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    context = (
        SupervisorLease(repo_state_dir(root), root)
        if acquire_lease
        else nullcontext()
    )
    with context:
        return _reconcile_locked(root)


def active_task_prompt_context(
    root: Path,
    *,
    state_dir: Path | None = None,
    authority_root: Path | None = None,
) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    state = _state(root, state_dir=state_dir)
    if not _active_fields_present(state):
        return None
    try:
        envelope = load_active_execution_envelope(
            root,
            state_dir=state_dir,
            authority_root=authority_root,
        )
        task_set = load_resolved_task_source_set(
            root,
            state_dir=state_dir,
            authority_root=authority_root,
        )
    except (ExecutionEnvelopeError, TaskSourceError) as exc:
        raise TaskAuthorityError(str(exc)) from exc
    row = _task_index(task_set).get(envelope["task_id"])
    if row is None:
        raise TaskAuthorityError(
            "active ExecutionEnvelope task is absent from the current TaskSourceSet"
        )
    task = row["task"]
    return {
        "id": task["id"],
        "depends_on": list(task.get("depends_on") or []),
        "authority_sets": list(task.get("authority_sets") or []),
        "owned_paths": list(task.get("owned_paths") or []),
        "evidence_paths": list(task.get("evidence_paths") or []),
        "runtime_scratch_paths": list(task.get("runtime_scratch_paths") or []),
        "verification": list(task.get("verification") or []),
        "task_spec_sha256": envelope["task_spec_sha256"],
        "execution_envelope_sha256": envelope["execution_envelope_sha256"],
        "product_base_sha": envelope["product_base_sha"],
    }


def ensure_supervisor_task_activation(root: Path) -> dict[str, Any]:
    """Resolve/reuse one deterministic active task while caller holds the repo lease.

    This is the P3 supervisor integration seam. It deliberately does not acquire
    another SupervisorLease: do_run/do_start already own the single-writer lease.
    """
    root = root.expanduser().resolve()
    try:
        governed = task_owned_mode(root)
    except ExecutionEnvelopeError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    if not governed:
        return {"status": "UNCONFIGURED", "repository": str(root)}

    state = _state(root)
    _ensure_no_orphan_authority(root, state)
    if _active_fields_present(state):
        try:
            envelope = load_active_execution_envelope(root)
            violation = load_task_violation(root)
            evaluated = evaluate_active_workspace(root, envelope=envelope)
        except ExecutionEnvelopeError as exc:
            raise TaskAuthorityError(str(exc)) from exc
        if violation is not None:
            raise TaskAuthorityError(
                "active task has an unresolved ExecutionEnvelope violation"
            )
        if evaluated["status"] != "VALID":
            detail = "; ".join(
                f"{row['path']}: {row['reason']}"
                for row in evaluated["violations"][:40]
            )
            raise TaskAuthorityError(
                "active task repository state violates its ExecutionEnvelope: "
                + detail
            )
        return {
            "status": "ACTIVE",
            "reused": True,
            "task_id": envelope["task_id"],
            "task_spec_sha256": envelope["task_spec_sha256"],
            "execution_envelope_sha256": envelope[
                "execution_envelope_sha256"
            ],
            "product_base_sha": envelope["product_base_sha"],
        }

    try:
        resolved = resolve_task_sources(root, persist=True)
    except TaskSourceError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    if resolved.get("status") != "READY":
        raise TaskAuthorityError(
            "repository TaskSources did not resolve to a READY TaskSourceSet"
        )

    state = _state(root)
    frontier = ready_frontier(root, task_set=resolved, state=state)
    if not frontier:
        readiness = task_readiness(root, task_set=resolved, state=state)
        blockers: list[str] = []
        for task_id in sorted(readiness):
            row = readiness[task_id]
            if row.get("status") == "ACCEPTED":
                continue
            for blocker in row.get("blockers") or []:
                blockers.append(
                    f"{task_id} <- {blocker.get('dependency')}: "
                    f"{blocker.get('reason')}"
                )
        if not blockers and readiness:
            raise TaskAuthorityError(
                "all current TaskSpecs are already durably accepted; "
                "P3 has no READY product task to activate"
            )
        raise TaskAuthorityError(
            "no dependency-safe READY TaskSpec is available"
            + (": " + "; ".join(blockers[:40]) if blockers else "")
        )

    selected = frontier[0]
    activated = _activate_locked(root, selected)
    activated["reused"] = False
    activated["ready_frontier"] = frontier
    return activated


def task_show(root: Path, task_id: str) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        task_set = load_resolved_task_source_set(
            root,
            require_current=False,
            require_state_binding=False,
        )
    except TaskSourceError as exc:
        raise TaskAuthorityError(str(exc)) from exc
    index = _task_index(task_set)
    row = index.get(task_id)
    if row is None:
        raise TaskAuthorityError(f"TaskSpec {task_id!r} is not present in the persisted TaskSourceSet")
    source_status = task_source_status(root)
    readiness: dict[str, Any] | None = None
    if source_status.get("status") == "READY":
        readiness = task_readiness(root, task_set=task_set).get(task_id)
    state = _state(root)
    return {
        "task": row["task"],
        "task_spec_sha256": row["task_spec_sha256"],
        "record_sha256": row.get("record_sha256"),
        "source_id": row.get("source_id"),
        "task_source_status": source_status.get("status"),
        "readiness": readiness,
        "active": state.get("active_task_id") == task_id,
    }


def task_explain(root: Path, task_id: str) -> dict[str, Any]:
    shown = task_show(root, task_id)
    task = shown["task"]
    explanation = {
        **shown,
        "authority": {
            "authority_sets": task.get("authority_sets") or [],
            "direct_edit_paths": sorted(set(
                list(task.get("owned_paths") or [])
                + list(task.get("evidence_paths") or [])
            )),
            "promotion_paths": sorted(set(
                list(task.get("owned_paths") or [])
                + list(task.get("evidence_paths") or [])
            )),
            "runtime_scratch_paths": task.get("runtime_scratch_paths") or [],
            "verification": task.get("verification") or [],
        },
    }
    return explanation


def task_status(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    source = task_source_status(root)
    state = _state(root)
    result: dict[str, Any] = {
        **source,
        "active_task_id": state.get("active_task_id"),
        "active_task_spec_sha256": state.get("active_task_spec_sha256"),
        "active_execution_envelope_sha256": state.get("active_execution_envelope_sha256"),
    }
    if source.get("status") == "READY":
        try:
            result["ready_frontier"] = ready_frontier(root)
        except TaskAuthorityError as exc:
            result["readiness_error"] = str(exc)
    if state.get("active_execution_envelope_sha256"):
        try:
            envelope = load_active_execution_envelope(root)
            result["active_envelope"] = {
                key: envelope[key]
                for key in (
                    "task_id",
                    "execution_envelope_sha256",
                    "product_base_sha",
                    "direct_edit_paths",
                    "promotion_paths",
                    "runtime_scratch_paths",
                )
            }
            violation = load_task_violation(root)
            result["violation"] = violation
        except ExecutionEnvelopeError as exc:
            result["active_envelope_error"] = str(exc)
    return result


def task_action(args: Any, *, find_repo_root) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    action = getattr(args, "tasks_command", None)
    try:
        if action == "status":
            result = task_status(root)
        elif action == "resolve":
            result = resolve_task_sources(root)
            if result.get("status") == "READY":
                external = result.get("external_dependencies", [])
                result = {
                    "status": "READY",
                    "repository": str(root),
                    "product_head": result.get("product_head"),
                    "authority_snapshot_sha256": result.get("authority_snapshot_sha256"),
                    "task_source_set_sha256": result.get("task_source_set_sha256"),
                    "merged_tasks_sha256": result.get("merged_tasks_sha256"),
                    "source_count": len(result.get("sources", [])),
                    "task_count": len(result.get("tasks", [])),
                    "external_dependency_count": len(external) if isinstance(external, list) else 0,
                }
        elif action == "show":
            result = task_show(root, args.task_id)
        elif action == "explain":
            result = task_explain(root, args.task_id)
        elif action == "validate-stage":
            result = validate_staged_diff(root)
        elif action == "activate":
            require_top_level_operator(root, "Task activation")
            result = activate_task(root, args.task_id)
        elif action == "deactivate":
            require_top_level_operator(root, "Task deactivation")
            result = deactivate_task(root)
        elif action == "reconcile":
            require_top_level_operator(root, "Task authority reconciliation")
            result = reconcile_task_authority(root)
        else:
            raise TaskAuthorityError(f"unsupported tasks command: {action}")
    except (
        ExecutionEnvelopeError,
        TaskAuthorityError,
        TaskSourceError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "repository": str(root),
            "error": str(exc),
        }, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "BLOCKED" else 0
