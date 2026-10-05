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
import subprocess
import unicodedata
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from execution_envelope import (
    ExecutionEnvelopeError,
    evaluate_active_workspace,
    load_active_execution_envelope,
    path_matches_any,
    validate_promotion_target_for_active_envelope,
    validate_staged_diff,
)
from git_trust import trusted_git_env
from governance_contract import canonical_json_bytes
from repo_identity import SupervisorLease, repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump
from task_sources import TaskSourceError, load_resolved_task_source_set
from task_workspace import (
    TaskWorkspaceError,
    candidate_ref_for_workspace,
    capture_git_ref_binding,
    evaluate_task_workspace_boundary,
    load_active_task_workspace,
    update_task_workspace_package_state,
)


class TaskAcceptanceError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git(
    root: Path,
    *args: str,
    state_dir: Path,
    input_data: str | bytes | None = None,
    text: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    env = trusted_git_env(root, state_dir=state_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_data,
        text=text,
        capture_output=True,
        env=env,
    )


def _require_git(
    root: Path,
    *args: str,
    state_dir: Path,
    input_data: str | bytes | None = None,
    text: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    cp = _git(
        root,
        *args,
        state_dir=state_dir,
        input_data=input_data,
        text=text,
        extra_env=extra_env,
    )
    if cp.returncode != 0:
        if text:
            detail = str(cp.stderr or cp.stdout or "Git command failed")
        else:
            detail = bytes(cp.stderr or cp.stdout or b"Git command failed").decode(
                "utf-8",
                errors="replace",
            )
        raise TaskAcceptanceError(
            "Git candidate operation failed: "
            + " ".join(args)
            + ": "
            + detail.strip()[:1600]
        )
    return cp


def _decode_nul_paths(payload: bytes) -> list[str]:
    out: list[str] = []
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TaskAcceptanceError(
                "candidate assembly requires UTF-8 repository paths"
            ) from exc
        out.append(unicodedata.normalize("NFC", value))
    return sorted(set(out), key=lambda item: item.encode("utf-8"))


def _changed_paths(
    task_root: Path,
    *,
    state_dir: Path,
) -> list[str]:
    tracked = _require_git(
        task_root,
        "diff",
        "--name-only",
        "-z",
        "--no-ext-diff",
        "HEAD",
        "--",
        state_dir=state_dir,
        text=False,
    )
    untracked = _require_git(
        task_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        state_dir=state_dir,
        text=False,
    )
    return sorted(
        set(_decode_nul_paths(bytes(tracked.stdout)))
        | set(_decode_nul_paths(bytes(untracked.stdout))),
        key=lambda item: item.encode("utf-8"),
    )


def _reset_index(task_root: Path, *, state_dir: Path) -> None:
    # read-tree changes only the index. Worker worktree content remains intact.
    _require_git(
        task_root,
        "read-tree",
        "HEAD",
        state_dir=state_dir,
    )


def _stage_exact_paths(
    task_root: Path,
    paths: list[str],
    *,
    state_dir: Path,
) -> None:
    if not paths:
        return
    payload = b"\0".join(path.encode("utf-8") for path in paths) + b"\0"
    _require_git(
        task_root,
        "--literal-pathspecs",
        "add",
        "-A",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        state_dir=state_dir,
        input_data=payload,
        text=False,
    )


def _task_record(
    task_root: Path,
    coordinator_root: Path,
    workspace: dict[str, Any],
    *,
    state_dir: Path,
) -> dict[str, Any]:
    try:
        task_set = load_resolved_task_source_set(
            task_root,
            state_dir=state_dir,
            authority_root=coordinator_root,
        )
    except TaskSourceError as exc:
        raise TaskAcceptanceError(str(exc)) from exc

    rows = task_set.get("tasks")
    if not isinstance(rows, list):
        raise TaskAcceptanceError("TaskSourceSet task records are malformed")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("task"), dict)
        and row["task"].get("id") == workspace["task_id"]
    ]
    if len(matches) != 1:
        raise TaskAcceptanceError(
            "active TaskSpec is missing or duplicated in the TaskSourceSet"
        )
    row = matches[0]
    if row.get("task_spec_sha256") != workspace["task_spec_sha256"]:
        raise TaskAcceptanceError(
            "active TaskSpec digest changed before candidate sealing"
        )
    return row


def _commit_subject(task_record: dict[str, Any]) -> str:
    task = task_record["task"]
    raw = task.get("commit_subject")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"Task {task['id']}"


def _git_identity(
    task_root: Path,
    *,
    state_dir: Path,
) -> dict[str, str]:
    def read(key: str) -> str | None:
        cp = _git(
            task_root,
            "config",
            "--get",
            key,
            state_dir=state_dir,
        )
        if cp.returncode != 0:
            return None
        value = str(cp.stdout or "").strip()
        if not value or any(ch in value for ch in ("\n", "\r", "\0")):
            return None
        return value

    name = read("user.name") or "Claude Auto"
    email = read("user.email") or "claude-auto@local.invalid"
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def _candidate_dir(state_dir: Path) -> Path:
    return ensure_private_dir(state_dir / "tasks" / "candidates")


def _candidate_path(state_dir: Path, candidate_sha: str) -> Path:
    if not (
        40 <= len(candidate_sha) <= 64
        and all(ch in "0123456789abcdef" for ch in candidate_sha.lower())
    ):
        raise TaskAcceptanceError("candidate SHA is malformed")
    return _candidate_dir(state_dir) / f"{candidate_sha.lower()}.json"


def _persist_candidate_record(
    state_dir: Path,
    *,
    workspace: dict[str, Any],
    candidate_sha: str,
    candidate_tree_sha: str,
    staged_entries: list[dict[str, Any]],
    no_op: bool,
) -> dict[str, Any]:
    core = {
        "schema_version": 1,
        "task_id": workspace["task_id"],
        "task_spec_sha256": workspace["task_spec_sha256"],
        "task_source_set_sha256": workspace["task_source_set_sha256"],
        "execution_envelope_sha256": workspace[
            "execution_envelope_sha256"
        ],
        "base_sha": workspace["product_base_sha"],
        "candidate_sha": candidate_sha,
        "candidate_tree_sha": candidate_tree_sha,
        "no_op": no_op,
        "staged_entries": staged_entries,
    }
    record = {
        **core,
        "candidate_record_sha256": _digest(core),
        "sealed_at": utcnow(),
    }
    json_dump(_candidate_path(state_dir, candidate_sha), record)
    return record


def _ref_value(
    root: Path,
    ref: str,
    *,
    state_dir: Path,
) -> str | None:
    cp = _git(
        root,
        "show-ref",
        "--verify",
        "--hash",
        ref,
        state_dir=state_dir,
    )
    if cp.returncode == 1:
        return None
    if cp.returncode != 0:
        detail = str(cp.stderr or cp.stdout or "git show-ref failed")
        raise TaskAcceptanceError(detail.strip()[:1600])
    value = str(cp.stdout or "").strip().lower()
    if not value:
        raise TaskAcceptanceError("candidate ref resolved to an empty object id")
    return value


def _binding_with_candidate_ref(
    binding: dict[str, Any],
    ref: str,
    candidate_sha: str,
) -> dict[str, Any]:
    rows = binding.get("refs")
    if not isinstance(rows, list):
        raise TaskAcceptanceError("stored Git ref binding is malformed")
    out = [
        {"name": str(row["name"]), "object": str(row["object"]).lower()}
        for row in rows
        if isinstance(row, dict)
    ]
    if len(out) != len(rows):
        raise TaskAcceptanceError("stored Git ref binding contains malformed rows")
    if any(row["name"] == ref for row in out):
        raise TaskAcceptanceError(
            "candidate ref already existed in the pre-candidate ref binding"
        )
    out.append({"name": ref, "object": candidate_sha.lower()})
    out.sort(key=lambda row: row["name"].encode("utf-8"))
    return {"schema_version": 1, "refs": out}


def _reconcile_pending_candidate_ref(
    coordinator_root: Path,
    task_root: Path,
    workspace: dict[str, Any],
    *,
    state_dir: Path,
) -> dict[str, Any]:
    candidate_sha = str(workspace.get("candidate_sha") or "").lower()
    if not candidate_sha:
        raise TaskAcceptanceError("pending candidate SHA is missing")
    candidate_ref = candidate_ref_for_workspace(workspace)

    object_check = _git(
        coordinator_root,
        "cat-file",
        "-e",
        f"{candidate_sha}^{{commit}}",
        state_dir=state_dir,
    )
    if object_check.returncode != 0:
        raise TaskAcceptanceError(
            "pending candidate commit object is unavailable"
        )

    current_ref = _ref_value(
        coordinator_root,
        candidate_ref,
        state_dir=state_dir,
    )
    if current_ref is None:
        boundary = evaluate_task_workspace_boundary(
            coordinator_root,
            task_root=task_root,
            state_dir=state_dir,
        )
        if boundary["status"] != "VALID":
            detail = "; ".join(
                f"{row['path']}: {row['reason']}"
                for row in boundary["violations"][:40]
            )
            raise TaskAcceptanceError(
                "candidate ref cannot be created after workspace drift: "
                + detail
            )
        zero = "0" * len(candidate_sha)
        _require_git(
            coordinator_root,
            "update-ref",
            candidate_ref,
            candidate_sha,
            zero,
            state_dir=state_dir,
        )
    elif current_ref != candidate_sha:
        raise TaskAcceptanceError(
            "candidate ref points at an unexpected object"
        )
    else:
        stored_binding = workspace.get("git_ref_binding")
        if not isinstance(stored_binding, dict):
            raise TaskAcceptanceError(
                "pending candidate has no prior Git ref binding"
            )
        expected = _binding_with_candidate_ref(
            stored_binding,
            candidate_ref,
            candidate_sha,
        )
        current = capture_git_ref_binding(
            coordinator_root,
            state_dir=state_dir,
        )
        if _digest(expected) != _digest(current):
            raise TaskAcceptanceError(
                "Git refs changed beyond the exact pending candidate ref"
            )

    _reset_index(task_root, state_dir=state_dir)
    workspace = update_task_workspace_package_state(
        coordinator_root,
        state_dir=state_dir,
        expected_states={"CANDIDATE"},
        updates={
            "candidate_ref": candidate_ref,
            "candidate_ref_pending": False,
            "candidate_ref_bound_at": utcnow(),
        },
        refresh_ref_binding=True,
    )
    return workspace


def reconcile_task_candidate(
    coordinator_root: Path,
    *,
    state_dir: Path | None = None,
    acquire_lease: bool = True,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(coordinator_root)
    )
    context = (
        SupervisorLease(state_root, coordinator_root)
        if acquire_lease
        else nullcontext()
    )
    with context:
        workspace = load_active_task_workspace(
            coordinator_root,
            state_dir=state_root,
        )
        if workspace is None:
            raise TaskAcceptanceError("no active TaskWorkspaceRecord exists")
        if workspace["lifecycle_state"] != "CANDIDATE":
            raise TaskAcceptanceError(
                "candidate reconciliation requires CANDIDATE lifecycle state"
            )
        task_root = Path(workspace["task_worktree"]).expanduser().resolve()
        candidate_sha = str(workspace.get("candidate_sha") or "").lower()
        if not candidate_sha:
            raise TaskAcceptanceError("candidate lifecycle has no candidate SHA")

        if bool(workspace.get("no_op_candidate")):
            if candidate_sha != str(workspace["product_base_sha"]).lower():
                raise TaskAcceptanceError(
                    "no-op candidate does not equal the recorded product base"
                )
            _reset_index(task_root, state_dir=state_root)
            boundary = evaluate_task_workspace_boundary(
                coordinator_root,
                task_root=task_root,
                state_dir=state_root,
            )
            if boundary["status"] != "VALID":
                detail = "; ".join(
                    f"{row['path']}: {row['reason']}"
                    for row in boundary["violations"][:40]
                )
                raise TaskAcceptanceError(
                    "no-op candidate workspace boundary changed: " + detail
                )
            return workspace

        if bool(workspace.get("candidate_ref_pending")):
            workspace = _reconcile_pending_candidate_ref(
                coordinator_root,
                task_root,
                workspace,
                state_dir=state_root,
            )

        candidate_ref = candidate_ref_for_workspace(workspace)
        current_ref = _ref_value(
            coordinator_root,
            candidate_ref,
            state_dir=state_root,
        )
        if current_ref != candidate_sha:
            raise TaskAcceptanceError(
                "sealed candidate ref does not match the candidate SHA"
            )
        boundary = evaluate_task_workspace_boundary(
            coordinator_root,
            task_root=task_root,
            state_dir=state_root,
        )
        if boundary["status"] != "VALID":
            detail = "; ".join(
                f"{row['path']}: {row['reason']}"
                for row in boundary["violations"][:40]
            )
            raise TaskAcceptanceError(
                "sealed candidate workspace boundary changed: " + detail
            )
        return workspace



def reopen_task_candidate_for_repair(
    coordinator_root: Path,
    *,
    findings: list[str] | None = None,
    state_dir: Path | None = None,
    acquire_lease: bool = True,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(coordinator_root)
    )
    context = (
        SupervisorLease(state_root, coordinator_root)
        if acquire_lease
        else nullcontext()
    )
    with context:
        workspace = load_active_task_workspace(
            coordinator_root,
            state_dir=state_root,
        )
        if workspace is None:
            raise TaskAcceptanceError("no active TaskWorkspaceRecord exists")
        if workspace["lifecycle_state"] not in {"CANDIDATE", "VERIFYING", "BLOCKED"}:
            raise TaskAcceptanceError(
                "candidate repair reopen requires CANDIDATE, VERIFYING or BLOCKED "
                f"state, found {workspace['lifecycle_state']!r}"
            )
        candidate_sha = str(workspace.get("candidate_sha") or "").lower()
        if not candidate_sha:
            raise TaskAcceptanceError(
                "candidate repair reopen requires an exact candidate SHA"
            )
        task_root = Path(workspace["task_worktree"]).expanduser().resolve()

        # Persist intent before mutating the package candidate ref so a crash
        # after ref deletion can be reconciled by calling this operation again.
        if not bool(workspace.get("candidate_ref_release_pending")):
            workspace = update_task_workspace_package_state(
                coordinator_root,
                state_dir=state_root,
                expected_states={"CANDIDATE", "VERIFYING", "BLOCKED"},
                updates={
                    "candidate_ref_release_pending": True,
                    "repair_findings": [
                        str(item)[:1800]
                        for item in (findings or [])
                        if str(item).strip()
                    ][:100],
                    "candidate_reopen_started_at": utcnow(),
                },
            )

        if not bool(workspace.get("no_op_candidate")):
            candidate_ref = candidate_ref_for_workspace(workspace)
            current_ref = _ref_value(
                coordinator_root,
                candidate_ref,
                state_dir=state_root,
            )
            if current_ref is not None and current_ref != candidate_sha:
                raise TaskAcceptanceError(
                    "candidate ref moved to an unexpected object before repair reopen"
                )
            if current_ref == candidate_sha:
                _require_git(
                    coordinator_root,
                    "update-ref",
                    "-d",
                    candidate_ref,
                    candidate_sha,
                    state_dir=state_root,
                )

        _reset_index(task_root, state_dir=state_root)
        updated = update_task_workspace_package_state(
            coordinator_root,
            state_dir=state_root,
            expected_states={"CANDIDATE", "VERIFYING", "BLOCKED"},
            updates={
                "lifecycle_state": "ACTIVE",
                "candidate_sha": None,
                "candidate_tree_sha": None,
                "candidate_ref": None,
                "candidate_ref_pending": False,
                "candidate_ref_release_pending": False,
                "no_op_candidate": False,
                "verified_candidate_sha": None,
                "acceptance_attestation_sha256": None,
                "candidate_reopened_at": utcnow(),
                "last_rejected_candidate_sha": candidate_sha,
            },
            refresh_ref_binding=True,
        )
        return {
            "status": "ACTIVE",
            "task_id": updated["task_id"],
            "rejected_candidate_sha": candidate_sha,
            "task_workspace_sha256": updated["task_workspace_sha256"],
            "repair_findings": updated.get("repair_findings") or [],
        }


def _seal_locked(
    coordinator_root: Path,
    *,
    state_dir: Path,
) -> dict[str, Any]:
    workspace = load_active_task_workspace(
        coordinator_root,
        state_dir=state_dir,
    )
    if workspace is None:
        raise TaskAcceptanceError("no active TaskWorkspaceRecord exists")
    if workspace["lifecycle_state"] == "CANDIDATE":
        return reconcile_task_candidate(
            coordinator_root,
            state_dir=state_dir,
            acquire_lease=False,
        )
    if workspace["lifecycle_state"] != "ACTIVE":
        raise TaskAcceptanceError(
            "candidate sealing requires ACTIVE task workspace state, found "
            f"{workspace['lifecycle_state']!r}"
        )

    task_root = Path(workspace["task_worktree"]).expanduser().resolve()
    boundary = evaluate_task_workspace_boundary(
        coordinator_root,
        task_root=task_root,
        state_dir=state_dir,
    )
    if boundary["status"] != "VALID":
        detail = "; ".join(
            f"{row['path']}: {row['reason']}"
            for row in boundary["violations"][:40]
        )
        raise TaskAcceptanceError(
            "candidate sealing is blocked by task workspace drift: " + detail
        )

    try:
        envelope = load_active_execution_envelope(
            task_root,
            state_dir=state_dir,
            authority_root=coordinator_root,
            git_state_dir=state_dir,
        )
    except ExecutionEnvelopeError as exc:
        raise TaskAcceptanceError(str(exc)) from exc
    if envelope["execution_envelope_sha256"] != workspace[
        "execution_envelope_sha256"
    ]:
        raise TaskAcceptanceError(
            "task workspace and ExecutionEnvelope digests disagree"
        )

    _reset_index(task_root, state_dir=state_dir)
    evaluated = evaluate_active_workspace(
        task_root,
        envelope=envelope,
        state_dir=state_dir,
        authority_root=coordinator_root,
        git_state_dir=state_dir,
    )
    if evaluated["status"] != "VALID":
        detail = "; ".join(
            f"{row['path']}: {row['reason']}"
            for row in evaluated["violations"][:40]
        )
        raise TaskAcceptanceError(
            "candidate worktree violates the active ExecutionEnvelope: "
            + detail
        )

    changed = _changed_paths(task_root, state_dir=state_dir)
    promotable = [
        path
        for path in changed
        if path_matches_any(path, envelope["promotion_paths"])
        and not path_matches_any(
            path,
            envelope["runtime_scratch_paths"],
        )
    ]
    _stage_exact_paths(
        task_root,
        promotable,
        state_dir=state_dir,
    )
    try:
        stage = validate_staged_diff(
            task_root,
            envelope=envelope,
            state_dir=state_dir,
            authority_root=coordinator_root,
            git_state_dir=state_dir,
        )
    except ExecutionEnvelopeError as exc:
        _reset_index(task_root, state_dir=state_dir)
        raise TaskAcceptanceError(str(exc)) from exc

    tree = str(
        _require_git(
            task_root,
            "write-tree",
            state_dir=state_dir,
        ).stdout
    ).strip().lower()
    base = str(workspace["product_base_sha"]).lower()
    base_tree = str(
        _require_git(
            task_root,
            "rev-parse",
            f"{base}^{{tree}}",
            state_dir=state_dir,
        ).stdout
    ).strip().lower()

    if tree == base_tree:
        _reset_index(task_root, state_dir=state_dir)
        workspace = update_task_workspace_package_state(
            coordinator_root,
            state_dir=state_dir,
            expected_states={"ACTIVE"},
            updates={
                "lifecycle_state": "CANDIDATE",
                "candidate_sha": base,
                "candidate_tree_sha": tree,
                "candidate_ref": None,
                "candidate_ref_pending": False,
                "no_op_candidate": True,
                "verified_candidate_sha": None,
                "acceptance_attestation_sha256": None,
                "candidate_sealed_at": utcnow(),
            },
        )
        candidate_record = _persist_candidate_record(
            state_dir,
            workspace=workspace,
            candidate_sha=base,
            candidate_tree_sha=tree,
            staged_entries=stage["staged_entries"],
            no_op=True,
        )
        return {
            "status": "CANDIDATE",
            "task_id": workspace["task_id"],
            "candidate_sha": base,
            "no_op": True,
            "candidate_record_sha256": candidate_record[
                "candidate_record_sha256"
            ],
        }

    task_record = _task_record(
        task_root,
        coordinator_root,
        workspace,
        state_dir=state_dir,
    )
    message = _commit_subject(task_record) + "\n"
    commit = str(
        _require_git(
            task_root,
            "commit-tree",
            tree,
            "-p",
            base,
            state_dir=state_dir,
            input_data=message,
            extra_env=_git_identity(
                task_root,
                state_dir=state_dir,
            ),
        ).stdout
    ).strip().lower()

    parent_line = str(
        _require_git(
            task_root,
            "rev-list",
            "--parents",
            "-n",
            "1",
            commit,
            state_dir=state_dir,
        ).stdout
    ).strip().split()
    if parent_line != [commit, base]:
        _reset_index(task_root, state_dir=state_dir)
        raise TaskAcceptanceError(
            "candidate commit parent is not the exact product base"
        )

    try:
        admitted = validate_promotion_target_for_active_envelope(
            task_root,
            base=base,
            target=commit,
            state_dir=state_dir,
            authority_root=coordinator_root,
            git_state_dir=state_dir,
        )
    except ExecutionEnvelopeError as exc:
        _reset_index(task_root, state_dir=state_dir)
        raise TaskAcceptanceError(str(exc)) from exc
    if not isinstance(admitted, dict) or admitted.get("status") != "VALID":
        _reset_index(task_root, state_dir=state_dir)
        raise TaskAcceptanceError(
            "candidate target did not pass task promotion admission"
        )

    candidate_ref = candidate_ref_for_workspace(workspace)
    workspace = update_task_workspace_package_state(
        coordinator_root,
        state_dir=state_dir,
        expected_states={"ACTIVE"},
        updates={
            "lifecycle_state": "CANDIDATE",
            "candidate_sha": commit,
            "candidate_tree_sha": tree,
            "candidate_ref": candidate_ref,
            "candidate_ref_pending": True,
            "no_op_candidate": False,
            "verified_candidate_sha": None,
            "acceptance_attestation_sha256": None,
            "candidate_sealed_at": utcnow(),
        },
    )

    workspace = _reconcile_pending_candidate_ref(
        coordinator_root,
        task_root,
        workspace,
        state_dir=state_dir,
    )
    candidate_record = _persist_candidate_record(
        state_dir,
        workspace=workspace,
        candidate_sha=commit,
        candidate_tree_sha=tree,
        staged_entries=stage["staged_entries"],
        no_op=False,
    )
    return {
        "status": "CANDIDATE",
        "task_id": workspace["task_id"],
        "candidate_sha": commit,
        "candidate_ref": candidate_ref,
        "no_op": False,
        "candidate_record_sha256": candidate_record[
            "candidate_record_sha256"
        ],
    }


def seal_task_candidate(
    coordinator_root: Path,
    *,
    state_dir: Path | None = None,
    acquire_lease: bool = True,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(coordinator_root)
    )
    context = (
        SupervisorLease(state_root, coordinator_root)
        if acquire_lease
        else nullcontext()
    )
    with context:
        return _seal_locked(
            coordinator_root,
            state_dir=state_root,
        )
