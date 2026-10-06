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
from repair_envelope import (
    RepairEnvelopeError,
    derive_repair_envelope,
    load_repair_envelope,
    persist_repair_envelope,
)
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
    canonical_plan = semantic["canonical_plan"]
    matching_sets: list[str] = []
    for authority_set in sets:
        if not isinstance(authority_set, dict) or not isinstance(
            authority_set.get("id"), str
        ):
            raise MigrationError("resolved AuthoritySet snapshot is malformed")
        members = authority_set.get("members")
        if not isinstance(members, list):
            raise MigrationError("resolved AuthoritySet members are malformed")
        for member in members:
            if (
                isinstance(member, dict)
                and member.get("path") == canonical_plan
                and member.get("role") == "source"
                and member.get("repair") == "repairable"
            ):
                matching_sets.append(authority_set["id"])
                break

    matching_sets = sorted(set(matching_sets))
    if not matching_sets:
        raise MigrationError(
            "legacy canonical plan is not represented as repairable source authority"
        )
    if snapshot.get("source_mode") == "legacy" and matching_sets != ["default"]:
        raise MigrationError(
            "pure legacy one-file policy did not resolve to synthetic default AuthoritySet"
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
        "authority_set_ids": matching_sets,
        "authority_set_id": (
            "default"
            if snapshot["source_mode"] == "legacy"
            else (matching_sets[0] if len(matching_sets) == 1 else None)
        ),
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


def _repair_active_path(root: Path) -> Path:
    return repo_state_dir(root) / "planning-repair" / "active.json"


def _repair_migration_active_path(root: Path) -> Path:
    return repo_state_dir(root) / "planning-repair" / "migration-active.json"


def _repair_migration_history_dir(root: Path) -> Path:
    return ensure_private_dir(
        repo_state_dir(root) / "planning-repair" / "migration-history"
    )


def _repair_worktree_path(root: Path) -> Path:
    return (repo_state_dir(root) / "planning-repair" / "worktree").resolve()


def _rev(root: Path, value: str) -> str:
    cp = _git(root, "rev-parse", "--verify", f"{value}^{{commit}}")
    if cp.returncode != 0 or not cp.stdout.strip():
        raise MigrationError(f"unable to resolve legacy repair commit: {value}")
    return cp.stdout.strip().lower()


def _branch(root: Path) -> str:
    cp = _git(root, "branch", "--show-current")
    if cp.returncode != 0 or not cp.stdout.strip():
        raise MigrationError("legacy repair worktree is not on a named branch")
    return cp.stdout.strip()


def _changed_paths(root: Path, base: str, target: str) -> set[str]:
    cp = _git(root, "diff", "--name-only", "-z", base, target, "--")
    if cp.returncode != 0:
        raise MigrationError("unable to inspect legacy repair candidate scope")
    return {item for item in cp.stdout.split("\0") if item}


def _dirty_paths(root: Path) -> set[str]:
    tracked = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        "HEAD",
        "--",
    )
    if tracked.returncode != 0:
        raise MigrationError("unable to inspect legacy repair tracked WIP")
    untracked = _git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if untracked.returncode != 0:
        raise MigrationError("unable to inspect legacy repair untracked WIP")
    return {
        item
        for payload in (tracked.stdout, untracked.stdout)
        for item in payload.split("\0")
        if item
    }


def _repair_state_digest(state: dict[str, Any]) -> str:
    return _digest(state)


def _repair_migration_record(root: Path) -> dict[str, Any]:
    obj = load_json(_repair_migration_active_path(root), {})
    if not obj:
        return {}
    if not isinstance(obj, dict):
        raise MigrationError("active planning-repair migration record is malformed")
    return obj


def _persist_repair_migration(root: Path, record: dict[str, Any]) -> None:
    path = _repair_migration_active_path(root)
    ensure_private_dir(path.parent)
    json_dump(path, record)


def _archive_repair_migration(root: Path, record: dict[str, Any]) -> None:
    migration_id = str(record.get("migration_id") or "")
    if not migration_id:
        raise MigrationError("planning-repair migration record has no migration_id")
    target = _repair_migration_history_dir(root) / f"{migration_id}.json"
    json_dump(target, record)
    try:
        _repair_migration_active_path(root).unlink()
    except FileNotFoundError:
        pass


def _block_repair_migration(
    root: Path,
    record: dict[str, Any],
    *,
    reason: str,
    dirty_paths: set[str] | None = None,
) -> None:
    record.update({
        "lifecycle_state": "BLOCKED",
        "blocker": str(reason)[:1800],
        "dirty_paths": sorted(dirty_paths or set()),
        "blocked_at": utcnow(),
    })
    _persist_repair_migration(root, record)
    raise MigrationError(reason)


def _recover_legacy_repair_worktree(
    root: Path,
    active: dict[str, Any],
    record: dict[str, Any],
) -> tuple[Path, str, str]:
    expected_worktree = _repair_worktree_path(root)
    raw_worktree = str(active.get("worktree") or "")
    if not raw_worktree:
        _block_repair_migration(
            root,
            record,
            reason="legacy planning repair has no recorded worktree path",
        )
    worktree = Path(raw_worktree).expanduser().resolve()
    if worktree != expected_worktree:
        _block_repair_migration(
            root,
            record,
            reason=(
                "legacy planning repair worktree is outside its exact package-owned "
                "planning-repair path"
            ),
        )

    repair_branch = str(active.get("repair_branch") or "")
    if not repair_branch.startswith("claude-auto/planning-repair/"):
        _block_repair_migration(
            root,
            record,
            reason="legacy planning repair branch is incomplete or untrusted",
        )
    base = _rev(root, str(active.get("base_sha") or ""))

    branch_exists = (
        _git(root, "show-ref", "--verify", f"refs/heads/{repair_branch}").returncode
        == 0
    )
    if worktree.exists() and not branch_exists:
        _block_repair_migration(
            root,
            record,
            reason="legacy repair worktree exists but its repair branch is missing",
        )
    if not worktree.exists():
        if not branch_exists:
            _block_repair_migration(
                root,
                record,
                reason=(
                    "legacy repair state exists but both package branch and "
                    "worktree are missing"
                ),
            )
        cp = _git(root, "worktree", "add", str(worktree), repair_branch)
        if cp.returncode != 0:
            _block_repair_migration(
                root,
                record,
                reason=(
                    "unable to recover exact legacy repair worktree: "
                    + (cp.stderr or cp.stdout or "git worktree add failed").strip()[:1200]
                ),
            )

    if _branch(worktree) != repair_branch:
        _block_repair_migration(
            root,
            record,
            reason="legacy repair worktree is not on its exact recorded branch",
        )
    return worktree, repair_branch, base


def _legacy_selected_authority_sets(
    root: Path,
    *,
    base: str,
    canonical_plan: str,
) -> list[str]:
    try:
        snapshot = build_authority_snapshot(root, base)
    except AuthoritySetError as exc:
        raise MigrationError(str(exc)) from exc
    if not isinstance(snapshot, dict):
        raise MigrationError(
            "legacy planning repair base no longer resolves planning authority"
        )
    matches: list[str] = []
    for authority_set in snapshot.get("sets", []):
        if not isinstance(authority_set, dict):
            continue
        set_id = authority_set.get("id")
        if not isinstance(set_id, str):
            continue
        for member in authority_set.get("members", []):
            if (
                isinstance(member, dict)
                and member.get("path") == canonical_plan
                and member.get("role") == "source"
                and member.get("repair") == "repairable"
            ):
                matches.append(set_id)
                break
    matches = sorted(set(matches))
    if snapshot.get("source_mode") == "legacy":
        if matches != ["default"]:
            raise MigrationError(
                "legacy repair base did not resolve synthetic default planning authority"
            )
        return matches
    if len(matches) != 1:
        raise MigrationError(
            "legacy repair maps ambiguously to current committed AuthoritySets at "
            f"its recorded base: {matches or '<none>'}"
        )
    return matches


def migrate_legacy_planning_repair(root: Path) -> dict[str, Any]:
    """Convert one clean schema-1 planning repair into P5 schema-2 state.

    Legacy verifier evidence is preserved only as migration provenance. Current
    P5 candidate validation, validators, independent verifier and attestation
    must run again before any promotion.
    """
    root = root.expanduser().resolve()
    active_path = _repair_active_path(root)
    active = load_json(active_path, {})
    if not active:
        return {"status": "NO_ACTIVE_REPAIR"}
    if not isinstance(active, dict):
        raise MigrationError("active planning repair state is malformed")

    schema = active.get("schema_version")
    if schema == 2:
        pending = _repair_migration_record(root)
        if pending:
            envelope = load_repair_envelope(root)
            if (
                not isinstance(envelope, dict)
                or envelope.get("repair_envelope_sha256")
                != active.get("repair_envelope_sha256")
            ):
                raise MigrationError(
                    "schema-2 repair exists but pending migration envelope binding is invalid"
                )
            expected_head = str(active.get("candidate_sha") or active.get("base_sha") or "")
            worktree = Path(str(active.get("worktree") or "")).expanduser().resolve()
            if not expected_head or not worktree.exists() or _rev(worktree, "HEAD") != expected_head:
                raise MigrationError(
                    "schema-2 repair exists but pending migration worktree identity is invalid"
                )
            pending.update({
                "lifecycle_state": "COMPLETED",
                "target_active_state_sha256": _repair_state_digest(active),
                "completed_at": utcnow(),
            })
            _persist_repair_migration(root, pending)
            _archive_repair_migration(root, pending)
            return {
                "status": "COMPLETED",
                "migration_id": pending.get("migration_id"),
                "candidate_sha": active.get("candidate_sha"),
                "repair_envelope_sha256": active.get("repair_envelope_sha256"),
            }
        return {
            "status": "CURRENT",
            "candidate_sha": active.get("candidate_sha"),
            "repair_envelope_sha256": active.get("repair_envelope_sha256"),
        }
    if schema != 1:
        raise MigrationError(
            f"unsupported active planning-repair schema for P6 migration: {schema!r}"
        )

    policy_record = normalise_legacy_planning_policy(root)
    if not isinstance(policy_record, dict):
        raise MigrationError(
            "schema-1 active repair has no valid legacy planning policy"
        )
    canonical_plan = _normalise_repo_path(active.get("canonical_plan"))
    if canonical_plan != policy_record["canonical_plan"]:
        raise MigrationError(
            "legacy active repair canonical plan and configured policy disagree"
        )
    product_branch = str(active.get("product_branch") or "").strip()
    if product_branch != policy_record["product_branch"]:
        raise MigrationError(
            "legacy active repair product branch and configured policy disagree"
        )

    source_digest = _repair_state_digest(active)
    migration_id = hashlib.sha256(
        (
            "planning-repair-v1-to-v2:"
            + source_digest
            + ":"
            + str(active.get("base_sha") or "")
        ).encode("utf-8")
    ).hexdigest()[:24]
    record = _repair_migration_record(root)
    if record:
        if (
            record.get("migration_id") != migration_id
            or record.get("source_active_state_sha256") != source_digest
        ):
            raise MigrationError(
                "active planning-repair migration record no longer matches schema-1 state"
            )
        if record.get("lifecycle_state") == "BLOCKED":
            # Re-evaluate on explicit retry in case the operator preserved/fixed
            # the blocking external condition without changing durable schema-1
            # state.
            record.pop("blocker", None)
            record.pop("dirty_paths", None)
    else:
        legacy_snapshot_path = (
            _repair_migration_history_dir(root)
            / f"{migration_id}-legacy-active.json"
        )
        json_dump(legacy_snapshot_path, active)
        record = {
            "schema_version": 1,
            "migration_id": migration_id,
            "source_schema": 1,
            "target_schema": 2,
            "source_active_state_sha256": source_digest,
            "legacy_active_snapshot_path": str(legacy_snapshot_path),
            "legacy_verified_sha_claim": active.get("verified_sha"),
            "lifecycle_state": "PREPARING",
            "started_at": utcnow(),
        }
    _persist_repair_migration(root, record)

    worktree, repair_branch, base = _recover_legacy_repair_worktree(
        root,
        active,
        record,
    )
    dirty = _dirty_paths(worktree)
    if dirty:
        unexpected = dirty - {canonical_plan}
        if unexpected:
            _block_repair_migration(
                root,
                record,
                reason=(
                    "legacy planning repair contains out-of-scope uncommitted WIP; "
                    "migration preserved the worktree without modifying it"
                ),
                dirty_paths=dirty,
            )
        _block_repair_migration(
            root,
            record,
            reason=(
                "legacy canonical-plan WIP is uncommitted; migration preserved it "
                "in place and refuses to manufacture a candidate commit"
            ),
            dirty_paths=dirty,
        )

    branch_head = _rev(worktree, "HEAD")
    candidate_raw = str(active.get("candidate_sha") or "").strip()
    inferred_candidate = False
    if candidate_raw:
        candidate = _rev(worktree, candidate_raw)
        if candidate != branch_head:
            _block_repair_migration(
                root,
                record,
                reason="legacy candidate SHA does not equal exact repair-branch HEAD",
            )
    elif branch_head != base:
        candidate = branch_head
        inferred_candidate = True
    else:
        candidate = None

    if candidate is not None:
        ancestor = _git(worktree, "merge-base", "--is-ancestor", base, candidate)
        if ancestor.returncode != 0:
            _block_repair_migration(
                root,
                record,
                reason="legacy repair candidate is not a descendant of its recorded base",
            )
        changed = _changed_paths(worktree, base, candidate)
        if changed != {canonical_plan}:
            _block_repair_migration(
                root,
                record,
                reason=(
                    "legacy repair candidate changes paths outside the canonical plan: "
                    + (", ".join(sorted(changed)) or "<none>")
                ),
            )
    elif branch_head != base:
        _block_repair_migration(
            root,
            record,
            reason="legacy repair branch moved without a valid candidate",
        )

    selected_sets = _legacy_selected_authority_sets(
        root,
        base=base,
        canonical_plan=canonical_plan,
    )
    try:
        envelope = derive_repair_envelope(
            root,
            reason=str(active.get("reason") or "migrate legacy planning repair"),
            authority_sets=selected_sets,
            base_ref=base,
            product_branch=product_branch,
        )
    except RepairEnvelopeError as exc:
        _block_repair_migration(root, record, reason=str(exc))
        raise AssertionError("unreachable")
    if canonical_plan not in set(envelope.get("repairable_paths", [])):
        _block_repair_migration(
            root,
            record,
            reason=(
                "derived RepairEnvelope does not retain the legacy canonical plan "
                "as repairable authority"
            ),
        )

    record["lifecycle_state"] = "APPLYING"
    record["base_sha"] = base
    record["candidate_sha"] = candidate
    record["candidate_inferred_from_branch"] = inferred_candidate
    record["repair_envelope_sha256"] = envelope["repair_envelope_sha256"]
    _persist_repair_migration(root, record)

    persisted_envelope = persist_repair_envelope(root, envelope)
    migrated = {
        "schema_version": 2,
        "status": "CANDIDATE" if candidate else "ACTIVE",
        "started_at": active.get("started_at") or utcnow(),
        "reason": str(active.get("reason") or "")[:1800],
        "base_sha": base,
        "product_branch": product_branch,
        "repair_branch": repair_branch,
        "worktree": str(worktree),
        "canonical_plan": canonical_plan,
        "repair_envelope_sha256": persisted_envelope[
            "repair_envelope_sha256"
        ],
        "selected_authority_sets": persisted_envelope[
            "selected_authority_sets"
        ],
        "candidate_sha": candidate,
        "verified_sha": None,
        "refresh": None,
        "architect_classification": active.get("architect_classification"),
        "architect_summary": active.get("architect_summary"),
        "candidate_created_at": active.get("candidate_created_at"),
        "migration_provenance": {
            "migration_id": migration_id,
            "source_schema": 1,
            "source_active_state_sha256": source_digest,
            "legacy_verified_sha_claim": active.get("verified_sha"),
            "legacy_verifier_summary": active.get("verifier_summary"),
            "legacy_verifier_findings": active.get("verifier_findings"),
            "legacy_verifier_attestation_sha256": (
                _digest(active.get("verifier_attestation"))
                if active.get("verifier_attestation") is not None
                else None
            ),
            "candidate_inferred_from_branch": inferred_candidate,
            "migrated_at": utcnow(),
        },
    }
    # Explicitly do not copy any legacy validation/verifier/attestation fields
    # into current trusted P5 fields.
    json_dump(active_path, migrated)

    record["lifecycle_state"] = "VERIFYING"
    record["target_active_state_sha256"] = _repair_state_digest(migrated)
    _persist_repair_migration(root, record)

    loaded_envelope = load_repair_envelope(root)
    if (
        not isinstance(loaded_envelope, dict)
        or loaded_envelope.get("repair_envelope_sha256")
        != migrated["repair_envelope_sha256"]
    ):
        raise MigrationError(
            "migrated planning repair did not reload its exact RepairEnvelope"
        )
    expected_head = candidate or base
    if _rev(worktree, "HEAD") != expected_head:
        raise MigrationError(
            "migrated planning repair worktree moved during migration"
        )

    record.update({
        "lifecycle_state": "COMPLETED",
        "target_active_state_sha256": _repair_state_digest(migrated),
        "completed_at": utcnow(),
    })
    _persist_repair_migration(root, record)
    _archive_repair_migration(root, record)
    return {
        "status": "COMPLETED",
        "migration_id": migration_id,
        "candidate_sha": candidate,
        "legacy_verified_sha_claim": active.get("verified_sha"),
        "repair_envelope_sha256": migrated["repair_envelope_sha256"],
        "selected_authority_sets": migrated["selected_authority_sets"],
    }
