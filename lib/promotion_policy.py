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
import re
from pathlib import Path
from typing import Any

from repo_identity import repo_state_dir
from operator_authority import require_top_level_operator
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_CONTRACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REPOSITORY_PLANNING_REPAIR_CONTRACT = "repository-planning-repair"


def _promotion_dir(root: Path) -> Path:
    return ensure_private_dir(repo_state_dir(root.expanduser().resolve()) / "promotion")


def _policy_path(root: Path) -> Path:
    return _promotion_dir(root) / "policy.json"


def _attestation_dir(root: Path) -> Path:
    return ensure_private_dir(_promotion_dir(root) / "attestations")


def _contract_key(contract: str) -> str:
    if not _CONTRACT_RE.fullmatch(contract):
        raise ValueError("attestation contract must be a compact stable identifier")
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()[:20]


def configure_promotion_policy(root: Path, required_contract: str | None) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if required_contract is None:
        try:
            _policy_path(root).unlink()
        except FileNotFoundError:
            pass
        return {"status": "cleared", "required_attestation_contract": None}
    _contract_key(required_contract)
    policy = {
        "schema_version": 1,
        "required_attestation_contract": required_contract,
        "configured_at": utcnow(),
    }
    json_dump(_policy_path(root), policy)
    return policy


def load_promotion_policy(root: Path) -> dict[str, Any]:
    obj = load_json(_policy_path(root.expanduser().resolve()), {})
    return obj if isinstance(obj, dict) else {}


def record_promotion_attestation(
    root: Path,
    *,
    target_sha: str,
    contract: str,
    verifier: str,
    evidence_sha256: str,
    summary: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = target_sha.lower()
    evidence = evidence_sha256.lower()
    if not _SHA_RE.fullmatch(target):
        raise ValueError("attestation target must be an exact 40-64 character Git SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence):
        raise ValueError("attestation evidence must be a SHA-256 digest")
    _contract_key(contract)
    verifier = str(verifier).strip()
    if not verifier:
        raise ValueError("attestation verifier identity is required")
    record = {
        "schema_version": 1,
        "verdict": "VERIFIED",
        "target_sha": target,
        "contract": contract,
        "verifier": verifier[:300],
        "evidence_sha256": evidence,
        "summary": str(summary)[:1800],
        "verified_at": utcnow(),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    name = f"{target}-{_contract_key(contract)}.json"
    json_dump(_attestation_dir(root) / name, record)
    return record


def load_promotion_attestation(root: Path, target_sha: str, contract: str) -> dict[str, Any]:
    target = target_sha.lower()
    if not _SHA_RE.fullmatch(target):
        return {}
    _contract_key(contract)
    obj = load_json(_attestation_dir(root) / f"{target}-{_contract_key(contract)}.json", {})
    return obj if isinstance(obj, dict) else {}


def required_attestation_contract(root: Path, explicit: str | None = None) -> str | None:
    if explicit:
        _contract_key(explicit)
        return explicit
    policy = load_promotion_policy(root)
    raw = policy.get("required_attestation_contract")
    return raw if isinstance(raw, str) and raw.strip() else None


def require_exact_attestation(
    root: Path,
    target_sha: str,
    *,
    contract: str | None = None,
) -> dict[str, Any] | None:
    required = required_attestation_contract(root, contract)
    if not required:
        return None
    record = load_promotion_attestation(root, target_sha, required)
    if not record:
        raise ValueError(
            f"exact-SHA VERIFIED attestation required for {target_sha} under contract {required!r}"
        )
    if (
        record.get("verdict") != "VERIFIED"
        or str(record.get("target_sha", "")).lower() != target_sha.lower()
        or record.get("contract") != required
    ):
        raise ValueError("promotion attestation does not exactly match target SHA and contract")
    return record


def promotion_status(root: Path, target_sha: str | None = None) -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = load_promotion_policy(root)
    result: dict[str, Any] = {"repository": str(root), "policy": policy}
    contract = required_attestation_contract(root)
    if target_sha and contract:
        result["attestation"] = load_promotion_attestation(root, target_sha, contract)
    return result


def promotion_policy_action(args: Any, *, find_repo_root) -> int:
    try:
        action = getattr(args, "promotion_command", None)
        root = find_repo_root(getattr(args, "repo", None))
        if action in ["require-contract","clear-contract"]:
            require_top_level_operator(root, "Promotion policy change")
        action = getattr(args, "promotion_command", None)
        if action == "status":
            result = promotion_status(root, getattr(args, "sha", None))
        elif action == "require-contract":
            result = configure_promotion_policy(root, args.contract)
        elif action == "clear-contract":
            result = configure_promotion_policy(root, None)
        else:
            raise ValueError("unknown promotion policy action")
    except (OSError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0
