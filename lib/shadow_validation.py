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
import stat
import subprocess
from pathlib import Path
from typing import Any

from accepted_task import AcceptedTaskError, load_accepted_task_record
from authority_set import AuthoritySetError, build_authority_snapshot
from environment_policy import sanitised_subprocess_env
from governance_contract import canonical_json_bytes
from repo_identity import repo_id, repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json
from task_sources import TaskSourceError, resolve_task_sources
from task_workspace import TaskWorkspaceError, load_active_task_workspace


class ShadowValidationError(ValueError):
    pass


MAX_LEGACY_OBSERVATION_BYTES = 2 * 1024 * 1024

_DIMENSIONS = (
    "product_sha",
    "authority",
    "task_source",
    "ready_frontier",
    "active_task",
    "next_task",
    "accepted_tasks",
    "blockers",
    "reservations",
    "planning_repair",
    "verification_through",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _state_root(root: Path, state_dir: Path | None = None) -> Path:
    return (
        state_dir.expanduser().resolve()
        if state_dir is not None
        else repo_state_dir(root)
    )


def _git_ancestor(
    root: Path,
    ancestor: str,
    descendant: str,
    *,
    state_dir: Path,
) -> bool:
    cp = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        text=True,
        capture_output=True,
        env=sanitised_subprocess_env(),
    )
    if cp.returncode == 0:
        return True
    if cp.returncode == 1:
        return False
    detail = (cp.stderr or cp.stdout or "git merge-base failed").strip()
    raise ShadowValidationError(detail[:1600])


def _accepted_path(state_dir: Path, task_id: str) -> Path:
    token = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:32]
    return state_dir / "tasks" / "accepted" / f"{token}.json"


def _accepted_truth(
    root: Path,
    task_rows: list[dict[str, Any]],
    state: dict[str, Any],
    product_sha: str,
    *,
    state_dir: Path,
) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    compact = state.get("accepted_tasks")
    compact_map = compact if isinstance(compact, dict) else {}
    accepted: set[str] = set()
    invalid: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []

    by_id = {
        row["task"]["id"]: row
        for row in task_rows
        if isinstance(row, dict)
        and isinstance(row.get("task"), dict)
        and isinstance(row["task"].get("id"), str)
    }
    for task_id in sorted(compact_map):
        claim = compact_map.get(task_id)
        if not isinstance(claim, dict):
            invalid.append({
                "task_id": task_id,
                "reason": "compact accepted-task claim is malformed",
            })
            continue
        claims.append({
            "task_id": task_id,
            "task_spec_sha256": claim.get("task_spec_sha256"),
            "accepted_product_sha": claim.get("accepted_product_sha"),
            "acceptance_sha256": claim.get("acceptance_sha256"),
        })
        current = by_id.get(task_id)
        if current is None:
            invalid.append({
                "task_id": task_id,
                "reason": "accepted claim is absent from current TaskSourceSet",
            })
            continue
        path = _accepted_path(state_dir, task_id)
        if not path.is_file():
            invalid.append({
                "task_id": task_id,
                "reason": "full AcceptedTaskRecord is missing",
            })
            continue
        try:
            record = load_accepted_task_record(state_dir, task_id)
        except AcceptedTaskError as exc:
            invalid.append({"task_id": task_id, "reason": str(exc)})
            continue
        if record.get("acceptance_sha256") != claim.get("acceptance_sha256"):
            invalid.append({
                "task_id": task_id,
                "reason": "compact/full acceptance digest mismatch",
            })
            continue
        if record.get("task_spec_sha256") != current.get("task_spec_sha256"):
            invalid.append({
                "task_id": task_id,
                "reason": "accepted TaskSpec digest is stale",
            })
            continue
        if claim.get("task_spec_sha256") != record.get("task_spec_sha256"):
            invalid.append({
                "task_id": task_id,
                "reason": "compact/full TaskSpec digest mismatch",
            })
            continue
        if claim.get("accepted_product_sha") != record.get(
            "accepted_product_sha"
        ):
            invalid.append({
                "task_id": task_id,
                "reason": "compact/full accepted product SHA mismatch",
            })
            continue
        accepted_sha = str(record.get("accepted_product_sha") or "")
        if not accepted_sha:
            invalid.append({
                "task_id": task_id,
                "reason": "accepted product SHA is missing",
            })
            continue
        try:
            if not _git_ancestor(
                root,
                accepted_sha,
                product_sha,
                state_dir=state_dir,
            ):
                invalid.append({
                    "task_id": task_id,
                    "reason": "accepted product SHA is not an ancestor of product",
                })
                continue
        except ShadowValidationError as exc:
            invalid.append({"task_id": task_id, "reason": str(exc)})
            continue
        accepted.add(task_id)

    return accepted, claims, invalid


def _task_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict) or not isinstance(row.get("task"), dict):
        return None
    task = row["task"]
    return {
        "id": task.get("id"),
        "task_spec_sha256": row.get("task_spec_sha256"),
        "depends_on": list(task.get("depends_on") or []),
        "owned_paths": list(task.get("owned_paths") or []),
        "evidence_paths": list(task.get("evidence_paths") or []),
        "runtime_scratch_paths": list(
            task.get("runtime_scratch_paths") or []
        ),
        "verification_claims": list(task.get("verification") or []),
    }


def _normalise_authority(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {
            "status": "UNCONFIGURED",
            "snapshot_sha256": None,
            "set_ids": [],
            "sets": [],
        }
    sets: list[dict[str, Any]] = []
    for raw in snapshot.get("sets") or []:
        if not isinstance(raw, dict):
            continue
        members = [
            {
                "path": item.get("path"),
                "role": item.get("role"),
                "repair": item.get("repair"),
            }
            for item in (raw.get("members") or [])
            if isinstance(item, dict)
        ]
        semantic = {
            "id": raw.get("id"),
            "members": members,
            "validators": raw.get("validators") or [],
            "reconcilers": raw.get("reconcilers") or [],
        }
        sets.append({
            **semantic,
            "authority_set_sha256": _digest(semantic),
        })
    sets.sort(key=lambda item: str(item.get("id")).encode("utf-8"))
    return {
        "status": "READY",
        "snapshot_sha256": snapshot.get("snapshot_sha256"),
        "source_mode": snapshot.get("source_mode"),
        "set_ids": [item.get("id") for item in sets],
        "sets": sets,
        "control_surface_digest": snapshot.get("control_surface_digest"),
        "protected_paths": list(snapshot.get("protected_paths") or []),
    }


def _read_repair_state(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / "planning-repair" / "active.json"
    if not path.is_file():
        return None
    obj = load_json(path, {})
    if not isinstance(obj, dict) or not obj:
        return None
    return {
        "schema_version": obj.get("schema_version"),
        "status": obj.get("status"),
        "base_sha": obj.get("base_sha"),
        "candidate_sha": obj.get("candidate_sha"),
        "verified_sha": obj.get("verified_sha"),
        "repair_envelope_sha256": obj.get("repair_envelope_sha256"),
        "selected_authority_sets": list(
            obj.get("selected_authority_sets") or []
        ),
    }


def compute_rc4_shadow_observation(
    root: Path,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state_root = _state_root(root, state_dir)
    state = load_json(state_root / "state.json", {})
    if not isinstance(state, dict):
        state = {}

    try:
        snapshot = build_authority_snapshot(root)
    except AuthoritySetError as exc:
        raise ShadowValidationError(str(exc)) from exc
    try:
        task_set = resolve_task_sources(
            root,
            persist=False,
            git_env=sanitised_subprocess_env(),
        )
    except TaskSourceError as exc:
        raise ShadowValidationError(str(exc)) from exc

    product_sha = (
        str(snapshot.get("product_head"))
        if isinstance(snapshot, dict)
        else subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            text=True,
            capture_output=True,
            env=sanitised_subprocess_env(),
            check=True,
        ).stdout.strip().lower()
    )
    task_rows = (
        list(task_set.get("tasks") or [])
        if task_set.get("status") == "READY"
        else []
    )
    task_index = {
        row["task"]["id"]: row
        for row in task_rows
        if isinstance(row, dict)
        and isinstance(row.get("task"), dict)
        and isinstance(row["task"].get("id"), str)
    }

    accepted, accepted_claims, invalid_acceptance = _accepted_truth(
        root,
        task_rows,
        state,
        product_sha,
        state_dir=state_root,
    )

    blockers: list[dict[str, Any]] = []
    ready: list[str] = []
    for task_id in sorted(task_index):
        if task_id in accepted:
            continue
        task = task_index[task_id]["task"]
        task_blockers: list[dict[str, str]] = []
        for dep in task.get("depends_on") or []:
            if dep in accepted:
                continue
            if dep in task_index:
                reason = "local dependency is not accepted"
            else:
                reason = "external dependency has no validated acceptance evidence"
            task_blockers.append({
                "dependency": str(dep),
                "reason": reason,
            })
        if task_blockers:
            blockers.append({
                "task_id": task_id,
                "blockers": task_blockers,
            })
        else:
            ready.append(task_id)
    blockers.extend({
        "task_id": item["task_id"],
        "blockers": [{"dependency": item["task_id"], "reason": item["reason"]}],
    } for item in invalid_acceptance)

    workspace: dict[str, Any] | None
    try:
        workspace = load_active_task_workspace(root, state_dir=state_root)
    except TaskWorkspaceError as exc:
        raise ShadowValidationError(str(exc)) from exc
    active_id = (
        str(workspace.get("task_id"))
        if isinstance(workspace, dict) and workspace.get("task_id")
        else (
            str(state.get("active_task_id"))
            if state.get("active_task_id")
            else None
        )
    )
    active_row = task_index.get(active_id) if active_id else None
    next_id = active_id or (ready[0] if ready else None)
    next_row = task_index.get(next_id) if next_id else None

    task_source_view = {
        "status": task_set.get("status"),
        "task_source_set_sha256": task_set.get("task_source_set_sha256"),
        "merged_tasks_sha256": task_set.get("merged_tasks_sha256"),
        "strict_dependencies": task_set.get("strict_dependencies"),
        "task_graph": [
            {
                "id": row["task"]["id"],
                "task_spec_sha256": row.get("task_spec_sha256"),
                "depends_on": list(row["task"].get("depends_on") or []),
            }
            for row in task_rows
            if isinstance(row, dict) and isinstance(row.get("task"), dict)
        ],
        "external_dependencies": list(
            task_set.get("external_dependencies") or []
        ),
    }

    active_view = _task_view(active_row)
    if active_view is not None and workspace is not None:
        active_view = {
            **active_view,
            "lifecycle_state": workspace.get("lifecycle_state"),
            "task_workspace_sha256": workspace.get(
                "task_workspace_sha256"
            ),
            "execution_envelope_sha256": workspace.get(
                "execution_envelope_sha256"
            ),
            "candidate_sha": workspace.get("candidate_sha"),
        }

    observation = {
        "schema_version": 1,
        "repository_id": repo_id(root),
        "product_sha": product_sha,
        "authority": _normalise_authority(snapshot),
        "task_source": task_source_view,
        "ready_frontier": ready,
        "active_task": active_view,
        "next_task": _task_view(next_row),
        "accepted_tasks": {
            "validated_ids": sorted(accepted),
            "persisted_claims": accepted_claims,
            "invalid_claims": invalid_acceptance,
        },
        "blockers": blockers,
        "reservations": [],
        "planning_repair": _read_repair_state(state_root),
        "verification_through": (
            {
                "candidate_sha": workspace.get("candidate_sha"),
                "verified_candidate_sha": workspace.get(
                    "verified_candidate_sha"
                ),
                "verification_bundle_sha256": workspace.get(
                    "verification_bundle_sha256"
                ),
                "acceptance_attestation_sha256": workspace.get(
                    "acceptance_attestation_sha256"
                ),
            }
            if isinstance(workspace, dict)
            else None
        ),
        "persisted_active_state": {
            "active_task_id": state.get("active_task_id"),
            "active_task_spec_sha256": state.get(
                "active_task_spec_sha256"
            ),
            "active_execution_envelope_sha256": state.get(
                "active_execution_envelope_sha256"
            ),
            "task_source_sha256": state.get("task_source_sha256"),
        },
        "imported_legacy_claims": state.get("legacy_adoption"),
    }
    semantic = {
        key: observation[key]
        for key in (
            "schema_version",
            "repository_id",
            *_DIMENSIONS,
            "persisted_active_state",
            "imported_legacy_claims",
        )
    }
    observation["observation_sha256"] = _digest(semantic)
    return observation


def load_legacy_shadow_observation(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    try:
        st = path.lstat()
    except OSError as exc:
        raise ShadowValidationError(
            f"unable to inspect legacy shadow observation: {exc}"
        ) from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ShadowValidationError(
            "legacy shadow observation must be a regular non-symlink file"
        )
    if st.st_size > MAX_LEGACY_OBSERVATION_BYTES:
        raise ShadowValidationError(
            "legacy shadow observation exceeds 2 MiB limit"
        )
    try:
        raw = path.read_bytes()
        obj = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShadowValidationError(
            f"legacy shadow observation is unreadable or malformed: {exc}"
        ) from exc
    if not isinstance(obj, dict):
        raise ShadowValidationError(
            "legacy shadow observation must be a JSON object"
        )
    if obj.get("schema_version") not in (None, 1):
        raise ShadowValidationError(
            "unsupported legacy ShadowObservation schema version"
        )
    return obj


def compare_shadow_observations(
    rc4: dict[str, Any],
    legacy: dict[str, Any],
    *,
    dispositions: dict[str, str] | None = None,
) -> dict[str, Any]:
    disposition_map = {
        str(key): str(value)[:2000]
        for key, value in (dispositions or {}).items()
        if str(key) and str(value).strip()
    }
    results: list[dict[str, Any]] = []
    for dimension in _DIMENSIONS:
        rc4_present = dimension in rc4
        legacy_present = dimension in legacy
        if not legacy_present:
            classification = "MISSING_LEGACY_EVIDENCE"
        elif not rc4_present:
            classification = "MISSING_RC4_EVIDENCE"
        elif canonical_json_bytes(rc4[dimension]) == canonical_json_bytes(
            legacy[dimension]
        ):
            classification = "MATCH"
        else:
            classification = "MISMATCH"
        results.append({
            "dimension": dimension,
            "classification": classification,
            "rc4_sha256": (
                _digest(rc4[dimension]) if rc4_present else None
            ),
            "legacy_sha256": (
                _digest(legacy[dimension]) if legacy_present else None
            ),
            "disposition": disposition_map.get(dimension),
        })
    mismatch_count = sum(
        1 for row in results if row["classification"] == "MISMATCH"
    )
    evidence_gap_count = sum(
        1
        for row in results
        if row["classification"]
        in {"MISSING_LEGACY_EVIDENCE", "MISSING_RC4_EVIDENCE"}
    )
    unresolved = [
        row["dimension"]
        for row in results
        if row["classification"] == "MISMATCH"
        and not row.get("disposition")
    ]
    return {
        "schema_version": 1,
        "results": results,
        "mismatch_count": mismatch_count,
        "evidence_gap_count": evidence_gap_count,
        "unresolved_mismatch_count": len(unresolved),
        "unresolved_mismatches": unresolved,
    }


def _load_dispositions(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    obj = load_legacy_shadow_observation(path)
    return {
        str(key): str(value)
        for key, value in obj.items()
        if isinstance(value, str) and value.strip()
    }


def persist_shadow_audit(
    root: Path,
    rc4: dict[str, Any],
    legacy: dict[str, Any],
    comparison: dict[str, Any],
    *,
    state_dir: Path | None = None,
    dispositions: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    state_root = _state_root(root, state_dir)
    core = {
        "schema_version": 1,
        "repository_id": repo_id(root),
        "product_sha": rc4.get("product_sha"),
        "rc4_observation_sha256": rc4.get("observation_sha256")
        or _digest(rc4),
        "legacy_observation_sha256": _digest(legacy),
        "comparison": comparison,
        "reviewed_dispositions": dict(dispositions or {}),
        "recorded_at": utcnow(),
    }
    record = {
        **core,
        "shadow_audit_sha256": _digest(core),
    }
    path = (
        ensure_private_dir(state_root / "shadow" / "audits")
        / f"{record['shadow_audit_sha256']}.json"
    )
    if path.exists():
        existing = load_json(path, {})
        if existing != record:
            raise ShadowValidationError(
                "shadow audit digest collision with different record"
            )
    else:
        json_dump(path, record)
    return {
        **record,
        "audit_path": str(path),
    }


def shadow_snapshot(root: Path) -> dict[str, Any]:
    return compute_rc4_shadow_observation(root)


def shadow_compare(
    root: Path,
    legacy_path: Path,
    *,
    dispositions_path: Path | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    rc4 = compute_rc4_shadow_observation(root)
    legacy = load_legacy_shadow_observation(legacy_path)
    dispositions = _load_dispositions(dispositions_path)
    comparison = compare_shadow_observations(
        rc4,
        legacy,
        dispositions=dispositions,
    )
    audit = persist_shadow_audit(
        root,
        rc4,
        legacy,
        comparison,
        dispositions=dispositions,
    )
    return {
        "status": (
            "INCOMPLETE"
            if comparison["evidence_gap_count"] > 0
            else (
                "MATCH"
                if comparison["mismatch_count"] == 0
                else (
                    "REVIEWED"
                    if comparison["unresolved_mismatch_count"] == 0
                    else "MISMATCH"
                )
            )
        ),
        "rc4": rc4,
        "comparison": comparison,
        "audit": audit,
    }


def shadow_action(args: Any, *, find_repo_root) -> int:
    root = find_repo_root(getattr(args, "repo", None))
    try:
        if args.shadow_command == "snapshot":
            result = shadow_snapshot(root)
        elif args.shadow_command == "compare":
            result = shadow_compare(
                root,
                Path(args.legacy),
                dispositions_path=(
                    Path(args.dispositions)
                    if getattr(args, "dispositions", None)
                    else None
                ),
            )
        else:
            raise ShadowValidationError(
                f"unsupported shadow command: {args.shadow_command}"
            )
    except (ShadowValidationError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "repository": str(root),
            "error": str(exc),
        }, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") in {"MISMATCH", "INCOMPLETE"} else 0
