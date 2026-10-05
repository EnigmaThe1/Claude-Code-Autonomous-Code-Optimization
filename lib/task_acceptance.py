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
import textwrap
import unicodedata
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Any

from accepted_task import (
    AcceptedTaskError,
    load_accepted_task_record,
    persist_accepted_task_record,
)
from execution_envelope import (
    ExecutionEnvelopeError,
    evaluate_active_workspace,
    invalidate_task_authority_after_head_change,
    load_active_execution_envelope,
    path_matches_any,
    validate_promotion_target_for_active_envelope,
    validate_staged_diff,
)
from git_trust import trusted_git_env
from governance_contract import canonical_json_bytes
from repo_identity import SupervisorLease, repo_state_dir
from control_plane import run_readonly_plan_agent
from promotion_policy import (
    record_promotion_attestation,
    require_exact_attestation,
)
from protocols import parse_json_protocol
from provider_config import provider_from_args
from repo_profile import profile_repo
from runtime_paths import ensure_private_dir, utcnow
from settings_policy import make_readonly_settings
from state_store import json_dump, load_json
from task_sources import (
    TaskSourceError,
    load_resolved_task_source_set,
    resolve_task_sources,
)
from task_authority import ready_frontier, task_readiness
from verification import _run_verification_command, _verification_commands
from workspace_recovery import promote_fast_forward
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


TASK_ACCEPTANCE_CONTRACT = "claude-auto-task-acceptance-v1"


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
    # show-ref --verify reports a missing fully-qualified ref as a fatal
    # "not a valid ref" on some supported Git versions. rev-parse --quiet is
    # portable for this package-generated/validated ref namespace and cleanly
    # distinguishes an absent ref from a present exact commit.
    cp = _git(
        root,
        "rev-parse",
        "--verify",
        "--quiet",
        f"{ref}^{{commit}}",
        state_dir=state_dir,
    )
    if cp.returncode != 0:
        return None
    value = str(cp.stdout or "").strip().lower()
    if not (
        40 <= len(value) <= 64
        and all(ch in "0123456789abcdef" for ch in value)
    ):
        raise TaskAcceptanceError(
            "candidate ref resolved to an invalid commit object id"
        )
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





def _verification_dir(state_dir: Path) -> Path:
    return ensure_private_dir(state_dir / "tasks" / "verification")


def _verification_bundle_path(
    state_dir: Path,
    candidate_sha: str,
) -> Path:
    return _verification_dir(state_dir) / f"{candidate_sha}.json"


def _verification_worktree_parent(state_dir: Path) -> Path:
    return ensure_private_dir(
        state_dir / "tasks" / "verification-worktrees"
    )


def _verification_worktree_path(
    state_dir: Path,
    candidate_sha: str,
    label: str,
) -> Path:
    if label not in {"base", "candidate", "independent"}:
        raise TaskAcceptanceError("unknown verification worktree label")
    token = candidate_sha[:24].lower()
    return (
        _verification_worktree_parent(state_dir)
        / token
        / label
    ).resolve()


def _remove_verification_worktree(
    coordinator_root: Path,
    path: Path,
    *,
    state_dir: Path,
) -> None:
    parent = _verification_worktree_parent(state_dir).resolve()
    target = path.expanduser().resolve()
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise TaskAcceptanceError(
            "verification worktree cleanup target escaped package state"
        ) from exc
    if target == parent:
        raise TaskAcceptanceError(
            "verification worktree cleanup target is package parent"
        )
    cp = _git(
        coordinator_root,
        "worktree",
        "remove",
        "--force",
        str(target),
        state_dir=state_dir,
    )
    if cp.returncode != 0 and target.exists():
        detail = str(cp.stderr or cp.stdout or "git worktree remove failed")
        raise TaskAcceptanceError(detail.strip()[:1600])


def _fresh_verification_worktree(
    coordinator_root: Path,
    *,
    sha: str,
    path: Path,
    state_dir: Path,
) -> Path:
    if path.exists():
        _remove_verification_worktree(
            coordinator_root,
            path,
            state_dir=state_dir,
        )
    ensure_private_dir(path.parent)
    cp = _git(
        coordinator_root,
        "worktree",
        "add",
        "--detach",
        str(path),
        sha,
        state_dir=state_dir,
    )
    if cp.returncode != 0:
        detail = str(cp.stderr or cp.stdout or "git worktree add failed")
        raise TaskAcceptanceError(
            "unable to create exact verification worktree: "
            + detail.strip()[:1600]
        )
    head = str(
        _require_git(
            path,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            state_dir=state_dir,
        ).stdout
    ).strip().lower()
    if head != sha.lower():
        raise TaskAcceptanceError(
            "verification worktree HEAD differs from requested exact SHA"
        )
    branch = str(
        _require_git(
            path,
            "branch",
            "--show-current",
            state_dir=state_dir,
        ).stdout
    ).strip()
    if branch:
        raise TaskAcceptanceError(
            "verification worktree must be detached at the exact SHA"
        )
    return path


def _verification_semantic_receipt(
    receipt: dict[str, Any],
) -> dict[str, Any]:
    return {
        key: receipt.get(key)
        for key in (
            "category",
            "command",
            "effective_command",
            "command_adjustment",
            "exit_code",
            "timed_out",
            "signature",
            "tracked_source_unchanged",
            "source_fingerprint",
            "execution_boundary",
            "sandboxed",
            "environment_scrubbed",
            "verdict",
        )
    }


def _classify_candidate_receipt(
    receipt: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> tuple[str, str | None]:
    if receipt.get("execution_boundary") == "unavailable":
        return (
            "UNVERIFIED",
            "safe repository execution boundary is unavailable",
        )
    if receipt.get("timed_out"):
        return "UNVERIFIED", "verification command timed out"
    try:
        exit_code = int(receipt.get("exit_code", 1))
    except (TypeError, ValueError):
        exit_code = 1
    if exit_code in {126, 127}:
        return (
            "UNVERIFIED",
            f"verification shell could not execute command (exit {exit_code})",
        )
    if not receipt.get("tracked_source_unchanged", True):
        return (
            "FAIL",
            "verification command mutated tracked/indexed candidate source",
        )
    if exit_code == 0:
        return "PASS", None
    if (
        isinstance(baseline, dict)
        and baseline.get("exit_code") == receipt.get("exit_code")
        and baseline.get("signature") == receipt.get("signature")
    ):
        return "BASELINE_FAILURE_UNCHANGED", None
    return "FAIL", f"verification command exited {exit_code}"


def _strong_verification_marker(root: Path) -> bool:
    markers = (
        "tox.ini",
        "noxfile.py",
        "build.gradle",
        "build.gradle.kts",
        "pom.xml",
        "CMakeLists.txt",
        "Rakefile",
    )
    return (
        any((root / marker).exists() for marker in markers)
        or bool(list(root.glob("*.sln")))
        or bool(list(root.glob("*.csproj")))
    )


def _persist_verification_bundle(
    state_dir: Path,
    *,
    workspace: dict[str, Any],
    commands: list[tuple[str, str]],
    task_claims: list[str],
    baseline_receipts: list[dict[str, Any]],
    candidate_receipts: list[dict[str, Any]],
    verdict: str,
    findings: list[str],
) -> dict[str, Any]:
    candidate_sha = str(workspace["candidate_sha"]).lower()
    semantic = {
        "schema_version": 1,
        "task_id": workspace["task_id"],
        "task_spec_sha256": workspace["task_spec_sha256"],
        "task_source_set_sha256": workspace["task_source_set_sha256"],
        "execution_envelope_sha256": workspace[
            "execution_envelope_sha256"
        ],
        "base_sha": workspace["product_base_sha"],
        "candidate_sha": candidate_sha,
        "candidate_tree_sha": workspace.get("candidate_tree_sha"),
        "no_op": bool(workspace.get("no_op_candidate")),
        "commands": [
            {"category": category, "command": command}
            for category, command in commands
        ],
        "task_verification_claims": list(task_claims),
        "baseline_receipts": [
            _verification_semantic_receipt(receipt)
            for receipt in baseline_receipts
        ],
        "candidate_receipts": [
            _verification_semantic_receipt(receipt)
            for receipt in candidate_receipts
        ],
        "verdict": verdict,
        "findings": list(findings),
    }
    bundle = {
        **semantic,
        "verification_bundle_sha256": _digest(semantic),
        "recorded_at": utcnow(),
        "baseline_receipt_details": baseline_receipts,
        "candidate_receipt_details": candidate_receipts,
    }
    json_dump(
        _verification_bundle_path(state_dir, candidate_sha),
        bundle,
    )
    return bundle


def verify_task_candidate_deterministic(
    coordinator_root: Path,
    *,
    timeout: int = 900,
    trust_repo_scripts: bool = False,
    unrestricted_host: bool = False,
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
            raise TaskAcceptanceError(
                "no active TaskWorkspaceRecord exists"
            )
        if workspace["lifecycle_state"] == "CANDIDATE":
            workspace = reconcile_task_candidate(
                coordinator_root,
                state_dir=state_root,
                acquire_lease=False,
            )
            validate_task_candidate_worktree(
                coordinator_root,
                state_dir=state_root,
            )
            workspace = update_task_workspace_package_state(
                coordinator_root,
                state_dir=state_root,
                expected_states={"CANDIDATE"},
                updates={
                    "lifecycle_state": "VERIFYING",
                    "deterministic_verification_sha256": None,
                    "deterministic_verification_verdict": None,
                    "verification_started_at": utcnow(),
                },
            )
        elif workspace["lifecycle_state"] == "VERIFYING":
            validate_task_candidate_worktree(
                coordinator_root,
                state_dir=state_root,
            )
        else:
            raise TaskAcceptanceError(
                "deterministic verification requires CANDIDATE or "
                f"VERIFYING state, found {workspace['lifecycle_state']!r}"
            )

        candidate_sha = str(workspace["candidate_sha"]).lower()
        base_sha = str(workspace["product_base_sha"]).lower()
        task_record = _task_record(
            Path(workspace["task_worktree"]).expanduser().resolve(),
            coordinator_root,
            workspace,
            state_dir=state_root,
        )
        task_claims = [
            str(item)
            for item in (task_record["task"].get("verification") or [])
        ]

        candidate_path = _verification_worktree_path(
            state_root,
            candidate_sha,
            "candidate",
        )
        base_path = _verification_worktree_path(
            state_root,
            candidate_sha,
            "base",
        )
        baseline_receipts: list[dict[str, Any]] = []
        candidate_receipts: list[dict[str, Any]] = []
        commands: list[tuple[str, str]] = []
        findings: list[str] = []
        verdict = "PASS"
        cleanup_errors: list[str] = []

        try:
            base_root = _fresh_verification_worktree(
                coordinator_root,
                sha=base_sha,
                path=base_path,
                state_dir=state_root,
            )
            candidate_root = _fresh_verification_worktree(
                coordinator_root,
                sha=candidate_sha,
                path=candidate_path,
                state_dir=state_root,
            )

            candidate_profile = asdict(profile_repo(candidate_root))
            commands = list(_verification_commands(candidate_profile))
            for category, command in commands:
                baseline = _run_verification_command(
                    base_root,
                    category,
                    command,
                    int(timeout),
                    trust_repo_scripts=trust_repo_scripts,
                    unrestricted_host=unrestricted_host,
                )
                if not baseline.get("tracked_source_unchanged", True):
                    baseline["verdict"] = "INVALID_BASELINE"
                    baseline_receipts.append(baseline)
                    verdict = "UNVERIFIED"
                    findings.append(
                        f"{category}: {command} mutated tracked/indexed "
                        "source at the exact task base"
                    )
                    break
                baseline["verdict"] = (
                    "PASS"
                    if int(baseline.get("exit_code", 1)) == 0
                    else "BASELINE_FAILURE"
                )
                baseline_receipts.append(baseline)

            if verdict == "PASS":
                baseline_by_key = {
                    (str(row.get("category")), str(row.get("command"))): row
                    for row in baseline_receipts
                }
                for category, command in commands:
                    receipt = _run_verification_command(
                        candidate_root,
                        category,
                        command,
                        int(timeout),
                        trust_repo_scripts=trust_repo_scripts,
                        unrestricted_host=unrestricted_host,
                    )
                    baseline = baseline_by_key.get((category, command))
                    row_verdict, reason = _classify_candidate_receipt(
                        receipt,
                        baseline,
                    )
                    receipt["verdict"] = row_verdict
                    candidate_receipts.append(receipt)
                    if row_verdict == "UNVERIFIED":
                        verdict = "UNVERIFIED"
                        if reason:
                            findings.append(
                                f"{category}: {command}: {reason}"
                            )
                    elif row_verdict == "FAIL" and verdict != "UNVERIFIED":
                        verdict = "FAIL"
                        if reason:
                            findings.append(
                                f"{category}: {command}: {reason}"
                            )

                if not commands and _strong_verification_marker(candidate_root):
                    verdict = "UNVERIFIED"
                    findings.append(
                        "repository contains recognised verification/build "
                        "markers but no deterministic verification command "
                        "could be established"
                    )
        finally:
            for path in (candidate_path, base_path):
                try:
                    _remove_verification_worktree(
                        coordinator_root,
                        path,
                        state_dir=state_root,
                    )
                except TaskAcceptanceError as exc:
                    cleanup_errors.append(str(exc))

        if cleanup_errors:
            verdict = "UNVERIFIED"
            findings.extend(
                "verification worktree cleanup failed: " + item
                for item in cleanup_errors
            )

        bundle = _persist_verification_bundle(
            state_root,
            workspace=workspace,
            commands=commands,
            task_claims=task_claims,
            baseline_receipts=baseline_receipts,
            candidate_receipts=candidate_receipts,
            verdict=verdict,
            findings=findings,
        )

        lifecycle = "VERIFYING" if verdict == "PASS" else "BLOCKED"
        workspace = update_task_workspace_package_state(
            coordinator_root,
            state_dir=state_root,
            expected_states={"VERIFYING"},
            updates={
                "lifecycle_state": lifecycle,
                "deterministic_verification_sha256": bundle[
                    "verification_bundle_sha256"
                ],
                "deterministic_verification_verdict": verdict,
                "deterministic_verification_findings": findings[:100],
                "verification_finished_at": utcnow(),
            },
        )
        return {
            "status": verdict,
            "task_id": workspace["task_id"],
            "candidate_sha": candidate_sha,
            "verification_bundle_sha256": bundle[
                "verification_bundle_sha256"
            ],
            "command_count": len(commands),
            "findings": findings,
            "receipts": candidate_receipts,
            "baseline_receipts": baseline_receipts,
        }



def load_deterministic_verification_bundle(
    coordinator_root: Path,
    *,
    candidate_sha: str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(coordinator_root)
    )
    workspace = load_active_task_workspace(
        coordinator_root,
        state_dir=state_root,
    )
    if workspace is None:
        raise TaskAcceptanceError("no active TaskWorkspaceRecord exists")
    candidate = str(
        candidate_sha or workspace.get("candidate_sha") or ""
    ).lower()
    if not candidate:
        raise TaskAcceptanceError("candidate SHA is missing")
    path = _verification_bundle_path(state_root, candidate)
    try:
        bundle = load_json(path, {})
    except OSError as exc:
        raise TaskAcceptanceError(
            f"deterministic verification bundle is unreadable: {exc}"
        ) from exc
    if not isinstance(bundle, dict) or not bundle:
        raise TaskAcceptanceError(
            "deterministic verification bundle does not exist"
        )

    semantic = {
        key: bundle.get(key)
        for key in (
            "schema_version",
            "task_id",
            "task_spec_sha256",
            "task_source_set_sha256",
            "execution_envelope_sha256",
            "base_sha",
            "candidate_sha",
            "candidate_tree_sha",
            "no_op",
            "commands",
            "task_verification_claims",
            "baseline_receipts",
            "candidate_receipts",
            "verdict",
            "findings",
        )
    }
    if bundle.get("verification_bundle_sha256") != _digest(semantic):
        raise TaskAcceptanceError(
            "deterministic verification bundle integrity check failed"
        )
    if (
        semantic["task_id"] != workspace["task_id"]
        or semantic["task_spec_sha256"] != workspace["task_spec_sha256"]
        or semantic["task_source_set_sha256"]
        != workspace["task_source_set_sha256"]
        or semantic["execution_envelope_sha256"]
        != workspace["execution_envelope_sha256"]
        or str(semantic["base_sha"]).lower()
        != str(workspace["product_base_sha"]).lower()
        or str(semantic["candidate_sha"]).lower() != candidate
        or str(semantic["candidate_tree_sha"]).lower()
        != str(workspace.get("candidate_tree_sha") or "").lower()
    ):
        raise TaskAcceptanceError(
            "deterministic verification bundle is not bound to the "
            "current exact task candidate"
        )
    return bundle


def _task_verifier_record_path(
    state_dir: Path,
    candidate_sha: str,
) -> Path:
    return (
        _verification_dir(state_dir)
        / f"{candidate_sha}-independent.json"
    )


def _task_verifier_evidence_path(
    state_dir: Path,
    candidate_sha: str,
) -> Path:
    return (
        _verification_dir(state_dir)
        / f"{candidate_sha}-evidence.json"
    )


def _attestation_digest(attestation: dict[str, Any]) -> str:
    return _digest(attestation)


def verify_task_candidate_independent(
    coordinator_root: Path,
    args: Any,
    *,
    state_dir: Path | None = None,
    acquire_lease: bool = True,
    verifier_runner=run_readonly_plan_agent,
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
            raise TaskAcceptanceError(
                "no active TaskWorkspaceRecord exists"
            )
        if workspace["lifecycle_state"] != "VERIFYING":
            raise TaskAcceptanceError(
                "independent verification requires VERIFYING lifecycle state"
            )
        if workspace.get("deterministic_verification_verdict") != "PASS":
            raise TaskAcceptanceError(
                "independent verifier requires deterministic PASS evidence"
            )

        candidate_sha = str(workspace.get("candidate_sha") or "").lower()
        base_sha = str(workspace["product_base_sha"]).lower()
        if not candidate_sha:
            raise TaskAcceptanceError("candidate SHA is missing")
        validate_task_candidate_worktree(
            coordinator_root,
            state_dir=state_root,
        )
        bundle = load_deterministic_verification_bundle(
            coordinator_root,
            candidate_sha=candidate_sha,
            state_dir=state_root,
        )
        if bundle.get("verdict") != "PASS":
            raise TaskAcceptanceError(
                "deterministic verification bundle is not PASS"
            )

        task_root = Path(workspace["task_worktree"]).expanduser().resolve()
        task_record = _task_record(
            task_root,
            coordinator_root,
            workspace,
            state_dir=state_root,
        )
        task = task_record["task"]
        state = load_json(state_root / "state.json", {})
        objective = str(
            state.get("objective")
            if isinstance(state, dict)
            else ""
        ).strip() or (
            "Preserve the repository objective and satisfy the exact "
            "repository-owned TaskSpec."
        )

        diff = _git(
            coordinator_root,
            "diff",
            "--no-ext-diff",
            "--binary",
            base_sha,
            candidate_sha,
            "--",
            state_dir=state_root,
        )
        if diff.returncode != 0:
            detail = str(diff.stderr or diff.stdout or "git diff failed")
            raise TaskAcceptanceError(
                "unable to capture exact candidate diff: "
                + detail.strip()[:1600]
            )

        evidence = {
            "schema_version": 1,
            "objective": objective,
            "task": task,
            "task_spec_sha256": workspace["task_spec_sha256"],
            "task_source_set_sha256": workspace[
                "task_source_set_sha256"
            ],
            "execution_envelope_sha256": workspace[
                "execution_envelope_sha256"
            ],
            "base_sha": base_sha,
            "candidate_sha": candidate_sha,
            "candidate_diff": diff.stdout,
            "deterministic_verification_sha256": bundle[
                "verification_bundle_sha256"
            ],
            "deterministic_verification_verdict": bundle["verdict"],
            "deterministic_verification_commands": bundle["commands"],
            "deterministic_verification_receipts": bundle[
                "candidate_receipts"
            ],
            "task_verification_claims": bundle[
                "task_verification_claims"
            ],
        }
        evidence_path = _task_verifier_evidence_path(
            state_root,
            candidate_sha,
        )
        json_dump(evidence_path, evidence)

        verifier_root = _verification_worktree_path(
            state_root,
            candidate_sha,
            "independent",
        )
        verifier_record: dict[str, Any] = {}
        try:
            verifier_root = _fresh_verification_worktree(
                coordinator_root,
                sha=candidate_sha,
                path=verifier_root,
                state_dir=state_root,
            )
            readonly = (
                _verification_dir(state_root)
                / f"{candidate_sha}-settings-readonly.json"
            )
            json_dump(
                readonly,
                make_readonly_settings(state_root, verifier_root),
            )
            env, provider_detail = provider_from_args(args)
            prompt = textwrap.dedent(
                f"""
                You are the independent Task Verifier. You did not author
                this candidate. Operate HARD READ-ONLY and verify exactly
                one immutable repository TaskSpec candidate.

                TASK ID: {workspace['task_id']}
                EXACT CANDIDATE SHA: {candidate_sha}
                BASE SHA: {base_sha}
                TASKSPEC SHA-256: {workspace['task_spec_sha256']}
                DETERMINISTIC VERIFICATION BUNDLE SHA-256:
                {bundle['verification_bundle_sha256']}

                PRIVATE EVIDENCE FILE:
                {evidence_path}

                Inspect the exact candidate repository and evidence. Verify
                that the exact candidate satisfies the repository-owned
                TaskSpec, its verification claims, dependencies and intended
                objective without unrelated scope expansion or material
                unresolved correctness/security defects.

                Deterministic verification receipts are evidence, not a
                command request. Do not modify any repository or package
                state. If evidence is insufficient, return BLOCKED. If any
                material defect remains, return REJECTED.

                Return exactly one JSON protocol record:
                TASK_ACCEPT_VERIFY: {{"verdict":"VERIFIED|REJECTED|BLOCKED",
                "task_id":"{workspace['task_id']}",
                "candidate_sha":"{candidate_sha}",
                "summary":"...","findings":["..."]}}
                """
            ).strip()

            result_text, meta = verifier_runner(
                root=verifier_root,
                sd=state_root,
                prompt=prompt,
                env=env,
                provider_detail=provider_detail,
                model=getattr(args, "model", None),
                timeout=(getattr(args, "timeout", 0) or None),
                max_turns=int(getattr(args, "max_turns", 35) or 35),
                verify_repo=True,
                max_budget_usd=getattr(args, "max_budget_usd", None),
                settings_path=readonly,
            )
            protocol = parse_json_protocol(
                result_text,
                "TASK_ACCEPT_VERIFY",
            )
            if not protocol:
                verdict = "BLOCKED"
                summary = "independent Task Verifier returned no valid protocol"
                findings = [summary]
            else:
                verdict = str(
                    protocol.get("verdict", "BLOCKED")
                ).upper()
                summary = str(protocol.get("summary", ""))[:1800]
                raw_findings = protocol.get("findings")
                findings = (
                    [str(item)[:1800] for item in raw_findings[:100]]
                    if isinstance(raw_findings, list)
                    else []
                )
                if verdict not in {"VERIFIED", "REJECTED", "BLOCKED"}:
                    verdict = "BLOCKED"
                    findings.append(
                        "verifier returned an unsupported verdict"
                    )
                if str(protocol.get("task_id", "")) != workspace["task_id"]:
                    verdict = "BLOCKED"
                    findings.append(
                        "verifier protocol task ID does not match active task"
                    )
                if (
                    str(protocol.get("candidate_sha", "")).lower()
                    != candidate_sha
                ):
                    verdict = "BLOCKED"
                    findings.append(
                        "verifier protocol candidate SHA does not match exact candidate"
                    )
                if meta.get("repository_unchanged") is not True:
                    verdict = "BLOCKED"
                    findings.append(
                        "independent verifier read-only repository invariant "
                        "was not positively proven"
                    )

            verifier_semantic = {
                "schema_version": 1,
                "task_id": workspace["task_id"],
                "task_spec_sha256": workspace["task_spec_sha256"],
                "candidate_sha": candidate_sha,
                "base_sha": base_sha,
                "deterministic_verification_sha256": bundle[
                    "verification_bundle_sha256"
                ],
                "verdict": verdict,
                "summary": summary,
                "findings": findings,
                "provider": meta.get("provider"),
                "model": meta.get("model"),
                "repository_unchanged": meta.get(
                    "repository_unchanged"
                ),
            }
            verifier_record = {
                **verifier_semantic,
                "task_verifier_evidence_sha256": _digest(
                    verifier_semantic
                ),
                "meta": meta,
                "recorded_at": utcnow(),
            }
            json_dump(
                _task_verifier_record_path(
                    state_root,
                    candidate_sha,
                ),
                verifier_record,
            )
        finally:
            _remove_verification_worktree(
                coordinator_root,
                verifier_root,
                state_dir=state_root,
            )

        verdict = str(verifier_record.get("verdict") or "BLOCKED")
        findings = list(verifier_record.get("findings") or [])
        summary = str(verifier_record.get("summary") or "")[:1800]
        if verdict == "REJECTED":
            reopened = reopen_task_candidate_for_repair(
                coordinator_root,
                findings=findings,
                state_dir=state_root,
                acquire_lease=False,
            )
            return {
                "status": "REJECTED",
                "task_id": workspace["task_id"],
                "candidate_sha": candidate_sha,
                "summary": summary,
                "findings": findings,
                "reopened": reopened,
            }

        if verdict != "VERIFIED":
            blocked = update_task_workspace_package_state(
                coordinator_root,
                state_dir=state_root,
                expected_states={"VERIFYING"},
                updates={
                    "lifecycle_state": "BLOCKED",
                    "independent_verification_verdict": "BLOCKED",
                    "independent_verification_findings": findings,
                    "independent_verification_summary": summary,
                    "independent_verification_record_sha256": verifier_record[
                        "task_verifier_evidence_sha256"
                    ],
                    "independent_verification_finished_at": utcnow(),
                },
            )
            return {
                "status": "BLOCKED",
                "task_id": blocked["task_id"],
                "candidate_sha": candidate_sha,
                "summary": summary,
                "findings": findings,
            }

        evidence_sha256 = verifier_record[
            "task_verifier_evidence_sha256"
        ]
        try:
            attestation = record_promotion_attestation(
                coordinator_root,
                target_sha=candidate_sha,
                contract=TASK_ACCEPTANCE_CONTRACT,
                verifier=(
                    "task-verifier:"
                    + str(
                        verifier_record.get("model")
                        or getattr(args, "model", None)
                        or "native-default"
                    )
                ),
                evidence_sha256=evidence_sha256,
                summary=summary,
                metadata={
                    "provider": verifier_record.get("provider"),
                    "findings": findings,
                    "repository_unchanged": verifier_record.get(
                        "repository_unchanged"
                    ),
                    "deterministic_verification_sha256": bundle[
                        "verification_bundle_sha256"
                    ],
                    "task_spec_sha256": workspace[
                        "task_spec_sha256"
                    ],
                    "task_source_set_sha256": workspace[
                        "task_source_set_sha256"
                    ],
                    "execution_envelope_sha256": workspace[
                        "execution_envelope_sha256"
                    ],
                },
            )
        except ValueError as exc:
            raise TaskAcceptanceError(str(exc)) from exc

        attestation_sha256 = _attestation_digest(attestation)
        verified = update_task_workspace_package_state(
            coordinator_root,
            state_dir=state_root,
            expected_states={"VERIFYING"},
            updates={
                "lifecycle_state": "VERIFIED_PENDING_PROMOTION",
                "verified_candidate_sha": candidate_sha,
                "acceptance_attestation_sha256": attestation_sha256,
                "acceptance_attestation_contract": TASK_ACCEPTANCE_CONTRACT,
                "deterministic_verification_sha256": bundle[
                    "verification_bundle_sha256"
                ],
                "independent_verification_verdict": "VERIFIED",
                "independent_verification_findings": findings,
                "independent_verification_summary": summary,
                "independent_verification_record_sha256": evidence_sha256,
                "independent_verification_finished_at": utcnow(),
            },
        )
        return {
            "status": "VERIFIED",
            "task_id": verified["task_id"],
            "candidate_sha": candidate_sha,
            "attestation": attestation,
            "acceptance_attestation_sha256": attestation_sha256,
            "summary": summary,
            "findings": findings,
        }



def load_independent_verifier_record(
    coordinator_root: Path,
    *,
    candidate_sha: str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(coordinator_root)
    )
    workspace = load_active_task_workspace(
        coordinator_root,
        state_dir=state_root,
    )
    if workspace is None:
        raise TaskAcceptanceError("no active TaskWorkspaceRecord exists")
    candidate = str(
        candidate_sha or workspace.get("candidate_sha") or ""
    ).lower()
    if not candidate:
        raise TaskAcceptanceError("candidate SHA is missing")
    obj = load_json(
        _task_verifier_record_path(state_root, candidate),
        {},
    )
    if not isinstance(obj, dict) or not obj:
        raise TaskAcceptanceError(
            "independent Task Verifier record does not exist"
        )
    semantic = {
        key: obj.get(key)
        for key in (
            "schema_version",
            "task_id",
            "task_spec_sha256",
            "candidate_sha",
            "base_sha",
            "deterministic_verification_sha256",
            "verdict",
            "summary",
            "findings",
            "provider",
            "model",
            "repository_unchanged",
        )
    }
    if obj.get("task_verifier_evidence_sha256") != _digest(semantic):
        raise TaskAcceptanceError(
            "independent Task Verifier record integrity check failed"
        )
    if (
        semantic["task_id"] != workspace["task_id"]
        or semantic["task_spec_sha256"] != workspace["task_spec_sha256"]
        or str(semantic["candidate_sha"]).lower() != candidate
        or str(semantic["base_sha"]).lower()
        != str(workspace["product_base_sha"]).lower()
    ):
        raise TaskAcceptanceError(
            "independent Task Verifier record is not bound to the "
            "current exact task candidate"
        )
    return obj


def _accepted_history_dir(state_dir: Path) -> Path:
    return ensure_private_dir(state_dir / "tasks" / "history")


def _accepted_workspace_history_path(
    state_dir: Path,
    acceptance_sha256: str,
) -> Path:
    return (
        _accepted_history_dir(state_dir)
        / f"workspace-accepted-{acceptance_sha256[:24]}.json"
    )


def _current_head(root: Path, *, state_dir: Path) -> str:
    return str(
        _require_git(
            root,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            state_dir=state_dir,
        ).stdout
    ).strip().lower()


def _persist_acceptance_after_exact_product(
    coordinator_root: Path,
    *,
    workspace: dict[str, Any],
    state_dir: Path,
) -> dict[str, Any]:
    candidate_sha = str(workspace["candidate_sha"]).lower()
    if _current_head(coordinator_root, state_dir=state_dir) != candidate_sha:
        raise TaskAcceptanceError(
            "accepted-task recording requires product HEAD at the exact "
            "verified candidate SHA"
        )

    bundle = load_deterministic_verification_bundle(
        coordinator_root,
        candidate_sha=candidate_sha,
        state_dir=state_dir,
    )
    if bundle.get("verdict") != "PASS":
        raise TaskAcceptanceError(
            "accepted-task recording requires deterministic PASS evidence"
        )
    verifier = load_independent_verifier_record(
        coordinator_root,
        candidate_sha=candidate_sha,
        state_dir=state_dir,
    )
    if verifier.get("verdict") != "VERIFIED":
        raise TaskAcceptanceError(
            "accepted-task recording requires independent VERIFIED evidence"
        )

    try:
        attestation = require_exact_attestation(
            coordinator_root,
            candidate_sha,
            contract=TASK_ACCEPTANCE_CONTRACT,
        )
    except ValueError as exc:
        raise TaskAcceptanceError(str(exc)) from exc
    if not isinstance(attestation, dict):
        raise TaskAcceptanceError(
            "task acceptance attestation is missing"
        )
    attestation_sha256 = _attestation_digest(attestation)
    if attestation_sha256 != workspace.get(
        "acceptance_attestation_sha256"
    ):
        raise TaskAcceptanceError(
            "task acceptance attestation digest does not match workspace state"
        )
    if attestation.get("evidence_sha256") != verifier.get(
        "task_verifier_evidence_sha256"
    ):
        raise TaskAcceptanceError(
            "task acceptance attestation does not bind the independent "
            "verifier evidence"
        )
    metadata = attestation.get("metadata")
    if not isinstance(metadata, dict) or metadata.get(
        "deterministic_verification_sha256"
    ) != bundle.get("verification_bundle_sha256"):
        raise TaskAcceptanceError(
            "task acceptance attestation does not bind deterministic "
            "verification evidence"
        )

    semantic = {
        "schema_version": 1,
        "task_id": workspace["task_id"],
        "task_spec_sha256": workspace["task_spec_sha256"],
        "task_source_set_sha256": workspace[
            "task_source_set_sha256"
        ],
        "authority_snapshot_sha256": workspace[
            "authority_snapshot_sha256"
        ],
        "execution_envelope_sha256": workspace[
            "execution_envelope_sha256"
        ],
        "base_sha": workspace["product_base_sha"],
        "candidate_sha": candidate_sha,
        "accepted_product_sha": candidate_sha,
        "no_op": bool(workspace.get("no_op_candidate")),
        "verification_bundle_sha256": bundle[
            "verification_bundle_sha256"
        ],
        "verifier_attestation_sha256": attestation_sha256,
        "attestation_contract": TASK_ACCEPTANCE_CONTRACT,
    }
    try:
        accepted = persist_accepted_task_record(
            state_dir,
            semantic,
        )
    except AcceptedTaskError as exc:
        raise TaskAcceptanceError(str(exc)) from exc

    state = load_json(state_dir / "state.json", {})
    if not isinstance(state, dict):
        raise TaskAcceptanceError("durable coordinator state is malformed")
    accepted_index = state.get("accepted_tasks")
    if not isinstance(accepted_index, dict):
        accepted_index = {}
    accepted_index = dict(accepted_index)
    accepted_index[workspace["task_id"]] = {
        "task_spec_sha256": workspace["task_spec_sha256"],
        "accepted_product_sha": candidate_sha,
        "acceptance_sha256": accepted["acceptance_sha256"],
    }
    state["accepted_tasks"] = accepted_index
    state["last_accepted_task_id"] = workspace["task_id"]
    state["last_accepted_product_sha"] = candidate_sha
    state["last_acceptance_sha256"] = accepted[
        "acceptance_sha256"
    ]
    json_dump(state_dir / "state.json", state)

    updated = update_task_workspace_package_state(
        coordinator_root,
        state_dir=state_dir,
        expected_states={"PROMOTING", "VERIFIED_PENDING_PROMOTION"},
        updates={
            "lifecycle_state": "ACCEPTED_PENDING_CLEANUP",
            "accepted_product_sha": candidate_sha,
            "acceptance_sha256": accepted["acceptance_sha256"],
            "accepted_at": utcnow(),
        },
        refresh_ref_binding=True,
    )
    return {
        "status": "ACCEPTED_PENDING_CLEANUP",
        "task_id": updated["task_id"],
        "candidate_sha": candidate_sha,
        "acceptance_sha256": accepted["acceptance_sha256"],
        "accepted_record": accepted,
    }


def accept_verified_task(
    coordinator_root: Path,
    *,
    remote: str | None = None,
    remote_branch: str | None = None,
    expected_remote: str | None = None,
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
            raise TaskAcceptanceError(
                "no active TaskWorkspaceRecord exists"
            )
        if workspace["lifecycle_state"] == "ACCEPTED_PENDING_CLEANUP":
            try:
                accepted = load_accepted_task_record(
                    state_root,
                    workspace["task_id"],
                )
            except AcceptedTaskError as exc:
                raise TaskAcceptanceError(str(exc)) from exc
            return {
                "status": "ACCEPTED_PENDING_CLEANUP",
                "task_id": workspace["task_id"],
                "candidate_sha": workspace["candidate_sha"],
                "acceptance_sha256": accepted["acceptance_sha256"],
                "accepted_record": accepted,
            }

        if workspace["lifecycle_state"] not in {
            "VERIFIED_PENDING_PROMOTION",
            "PROMOTING",
        }:
            raise TaskAcceptanceError(
                "task acceptance requires VERIFIED_PENDING_PROMOTION "
                f"or PROMOTING state, found {workspace['lifecycle_state']!r}"
            )
        candidate_sha = str(workspace.get("candidate_sha") or "").lower()
        verified_sha = str(
            workspace.get("verified_candidate_sha") or ""
        ).lower()
        base_sha = str(workspace["product_base_sha"]).lower()
        if not candidate_sha or candidate_sha != verified_sha:
            raise TaskAcceptanceError(
                "task acceptance requires candidate_sha == verified_candidate_sha"
            )

        # Re-validate exact verifier/attestation evidence before any product
        # mutation, even though their digests are already bound to workspace.
        bundle = load_deterministic_verification_bundle(
            coordinator_root,
            candidate_sha=candidate_sha,
            state_dir=state_root,
        )
        verifier = load_independent_verifier_record(
            coordinator_root,
            candidate_sha=candidate_sha,
            state_dir=state_root,
        )
        if bundle.get("verdict") != "PASS" or verifier.get(
            "verdict"
        ) != "VERIFIED":
            raise TaskAcceptanceError(
                "verified promotion evidence is no longer valid"
            )
        try:
            attestation = require_exact_attestation(
                coordinator_root,
                candidate_sha,
                contract=TASK_ACCEPTANCE_CONTRACT,
            )
        except ValueError as exc:
            raise TaskAcceptanceError(str(exc)) from exc
        if _attestation_digest(attestation or {}) != workspace.get(
            "acceptance_attestation_sha256"
        ):
            raise TaskAcceptanceError(
                "exact task promotion attestation digest is stale"
            )

        current_head = _current_head(
            coordinator_root,
            state_dir=state_root,
        )
        if workspace["lifecycle_state"] == "VERIFIED_PENDING_PROMOTION":
            if current_head != base_sha:
                updated = update_task_workspace_package_state(
                    coordinator_root,
                    state_dir=state_root,
                    expected_states={"VERIFIED_PENDING_PROMOTION"},
                    updates={
                        "lifecycle_state": "STALE_BASE",
                        "promotion_last_error": (
                            "product HEAD moved before verified promotion"
                        ),
                    },
                )
                return {
                    "status": "STALE_BASE",
                    "task_id": updated["task_id"],
                    "head": current_head,
                    "base_sha": base_sha,
                    "candidate_sha": candidate_sha,
                }
            if not bool(workspace.get("no_op_candidate")):
                validate_task_candidate_worktree(
                    coordinator_root,
                    state_dir=state_root,
                )
            workspace = update_task_workspace_package_state(
                coordinator_root,
                state_dir=state_root,
                expected_states={"VERIFIED_PENDING_PROMOTION"},
                updates={
                    "lifecycle_state": "PROMOTING",
                    "promotion_started_at": utcnow(),
                },
            )

        current_head = _current_head(
            coordinator_root,
            state_dir=state_root,
        )
        if bool(workspace.get("no_op_candidate")):
            if candidate_sha != base_sha or current_head != base_sha:
                raise TaskAcceptanceError(
                    "verified no-op acceptance requires unchanged exact "
                    "product base"
                )
            # No HEAD movement occurs, but old task authority still must be
            # retired before the next task source generation is selected.
            invalidate_task_authority_after_head_change(
                coordinator_root,
                reason=(
                    "verified no-op task acceptance at "
                    + candidate_sha
                ),
                state_dir=state_root,
            )
        elif current_head == base_sha:
            try:
                promote_fast_forward(
                    coordinator_root,
                    candidate_sha,
                    attestation_contract=TASK_ACCEPTANCE_CONTRACT,
                    remote=remote,
                    remote_branch=remote_branch,
                    expected_remote=expected_remote,
                )
            except (OSError, ValueError) as exc:
                observed = _current_head(
                    coordinator_root,
                    state_dir=state_root,
                )
                if observed == base_sha:
                    update_task_workspace_package_state(
                        coordinator_root,
                        state_dir=state_root,
                        expected_states={"PROMOTING"},
                        updates={
                            "lifecycle_state": "VERIFIED_PENDING_PROMOTION",
                            "promotion_last_error": str(exc)[:1800],
                        },
                    )
                elif observed != candidate_sha:
                    update_task_workspace_package_state(
                        coordinator_root,
                        state_dir=state_root,
                        expected_states={"PROMOTING"},
                        updates={
                            "lifecycle_state": "STALE_BASE",
                            "promotion_last_error": str(exc)[:1800],
                        },
                    )
                raise TaskAcceptanceError(str(exc)) from exc
        elif current_head != candidate_sha:
            updated = update_task_workspace_package_state(
                coordinator_root,
                state_dir=state_root,
                expected_states={"PROMOTING"},
                updates={
                    "lifecycle_state": "STALE_BASE",
                    "promotion_last_error": (
                        "product HEAD differs from both base and exact candidate"
                    ),
                },
            )
            return {
                "status": "STALE_BASE",
                "task_id": updated["task_id"],
                "head": current_head,
                "base_sha": base_sha,
                "candidate_sha": candidate_sha,
            }

        return _persist_acceptance_after_exact_product(
            coordinator_root,
            workspace=workspace,
            state_dir=state_root,
        )


def cleanup_accepted_task_workspace(
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
            return {"status": "CLEAN", "active_task_id": None}
        if workspace["lifecycle_state"] != "ACCEPTED_PENDING_CLEANUP":
            raise TaskAcceptanceError(
                "accepted-task cleanup requires ACCEPTED_PENDING_CLEANUP state"
            )
        try:
            accepted = load_accepted_task_record(
                state_root,
                workspace["task_id"],
            )
        except AcceptedTaskError as exc:
            raise TaskAcceptanceError(str(exc)) from exc
        if accepted["acceptance_sha256"] != workspace.get(
            "acceptance_sha256"
        ):
            raise TaskAcceptanceError(
                "accepted workspace does not match full AcceptedTaskRecord"
            )

        worktree = Path(workspace["task_worktree"]).expanduser().resolve()
        candidate_ref = candidate_ref_for_workspace(workspace)
        current_ref = _ref_value(
            coordinator_root,
            candidate_ref,
            state_dir=state_root,
        )
        candidate_sha = str(workspace["candidate_sha"]).lower()
        if current_ref is not None and current_ref != candidate_sha:
            raise TaskAcceptanceError(
                "candidate ref moved before accepted-task cleanup"
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

        if worktree.exists():
            cp = _git(
                coordinator_root,
                "worktree",
                "remove",
                "--force",
                str(worktree),
                state_dir=state_root,
            )
            if cp.returncode != 0 and worktree.exists():
                detail = str(
                    cp.stderr or cp.stdout or "git worktree remove failed"
                )
                raise TaskAcceptanceError(detail.strip()[:1600])

        branch = str(workspace["task_branch"])
        branch_probe = _git(
            coordinator_root,
            "show-ref",
            "--verify",
            f"refs/heads/{branch}",
            state_dir=state_root,
        )
        if branch_probe.returncode == 0:
            _require_git(
                coordinator_root,
                "branch",
                "-D",
                branch,
                state_dir=state_root,
            )

        json_dump(
            _accepted_workspace_history_path(
                state_root,
                accepted["acceptance_sha256"],
            ),
            workspace,
        )
        active_path = state_root / "tasks" / "workspace-active.json"
        try:
            active_path.unlink()
        except FileNotFoundError:
            pass

        state = load_json(state_root / "state.json", {})
        if not isinstance(state, dict):
            raise TaskAcceptanceError(
                "durable coordinator state is malformed"
            )
        for key in (
            "active_task_workspace_sha256",
            "active_task_worktree",
            "active_task_branch",
            "active_task_candidate_sha",
            "active_task_verified_sha",
        ):
            state[key] = None
        state["task_workspace_lifecycle_state"] = None
        json_dump(state_root / "state.json", state)

        try:
            task_set = resolve_task_sources(
                coordinator_root,
                persist=True,
            )
        except TaskSourceError as exc:
            raise TaskAcceptanceError(str(exc)) from exc

        state = load_json(state_root / "state.json", {})
        if not isinstance(state, dict):
            raise TaskAcceptanceError(
                "durable coordinator state is malformed after task cleanup"
            )
        try:
            readiness = task_readiness(
                coordinator_root,
                task_set=task_set,
                state=state,
                state_dir=state_root,
                authority_root=coordinator_root,
            )
            frontier = ready_frontier(
                coordinator_root,
                task_set=task_set,
                state=state,
                state_dir=state_root,
                authority_root=coordinator_root,
            )
        except Exception as exc:
            raise TaskAcceptanceError(
                "unable to recompute post-acceptance task readiness: "
                + str(exc)
            ) from exc

        unresolved = [
            task_id
            for task_id, row in sorted(readiness.items())
            if row.get("status") != "ACCEPTED"
        ]
        if frontier:
            scheduler_status = "NEXT_READY"
            next_task_id: str | None = frontier[0]
        elif unresolved:
            scheduler_status = "BLOCKED"
            next_task_id = None
        else:
            scheduler_status = "COMPLETE"
            next_task_id = None

        return {
            "status": "CLEAN",
            "scheduler_status": scheduler_status,
            "accepted_task_id": accepted["task_id"],
            "accepted_product_sha": accepted[
                "accepted_product_sha"
            ],
            "acceptance_sha256": accepted["acceptance_sha256"],
            "task_source_set_sha256": task_set.get(
                "task_source_set_sha256"
            ),
            "task_count": len(task_set.get("tasks") or []),
            "ready_frontier": frontier,
            "next_task_id": next_task_id,
            "unresolved_task_ids": unresolved,
            "readiness": readiness,
        }


def validate_task_candidate_worktree(
    coordinator_root: Path,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    coordinator_root = coordinator_root.expanduser().resolve()
    state_root = (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(coordinator_root)
    )
    workspace = load_active_task_workspace(
        coordinator_root,
        state_dir=state_root,
    )
    if workspace is None:
        raise TaskAcceptanceError("no active TaskWorkspaceRecord exists")
    if workspace["lifecycle_state"] not in {
        "CANDIDATE",
        "VERIFYING",
        "VERIFIED_PENDING_PROMOTION",
    }:
        raise TaskAcceptanceError(
            "candidate worktree validation requires a sealed candidate state"
        )
    candidate_sha = str(workspace.get("candidate_sha") or "").lower()
    candidate_tree = str(workspace.get("candidate_tree_sha") or "").lower()
    if not candidate_sha or not candidate_tree:
        raise TaskAcceptanceError(
            "sealed candidate identity/tree is missing from workspace state"
        )

    task_root = Path(workspace["task_worktree"]).expanduser().resolve()
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
            "candidate worktree boundary changed: " + detail
        )
    try:
        envelope = load_active_execution_envelope(
            task_root,
            state_dir=state_root,
            authority_root=coordinator_root,
            git_state_dir=state_root,
        )
    except ExecutionEnvelopeError as exc:
        raise TaskAcceptanceError(str(exc)) from exc

    _reset_index(task_root, state_dir=state_root)
    try:
        evaluated = evaluate_active_workspace(
            task_root,
            envelope=envelope,
            state_dir=state_root,
            authority_root=coordinator_root,
            git_state_dir=state_root,
        )
        if evaluated["status"] != "VALID":
            detail = "; ".join(
                f"{row['path']}: {row['reason']}"
                for row in evaluated["violations"][:40]
            )
            raise TaskAcceptanceError(
                "candidate worktree violates its ExecutionEnvelope: "
                + detail
            )
        changed = _changed_paths(task_root, state_dir=state_root)
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
            state_dir=state_root,
        )
        try:
            validate_staged_diff(
                task_root,
                envelope=envelope,
                state_dir=state_root,
                authority_root=coordinator_root,
                git_state_dir=state_root,
            )
        except ExecutionEnvelopeError as exc:
            raise TaskAcceptanceError(str(exc)) from exc
        current_tree = str(
            _require_git(
                task_root,
                "write-tree",
                state_dir=state_root,
            ).stdout
        ).strip().lower()
    finally:
        _reset_index(task_root, state_dir=state_root)

    if current_tree != candidate_tree:
        raise TaskAcceptanceError(
            "task worktree promotable tree changed after candidate sealing"
        )
    return {
        "status": "VALID",
        "task_id": workspace["task_id"],
        "candidate_sha": candidate_sha,
        "candidate_tree_sha": candidate_tree,
        "task_workspace_sha256": workspace["task_workspace_sha256"],
    }


def _rejected_candidate_ref(
    workspace: dict[str, Any],
    candidate_sha: str,
) -> str:
    candidate_ref = candidate_ref_for_workspace(workspace)
    prefix = "refs/claude-auto/task-candidates/"
    if not candidate_ref.startswith(prefix):
        raise TaskAcceptanceError("package candidate ref namespace is malformed")
    token = candidate_ref[len(prefix):]
    digest = candidate_sha.lower()
    if not (
        40 <= len(digest) <= 64
        and all(ch in "0123456789abcdef" for ch in digest)
    ):
        raise TaskAcceptanceError("rejected candidate SHA is malformed")
    return f"refs/claude-auto/task-rejected/{token}/{digest}"


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

        rejected_ref: str | None = None
        if not bool(workspace.get("no_op_candidate")):
            candidate_ref = candidate_ref_for_workspace(workspace)
            rejected_ref = _rejected_candidate_ref(
                workspace,
                candidate_sha,
            )
            current_ref = _ref_value(
                coordinator_root,
                candidate_ref,
                state_dir=state_root,
            )
            if current_ref is not None and current_ref != candidate_sha:
                raise TaskAcceptanceError(
                    "candidate ref moved to an unexpected object before repair reopen"
                )

            archived = _ref_value(
                coordinator_root,
                rejected_ref,
                state_dir=state_root,
            )
            if archived is None:
                zero = "0" * len(candidate_sha)
                _require_git(
                    coordinator_root,
                    "update-ref",
                    rejected_ref,
                    candidate_sha,
                    zero,
                    state_dir=state_root,
                )
            elif archived != candidate_sha:
                raise TaskAcceptanceError(
                    "rejected-candidate archive ref points at an unexpected object"
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
                "last_rejected_candidate_ref": rejected_ref,
            },
            refresh_ref_binding=True,
        )
        return {
            "status": "ACTIVE",
            "task_id": updated["task_id"],
            "rejected_candidate_sha": candidate_sha,
            "task_workspace_sha256": updated["task_workspace_sha256"],
            "repair_findings": updated.get("repair_findings") or [],
            "rejected_candidate_ref": updated.get(
                "last_rejected_candidate_ref"
            ),
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
