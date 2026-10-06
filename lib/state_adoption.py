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
import stat
import subprocess
from pathlib import Path
from typing import Any

from authority_set import AuthoritySetError, build_authority_snapshot
from git_trust import trusted_git_env
from governance_contract import canonical_json_bytes
from planning_repair import load_active_repair
from repo_identity import repo_state_dir, repository_identity
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json
from task_authority import TaskAuthorityError, task_readiness
from task_sources import TaskSourceError, resolve_task_sources
from task_workspace import TaskWorkspaceError, load_active_task_workspace


MAX_ADOPTION_BYTES = 2 * 1024 * 1024


class StateAdoptionError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=trusted_git_env(root),
    )


def _resolve_commit(root: Path, value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    cp = _git(root, "rev-parse", "--verify", f"{raw}^{{commit}}")
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    return cp.stdout.strip().lower()


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    cp = _git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    return cp.returncode == 0


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    path = Path(os.path.abspath(str(path.expanduser())))
    try:
        st = path.lstat()
    except OSError as exc:
        raise StateAdoptionError(f"unable to inspect adoption document: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise StateAdoptionError(
            "adoption document must be a regular non-symlink file"
        )
    if st.st_size > MAX_ADOPTION_BYTES:
        raise StateAdoptionError(
            f"adoption document exceeds {MAX_ADOPTION_BYTES} byte limit"
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise StateAdoptionError(f"unable to read adoption document: {exc}") from exc
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StateAdoptionError("adoption document must be valid UTF-8") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateAdoptionError(
            f"adoption document is malformed JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise StateAdoptionError("adoption document must be a JSON object")
    return value, hashlib.sha256(payload).hexdigest()


def _normalise_document(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != 1:
        raise StateAdoptionError("unsupported adoption document schema")
    source_system = str(value.get("source_system") or "").strip()
    source_state_id = str(value.get("source_state_id") or "").strip()
    if not source_system or len(source_system) > 200:
        raise StateAdoptionError("adoption source_system is missing or too long")
    if not source_state_id or len(source_state_id) > 500:
        raise StateAdoptionError("adoption source_state_id is missing or too long")

    def _bounded_str(raw: Any, *, limit: int = 2000) -> str | None:
        if raw in (None, ""):
            return None
        out = str(raw).strip()
        if not out:
            return None
        if len(out) > limit:
            raise StateAdoptionError("adoption string field exceeds size limit")
        return out

    accepted_raw = value.get("accepted_tasks")
    if accepted_raw is None:
        accepted_raw = []
    if not isinstance(accepted_raw, list) or len(accepted_raw) > 10000:
        raise StateAdoptionError("accepted_tasks must be a bounded list")
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in accepted_raw:
        if not isinstance(row, dict):
            raise StateAdoptionError("accepted_tasks entries must be objects")
        task_id = _bounded_str(row.get("id"), limit=500)
        task_sha = _bounded_str(row.get("task_spec_sha256"), limit=128)
        product_sha = _bounded_str(row.get("accepted_product_sha"), limit=128)
        if not task_id or not task_sha or not product_sha:
            raise StateAdoptionError(
                "accepted task claim requires id, task_spec_sha256 and accepted_product_sha"
            )
        if task_id in seen:
            raise StateAdoptionError(f"duplicate accepted task claim: {task_id}")
        seen.add(task_id)
        accepted.append({
            "id": task_id,
            "task_spec_sha256": task_sha.lower(),
            "accepted_product_sha": product_sha.lower(),
        })

    active_raw = value.get("active_task")
    active: dict[str, Any] | None = None
    if active_raw is not None:
        if not isinstance(active_raw, dict):
            raise StateAdoptionError("active_task must be an object or null")
        active_id = _bounded_str(active_raw.get("id"), limit=500)
        active_sha = _bounded_str(
            active_raw.get("task_spec_sha256"),
            limit=128,
        )
        base_sha = _bounded_str(active_raw.get("base_sha"), limit=128)
        if not active_id or not active_sha or not base_sha:
            raise StateAdoptionError(
                "active_task requires id, task_spec_sha256 and base_sha"
            )
        active = {
            "id": active_id,
            "task_spec_sha256": active_sha.lower(),
            "base_sha": base_sha.lower(),
        }

    def _bounded_list(raw: Any, label: str) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 1000:
            raise StateAdoptionError(f"{label} must be a bounded list")
        out: list[str] = []
        for item in raw:
            value = _bounded_str(item, limit=1800)
            if value:
                out.append(value)
        return out

    return {
        "schema_version": 1,
        "source_system": source_system,
        "source_state_id": source_state_id,
        "product_sha": _bounded_str(value.get("product_sha"), limit=128),
        "verified_through_sha": _bounded_str(
            value.get("verified_through_sha"),
            limit=128,
        ),
        "authority_snapshot_sha256": _bounded_str(
            value.get("authority_snapshot_sha256"),
            limit=128,
        ),
        "active_task": active,
        "accepted_tasks": accepted,
        "blockers": _bounded_list(value.get("blockers"), "blockers"),
        "reservations": _bounded_list(value.get("reservations"), "reservations"),
    }


def _task_index(task_set: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = task_set.get("tasks")
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            isinstance(row, dict)
            and isinstance(row.get("task"), dict)
            and isinstance(row["task"].get("id"), str)
        ):
            out[row["task"]["id"]] = row
    return out


def _classify_commit_relationship(
    root: Path,
    claimed: str | None,
    current: str,
) -> dict[str, Any]:
    if not claimed:
        return {"status": "MISSING_LEGACY_EVIDENCE", "resolved_sha": None}
    resolved = _resolve_commit(root, claimed)
    if resolved is None:
        return {"status": "UNAVAILABLE", "resolved_sha": None}
    if resolved == current:
        return {"status": "MATCH", "resolved_sha": resolved}
    if _is_ancestor(root, resolved, current):
        return {"status": "ANCESTOR", "resolved_sha": resolved}
    return {"status": "DIVERGENT", "resolved_sha": resolved}


def _runtime_conflicts(root: Path) -> list[str]:
    out: list[str] = []
    try:
        workspace = load_active_task_workspace(root)
    except TaskWorkspaceError as exc:
        out.append(f"current P4 task workspace state is invalid: {exc}")
    else:
        if workspace is not None:
            out.append(
                "current P4 task workspace is active: "
                + str(workspace.get("task_id") or "<unknown>")
            )
    repair = load_active_repair(root)
    if repair:
        out.append(
            "current planning repair is active: "
            + str(repair.get("status") or "<unknown>")
        )
    return out


def _classify_accepted_claim(
    root: Path,
    row: dict[str, Any],
    *,
    task_index: dict[str, dict[str, Any]],
    current_product_sha: str,
) -> dict[str, Any]:
    task_id = row["id"]
    current = task_index.get(task_id)
    if current is None:
        return {**row, "status": "UNKNOWN_TASK_ID"}
    expected = current.get("task_spec_sha256")
    if row["task_spec_sha256"] != expected:
        return {
            **row,
            "status": "TASK_SPEC_MISMATCH",
            "current_task_spec_sha256": expected,
        }
    accepted_sha = _resolve_commit(root, row["accepted_product_sha"])
    if accepted_sha is None:
        return {**row, "status": "ACCEPTED_SHA_UNAVAILABLE"}
    if not _is_ancestor(root, accepted_sha, current_product_sha):
        return {
            **row,
            "status": "ACCEPTED_SHA_NOT_ANCESTOR",
            "resolved_accepted_product_sha": accepted_sha,
        }
    return {
        **row,
        "status": "ELIGIBLE_FOR_REATTESTATION",
        "resolved_accepted_product_sha": accepted_sha,
    }


def _classify_active_claim(
    root: Path,
    row: dict[str, Any] | None,
    *,
    task_index: dict[str, dict[str, Any]],
    current_product_sha: str,
    runtime_conflicts: list[str],
    current_readiness: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if row is None:
        return None
    if runtime_conflicts:
        return {
            **row,
            "status": "CONFLICT_CURRENT_RUNTIME",
            "conflicts": runtime_conflicts,
        }
    current = task_index.get(row["id"])
    if current is None:
        return {**row, "status": "UNKNOWN_TASK_ID"}
    expected = current.get("task_spec_sha256")
    if row["task_spec_sha256"] != expected:
        return {
            **row,
            "status": "TASK_SPEC_MISMATCH",
            "current_task_spec_sha256": expected,
        }
    base = _resolve_commit(root, row["base_sha"])
    if base is None:
        return {**row, "status": "BASE_SHA_UNAVAILABLE"}
    if base != current_product_sha:
        return {
            **row,
            "status": "STALE_BASE",
            "resolved_base_sha": base,
            "current_product_sha": current_product_sha,
        }
    readiness = current_readiness.get(row["id"], {})
    if readiness.get("status") == "ACCEPTED":
        return {
            **row,
            "status": "ALREADY_ACCEPTED_CURRENTLY",
            "resolved_base_sha": base,
        }
    if readiness.get("status") != "READY":
        return {
            **row,
            "status": "DEPENDENCY_NOT_READY",
            "resolved_base_sha": base,
            "blockers": readiness.get("blockers") or [],
        }
    return {
        **row,
        "status": "MAPPABLE_CURRENT_TASK",
        "resolved_base_sha": base,
    }


def _adoption_dir(root: Path) -> Path:
    return ensure_private_dir(repo_state_dir(root) / "adoption")


def import_legacy_state_claims(
    root: Path,
    document_path: Path,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    raw, source_file_sha256 = _read_document(document_path)
    document = _normalise_document(raw)

    try:
        snapshot = build_authority_snapshot(root)
    except AuthoritySetError as exc:
        raise StateAdoptionError(str(exc)) from exc
    current_head_cp = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if current_head_cp.returncode != 0 or not current_head_cp.stdout.strip():
        raise StateAdoptionError("adoption requires a valid current Git HEAD")
    current_head = current_head_cp.stdout.strip().lower()

    try:
        task_set = resolve_task_sources(root, persist=False)
    except TaskSourceError as exc:
        raise StateAdoptionError(str(exc)) from exc
    task_index = (
        _task_index(task_set)
        if task_set.get("status") == "READY"
        else {}
    )
    current_product_sha = str(
        task_set.get("product_head") or current_head
    ).lower()
    runtime_conflicts = _runtime_conflicts(root)
    state_path = repo_state_dir(root) / "state.json"
    state_before = load_json(state_path, {})
    if not isinstance(state_before, dict):
        raise StateAdoptionError("durable repository state is malformed")
    try:
        current_readiness = (
            task_readiness(
                root,
                task_set=task_set,
                state=state_before,
                state_dir=repo_state_dir(root),
                authority_root=root,
            )
            if task_set.get("status") == "READY"
            else {}
        )
    except TaskAuthorityError as exc:
        raise StateAdoptionError(str(exc)) from exc

    authority_claim = document.get("authority_snapshot_sha256")
    authority_status = (
        "MISSING_LEGACY_EVIDENCE"
        if not authority_claim
        else (
            "MATCH"
            if isinstance(snapshot, dict)
            and authority_claim == snapshot.get("snapshot_sha256")
            else "MISMATCH"
        )
    )
    accepted = [
        _classify_accepted_claim(
            root,
            row,
            task_index=task_index,
            current_product_sha=current_product_sha,
        )
        for row in document["accepted_tasks"]
    ]
    active = _classify_active_claim(
        root,
        document["active_task"],
        task_index=task_index,
        current_product_sha=current_product_sha,
        runtime_conflicts=runtime_conflicts,
        current_readiness=current_readiness,
    )
    product_relationship = _classify_commit_relationship(
        root,
        document.get("product_sha"),
        current_product_sha,
    )
    verified_relationship = _classify_commit_relationship(
        root,
        document.get("verified_through_sha"),
        current_product_sha,
    )

    semantic = {
        "schema_version": 1,
        "source_system": document["source_system"],
        "source_state_id": document["source_state_id"],
        "source_file_sha256": source_file_sha256,
        "repository_identity": repository_identity(root),
        "current_product_sha": current_product_sha,
        "current_authority_snapshot_sha256": (
            snapshot.get("snapshot_sha256")
            if isinstance(snapshot, dict)
            else None
        ),
        "current_task_source_set_sha256": task_set.get(
            "task_source_set_sha256"
        ),
        "legacy_product_relationship": product_relationship,
        "legacy_verified_relationship": verified_relationship,
        "legacy_authority_status": authority_status,
        "accepted_task_claims": accepted,
        "active_task_claim": active,
        "legacy_blockers": document["blockers"],
        "legacy_reservations": document["reservations"],
        "runtime_conflicts": runtime_conflicts,
    }
    adoption_sha = _digest(semantic)
    record = {
        **semantic,
        "adoption_sha256": adoption_sha,
        "imported_at": utcnow(),
    }
    adoption_dir = _adoption_dir(root)
    history = ensure_private_dir(adoption_dir / "history")
    history_path = history / f"{adoption_sha}.json"
    if history_path.exists():
        existing = load_json(history_path, {})
        if not isinstance(existing, dict) or existing.get(
            "adoption_sha256"
        ) != adoption_sha:
            raise StateAdoptionError(
                "existing adoption record conflicts with deterministic import digest"
            )
        record = existing
    else:
        json_dump(history_path, record)
    json_dump(adoption_dir / "current.json", record)

    state = load_json(state_path, {})
    if not isinstance(state, dict):
        raise StateAdoptionError("durable repository state is malformed")
    for key in (
        "accepted_tasks",
        "active_task_id",
        "active_task_spec_sha256",
        "active_execution_envelope_sha256",
    ):
        if state.get(key) != state_before.get(key):
            raise StateAdoptionError(
                "legacy adoption import observed unexpected authority-state mutation "
                f"before persistence: {key}"
            )
    # Claims remain evidence only. Do not modify accepted_tasks or any active
    # task/ExecutionEnvelope fields here.
    state["legacy_adoption"] = {
        "adoption_sha256": adoption_sha,
        "source_system": document["source_system"],
        "source_state_id": document["source_state_id"],
        "imported_at": record["imported_at"],
    }
    json_dump(state_path, state)
    return record
