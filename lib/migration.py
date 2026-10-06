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
)
from git_trust import trusted_git_env
from governance_contract import canonical_json_bytes
from promotion_policy import REPOSITORY_PLANNING_REPAIR_CONTRACT
from repo_identity import repo_state_dir
from runtime_paths import ensure_private_dir, utcnow
from state_store import json_dump, load_json


class MigrationError(ValueError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _policy_path(root: Path) -> Path:
    return repo_state_dir(root) / "planning-repair" / "policy.json"


def _policy_migration_path(root: Path) -> Path:
    return repo_state_dir(root) / "planning-repair" / "policy-migration.json"


def _normalise_repo_path(raw: Any) -> str:
    text = str(raw or "").strip()
    path = Path(text)
    if (
        not text
        or path.is_absolute()
        or path == Path(".")
        or ".." in path.parts
    ):
        raise MigrationError(
            "legacy planning policy canonical_plan is not a safe repository-relative path"
        )
    return path.as_posix()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        env=trusted_git_env(root),
    )


def load_legacy_planning_policy(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = load_json(_policy_path(root), {})
    if not policy:
        return {}
    if not isinstance(policy, dict):
        raise MigrationError("legacy planning policy must be a JSON object")
    return policy


def _semantic_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if policy.get("schema_version") != 1:
        raise MigrationError("unsupported legacy planning policy schema")
    canonical_plan = _normalise_repo_path(policy.get("canonical_plan"))
    product_branch = str(policy.get("product_branch") or "").strip()
    if not product_branch:
        raise MigrationError("legacy planning policy has no product_branch")
    remote_raw = policy.get("remote")
    remote = str(remote_raw).strip() if remote_raw not in (None, "") else None
    remote_branch = str(
        policy.get("remote_branch") or product_branch
    ).strip()
    if not remote_branch:
        raise MigrationError("legacy planning policy has no remote branch")
    contract = str(policy.get("attestation_contract") or "").strip()
    if contract != REPOSITORY_PLANNING_REPAIR_CONTRACT:
        raise MigrationError(
            "legacy planning policy attestation contract is not the repository planning contract"
        )
    return {
        "schema_version": 1,
        "canonical_plan": canonical_plan,
        "product_branch": product_branch,
        "remote": remote,
        "remote_branch": remote_branch,
        "attestation_contract": contract,
    }


def normalise_legacy_planning_policy(root: Path) -> dict[str, Any] | None:
    root = root.expanduser().resolve()
    raw = load_legacy_planning_policy(root)
    if not raw:
        return None
    semantic = _semantic_policy(raw)
    branch = semantic["product_branch"]

    branch_probe = _git(root, "rev-parse", "--verify", f"{branch}^{{commit}}")
    if branch_probe.returncode != 0:
        raise MigrationError(
            f"legacy planning policy product branch does not resolve: {branch}"
        )
    product_sha = branch_probe.stdout.strip().lower()

    if semantic["remote"]:
        remote_probe = _git(root, "remote", "get-url", semantic["remote"])
        if remote_probe.returncode != 0:
            raise MigrationError(
                f"legacy planning policy remote does not exist: {semantic['remote']}"
            )

    try:
        snapshot = build_authority_snapshot(root, product_sha)
    except AuthoritySetError as exc:
        raise MigrationError(str(exc)) from exc
    if not isinstance(snapshot, dict):
        raise MigrationError(
            "legacy planning policy did not resolve to repository planning authority"
        )
    if snapshot.get("source_mode") not in {"legacy", "contract+legacy"}:
        raise MigrationError(
            "legacy planning policy is not represented in the current AuthoritySet snapshot"
        )

    sets = snapshot.get("sets")
    if not isinstance(sets, list):
        raise MigrationError("resolved AuthoritySet snapshot is malformed")
    default_sets = [
        item
        for item in sets
        if isinstance(item, dict) and item.get("id") == "default"
    ]
    if len(default_sets) != 1:
        raise MigrationError(
            "legacy one-file policy must resolve to exactly one synthetic/default AuthoritySet"
        )
    members = default_sets[0].get("members")
    if not isinstance(members, list):
        raise MigrationError("resolved default AuthoritySet members are malformed")
    canonical_plan = semantic["canonical_plan"]
    matching = [
        member
        for member in members
        if isinstance(member, dict) and member.get("path") == canonical_plan
    ]
    if len(matching) != 1:
        raise MigrationError(
            "legacy canonical plan is not represented exactly once in the default AuthoritySet"
        )
    member = matching[0]
    if member.get("role") != "source" or member.get("repair") != "repairable":
        raise MigrationError(
            "legacy canonical plan did not preserve repairable source semantics"
        )

    semantic_digest = _digest(semantic)
    content_digest = authority_content_sha256(snapshot)
    active_path = _policy_migration_path(root)
    existing = load_json(active_path, {})
    if existing and not isinstance(existing, dict):
        raise MigrationError("legacy planning policy migration record is malformed")
    if (
        isinstance(existing, dict)
        and existing.get("legacy_policy_sha256") == semantic_digest
        and existing.get("authority_content_sha256") == content_digest
        and existing.get("product_sha") == product_sha
    ):
        return existing

    generation = (
        int(existing.get("generation", 0) or 0) + 1
        if isinstance(existing, dict)
        else 1
    )
    semantic_record = {
        "schema_version": 1,
        "generation": generation,
        "legacy_policy_sha256": semantic_digest,
        "product_sha": product_sha,
        "product_branch": branch,
        "canonical_plan": canonical_plan,
        "source_mode": snapshot["source_mode"],
        "authority_set_id": "default",
        "authority_content_sha256": content_digest,
        "authority_snapshot_sha256": snapshot["snapshot_sha256"],
    }
    record = {
        **semantic_record,
        "migration_record_sha256": _digest(semantic_record),
        "normalised_at": utcnow(),
    }
    ensure_private_dir(active_path.parent)
    json_dump(active_path, record)
    return record
