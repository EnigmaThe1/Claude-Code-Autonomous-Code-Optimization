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
from cli_schema import build_parser
import claude_auto
from repo_identity import SupervisorLease, repo_state_dir
from repo_runtime import activate
from state_adoption import (
    MAX_ADOPTION_BYTES,
    StateAdoptionError,
    adopt_active_task_primary_wip,
    begin_adopted_active_task,
    begin_adopted_task_reattestation,
    import_legacy_state_claims,
    load_current_adoption,
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


def test_p6_explicit_wip_adoption_preserves_bytes_modes_deletions_and_primary(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        existing = root / "src" / "T1" / "existing.py"
        deleted = root / "src" / "T1" / "delete.py"
        existing.parent.mkdir(parents=True)
        existing.write_text("print('base')\n")
        deleted.write_text("delete me\n")
        _run(root, "git", "add", "src/T1")
        _run(root, "git", "commit", "-qm", "task source baseline")

        task_set = _configure(root, [_task("T1")])
        row = _task_rows(task_set)["T1"]
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        doc = _write_document(
            Path(td) / "wip-adopt.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "wip-001",
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

        existing.write_text("print('legacy progress')\n")
        existing.chmod(0o755)
        deleted.unlink()
        new_file = root / "src" / "T1" / "new.bin"
        new_file.write_bytes(b"\x00legacy-progress\xff")
        new_file.chmod(0o640)
        scratch = root / ".scratch" / "T1" / "cache.bin"
        scratch.parent.mkdir(parents=True)
        scratch.write_bytes(b"non-durable scratch")

        primary_existing = existing.read_bytes()
        primary_existing_mode = existing.stat().st_mode & 0o777
        primary_new = new_file.read_bytes()
        primary_new_mode = new_file.stat().st_mode & 0o777
        primary_scratch = scratch.read_bytes()

        result = adopt_active_task_primary_wip(root)
        assert result["status"] == "ACTIVE"
        assert result["task_id"] == "T1"
        assert result["adopted_paths"] == [
            "src/T1/delete.py",
            "src/T1/existing.py",
            "src/T1/new.bin",
        ]
        assert result["ignored_scratch_paths"] == [
            ".scratch/T1/cache.bin"
        ]

        task_root = Path(result["task_worktree"])
        assert (task_root / "src/T1/existing.py").read_bytes() == primary_existing
        assert (
            (task_root / "src/T1/existing.py").stat().st_mode & 0o777
        ) == primary_existing_mode
        assert not (task_root / "src/T1/delete.py").exists()
        assert (task_root / "src/T1/new.bin").read_bytes() == primary_new
        assert (
            (task_root / "src/T1/new.bin").stat().st_mode & 0o777
        ) == primary_new_mode
        assert not (task_root / ".scratch/T1/cache.bin").exists()

        # Primary checkout remains exactly the dirty legacy source of truth.
        assert existing.read_bytes() == primary_existing
        assert (existing.stat().st_mode & 0o777) == primary_existing_mode
        assert not deleted.exists()
        assert new_file.read_bytes() == primary_new
        assert (new_file.stat().st_mode & 0o777) == primary_new_mode
        assert scratch.read_bytes() == primary_scratch

        workspace = load_active_task_workspace(root)
        assert workspace is not None
        assert workspace["adoption_mode"] == "active-task-wip"
        assert workspace["adoption_wip_sha256"] == result[
            "wip_adoption_sha256"
        ]
        assert workspace["adoption_ignored_scratch_paths"] == [
            ".scratch/T1/cache.bin"
        ]
        assert not (
            repo_state_dir(root) / "adoption" / "wip-active.json"
        ).exists()
        assert (
            repo_state_dir(root)
            / "adoption"
            / "wip-history"
            / f"{result['wip_adoption_sha256']}.json"
        ).is_file()


def test_p6_wip_adoption_blocks_unrelated_primary_changes_without_creating_workspace(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1")])
        row = _task_rows(task_set)["T1"]
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        doc = _write_document(
            Path(td) / "wip-block.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "wip-block-001",
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

        owned = root / "src" / "T1" / "work.py"
        owned.parent.mkdir(parents=True)
        owned.write_text("legacy work\n")
        unrelated = root / "README.md"
        unrelated.write_text("unrelated primary edit\n")
        owned_before = owned.read_bytes()
        unrelated_before = unrelated.read_bytes()

        with pytest.raises(StateAdoptionError, match="out-of-envelope WIP"):
            adopt_active_task_primary_wip(root)

        assert owned.read_bytes() == owned_before
        assert unrelated.read_bytes() == unrelated_before
        assert load_active_task_workspace(root) is None
        assert not (
            repo_state_dir(root) / "adoption" / "wip-active.json"
        ).exists()


def test_p6_wip_adoption_refuses_symlink_task_progress(monkeypatch):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1")])
        row = _task_rows(task_set)["T1"]
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        doc = _write_document(
            Path(td) / "wip-symlink.json",
            {
                "schema_version": 1,
                "source_system": "legacy-harness",
                "source_state_id": "wip-symlink-001",
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

        outside = Path(td) / "outside.txt"
        outside.write_text("outside\n")
        link = root / "src" / "T1" / "link.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)

        with pytest.raises(StateAdoptionError, match="symlink"):
            adopt_active_task_primary_wip(root)

        assert link.is_symlink()
        assert outside.read_text() == "outside\n"
        assert load_active_task_workspace(root) is None


def test_p6_migrate_cli_parser_exposes_bounded_adoption_actions():
    parser = build_parser("test")
    cases = (
        (["migrate", "status"], "status"),
        (["migrate", "state"], "state"),
        (["migrate", "planning-repair"], "planning-repair"),
        (["migrate", "adopt-state", "--from", "legacy.json"], "adopt-state"),
        (["migrate", "reattest", "T1"], "reattest"),
        (["migrate", "adopt-active"], "adopt-active"),
        (["migrate", "adopt-wip"], "adopt-wip"),
    )
    for argv, expected in cases:
        args = parser.parse_args(argv)
        assert args.command == "migrate"
        assert args.migrate_command == expected


def test_p6_migrate_adopt_state_cli_routes_real_claim_import(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        task_set = _configure(root, [_task("T1")])
        head = _run(root, "git", "rev-parse", "HEAD").stdout.strip()
        snapshot = build_authority_snapshot(root)
        assert snapshot is not None

        doc = _write_document(
            Path(td) / "legacy-cli.json",
            {
                "schema_version": 1,
                "source_system": "legacy-cli-test",
                "source_state_id": "cli-state-001",
                "product_sha": head,
                "verified_through_sha": head,
                "authority_snapshot_sha256": snapshot["snapshot_sha256"],
                "active_task": None,
                "accepted_tasks": [],
                "blockers": [],
                "reservations": [],
            },
        )
        monkeypatch.setattr(
            claude_auto,
            "require_top_level_operator",
            lambda _root, _operation: None,
        )
        parser = build_parser("test")
        args = parser.parse_args([
            "migrate",
            "adopt-state",
            "--repo",
            str(root),
            "--from",
            str(doc),
        ])

        rc = claude_auto._p6_migration_cli_action(args)
        assert rc == 0
        output = json.loads(capsys.readouterr().out)
        assert output["source_system"] == "legacy-cli-test"
        assert output["current_task_source_set_sha256"] == task_set[
            "task_source_set_sha256"
        ]

        current = load_current_adoption(root)
        assert current["adoption_sha256"] == output["adoption_sha256"]
        assert current["source_state_id"] == "cli-state-001"


def test_p6_migrate_status_is_read_only_for_absent_adoption_state(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        assert not (sd / "adoption").exists()
        assert not (sd / "session-adoption").exists()

        args = build_parser("test").parse_args([
            "migrate",
            "status",
            "--repo",
            str(root),
        ])
        rc = claude_auto._p6_migration_cli_action(args)
        assert rc == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "READY"
        assert output["legacy_adoption"] is None
        assert output["session_adoption"] is None
        assert not (sd / "adoption").exists()
        assert not (sd / "session-adoption").exists()


def test_p6_migrate_status_reports_corrupt_session_adoption_instead_of_hiding(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("CLAUDE_AUTONOMY_HOME", home)
        root = _repo(Path(td) / "repo")
        sd = repo_state_dir(root)
        path = sd / "session-adoption" / "active.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_version":1,"corrupt":true}\n')

        args = build_parser("test").parse_args([
            "migrate",
            "status",
            "--repo",
            str(root),
        ])
        rc = claude_auto._p6_migration_cli_action(args)
        assert rc == 2
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "BLOCKED"
        assert "SessionAdoptionRecord" in output["error"]
        assert (
            "semantic integrity check failed" in output["error"]
            or "missing semantic field" in output["error"]
        )
