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
from pathlib import Path
from typing import Any

from governance_contract import canonical_json_bytes
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json


class AcceptedTaskError(ValueError):
    pass


_SEMANTIC_KEYS = (
    "schema_version",
    "task_id",
    "task_spec_sha256",
    "task_source_set_sha256",
    "authority_snapshot_sha256",
    "execution_envelope_sha256",
    "base_sha",
    "candidate_sha",
    "accepted_product_sha",
    "no_op",
    "verification_bundle_sha256",
    "verifier_attestation_sha256",
    "attestation_contract",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _token(task_id: str) -> str:
    value = str(task_id)
    if not value:
        raise AcceptedTaskError("accepted task ID is empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _dir(state_dir: Path) -> Path:
    return ensure_private_dir(
        state_dir.expanduser().resolve() / "tasks" / "accepted"
    )


def accepted_task_path(state_dir: Path, task_id: str) -> Path:
    return _dir(state_dir) / f"{_token(task_id)}.json"


def _semantic(record: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key in _SEMANTIC_KEYS if key not in record]
    if missing:
        raise AcceptedTaskError(
            "AcceptedTaskRecord is missing semantic field(s): "
            + ", ".join(missing)
        )
    semantic = {key: record[key] for key in _SEMANTIC_KEYS}
    if semantic["schema_version"] != 1:
        raise AcceptedTaskError("AcceptedTaskRecord schema_version must be 1")
    if not isinstance(semantic["task_id"], str) or not semantic["task_id"]:
        raise AcceptedTaskError("AcceptedTaskRecord task_id is invalid")
    for key in (
        "task_spec_sha256",
        "task_source_set_sha256",
        "authority_snapshot_sha256",
        "execution_envelope_sha256",
        "verification_bundle_sha256",
        "verifier_attestation_sha256",
    ):
        value = semantic[key]
        if not (
            isinstance(value, str)
            and len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value.lower())
        ):
            raise AcceptedTaskError(
                f"AcceptedTaskRecord {key} must be a SHA-256 digest"
            )
        semantic[key] = value.lower()
    for key in ("base_sha", "candidate_sha", "accepted_product_sha"):
        value = semantic[key]
        if not (
            isinstance(value, str)
            and 40 <= len(value) <= 64
            and all(ch in "0123456789abcdef" for ch in value.lower())
        ):
            raise AcceptedTaskError(
                f"AcceptedTaskRecord {key} must be an exact Git object id"
            )
        semantic[key] = value.lower()
    if semantic["candidate_sha"] != semantic["accepted_product_sha"]:
        raise AcceptedTaskError(
            "AcceptedTaskRecord candidate and accepted product SHA must match"
        )
    if not isinstance(semantic["no_op"], bool):
        raise AcceptedTaskError("AcceptedTaskRecord no_op must be boolean")
    contract = semantic["attestation_contract"]
    if not isinstance(contract, str) or not contract.strip():
        raise AcceptedTaskError(
            "AcceptedTaskRecord attestation contract is required"
        )
    semantic["attestation_contract"] = contract.strip()
    return semantic


def persist_accepted_task_record(
    state_dir: Path,
    semantic_record: dict[str, Any],
) -> dict[str, Any]:
    semantic = _semantic(semantic_record)
    record = {
        **semantic,
        "acceptance_sha256": _digest(semantic),
        "accepted_at": utcnow(),
    }
    json_dump(
        accepted_task_path(state_dir, semantic["task_id"]),
        record,
    )
    return record


def load_accepted_task_record(
    state_dir: Path,
    task_id: str,
) -> dict[str, Any]:
    path = accepted_task_path(state_dir, task_id)
    obj = load_json(path, {})
    if not isinstance(obj, dict) or not obj:
        raise AcceptedTaskError(
            f"AcceptedTaskRecord does not exist for {task_id!r}"
        )
    semantic = _semantic(obj)
    expected = _digest(semantic)
    if obj.get("acceptance_sha256") != expected:
        raise AcceptedTaskError(
            "AcceptedTaskRecord semantic integrity check failed"
        )
    return {**obj, **semantic, "acceptance_sha256": expected}
