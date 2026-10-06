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

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from authority_set import build_authority_snapshot
from repo_identity import SupervisorLease, repo_state_dir
from repo_runtime import activate
from state_adoption import (
    MAX_ADOPTION_BYTES,
    StateAdoptionError,
    begin_adopted_active_task,
    begin_adopted_task_reattestation,
    import_legacy_state_claims,
)
from state_store import load_json
from task_authority import task_readiness
from task_workspace import load_active_task_workspace
from task_sources import resolve_task_sources


def _run(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "git", "init", "-q", "-b", "main")
    _run(root, "git", "config", "user.name", "Adoption Test")
    _run(root, "git", "config", "user.email", "adoption@example.invalid")
    (root / "PLAN.md").write_text("# plan\n")
    (root / "README.md").write_text("base\n")
    _run(root, "git", "add", "-A")
    _run(root, "git", "commit", "-qm", "base")
    return root


def _task(task_id: str, *, depends_on: list[str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "id": task_id,
        "authority_sets": ["default"],
        "depends_on": depends_on or [],
        "owned_paths": [f"src/{task_id}/**"],
        "evidence_paths": [f"evidence/{task_id}/**"],
        "runtime_scratch_paths": [f".scratch/{task_id}/**"],
        "verification": ["verify current implementation"],
        "commit_subject": None,
        "metadata": {},
    }


def _configure(root: Path, tasks: list[dict]) -> dict:
    contract = {
        "schema_version": 1,
        "planning_authority": {
            "sets": [{
                "id": "default",
                "members": [{
                    "path": "PLAN.md",
                    "role": "source",
                    "repair": "repairable",
                    "required": True,
                }],
                "validators": [],
                "reconcilers": [],
            }],
        },
        "tasks": {
            "sources": [{
                "id": "tasks",
                "kind": "static",
                "authority_sets": ["default"],
                "tasks": tasks,
            }],
            "execution_mode": "single-writer",
            "strict_dependencies": True,
        },
        "control_surfaces": [],
    }
    path = root / ".claude-auto" / "governance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
    _run(root, "git", "add", ".claude-auto/governance.json")
    _run(root, "git", "commit", "-qm", "governance")

    sd = repo_state_dir(root)
    with SupervisorLease(sd, root):
        activate(root)
        resolved = resolve_task_sources(root, persist=True)
    assert resolved["status"] == "READY"
    return resolved


def _task_rows(task_set: dict) -> dict[str, dict]:
    return {
        row["task"]["id"]: row
        for row in task_set["tasks"]
    }


def _write_document(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def test_p6_adoption_import_preserves_claims_without_unlocking_dependencies(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(
            root,
            [_task("T1"), _task("T2", depends_on=["T1"])],
        )
        rows = _task_rows(task_set)
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        snapshot = build_authority_snapshot(root)
        assert snapshot is not None

        state_path = repo_state_dir(root) / "state.json"
        before = load_json(state_path, {})
        before_authority = {
            key: before.get(key)
            for key in (
                "accepted_tasks",
                "active_task_id",
                "active_task_spec_sha256",
                "active_execution_envelope_sha256",
            )
        }

        doc = _write_document(
            Path(td) / "legacy.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "state-001",
                "product_sha": head,
                "verified_through_sha": head,
                "authority_snapshot_sha256": snapshot["snapshot_sha256"],
                "active_task": {
                    "id": "T2",
                    "task_spec_sha256": rows["T2"]["task_spec_sha256"],
                    "base_sha": head,
                },
                "accepted_tasks": [{
                    "id": "T1",
                    "task_spec_sha256": rows["T1"]["task_spec_sha256"],
                    "accepted_product_sha": head,
                }],
                "blockers": ["awaiting external approval"],
                "reservations": ["legacy-worker-1"],
            },
        )

        record = import_legacy_state_claims(root, doc)
        assert record["legacy_product_relationship"]["status"] == "MATCH"
        assert record["legacy_verified_relationship"]["status"] == "MATCH"
        assert record["legacy_authority_status"] == "MATCH"
        assert record["accepted_task_claims"] == [{
            "id": "T1",
            "task_spec_sha256": rows["T1"]["task_spec_sha256"],
            "accepted_product_sha": head,
            "status": "ELIGIBLE_FOR_REATTESTATION",
            "resolved_accepted_product_sha": head,
        }]
        assert record["active_task_claim"]["status"] == "DEPENDENCY_NOT_READY"
        assert record["active_task_claim"]["blockers"]
        assert record["legacy_blockers"] == ["awaiting external approval"]
        assert record["legacy_reservations"] == ["legacy-worker-1"]

        after = load_json(state_path, {})
        for key, value in before_authority.items():
            assert after.get(key) == value
        assert after["legacy_adoption"]["adoption_sha256"] == record[
            "adoption_sha256"
        ]

        readiness = task_readiness(root)
        assert readiness["T1"]["status"] == "READY"
        assert readiness["T2"]["status"] == "BLOCKED"

        adoption_dir = repo_state_dir(root) / "adoption"
        assert (adoption_dir / "current.json").is_file()
        assert (
            adoption_dir / "history" / f"{record['adoption_sha256']}.json"
        ).is_file()


def test_p6_imported_root_task_maps_only_when_currently_ready(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1")])
        row = _task_rows(task_set)["T1"]
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()

        doc = _write_document(
            Path(td) / "active.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "active-001",
                "product_sha": head,
                "active_task": {
                    "id": "T1",
                    "task_spec_sha256": row["task_spec_sha256"],
                    "base_sha": head,
                },
                "accepted_tasks": [],
                "blockers": [],
                "reservations": [],
            },
        )
        record = import_legacy_state_claims(root, doc)
        assert record["active_task_claim"]["status"] == "MAPPABLE_CURRENT_TASK"
        assert record["active_task_claim"]["resolved_base_sha"] == head


def test_p6_adoption_classifies_unknown_mismatch_and_unavailable_claims(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1"), _task("T2")])
        rows = _task_rows(task_set)
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()

        doc = _write_document(
            Path(td) / "mismatch.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "mismatch-001",
                "product_sha": "f" * 40,
                "active_task": None,
                "accepted_tasks": [
                    {
                        "id": "UNKNOWN",
                        "task_spec_sha256": "0" * 64,
                        "accepted_product_sha": head,
                    },
                    {
                        "id": "T1",
                        "task_spec_sha256": "1" * 64,
                        "accepted_product_sha": head,
                    },
                    {
                        "id": "T2",
                        "task_spec_sha256": rows["T2"]["task_spec_sha256"],
                        "accepted_product_sha": "e" * 40,
                    },
                ],
                "blockers": [],
                "reservations": [],
            },
        )
        record = import_legacy_state_claims(root, doc)
        statuses = {
            row["id"]: row["status"]
            for row in record["accepted_task_claims"]
        }
        assert statuses == {
            "UNKNOWN": "UNKNOWN_TASK_ID",
            "T1": "TASK_SPEC_MISMATCH",
            "T2": "ACCEPTED_SHA_UNAVAILABLE",
        }
        assert record["legacy_product_relationship"]["status"] == "UNAVAILABLE"


def test_p6_imported_active_task_stale_base_is_not_mappable(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        first = _configure(root, [_task("T1")])
        row = _task_rows(first)["T1"]
        old_head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()

        (root / "README.md").write_text("new product head\n")
        _run(root, "git", "add", "README.md")
        _run(root, "git", "commit", "-qm", "advance product")
        with SupervisorLease(repo_state_dir(root), root):
            activate(root)
            resolve_task_sources(root, persist=True)
        new_head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        assert new_head != old_head

        doc = _write_document(
            Path(td) / "stale.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "stale-001",
                "product_sha": old_head,
                "active_task": {
                    "id": "T1",
                    "task_spec_sha256": row["task_spec_sha256"],
                    "base_sha": old_head,
                },
                "accepted_tasks": [],
                "blockers": [],
                "reservations": [],
            },
        )
        record = import_legacy_state_claims(root, doc)
        assert record["legacy_product_relationship"]["status"] == "ANCESTOR"
        assert record["active_task_claim"]["status"] == "STALE_BASE"
        assert record["active_task_claim"]["current_product_sha"] == new_head


def test_p6_adoption_input_rejects_symlink_oversize_and_non_utf8(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        _configure(root, [_task("T1")])

        target = Path(td) / "target.json"
        _write_document(
            target,
            {
                "schema_version": 1,
                "source_system": "legacy",
                "source_state_id": "x",
                "accepted_tasks": [],
            },
        )
        link = Path(td) / "link.json"
        link.symlink_to(target)
        with pytest.raises(StateAdoptionError, match="non-symlink"):
            import_legacy_state_claims(root, link)

        oversized = Path(td) / "oversized.json"
        oversized.write_bytes(b"x" * (MAX_ADOPTION_BYTES + 1))
        with pytest.raises(StateAdoptionError, match="exceeds"):
            import_legacy_state_claims(root, oversized)

        invalid = Path(td) / "invalid.json"
        invalid.write_bytes(b"\xff\xfe")
        with pytest.raises(StateAdoptionError, match="UTF-8"):
            import_legacy_state_claims(root, invalid)


def test_p6_adoption_import_is_deterministic_for_same_document(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        _configure(root, [_task("T1")])
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        doc = _write_document(
            Path(td) / "same.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "same-001",
                "product_sha": head,
                "accepted_tasks": [],
                "blockers": [],
                "reservations": [],
            },
        )
        first = import_legacy_state_claims(root, doc)
        second = import_legacy_state_claims(root, doc)
        assert second["adoption_sha256"] == first["adoption_sha256"]
        assert second["imported_at"] == first["imported_at"]
        history = list(
            (repo_state_dir(root) / "adoption" / "history").glob("*.json")
        )
        assert len(history) == 1


def test_p6_imported_accepted_task_prepares_noop_p4_reattestation(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1")])
        row = _task_rows(task_set)["T1"]
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        doc = _write_document(
            Path(td) / "accepted.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "accepted-001",
                "product_sha": head,
                "accepted_tasks": [{
                    "id": "T1",
                    "task_spec_sha256": row["task_spec_sha256"],
                    "accepted_product_sha": head,
                }],
                "blockers": [],
                "reservations": [],
            },
        )
        adoption = import_legacy_state_claims(root, doc)
        before = load_json(repo_state_dir(root) / "state.json", {})
        assert before["accepted_tasks"] == {}

        prepared = begin_adopted_task_reattestation(root, "T1")
        assert prepared["status"] == "CANDIDATE"
        assert prepared["candidate_sha"] == head
        assert prepared["no_op"] is True
        assert prepared["next_gate"] == "deterministic-verification"
        assert prepared["adoption_sha256"] == adoption["adoption_sha256"]

        workspace = load_active_task_workspace(root)
        assert workspace is not None
        assert workspace["task_id"] == "T1"
        assert workspace["lifecycle_state"] == "CANDIDATE"
        assert workspace["no_op_candidate"] is True
        assert workspace["adoption_mode"] == "accepted-claim-reattestation"
        assert workspace["adoption_sha256"] == adoption["adoption_sha256"]
        assert workspace["legacy_accepted_product_sha"] == head

        after = load_json(repo_state_dir(root) / "state.json", {})
        # Preparing re-attestation must not itself create current acceptance.
        assert after["accepted_tasks"] == {}


def test_p6_imported_active_task_maps_to_clean_p4_workspace(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1")])
        row = _task_rows(task_set)["T1"]
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()

        # Unrelated primary WIP is allowed and remains in the primary checkout.
        (root / "notes.local").write_text("unrelated human WIP\n")
        doc = _write_document(
            Path(td) / "active-map.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "active-map-001",
                "product_sha": head,
                "active_task": {
                    "id": "T1",
                    "task_spec_sha256": row["task_spec_sha256"],
                    "base_sha": head,
                },
                "accepted_tasks": [],
                "blockers": [],
                "reservations": [],
            },
        )
        adoption = import_legacy_state_claims(root, doc)
        mapped = begin_adopted_active_task(root)
        assert mapped["task_id"] == "T1"
        assert mapped["lifecycle_state"] == "ACTIVE"
        assert mapped["adoption_mode"] == "active-task-map"
        assert mapped["adoption_sha256"] == adoption["adoption_sha256"]
        task_root = Path(mapped["task_worktree"])
        assert task_root.is_dir()
        assert not (task_root / "notes.local").exists()
        assert (root / "notes.local").read_text() == "unrelated human WIP\n"


def test_p6_imported_active_task_with_owned_primary_wip_requires_explicit_adoption(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1")])
        row = _task_rows(task_set)["T1"]
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        owned = root / "src" / "T1" / "work.py"
        owned.parent.mkdir(parents=True)
        owned.write_text("legacy active task WIP\n")

        doc = _write_document(
            Path(td) / "active-wip.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "active-wip-001",
                "product_sha": head,
                "active_task": {
                    "id": "T1",
                    "task_spec_sha256": row["task_spec_sha256"],
                    "base_sha": head,
                },
                "accepted_tasks": [],
                "blockers": [],
                "reservations": [],
            },
        )
        import_legacy_state_claims(root, doc)
        before = owned.read_bytes()

        with pytest.raises(StateAdoptionError, match="explicit P6 WIP adoption"):
            begin_adopted_active_task(root)

        assert owned.read_bytes() == before
        assert load_active_task_workspace(root) is None
