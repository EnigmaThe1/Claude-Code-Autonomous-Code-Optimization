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
import subprocess
import tempfile
from pathlib import Path

import pytest

from migration import (
    MigrationError,
    migrate_legacy_planning_repair,
    normalise_legacy_planning_policy,
)
from planning_repair import (
    begin_planning_repair,
    configure_planning_repair,
    load_active_repair,
)
from repair_envelope import load_repair_envelope
from repo_identity import SupervisorLease, repo_state_dir
from repo_runtime import activate
from state_migration import (
    CURRENT_STATE_SCHEMA,
    StateMigrationError,
    migrate_state_on_disk,
)
from state_store import json_dump, load_json


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "git", "init", "-q")
    _run(root, "git", "config", "user.name", "Migration Test")
    _run(root, "git", "config", "user.email", "migration@example.invalid")
    (root / "README.md").write_text("base\n")
    _run(root, "git", "add", "README.md")
    _run(root, "git", "commit", "-qm", "base")
    return root


def _head(root: Path) -> str:
    return _run(root, "git", "rev-parse", "HEAD").stdout.strip()


def _status(root: Path) -> str:
    return _run(root, "git", "status", "--porcelain=v1", "--untracked-files=all").stdout


def _state_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()




def _legacy_repair_fixture(
    root: Path,
    *,
    candidate: bool = False,
    verified: bool = False,
) -> tuple[dict, Path, str | None]:
    sd = repo_state_dir(root)
    repair_dir = sd / "planning-repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    branch = "claude-auto/planning-repair/legacy-migration-test"
    worktree = repair_dir / "worktree"
    base = _head(root)
    _run(
        root,
        "git",
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree),
        base,
    )
    candidate_sha: str | None = None
    if candidate:
        plan = worktree / "PLAN.md"
        plan.write_text(plan.read_text() + "\nMigrated repair candidate.\n")
        _run(worktree, "git", "add", "PLAN.md")
        _run(worktree, "git", "commit", "-qm", "legacy planning candidate")
        candidate_sha = _head(worktree)

    active = {
        "schema_version": 1,
        "status": "ACTIVE",
        "started_at": "2026-01-01T00:00:00+00:00",
        "reason": "legacy planning repair",
        "base_sha": base,
        "product_branch": _run(
            root, "git", "branch", "--show-current"
        ).stdout.strip(),
        "repair_branch": branch,
        "worktree": str(worktree),
        "canonical_plan": "PLAN.md",
        "candidate_sha": candidate_sha,
        "verified_sha": candidate_sha if verified else None,
        "refresh": None,
    }
    if verified:
        active.update({
            "verifier_summary": "legacy verifier said this was valid",
            "verifier_findings": [],
            "verifier_attestation": {
                "target_sha": candidate_sha,
                "legacy": True,
            },
        })
    json_dump(repair_dir / "active.json", active)
    return active, worktree, candidate_sha


def test_p6_new_repository_activate_creates_schema10(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        before_head = _head(root)

        with SupervisorLease(sd, root):
            activate(root)

        state = load_json(sd / "state.json", {})
        assert state["schema_version"] == CURRENT_STATE_SCHEMA == 10
        assert state["migration_generation"] == 0
        assert state["state_schema_migration"] is None
        assert state["legacy_adoption"] is None
        assert state["session_adoption"] is None
        assert state["shadow_validation"] is None
        assert state["accepted_tasks"] == {}
        assert _head(root) == before_head
        assert _status(root) == ""


def test_p6_schema9_migrates_before_activation_writes(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        sd.mkdir(parents=True)
        json_dump(sd / "state.json", {
            "schema_version": 9,
            "objective": "preserve migrated objective",
            "status": "READY",
            "cycle": 7,
            "accepted_tasks": {},
            "governance_snapshot_sha256": None,
            "governance_snapshot_generation": 0,
            "task_source_sha256": None,
            "active_task_id": None,
            "active_task_spec_sha256": None,
            "active_execution_envelope_sha256": None,
            "governance_blocker": None,
        })
        before_head = _head(root)
        before_status = _status(root)

        with SupervisorLease(sd, root):
            activate(root)

        state = load_json(sd / "state.json", {})
        assert state["schema_version"] == 10
        assert state["objective"] == "preserve migrated objective"
        assert state["cycle"] == 7
        assert state["migration_generation"] == 1
        migration = state["state_schema_migration"]
        assert migration["source_schema"] == 9
        assert migration["target_schema"] == 10
        history = list((sd / "migrations" / "history").glob("*.json"))
        assert len(history) == 1
        record = json.loads(history[0].read_text())
        assert record["lifecycle_state"] == "COMPLETED"
        assert record["source_schema"] == 9
        assert record["target_schema"] == 10
        assert record["initial_git_head"] == before_head
        assert record["repository_identity"]
        assert "state.json" in record["mutable_paths"]
        assert _head(root) == before_head
        assert _status(root) == before_status


def test_p6_legacy_schema5_walks_explicit_chain_to10(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        sd.mkdir(parents=True)
        json_dump(sd / "state.json", {
            "schema_version": 5,
            "objective": "legacy objective survives",
            "status": "BLOCKED",
            "cycle": 12,
            "permission_grants": [],
            "permission_decisions": [],
            "permission_requests": [],
            "last_verification_receipts": [{"legacy": True}],
            "completion_anchor": {"legacy": True},
        })

        with SupervisorLease(sd, root):
            activate(root)

        state = load_json(sd / "state.json", {})
        assert state["schema_version"] == 10
        assert state["objective"] == "legacy objective survives"
        assert state["cycle"] == 12
        assert state["migration_generation"] == 1
        assert state["last_verification_receipts"] is None
        assert state["completion_anchor"] is None
        assert "current-authority revalidation" in state[
            "completion_anchor_invalidated_reason"
        ]
        assert isinstance(state["accepted_tasks"], dict)


def test_p6_preparing_migration_record_resumes_idempotently(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        (sd / "migrations").mkdir(parents=True)
        state_path = sd / "state.json"
        json_dump(state_path, {
            "schema_version": 9,
            "objective": "resume migration",
            "accepted_tasks": {},
        })
        before_sha = _state_digest(state_path)
        json_dump(sd / "migrations" / "active.json", {
            "schema_version": 1,
            "migration_id": "resume-test",
            "source_schema": 9,
            "target_schema": 10,
            "before_state_sha256": before_sha,
            "repository_identity": {},
            "initial_git_head": _head(root),
            "mutable_paths": [
                "state.json",
                "migrations/active.json",
                "migrations/history/<migration-id>.json",
            ],
            "lifecycle_state": "PREPARING",
        })

        result = migrate_state_on_disk(sd)
        assert result["status"] == "COMPLETED"
        assert result["migration_id"] == "resume-test"
        state = load_json(state_path, {})
        assert state["schema_version"] == 10
        assert not (sd / "migrations" / "active.json").exists()
        assert (sd / "migrations" / "history" / "resume-test.json").is_file()


def test_p6_apply_crash_with_schema10_finishes_verification(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        (sd / "migrations").mkdir(parents=True)
        state_path = sd / "state.json"
        json_dump(state_path, {
            "schema_version": 10,
            "accepted_tasks": {},
            "migration_generation": 1,
            "state_schema_migration": {
                "migration_id": "apply-crash",
                "source_schema": 9,
                "target_schema": 10,
            },
            "legacy_adoption": None,
            "session_adoption": None,
            "shadow_validation": None,
        })
        json_dump(sd / "migrations" / "active.json", {
            "schema_version": 1,
            "migration_id": "apply-crash",
            "source_schema": 9,
            "target_schema": 10,
            "before_state_sha256": "0" * 64,
            "repository_identity": {},
            "initial_git_head": _head(root),
            "mutable_paths": [
                "state.json",
                "migrations/active.json",
                "migrations/history/<migration-id>.json",
            ],
            "lifecycle_state": "APPLYING",
        })

        result = migrate_state_on_disk(sd)
        assert result["status"] == "COMPLETED"
        assert result["migration_id"] == "apply-crash"
        assert not (sd / "migrations" / "active.json").exists()
        assert (sd / "migrations" / "history" / "apply-crash.json").is_file()


def test_p6_future_state_schema_refuses_before_activation_semantic_writes(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        sd.mkdir(parents=True)
        state_path = sd / "state.json"
        json_dump(state_path, {
            "schema_version": CURRENT_STATE_SCHEMA + 1,
            "objective": "future state",
        })
        before = _state_digest(state_path)

        with SupervisorLease(sd, root):
            with pytest.raises(StateMigrationError, match="newer than supported"):
                activate(root)

        assert _state_digest(state_path) == before
        assert not (sd / "profile.json").exists()
        assert not (sd / "settings.json").exists()
        assert not (sd / "governance" / "snapshot.json").exists()
        assert not (sd / "migrations" / "active.json").exists()


def test_p6_legacy_one_file_policy_normalises_without_rewriting_policy(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        plan = root / "PLAN.md"
        plan.write_text("# canonical plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "add canonical plan")

        configure_planning_repair(root, canonical_plan="PLAN.md")
        sd = repo_state_dir(root)
        policy_path = sd / "planning-repair" / "policy.json"
        before_bytes = policy_path.read_bytes()
        before_sha = hashlib.sha256(before_bytes).hexdigest()

        with SupervisorLease(sd, root):
            activate(root)

        assert policy_path.read_bytes() == before_bytes
        assert hashlib.sha256(policy_path.read_bytes()).hexdigest() == before_sha

        normalised_path = sd / "planning-repair" / "policy-migration.json"
        assert normalised_path.is_file()
        record = json.loads(normalised_path.read_text())
        assert record["schema_version"] == 1
        assert record["generation"] == 1
        assert record["canonical_plan"] == "PLAN.md"
        assert record["authority_set_id"] == "default"
        assert record["source_mode"] == "legacy"
        assert len(record["authority_content_sha256"]) == 64
        assert len(record["authority_snapshot_sha256"]) == 64
        assert record["product_sha"] == _head(root)

        # Re-activation does not rewrite either the legacy policy or its
        # normalised semantic generation when all exact inputs are unchanged.
        first_record = dict(record)
        with SupervisorLease(sd, root):
            activate(root)
        assert policy_path.read_bytes() == before_bytes
        second_record = json.loads(normalised_path.read_text())
        assert second_record == first_record


def test_p6_legacy_policy_normaliser_is_idempotent_when_called_directly(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("# plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        configure_planning_repair(root, canonical_plan="PLAN.md")

        first = normalise_legacy_planning_policy(root)
        second = normalise_legacy_planning_policy(root)
        assert first is not None and second is not None
        assert first == second
        assert first["generation"] == 1
        assert first["authority_set_id"] == "default"


def test_p6_corrupt_legacy_policy_blocks_without_mutating_policy(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("# plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        configure_planning_repair(root, canonical_plan="PLAN.md")

        sd = repo_state_dir(root)
        policy_path = sd / "planning-repair" / "policy.json"
        policy = json.loads(policy_path.read_text())
        policy["product_branch"] = "missing-branch"
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
        before = policy_path.read_bytes()

        with pytest.raises(MigrationError, match="product branch does not resolve"):
            normalise_legacy_planning_policy(root)

        assert policy_path.read_bytes() == before
        assert not (sd / "planning-repair" / "policy-migration.json").exists()


def test_p6_clean_schema1_repair_migrates_to_p5_envelope(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("# plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        configure_planning_repair(root, canonical_plan="PLAN.md")
        legacy, worktree, _candidate = _legacy_repair_fixture(root)

        result = migrate_legacy_planning_repair(root)
        assert result["status"] == "COMPLETED"
        assert result["candidate_sha"] is None
        active = load_active_repair(root)
        assert active["schema_version"] == 2
        assert active["status"] == "ACTIVE"
        assert active["base_sha"] == legacy["base_sha"]
        assert active["candidate_sha"] is None
        assert active["verified_sha"] is None
        assert active["selected_authority_sets"] == ["default"]
        assert active["repair_envelope_sha256"] == result[
            "repair_envelope_sha256"
        ]
        envelope = load_repair_envelope(root)
        assert envelope is not None
        assert envelope["base_sha"] == legacy["base_sha"]
        assert envelope["product_branch"] == legacy["product_branch"]
        assert envelope["repairable_paths"] == ["PLAN.md"]
        assert _head(worktree) == legacy["base_sha"]

        # P5 can reopen the migrated state without another migration.
        reopened = begin_planning_repair(root, reason="resume migrated repair")
        assert reopened["schema_version"] == 2
        assert reopened["repair_envelope_sha256"] == active[
            "repair_envelope_sha256"
        ]
        assert migrate_legacy_planning_repair(root)["status"] == "CURRENT"


def test_p6_legacy_verified_candidate_loses_trusted_verifier_status(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("# plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        configure_planning_repair(root, canonical_plan="PLAN.md")
        legacy, worktree, candidate = _legacy_repair_fixture(
            root,
            candidate=True,
            verified=True,
        )
        assert candidate is not None

        result = migrate_legacy_planning_repair(root)
        assert result["status"] == "COMPLETED"
        assert result["candidate_sha"] == candidate
        assert result["legacy_verified_sha_claim"] == candidate

        active = load_active_repair(root)
        assert active["schema_version"] == 2
        assert active["status"] == "CANDIDATE"
        assert active["candidate_sha"] == candidate
        assert active["verified_sha"] is None
        assert active.get("verifier_attestation") is None
        assert active.get("validated_candidate_sha") is None
        provenance = active["migration_provenance"]
        assert provenance["legacy_verified_sha_claim"] == candidate
        assert provenance["legacy_verifier_summary"] == (
            "legacy verifier said this was valid"
        )
        assert provenance["legacy_verifier_attestation_sha256"]
        assert _head(worktree) == candidate

        # Restart/recovery accepts the exact durable candidate but does not
        # restore legacy verification trust.
        reopened = begin_planning_repair(root, reason="resume candidate")
        assert reopened["candidate_sha"] == candidate
        assert reopened["verified_sha"] is None


def test_p6_schema1_repair_recovers_missing_worktree_from_surviving_branch(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("# plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        configure_planning_repair(root, canonical_plan="PLAN.md")
        legacy, worktree, _candidate = _legacy_repair_fixture(root)
        _run(root, "git", "worktree", "remove", "--force", str(worktree))
        assert not worktree.exists()

        result = migrate_legacy_planning_repair(root)
        assert result["status"] == "COMPLETED"
        assert worktree.is_dir()
        assert _head(worktree) == legacy["base_sha"]
        assert load_active_repair(root)["schema_version"] == 2


def test_p6_schema1_uncommitted_plan_wip_is_preserved_and_blocks(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("# plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        configure_planning_repair(root, canonical_plan="PLAN.md")
        _legacy, worktree, _candidate = _legacy_repair_fixture(root)
        plan = worktree / "PLAN.md"
        plan.write_text(plan.read_text() + "\nUncommitted legacy progress.\n")
        before = plan.read_bytes()

        with pytest.raises(MigrationError, match="uncommitted"):
            migrate_legacy_planning_repair(root)

        assert plan.read_bytes() == before
        active = load_active_repair(root)
        assert active["schema_version"] == 1
        record = load_json(
            repo_state_dir(root)
            / "planning-repair"
            / "migration-active.json",
            {},
        )
        assert record["lifecycle_state"] == "BLOCKED"
        assert record["dirty_paths"] == ["PLAN.md"]
        assert not (
            repo_state_dir(root)
            / "planning-repair"
            / "repair-envelope.json"
        ).exists()


def test_p6_schema1_out_of_scope_wip_is_preserved_and_blocks(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        (root / "PLAN.md").write_text("# plan\n")
        _run(root, "git", "add", "PLAN.md")
        _run(root, "git", "commit", "-qm", "plan")
        configure_planning_repair(root, canonical_plan="PLAN.md")
        _legacy, worktree, _candidate = _legacy_repair_fixture(root)
        app = worktree / "README.md"
        app.write_text("unexpected product WIP\n")
        before = app.read_bytes()

        with pytest.raises(MigrationError, match="out-of-scope"):
            migrate_legacy_planning_repair(root)

        assert app.read_bytes() == before
        assert load_active_repair(root)["schema_version"] == 1
        record = load_json(
            repo_state_dir(root)
            / "planning-repair"
            / "migration-active.json",
            {},
        )
        assert record["lifecycle_state"] == "BLOCKED"
        assert "README.md" in record["dirty_paths"]
