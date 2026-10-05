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
from pathlib import Path
from typing import Any

from authority_set import (
    AuthoritySetError,
    authority_content_sha256,
    build_authority_snapshot,
    planning_repair_control_paths,
    planning_repair_control_selectors,
)
from governance_contract import (
    GovernanceContractError,
    canonical_json_bytes,
    load_governance_contract,
    normalise_repo_selector,
)
from task_spec import selector_matches_path
from repo_identity import repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump


class RepairEnvelopeError(ValueError):
    pass


_SEMANTIC_KEYS = (
    "schema_version",
    "base_sha",
    "product_branch",
    "coordinator_authority_snapshot_sha256",
    "base_authority_content_sha256",
    "governance_blob",
    "source_mode",
    "selected_authority_sets",
    "repair_reason_sha256",
    "base_members",
    "repairable_paths",
    "immutable_paths",
    "generated_paths",
    "repairable_selectors",
    "immutable_selectors",
    "generated_selectors",
    "allowed_new_repairable_selectors",
    "allowed_new_generated_selectors",
    "validator_contracts",
    "reconciler_contracts",
    "task_source_contract_digest",
    "protected_control_paths",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reason_digest(reason: str) -> str:
    return hashlib.sha256(str(reason).strip().encode("utf-8")).hexdigest()


def _path(root: Path) -> Path:
    return repo_state_dir(root.expanduser().resolve()) / "planning-repair" / "repair-envelope.json"


def _has_pattern(selector: str) -> bool:
    return "*" in selector or "?" in selector


def _semantic(record: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in _SEMANTIC_KEYS if key not in record]
    if missing:
        raise RepairEnvelopeError(
            "RepairEnvelope is missing semantic field(s): " + ", ".join(missing)
        )
    if record.get("schema_version") != 1:
        raise RepairEnvelopeError("unsupported RepairEnvelope schema")
    return {key: record[key] for key in _SEMANTIC_KEYS}


def _selected_ids(
    snapshot: dict[str, Any],
    requested: list[str] | tuple[str, ...] | None,
) -> list[str]:
    known = [item["id"] for item in snapshot.get("sets", []) if isinstance(item, dict)]
    known_set = set(known)
    if requested is None:
        if len(known) != 1:
            raise RepairEnvelopeError(
                "planning repair AuthoritySet selection is ambiguous; "
                "explicit package/operator selection is required"
            )
        return known
    values = [str(item).strip() for item in requested]
    if not values or any(not item for item in values):
        raise RepairEnvelopeError("selected AuthoritySet list must not be empty")
    if len(values) != len(set(values)):
        raise RepairEnvelopeError("selected AuthoritySet list contains duplicates")
    unknown = sorted(set(values) - known_set)
    if unknown:
        raise RepairEnvelopeError(
            "unknown selected AuthoritySet(s): " + ", ".join(unknown)
        )
    return sorted(values)


def _selector_record(set_id: str, member: dict[str, Any]) -> dict[str, Any]:
    return {
        "set_id": set_id,
        "path": member["path"],
        "role": member["role"],
        "repair": member["repair"],
        "required": bool(member["required"]),
    }


def derive_repair_envelope(
    root: Path,
    *,
    reason: str,
    authority_sets: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    try:
        snapshot = build_authority_snapshot(root)
        contract = load_governance_contract(root)
    except (AuthoritySetError, GovernanceContractError) as exc:
        raise RepairEnvelopeError(str(exc)) from exc
    if not isinstance(snapshot, dict):
        raise RepairEnvelopeError("repository planning authority is not configured")
    branch = snapshot.get("branch")
    if not isinstance(branch, str) or not branch:
        raise RepairEnvelopeError(
            "planning repair requires a named coordinator product branch"
        )

    selected = _selected_ids(snapshot, authority_sets)
    selected_set = set(selected)
    snapshot_sets = {
        item["id"]: item
        for item in snapshot.get("sets", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    base_members: list[dict[str, Any]] = []
    by_repair: dict[str, set[str]] = {
        "repairable": set(),
        "immutable": set(),
        "generated": set(),
    }
    for set_id in selected:
        current = snapshot_sets[set_id]
        for member in current.get("members", []):
            row = {"set_id": set_id, **dict(member)}
            base_members.append(row)
            repair = str(member.get("repair"))
            if repair not in by_repair:
                raise RepairEnvelopeError(
                    f"AuthoritySet {set_id!r} contains unsupported repair class {repair!r}"
                )
            by_repair[repair].add(str(member["path"]))

    declared: dict[str, list[dict[str, Any]]] = {
        "repairable": [],
        "immutable": [],
        "generated": [],
    }
    validators: list[dict[str, Any]] = []
    reconcilers: list[dict[str, Any]] = []

    if contract is not None:
        contract_sets = {
            item["id"]: item
            for item in contract["planning_authority"]["sets"]
        }
        for set_id in selected:
            current = contract_sets.get(set_id)
            if current is None:
                raise RepairEnvelopeError(
                    f"selected AuthoritySet {set_id!r} is absent from committed governance"
                )
            for member in current["members"]:
                repair = str(member["repair"])
                declared[repair].append(_selector_record(set_id, member))
            validators.extend(
                {"set_id": set_id, "helper": dict(helper)}
                for helper in current["validators"]
            )
            reconcilers.extend(
                {"set_id": set_id, "helper": dict(helper)}
                for helper in current["reconcilers"]
            )
        governance_blob = snapshot.get("governance_blob")
    else:
        # P1 synthetic legacy AuthoritySet: exact existing paths only, no helper
        # or growth authority exists outside that one-file declaration.
        for set_id in selected:
            for member in snapshot_sets[set_id].get("members", []):
                declared[str(member["repair"])].append({
                    "set_id": set_id,
                    "path": member["path"],
                    "role": member["role"],
                    "repair": member["repair"],
                    "required": bool(member["required"]),
                })
        governance_blob = None

    try:
        controls = planning_repair_control_paths(root)
    except AuthoritySetError as exc:
        raise RepairEnvelopeError(str(exc)) from exc

    semantic = {
        "schema_version": 1,
        "base_sha": snapshot["product_head"],
        "product_branch": branch,
        "coordinator_authority_snapshot_sha256": snapshot["snapshot_sha256"],
        "base_authority_content_sha256": authority_content_sha256(snapshot),
        "governance_blob": governance_blob,
        "source_mode": snapshot["source_mode"],
        "selected_authority_sets": selected,
        "repair_reason_sha256": _reason_digest(reason),
        "base_members": sorted(
            base_members,
            key=lambda item: (item["set_id"], item["path"]),
        ),
        "repairable_paths": sorted(by_repair["repairable"]),
        "immutable_paths": sorted(by_repair["immutable"]),
        "generated_paths": sorted(by_repair["generated"]),
        "repairable_selectors": sorted(
            declared["repairable"],
            key=lambda item: (item["set_id"], item["path"]),
        ),
        "immutable_selectors": sorted(
            declared["immutable"],
            key=lambda item: (item["set_id"], item["path"]),
        ),
        "generated_selectors": sorted(
            declared["generated"],
            key=lambda item: (item["set_id"], item["path"]),
        ),
        "allowed_new_repairable_selectors": sorted(
            {
                item["path"]
                for item in declared["repairable"]
                if _has_pattern(str(item["path"]))
            }
        ),
        "allowed_new_generated_selectors": sorted(
            {
                item["path"]
                for item in declared["generated"]
                if _has_pattern(str(item["path"]))
            }
        ),
        "validator_contracts": sorted(
            validators,
            key=lambda item: (item["set_id"], item["helper"]["id"]),
        ),
        "reconciler_contracts": sorted(
            reconcilers,
            key=lambda item: (item["set_id"], item["helper"]["id"]),
        ),
        "task_source_contract_digest": snapshot["task_source_contract_digest"],
        "protected_control_paths": controls,
    }
    return {
        **semantic,
        "repair_envelope_sha256": _digest(semantic),
        "repair_reason": str(reason).strip(),
        "created_at": utcnow(),
    }


def persist_repair_envelope(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    root = root.expanduser().resolve()
    semantic = _semantic(record)
    actual = _digest(semantic)
    recorded = record.get("repair_envelope_sha256")
    if recorded is not None and recorded != actual:
        raise RepairEnvelopeError(
            "RepairEnvelope semantic digest does not match its content"
        )
    persisted = {
        **record,
        "repair_envelope_sha256": actual,
    }
    path = _path(root)
    ensure_private_dir(path.parent)
    json_dump(path, persisted)
    return persisted


def load_repair_envelope_file(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairEnvelopeError(
            f"RepairEnvelope is unreadable or malformed: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise RepairEnvelopeError("RepairEnvelope must be a JSON object")
    actual = _digest(_semantic(value))
    recorded = value.get("repair_envelope_sha256")
    if not isinstance(recorded, str) or len(recorded) != 64 or recorded != actual:
        raise RepairEnvelopeError("RepairEnvelope semantic integrity check failed")
    return value


def direct_repair_path_reason(
    root: Path,
    envelope: dict[str, Any],
    rel_path: str,
) -> str | None:
    """Return a fail-closed denial reason for one Architect direct-write path."""
    root = root.expanduser().resolve()
    _semantic(envelope)
    raw = str(rel_path)
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or path == Path(".")
    ):
        return "planning repair target must be a repository-relative file path"
    try:
        rel = normalise_repo_selector(path.as_posix())
    except GovernanceContractError as exc:
        return str(exc)

    try:
        contract = load_governance_contract(root)
    except GovernanceContractError as exc:
        return f"committed governance could not be verified: {exc}"

    governance_blob = envelope.get("governance_blob")
    if governance_blob is None:
        if contract is not None:
            return "legacy RepairEnvelope no longer matches repository governance"
        return (
            None
            if rel in set(envelope.get("repairable_paths", []))
            else "path is outside the legacy one-file RepairEnvelope"
        )

    if contract is None or contract.get("_git", {}).get("blob") != governance_blob:
        return "committed governance no longer matches the RepairEnvelope"

    try:
        control_selectors = planning_repair_control_selectors(root)
    except AuthoritySetError as exc:
        return f"planning control authority could not be verified: {exc}"
    if any(selector_matches_path(selector, rel) for selector in control_selectors):
        return "path is planning governance/control/executable state"

    selected = set(envelope.get("selected_authority_sets", []))
    matches: list[tuple[str, dict[str, Any]]] = []
    for authority_set in contract["planning_authority"]["sets"]:
        set_id = authority_set["id"]
        for member in authority_set["members"]:
            if selector_matches_path(member["path"], rel):
                matches.append((set_id, member))

    if not matches:
        return "path matches no committed planning-authority declaration"
    for set_id, member in matches:
        if set_id not in selected:
            return f"path also belongs to unselected AuthoritySet {set_id!r}"
        if member["repair"] != "repairable":
            return (
                f"path is declared {member['repair']!r} in selected "
                f"AuthoritySet {set_id!r}"
            )

    base_paths = set(envelope.get("repairable_paths", []))
    if rel in base_paths:
        return None
    allowed_new = envelope.get("allowed_new_repairable_selectors", [])
    if any(selector_matches_path(selector, rel) for selector in allowed_new):
        return None
    return "new planning path is not admitted by a selected repairable pattern"


def generated_repair_path_reason(
    root: Path,
    envelope: dict[str, Any],
    rel_path: str,
    *,
    set_id: str | None = None,
) -> str | None:
    """Return a fail-closed denial reason for one package reconciler output path."""
    root = root.expanduser().resolve()
    _semantic(envelope)
    raw = str(rel_path)
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or path == Path(".")
    ):
        return "generated planning target must be a repository-relative file path"
    try:
        rel = normalise_repo_selector(path.as_posix())
    except GovernanceContractError as exc:
        return str(exc)

    try:
        contract = load_governance_contract(root)
    except GovernanceContractError as exc:
        return f"committed governance could not be verified: {exc}"

    governance_blob = envelope.get("governance_blob")
    if governance_blob is None:
        return "legacy one-file RepairEnvelope has no generated-member authority"
    if contract is None or contract.get("_git", {}).get("blob") != governance_blob:
        return "committed governance no longer matches the RepairEnvelope"

    try:
        control_selectors = planning_repair_control_selectors(root)
    except AuthoritySetError as exc:
        return f"planning control authority could not be verified: {exc}"
    if any(selector_matches_path(selector, rel) for selector in control_selectors):
        return "path is planning governance/control/executable state"

    selected = set(envelope.get("selected_authority_sets", []))
    matches: list[tuple[str, dict[str, Any]]] = []
    for authority_set in contract["planning_authority"]["sets"]:
        current_set_id = authority_set["id"]
        for member in authority_set["members"]:
            if selector_matches_path(member["path"], rel):
                matches.append((current_set_id, member))

    if not matches:
        return "path matches no committed planning-authority declaration"
    for current_set_id, member in matches:
        if current_set_id not in selected:
            return f"path also belongs to unselected AuthoritySet {current_set_id!r}"
        if member["repair"] != "generated":
            return (
                f"path is declared {member['repair']!r} in selected "
                f"AuthoritySet {current_set_id!r}"
            )
    if set_id is not None and not any(
        current_set_id == set_id and member["repair"] == "generated"
        for current_set_id, member in matches
    ):
        return f"path is not generated authority owned by reconciler AuthoritySet {set_id!r}"

    base_paths = set(envelope.get("generated_paths", []))
    if rel in base_paths:
        return None
    allowed_new = envelope.get("allowed_new_generated_selectors", [])
    if any(selector_matches_path(selector, rel) for selector in allowed_new):
        return None
    return "new generated planning path is not admitted by a selected generated pattern"


def load_repair_envelope(root: Path) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    path = _path(root)
    if not path.exists():
        return None
    return load_repair_envelope_file(path)
