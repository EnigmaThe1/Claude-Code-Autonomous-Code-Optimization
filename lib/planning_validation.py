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
import subprocess
from pathlib import Path
from typing import Any

from authority_set import (
    AuthoritySetError,
    authority_content_sha256,
    build_authority_snapshot,
    planning_repair_control_digest,
)
from execution import run_repository_command
from git_trust import trusted_git_env
from governance_contract import canonical_json_bytes
from planning_helpers import (
    HelperRunner,
    PlanningHelperError,
    load_reconciler_bundle,
)
from repair_envelope import (
    direct_repair_path_reason,
    generated_repair_path_reason,
)
from task_sources import TaskSourceError, resolve_task_sources


class PlanningValidationError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git_paths(
    root: Path,
    *args: str,
) -> list[str]:
    cp = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        env=trusted_git_env(root),
    )
    if cp.returncode != 0:
        detail = bytes(cp.stderr or cp.stdout or b"").decode(
            "utf-8", errors="replace"
        )
        raise PlanningValidationError(
            f"unable to inspect exact planning candidate diff: {detail[:1600]}"
        )
    out: list[str] = []
    for raw in bytes(cp.stdout).split(b"\0"):
        if not raw:
            continue
        try:
            out.append(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise PlanningValidationError(
                "planning candidate validation requires UTF-8 paths"
            ) from exc
    return sorted(set(out), key=lambda item: item.encode("utf-8"))


def _sets(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("sets")
    if not isinstance(rows, list):
        raise PlanningValidationError("authority snapshot set records are malformed")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise PlanningValidationError("authority snapshot contains malformed set")
        if row["id"] in out:
            raise PlanningValidationError("authority snapshot contains duplicate set ID")
        out[row["id"]] = row
    return out


def _member(
    sets: dict[str, dict[str, Any]],
    *,
    set_id: str,
    path: str,
) -> dict[str, Any] | None:
    current = sets.get(set_id)
    if current is None:
        return None
    rows = current.get("members")
    if not isinstance(rows, list):
        raise PlanningValidationError(
            f"AuthoritySet {set_id!r} member records are malformed"
        )
    matches = [
        row for row in rows
        if isinstance(row, dict) and row.get("path") == path
    ]
    if len(matches) > 1:
        raise PlanningValidationError(
            f"AuthoritySet {set_id!r} contains duplicate resolved member {path!r}"
        )
    return matches[0] if matches else None


def _receipt_generated_paths(
    active: dict[str, Any],
    envelope: dict[str, Any],
) -> tuple[set[str], dict[str, Any] | None]:
    digest = active.get("reconciler_receipt_bundle_sha256")
    if digest is None:
        return set(), None
    if not isinstance(digest, str) or len(digest) != 64:
        raise PlanningValidationError(
            "active repair has malformed reconciler receipt bundle digest"
        )
    raw_path = active.get("reconciler_receipt_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise PlanningValidationError(
            "active repair is missing reconciler receipt bundle path"
        )
    try:
        bundle = load_reconciler_bundle(
            Path(raw_path),
            repair_envelope_sha256=envelope["repair_envelope_sha256"],
            base_sha=envelope["base_sha"],
        )
    except PlanningHelperError as exc:
        raise PlanningValidationError(str(exc)) from exc
    if bundle.get("reconciler_receipt_bundle_sha256") != digest:
        raise PlanningValidationError(
            "active repair reconciler receipt digest does not match persisted bundle"
        )
    output_paths = {
        row["path"]
        for receipt in bundle.get("receipts", [])
        if isinstance(receipt, dict)
        for row in receipt.get("output_state", [])
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    return output_paths, bundle


def load_candidate_authority_evidence(
    path: Path,
    *,
    candidate_sha: str,
    repair_envelope_sha256: str,
) -> dict[str, Any]:
    import json

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanningValidationError(
            f"candidate authority evidence is unreadable or malformed: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PlanningValidationError(
            "candidate authority evidence must be a JSON object"
        )
    semantic_keys = (
        "schema_version",
        "base_sha",
        "candidate_sha",
        "repair_envelope_sha256",
        "base_authority_content_sha256",
        "candidate_authority_content_sha256",
        "candidate_authority_snapshot",
        "selected_authority_sets",
        "changed_paths",
        "reconciler_receipt_bundle_sha256",
        "candidate_task_sources",
    )
    missing = [key for key in semantic_keys if key not in value]
    if missing:
        raise PlanningValidationError(
            "candidate authority evidence is missing field(s): "
            + ", ".join(missing)
        )
    semantic = {key: value[key] for key in semantic_keys}
    if semantic["schema_version"] != 1:
        raise PlanningValidationError(
            "unsupported candidate authority evidence schema"
        )
    if semantic["candidate_sha"] != candidate_sha:
        raise PlanningValidationError(
            "candidate authority evidence SHA binding is stale"
        )
    if semantic["repair_envelope_sha256"] != repair_envelope_sha256:
        raise PlanningValidationError(
            "candidate authority evidence RepairEnvelope binding is stale"
        )
    if value.get("candidate_authority_evidence_sha256") != _digest(semantic):
        raise PlanningValidationError(
            "candidate authority evidence semantic integrity check failed"
        )
    return value


def validate_planning_candidate(
    root: Path,
    active: dict[str, Any],
    envelope: dict[str, Any],
    *,
    task_source_runner: HelperRunner = run_repository_command,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    base = str(active.get("base_sha") or "")
    candidate = str(active.get("candidate_sha") or "")
    if not base or not candidate:
        raise PlanningValidationError(
            "candidate AuthoritySet validation requires exact base and candidate SHAs"
        )
    if envelope.get("repair_envelope_sha256") != active.get(
        "repair_envelope_sha256"
    ):
        raise PlanningValidationError(
            "active repair and RepairEnvelope identity disagree"
        )
    if envelope.get("base_sha") != base:
        raise PlanningValidationError(
            "RepairEnvelope base and active repair base disagree"
        )

    try:
        base_snapshot = build_authority_snapshot(root, base)
        candidate_snapshot = build_authority_snapshot(root, candidate)
    except AuthoritySetError as exc:
        raise PlanningValidationError(str(exc)) from exc
    if not isinstance(base_snapshot, dict) or not isinstance(candidate_snapshot, dict):
        raise PlanningValidationError(
            "candidate planning validation requires configured AuthoritySets"
        )

    base_content = authority_content_sha256(base_snapshot)
    if base_content != envelope.get("base_authority_content_sha256"):
        raise PlanningValidationError(
            "RepairEnvelope base authority-content binding is stale"
        )
    if candidate_snapshot.get("governance_blob") != envelope.get("governance_blob"):
        raise PlanningValidationError(
            "candidate changed committed governance outside Planning Repair authority"
        )
    if candidate_snapshot.get("source_mode") != envelope.get("source_mode"):
        raise PlanningValidationError(
            "candidate changed planning authority source mode"
        )
    if (
        candidate_snapshot.get("task_source_contract_digest")
        != envelope.get("task_source_contract_digest")
    ):
        raise PlanningValidationError(
            "candidate changed TaskSource contract authority"
        )
    try:
        base_control_digest = planning_repair_control_digest(root, base)
        candidate_control_digest = planning_repair_control_digest(
            root,
            candidate,
        )
    except AuthoritySetError as exc:
        raise PlanningValidationError(str(exc)) from exc
    if candidate_control_digest != base_control_digest:
        raise PlanningValidationError(
            "candidate changed planning/control surface authority"
        )

    selected = set(envelope.get("selected_authority_sets", []))
    base_sets = _sets(base_snapshot)
    candidate_sets = _sets(candidate_snapshot)
    if set(base_sets) != set(candidate_sets):
        raise PlanningValidationError(
            "candidate changed the declared AuthoritySet ID topology"
        )
    for set_id in sorted(set(base_sets) - selected):
        if base_sets[set_id] != candidate_sets[set_id]:
            raise PlanningValidationError(
                f"unselected AuthoritySet {set_id!r} changed semantically"
            )

    for member in envelope.get("base_members", []):
        if not isinstance(member, dict) or member.get("repair") != "immutable":
            continue
        set_id = str(member.get("set_id") or "")
        path = str(member.get("path") or "")
        candidate_member = _member(
            candidate_sets,
            set_id=set_id,
            path=path,
        )
        base_member = {
            key: value
            for key, value in member.items()
            if key != "set_id"
        }
        if candidate_member != base_member:
            raise PlanningValidationError(
                f"immutable planning member changed in candidate: {set_id}:{path}"
            )

    changed_paths = _git_paths(
        root,
        "diff",
        "--name-only",
        "-z",
        base,
        candidate,
        "--",
    )
    deleted_paths = set(_git_paths(
        root,
        "diff",
        "--name-only",
        "--diff-filter=D",
        "-z",
        base,
        candidate,
        "--",
    ))
    approved_deletes = set(active.get("architect_delete_paths") or [])
    generated_receipt_paths, reconciler_bundle = _receipt_generated_paths(
        active,
        envelope,
    )

    classified: list[dict[str, str]] = []
    for rel in changed_paths:
        direct = direct_repair_path_reason(root, envelope, rel)
        if direct is None:
            if rel in deleted_paths and rel not in approved_deletes:
                raise PlanningValidationError(
                    f"repairable candidate deletion lacks Architect package approval: {rel}"
                )
            classified.append({"path": rel, "class": "repairable"})
            continue
        generated = generated_repair_path_reason(root, envelope, rel)
        if generated is None:
            if rel not in generated_receipt_paths:
                raise PlanningValidationError(
                    f"generated candidate change lacks reconciler evidence: {rel}"
                )
            classified.append({"path": rel, "class": "generated"})
            continue
        raise PlanningValidationError(
            f"candidate path {rel!r} is outside RepairEnvelope authority; "
            f"repairable denial: {direct}; generated denial: {generated}"
        )

    try:
        task_sources = resolve_task_sources(
            root,
            runner=task_source_runner,
            persist=False,
            ref=candidate,
        )
    except TaskSourceError as exc:
        raise PlanningValidationError(
            f"candidate TaskSource validation failed: {exc}"
        ) from exc

    task_graph = [
        {
            "id": row["task"]["id"],
            "depends_on": list(row["task"].get("depends_on") or []),
            "authority_sets": list(row["task"].get("authority_sets") or []),
            "task_spec_sha256": row.get("task_spec_sha256"),
        }
        for row in task_sources.get("tasks", [])
        if isinstance(row, dict) and isinstance(row.get("task"), dict)
    ]
    task_summary = {
        "status": task_sources.get("status"),
        "task_source_set_sha256": task_sources.get("task_source_set_sha256"),
        "merged_tasks_sha256": task_sources.get("merged_tasks_sha256"),
        "external_dependencies": task_sources.get("external_dependencies", []),
        "source_evidence": task_sources.get("source_evidence", []),
        "task_graph": task_graph,
    }
    candidate_content = authority_content_sha256(candidate_snapshot)
    semantic = {
        "schema_version": 1,
        "base_sha": base,
        "candidate_sha": candidate,
        "repair_envelope_sha256": envelope["repair_envelope_sha256"],
        "base_authority_content_sha256": base_content,
        "candidate_authority_content_sha256": candidate_content,
        "candidate_authority_snapshot": candidate_snapshot,
        "selected_authority_sets": sorted(selected),
        "changed_paths": classified,
        "reconciler_receipt_bundle_sha256": (
            reconciler_bundle.get("reconciler_receipt_bundle_sha256")
            if reconciler_bundle is not None
            else None
        ),
        "candidate_task_sources": task_summary,
    }
    return {
        **semantic,
        "candidate_authority_evidence_sha256": _digest(semantic),
    }
