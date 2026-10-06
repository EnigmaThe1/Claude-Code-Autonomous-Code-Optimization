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
from typing import Any, Callable

from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json


CURRENT_STATE_SCHEMA = 10


class StateMigrationError(ValueError):
    pass


_COMPLETION_EVIDENCE_FIELDS = (
    "last_verification_receipts",
    "last_verification_findings",
    "last_runtime_verify_verdict",
    "last_runtime_verify_summary",
    "last_runtime_verify_findings",
    "last_correctness_review_verdict",
    "last_correctness_review_findings",
    "last_security_scanner_receipts",
    "last_security_scanner_summary",
    "last_security_review_verdict",
    "last_security_review_summary",
    "last_security_review_findings",
    "last_challenger_verdict",
    "last_challenger_findings",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _state_file_digest(path: Path) -> str | None:
    try:
        return _sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateMigrationError(f"unable to hash durable state: {exc}") from exc


def _migration_dir(state_dir: Path) -> Path:
    return ensure_private_dir(state_dir / "migrations")


def _active_path(state_dir: Path) -> Path:
    return state_dir / "migrations" / "active.json"


def _history_dir(state_dir: Path) -> Path:
    return ensure_private_dir(state_dir / "migrations" / "history")


def _schema_version(state: dict[str, Any]) -> int:
    raw = state.get("schema_version", 1)
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise StateMigrationError("durable state schema_version is not an integer") from exc
    if version < 1:
        raise StateMigrationError("durable state schema_version must be >= 1")
    return version


def preflight_state_schema(state_dir: Path) -> int | None:
    path = state_dir / "state.json"
    if not path.exists():
        return None
    state = load_json(path, {})
    if not isinstance(state, dict):
        raise StateMigrationError("durable state must be a JSON object")
    version = _schema_version(state)
    if version > CURRENT_STATE_SCHEMA:
        raise StateMigrationError(
            f"durable state schema {version} is newer than supported schema "
            f"{CURRENT_STATE_SCHEMA}; upgrade Claude Auto before writing this state"
        )
    return version


def _ensure_list(state: dict[str, Any], key: str) -> None:
    value = state.get(key)
    if value is None:
        state[key] = []
    elif not isinstance(value, list):
        raise StateMigrationError(f"legacy state field {key!r} must be a list")


def _ensure_dict(state: dict[str, Any], key: str) -> None:
    value = state.get(key)
    if value is None:
        state[key] = {}
    elif not isinstance(value, dict):
        raise StateMigrationError(f"legacy state field {key!r} must be an object")


def _v1_to_v2(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("objective", None)
    state.setdefault("status", "READY")
    state.setdefault("cycle", 0)
    state["schema_version"] = 2
    return state


def _v2_to_v3(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("last_git_head", None)
    state.setdefault("last_summary", None)
    state.setdefault("last_result_status", None)
    state.setdefault("blocker", None)
    state["schema_version"] = 3
    return state


def _v3_to_v4(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("plan_version", 0)
    state.setdefault("plan_status", None)
    state.setdefault("last_session_id", None)
    state["schema_version"] = 4
    return state


def _v4_to_v5(state: dict[str, Any]) -> dict[str, Any]:
    _ensure_list(state, "permission_grants")
    _ensure_list(state, "permission_decisions")
    _ensure_list(state, "permission_requests")
    state["schema_version"] = 5
    return state


def _v5_to_v6(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("pending_permission_request", None)
    state.setdefault("active_permission_objective_hash", None)
    state["schema_version"] = 6
    return state


def _v6_to_v7(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("autonomy_profile", state.get("autonomy_profile") or "balanced")
    state.setdefault("memory_mode", state.get("memory_mode") or "external")
    state["schema_version"] = 7
    return state


def _v7_to_v8(state: dict[str, Any]) -> dict[str, Any]:
    # RC3's durable contract. Repository identity/root/id are refreshed by
    # activate() from current Git truth after migration, so legacy values are
    # preserved here only as evidence.
    state.setdefault("pending_permission_request", None)
    _ensure_list(state, "permission_grants")
    _ensure_list(state, "permission_decisions")
    _ensure_list(state, "permission_requests")
    state.setdefault("active_permission_objective_hash", None)
    state["schema_version"] = 8
    return state


def _invalidate_pre_governance_completion(state: dict[str, Any]) -> None:
    for key in _COMPLETION_EVIDENCE_FIELDS:
        if key in state:
            state[key] = None
    for key in (
        "completion_anchor",
        "completion_anchor_hash",
        "completion_anchor_verified_at",
    ):
        if key in state:
            state[key] = None
    state["completion_anchor_invalidated_reason"] = (
        "state migrated into RC4 governance/task authority; completion evidence "
        "requires current-authority revalidation"
    )


def _v8_to_v9(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("governance_snapshot_sha256", None)
    state.setdefault("governance_snapshot_generation", 0)
    state.setdefault("task_source_sha256", None)
    state.setdefault("active_task_id", None)
    state.setdefault("active_task_spec_sha256", None)
    state.setdefault("active_execution_envelope_sha256", None)
    _ensure_dict(state, "accepted_tasks")
    state.setdefault("governance_blocker", None)
    _invalidate_pre_governance_completion(state)
    state["schema_version"] = 9
    return state


def _v9_to_v10(state: dict[str, Any]) -> dict[str, Any]:
    _ensure_dict(state, "accepted_tasks")
    state.setdefault("state_schema_migration", None)
    state.setdefault("legacy_adoption", None)
    state.setdefault("session_adoption", None)
    state.setdefault("shadow_validation", None)
    state.setdefault("migration_generation", 0)
    state["schema_version"] = 10
    return state


_STEPS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    1: _v1_to_v2,
    2: _v2_to_v3,
    3: _v3_to_v4,
    4: _v4_to_v5,
    5: _v5_to_v6,
    6: _v6_to_v7,
    7: _v7_to_v8,
    8: _v8_to_v9,
    9: _v9_to_v10,
}


def migrate_state_dict(
    state: dict[str, Any],
    *,
    target_schema: int = CURRENT_STATE_SCHEMA,
) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise StateMigrationError("durable state must be a JSON object")
    current = _schema_version(state)
    if current > target_schema:
        raise StateMigrationError(
            f"durable state schema {current} is newer than target schema {target_schema}"
        )
    out = dict(state)
    while current < target_schema:
        step = _STEPS.get(current)
        if step is None:
            raise StateMigrationError(
                f"no explicit migration step exists from schema {current}"
            )
        out = step(out)
        next_version = _schema_version(out)
        if next_version != current + 1:
            raise StateMigrationError(
                f"migration step {current} produced invalid schema {next_version}"
            )
        current = next_version
    return out


def _migration_id(
    *,
    source_schema: int,
    target_schema: int,
    before_sha256: str,
    repository_identity: dict[str, Any] | None = None,
) -> str:
    identity_digest = hashlib.sha256(
        json.dumps(
            repository_identity or {},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    material = (
        f"state-schema:{source_schema}:{target_schema}:{before_sha256}:"
        f"{identity_digest}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def _load_active_migration(state_dir: Path) -> dict[str, Any] | None:
    obj = load_json(_active_path(state_dir), {})
    if not obj:
        return None
    if not isinstance(obj, dict):
        raise StateMigrationError("active state migration record is malformed")
    return obj


def _persist_migration(state_dir: Path, record: dict[str, Any]) -> None:
    _migration_dir(state_dir)
    json_dump(_active_path(state_dir), record)


def _archive_completed(state_dir: Path, record: dict[str, Any]) -> None:
    target = _history_dir(state_dir) / f"{record['migration_id']}.json"
    json_dump(target, record)
    try:
        _active_path(state_dir).unlink()
    except FileNotFoundError:
        pass


def _verify_current_state(
    state_dir: Path,
    *,
    target_schema: int,
) -> dict[str, Any]:
    state = load_json(state_dir / "state.json", {})
    if not isinstance(state, dict):
        raise StateMigrationError("migrated durable state is malformed")
    if _schema_version(state) != target_schema:
        raise StateMigrationError(
            f"migrated durable state did not reach schema {target_schema}"
        )
    if target_schema >= 10:
        for key in (
            "accepted_tasks",
            "migration_generation",
        ):
            if key not in state:
                raise StateMigrationError(
                    f"migrated schema-10 state is missing required field {key!r}"
                )
    return state


def migrate_state_on_disk(
    state_dir: Path,
    *,
    target_schema: int = CURRENT_STATE_SCHEMA,
    repository_identity: dict[str, Any] | None = None,
    git_head: str | None = None,
) -> dict[str, Any]:
    state_dir = state_dir.expanduser().resolve()
    state_path = state_dir / "state.json"
    if not state_path.exists():
        return {
            "status": "NEW",
            "source_schema": None,
            "target_schema": target_schema,
        }

    state = load_json(state_path, {})
    if not isinstance(state, dict):
        raise StateMigrationError("durable state must be a JSON object")
    source_schema = _schema_version(state)
    if source_schema > target_schema:
        raise StateMigrationError(
            f"durable state schema {source_schema} is newer than supported schema "
            f"{target_schema}; refusing semantic writes"
        )
    if source_schema == target_schema:
        active = _load_active_migration(state_dir)
        if active:
            if (
                int(active.get("target_schema") or 0) == target_schema
                and active.get("lifecycle_state") in {"APPLYING", "VERIFYING"}
            ):
                active["lifecycle_state"] = "VERIFYING"
                _persist_migration(state_dir, active)
                verified = _verify_current_state(
                    state_dir,
                    target_schema=target_schema,
                )
                active.update({
                    "lifecycle_state": "COMPLETED",
                    "after_state_sha256": _state_file_digest(state_path),
                    "completed_at": utcnow(),
                })
                _persist_migration(state_dir, active)
                _archive_completed(state_dir, active)
                return {
                    "status": "COMPLETED",
                    "source_schema": active.get("source_schema"),
                    "target_schema": target_schema,
                    "migration_id": active.get("migration_id"),
                    "state": verified,
                }
            if active.get("lifecycle_state") == "COMPLETED":
                _archive_completed(state_dir, active)
            else:
                raise StateMigrationError(
                    "unexpected active migration record exists for already-current state"
                )
        verified = _verify_current_state(
            state_dir,
            target_schema=target_schema,
        )
        return {
            "status": "CURRENT",
            "source_schema": source_schema,
            "target_schema": target_schema,
            "state": verified,
        }

    before_sha = _state_file_digest(state_path)
    if not before_sha:
        raise StateMigrationError("unable to bind migration to existing state.json")

    active = _load_active_migration(state_dir)
    if active:
        if (
            int(active.get("source_schema") or 0) != source_schema
            or int(active.get("target_schema") or 0) != target_schema
            or active.get("before_state_sha256") != before_sha
        ):
            raise StateMigrationError(
                "active migration record no longer matches durable source state"
            )
        record = active
    else:
        migration_id = _migration_id(
            source_schema=source_schema,
            target_schema=target_schema,
            before_sha256=before_sha,
            repository_identity=repository_identity,
        )
        record = {
            "schema_version": 1,
            "migration_id": migration_id,
            "source_schema": source_schema,
            "target_schema": target_schema,
            "before_state_sha256": before_sha,
            "repository_identity": repository_identity or {},
            "initial_git_head": str(git_head or ""),
            "mutable_paths": [
                "state.json",
                "migrations/active.json",
                "migrations/history/<migration-id>.json",
            ],
            "lifecycle_state": "PREPARING",
            "started_at": utcnow(),
        }
        _persist_migration(state_dir, record)

    record["lifecycle_state"] = "APPLYING"
    _persist_migration(state_dir, record)

    migrated = migrate_state_dict(
        state,
        target_schema=target_schema,
    )
    migrated["migration_generation"] = int(
        migrated.get("migration_generation", 0) or 0
    ) + 1
    migrated["state_schema_migration"] = {
        "migration_id": record["migration_id"],
        "source_schema": source_schema,
        "target_schema": target_schema,
        "migrated_at": utcnow(),
    }
    json_dump(state_path, migrated)

    record["lifecycle_state"] = "VERIFYING"
    record["after_state_sha256"] = _state_file_digest(state_path)
    _persist_migration(state_dir, record)

    verified = _verify_current_state(
        state_dir,
        target_schema=target_schema,
    )
    record.update({
        "lifecycle_state": "COMPLETED",
        "after_state_sha256": _state_file_digest(state_path),
        "completed_at": utcnow(),
    })
    _persist_migration(state_dir, record)
    _archive_completed(state_dir, record)
    return {
        "status": "COMPLETED",
        "source_schema": source_schema,
        "target_schema": target_schema,
        "migration_id": record["migration_id"],
        "state": verified,
    }
